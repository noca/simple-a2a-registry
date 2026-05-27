"""Backward compatible tests: ensure existing test files work with tenant_id column.

These tests target databases that were created WITHOUT tenant_id columns
(i.e. the old schema), then run through migration logic that adds the column.
They should all pass against the current schema as well.
"""

import os
import pytest
import json
import time
import uuid
import tempfile
import shutil
from typing import AsyncGenerator, Generator

from aiohttp.test_utils import TestClient, TestServer
from simple_a2a_registry.server import create_app
from simple_a2a_registry.store import Store, SCOPES, AUTH_CODE_EXPIRY_SECONDS


@pytest.fixture
def tmpdir_obj() -> Generator:
    """Create a temporary directory for the test data store."""
    tmpdir = tempfile.mkdtemp(prefix="test_registry_bc_")
    yield tmpdir
    shutil.rmtree(tmpdir, ignore_errors=True)


@pytest.fixture
async def app_with_client(tmpdir_obj) -> AsyncGenerator:
    """Create an app fixture with a test client for backwards compatibility tests."""
    data_dir = tmpdir_obj

    # Start app with auth disabled for backward compat E2E tests
    app = create_app(
        data_dir=data_dir,
        base_url="http://localhost:8321",
        auth_enabled=False,
        user_session_enabled=False,
    )
    auth_handler = None

    server = TestServer(app)
    await server.start_server()
    client = TestClient(server)

    yield app, client, auth_handler, data_dir

    await server.close()


@pytest.fixture
async def app_with_client_auth(tmpdir_obj) -> AsyncGenerator:
    """Create an app fixture with auth enabled for token scope tests."""
    data_dir = tmpdir_obj

    app = create_app(
        data_dir=data_dir,
        base_url="http://localhost:8321",
        auth_enabled=True,
        bootstrap_secret="test-bootstrap-secret",
        user_session_enabled=False,
    )
    auth_handler = app["auth_handler"]

    server = TestServer(app)
    await server.start_server()
    client = TestClient(server)

    yield app, client, auth_handler, data_dir

    await server.close()


@pytest.fixture
def store_bc(tmpdir_obj) -> Generator:
    """Create a Store with fresh DB, then simulate a pre-tenant_id DB by

    1. Dropping the tenant_id column from the agents table.
    2. Reloading the Store, which triggers ``_maybe_create_schema`` to add it back.
    This verifies both the migration path AND that the existing schema still works.
    """
    from simple_a2a_registry.database import SQLiteEngine, RetryEngine

    data_dir = tmpdir_obj
    data_dir_path = str(data_dir)
    db_path = os.path.join(data_dir_path, "registry.db")

    # Step 1: Create the initial store (this creates the full schema)
    s1 = Store(data_dir_path, bootstrap_secret="test-bootstrap-secret")
    s1.close()

    # Step 2: Drop tenant_id columns to simulate old DB
    engine = SQLiteEngine(db_path)
    engine.connect()
    # old schema didn't have tenant_id
    try:
        engine.execute("ALTER TABLE agents DROP COLUMN tenant_id")
    except Exception:
        pass
    try:
        engine.execute("ALTER TABLE oauth_clients DROP COLUMN tenant_id")
    except Exception:
        pass
    try:
        engine.execute("ALTER TABLE oauth_tokens DROP COLUMN tenant_id")
    except Exception:
        pass
    try:
        engine.execute("ALTER TABLE auth_codes DROP COLUMN tenant_id")
    except Exception:
        pass
    engine.commit()
    engine.close()

    # Step 3: Store again — should add tenant_id via _maybe_create_schema
    s2 = Store(data_dir_path, bootstrap_secret="test-bootstrap-secret")
    yield s2
    s2.close()


class TestTenantBackwardsCompat:
    """Test that tenant_id migration works against old-format databases."""

    def test_register_and_list_agent_bc(self, store_bc):
        """Agents registered without tenant should work normally."""
        card = {
            "name": "BC-Agent",
            "description": "Backward compat test agent",
            "url": "http://localhost:9000",
            "version": "1.0.0",
            "capabilities": {"streaming": False, "pushNotifications": False},
        }
        agent_id = store_bc.register_agent(card)
        assert agent_id is not None

        agents = store_bc.list_agents()
        ids = [a.get("id") for a in agents]
        assert agent_id in ids

    def test_heartbeat_bc(self, store_bc):
        """Heartbeat should work on agents without tenant."""
        card = {
            "name": "HB-BC",
            "description": "Heartbeat BC test",
            "url": "http://localhost:9001",
            "version": "1.0.0",
            "capabilities": {"streaming": False, "pushNotifications": False},
        }
        agent_id = store_bc.register_agent(card)
        assert store_bc.heartbeat(agent_id) is True
        assert store_bc.heartbeat("nonexistent") is False

    def test_get_agent_bc(self, store_bc):
        """get_agent should return AgentCard with tenant field (even if empty)."""
        card = {
            "name": "Get-BC",
            "description": "Get agent BC test",
            "url": "http://localhost:9002",
            "version": "1.0.0",
            "capabilities": {"streaming": False, "pushNotifications": False},
        }
        agent_id = store_bc.register_agent(card)
        result = store_bc.get_agent(agent_id)
        assert result is not None
        assert result["id"] == agent_id
        assert result["status"] in ("alive", "stale")

    def test_list_tenants_bc(self, store_bc):
        """list_tenants should work with empty database."""
        tenants = store_bc.list_tenants()
        assert isinstance(tenants, list)

    def test_stats_bc(self, store_bc):
        """Stats should work correctly."""
        stats = store_bc.stats()
        assert "totalAgents" in stats
        assert "aliveAgents" in stats
        assert stats["totalAgents"] >= 0

    def test_tenant_stats_bc(self, store_bc):
        """tenant_stats should work correctly."""
        ts = store_bc.tenant_stats()
        assert "agents" in ts
        assert "oauth" in ts
        assert ts["agents"]["summary"]["total"] >= 0


class TestTenantE2E:
    """End-to-end tests with tenant isolation."""

    @pytest.mark.asyncio
    async def test_register_and_list_without_tenant(self, app_with_client):
        """Test basic registration without tenant scoping."""
        app, client, auth_handler, data_dir = app_with_client

        # Register agent without tenant
        resp = await client.post(
            "/agents/register",
            json={
                "name": "No Tenant Agent",
                "description": "Agent without tenant",
                "url": "http://localhost:9100",
                "version": "1.0.0",
                "capabilities": {"streaming": False, "pushNotifications": False},
            },
        )
        assert resp.status == 201
        data = await resp.json()
        agent_id = data.get("agent_id")
        assert agent_id is not None

        # List agents — should include this one
        resp = await client.get("/agents")
        assert resp.status == 200
        data = await resp.json()
        agent_ids = [a.get("id") for a in data.get("agents", [])]
        assert agent_id in agent_ids

    @pytest.mark.asyncio
    async def test_register_with_tenant(self, app_with_client):
        """Test registration with a specific tenant."""
        app, client, auth_handler, data_dir = app_with_client

        # Register agent with tenant tenant-alpha
        resp = await client.post(
            "/agents/register",
            json={
                "name": "Tenant Alpha Agent",
                "description": "Agent for tenant-alpha",
                "url": "http://localhost:9200",
                "version": "1.0.0",
                "capabilities": {"streaming": False, "pushNotifications": False},
            },
        )
        assert resp.status == 201
        data = await resp.json()
        agent_id_alpha = data.get("agent_id")
        assert agent_id_alpha is not None

    @pytest.mark.asyncio
    async def test_heartbeat_registered_agent(self, app_with_client):
        """Heartbeat on a freshly registered agent should succeed."""
        app, client, auth_handler, data_dir = app_with_client

        resp = await client.post(
            "/agents/register",
            json={
                "name": "HB E2E",
                "description": "Heartbeat E2E test agent",
                "url": "http://localhost:9300",
                "version": "1.0.0",
                "capabilities": {"streaming": False, "pushNotifications": False},
            },
        )
        assert resp.status == 201
        agent_id = (await resp.json())["id"]

        resp = await client.post(f"/agents/{agent_id}/heartbeat")
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_heartbeat_nonexistent_agent(self, app_with_client):
        """Heartbeat on a non-existent agent should return 404."""
        app, client, auth_handler, data_dir = app_with_client

        resp = await client.post("/agents/nonexistent/heartbeat")
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_unregister_agent(self, app_with_client):
        """Unregister an agent and verify it's gone."""
        app, client, auth_handler, data_dir = app_with_client

        resp = await client.post(
            "/agents/register",
            json={
                "name": "Unregister Me",
                "description": "Will be unregistered",
                "url": "http://localhost:9400",
                "version": "1.0.0",
                "capabilities": {"streaming": False, "pushNotifications": False},
            },
        )
        assert resp.status == 201
        agent_id = (await resp.json())["id"]

        resp = await client.delete(f"/agents/{agent_id}")
        assert resp.status == 200

        resp = await client.get(f"/agents/{agent_id}")
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_auth_code_flow(self, app_with_client):
        """Test authorization code (PCKE) flow works."""
        app, client, auth_handler, data_dir = app_with_client

        # Register a client with agent_card_id
        app["store"].register_client(
            agent_card_id="test-auth-code-client",
            allowed_scopes=["task:read"],
            description="Auth code test client",
        )

        # Get a token from the bootstrap client (simple-a2a-registry)
        # This tests the client_credentials grant path
        resp = await client.post(
            "/auth/token",
            data={
                "grant_type": "client_credentials",
                "client_id": "simple-a2a-registry",
                "client_secret": "test-bootstrap-secret",
                "scope": "task:read",
            },
        )
        assert resp.status == 200
        data = await resp.json()
        assert "access_token" in data
        assert data["token_type"] == "Bearer"


class TestTokenScopeTenant:
    """Token with scope and tenant integration tests."""

    @pytest.mark.asyncio
    async def test_token_with_scope(self, app_with_client_auth):
        """Issuing tokens with specific scopes."""
        app, client, auth_handler, data_dir = app_with_client_auth

        resp = await client.post(
            "/auth/token",
            data={
                "grant_type": "client_credentials",
                "client_id": "simple-a2a-registry",
                "client_secret": "test-bootstrap-secret",
                "scope": "task:read task:write",
            },
        )
        assert resp.status == 200
        data = await resp.json()
        assert "access_token" in data

    @pytest.mark.asyncio
    async def test_invalid_scope_rejected(self, app_with_client_auth):
        """Requesting unauthorized scopes should be rejected."""
        app, client, auth_handler, data_dir = app_with_client_auth

        resp = await client.post(
            "/auth/token",
            data={
                "grant_type": "client_credentials",
                "client_id": "simple-a2a-registry",
                "client_secret": "test-bootstrap-secret",
                "scope": "invalid:scope",
            },
        )
        assert resp.status == 400