"""向后兼容测试 — 旧 token 无 tenant 字段视为默认租户

P6-A2-3: 验证旧版 JWT token（在 tenant 功能引入前签发）不包含 tenant
字段时，系统能够正确地将其视为默认租户（空字符串 ""），而不是报错
或被拒绝。

覆盖场景:
  1. Token 无 tenant 字段 → 视为默认租户（空字符串）
  2. Token 有 tenant="acme-corp" → 视为该租户
  3. 无 tenant 和 有 tenant 的 token 可以共存工作
  4. 旧 token 可以通过认证并正常调用 API
"""
from __future__ import annotations

import json
import tempfile
import time

import pytest
from aiohttp.test_utils import TestServer, TestClient

from simple_a2a_registry.server import create_app
from simple_a2a_registry.store import Store
from simple_a2a_registry.auth import (
    create_token,
    SCOPES,
    ISSUER,
    TOKEN_EXPIRY_SECONDS,
)

pytestmark = pytest.mark.asyncio

TENANT_A = "acme-corp"
TENANT_DEFAULT = ""  # 默认租户 = 空字符串


# ======================================================================
# Fixtures
# ======================================================================


@pytest.fixture(scope="module")
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def app_with_auth():
    """创建一个启用了 OAuth 认证的测试 app。

    create_app(auth_enabled=True) 内部生成 RS256 key pair，
    测试函数通过 app["auth_handler"].private_key 获取私钥用于签发 token。
    """
    tmpdir_obj = tempfile.TemporaryDirectory()
    data_dir = tmpdir_obj.name

    # 启动 app，启用 auth
    app = create_app(
        data_dir=data_dir,
        base_url="http://localhost:8321",
        auth_enabled=True,
    )
    store: Store = app["store"]
    auth_handler = app["auth_handler"]

    server = TestServer(app)
    await server.start_server()
    client = TestClient(server)

    # 注册一个测试 client 用于签发 token
    client_info = store.register_client(
        agent_card_id="test-agent",
        allowed_scopes=list(SCOPES.keys()),
        description="Test client for tenant BC tests",
    )

    result = {
        "client": client,
        "store": store,
        "auth_handler": auth_handler,
        "private_key": auth_handler.private_key,
        "client_id": client_info["client_id"],
        "client_secret": client_info["client_secret"],
        "tmpdir": tmpdir_obj,
    }

    yield result

    await client.close()
    await server.close()
    tmpdir_obj.cleanup()


# ======================================================================
# Helper functions
# ======================================================================


def _make_token(
    sub: str,
    private_key: str,
    *,
    tenant: str | None = None,
    scope: str | None = None,
) -> str:
    """签发 JWT token，tenant 参数控制是否包含 tenant 字段。

    Args:
        sub: 主题（client_id）
        private_key: RSA 私钥 PEM
        tenant: None=不包含 tenant 字段，""=tenant 字段为空字符串
        scope: 可选的 scope 字符串
    """
    return create_token(
        sub=sub,
        private_key=private_key,
        algorithm="RS256",
        scope=scope or " ".join(SCOPES.keys()),
        tenant=tenant,  # None → 不写入 payload, "" → payload["tenant"]=""
    )


async def _register_with_token(
    client: TestClient, token: str, name: str, *, tenant: str = ""
) -> dict:
    """使用 Bearer token 注册 agent。"""
    card = {
        "name": name,
        "description": f"Agent {name}",
        "interfaces": [{
            "url": f"https://agent.{name.lower()}.example.com",
            "protocol_binding": "JSONRPC",
            "protocol_version": "1.0",
        }],
    }
    if tenant:
        card["tenant"] = tenant

    resp = await client.post(
        "/v1/agents",
        json=card,
        headers={"Authorization": f"Bearer {token}"},
    )
    return {"status": resp.status, "body": await resp.json()}


async def _list_with_token(
    client: TestClient, token: str, *, tenant: str | None = None
) -> dict:
    """使用 Bearer token 列出 agent。"""
    params = {}
    if tenant is not None:
        params["tenant"] = tenant

    resp = await client.get(
        "/v1/agents",
        params=params,
        headers={"Authorization": f"Bearer {token}"},
    )
    return {"status": resp.status, "body": await resp.json()}


# ======================================================================
# Tests
# ======================================================================


class TestTokenWithoutTenantField:
    """旧 token 无 tenant 字段 → 视为默认租户。"""

    async def test_token_without_tenant_is_accepted(self, app_with_auth):
        """Token 不含 tenant 字段 → 能通过认证。"""
        ctx = app_with_auth

        # 签发一个不含 tenant 字段的 token（模拟旧 token）
        token = _make_token(
            sub=ctx["client_id"],
            private_key=ctx["private_key"],
        )

        # 用 token 访问受保护端点（list agents）
        result = await _list_with_token(ctx["client"], token)
        assert result["status"] == 200, (
            f"旧 token 应通过认证，但返回 {result['status']}: {result['body']}"
        )

    async def test_token_without_tenant_registers_to_default_tenant(
        self, app_with_auth,
    ):
        """用无 tenant 字段的 token 注册 agent → agent 属于默认租户（空字符串）。"""
        ctx = app_with_auth

        # 签发旧 token（无 tenant）
        token = _make_token(
            sub=ctx["client_id"],
            private_key=ctx["private_key"],
        )

        # 注册 agent（不指定 tenant）
        reg = await _register_with_token(
            ctx["client"], token, "LegacyBot",
        )
        assert reg["status"] == 201, f"注册失败: {reg['body']}"
        agent_id = reg["body"]["id"]

        # 验证 response card 没有 tenant 字段（空字符串不写入 card_json）
        card = reg["body"]["card"]
        tenant = card.get("tenant")
        assert tenant is None, (
            f"空字符串 tenant 不应写入 card_json，"
            f"实际为 {tenant!r}"
        )

        # 通过 store 直接验证 DB 中的 tenant_id 是空字符串（默认租户）
        db_card = ctx["store"].get_agent(agent_id)
        assert db_card is not None

        # 直接查询数据库验证 tenant_id
        with ctx["store"]._tx("DEFERRED") as engine:
            row = engine.execute(
                "SELECT tenant_id FROM agents WHERE id=?", (agent_id,)
            ).fetchone()
        assert row is not None, f"Agent {agent_id} not found in DB"
        db_tenant = row["tenant_id"]
        assert db_tenant == "", (
            f"DB 中 agent 的 tenant_id 应为空字符串（默认租户），"
            f"实际为 {db_tenant!r}"
        )

    async def test_token_with_and_without_tenant_coexist(self, app_with_auth):
        """无 tenant token 和有 tenant token 都可以通过认证并正常工作。"""
        ctx = app_with_auth

        # 1) 旧 token（无 tenant）
        old_token = _make_token(
            sub=ctx["client_id"],
            private_key=ctx["private_key"],
        )

        # 2) 新 token（有 tenant）
        new_token = _make_token(
            sub=ctx["client_id"],
            private_key=ctx["private_key"],
            tenant=TENANT_A,
        )

        # 两种 token 都能通过认证
        old_result = await _list_with_token(ctx["client"], old_token)
        assert old_result["status"] == 200

        new_result = await _list_with_token(ctx["client"], new_token)
        assert new_result["status"] == 200

        # 用旧 token 注册一个默认租户的 agent
        reg_old = await _register_with_token(
            ctx["client"], old_token, "DefaultBot",
        )
        assert reg_old["status"] == 201

        # 用新 token 注册一个指定租户的 agent
        reg_new = await _register_with_token(
            ctx["client"], new_token, "TenantBot",
            tenant=TENANT_A,
        )
        assert reg_new["status"] == 201

        # 无 tenant 过滤 → 应看到 2 个 agent（向后兼容行为）
        all_list = await _list_with_token(ctx["client"], old_token)
        assert all_list["status"] == 200
        assert len(all_list["body"]["agents"]) == 2, (
            f"应看到 2 个 agent，实际 {len(all_list['body']['agents'])}"
        )

        # 旧 token 的 tenant="" 等同于 "显示全部"（向后兼容设计）
        # 见 list_agents: tenant="" 视为跳过过滤
        with_tenant_param = await _list_with_token(
            ctx["client"], old_token, tenant="",
        )
        assert with_tenant_param["status"] == 200

        # 指定 tenant 过滤 → 应看到 1 个
        a_list = await _list_with_token(
            ctx["client"], old_token, tenant=TENANT_A,
        )
        assert a_list["status"] == 200
        assert len(a_list["body"]["agents"]) == 1
        assert a_list["body"]["agents"][0]["name"] == "TenantBot"

    async def test_create_token_does_not_set_empty_tenant(self, app_with_auth):
        """create_token 不会把空字符串 tenant 写入 JWT payload。

        验证：当 tenant="" 时，JWT 中应不包含 tenant 字段（而不是 tenant:""），
        这样才能与旧 token 行为一致。
        """
        ctx = app_with_auth

        # 签发 tenant="" 的 token
        token = _make_token(
            sub=ctx["client_id"],
            private_key=ctx["private_key"],
            tenant="",
        )

        # 解码 JWT 检查 payload
        import jwt
        payload = jwt.decode(token, options={"verify_signature": False})

        # tenant 字段应不存在（而不是 tenant:""）
        assert "tenant" not in payload, (
            f"tenant='' 时 payload 不应包含 tenant 字段，"
            f"实际为 {payload.get('tenant')!r}"
        )

        # 使用此 token 应正常工作
        result = await _list_with_token(ctx["client"], token)
        assert result["status"] == 200

    async def test_old_token_register_with_explicit_tenant_in_body(
        self, app_with_auth,
    ):
        """旧 token（无 tenant）注册时，若 body 包含 tenant，应使用 body 中的 tenant。

        这是通过 store.register_agent 的 tenant 参数或 card 中的 tenant 字段决定的，
        不是从 token 中提取的。token 仅用于认证，不决定 agent 的 tenant 归属。
        """
        ctx = app_with_auth

        # 旧 token（无 tenant）
        token = _make_token(
            sub=ctx["client_id"],
            private_key=ctx["private_key"],
        )

        # 注册时在 body 中指定 tenant
        reg = await _register_with_token(
            ctx["client"], token, "ExplicitTenantBot",
            tenant=TENANT_A,
        )
        assert reg["status"] == 201
        card = reg["body"]["card"]
        assert card.get("tenant") == TENANT_A, (
            f"body 指定 tenant 时 agent 应属于 {TENANT_A}，"
            f"实际为 {card.get('tenant')!r}"
        )


class TestTokenWithTenantField:
    """新 token 有 tenant 字段 → 按 tenant 隔离。"""

    async def test_token_with_tenant_is_accepted(self, app_with_auth):
        """Token 含 tenant 字段 → 能通过认证。"""
        ctx = app_with_auth
        token = _make_token(
            sub=ctx["client_id"],
            private_key=ctx["private_key"],
            tenant=TENANT_A,
        )
        result = await _list_with_token(ctx["client"], token)
        assert result["status"] == 200

    async def test_token_with_tenant_works_with_tenant_filter(self, app_with_auth):
        """新 token + tenant 参数配合使用。"""
        ctx = app_with_auth

        token = _make_token(
            sub=ctx["client_id"],
            private_key=ctx["private_key"],
            tenant=TENANT_A,
            scope="agent:register agent:read",
        )

        # 注册 agent
        reg = await _register_with_token(
            ctx["client"], token, "AcmeBot",
            tenant=TENANT_A,
        )
        assert reg["status"] == 201

        # 列出 agent（带 tenant 过滤）
        result = await _list_with_token(
            ctx["client"], token, tenant=TENANT_A,
        )
        assert result["status"] == 200
        assert len(result["body"]["agents"]) == 1


class TestTokenEdgeCases:
    """Token 边界场景。"""

    async def test_expired_token_rejected(self, app_with_auth):
        """已过期的 token 应被拒绝。"""
        ctx = app_with_auth

        # 签发已过期的 token
        import time as t_module
        past_time = int(t_module.time()) - 3600

        # 直接构造已过期的 payload
        import jwt as jwt_lib
        payload = {
            "iss": ISSUER,
            "sub": ctx["client_id"],
            "iat": past_time - 10,
            "exp": past_time,
            "jti": "expired-test-jti",
            "scope": " ".join(SCOPES.keys()),
        }
        expired_token = jwt_lib.encode(
            payload, ctx["private_key"], algorithm="RS256",
        )

        result = await _list_with_token(ctx["client"], expired_token)
        assert result["status"] == 401, (
            f"过期 token 应返回 401，实际 {result['status']}"
        )

    async def test_bad_token_rejected(self, app_with_auth):
        """无效 token 应被拒绝。"""
        ctx = app_with_auth
        result = await _list_with_token(
            ctx["client"], "Bearer this-is-garbage",
        )
        assert result["status"] == 401

    async def test_no_token_rejected(self, app_with_auth):
        """无 token 请求应被拒绝。"""
        ctx = app_with_auth
        resp = await ctx["client"].get("/v1/agents")
        assert resp.status == 401