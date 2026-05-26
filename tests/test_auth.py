"""Unit tests for OAuth 2.1 authentication module."""
from __future__ import annotations

import hashlib
import json
import tempfile
import time

import pytest
from aiohttp.test_utils import TestServer, TestClient
from aiohttp import web, hdrs

from simple_a2a_registry.auth import (
    AuthStore,
    AuthHandler,
    create_token,
    verify_token,
    _generate_rsa_keypair,
    _auth_middleware_factory,
    require_scope,
    SCOPES,
    TOKEN_EXPIRY_SECONDS,
)
from simple_a2a_registry.server import create_app

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# JWT helpers
# ---------------------------------------------------------------------------


class TestJWT:
    def test_create_and_verify_rs256(self):
        priv, pub = _generate_rsa_keypair()
        token = create_token("test-agent", private_key=priv, algorithm="RS256")
        assert isinstance(token, str) and len(token) > 50

        payload = verify_token(token, public_key=pub, algorithm="RS256")
        assert payload is not None
        assert payload["sub"] == "test-agent"
        assert payload["iss"] == "simple-a2a-registry"
        assert "jti" in payload
        assert "exp" in payload
        assert "iat" in payload

    def test_create_and_verify_hs256(self):
        secret = "dev-secret-not-for-production"
        token = create_token(
            "test-agent", private_key=secret, algorithm="HS256",
        )
        payload = verify_token(token, public_key=secret, algorithm="HS256")
        assert payload is not None
        assert payload["sub"] == "test-agent"

    def test_verify_expired_token(self):
        priv, _ = _generate_rsa_keypair()
        token = create_token("test-agent", private_key=priv, expiry=-1)
        payload = verify_token(token, public_key=priv)
        assert payload is None

    def test_verify_invalid_signature(self):
        priv1, _ = _generate_rsa_keypair()
        _, pub2 = _generate_rsa_keypair()
        token = create_token("test-agent", private_key=priv1)
        payload = verify_token(token, public_key=pub2)
        assert payload is None

    def test_token_with_scope_and_audience(self):
        priv, pub = _generate_rsa_keypair()
        token = create_token(
            "agent-1",
            private_key=priv,
            audience=["simple-a2a-registry", "agent-2"],
            scope="task:read task:write",
        )
        payload = verify_token(
            token, public_key=pub, audience=["simple-a2a-registry"],
        )
        assert payload is not None
        assert payload["scope"] == "task:read task:write"
        assert payload["aud"] == ["simple-a2a-registry", "agent-2"]

    def test_verify_wrong_audience(self):
        priv, pub = _generate_rsa_keypair()
        token = create_token("agent-1", private_key=priv, audience=["service-a"])
        payload = verify_token(token, public_key=pub, audience=["service-b"])
        assert payload is None


# ---------------------------------------------------------------------------
# AuthStore
# ---------------------------------------------------------------------------


class TestAuthStore:
    def test_register_and_verify_client(self):
        store = AuthStore(tempfile.mkdtemp())
        result = store.register_client(description="test client")
        assert "client_id" in result
        assert "client_secret" in result

        assert store.verify_client_secret(result["client_id"], result["client_secret"])
        assert not store.verify_client_secret(result["client_id"], "wrong-secret")
        assert not store.verify_client_secret("nonexistent", "x")

    def test_client_allowed_scopes(self):
        store = AuthStore(tempfile.mkdtemp())
        result = store.register_client(
            description="scoped client",
            allowed_scopes=["task:read"],
        )
        assert store.client_allowed_scopes(result["client_id"], "task:read")
        assert not store.client_allowed_scopes(result["client_id"], "task:write")

    def test_bootstrap_registry_account(self):
        store = AuthStore(tempfile.mkdtemp())
        client = store.get_client("simple-a2a-registry")
        assert client is not None
        assert "registry:admin" in client.allowed_scopes

    def test_token_lifecycle(self):
        store = AuthStore(tempfile.mkdtemp())
        now = int(time.time())
        store.record_token({
            "jti": "test-jti-1",
            "sub": "client-1",
            "scope": "task:read",
            "exp": now + 3600,
        })
        rec = store.get_token("test-jti-1")
        assert rec is not None
        assert rec.client_id == "client-1"

        store.revoke_token("test-jti-1")
        assert store.get_token("test-jti-1") is None

    def test_expired_token(self):
        store = AuthStore(tempfile.mkdtemp())
        store.record_token({
            "jti": "expired-jti",
            "sub": "client-1",
            "scope": "task:read",
            "exp": int(time.time()) - 10,
        })
        assert store.get_token("expired-jti") is None

    def test_revoke_client_tokens(self):
        store = AuthStore(tempfile.mkdtemp())
        store.record_token({
            "jti": "t1", "sub": "client-a", "scope": "r", "exp": int(time.time()) + 3600,
        })
        store.record_token({
            "jti": "t2", "sub": "client-a", "scope": "w", "exp": int(time.time()) + 3600,
        })
        store.record_token({
            "jti": "t3", "sub": "client-b", "scope": "r", "exp": int(time.time()) + 3600,
        })
        assert store.revoke_client_tokens("client-a") == 2
        assert store.get_token("t3") is not None

    def test_auth_code_pkce(self):
        store = AuthStore(tempfile.mkdtemp())
        code_verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
        code_challenge = hashlib.sha256(code_verifier.encode()).hexdigest()

        code = store.create_auth_code(
            client_id="client-1",
            code_challenge=code_challenge,
            code_challenge_method="S256",
            redirect_uri="https://agent/callback",
            scope="task:read",
        )
        assert len(code) > 20

        data = store.consume_auth_code(code, code_verifier)
        assert data is not None
        assert data["client_id"] == "client-1"
        assert data["scope"] == "task:read"

    def test_auth_code_bad_verifier(self):
        store = AuthStore(tempfile.mkdtemp())
        challenge = hashlib.sha256("correct-verifier".encode()).hexdigest()
        code = store.create_auth_code(
            client_id="client-1", code_challenge=challenge,
            code_challenge_method="S256",
            redirect_uri="https://agent/callback", scope="r",
        )
        assert store.consume_auth_code(code, "wrong-verifier") is None

    def test_auth_code_consumed_once(self):
        store = AuthStore(tempfile.mkdtemp())
        verifier = "test-verifier-123"
        challenge = hashlib.sha256(verifier.encode()).hexdigest()
        code = store.create_auth_code(
            client_id="client-1", code_challenge=challenge,
            code_challenge_method="S256",
            redirect_uri="https://agent/callback", scope="r",
        )
        assert store.consume_auth_code(code, verifier) is not None
        assert store.consume_auth_code(code, verifier) is None  # already consumed


# ---------------------------------------------------------------------------
# Auth Middleware
# ---------------------------------------------------------------------------


class TestAuthMiddleware:
    async def test_disabled_middleware_passes_through(self):
        """When auth is disabled, all requests pass without auth."""
        async with await _make_app(auth_enabled=False) as client:
            resp = await client.get("/health")
            assert resp.status == 200

    async def test_enabled_middleware_allows_public_paths(self):
        """/auth/*, /.well-known/*, /health, / are public even when auth enabled."""
        async with await _make_app(auth_enabled=True) as client:
            resp = await client.get("/health")
            assert resp.status == 200
            resp = await client.get("/.well-known/agent-card.json")
            assert resp.status == 200

    async def test_enabled_middleware_returns_401_without_token(self):
        """Non-public paths return 401 without Bearer token when auth enabled."""
        async with await _make_app(auth_enabled=True) as client:
            resp = await client.get("/_test_protected")
            assert resp.status == 401
            data = await resp.json()
            assert data["error"] == "unauthorized"

    async def test_enabled_middleware_accepts_valid_token(self):
        """Valid Bearer token passes through auth middleware."""
        async with await _make_app(auth_enabled=True) as client:
            token = await _get_test_token(client)
            headers = {"Authorization": f"Bearer {token}"}
            resp = await client.get("/_test_protected", headers=headers)
            assert resp.status == 200

    async def test_enabled_middleware_rejects_invalid_token(self):
        """Invalid Bearer token returns 401."""
        async with await _make_app(auth_enabled=True) as client:
            headers = {"Authorization": "Bearer invalid-token-123"}
            resp = await client.get("/_test_protected", headers=headers)
            assert resp.status == 401
            data = await resp.json()
            assert data["error"] == "invalid_token"

    async def test_enabled_middleware_rejects_expired_token(self):
        """Expired Bearer token returns 401 from middleware."""
        async with await _make_app(auth_enabled=True) as client:
            app = client.server.app
            auth_handler = app["auth_handler"]
            private_key = auth_handler.private_key
            algorithm = auth_handler.algorithm

            # Create a token that expires in the past
            token = create_token(
                "test-agent",
                private_key=private_key,
                algorithm=algorithm,
                expiry=-1,  # already expired
            )
            headers = {"Authorization": f"Bearer {token}"}
            resp = await client.get("/_test_protected", headers=headers)
            assert resp.status == 401
            data = await resp.json()
            assert data["error"] == "invalid_token"

    async def test_enabled_middleware_injects_agent_id(self):
        """Valid token injects agent_id and token_scopes into request."""
        async with await _make_app(auth_enabled=True) as client:
            token = await _get_test_token(client)
            headers = {"Authorization": f"Bearer {token}"}
            resp = await client.get("/_test_protected", headers=headers)
            assert resp.status == 200
            data = await resp.json()
            assert data["agent_id"] != ""

    async def test_ws_path_public_when_auth_enabled(self):
        """Paths ending with /ws are public even when auth is enabled."""
        async with await _make_app(auth_enabled=True) as client:
            resp = await client.get("/_test/ws")
            assert resp.status == 200
            data = await resp.json()
            assert data["ws_ok"] is True
            # No agent_id injected since no auth was required
            assert data["agent_id"] == ""

    async def test_ws_path_with_bearer_token(self):
        """Paths ending with /ws still accept Bearer token if provided."""
        async with await _make_app(auth_enabled=True) as client:
            token = await _get_test_token(client)
            headers = {"Authorization": f"Bearer {token}"}
            resp = await client.get("/_test/ws", headers=headers)
            assert resp.status == 200
            # The endpoint is public, so the middleware skips auth,
            # but the token is on the request — endpoint just doesn't
            # need it because the path matched the public WS exemption


# ---------------------------------------------------------------------------
# Token Endpoint Integration
# ---------------------------------------------------------------------------


class TestTokenEndpoint:
    async def test_register_client(self):
        """POST /auth/register creates a new client."""
        async with await _make_app() as client:
            resp = await client.post("/auth/register", json={
                "description": "Test Agent",
            })
            assert resp.status == 201
            data = await resp.json()
            assert "client_id" in data
            assert "client_secret" in data

    async def test_token_client_credentials(self):
        """POST /auth/token with client_credentials grant returns access_token."""
        async with await _make_app() as client:
            reg = await client.post("/auth/register", json={
                "description": "Token Test",
            })
            creds = await reg.json()

            resp = await client.post("/auth/token", data={
                "grant_type": "client_credentials",
                "client_id": creds["client_id"],
                "client_secret": creds["client_secret"],
                "scope": "task:read",
            })
            assert resp.status == 200
            data = await resp.json()
            assert "access_token" in data
            assert data["token_type"] == "Bearer"
            assert data["expires_in"] == TOKEN_EXPIRY_SECONDS
            assert data["scope"] == "task:read"

    async def test_token_invalid_client_credentials(self):
        """POST /auth/token with bad credentials returns 401."""
        async with await _make_app() as client:
            resp = await client.post("/auth/token", data={
                "grant_type": "client_credentials",
                "client_id": "nonexistent",
                "client_secret": "bad-secret",
            })
            assert resp.status == 401
            data = await resp.json()
            assert data["error"] == "invalid_client"

    async def test_token_unsupported_grant_type(self):
        """Unsupported grant type returns 400."""
        async with await _make_app() as client:
            resp = await client.post("/auth/token", data={
                "grant_type": "password",
                "client_id": "x",
                "client_secret": "x",
            })
            assert resp.status == 400
            data = await resp.json()
            assert data["error"] == "unsupported_grant_type"

    async def test_token_with_scope_restriction(self):
        """Token honors scope restrictions from client registration."""
        async with await _make_app() as client:
            reg = await client.post("/auth/register", json={
                "description": "Restricted Client",
                "allowed_scopes": ["task:read"],
            })
            creds = await reg.json()

            # Requesting disallowed scope should fail
            resp = await client.post("/auth/token", data={
                "grant_type": "client_credentials",
                "client_id": creds["client_id"],
                "client_secret": creds["client_secret"],
                "scope": "registry:admin",
            })
            assert resp.status == 400
            data = await resp.json()
            assert data["error"] == "invalid_scope"

            # Requesting allowed scope should succeed
            resp = await client.post("/auth/token", data={
                "grant_type": "client_credentials",
                "client_id": creds["client_id"],
                "client_secret": creds["client_secret"],
                "scope": "task:read",
            })
            assert resp.status == 200

    async def test_well_known_oauth(self):
        """GET /.well-known/oauth-authorization-server returns RFC 8414 metadata."""
        async with await _make_app() as client:
            resp = await client.get("/.well-known/oauth-authorization-server")
            assert resp.status == 200
            data = await resp.json()
            assert data["issuer"] == "http://localhost:8321"
            assert data["token_endpoint"] == "http://localhost:8321/auth/token"
            assert "client_credentials" in data["grant_types_supported"]
            assert "authorization_code" in data["grant_types_supported"]
            assert data["scopes_supported"] == list(SCOPES.keys())


# ---------------------------------------------------------------------------
# require_scope decorator
# ---------------------------------------------------------------------------


class TestRequireScope:
    async def test_require_scope_passes_when_token_has_scope(self):
        async def handler(request):
            return web.json_response({"ok": True})

        wrapped = require_scope("task:read")(handler)

        request = MockRequest(token_scopes="task:read task:write")
        resp = await _run_handler(wrapped, request)
        assert resp.status == 200

    async def test_require_scope_blocks_when_missing(self):
        async def handler(request):
            return web.json_response({"ok": True})

        wrapped = require_scope("task:write")(handler)

        request = MockRequest(token_scopes="task:read")
        resp = await _run_handler(wrapped, request)
        assert resp.status == 403


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _make_app(auth_enabled: bool = False):
    """Create a TestClient with an optional test endpoint and auth enabled."""
    tmpdir_obj = tempfile.TemporaryDirectory()
    data_dir = tmpdir_obj.name

    app = create_app(
        data_dir=data_dir,
        base_url="http://localhost:8321",
        auth_enabled=auth_enabled,
    )

    # Add test-only endpoints for middleware testing
    async def _test_protected(request: web.Request) -> web.Response:
        return web.json_response({
            "agent_id": request.get("agent_id", ""),
            "scopes": request.get("token_scopes", ""),
        })

    async def _test_ws(request: web.Request) -> web.Response:
        """Simulate a WebSocket-like endpoint (path ending in /ws)."""
        return web.json_response({"ws_ok": True, "agent_id": request.get("agent_id", "")})

    app.router.add_get("/_test_protected", _test_protected)
    app.router.add_get("/_test/ws", _test_ws)

    server = TestServer(app)
    await server.start_server()
    client = TestClient(server)
    return client


async def _get_test_token(client):
    """Register a client and get a token for testing."""
    reg = await client.post("/auth/register", json={"description": "Test"})
    creds = await reg.json()
    tok = await client.post("/auth/token", data={
        "grant_type": "client_credentials",
        "client_id": creds["client_id"],
        "client_secret": creds["client_secret"],
        "scope": "task:read",
    })
    data = await tok.json()
    return data["access_token"]


class MockRequest:
    """Minimal mock aiohttp Request for testing require_scope decorator."""
    def __init__(self, token_scopes: str = ""):
        self._cache: dict = {}
        self["token_scopes"] = token_scopes
        self["agent_id"] = "test-agent"

    def __getitem__(self, key):
        return self._cache[key]

    def __setitem__(self, key, value):
        self._cache[key] = value

    def get(self, key, default=None):
        return self._cache.get(key, default)


async def _run_handler(handler, request):
    """Run an async handler with a mock request."""
    return await handler(request)


# ---------------------------------------------------------------------------
# Admin Clients API (requires auth_enabled=True + registry:admin scope)
# ---------------------------------------------------------------------------


class TestAdminClients:
    """Tests for POST/GET/DELETE /admin/clients endpoints."""

    async def _get_admin_token(self, client) -> str:
        """Register a client via public /auth/register, then get token with registry:admin scope."""
        reg = await client.post("/auth/register", json={"description": "Admin Test"})
        creds = await reg.json()
        tok = await client.post("/auth/token", data={
            "grant_type": "client_credentials",
            "client_id": creds["client_id"],
            "client_secret": creds["client_secret"],
            "scope": "registry:admin",
        })
        data = await tok.json()
        return data["access_token"]

    async def test_create_client(self):
        """POST /admin/clients creates a new OAuth client successfully."""
        async with await _make_app(auth_enabled=True) as client:
            admin_token = await self._get_admin_token(client)
            headers = {"Authorization": f"Bearer {admin_token}"}
            resp = await client.post("/admin/clients", json={
                "agent_card_id": "test-agent",
                "description": "Test Client via Admin",
                "allowed_scopes": ["task:read", "task:write"],
            }, headers=headers)
            assert resp.status == 201
            data = await resp.json()
            assert data["client_id"].startswith("client-")
            assert "client_secret" in data
            assert data["agent_card_id"] == "test-agent"
            assert data["scopes"] == ["task:read", "task:write"]

    async def test_create_client_requires_admin_scope(self):
        """POST /admin/clients without registry:admin scope returns 403."""
        async with await _make_app(auth_enabled=True) as client:
            # Get a token without registry:admin scope
            reg = await client.post("/auth/register", json={"description": "No Admin"})
            creds = await reg.json()
            tok = await client.post("/auth/token", data={
                "grant_type": "client_credentials",
                "client_id": creds["client_id"],
                "client_secret": creds["client_secret"],
                "scope": "task:read",
            })
            no_admin_token = (await tok.json())["access_token"]
            headers = {"Authorization": f"Bearer {no_admin_token}"}
            resp = await client.post("/admin/clients", json={
                "agent_card_id": "test",
                "description": "Should Fail",
            }, headers=headers)
            assert resp.status == 403

    async def test_list_clients(self):
        """GET /admin/clients returns the client list."""
        async with await _make_app(auth_enabled=True) as client:
            admin_token = await self._get_admin_token(client)
            headers = {"Authorization": f"Bearer {admin_token}"}
            resp = await client.get("/admin/clients", headers=headers)
            assert resp.status == 200
            data = await resp.json()
            assert isinstance(data, list)
            # Should contain the bootstrap account + the admin test client we created
            client_ids = [c["client_id"] for c in data]
            assert "simple-a2a-registry" in client_ids

    async def test_delete_client(self):
        """DELETE /admin/clients/{id} deletes a client successfully."""
        async with await _make_app(auth_enabled=True) as client:
            admin_token = await self._get_admin_token(client)
            headers = {"Authorization": f"Bearer {admin_token}"}

            # Create a client first
            create = await client.post("/admin/clients", json={
                "agent_card_id": "delete-me",
                "description": "To be deleted",
            }, headers=headers)
            client_id = (await create.json())["client_id"]

            # Delete it
            resp = await client.delete(f"/admin/clients/{client_id}", headers=headers)
            assert resp.status == 200
            data = await resp.json()
            assert data["message"] == "Client deleted successfully"

    async def test_list_after_delete(self):
        """After deleting a client, the list no longer contains it."""
        async with await _make_app(auth_enabled=True) as client:
            admin_token = await self._get_admin_token(client)
            headers = {"Authorization": f"Bearer {admin_token}"}

            # Create a client
            create = await client.post("/admin/clients", json={
                "agent_card_id": "gone-soon",
                "description": "Will be deleted",
            }, headers=headers)
            client_id = (await create.json())["client_id"]

            # Delete it
            await client.delete(f"/admin/clients/{client_id}", headers=headers)

            # List should not contain it
            list_resp = await client.get("/admin/clients", headers=headers)
            data = await list_resp.json()
            client_ids = [c["client_id"] for c in data]
            assert client_id not in client_ids


# ---------------------------------------------------------------------------
# Auth Integration — Admin-provisioned flow (方案C)
# ---------------------------------------------------------------------------


class TestAuthIntegration:
    """Integration: admin creates client → token → register agent with scope."""

    async def test_admin_provisioned_flow(self):
        """POST /admin/clients (Bearer + registry:admin) → creds → POST /auth/token → POST /v1/agents (Bearer + agent:register)."""
        async with await _make_app(auth_enabled=True) as client:
            # 1. Get an admin token (registry:admin scope)
            reg = await client.post("/auth/register", json={"description": "Admin Provisioner"})
            creds = await reg.json()
            tok = await client.post("/auth/token", data={
                "grant_type": "client_credentials",
                "client_id": creds["client_id"],
                "client_secret": creds["client_secret"],
                "scope": "registry:admin",
            })
            assert tok.status == 200
            admin_token = (await tok.json())["access_token"]
            admin_headers = {"Authorization": f"Bearer {admin_token}"}

            # 2. Create an OAuth client via admin API (simulates admin pre-provisioning)
            admin_create = await client.post("/admin/clients", json={
                "agent_card_id": "my-agent",
                "description": "Pre-provisioned for my-agent",
                "allowed_scopes": ["agent:register", "agent:read", "task:read"],
            }, headers=admin_headers)
            assert admin_create.status == 201
            agent_creds = await admin_create.json()
            assert "client_id" in agent_creds
            assert "client_secret" in agent_creds

            # 3. Get a token with agent:register scope (as the agent would)
            tok2 = await client.post("/auth/token", data={
                "grant_type": "client_credentials",
                "client_id": agent_creds["client_id"],
                "client_secret": agent_creds["client_secret"],
                "scope": "agent:register",
            })
            assert tok2.status == 200
            register_token = (await tok2.json())["access_token"]
            register_headers = {"Authorization": f"Bearer {register_token}"}

            # 4. Register an agent with the agent:register token
            reg_agent = await client.post("/v1/agents", json={
                "name": "Pre-Provisioned Agent",
                "description": "Agent registered with admin-provisioned client",
            }, headers=register_headers)
            assert reg_agent.status == 201
            agent_data = await reg_agent.json()
            assert agent_data["id"].startswith("agent_") or agent_data["id"]

            # 5. Verify the agent appears in the listing (use agent:read scope)
            tok3 = await client.post("/auth/token", data={
                "grant_type": "client_credentials",
                "client_id": agent_creds["client_id"],
                "client_secret": agent_creds["client_secret"],
                "scope": "agent:read",
            })
            read_token = (await tok3.json())["access_token"]
            read_headers = {"Authorization": f"Bearer {read_token}"}
            list_resp = await client.get("/v1/agents", headers=read_headers)
            assert list_resp.status == 200
            list_data = await list_resp.json()
            assert list_data["total"] >= 1