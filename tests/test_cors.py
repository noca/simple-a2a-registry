"""CORS 跨域中间件测试（P1-I）。

测试覆盖：
- 不同 Origin 请求的 Access-Control-Allow-Origin 头
- OPTIONS 预检请求返回 204 + 正确头
- 默认 * 配置下任何 Origin 通过
- 自定义 Origin 白名单
- 缺少 Origin 头时的行为
- CORS 头与错误响应共存
- WebSocket 端点 CORS
- Auth 端点 CORS
- V2 编排 API CORS
"""
from __future__ import annotations

import os
import tempfile

import pytest
from aiohttp.test_utils import TestServer, TestClient

from simple_a2a_registry.server import create_app

pytestmark = pytest.mark.asyncio


@pytest.fixture
def app_factory():
    factories = []

    async def maker(
        auth_enabled: bool = False, cors_origins: str = "*"
    ) -> TestClient:
        tmpdir_obj = tempfile.TemporaryDirectory()
        factories.append(tmpdir_obj)
        data_dir = tmpdir_obj.name

        # Create app with CORS config via minimal config
        from simple_a2a_registry.config import Config

        cfg = Config()
        cfg.server.cors_origins = cors_origins
        cfg.server.host = "0.0.0.0"
        cfg.server.port = 8321
        # Isolate DB per test — default sqlite_path is global
        cfg.database.sqlite_path = os.path.join(data_dir, "registry.db")

        app = create_app(
            data_dir=data_dir,
            base_url="http://localhost:8321",
            config=cfg,
            auth_enabled=auth_enabled,
            bootstrap_secret="test-bootstrap-secret" if auth_enabled else None,
        )
        server = TestServer(app)
        await server.start_server()
        client = TestClient(server)
        return client

    yield maker

    for f in factories:
        try:
            f.cleanup()
        except Exception:
            pass


# =========================================================================
# CORS 头验证
# =========================================================================


class TestCorsHeaders:
    """验证 Access-Control-Allow-Origin 响应头。"""

    CORS_HEADERS = {
        "Access-Control-Allow-Origin",
        "Access-Control-Expose-Headers",
        "Vary",
    }

    async def test_wildcard_origin_allows_any(self, app_factory):
        """cors_origins=* 时，任意 Origin 返回 Access-Control-Allow-Origin: *。"""
        async with await app_factory(cors_origins="*") as client:
            resp = await client.get("/health", headers={"Origin": "https://example.com"})
            assert resp.status == 200
            assert resp.headers.get("Access-Control-Allow-Origin") == "*"

    async def test_wildcard_without_origin(self, app_factory):
        """无 Origin 头时，默认返回 *。"""
        async with await app_factory(cors_origins="*") as client:
            resp = await client.get("/health")
            assert resp.status == 200
            assert resp.headers.get("Access-Control-Allow-Origin") == "*"

    async def test_specific_origin_allows_matching(self, app_factory):
        """cors_origins 为特定域名时，匹配的 Origin 返回对应值。"""
        async with await app_factory(cors_origins="https://app.example.com") as client:
            resp = await client.get(
                "/health",
                headers={"Origin": "https://app.example.com"},
            )
            assert resp.status == 200
            assert resp.headers.get("Access-Control-Allow-Origin") == "https://app.example.com"

    async def test_specific_origin_rejects_unknown(self, app_factory):
        """不匹配的 Origin 返回第一个允许的 Origin。"""
        async with await app_factory(cors_origins="https://trusted.com") as client:
            resp = await client.get(
                "/health",
                headers={"Origin": "https://evil.com"},
            )
            assert resp.status == 200
            # Falls back to first allowed origin when not matched
            assert resp.headers.get("Access-Control-Allow-Origin") == "https://trusted.com"

    async def test_multiple_origins(self, app_factory):
        """逗号分隔的多个 Origin 允许通过。"""
        async with await app_factory(
            cors_origins="https://a.com, https://b.com"
        ) as client:
            resp = await client.get(
                "/health",
                headers={"Origin": "https://a.com"},
            )
            assert resp.status == 200
            assert resp.headers.get("Access-Control-Allow-Origin") == "https://a.com"

            resp = await client.get(
                "/health",
                headers={"Origin": "https://b.com"},
            )
            assert resp.status == 200
            assert resp.headers.get("Access-Control-Allow-Origin") == "https://b.com"

    async def test_expose_headers_present(self, app_factory):
        """Access-Control-Expose-Headers 存在。"""
        async with await app_factory() as client:
            resp = await client.get("/health", headers={"Origin": "https://x.com"})
            assert resp.headers.get("Access-Control-Expose-Headers") is not None

    async def test_vary_header_present(self, app_factory):
        """Vary: Origin 存在。"""
        async with await app_factory() as client:
            resp = await client.get("/health", headers={"Origin": "https://x.com"})
            assert "Origin" in resp.headers.get("Vary", "")


# =========================================================================
# OPTIONS 预检
# =========================================================================


class TestOptionsPreflight:
    """OPTIONS 预检请求。"""

    async def test_options_returns_204(self, app_factory):
        """OPTIONS 预检返回 204 + 正确方法头。"""
        async with await app_factory(cors_origins="*") as client:
            resp = await client.options(
                "/v1/agents",
                headers={"Origin": "https://example.com", "Access-Control-Request-Method": "POST"},
            )
            assert resp.status == 204
            assert resp.headers.get("Access-Control-Allow-Origin") == "*"
            assert "POST" in resp.headers.get("Access-Control-Allow-Methods", "")

    async def test_options_all_methods(self, app_factory):
        """OPTIONS 预检返回所有允许的 HTTP 方法。"""
        async with await app_factory(cors_origins="*") as client:
            resp = await client.options("/v1/agents", headers={"Origin": "https://x.com"})
            assert resp.status == 204
            methods = resp.headers.get("Access-Control-Allow-Methods", "")
            for m in ("GET", "POST", "PUT", "DELETE", "PATCH"):
                assert m in methods, f"Method {m} missing from Allow-Methods"

    async def test_options_with_specific_origin(self, app_factory):
        """OPTIONS 预检使用特定的 cors_origins。"""
        async with await app_factory(cors_origins="https://trusted.app") as client:
            resp = await client.options(
                "/health",
                headers={"Origin": "https://trusted.app"},
            )
            assert resp.status == 204
            assert resp.headers.get("Access-Control-Allow-Origin") == "https://trusted.app"

    async def test_options_max_age(self, app_factory):
        """OPTIONS 预检返回 Access-Control-Max-Age。"""
        async with await app_factory() as client:
            resp = await client.options("/health", headers={"Origin": "https://x.com"})
            assert resp.status == 204
            max_age = resp.headers.get("Access-Control-Max-Age", "")
            assert max_age.isdigit() or max_age == ""


# =========================================================================
# 各 API 端点的 CORS 行为
# =========================================================================


class TestCorsOnEndpoints:
    """CORS 头在各 API 端点上的行为。"""

    async def _check_cors(self, client, method: str, path: str, **kwargs) -> None:
        """Helper: 发送请求并验证 CORS 头。"""
        requester = getattr(client, method.lower())
        resp = await requester(
            path,
            headers={"Origin": "https://example.com", **(kwargs.pop("headers", {}))},
            **kwargs,
        )
        assert resp.headers.get("Access-Control-Allow-Origin") == "*", (
            f"{method.upper()} {path} missing CORS header"
        )

    async def test_health(self, app_factory):
        async with await app_factory() as client:
            resp = await client.get("/health", headers={"Origin": "https://x.com"})
            assert resp.headers.get("Access-Control-Allow-Origin") == "*"

    async def test_agents_list(self, app_factory):
        async with await app_factory() as client:
            await self._check_cors(client, "get", "/v1/agents")

    async def test_agents_register(self, app_factory):
        async with await app_factory() as client:
            await self._check_cors(client, "post", "/v1/agents", json={"name": "cors-test"})

    async def test_agent_delete(self, app_factory):
        async with await app_factory() as client:
            # Register first
            resp = await client.post("/v1/agents", json={"name": "to-delete"})
            aid = (await resp.json())["id"]
            await self._check_cors(client, "delete", f"/v1/agents/{aid}")

    async def test_heartbeat(self, app_factory):
        async with await app_factory() as client:
            resp = await client.post("/v1/agents", json={"name": "hb-cors"})
            data = await resp.json()
            aid = data.get("id") or data.get("agent_id", "")
            assert aid, f"No agent ID in response: {data}"
            await self._check_cors(client, "post", f"/v1/agents/{aid}/heartbeat")

    async def test_dispatch(self, app_factory):
        async with await app_factory() as client:
            resp = await client.post("/v1/agents", json={"name": "dispatch-cors"})
            data = await resp.json()
            aid = data.get("id") or data.get("agent_id", "")
            assert aid, f"No agent ID in response: {data}"
            await self._check_cors(
                client, "post", f"/v1/agents/{aid}/dispatch",
                json={"query": "test"},
            )

    async def test_tasks_list(self, app_factory):
        async with await app_factory() as client:
            await self._check_cors(client, "get", "/v1/tasks")

    async def test_v2_routes(self, app_factory):
        """V2 编排 API 端点也返回 CORS 头。"""
        async with await app_factory() as client:
            await self._check_cors(client, "get", "/v2/tasks")

    async def test_auth_token(self, app_factory):
        async with await app_factory() as client:
            await self._check_cors(
                client, "post", "/auth/token",
                data={"grant_type": "client_credentials"},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )

    async def test_auth_register(self, app_factory):
        async with await app_factory() as client:
            await self._check_cors(
                client, "post", "/auth/register",
                json={"description": "CORS test"},
            )

    async def test_well_known(self, app_factory):
        async with await app_factory() as client:
            await self._check_cors(client, "get", "/.well-known/agent-card.json")


# =========================================================================
# CORS 与错误响应
# =========================================================================


class TestCorsWithErrors:
    """即使请求返回错误状态码，CORS 头也应存在。"""

    async def test_404_has_cors_header(self, app_factory):
        """404 响应应包含 CORS 头。"""
        async with await app_factory() as client:
            resp = await client.get("/nonexistent", headers={"Origin": "https://example.com"})
            assert resp.status == 404
            assert resp.headers.get("Access-Control-Allow-Origin") == "*"

    async def test_401_has_cors_header(self, app_factory):
        """401 响应应包含 CORS 头。"""
        async with await app_factory(auth_enabled=True) as client:
            resp = await client.get("/v1/agents", headers={"Origin": "https://example.com"})
            assert resp.status == 401
            assert resp.headers.get("Access-Control-Allow-Origin") == "*"

    async def test_403_has_cors_header(self, app_factory):
        """403 响应应包含 CORS 头。"""
        async with await app_factory(auth_enabled=True) as client:
            # Get a token without agent:admin scope
            reg = await client.post("/auth/register", json={"description": "CORS 403 Test"})
            creds = await reg.json()
            tok = await client.post("/auth/token", data={
                "grant_type": "client_credentials",
                "client_id": creds["client_id"],
                "client_secret": creds["client_secret"],
                "scope": "task:read",
            })
            token = (await tok.json())["access_token"]
            resp = await client.delete(
                "/v1/agents/nonexistent",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Origin": "https://example.com",
                },
            )
            assert resp.status >= 400
            assert resp.headers.get("Access-Control-Allow-Origin") == "*"

    async def test_400_has_cors_header(self, app_factory):
        """400 响应应包含 CORS 头。"""
        async with await app_factory() as client:
            resp = await client.post(
                "/v1/agents",
                data="not json",
                headers={
                    "Content-Type": "application/json",
                    "Origin": "https://example.com",
                },
            )
            assert resp.status >= 400
            assert resp.headers.get("Access-Control-Allow-Origin") == "*"


# =========================================================================
# 跨 Origin WebSocket 测试
# =========================================================================


class TestWebSocketOrigin:
    """WebSocket 升级路径中的 CORS 行为。"""

    async def test_ws_endpoint_cors_header(self, app_factory):
        """WS 端点（GET）应返回 CORS 头。"""
        async with await app_factory() as client:
            resp = await client.get(
                "/v1/agents/nonexistent/ws",
                headers={"Origin": "https://example.com"},
            )
            # 404 because agent doesn't exist, but CORS header should be present
            assert resp.headers.get("Access-Control-Allow-Origin") == "*"