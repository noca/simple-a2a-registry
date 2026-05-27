"""端到端集成测试：登录 → Session → 角色鉴权 → 登出

覆盖的端到端流程：
  1. POST /api/login      — 密码认证 + 获取 HTTP-only session cookie
  2. GET  /api/me          — 验证 session 生效，返回当前用户信息
  3. POST /admin/users     — 创建 operator/viewer 用户（需 admin 角色）
  4. GET  /admin/users     — admin 可访问，viewer 被拒绝 403
  5. POST /api/logout      — 清除 session cookie
  6. GET  /api/me          — 登出后返回 401
  7. 无效凭证 / 恶意 cookie — 拒绝
"""
from __future__ import annotations

import json
import tempfile
import time

import pytest
from aiohttp.test_utils import TestServer, TestClient

from simple_a2a_registry.server import create_app
from simple_a2a_registry.users import UserStore, SessionManager, ROLES, ROLE_HIERARCHY

pytestmark = pytest.mark.asyncio

KNOWN_ADMIN_PASSWORD = "Admin@123!test"
KNOWN_OP_PASSWORD = "Operator@123"
KNOWN_VIEW_PASSWORD = "Viewer@123"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def user_auth_env():
    """端到端测试环境: 启动服务器 + 预置已知用户 + SessionManager 引用。

    Yields (client, session_manager, user_store):
      - client:         ``TestClient``（附带 cookie jar）
      - session_manager: 用于构造/验证 session token
      - user_store:      用于数据库层面的用户操作
    """
    tmpdir_obj = tempfile.TemporaryDirectory()
    data_dir = tmpdir_obj.name
    app = create_app(
        data_dir=data_dir,
        base_url="http://localhost:8321",
    )
    server = TestServer(app)
    await server.start_server()
    client = TestClient(server)

    # 取出内部组件
    user_store: UserStore = app["user_store"]
    session_manager: SessionManager = app["session_manager"]
    assert user_store is not None, "UserStore should be created"
    assert session_manager is not None, "SessionManager should be created"

    # 用已知密码覆盖自动引导的 admin 账户
    user_store.update_user("admin", password=KNOWN_ADMIN_PASSWORD)

    yield client, session_manager, user_store

    await client.close()
    await server.close()
    tmpdir_obj.cleanup()


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------


async def login(client: TestClient, username: str, password: str) -> dict:
    """POST /api/login，返回 JSON body。"""
    resp = await client.post("/api/login", json={
        "username": username,
        "password": password,
    })
    return {"status": resp.status, "body": await resp.json(), "cookies": resp.cookies}


async def logout(client: TestClient) -> dict:
    """POST /api/logout，返回 JSON body。"""
    resp = await client.post("/api/logout")
    return {"status": resp.status, "body": await resp.json(), "cookies": resp.cookies}


async def me(client: TestClient) -> dict:
    """GET /api/me，返回 JSON body。"""
    resp = await client.get("/api/me")
    return {"status": resp.status, "body": await resp.json()}


# ---------------------------------------------------------------------------
# Tests: 登录
# ---------------------------------------------------------------------------


class TestLogin:
    """登录流程端到端测试。"""

    async def test_valid_admin_login_returns_200_and_cookie(self, user_auth_env):
        """有效凭证 → 200 + HTTP-only session cookie。"""
        client, *_ = user_auth_env
        result = await login(client, "admin", KNOWN_ADMIN_PASSWORD)
        assert result["status"] == 200, f"Expected 200, got {result['status']}: {result['body']}"
        assert result["body"]["success"] is True
        assert result["body"]["username"] == "admin"
        assert result["body"]["role"] == "admin"
        # 验证 Set-Cookie header 存在（a2a_session）
        assert "a2a_session" in result["cookies"] or "Set-Cookie" in str(result), \
            "Should set session cookie"

    async def test_invalid_credentials_returns_401(self, user_auth_env):
        """无效密码 → 401。"""
        client, *_ = user_auth_env
        result = await login(client, "admin", "wrong-password")
        assert result["status"] == 401
        assert result["body"]["error"] == "invalid_credentials"

    async def test_nonexistent_user_returns_401(self, user_auth_env):
        """不存在的用户 → 401。"""
        client, *_ = user_auth_env
        result = await login(client, "nobody", "anypassword")
        assert result["status"] == 401

    async def test_login_missing_fields_returns_400(self, user_auth_env):
        """缺少 username/password → 400。"""
        client, *_ = user_auth_env
        # 无密码
        r1 = await login(client, "admin", "")
        assert r1["status"] == 400
        # 无用户名
        r2 = await login(client, "", "whatever")
        assert r2["status"] == 400

    async def test_invalid_json_body_returns_400(self, user_auth_env):
        """非 JSON body → 400。"""
        client, *_ = user_auth_env
        resp = await client.post(
            "/api/login",
            data=b"not json at all",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status == 400


# ---------------------------------------------------------------------------
# Tests: Session
# ---------------------------------------------------------------------------


class TestSession:
    """Session 验证和 /api/me 端到端测试。"""

    async def test_me_returns_user_info_after_login(self, user_auth_env):
        """登录后 /api/me → 200 + 用户信息。"""
        client, *_ = user_auth_env
        # 登录
        login_result = await login(client, "admin", KNOWN_ADMIN_PASSWORD)
        assert login_result["status"] == 200
        # /api/me
        m = await me(client)
        assert m["status"] == 200, f"Expected 200, got {m['status']}: {m['body']}"
        assert m["body"]["username"] == "admin"
        assert m["body"]["role"] == "admin"

    async def test_me_returns_401_without_login(self, user_auth_env):
        """未登录 → /api/me → 401。"""
        client, *_ = user_auth_env
        # 不登录直接访问
        m = await me(client)
        assert m["status"] == 401
        assert m["body"]["error"] == "unauthorized"

    async def test_me_returns_401_after_logout(self, user_auth_env):
        """登出后 /api/me → 401。"""
        client, *_ = user_auth_env
        # 登录
        await login(client, "admin", KNOWN_ADMIN_PASSWORD)
        # 登出
        await logout(client)
        # 再次访问
        m = await me(client)
        assert m["status"] == 401

    async def test_invalid_session_cookie_rejected(self, user_auth_env):
        """伪造的 session cookie → 401。"""
        client, *_ = user_auth_env
        # 设置一个明显伪造的 cookie
        client.session.cookie_jar.update_cookies({
            "a2a_session": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.e30."
        })
        m = await me(client)
        assert m["status"] == 401

    async def test_session_manager_create_and_verify(self, user_auth_env):
        """SessionManager 直接创建 + 验证 session。"""
        _, session_manager, _ = user_auth_env
        token = session_manager.create_session("admin", "admin")
        payload = session_manager.verify_session(token)
        assert payload is not None
        assert payload["sub"] == "admin"
        assert payload["role"] == "admin"
        assert payload["type"] == "user_session"

    async def test_expired_session_rejected(self, user_auth_env):
        """过期的 session → /api/me → 401。"""
        client, session_manager, _ = user_auth_env
        # 创建一个已过期的 session token
        import time
        import jwt as pyjwt
        import uuid
        old_token = pyjwt.encode(
            {
                "sub": "admin",
                "role": "admin",
                "iat": int(time.time()) - 7200,
                "exp": int(time.time()) - 3600,
                "jti": str(uuid.uuid4()),
                "type": "user_session",
            },
            session_manager._secret,
            algorithm="HS256",
        )
        # 通过 cookie 注入
        client.session.cookie_jar.update_cookies({"a2a_session": old_token})
        m = await me(client)
        assert m["status"] == 401


# ---------------------------------------------------------------------------
# Tests: 角色权限
# ---------------------------------------------------------------------------


class TestRoleAuthorization:
    """角色鉴权端到端测试。"""

    async def _create_user_via_api(self, client, username, password, role):
        """用 admin session 创建用户。"""
        resp = await client.post("/admin/users", json={
            "username": username,
            "password": password,
            "role": role,
        })
        return resp.status, await resp.json()

    async def test_admin_list_users(self, user_auth_env):
        """admin 可以列出所有用户。"""
        client, *_ = user_auth_env
        await login(client, "admin", KNOWN_ADMIN_PASSWORD)
        resp = await client.get("/admin/users")
        assert resp.status == 200
        data = await resp.json()
        assert isinstance(data, list)
        usernames = [u["username"] for u in data]
        assert "admin" in usernames

    async def test_admin_create_operator_and_viewer(self, user_auth_env):
        """admin 可以创建 operator 和 viewer 用户。"""
        client, *_ = user_auth_env
        await login(client, "admin", KNOWN_ADMIN_PASSWORD)

        # 创建 operator
        status, body = await self._create_user_via_api(
            client, "operator1", KNOWN_OP_PASSWORD, "operator",
        )
        assert status == 201, f"Create operator failed: {body}"
        assert body["role"] == "operator"

        # 创建 viewer
        status, body = await self._create_user_via_api(
            client, "viewer1", KNOWN_VIEW_PASSWORD, "viewer",
        )
        assert status == 201
        assert body["role"] == "viewer"

        # 验证已创建
        resp = await client.get("/admin/users")
        users = await resp.json()
        roles = {u["username"]: u["role"] for u in users}
        assert roles["operator1"] == "operator"
        assert roles["viewer1"] == "viewer"

    async def test_viewer_cannot_access_admin_users(self, user_auth_env):
        """viewer 无法访问 /admin/users（403）。"""
        client, *_ = user_auth_env
        # 先用 admin 创建 viewer
        await login(client, "admin", KNOWN_ADMIN_PASSWORD)
        await self._create_user_via_api(client, "v1", KNOWN_VIEW_PASSWORD, "viewer")

        # 登出 admin
        await logout(client)

        # 以 viewer 身份登录
        await login(client, "v1", KNOWN_VIEW_PASSWORD)
        resp = await client.get("/admin/users")
        assert resp.status == 403, f"Expected 403, got {resp.status}"
        data = await resp.json()
        assert data["error"] == "forbidden"
        assert "insufficient" in data["detail"].lower()

    async def test_operator_cannot_access_admin_users(self, user_auth_env):
        """operator 也无法访问 /admin/users（403）。"""
        client, *_ = user_auth_env
        await login(client, "admin", KNOWN_ADMIN_PASSWORD)
        await self._create_user_via_api(client, "op1", KNOWN_OP_PASSWORD, "operator")

        await logout(client)
        await login(client, "op1", KNOWN_OP_PASSWORD)
        resp = await client.get("/admin/users")
        assert resp.status == 403

    async def test_admin_delete_user(self, user_auth_env):
        """admin 可以删除其他用户。"""
        client, *_ = user_auth_env
        await login(client, "admin", KNOWN_ADMIN_PASSWORD)
        await self._create_user_via_api(client, "delete_me", "pass1234", "viewer")

        resp = await client.delete("/admin/users/delete_me")
        assert resp.status == 200
        data = await resp.json()
        assert data["success"] is True

        # 确认已删除
        resp = await client.get("/admin/users")
        users = await resp.json()
        usernames = [u["username"] for u in users]
        assert "delete_me" not in usernames

    async def test_admin_cannot_delete_self(self, user_auth_env):
        """admin 不能删除自己的账户。"""
        client, *_ = user_auth_env
        await login(client, "admin", KNOWN_ADMIN_PASSWORD)
        resp = await client.delete("/admin/users/admin")
        assert resp.status == 403

    async def test_admin_cannot_delete_admin_account(self, user_auth_env):
        """任何人不能删除 admin 账户。"""
        client, *_ = user_auth_env
        await login(client, "admin", KNOWN_ADMIN_PASSWORD)
        # 即使另一个 admin 账户也不行
        # /admin/users/admin 直接返回 protected
        resp = await client.delete("/admin/users/admin")
        assert resp.status == 403
        data = await resp.json()
        assert data["error"] == "protected"


# ---------------------------------------------------------------------------
# Tests: 登出
# ---------------------------------------------------------------------------


class TestLogout:
    """登出流程端到端测试。"""

    async def test_logout_clears_session(self, user_auth_env):
        """登出后 session cookie 被清除。"""
        client, *_ = user_auth_env
        await login(client, "admin", KNOWN_ADMIN_PASSWORD)

        result = await logout(client)
        assert result["status"] == 200
        assert result["body"]["success"] is True

        # Cookie 被清除（Max-Age=0 或过期）
        # 验证 session 不再有效
        m = await me(client)
        assert m["status"] == 401

    async def test_double_logout_is_idempotent(self, user_auth_env):
        """重复登出应无副作用（幂等）。"""
        client, *_ = user_auth_env
        await login(client, "admin", KNOWN_ADMIN_PASSWORD)

        await logout(client)
        r2 = await logout(client)
        assert r2["status"] == 200  # 即使已登出，清除 cookie 也不报错

    async def test_login_after_logout(self, user_auth_env):
        """登出后重新登录应正常工作。"""
        client, *_ = user_auth_env
        await login(client, "admin", KNOWN_ADMIN_PASSWORD)
        await logout(client)
        r = await login(client, "admin", KNOWN_ADMIN_PASSWORD)
        assert r["status"] == 200

        m = await me(client)
        assert m["status"] == 200
        assert m["body"]["username"] == "admin"


# ---------------------------------------------------------------------------
# Tests: 完整端到端流程（单一测试覆盖整个生命周期）
# ---------------------------------------------------------------------------


class TestFullFlow:
    """单一端到端场景：登录 → 操作 → 角色验证 → 登出。"""

    async def test_full_e2e_flow(self, user_auth_env):
        """完整场景：admin 登录 → 创建 operator/viewer → 切换角色验证权限 → 登出。"""
        client, *_ = user_auth_env

        # ── Phase 1: admin 登录 ──────────────────────────────────────────
        r = await login(client, "admin", KNOWN_ADMIN_PASSWORD)
        assert r["status"] == 200, "Phase 1: admin login failed"
        assert r["body"]["role"] == "admin"

        # ── Phase 2: admin 创建 operator 和 viewer ────────────────────────
        for u, p, role in [
            ("op_e2e", "OpPass_123", "operator"),
            ("vw_e2e", "VwPass_123", "viewer"),
        ]:
            resp = await client.post("/admin/users", json={
                "username": u, "password": p, "role": role,
            })
            assert resp.status == 201, f"Phase 2: create {u} failed"

        # 验证列表
        resp = await client.get("/admin/users")
        users = await resp.json()
        roles_map = {u["username"]: u["role"] for u in users}
        assert roles_map["op_e2e"] == "operator"
        assert roles_map["vw_e2e"] == "viewer"

        # ── Phase 3: viewer 无法访问 /admin/users ────────────────────────
        await logout(client)
        r = await login(client, "vw_e2e", "VwPass_123")
        assert r["status"] == 200

        resp = await client.get("/admin/users")
        assert resp.status == 403, "Phase 3: viewer should be denied"

        # ── Phase 4: operator 也无法访问 /admin/users ────────────────────
        await logout(client)
        r = await login(client, "op_e2e", "OpPass_123")
        assert r["status"] == 200

        resp = await client.get("/admin/users")
        assert resp.status == 403, "Phase 4: operator should be denied"

        # ── Phase 5: admin 重新登录并清理 ────────────────────────────────
        await logout(client)
        r = await login(client, "admin", KNOWN_ADMIN_PASSWORD)
        assert r["status"] == 200

        # 删除创建的测试用户
        for uname in ("op_e2e", "vw_e2e"):
            resp = await client.delete(f"/admin/users/{uname}")
            assert resp.status == 200

        # ── Phase 6: 登出 ──────────────────────────────────────────────
        r2 = await logout(client)
        assert r2["status"] == 200

        m = await me(client)
        assert m["status"] == 401, "Phase 6: should be logged out"

    async def test_role_hierarchy(self, user_auth_env):
        """验证角色层级：admin > operator > viewer。"""
        _, _, user_store = user_auth_env

        assert user_store is not None

        # 验证角色层级关系
        assert ROLE_HIERARCHY["admin"] > ROLE_HIERARCHY["operator"]
        assert ROLE_HIERARCHY["operator"] > ROLE_HIERARCHY["viewer"]
        assert "admin" in ROLES
        assert "operator" in ROLES
        assert "viewer" in ROLES

        # 验证 role_ge 函数
        from simple_a2a_registry.users import role_ge
        assert role_ge("admin", "admin")
        assert role_ge("admin", "operator")
        assert role_ge("admin", "viewer")
        assert role_ge("operator", "operator")
        assert role_ge("operator", "viewer")
        assert not role_ge("viewer", "operator")
        assert not role_ge("viewer", "admin")
        assert role_ge("viewer", "viewer")

    async def test_protected_endpoints_require_session(self, user_auth_env):
        """受保护 API 端点无 session 时返回 401。"""
        client, *_ = user_auth_env

        # /admin/users without session
        resp = await client.get("/admin/users")
        assert resp.status == 401

        # /admin/users POST without session
        resp = await client.post("/admin/users", json={
            "username": "should_fail", "password": "test123", "role": "viewer",
        })
        assert resp.status == 401

        # /admin/users/{name} DELETE without session
        resp = await client.delete("/admin/users/test_delete")
        assert resp.status == 401

        # /admin/users/{name} PUT without session
        resp = await client.put("/admin/users/test_update", json={"role": "operator"})
        assert resp.status == 401