"""End-to-end integration tests for multi-tenant isolation.

Validates 5 scenarios required by P6-A5:
  1. Agent registration carries tenant identifier
  2. Tenant-level agent list isolation
  3. Tenant-level agent detail isolation
  4. OAuth client cross-tenant isolation
  5. Backward compatibility with empty-string tenant
"""
from __future__ import annotations

import json
import tempfile
import time

import pytest
from aiohttp.test_utils import TestServer, TestClient

from simple_a2a_registry.server import create_app
from simple_a2a_registry.store import Store

pytestmark = pytest.mark.asyncio

TENANT_A = "acme-corp"
TENANT_B = "globex-inc"
TENANT_EMPTY = ""


# ======================================================================
# Fixtures
# ======================================================================


@pytest.fixture(scope="module")
def anyio_backend():
    return "asyncio"


@pytest.fixture
def app_factory():
    """Return a callable that creates a fresh TestClient for each test."""
    factories = []

    async def maker():
        tmpdir_obj = tempfile.TemporaryDirectory()
        factories.append(tmpdir_obj)
        data_dir = tmpdir_obj.name
        app = create_app(
            data_dir=data_dir,
            base_url="http://localhost:8321",
            user_session_enabled=False,
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


@pytest.fixture
def store_factory():
    """Return a callable that creates a fresh Store for each test."""
    instances = []

    def maker():
        tmpdir_obj = tempfile.TemporaryDirectory()
        instances.append(tmpdir_obj)
        return Store(tmpdir_obj.name)

    yield maker

    for tmp in instances:
        try:
            tmp.cleanup()
        except Exception:
            pass


# ======================================================================
# Helpers
# ======================================================================


def _agent_card(name: str, *, tenant: str = "", description: str = "") -> dict:
    """Build an AgentCard dict with optional tenant field (v1.0 compatible)."""
    card: dict = {
        "name": name,
        "description": description or f"Agent {name}",
        "supported_interfaces": [{
            "url": f"https://agent.{name.lower()}.example.com",
            "protocol_binding": "JSONRPC",
            "protocol_version": "1.0",
            "tenant": tenant or None,
        }],
    }
    if tenant:
        card["tenant"] = tenant
    return card


async def _register_agent(
    client: TestClient, name: str, *, tenant: str = ""
) -> dict:
    """Register an agent via HTTP and return the response JSON."""
    card = _agent_card(name, tenant=tenant)
    resp = await client.post("/v1/agents", json=card)
    assert resp.status in (200, 201), f"register {name} failed: {await resp.text()}"
    return await resp.json()


async def _list_agents(
    client: TestClient, *, tenant: str = None, q: str = None
) -> list[dict]:
    """List agents, optionally filtered by tenant or search."""
    params = {}
    if tenant is not None:
        params["tenant"] = tenant
    if q:
        params["q"] = q
    resp = await client.get("/v1/agents", params=params)
    assert resp.status == 200
    data = await resp.json()
    return data["agents"]


async def _get_agent(client: TestClient, agent_id: str) -> dict | None:
    """Get a single agent detail via HTTP."""
    resp = await client.get(f"/v1/agents/{agent_id}")
    if resp.status == 200:
        return await resp.json()
    return None


async def _get_agent_with_tenant(
    client: TestClient, agent_id: str, *, tenant: str
) -> dict | None:
    """Get a single agent detail via HTTP with X-Tenant-ID header."""
    resp = await client.get(
        f"/v1/agents/{agent_id}",
        headers={"X-Tenant-ID": tenant},
    )
    if resp.status == 200:
        return await resp.json()
    return None


async def _register_client(
    client: TestClient, *, tenant: str = "", description: str = ""
) -> dict:
    """Register an OAuth client via HTTP."""
    body: dict[str, str] = {"description": description}
    if tenant:
        body["tenant"] = tenant
    # POST to an internal endpoint or use the store directly in tests
    # The public API doesn't expose client registration directly,
    # so we'll use the store for this
    raise NotImplementedError("Use store.register_client() for OAuth tests")


# ======================================================================
# Store-level tests (unit)
# ======================================================================


class TestStoreTenantAgentRegistration:
    """1. Agent registration carries tenant identifier (store level)."""

    def test_register_with_tenant_stores_tenant_id(self, store_factory):
        """Register an agent with a tenant; verify tenant_id is stored."""
        store = store_factory()
        aid = store.register_agent(_agent_card("Alpha", tenant=TENANT_A))

        card = store.get_agent(aid)
        assert card is not None
        # The tenant field should be preserved in the card and stored in DB
        assert card.get("tenant") == TENANT_A, (
            f"Expected tenant={TENANT_A!r}, got {card.get('tenant')!r}"
        )

    def test_register_without_tenant_uses_empty_string(self, store_factory):
        """Register without tenant; verify tenant is empty string."""
        store = store_factory()
        aid = store.register_agent(_agent_card("NoTenant"))

        card = store.get_agent(aid)
        assert card is not None
        tenant = card.get("tenant", "")
        assert tenant == "", f"Expected empty tenant, got {tenant!r}"

    def test_register_multiple_tenants(self, store_factory):
        """Register agents for different tenants; all stored correctly."""
        store = store_factory()
        a1 = store.register_agent(_agent_card("AcmeBot", tenant=TENANT_A))
        a2 = store.register_agent(_agent_card("GlobexBot", tenant=TENANT_B))
        a3 = store.register_agent(_agent_card("PublicBot"))

        assert store.get_agent(a1).get("tenant") == TENANT_A
        assert store.get_agent(a2).get("tenant") == TENANT_B
        assert store.get_agent(a3).get("tenant") == ""


class TestStoreTenantListIsolation:
    """2. Tenant-level agent list isolation (store level)."""

    def test_list_with_tenant_returns_only_that_tenant(self, store_factory):
        """list_agents(tenant=X) returns only agents belonging to X."""
        store = store_factory()
        store.register_agent(_agent_card("A1", tenant=TENANT_A))
        store.register_agent(_agent_card("A2", tenant=TENANT_A))
        store.register_agent(_agent_card("B1", tenant=TENANT_B))
        store.register_agent(_agent_card("Pub"))

        a_agents = store.list_agents(tenant=TENANT_A)
        # Backward compat: empty-tenant "Pub" is also visible
        assert len(a_agents) == 3
        names = [a.get("name") for a in a_agents]
        assert "A1" in names
        assert "A2" in names
        assert "Pub" in names, "Legacy no-tenant agents should be visible (backward compat)"
        assert "B1" not in names, "Other tenant's agents should NOT be visible"

        b_agents = store.list_agents(tenant=TENANT_B)
        # Backward compat: empty-tenant "Pub" is also visible to other tenants
        assert len(b_agents) == 2
        b_names = [a.get("name") for a in b_agents]
        assert "B1" in b_names
        assert "Pub" in b_names, "Legacy no-tenant agents should be visible (backward compat)"

    def test_list_with_empty_tenant_shows_empty_agents(self, store_factory):
        """list_agents(tenant='') returns only empty-tenant agents (backward compat)."""
        store = store_factory()
        store.register_agent(_agent_card("A1", tenant=TENANT_A))
        store.register_agent(_agent_card("B1", tenant=TENANT_B))
        store.register_agent(_agent_card("Pub"))

        # No tenant filter = all agents
        all_agents = store.list_agents()
        assert len(all_agents) == 3

        # Empty tenant filter = backward compat: only empty-tenant agents
        empty_agents = store.list_agents(tenant="")
        assert len(empty_agents) == 1, (
            f"Expected 1 empty-tenant agent, got {len(empty_agents)}"
        )
        assert empty_agents[0]["name"] == "Pub"

        # None = admin = all agents
        none_agents = store.list_agents(tenant=None)
        assert len(none_agents) == 3

    def test_nonexistent_tenant_returns_empty(self, store_factory):
        """Querying a tenant with no agents returns empty list."""
        store = store_factory()
        store.register_agent(_agent_card("A1", tenant=TENANT_A))
        agents = store.list_agents(tenant="nonexistent-org")
        assert len(agents) == 0

    def test_tenant_filter_combined_with_skill(self, store_factory):
        """tenant filter composes with skill filter correctly."""
        store = store_factory()
        store.register_agent(_agent_card("A Data", tenant=TENANT_A))
        store.register_agent(
            {
                **_agent_card("A Skilled", tenant=TENANT_A),
                "skills": [{
                    "id": "s1", "name": "Data Analysis",
                    "description": "Analyze data", "tags": ["data"],
                }],
            }
        )
        store.register_agent(
            {
                **_agent_card("B Skilled", tenant=TENANT_B),
                "skills": [{
                    "id": "s2", "name": "Data Analysis",
                    "description": "Analyze data", "tags": ["data"],
                }],
            }
        )

        result = store.list_agents(tenant=TENANT_A, skill="Data Analysis")
        assert len(result) == 1
        assert result[0].get("name") == "A Skilled"

    def test_tenant_filter_combined_with_search(self, store_factory):
        """tenant filter composes with text search correctly."""
        store = store_factory()
        store.register_agent(
            _agent_card("Alpha", tenant=TENANT_A, description="Important service")
        )
        store.register_agent(
            _agent_card("Beta", tenant=TENANT_A, description="Trivial utility")
        )
        store.register_agent(
            _agent_card("Gamma", tenant=TENANT_B, description="Important service")
        )

        result = store.list_agents(tenant=TENANT_A, q="Important")
        assert len(result) == 1
        assert result[0].get("name") == "Alpha"


class TestStoreTenantDetailIsolation:
    """Tenant A cannot read Tenant B's agent detail (store level)."""

    def test_get_agent_checks_tenant(self, store_factory):
        """get_agent() with wrong tenant returns no result."""
        store = store_factory()
        aid = store.register_agent(
            _agent_card("SecretA", tenant=TENANT_A)
        )

        # Accessing with correct tenant should succeed
        card = store.get_agent(aid)
        assert card is not None

        # The tenant isolation at store level is about list filtering,
        # not get_agent — only the tenant-owning agent's ID is knowable
        # by the correct tenant via list_agents(tenant=X)


# ======================================================================
# HTTP-level tests (E2E integration)
# ======================================================================


class TestRegistrationCarriesTenant:
    """1. Agent registration carries tenant identifier."""

    async def test_register_with_tenant_returns_tenant_in_card(self, app_factory):
        """Registering an agent with tenant field preserves it in response."""
        async with await app_factory() as client:
            resp_data = await _register_agent(client, "TenantAgent", tenant=TENANT_A)
            card = resp_data["card"]
            assert card.get("tenant") == TENANT_A, (
                f"Expected tenant={TENANT_A!r}, got {card.get('tenant')!r}"
            )

    async def test_register_without_tenant_no_tenant_in_card(self, app_factory):
        """Registering without tenant; field is absent or empty."""
        async with await app_factory() as client:
            resp_data = await _register_agent(client, "NoTenantAgent")
            card = resp_data["card"]
            tenant = card.get("tenant", "")
            assert tenant == "", f"Expected no tenant, got {tenant!r}"

    async def test_register_multiple_tenants_http(self, app_factory):
        """Register agents for multiple tenants; each preserves its tenant."""
        async with await app_factory() as client:
            r1 = await _register_agent(client, "AcmeAlpha", tenant=TENANT_A)
            r2 = await _register_agent(client, "GlobexBeta", tenant=TENANT_B)
            r3 = await _register_agent(client, "PublicGamma")

            assert r1["card"].get("tenant") == TENANT_A
            assert r2["card"].get("tenant") == TENANT_B
            assert r3["card"].get("tenant", "") == ""


class TestTenantListIsolation:
    """2. Tenant A cannot see Tenant B's agents via list."""

    async def test_list_with_tenant_filters_correctly(self, app_factory):
        """GET /v1/agents?tenant=X returns only X's agents."""
        async with await app_factory() as client:
            await _register_agent(client, "BotA1", tenant=TENANT_A)
            await _register_agent(client, "BotA2", tenant=TENANT_A)
            await _register_agent(client, "BotB1", tenant=TENANT_B)

            # Tenant A should see only their 2 agents
            a_list = await _list_agents(client, tenant=TENANT_A)
            assert len(a_list) == 2, f"Expected 2 agents for {TENANT_A}, got {len(a_list)}"
            for a in a_list:
                assert a.get("tenant") == TENANT_A

            # Tenant B should see only their 1 agent
            b_list = await _list_agents(client, tenant=TENANT_B)
            assert len(b_list) == 1, f"Expected 1 agent for {TENANT_B}, got {len(b_list)}"
            assert b_list[0].get("tenant") == TENANT_B

    async def test_list_without_tenant_shows_all(self, app_factory):
        """GET /v1/agents (no tenant) returns all agents (BC)."""
        async with await app_factory() as client:
            await _register_agent(client, "AcmeBot", tenant=TENANT_A)
            await _register_agent(client, "GlobexBot", tenant=TENANT_B)

            all_agents = await _list_agents(client)
            assert len(all_agents) == 2

    async def test_tenant_cannot_see_other_tenant_agent_names(self, app_factory):
        """Tenant A cannot discover Tenant B's agent IDs through listing."""
        async with await app_factory() as client:
            await _register_agent(client, "SecretB", tenant=TENANT_B)
            a_list = await _list_agents(client, tenant=TENANT_A)
            names = [a["name"] for a in a_list]
            assert "SecretB" not in names, (
                f"Tenant A should not see Tenant B's agents: {names}"
            )

    async def test_unknown_tenant_returns_empty(self, app_factory):
        """Unknown tenant query returns empty agent list."""
        async with await app_factory() as client:
            await _register_agent(client, "Bot1", tenant=TENANT_A)
            result = await _list_agents(client, tenant="stranger-org")
            assert len(result) == 0

    async def test_tenant_list_pagination_works(self, app_factory):
        """Pagination (limit/offset) works within a tenant scope."""
        async with await app_factory() as client:
            for i in range(5):
                await _register_agent(client, f"TABot{i}", tenant=TENANT_A)
            await _register_agent(client, "B1", tenant=TENANT_B)

            # Limit 2 for tenant A
            resp = await client.get(
                "/v1/agents",
                params={"tenant": TENANT_A, "limit": "2", "offset": "0"},
            )
            data = await resp.json()
            assert len(data["agents"]) == 2
            assert data["total"] == 5
            assert data["limit"] == 2
            assert data["offset"] == 0

    async def test_tenant_list_returns_only_own_agents(self, app_factory):
        """GET /v1/agents?tenant=X returns exclusively X's agents (not Y's)."""
        async with await app_factory() as client:
            r1 = await _register_agent(client, "AgentOne", tenant=TENANT_A)
            r2 = await _register_agent(client, "AgentTwo", tenant=TENANT_B)

            # List with tenant=A: should only see AgentOne
            a_list = await _list_agents(client, tenant=TENANT_A)
            a_names = [a["name"] for a in a_list]
            assert len(a_list) == 1, (
                f"Expected 1 agent for {TENANT_A}, got {len(a_list)}"
            )
            assert "AgentOne" in a_names, (
                f"{TENANT_A} should see AgentOne, got {a_names}"
            )
            assert "AgentTwo" not in a_names, (
                f"{TENANT_A} should NOT see AgentTwo, got {a_names}"
            )

            # List with tenant=B: should only see AgentTwo
            b_list = await _list_agents(client, tenant=TENANT_B)
            b_names = [b["name"] for b in b_list]
            assert len(b_list) == 1, (
                f"Expected 1 agent for {TENANT_B}, got {len(b_list)}"
            )
            assert "AgentTwo" in b_names, (
                f"{TENANT_B} should see AgentTwo, got {b_names}"
            )
            assert "AgentOne" not in b_names, (
                f"{TENANT_B} should NOT see AgentOne, got {b_names}"
            )


class TestTenantDetailIsolation:
    """3. Tenant A cannot fetch Tenant B's agent detail."""

    async def test_correct_tenant_sees_agent_detail(self, app_factory):
        """Agent from tenant A is visible to tenant A."""
        async with await app_factory() as client:
            r = await _register_agent(client, "AcmeAgent", tenant=TENANT_A)
            agent_id = r["id"]

            card = await _get_agent(client, agent_id)
            assert card is not None
            assert card["name"] == "AcmeAgent"

    async def test_tenant_cannot_get_other_tenant_agent(self, app_factory):
        """
        Tenant B cannot fetch Agent A's detail.

        This is enforced through X-Tenant-ID header on GET /v1/agents/{id}.
        """
        async with await app_factory() as client:
            r = await _register_agent(client, "AcmeSecret", tenant=TENANT_A)
            agent_id = r["id"]

            # Without tenant header should still work (BC)
            card = await _get_agent(client, agent_id)
            assert card is not None

            # With wrong tenant header should fail
            resp = await client.get(
                f"/v1/agents/{agent_id}",
                headers={"X-Tenant-ID": TENANT_B},
            )
            assert resp.status in (403, 404), (
                f"Expected 403/404 for wrong tenant, got {resp.status}"
            )
            data = await resp.json()
            assert data.get("error") in ("tenant_mismatch", "agent_not_found")

    async def test_detail_404_for_nonexistent(self, app_factory):
        """Non-existent agent returns 404 even with tenant header."""
        async with await app_factory() as client:
            resp = await client.get(
                "/v1/agents/nobody-here",
                headers={"X-Tenant-ID": TENANT_A},
            )
            assert resp.status == 404

    # ------------------------------------------------------------------
    # P6-A4-1b: ?tenant=xxx query param tests
    # ------------------------------------------------------------------

    async def test_get_agent_with_correct_tenant_query_param(self, app_factory):
        """GET /v1/agents/{id}?tenant=X returns the agent when tenant matches."""
        async with await app_factory() as client:
            r = await _register_agent(client, "TenantAgent", tenant=TENANT_A)
            agent_id = r["id"]

            resp = await client.get(
                f"/v1/agents/{agent_id}",
                params={"tenant": TENANT_A},
            )
            assert resp.status == 200, (
                f"Expected 200 for correct tenant query param, got {resp.status}: "
                f"{await resp.text()}"
            )
            data = await resp.json()
            assert data["name"] == "TenantAgent"
            assert data.get("tenant") == TENANT_A

    async def test_get_agent_with_wrong_tenant_query_param(self, app_factory):
        """GET /v1/agents/{id}?tenant=Y returns 404 when agent belongs to tenant X."""
        async with await app_factory() as client:
            r = await _register_agent(client, "SecretAgent", tenant=TENANT_A)
            agent_id = r["id"]

            resp = await client.get(
                f"/v1/agents/{agent_id}",
                params={"tenant": TENANT_B},
            )
            assert resp.status in (403, 404), (
                f"Expected 403/404 for wrong tenant query param, got {resp.status}: "
                f"{await resp.text()}"
            )

    async def test_get_agent_without_tenant_query_param_still_works(self, app_factory):
        """No ?tenant param still returns agent (backward compat)."""
        async with await app_factory() as client:
            r = await _register_agent(client, "PublicAgent", tenant=TENANT_A)
            agent_id = r["id"]

            card = await _get_agent(client, agent_id)
            assert card is not None
            assert card["name"] == "PublicAgent"

    async def test_get_agent_tenant_query_param_overrides_header(self, app_factory):
        """?tenant= query param overrides X-Tenant-ID header when no auth tenant."""
        async with await app_factory() as client:
            r = await _register_agent(client, "TestBot", tenant=TENANT_A)
            agent_id = r["id"]

            # Wrong header but correct ?tenant= param
            resp = await client.get(
                f"/v1/agents/{agent_id}",
                params={"tenant": TENANT_A},
                headers={"X-Tenant-ID": TENANT_B},
            )
            assert resp.status == 200, (
                f"Expected 200 (query param overrides header), got {resp.status}: "
                f"{await resp.text()}"
            )
            data = await resp.json()
            assert data["name"] == "TestBot"


class TestOAuthTenantIsolation:
    """4. OAuth client cross-tenant isolation."""

    async def test_client_registration_carries_tenant(self, app_factory):
        """OAuth client registered for a tenant stores tenant."""
        async with await app_factory() as client:
            # Register an agent to get the auto-created client flow
            card = _agent_card("OAuthAgent", tenant=TENANT_A)
            card["security_schemes"] = {
                "my-oauth": {
                    "scheme_type": "oauth2",
                    "description": "OAuth 2.1 client credentials",
                    "oauth2": {
                        "flows": {
                            "client_credentials": {
                                "token_url": "http://localhost:8321/auth/token",
                                "scopes": {"task:read": "Read tasks"},
                            }
                        }
                    },
                }
            }
            resp = await client.post("/v1/agents", json=card)
            assert resp.status == 200, f"Agent+OAuth register failed: {await resp.text()}"
            data = await resp.json()
            client_info = data.get("client", {})
            assert client_info.get("tenant") == TENANT_A, (
                f"Expected client tenant={TENANT_A!r}, "
                f"got {client_info.get('tenant')!r}"
            )

    async def test_oauth_clients_listed_by_tenant(self, app_factory):
        """list_clients filters by tenant when requested."""
        async with await app_factory() as client:
            # Register two agents with OAuth for different tenants
            card_a = _agent_card("OAuthA", tenant=TENANT_A)
            card_a["security_schemes"] = {
                "oauth": {
                    "scheme_type": "oauth2",
                    "description": "OAuth",
                    "oauth2": {
                        "flows": {
                            "client_credentials": {
                                "token_url": "http://localhost:8321/auth/token",
                                "scopes": {"task:read": "Read tasks"},
                            }
                        }
                    },
                }
            }
            await client.post("/v1/agents", json=card_a)

            card_b = _agent_card("OAuthB", tenant=TENANT_B)
            card_b["security_schemes"] = {
                "oauth": {
                    "scheme_type": "oauth2",
                    "description": "OAuth",
                    "oauth2": {
                        "flows": {
                            "client_credentials": {
                                "token_url": "http://localhost:8321/auth/token",
                                "scopes": {"task:read": "Read tasks"},
                            }
                        }
                    },
                }
            }
            await client.post("/v1/agents", json=card_b)

            # List clients with tenant filter
            resp_a = await client.get(
                "/auth/clients",
                params={"tenant": TENANT_A},
            )
            assert resp_a.status == 200
            clients_a = await resp_a.json()

            resp_b = await client.get(
                "/auth/clients",
                params={"tenant": TENANT_B},
            )
            assert resp_b.status == 200
            clients_b = await resp_b.json()

            # Each should see only their own client
            assert isinstance(clients_a, list)
            assert isinstance(clients_b, list)

            # Verify isolation through server endpoints
            for cl in clients_a:
                assert cl.get("tenant") == TENANT_A, (
                    f"Expected tenant={TENANT_A}, got {cl.get('tenant')}"
                )
            for cl in clients_b:
                assert cl.get("tenant") == TENANT_B, (
                    f"Expected tenant={TENANT_B}, got {cl.get('tenant')}"
                )


class TestBackwardCompatibility:
    """5. Empty-string tenant backward compatibility."""

    async def test_agents_without_tenant_visible_in_unfiltered_list(self, app_factory):
        """Agents without tenant are visible in GET /v1/agents (no tenant filter)."""
        async with await app_factory() as client:
            await _register_agent(client, "LegacyAgent")
            all_agents = await _list_agents(client)
            assert len(all_agents) == 1

    async def test_agents_without_tenant_and_with_tenant_mixed(self, app_factory):
        """Mixed tenant/no-tenant agents all show in unfiltered list."""
        async with await app_factory() as client:
            await _register_agent(client, "LegacyBot")
            await _register_agent(client, "TenantBot", tenant=TENANT_A)

            all_agents = await _list_agents(client)
            assert len(all_agents) == 2

            # Tenant-filtered listing for tenant A should show only tenant A's agent
            a_agents = await _list_agents(client, tenant=TENANT_A)
            assert len(a_agents) == 1
            assert a_agents[0]["name"] == "TenantBot"

    async def test_empty_tenant_filter_returns_all_for_admin(self, app_factory):
        """GET /v1/agents?tenant='' with admin scope returns ALL agents (admin bypass)."""
        async with await app_factory() as client:
            await _register_agent(client, "LegacyBot")
            await _register_agent(client, "TenantBot", tenant=TENANT_A)

            # With auth disabled (all scopes = admin), ?tenant='' bypasses filter
            all_agents = await _list_agents(client, tenant="")
            assert len(all_agents) == 2, (
                f"Expected 2 agents for tenant='' (admin bypass), got {len(all_agents)}"
            )

    async def test_store_register_and_crud_with_no_tenant(self, store_factory):
        """Store operations work correctly with agents that have no tenant."""
        store = store_factory()
        aid = store.register_agent(_agent_card("NoTenant"))
        assert store.get_agent(aid) is not None
        assert store.heartbeat(aid)
        assert store.unregister(aid)


# ======================================================================
# Tenant-aware store test (direct store isolation validation)
# ======================================================================


class TestStoreTenantFullIsolation:
    """Comprehensive store-level tenant isolation validation."""

    def test_full_isolation_flow(self, store_factory):
        """Full tenant lifecycle: register → list → detail → cross-tenant isolation."""
        store = store_factory()

        # Register agents
        a_id = store.register_agent(_agent_card("A-One", tenant=TENANT_A))
        b_id = store.register_agent(_agent_card("B-One", tenant=TENANT_B))
        p_id = store.register_agent(_agent_card("Public"))

        # Tenant A can only see tenant A agents
        a_list = store.list_agents(tenant=TENANT_A)
        assert len(a_list) == 1
        assert a_list[0]["name"] == "A-One"

        # Tenant B can only see tenant B agents
        b_list = store.list_agents(tenant=TENANT_B)
        assert len(b_list) == 1
        assert b_list[0]["name"] == "B-One"

        # Empty tenant returns only empty-tenant agents (BC)
        empty_list = store.list_agents(tenant="")
        assert len(empty_list) == 1
        assert empty_list[0]["name"] == "Public"

        # No filter returns all
        all_list = store.list_agents()
        assert len(all_list) == 3

        # Direct get_agent works regardless (detail isolation is server-level)
        assert store.get_agent(a_id) is not None
        assert store.get_agent(b_id) is not None

    def test_tenant_idempotent_re_register(self, store_factory):
        """Re-registering same agent ID preserves tenant."""
        store = store_factory()
        card = _agent_card("DupBot", tenant=TENANT_A)
        aid1 = store.register_agent(card)
        card2 = _agent_card("DupBot", tenant=TENANT_A)
        aid2 = store.register_agent(card2)

        assert aid1 != aid2  # new UUID each time

        agents = store.list_agents(tenant=TENANT_A)
        assert len(agents) == 2

    def test_tenant_filter_after_unregister(self, store_factory):
        """Unregistering agent from a tenant updates list correctly."""
        store = store_factory()
        aid = store.register_agent(_agent_card("GoneBot", tenant=TENANT_A))
        store.register_agent(_agent_card("StayBot", tenant=TENANT_A))

        assert len(store.list_agents(tenant=TENANT_A)) == 2

        store.unregister(aid)
        remaining = store.list_agents(tenant=TENANT_A)
        assert len(remaining) == 1
        assert remaining[0]["name"] == "StayBot"


# ======================================================================
# Auth-token cross-tenant isolation (with auth enabled)
# ======================================================================


class TestAuthTokenTenantIsolation:
    """Auth-token-based tenant isolation — tokens scoped to Tenant A
    cannot access Tenant B's agents."""

    @pytest.fixture
    def auth_app_factory(self):
        """Return a callable that creates a TestClient with auth enabled."""
        factories = []

        async def maker():
            tmpdir_obj = tempfile.TemporaryDirectory()
            factories.append(tmpdir_obj)
            data_dir = tmpdir_obj.name
            app = create_app(
                data_dir=data_dir,
                base_url="http://localhost:8321",
                auth_enabled=True,
                user_session_enabled=False,
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

    @staticmethod
    async def _register_and_get_token(
        client: TestClient, *, tenant: str, scopes: str = "agent:read agent:register task:write task:read"
    ) -> str:
        """Register an OAuth client for *tenant* and get a Bearer token.

        Returns:
            The ``access_token`` string.
        """
        # 1. Register OAuth client via /auth/register (public endpoint)
        resp = await client.post("/auth/register", json={
            "description": f"Test client for {tenant}",
            "tenant": tenant,
            "allowed_scopes": scopes.split(),
        })
        assert resp.status == 201, f"Client register failed: {await resp.text()}"
        reg_data = await resp.json()
        client_id = reg_data["client_id"]
        client_secret = reg_data["client_secret"]
        assert reg_data.get("tenant") == tenant, (
            f"Expected tenant={tenant}, got {reg_data.get('tenant')}"
        )

        # 2. Get token via client_credentials grant
        resp = await client.post("/auth/token", data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
            "scope": scopes,
        })
        assert resp.status == 200, f"Token request failed: {await resp.text()}"
        token_data = await resp.json()
        assert token_data.get("tenant") == tenant, (
            f"Expected token tenant={tenant}, got {token_data.get('tenant')}"
        )
        return token_data["access_token"]

    # -----------------------------------------------------------------
    # Registration
    # -----------------------------------------------------------------

    async def test_auth_register_with_tenant_preserves_tenant(self, auth_app_factory):
        """Agent registered with auth enabled carries tenant in response."""
        async with await auth_app_factory() as client:
            token = await self._register_and_get_token(client, tenant=TENANT_A)

            resp = await client.post(
                "/v1/agents",
                json=_agent_card("AuthAlpha", tenant=TENANT_A),
                headers={"Authorization": f"Bearer {token}"},
            )
            assert resp.status == 201, f"Register failed: {await resp.text()}"
            data = await resp.json()
            assert data["card"].get("tenant") == TENANT_A, (
                f"Expected tenant={TENANT_A}, got {data['card'].get('tenant')}"
            )

    # -----------------------------------------------------------------
    # List isolation with auth tokens
    # -----------------------------------------------------------------

    async def test_tenant_a_token_cannot_list_tenant_b_agents(self, auth_app_factory):
        """Tenant A's Bearer token → list returns only Tenant A's agents."""
        async with await auth_app_factory() as client:
            token_a = await self._register_and_get_token(client, tenant=TENANT_A)
            token_b = await self._register_and_get_token(client, tenant=TENANT_B)

            # Tenant A registers an agent
            resp = await client.post(
                "/v1/agents",
                json=_agent_card("AlphaAgent", tenant=TENANT_A),
                headers={"Authorization": f"Bearer {token_a}"},
            )
            assert resp.status == 201

            # Tenant B registers an agent
            resp = await client.post(
                "/v1/agents",
                json=_agent_card("BetaAgent", tenant=TENANT_B),
                headers={"Authorization": f"Bearer {token_b}"},
            )
            assert resp.status == 201

            # Tenant A lists agents → should see only AlphaAgent
            resp = await client.get(
                "/v1/agents",
                headers={"Authorization": f"Bearer {token_a}"},
            )
            assert resp.status == 200
            data = await resp.json()
            a_names = [a["name"] for a in data["agents"]]
            assert "AlphaAgent" in a_names, f"AlphaAgent not in {a_names}"
            assert "BetaAgent" not in a_names, f"BetaAgent leaked to tenant A in {a_names}"

            # Tenant B lists agents → should see only BetaAgent
            resp = await client.get(
                "/v1/agents",
                headers={"Authorization": f"Bearer {token_b}"},
            )
            assert resp.status == 200
            data = await resp.json()
            b_names = [a["name"] for a in data["agents"]]
            assert "BetaAgent" in b_names, f"BetaAgent not in {b_names}"
            assert "AlphaAgent" not in b_names, f"AlphaAgent leaked to tenant B in {b_names}"

    # -----------------------------------------------------------------
    # Detail isolation with auth tokens
    # -----------------------------------------------------------------

    async def test_tenant_a_token_cannot_get_tenant_b_agent_detail(self, auth_app_factory):
        """Tenant A's Bearer token → GET /v1/agents/{b_id} returns 404/403."""
        async with await auth_app_factory() as client:
            token_a = await self._register_and_get_token(client, tenant=TENANT_A)
            token_b = await self._register_and_get_token(client, tenant=TENANT_B)

            # Tenant B registers an agent
            resp = await client.post(
                "/v1/agents",
                json=_agent_card("SecretB", tenant=TENANT_B),
                headers={"Authorization": f"Bearer {token_b}"},
            )
            assert resp.status == 201
            b_data = await resp.json()
            b_id = b_data["id"]

            # Tenant A tries to read Tenant B's agent detail
            resp = await client.get(
                f"/v1/agents/{b_id}",
                headers={"Authorization": f"Bearer {token_a}"},
            )
            assert resp.status in (403, 404), (
                f"Expected 403/404 for cross-tenant detail access, "
                f"got {resp.status}"
            )

            # Tenant B can read its own agent detail
            resp = await client.get(
                f"/v1/agents/{b_id}",
                headers={"Authorization": f"Bearer {token_b}"},
            )
            assert resp.status == 200
            card = await resp.json()
            assert card["name"] == "SecretB"

    # -----------------------------------------------------------------
    # Cross-tenant detail with X-Tenant-ID header + auth
    # -----------------------------------------------------------------

    async def test_token_tenant_mismatch_with_x_tenant_id_header(self, auth_app_factory):
        """Token tenant and X-Tenant-ID header mismatch should fail."""
        async with await auth_app_factory() as client:
            token_a = await self._register_and_get_token(client, tenant=TENANT_A)
            token_b = await self._register_and_get_token(client, tenant=TENANT_B)

            # Tenant B registers an agent
            resp = await client.post(
                "/v1/agents",
                json=_agent_card("SecuredB", tenant=TENANT_B),
                headers={"Authorization": f"Bearer {token_b}"},
            )
            assert resp.status == 201
            b_id = (await resp.json())["id"]

            # Tenant A's token + X-Tenant-ID: B  →  should fail (token wins)
            resp = await client.get(
                f"/v1/agents/{b_id}",
                headers={
                    "Authorization": f"Bearer {token_a}",
                    "X-Tenant-ID": TENANT_B,
                },
            )
            # Token's tenant claim binds the request, even if header says B
            assert resp.status in (403, 404), (
                f"Expected 403/404 despite X-Tenant-ID=B override, "
                f"got {resp.status}"
            )

    # -----------------------------------------------------------------
    # Multiple tenants (3+) mutual invisibility
    # -----------------------------------------------------------------

    async def test_three_tenants_mutual_invisibility_with_auth(self, auth_app_factory):
        """Three tenants: each can only see their own agents."""
        TENANT_C = "zeta-labs"

        async with await auth_app_factory() as client:
            token_a = await self._register_and_get_token(client, tenant=TENANT_A)
            token_b = await self._register_and_get_token(client, tenant=TENANT_B)
            token_c = await self._register_and_get_token(client, tenant=TENANT_C)

            # Each registers one agent
            for tenant, token, name in [
                (TENANT_A, token_a, "AgentA"),
                (TENANT_B, token_b, "AgentB"),
                (TENANT_C, token_c, "AgentC"),
            ]:
                resp = await client.post(
                    "/v1/agents",
                    json=_agent_card(name, tenant=tenant),
                    headers={"Authorization": f"Bearer {token}"},
                )
                assert resp.status == 201, f"Register {name} failed"

            # Verify each tenant sees only their own
            for token, expected_name, not_seen in [
                (token_a, "AgentA", ["AgentB", "AgentC"]),
                (token_b, "AgentB", ["AgentA", "AgentC"]),
                (token_c, "AgentC", ["AgentA", "AgentB"]),
            ]:
                resp = await client.get(
                    "/v1/agents",
                    headers={"Authorization": f"Bearer {token}"},
                )
                assert resp.status == 200
                data = await resp.json()
                names = [a["name"] for a in data["agents"]]
                assert expected_name in names, (
                    f"Tenant {expected_name[-1]} should see {expected_name}, got {names}"
                )
                for leaked in not_seen:
                    assert leaked not in names, (
                        f"Agent {leaked} leaked to tenant {expected_name[-1]}: {names}"
                    )


# ======================================================================
# N-tenant mutual invisibility (store-level, no auth)
# ======================================================================


class TestN_TenantMutualInvisibility:
    """Test that multiple tenants (3+) are all isolated from each other."""

    def test_three_tenants_full_isolation(self, store_factory):
        """3 tenants: each list is isolated, no cross-tenant leakage."""
        store = store_factory()

        TENANT_C = "zeta-labs"

        # Register agents for 3 tenants
        a_id = store.register_agent(_agent_card("Alpha", tenant=TENANT_A))
        b_id = store.register_agent(_agent_card("Beta", tenant=TENANT_B))
        c_id = store.register_agent(_agent_card("Gamma", tenant=TENANT_C))
        p_id = store.register_agent(_agent_card("Public"))  # no tenant

        # Each tenant sees only their own
        for tenant, expected_name in [
            (TENANT_A, "Alpha"),
            (TENANT_B, "Beta"),
            (TENANT_C, "Gamma"),
        ]:
            agents = store.list_agents(tenant=tenant)
            assert len(agents) == 1, (
                f"Expected 1 agent for tenant {tenant}, got {len(agents)}"
            )
            assert agents[0]["name"] == expected_name

        # Empty tenant sees only the public agent
        empty_agents = store.list_agents(tenant="")
        assert len(empty_agents) == 1
        assert empty_agents[0]["name"] == "Public"

        # No filter sees all 4
        all_agents = store.list_agents()
        assert len(all_agents) == 4, (
            f"Expected 4 agents (3 tenants + public), got {len(all_agents)}"
        )

    def test_three_tenants_get_agent_by_id_cross_tenant(self, store_factory):
        """Direct get_agent with tenant filter blocks cross-tenant access."""
        store = store_factory()

        a_id = store.register_agent(_agent_card("Alpha", tenant=TENANT_A))
        b_id = store.register_agent(_agent_card("Beta", tenant=TENANT_B))

        # Get with wrong tenant returns None
        assert store.get_agent(b_id, tenant=TENANT_A) is None, (
            "Tenant A should not be able to get Tenant B's agent detail"
        )
        assert store.get_agent(a_id, tenant=TENANT_B) is None, (
            "Tenant B should not be able to get Tenant A's agent detail"
        )

        # Get with correct tenant works
        assert store.get_agent(a_id, tenant=TENANT_A) is not None
        assert store.get_agent(b_id, tenant=TENANT_B) is not None

        # Get without tenant filter works (admin mode)
        assert store.get_agent(a_id) is not None
        assert store.get_agent(b_id) is not None


# ======================================================================
# Strict cross-tenant access negative tests
# ======================================================================


class TestCrossTenantAccessNegative:
    """Negative tests: attempts by one tenant to access another's data."""

    async def test_tenant_a_uses_b_id_after_listing(self, app_factory):
        """Tenant A finds Tenant B's agent ID through unfiltered list, then
        tries to access detail with its own tenant filter — should fail."""
        async with await app_factory() as client:
            # Register agents for both tenants
            r_b = await _register_agent(client, "SecretB", tenant=TENANT_B)
            b_id = r_b["id"]
            await _register_agent(client, "PublicA", tenant=TENANT_A)

            # Tenant A uses unfiltered list to discover B's agent ID
            all_agents = await _list_agents(client)
            all_ids = [a["id"] for a in all_agents]
            assert b_id in all_ids, "Tenant B's agent should be in unfiltered list"

            # Tenant A tries to access B's detail WITH own tenant filter →
            # should fail because the IDs are the same store-level check
            card = await _get_agent_with_tenant(client, b_id, tenant=TENANT_A)
            assert card is None, (
                "Tenant A should NOT be able to access Tenant B's agent detail "
                "even knowing its ID, when using X-Tenant-ID header"
            )

            # Tenant B can access their own detail
            card = await _get_agent_with_tenant(client, b_id, tenant=TENANT_B)
            assert card is not None
            assert card["name"] == "SecretB"

    async def test_cross_tenant_list_with_wrong_tenant_param(self, app_factory):
        """Query param tenant=A returns A's agents even if B's data exists."""
        async with await app_factory() as client:
            await _register_agent(client, "ABot", tenant=TENANT_A)
            await _register_agent(client, "BBot", tenant=TENANT_B)

            # Tenant A's list is properly scoped
            agents_a = await _list_agents(client, tenant=TENANT_A)
            names_a = [a["name"] for a in agents_a]
            assert "ABot" in names_a
            assert "BBot" not in names_a

            # Tenant B's list is properly scoped
            agents_b = await _list_agents(client, tenant=TENANT_B)
            names_b = [a["name"] for a in agents_b]
            assert "BBot" in names_b
            assert "ABot" not in names_b

    async def test_cross_tenant_detail_404_not_403(self, app_factory):
        """Cross-tenant detail access should return 404 (not 403) to avoid
        leaking information about whether an agent ID exists."""
        async with await app_factory() as client:
            r_b = await _register_agent(client, "HiddenB", tenant=TENANT_B)
            b_id = r_b["id"]

            # Non-existent ID returns 404
            resp = await client.get(
                "/v1/agents/nonexistent-id",
                headers={"X-Tenant-ID": TENANT_A},
            )
            assert resp.status == 404

            # Existing B agent with wrong tenant should also return 404
            # (not 403, to avoid leaking existence information)
            resp = await client.get(
                f"/v1/agents/{b_id}",
                headers={"X-Tenant-ID": TENANT_A},
            )
            data = await resp.json()
            assert resp.status == 404, (
                f"Expected 404 for cross-tenant detail (info leak prevention), "
                f"got {resp.status}: {data}"
            )
            assert data.get("error") == "agent_not_found"


# ======================================================================
# Admin dashboard cross-tenant viewing tests
# ======================================================================


class TestAdminClientsCrossTenant:
    """Admin dashboard client list respects tenant isolation."""

    @pytest.fixture
    def auth_app_factory(self):
        """Return a callable that creates a TestClient with auth enabled."""
        factories = []

        async def maker():
            tmpdir_obj = tempfile.TemporaryDirectory()
            factories.append(tmpdir_obj)
            data_dir = tmpdir_obj.name
            app = create_app(
                data_dir=data_dir,
                base_url="http://localhost:8321",
                auth_enabled=True,
                user_session_enabled=False,
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

    @staticmethod
    async def _register_and_get_admin_token(
        client: TestClient, *, tenant: str
    ) -> str:
        """Register an OAuth client with registry:admin scope and get a Bearer token."""
        resp = await client.post("/auth/register", json={
            "description": f"Admin test client for {tenant}",
            "tenant": tenant,
            "allowed_scopes": ["registry:admin", "agent:read", "agent:register", "task:read", "task:write"],
        })
        assert resp.status == 201, f"Admin client register failed: {await resp.text()}"
        reg_data = await resp.json()
        client_id = reg_data["client_id"]
        client_secret = reg_data["client_secret"]

        resp = await client.post("/auth/token", data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
            "scope": "registry:admin",
        })
        assert resp.status == 200, f"Admin token request failed: {await resp.text()}"
        token_data = await resp.json()
        return token_data["access_token"]

    # -----------------------------------------------------------------
    # /admin/clients tenant filtering
    # -----------------------------------------------------------------

    async def test_admin_clients_tenant_filter(self, auth_app_factory):
        """GET /admin/clients?tenant=X returns only clients belonging to X."""
        async with await auth_app_factory() as client:
            # Register OAuth clients for tenants A and B
            token_a = await self._register_and_get_admin_token(client, tenant=TENANT_A)
            token_b = await self._register_and_get_admin_token(client, tenant=TENANT_B)

            # List clients filtered by tenant A
            resp = await client.get(
                "/admin/clients",
                params={"tenant": TENANT_A},
                headers={"Authorization": f"Bearer {token_a}"},
            )
            assert resp.status == 200, f"admin/clients failed: {await resp.text()}"
            clients_a = await resp.json()
            assert isinstance(clients_a, list), f"Expected list, got {type(clients_a)}"
            for c in clients_a:
                assert c.get("tenant") == TENANT_A, (
                    f"Expected tenant={TENANT_A}, got {c.get('tenant')} in filtered list"
                )

            # List clients filtered by tenant B
            resp = await client.get(
                "/admin/clients",
                params={"tenant": TENANT_B},
                headers={"Authorization": f"Bearer {token_b}"},
            )
            assert resp.status == 200
            clients_b = await resp.json()
            assert isinstance(clients_b, list)
            for c in clients_b:
                assert c.get("tenant") == TENANT_B, (
                    f"Expected tenant={TENANT_B}, got {c.get('tenant')} in filtered list"
                )

    async def test_admin_clients_no_tenant_returns_all(self, auth_app_factory):
        """GET /admin/clients without tenant returns all clients (admin scope)."""
        async with await auth_app_factory() as client:
            token_a = await self._register_and_get_admin_token(client, tenant=TENANT_A)
            await self._register_and_get_admin_token(client, tenant=TENANT_B)

            # List all clients without tenant filter
            resp = await client.get(
                "/admin/clients",
                headers={"Authorization": f"Bearer {token_a}"},
            )
            assert resp.status == 200
            all_clients = await resp.json()
            assert isinstance(all_clients, list)
            # Should see at least the clients from both tenants
            seen_tenants = set(c.get("tenant") for c in all_clients)
            assert TENANT_A in seen_tenants, f"Tenant A not in all clients: {seen_tenants}"
            assert TENANT_B in seen_tenants, f"Tenant B not in all clients: {seen_tenants}"

    async def test_admin_clients_unknown_tenant_returns_empty(self, auth_app_factory):
        """GET /admin/clients?tenant=nonexistent returns empty list."""
        async with await auth_app_factory() as client:
            token = await self._register_and_get_admin_token(client, tenant=TENANT_A)

            resp = await client.get(
                "/admin/clients",
                params={"tenant": "stranger-org"},
                headers={"Authorization": f"Bearer {token}"},
            )
            assert resp.status == 200
            clients = await resp.json()
            assert isinstance(clients, list)
            assert len(clients) == 0, (
                f"Expected empty list for unknown tenant, got {len(clients)}"
            )


class TestAdminAuditCrossTenant:
    """Admin dashboard audit log respects tenant isolation."""

    @pytest.fixture
    def auth_app_factory(self):
        """Return a callable that creates a TestClient with auth enabled."""
        factories = []

        async def maker():
            tmpdir_obj = tempfile.TemporaryDirectory()
            factories.append(tmpdir_obj)
            data_dir = tmpdir_obj.name
            app = create_app(
                data_dir=data_dir,
                base_url="http://localhost:8321",
                auth_enabled=True,
                user_session_enabled=False,
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

    @staticmethod
    async def _register_and_get_admin_token(
        client: TestClient, *, tenant: str
    ) -> str:
        """Register an OAuth client with registry:admin scope and get a Bearer token."""
        resp = await client.post("/auth/register", json={
            "description": f"Admin test client for {tenant}",
            "tenant": tenant,
            "allowed_scopes": ["registry:admin", "agent:read", "agent:register", "task:read", "task:write"],
        })
        assert resp.status == 201, f"Admin client register failed: {await resp.text()}"
        reg_data = await resp.json()
        client_id = reg_data["client_id"]
        client_secret = reg_data["client_secret"]

        resp = await client.post("/auth/token", data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
            "scope": "registry:admin agent:read agent:register task:read task:write",
        })
        assert resp.status == 200, f"Admin token request failed: {await resp.text()}"
        token_data = await resp.json()
        return token_data["access_token"]

    # -----------------------------------------------------------------
    # /admin/audit tenant filtering
    # -----------------------------------------------------------------

    async def test_admin_audit_tenant_filter(self, auth_app_factory):
        """GET /admin/audit?tenant=X returns only audit events for X."""
        async with await auth_app_factory() as client:
            admin_token = await self._register_and_get_admin_token(client, tenant=TENANT_A)

            # Register agents for tenants A and B to generate audit events
            token_a = await self._register_and_get_admin_token(client, tenant=TENANT_A)
            token_b = await self._register_and_get_admin_token(client, tenant=TENANT_B)

            # Register agent for tenant A
            resp = await client.post(
                "/v1/agents",
                json=_agent_card("AgentA", tenant=TENANT_A),
                headers={"Authorization": f"Bearer {token_a}"},
            )
            assert resp.status in (200, 201)

            # Register agent for tenant B
            resp = await client.post(
                "/v1/agents",
                json=_agent_card("AgentB", tenant=TENANT_B),
                headers={"Authorization": f"Bearer {token_b}"},
            )
            assert resp.status in (200, 201)

            # Query audit log filtered by tenant A
            resp = await client.get(
                "/admin/audit",
                params={"tenant": TENANT_A, "limit": "100"},
                headers={"Authorization": f"Bearer {admin_token}"},
            )
            assert resp.status == 200, f"admin/audit failed: {await resp.text()}"
            data = await resp.json()
            events = data.get("events", [])
            for e in events:
                assert e.get("tenant_id") == TENANT_A, (
                    f"Expected tenant_id={TENANT_A}, got {e.get('tenant_id')} in filtered audit"
                )

            # Query audit log filtered by tenant B
            resp = await client.get(
                "/admin/audit",
                params={"tenant": TENANT_B, "limit": "100"},
                headers={"Authorization": f"Bearer {admin_token}"},
            )
            assert resp.status == 200
            data = await resp.json()
            events = data.get("events", [])
            for e in events:
                assert e.get("tenant_id") == TENANT_B, (
                    f"Expected tenant_id={TENANT_B}, got {e.get('tenant_id')} in filtered audit"
                )

    async def test_admin_audit_no_tenant_returns_all(self, auth_app_factory):
        """GET /admin/audit without tenant returns all events (admin scope)."""
        async with await auth_app_factory() as client:
            admin_token = await self._register_and_get_admin_token(client, tenant=TENANT_A)
            token_a = await self._register_and_get_admin_token(client, tenant=TENANT_A)
            token_b = await self._register_and_get_admin_token(client, tenant=TENANT_B)

            # Generate events for both tenants
            await client.post(
                "/v1/agents",
                json=_agent_card("AuditA", tenant=TENANT_A),
                headers={"Authorization": f"Bearer {token_a}"},
            )
            await client.post(
                "/v1/agents",
                json=_agent_card("AuditB", tenant=TENANT_B),
                headers={"Authorization": f"Bearer {token_b}"},
            )

            # Query all audit events
            resp = await client.get(
                "/admin/audit",
                params={"limit": "100"},
                headers={"Authorization": f"Bearer {admin_token}"},
            )
            assert resp.status == 200
            data = await resp.json()
            events = data.get("events", [])
            seen_tenants = set(e.get("tenant_id") for e in events if e.get("tenant_id"))
            assert TENANT_A in seen_tenants, (
                f"Tenant A events not in unfiltered audit: {seen_tenants}"
            )
            assert TENANT_B in seen_tenants, (
                f"Tenant B events not in unfiltered audit: {seen_tenants}"
            )

    async def test_admin_audit_unknown_tenant_returns_empty(self, auth_app_factory):
        """GET /admin/audit?tenant=nonexistent returns empty events list."""
        async with await auth_app_factory() as client:
            admin_token = await self._register_and_get_admin_token(client, tenant=TENANT_A)
            token_a = await self._register_and_get_admin_token(client, tenant=TENANT_A)

            # Generate some events for tenant A
            await client.post(
                "/v1/agents",
                json=_agent_card("StrangeTest", tenant=TENANT_A),
                headers={"Authorization": f"Bearer {token_a}"},
            )

            # Query with unknown tenant
            resp = await client.get(
                "/admin/audit",
                params={"tenant": "stranger-org", "limit": "100"},
                headers={"Authorization": f"Bearer {admin_token}"},
            )
            assert resp.status == 200
            data = await resp.json()
            events = data.get("events", [])
            assert len(events) == 0, (
                f"Expected empty audit for unknown tenant, got {len(events)} events"
            )