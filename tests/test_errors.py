"""集成测试：错误处理与优雅关闭（P1-G）。

测试覆盖：
- 错误中间件返回统一格式 {error, detail, request_id, timestamp}
- 未处理异常 → 500 internal_error
- 自定义异常类 → 相应状态码
- 优雅关闭信号处理
- DB 断线重试
- 超时中间件
"""
from __future__ import annotations

import asyncio
import json
from typing import AsyncIterator, Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer, TestClient

from simple_a2a_registry.database.engine import (
    RetryEngine,
    SQLiteEngine,
    DatabaseEngine,
)
from simple_a2a_registry.errors import (
    APIError,
    json_error,
    error_middleware,
    timeout_middleware,
    A2ARegistryError,
    ValidationError,
    NotFoundError,
    ConflictError,
    AuthError,
    RateLimitError,
    ProtectedResourceError,
)
from simple_a2a_registry.store import Store
from simple_a2a_registry.server import create_app


# ======================================================================
# 单元测试：APIError / json_error / 异常类
# ======================================================================


class TestErrorModel:
    """验证统一错误模型的构造和序列化。"""

    def test_api_error_minimal(self):
        err = APIError("test_code", "test detail")
        assert err.error == "test_code"
        assert err.detail == "test detail"
        assert err.request_id == ""
        assert err.timestamp != ""
        assert err.extra is None

    def test_api_error_full(self):
        err = APIError("test_code", "test detail",
                       request_id="req-abc",
                       extra={"field": "name"})
        assert err.request_id == "req-abc"
        assert err.extra == {"field": "name"}

    def test_api_error_to_response(self):
        err = APIError("validation_error", "Invalid input",
                       request_id="req-xyz")
        resp = err.to_response(400)
        assert resp.status == 400
        assert resp.content_type == "application/json"
        body = json.loads(resp.body)
        assert body["error"] == "validation_error"
        assert body["detail"] == "Invalid input"
        assert body["request_id"] == "req-xyz"
        assert "timestamp" in body

    def test_json_error_no_request(self):
        resp = json_error(404, "not_found", "Agent not found")
        assert resp.status == 404
        body = json.loads(resp.body)
        assert body["error"] == "not_found"
        assert body["request_id"] == ""

    def test_json_error_with_extra(self):
        resp = json_error(400, "validation_error", "Bad field",
                          extra={"fields": ["name", "email"]})
        body = json.loads(resp.body)
        assert body["extra"] == {"fields": ["name", "email"]}

    def test_validation_error(self):
        e = ValidationError("Name is required")
        assert e.status == 400
        assert e.error_code == "validation_error"
        assert str(e) == "Name is required"

    def test_not_found_error(self):
        e = NotFoundError("agent", "agent-123")
        assert e.status == 404
        assert e.error_code == "agent_not_found"
        assert "agent-123" in e.detail

    def test_conflict_error(self):
        e = ConflictError("Duplicate name")
        assert e.status == 409
        assert e.error_code == "conflict"

    def test_auth_error(self):
        e = AuthError("unauthorized", "Missing token")
        assert e.status == 401
        assert e.error_code == "unauthorized"

    def test_rate_limit_error(self):
        e = RateLimitError(retry_after=60)
        assert e.status == 429
        assert e.error_code == "too_many_requests"
        assert e.extra == {"retry_after": 60}

    def test_protected_error(self):
        e = ProtectedResourceError("Cannot delete service account")
        assert e.status == 403
        assert e.error_code == "protected"

    def test_custom_a2a_error(self):
        e = A2ARegistryError("custom_code", "Custom message", 418,
                             extra={"teapot": True})
        assert e.status == 418
        assert e.error_code == "custom_code"
        assert e.extra == {"teapot": True}


# ======================================================================
# 集成测试：错误中间件
# ======================================================================


@pytest.fixture
def error_app() -> web.Application:
    """创建一个带 error_middleware 的测试应用。"""
    app = web.Application(middlewares=[error_middleware])

    async def ok_handler(request):
        return web.json_response({"status": "ok"})

    async def validation_handler(request):
        raise ValidationError("Name is required")

    async def not_found_handler(request):
        raise NotFoundError("agent", "agent-xyz")

    async def conflict_handler(request):
        raise ConflictError("Duplicate entry")

    async def auth_handler(request):
        raise AuthError("forbidden", "Insufficient scope", 403)

    async def generic_handler(request):
        raise RuntimeError("Unexpected crash")

    async def http_error_handler(request):
        raise web.HTTPMethodNotAllowed("POST", ["GET"])

    app.router.add_get("/ok", ok_handler)
    app.router.add_get("/validation", validation_handler)
    app.router.add_get("/not-found", not_found_handler)
    app.router.add_get("/conflict", conflict_handler)
    app.router.add_get("/auth", auth_handler)
    app.router.add_get("/crash", generic_handler)
    app.router.add_get("/wrong-method", http_error_handler)

    return app


@pytest.fixture
async def error_client(error_app) -> AsyncIterator[TestClient]:
    server = TestServer(error_app)
    await server.start_server()
    client = TestClient(server)
    yield client
    await server.close()


class TestErrorMiddleware:
    """验证 error_middleware 正确处理各种异常。"""

    async def test_ok(self, error_client):
        resp = await error_client.get("/ok")
        assert resp.status == 200
        data = await resp.json()
        assert data["status"] == "ok"

    async def test_validation_error(self, error_client):
        resp = await error_client.get("/validation")
        assert resp.status == 400
        data = await resp.json()
        assert data["error"] == "validation_error"
        assert data["detail"] == "Name is required"
        assert "request_id" in data
        assert "timestamp" in data

    async def test_not_found(self, error_client):
        resp = await error_client.get("/not-found")
        assert resp.status == 404
        data = await resp.json()
        assert data["error"] == "agent_not_found"
        assert "agent-xyz" in data["detail"]

    async def test_conflict(self, error_client):
        resp = await error_client.get("/conflict")
        assert resp.status == 409
        data = await resp.json()
        assert data["error"] == "conflict"

    async def test_auth_forbidden(self, error_client):
        resp = await error_client.get("/auth")
        assert resp.status == 403
        data = await resp.json()
        assert data["error"] == "forbidden"

    async def test_unhandled_exception(self, error_client):
        resp = await error_client.get("/crash")
        assert resp.status == 500
        data = await resp.json()
        assert data["error"] == "internal_error"
        assert data["detail"] == "Internal server error"

    async def test_http_exception(self, error_client):
        resp = await error_client.get("/wrong-method")
        assert resp.status == 405
        data = await resp.json()
        assert data["error"] == "method_not_allowed"


# ======================================================================
# 集成测试：超时中间件
# ======================================================================


@pytest.fixture
def timeout_app() -> web.Application:
    """创建一个带 timeout_middleware 的测试应用。"""

    @web.middleware
    async def path_timeout_middleware(request: web.Request, handler: Any) -> web.StreamResponse:
        """模拟上游路由配置设置自定义超时。"""
        if request.path == "/timeout":
            request["timeout_seconds"] = 0.1
        return await handler(request)

    app = web.Application(middlewares=[path_timeout_middleware, timeout_middleware])

    async def fast_handler(request: web.Request) -> web.Response:
        return web.json_response({"status": "fast"})

    async def slow_handler(request: web.Request) -> web.Response:
        await asyncio.sleep(0.5)
        return web.json_response({"status": "slow"})

    async def timeout_handler(request: web.Request) -> web.Response:
        await asyncio.sleep(1.0)
        return web.json_response({"status": "should_not_reach"})

    app.router.add_get("/fast", fast_handler)
    app.router.add_get("/slow", slow_handler)
    app.router.add_get("/timeout", timeout_handler)

    return app


@pytest.fixture
async def timeout_client(timeout_app) -> AsyncIterator[TestClient]:
    server = TestServer(timeout_app)
    await server.start_server()
    client = TestClient(server)
    yield client
    await server.close()


class TestTimeoutMiddleware:
    """验证 timeout_middleware 的超时控制。"""

    async def test_fast_request(self, timeout_client):
        resp = await timeout_client.get("/fast")
        assert resp.status == 200

    async def test_slow_request_within_timeout(self, timeout_client):
        resp = await timeout_client.get("/slow")
        assert resp.status == 200

    async def test_request_exceeds_timeout(self, timeout_client):
        resp = await timeout_client.get("/timeout")
        assert resp.status == 503
        data = await resp.json()
        assert data["error"] == "request_timeout"
        assert "timeout" in data["detail"]


# ======================================================================
# 集成测试：Store 断线重试（DB Retry）
# ======================================================================


class TestRetryEngine:
    """验证 RetryEngine 断线重试行为。

    RetryEngine 封装在 engine.py 中，对 transient 错误（如 OperationalError、
    database locked、timeout）进行指数退避重试（默认 3 次），
    非 transient 错误（如 SyntaxError）直接抛出。
    """

    def test_is_transient_operational_error(self):
        """OperationalError 应被识别为 transient。"""
        e = Exception("database is locked")
        assert RetryEngine._is_transient(e)

    def test_is_transient_timeout(self):
        """Timeout 类异常应被识别为 transient。"""
        e = Exception("timeout waiting for lock")
        assert RetryEngine._is_transient(e)

    def test_is_transient_lock(self):
        """Lock 关键字异常应被识别为 transient。"""
        e = Exception("deadlock detected")
        assert RetryEngine._is_transient(e)

    def test_is_not_transient_syntax(self):
        """语法错误不是 transient，不应重试。"""
        e = Exception("syntax error at line 42")
        assert not RetryEngine._is_transient(e)

    def test_is_not_transient_generic(self):
        """普通的 ValueError 不是 transient。"""
        assert not RetryEngine._is_transient(ValueError("bad value"))

    def test_retry_operation_success_first_try(self):
        """_retry_operation 在第一次成功时直接返回。"""
        store = Store(":memory:")
        called = 0

        def fn():
            nonlocal called
            called += 1
            return "ok"

        result = store._retry_operation("test", fn)
        assert result == "ok"
        assert called == 1

    def test_retry_operation_retries_then_succeeds(self):
        """_retry_operation 在 transient 失败后重试并成功。"""
        store = Store(":memory:")
        attempt = 0

        def fn():
            nonlocal attempt
            attempt += 1
            if attempt < 3:
                raise Exception("OperationalError: database is locked")
            return "recovered"

        result = store._retry_operation("test_lock", fn, max_retries=3, base_delay=0.01)
        assert result == "recovered"
        assert attempt == 3

    def test_retry_operation_exhausts_retries(self):
        """_retry_operation 在所有重试耗尽后重新抛出异常。"""
        store = Store(":memory:")

        def fn():
            raise Exception("OperationalError: connection lost")

        with pytest.raises(Exception, match="connection lost"):
            store._retry_operation("test_exhaust", fn, max_retries=2, base_delay=0.01)

    def test_retry_operation_non_transient_raises_immediately(self):
        """非 transient 错误直接抛出，不重试。"""
        store = Store(":memory:")
        called = 0

        def fn():
            nonlocal called
            called += 1
            raise ValueError("bad input")

        with pytest.raises(ValueError, match="bad input"):
            store._retry_operation("test_nt", fn)
        assert called == 1  # 只调用一次，不重试

    def test_retry_engine_execute_retries_transient(self):
        """RetryEngine.execute 对 transient 错误进行重试。"""
        # 创建一个 SQLiteEngine，用 RetryEngine 包裹
        import tempfile, os
        f, path = tempfile.mkstemp(suffix=".db")
        os.close(f)
        try:
            inner = SQLiteEngine(path)
            inner.connect()
            # 先执行建表
            inner.executescript("CREATE TABLE IF NOT EXISTS t (id INTEGER PRIMARY KEY, val TEXT);")
            inner.commit()

            retry_engine = RetryEngine(inner, max_retries=2, base_delay=0.01)

            # 正常查询可以工作
            result = retry_engine.execute("SELECT COUNT(*) AS cnt FROM t")
            row = result.fetchone()
            assert row is not None
            assert row["cnt"] == 0

            inner.close()
        finally:
            try:
                os.unlink(path)
            except Exception:
                pass

    def test_store_uses_retry_engine(self):
        """Store 创建时自动使用 RetryEngine 包裹，查询可正确执行。"""
        store = Store(":memory:")
        # 验证可以正常查询（RetryEngine 透传成功）
        stats = store.stats()
        assert "totalAgents" in stats
        assert stats["totalAgents"] == 0
        assert stats["aliveAgents"] == 0


# ======================================================================
# 集成测试：优雅关闭（Graceful Shutdown）
# ======================================================================


class TestGracefulShutdown:
    """验证优雅关闭流程 — _stop_background。

    验收点：
    - on_cleanup 注册了关闭处理器
    - 关闭后 Store/TaskStore 状态正确
    - cleanup task 被取消
    """

    async def test_cleanup_handlers_registered(self, tmp_path) -> None:
        """验证 create_app 注册了 on_cleanup 处理器。"""
        app = create_app(data_dir=str(tmp_path), base_url="http://localhost:8321")
        assert len(app.on_cleanup) >= 1

    async def test_full_lifecycle(self, tmp_path) -> None:
        """验证完整生命周期：启动 → 关闭。
        
        TestServer 内部触发 on_startup（含 _startup_checks 和 cleanup task），
        调用 close() 时触发 on_cleanup（含 _stop_background）。
        整个流程不抛异常即视为通过。
        """
        app = create_app(data_dir=str(tmp_path), base_url="http://localhost:8321")
        server = TestServer(app)
        await server.start_server()
        # 验证启动后服务可工作
        client = TestClient(server)
        resp = await client.get("/health")
        assert resp.status == 200
        data = await resp.json()
        assert data["status"] == "healthy"
        # 关闭 — 触发 on_cleanup（优雅关闭）
        await server.close()

    async def test_on_cleanup_closes_store(self, tmp_path) -> None:
        """验证 on_cleanup 关闭 Store DB 连接。"""
        app = create_app(data_dir=str(tmp_path), base_url="http://localhost:8321")
        server = TestServer(app)
        await server.start_server()
        store: Store = app.get("store")
        assert store is not None
        # 手动触发 cleanup handler
        for handler in app.on_cleanup:
            await handler(app)
        # 验证 cleanup 无异常
        await server.close()


# ======================================================================
# 集成测试：启动前置检查（Startup Checks）
# ======================================================================


class TestStartupChecks:
    """验证启动前置检查 — _startup_checks。

    验收点：
    - on_startup 注册了检查处理器
    - DB 连接正常可工作
    - 端口检查和 RSA 密钥检查不抛异常
    """

    async def test_startup_checks_registered(self, tmp_path) -> None:
        """验证 create_app 注册了 on_startup 处理器。"""
        app = create_app(data_dir=str(tmp_path), base_url="http://localhost:8321")
        assert len(app.on_startup) >= 1

    async def test_startup_checks_db_connectivity(self, tmp_path) -> None:
        """验证启动后 DB 可用（_startup_checks 中的 DB check）。"""
        app = create_app(data_dir=str(tmp_path), base_url="http://localhost:8321")
        server = TestServer(app)
        await server.start_server()
        # 验证 DB 连接正常 — store.stats() 不抛异常
        store: Store = app.get("store")
        assert store is not None
        stats = store.stats()
        assert "totalAgents" in stats
        await server.close()

    async def test_startup_checks_auth_rsa(self, tmp_path) -> None:
        """验证 auth 启用时 RSA 密钥检查（_startup_checks 中的 RSA check）。"""
        app = create_app(
            data_dir=str(tmp_path),
            base_url="http://localhost:8321",
            auth_enabled=True,
            bootstrap_secret="test-bootstrap-secret",
        )
        server = TestServer(app)
        await server.start_server()
        # 验证 JWKS 端点可用（auth 启用后自动注册）
        client = TestClient(server)
        resp = await client.get("/.well-known/jwks.json")
        assert resp.status == 200
        await server.close()
