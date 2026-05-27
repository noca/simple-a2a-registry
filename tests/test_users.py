"""Unit tests for UserStore, SessionManager, and login flow.

Tests cover:
- UserStore CRUD (create, get, list, update, delete)
- Password hashing and authentication
- ``bootstrap_admin()`` — first-start default admin account
- ``bootstrap_admin()`` — idempotent (no-op when users exist)
- SessionManager — JWT session creation, verification, expiration
- Login API endpoint integration
"""
from __future__ import annotations

import logging
import tempfile
import time

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer, TestClient

from simple_a2a_registry.database import SQLiteEngine
from simple_a2a_registry.users import (
    UserStore,
    SessionManager,
    UserHandler,
    user_session_middleware_factory,
    require_role,
    SESSION_EXPIRY_SECONDS,
)
from simple_a2a_registry.audit import AuditStore, _maybe_create_audit_schema

# Async tests need asyncio mark. Sync tests below don't use it.
# Individual async test classes will be marked with @pytest.mark.asyncio.


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def engine():
    """Create a SQLiteEngine backed by a temp file for isolation."""
    db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db.close()
    e = SQLiteEngine(db.name)
    e.connect()
    yield e
    e.close()


@pytest.fixture
def store(engine):
    """UserStore backed by an isolated SQLite database."""
    return UserStore(engine)


@pytest.fixture
def sm():
    """SessionManager with a predictable secret for deterministic testing."""
    return SessionManager(secret_key="test-secret-for-testing-only")


@pytest.fixture
def app():
    """Minimal aiohttp application with session middleware for decorator tests."""
    from aiohttp import web
    from simple_a2a_registry.users import (
        SessionManager, user_session_middleware_factory,
    )
    sm = SessionManager(secret_key="test-secret-for-testing-only")
    app = web.Application(middlewares=[
        user_session_middleware_factory(sm, enabled=True),
    ])
    app["sm"] = sm
    return app


# ---------------------------------------------------------------------------
# UserStore — password helpers
# ---------------------------------------------------------------------------


class TestPasswordHasher:
    def test_hash_and_verify(self):
        pw = "my-secure-password-123!"
        hashed = UserStore.hash_password(pw)
        assert hashed != pw  # hash is never plaintext
        assert hashed.startswith("$2b$") or hashed.startswith("$2a$")  # bcrypt prefix
        assert UserStore.verify_password(pw, hashed)

    def test_verify_wrong_password(self):
        pw = "correct-password"
        hashed = UserStore.hash_password(pw)
        assert not UserStore.verify_password("wrong-password", hashed)

    def test_verify_empty_string(self):
        hashed = UserStore.hash_password("some-pass")
        assert not UserStore.verify_password("", hashed)

    def test_verify_invalid_hash(self):
        assert not UserStore.verify_password("any", "invalid-hash")

    def test_same_password_different_hashes(self):
        """bcrypt uses a random salt, so two hashes of the same password differ."""
        h1 = UserStore.hash_password("same-pass")
        h2 = UserStore.hash_password("same-pass")
        assert h1 != h2
        assert UserStore.verify_password("same-pass", h1)
        assert UserStore.verify_password("same-pass", h2)


# ---------------------------------------------------------------------------
# UserStore — CRUD
# ---------------------------------------------------------------------------


class TestUserStore:
    def test_create_and_get_user(self, store):
        user = store.create_user("alice", "password123", role="operator")
        assert user.username == "alice"
        assert user.role == "operator"
        assert user.created_at > 0
        assert user.updated_at > 0
        assert user.password_hash != "password123"  # hashed

        fetched = store.get_user("alice")
        assert fetched is not None
        assert fetched.username == "alice"
        assert fetched.role == "operator"

    def test_create_user_default_role(self, store):
        user = store.create_user("viewer1", "pass456")
        assert user.role == "viewer"

    def test_create_user_duplicate_raises(self, store):
        store.create_user("bob", "pass1", role="viewer")
        with pytest.raises(ValueError, match="already exists"):
            store.create_user("bob", "pass2", role="admin")

    def test_create_user_invalid_role(self, store):
        with pytest.raises(ValueError, match="Invalid role"):
            store.create_user("bad", "pass", role="superadmin")

    def test_get_user_nonexistent(self, store):
        assert store.get_user("nobody") is None

    def test_list_users_empty(self, store):
        users = store.list_users()
        assert users == []

    def test_list_users_excludes_password_hash(self, store):
        store.create_user("alice", "secret1", role="admin")
        store.create_user("bob", "secret2", role="operator")
        users = store.list_users()
        assert len(users) == 2
        for u in users:
            assert "password_hash" not in u
            assert "username" in u
            assert "role" in u

    def test_list_users_ordered_by_created(self, store):
        store.create_user("first", "p1", role="viewer")
        time.sleep(0.01)
        store.create_user("second", "p2", role="operator")
        users = store.list_users()
        assert users[0]["username"] == "first"
        assert users[1]["username"] == "second"

    def test_update_user_password(self, store):
        store.create_user("alice", "old-pass", role="viewer")
        ok = store.update_user("alice", password="new-pass")
        assert ok
        assert store.authenticate("alice", "new-pass") is not None
        assert store.authenticate("alice", "old-pass") is None

    def test_update_user_role(self, store):
        store.create_user("alice", "pass", role="viewer")
        ok = store.update_user("alice", role="admin")
        assert ok
        user = store.get_user("alice")
        assert user is not None
        assert user.role == "admin"

    def test_update_user_nonexistent(self, store):
        ok = store.update_user("nobody", password="new-pass")
        assert not ok

    def test_delete_user(self, store):
        store.create_user("alice", "pass", role="viewer")
        ok = store.delete_user("alice")
        assert ok
        assert store.get_user("alice") is None

    def test_delete_user_nonexistent(self, store):
        ok = store.delete_user("nobody")
        assert not ok

    def test_authenticate_success(self, store):
        store.create_user("alice", "correct-pass", role="operator")
        user = store.authenticate("alice", "correct-pass")
        assert user is not None
        assert user.username == "alice"
        assert user.role == "operator"

    def test_authenticate_wrong_password(self, store):
        store.create_user("alice", "correct-pass", role="admin")
        assert store.authenticate("alice", "wrong-pass") is None

    def test_authenticate_nonexistent_user(self, store):
        assert store.authenticate("nobody", "any-pass") is None

    def test_authenticate_empty_password(self, store):
        store.create_user("alice", "some-pass", role="viewer")
        assert store.authenticate("alice", "") is None


# ---------------------------------------------------------------------------
# UserStore — bootstrap_admin
# ---------------------------------------------------------------------------


class TestBootstrapAdmin:
    def test_bootstrap_on_empty_db(self, store):
        """When no users exist, bootstrap creates admin with random password."""
        pw = store.bootstrap_admin()
        assert len(pw) > 0  # random password returned
        user = store.get_user("admin")
        assert user is not None
        assert user.role == "admin"
        # Password was properly hashed
        assert UserStore.verify_password(pw, user.password_hash)
        # Can authenticate
        assert store.authenticate("admin", pw) is not None

    def test_bootstrap_returns_different_password_each_time(self, store):
        """Each bootstrap generates a fresh random password."""
        pw1 = store.bootstrap_admin()
        # Reset: delete admin and bootstrap again
        store.delete_user("admin")
        pw2 = store.bootstrap_admin()
        assert pw1 != pw2

    def test_bootstrap_idempotent_when_users_exist(self, store):
        """Once users exist, bootstrap_admin() is a no-op and returns empty string."""
        pw = store.bootstrap_admin()
        assert len(pw) > 0

        # Second call — users already exist
        pw2 = store.bootstrap_admin()
        assert pw2 == ""  # no-op

        # Admin still exists with the original password
        user = store.get_user("admin")
        assert user is not None
        assert UserStore.verify_password(pw, user.password_hash)

    def test_bootstrap_with_existing_non_admin_user(self, store):
        """If a non-admin user already exists, bootstrap is still a no-op."""
        store.create_user("operator1", "op-pass", role="operator")
        pw = store.bootstrap_admin()
        assert pw == ""  # no-op — users exist
        assert store.get_user("admin") is None  # no admin created

    def test_bootstrap_admin_can_login_via_api(self, engine, store):
        """End-to-end: bootstrap_admin creates admin user that can authenticate."""
        admin_pw = store.bootstrap_admin()
        assert admin_pw != "", "should have created admin user"

        # Verify the admin user exists with correct role
        user = store.get_user("admin")
        assert user is not None
        assert user.role == "admin"

        # Verify authentication with generated password works
        assert store.authenticate("admin", admin_pw) is not None
        # Wrong password should be rejected
        assert store.authenticate("admin", "wrong-password") is None
        # Nonexistent user should be rejected
        assert store.authenticate("nonexistent", admin_pw) is None


# ---------------------------------------------------------------------------
# SessionManager
# ---------------------------------------------------------------------------


class TestSessionManager:
    """Session JWT lifecycle tests. ``test_get_session_from_request`` is async."""

    def test_create_and_verify(self, sm):
        token = sm.create_session("alice", "admin")
        assert len(token) > 20
        payload = sm.verify_session(token)
        assert payload is not None
        assert payload["sub"] == "alice"
        assert payload["role"] == "admin"
        assert payload["type"] == "user_session"
        assert "jti" in payload
        assert "exp" in payload
        assert "iat" in payload

    def test_verify_expired_token(self, sm):
        """A token created in the past should be rejected."""
        import jwt as pyjwt
        import time
        # Create a token that already expired
        expired_payload = {
            "sub": "alice",
            "role": "viewer",
            "iat": int(time.time()) - 7200,
            "exp": int(time.time()) - 3600,
            "jti": "expired-test-jti",
            "type": "user_session",
        }
        expired_token = pyjwt.encode(
            expired_payload,
            "test-secret-for-testing-only",
            algorithm="HS256",
        )
        assert sm.verify_session(expired_token) is None

    def test_verify_invalid_token(self, sm):
        assert sm.verify_session("not-a-jwt-token") is None
        assert sm.verify_session("") is None

    def test_verify_wrong_type(self, sm):
        """A JWT that isn't a user_session should be rejected."""
        import jwt as pyjwt
        bad_token = pyjwt.encode(
            {"sub": "hacker", "type": "access_token", "exp": int(time.time()) + 3600},
            "test-secret-for-testing-only",
            algorithm="HS256",
        )
        assert sm.verify_session(bad_token) is None

    @pytest.mark.asyncio
    async def test_get_session_from_request(self, sm):
        """Test extracting session from aiohttp request cookies."""
        app = web.Application()
        app["sm"] = sm

        async def handler(request):
            session = sm.get_session(request)
            if session:
                return web.json_response({"user": session["sub"]})
            return web.json_response({"user": None})

        app.router.add_get("/test", handler)
        async with TestClient(TestServer(app)) as client:
            # No cookie
            resp = await client.get("/test")
            data = await resp.json()
            assert data["user"] is None

            # With valid cookie
            token = sm.create_session("alice", "admin")
            client.session.cookie_jar.update_cookies(
                {"a2a_session": token}
            )
            resp = await client.get("/test")
            data = await resp.json()
            assert data["user"] == "alice"

    def test_different_secrets_produce_different_tokens(self):
        sm1 = SessionManager(secret_key="secret-1")
        sm2 = SessionManager(secret_key="secret-2")
        token = sm1.create_session("alice", "admin")
        # sm2 cannot verify sm1's token
        assert sm2.verify_session(token) is None


# ---------------------------------------------------------------------------
# role_ge / require_role
# ---------------------------------------------------------------------------


class TestRoleHelpers:
    def test_role_hierarchy(self):
        from simple_a2a_registry.users import role_ge, ROLES

        assert role_ge("admin", "admin")
        assert role_ge("admin", "operator")
        assert role_ge("admin", "viewer")
        assert role_ge("operator", "viewer")
        assert not role_ge("viewer", "operator")
        assert not role_ge("operator", "admin")

    def test_unknown_role_lowest(self):
        from simple_a2a_registry.users import role_ge
        assert not role_ge("unknown", "viewer")
        assert role_ge("admin", "unknown")  # unknown has level 0

    @pytest.mark.asyncio
    async def test_require_role_decorator(self, app: web.Application):
        """Test require_role decorator returns 401/403 correctly."""

        @require_role("admin")
        async def admin_only(request):
            return web.json_response({"ok": True})

        app.router.add_get("/admin-only", admin_only)

        async with TestClient(TestServer(app)) as client:
            # No session
            resp = await client.get("/admin-only")
            assert resp.status == 401

            # Insufficient role
            token = sm_token("viewer")
            client.session.cookie_jar.update_cookies({"a2a_session": token})
            resp = await client.get("/admin-only")
            assert resp.status == 403

            # Sufficient role
            token = sm_token("admin")
            client.session.cookie_jar.update_cookies({"a2a_session": token})
            resp = await client.get("/admin-only")
            assert resp.status == 200


# ---------------------------------------------------------------------------
# UserHandler — login API
# ---------------------------------------------------------------------------


def sm_token(role: str = "admin") -> str:
    """Create a deterministic session token with the given role."""
    sm = SessionManager(secret_key="test-secret-for-testing-only")
    return sm.create_session("admin", role)


def make_app_with_users(engine, user_store) -> web.Application:
    """Build a minimal app with user auth middleware and UserHandler."""
    from simple_a2a_registry.audit import EventType, _maybe_create_audit_schema

    _maybe_create_audit_schema(engine, retention_days=90)
    audit_store = AuditStore(engine, retention_days=90)
    session_manager = SessionManager(secret_key="test-secret-for-testing-only")
    user_handler = UserHandler(user_store, session_manager, audit_store=audit_store)

    app = web.Application(middlewares=[
        user_session_middleware_factory(session_manager, enabled=True),
    ])

    app["user_store"] = user_store
    app["session_manager"] = session_manager

    app.router.add_get("/login", user_handler.handle_login_page)
    app.router.add_post("/api/login", user_handler.handle_login)
    app.router.add_post("/api/logout", user_handler.handle_logout)
    app.router.add_get("/api/me", user_handler.handle_get_current_user)

    # Admin routes
    app.router.add_get("/admin/users", require_role("admin")(user_handler.handle_list_users))
    app.router.add_post("/admin/users", require_role("admin")(user_handler.handle_create_user))
    app.router.add_put(
        "/admin/users/{username}",
        require_role("admin")(user_handler.handle_update_user),
    )
    app.router.add_delete(
        "/admin/users/{username}",
        require_role("admin")(user_handler.handle_delete_user),
    )

    return app


@pytest.fixture
def login_app(engine, store):
    """Full login app fixture for endpoint tests."""
    yield make_app_with_users(engine, store)


@pytest.mark.asyncio
class TestLoginAPI:
    async def test_login_success(self, login_app):
        """POST /api/login with valid credentials returns session cookie."""
        us: UserStore = login_app["user_store"]

        # Bootstrap admin first
        admin_pw = us.bootstrap_admin()

        async with TestClient(TestServer(login_app)) as client:
            resp = await client.post("/api/login", json={
                "username": "admin",
                "password": admin_pw,
            })
            body = await resp.json()
            assert resp.status == 200
            assert body["success"] is True
            assert body["username"] == "admin"
            assert body["role"] == "admin"
            # Cookie set
            cookies = resp.headers.getall("Set-Cookie", [])
            assert any("a2a_session=" in c for c in cookies)

    async def test_login_wrong_password(self, login_app):
        us: UserStore = login_app["user_store"]
        us.bootstrap_admin()

        async with TestClient(TestServer(login_app)) as client:
            resp = await client.post("/api/login", json={
                "username": "admin",
                "password": "wrong-password",
            })
            assert resp.status == 401
            body = await resp.json()
            assert "invalid_credentials" in body.get("error", "")

    async def test_login_nonexistent_user(self, login_app):
        async with TestClient(TestServer(login_app)) as client:
            resp = await client.post("/api/login", json={
                "username": "nobody",
                "password": "any-pass",
            })
            assert resp.status == 401

    async def test_login_missing_fields(self, login_app):
        async with TestClient(TestServer(login_app)) as client:
            resp = await client.post("/api/login", json={})
            assert resp.status == 400
            body = await resp.json()
            assert "validation_error" in body.get("error", "")

    async def test_logout_clears_cookie(self, login_app):
        us: UserStore = login_app["user_store"]
        admin_pw = us.bootstrap_admin()

        async with TestClient(TestServer(login_app)) as client:
            # Login first
            resp = await client.post("/api/login", json={
                "username": "admin",
                "password": admin_pw,
            })
            assert resp.status == 200

            # Logout
            resp = await client.post("/api/logout")
            assert resp.status == 200
            cookies = resp.headers.getall("Set-Cookie", [])
            assert any("a2a_session=;" in c or "a2a_session=\"\"" in c
                       or "Max-Age=0" in c for c in cookies)

    async def test_get_current_user_authenticated(self, login_app):
        us: UserStore = login_app["user_store"]
        admin_pw = us.bootstrap_admin()

        async with TestClient(TestServer(login_app)) as client:
            # Login
            resp = await client.post("/api/login", json={
                "username": "admin",
                "password": admin_pw,
            })
            assert resp.status == 200

            # Get current user
            resp = await client.get("/api/me")
            assert resp.status == 200
            body = await resp.json()
            assert body["username"] == "admin"
            assert body["role"] == "admin"

    async def test_get_current_user_unauthenticated(self, login_app):
        async with TestClient(TestServer(login_app)) as client:
            resp = await client.get("/api/me")
            assert resp.status == 401


@pytest.mark.asyncio
class TestUserManagementAPI:
    """Tests for \"/admin/users\" endpoints — require admin session."""

    async def test_list_users(self, login_app):
        us: UserStore = login_app["user_store"]
        admin_pw = us.bootstrap_admin()
        us.create_user("operator1", "op-pass", role="operator")

        async with TestClient(TestServer(login_app)) as client:
            # Login as admin
            resp = await client.post("/api/login", json={
                "username": "admin",
                "password": admin_pw,
            })
            assert resp.status == 200

            # List users
            resp = await client.get("/admin/users")
            assert resp.status == 200
            users = await resp.json()
            assert len(users) == 2
            usernames = [u["username"] for u in users]
            assert "admin" in usernames
            assert "operator1" in usernames
            # Password hashes not exposed
            assert all("password_hash" not in u for u in users)

    async def test_create_user_via_api(self, login_app):
        us: UserStore = login_app["user_store"]
        admin_pw = us.bootstrap_admin()

        async with TestClient(TestServer(login_app)) as client:
            # Login as admin
            resp = await client.post("/api/login", json={
                "username": "admin",
                "password": admin_pw,
            })
            assert resp.status == 200

            # Create new user
            resp = await client.post("/admin/users", json={
                "username": "newop",
                "password": "new-pass-123",
                "role": "operator",
            })
            assert resp.status == 201
            data = await resp.json()
            assert data["username"] == "newop"
            assert data["role"] == "operator"

            # Verify user exists
            user = us.get_user("newop")
            assert user is not None

    async def test_create_user_requires_admin_role(self, login_app):
        us: UserStore = login_app["user_store"]
        us.bootstrap_admin()
        us.create_user("operator1", "op-pass", role="operator")

        async with TestClient(TestServer(login_app)) as client:
            # Login as operator (not admin)
            resp = await client.post("/api/login", json={
                "username": "operator1",
                "password": "op-pass",
            })
            assert resp.status == 200

            # Try to create user — forbidden
            resp = await client.post("/admin/users", json={
                "username": "hacker",
                "password": "hack-pass",
            })
            assert resp.status == 403

    async def test_delete_user_via_api(self, login_app):
        us: UserStore = login_app["user_store"]
        admin_pw = us.bootstrap_admin()
        us.create_user("operator1", "op-pass", role="operator")

        async with TestClient(TestServer(login_app)) as client:
            # Login as admin
            resp = await client.post("/api/login", json={
                "username": "admin",
                "password": admin_pw,
            })
            assert resp.status == 200

            # Delete operator
            resp = await client.delete("/admin/users/operator1")
            assert resp.status == 200

            assert us.get_user("operator1") is None

    async def test_cannot_delete_admin_account(self, login_app):
        us: UserStore = login_app["user_store"]
        admin_pw = us.bootstrap_admin()

        async with TestClient(TestServer(login_app)) as client:
            resp = await client.post("/api/login", json={
                "username": "admin",
                "password": admin_pw,
            })
            assert resp.status == 200

            resp = await client.delete("/admin/users/admin")
            assert resp.status == 403

    async def test_update_user_password(self, login_app):
        us: UserStore = login_app["user_store"]
        admin_pw = us.bootstrap_admin()
        us.create_user("op1", "old-pass", role="operator")

        async with TestClient(TestServer(login_app)) as client:
            # Admin login
            resp = await client.post("/api/login", json={
                "username": "admin",
                "password": admin_pw,
            })
            assert resp.status == 200

            # Update password
            resp = await client.put("/admin/users/op1", json={
                "password": "new-pass-999",
            })
            assert resp.status == 200

            # Old password no longer works
            assert us.authenticate("op1", "old-pass") is None
            assert us.authenticate("op1", "new-pass-999") is not None
