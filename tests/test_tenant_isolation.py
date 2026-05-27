"""
P6-A5-1: 不同租户注册 agent 互相不可见的隔离测试

覆盖场景:
  1. 注册 — agent 携带租户标识并正确存储
  2. 列表隔离 — Tenant A 看不到 Tenant B 的 agent
  3. 详情隔离 — Tenant A 获取 Tenant B 的 agent 详情返回 404
  4. Heartbeat 隔离 — Tenant A 不能对 Tenant B 的 agent 发心跳
  5. Toggle 隔离 — Tenant A 不能切换 Tenant B 的 agent 的禁用状态
  6. Unregister 隔离 — Tenant A 不能删除 Tenant B 的 agent
  7. 三租户互不可见 — N=3 的完全隔离
  8. 空字符串租户向后兼容 — 空 tenant 旧 agent 对所有租户 visible
  9. 租户+搜索组合过滤

注意: 本测试覆盖现有 test_tenant_e2e.py 之外的隔离场景，
      特别是 heartbeat / toggle / unregister 的跨租户安全防线。
"""
from __future__ import annotations

import json
import tempfile
import time

import pytest
from aiohttp.test_utils import TestServer, TestClient

from simple_a2a_registry.server import create_app
from simple_a2a_registry.store import Store
from simple_a2a_registry.auth import create_token, SCOPES, ISSUER

TENANT_A = "acme-corp"
TENANT_B = "globex-inc"
TENANT_C = "zeta-labs"


# ======================================================================
# Fixtures
# ======================================================================


@pytest.fixture(scope="module")
def anyio_backend():
    return "asyncio"


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


# ======================================================================
# Helpers
# ======================================================================


def _agent_card(name: str, *, tenant: str = "", description: str = "") -> dict:
    """Build an AgentCard dict."""
    card: dict = {
        "name": name,
        "description": description or f"Agent {name}",
        "interfaces": [{
            "url": f"https://agent.{name.lower()}.example.com",
            "protocol_binding": "JSONRPC",
            "protocol_version": "1.0",
        }],
    }
    if tenant:
        card["tenant"] = tenant
    return card


async def _register(client, name: str, *, tenant: str = "", description: str = "") -> dict:
    """Register an agent via HTTP."""
    card = _agent_card(name, tenant=tenant, description=description)
    resp = await client.post("/v1/agents", json=card)
    assert resp.status in (200, 201), f"register {name} failed: {await resp.text()}"
    return await resp.json()


async def _list(client, *, tenant: str = None) -> list[dict]:
    """List agents, optionally filtered."""
    params = {}
    if tenant is not None:
        params["tenant"] = tenant
    resp = await client.get("/v1/agents", params=params)
    assert resp.status == 200
    data = await resp.json()
    return data["agents"]


async def _get(client, agent_id: str, *, tenant: str = None) -> dict | None:
    """Get agent detail, optionally with tenant header."""
    headers = {}
    if tenant is not None:
        headers["X-Tenant-ID"] = tenant
    resp = await client.get(f"/v1/agents/{agent_id}", headers=headers)
    if resp.status == 200:
        return await resp.json()
    return None


async def _heartbeat(client, agent_id: str, *, tenant: str = None) -> int:
    """Send heartbeat, optionally with tenant query param."""
    params = {}
    if tenant is not None:
        params["tenant"] = tenant
    resp = await client.post(f"/v1/agents/{agent_id}/heartbeat", params=params)
    return resp.status


async def _toggle(client, agent_id: str, *, tenant: str = None) -> int:
    """Toggle agent disabled status, optionally with tenant query param."""
    params = {}
    if tenant is not None:
        params["tenant"] = tenant
    resp = await client.post(f"/v1/agents/{agent_id}/toggle", params=params)
    return resp.status


async def _unregister(client, agent_id: str, *, tenant: str = None) -> int:
    """Unregister an agent, optionally with tenant query param."""
    params = {}
    if tenant is not None:
        params["tenant"] = tenant
    resp = await client.delete(f"/v1/agents/{agent_id}", params=params)
    return resp.status


# ======================================================================
# Store-level isolation tests
# ======================================================================


class TestStoreHeartbeatIsolation:
    """Store-level heartbeat tenant isolation."""

    def test_heartbeat_with_correct_tenant_succeeds(self, store_factory):
        """Agent A's heartbeat with right tenant works."""
        store = store_factory()
        aid = store.register_agent(_agent_card("Alpha", tenant=TENANT_A))
        assert store.heartbeat(aid, tenant=TENANT_A) is True

    def test_heartbeat_with_wrong_tenant_fails(self, store_factory):
        """Heartbeat with wrong tenant returns False (no row updated)."""
        store = store_factory()
        aid = store.register_agent(_agent_card("Alpha", tenant=TENANT_A))
        assert store.heartbeat(aid, tenant=TENANT_B) is False

    def test_heartbeat_no_tenant_still_works(self, store_factory):
        """Heartbeat without tenant (admin scope) works for any agent."""
        store = store_factory()
        aid = store.register_agent(_agent_card("Alpha", tenant=TENANT_A))
        assert store.heartbeat(aid) is True


class TestStoreUnregisterIsolation:
    """Store-level unregister tenant isolation."""

    def test_unregister_with_correct_tenant_succeeds(self, store_factory):
        """Unregister with right tenant removes the agent."""
        store = store_factory()
        aid = store.register_agent(_agent_card("Alpha", tenant=TENANT_A))
        assert store.unregister(aid, tenant=TENANT_A) is True
        assert store.get_agent(aid) is None

    def test_unregister_with_wrong_tenant_fails(self, store_factory):
        """Unregister with wrong tenant returns False."""
        store = store_factory()
        aid = store.register_agent(_agent_card("Alpha", tenant=TENANT_A))
        assert store.unregister(aid, tenant=TENANT_B) is False
        assert store.get_agent(aid) is not None  # agent still exists

    def test_unregister_no_tenant_still_works(self, store_factory):
        """Unregister without tenant (admin scope) works."""
        store = store_factory()
        aid = store.register_agent(_agent_card("Alpha", tenant=TENANT_A))
        assert store.unregister(aid) is True


class TestStoreToggleIsolation:
    """Store-level toggle tenant isolation."""

    def test_toggle_with_correct_tenant_succeeds(self, store_factory):
        """Toggle with right tenant works."""
        store = store_factory()
        aid = store.register_agent(_agent_card("Alpha", tenant=TENANT_A))
        result = store.toggle_agent(aid, tenant=TENANT_A)
        assert result is True  # now disabled

    def test_toggle_with_wrong_tenant_returns_none(self, store_factory):
        """Toggle with wrong tenant returns None (agent not found)."""
        store = store_factory()
        aid = store.register_agent(_agent_card("Alpha", tenant=TENANT_A))
        result = store.toggle_agent(aid, tenant=TENANT_B)
        assert result is None

    def test_toggle_no_tenant_still_works(self, store_factory):
        """Toggle without tenant (admin scope) works."""
        store = store_factory()
        aid = store.register_agent(_agent_card("Alpha", tenant=TENANT_A))
        assert store.toggle_agent(aid) is not None


class TestStoreFullLifecycleIsolation:
    """Complete lifecycle: register → list → heartbeat → toggle → unregister, tenant-scoped."""

    def test_tenant_a_full_lifecycle(self, store_factory):
        """Tenant A can perform all lifecycle ops on its own agent."""
        store = store_factory()
        aid = store.register_agent(_agent_card("MyBot", tenant=TENANT_A))

        assert store.get_agent(aid, tenant=TENANT_A) is not None
        assert store.heartbeat(aid, tenant=TENANT_A) is True
        assert store.toggle_agent(aid, tenant=TENANT_A) is True
        assert store.unregister(aid, tenant=TENANT_A) is True

    def test_tenant_b_cannot_touch_tenant_a_agent(self, store_factory):
        """Tenant B cannot heartbeat/toggle/unregister Tenant A's agent."""
        store = store_factory()
        aid = store.register_agent(_agent_card("Secret", tenant=TENANT_A))

        # Should not see it in list
        agents = store.list_agents(tenant=TENANT_B)
        assert len(agents) == 0

        # Should not be able to get it
        assert store.get_agent(aid, tenant=TENANT_B) is None

        # Should not be able to heartbeat it
        assert store.heartbeat(aid, tenant=TENANT_B) is False

        # Should not be able to toggle it
        assert store.toggle_agent(aid, tenant=TENANT_B) is None

        # Should not be able to unregister it
        assert store.unregister(aid, tenant=TENANT_B) is False

        # Agent still exists for Tenant A
        assert store.get_agent(aid, tenant=TENANT_A) is not None


# ======================================================================
# HTTP-level isolation tests (E2E)
# ======================================================================


class TestHttpHeartbeatIsolation:
    """HTTP POST /v1/agents/{id}/heartbeat tenant isolation."""
    pytestmark = pytest.mark.asyncio

    async def test_heartbeat_no_tenant_works(self, app_factory):
        """Heartbeat without tenant param succeeds (admin scope)."""
        async with await app_factory() as client:
            r = await _register(client, "Bot", tenant=TENANT_A)
            aid = r["id"]
            status = await _heartbeat(client, aid)
            assert status == 203, f"Expected 203, got {status}"

    async def test_heartbeat_correct_tenant_succeeds(self, app_factory):
        """Heartbeat with correct tenant succeeds."""
        async with await app_factory() as client:
            r = await _register(client, "BotA", tenant=TENANT_A)
            aid = r["id"]
            status = await _heartbeat(client, aid, tenant=TENANT_A)
            assert status == 203, f"Expected 203, got {status}"

    async def test_heartbeat_wrong_tenant_returns_404(self, app_factory):
        """Heartbeat with wrong tenant returns 404 (agent not visible)."""
        async with await app_factory() as client:
            r = await _register(client, "SecretA", tenant=TENANT_A)
            aid = r["id"]
            status = await _heartbeat(client, aid, tenant=TENANT_B)
            assert status == 404, f"Expected 404, got {status}"


class TestHttpToggleIsolation:
    """HTTP POST /v1/agents/{id}/toggle tenant isolation."""
    pytestmark = pytest.mark.asyncio

    async def test_toggle_correct_tenant_succeeds(self, app_factory):
        """Toggle with correct tenant succeeds."""
        async with await app_factory() as client:
            r = await _register(client, "BotA", tenant=TENANT_A)
            aid = r["id"]
            status = await _toggle(client, aid, tenant=TENANT_A)
            assert status == 200, f"Expected 200, got {status}"

    async def test_toggle_wrong_tenant_returns_404(self, app_factory):
        """Toggle with wrong tenant returns 404."""
        async with await app_factory() as client:
            r = await _register(client, "SecretA", tenant=TENANT_A)
            aid = r["id"]
            status = await _toggle(client, aid, tenant=TENANT_B)
            assert status == 404, f"Expected 404, got {status}"


class TestHttpUnregisterIsolation:
    """HTTP DELETE /v1/agents/{id} tenant isolation."""
    pytestmark = pytest.mark.asyncio

    async def test_unregister_correct_tenant_succeeds(self, app_factory):
        """Unregister with correct tenant succeeds."""
        async with await app_factory() as client:
            r = await _register(client, "BotA", tenant=TENANT_A)
            aid = r["id"]

            status = await _unregister(client, aid, tenant=TENANT_A)
            assert status == 200, f"Expected 200, got {status}"

            # Agent is gone
            assert await _get(client, aid) is None

    async def test_unregister_wrong_tenant_returns_404(self, app_factory):
        """Unregister with wrong tenant returns 404."""
        async with await app_factory() as client:
            r = await _register(client, "SecretA", tenant=TENANT_A)
            aid = r["id"]

            status = await _unregister(client, aid, tenant=TENANT_B)
            assert status == 404, f"Expected 404, got {status}"

            # Agent still exists for Tenant A
            card = await _get(client, aid, tenant=TENANT_A)
            assert card is not None
            assert card["name"] == "SecretA"


class TestN_TenantFullIsolation:
    """N=3 tenants: complete mutual invisibility across all lifecycle ops."""
    pytestmark = pytest.mark.asyncio

    async def test_three_tenants_complete_isolation(self, app_factory):
        """3 tenants: each cannot heartbeat/toggle/unregister another's agents."""
        TENANTS = {
            TENANT_A: "AgentA",
            TENANT_B: "AgentB",
            TENANT_C: "AgentC",
        }
        all_ids: dict[str, str] = {}

        async with await app_factory() as client:
            # Register one agent per tenant
            for tenant, name in TENANTS.items():
                r = await _register(client, name, tenant=tenant)
                all_ids[tenant] = r["id"]

            # Each tenant operates only on their own agent
            for my_tenant in TENANTS:
                my_id = all_ids[my_tenant]
                for other_tenant, other_name in TENANTS.items():
                    other_id = all_ids[other_tenant]

                    if other_tenant == my_tenant:
                        # Self operations should succeed
                        status_hb = await _heartbeat(client, my_id, tenant=my_tenant)
                        assert status_hb == 203, (
                            f"{my_tenant} heartbeat own agent failed: {status_hb}"
                        )
                    else:
                        # Cross-tenant operations should fail with 404
                        status_hb = await _heartbeat(client, other_id, tenant=my_tenant)
                        assert status_hb == 404, (
                            f"{my_tenant} should NOT heartbeat {other_tenant}'s agent, "
                            f"got {status_hb}"
                        )

            # Verify list isolation
            for tenant, name in TENANTS.items():
                agents = await _list(client, tenant=tenant)
                names = [a["name"] for a in agents]
                assert name in names, (
                    f"{tenant} should see their own agent {name}, got {names}"
                )
                for other_t, other_n in TENANTS.items():
                    if other_t != tenant:
                        assert other_n not in names, (
                            f"{other_n} leaked into {tenant}'s list: {names}"
                        )


class TestVisibilityAfterOperation:
    """Operations don't change visibility — invisible agents stay invisible."""
    pytestmark = pytest.mark.asyncio

    async def test_cross_tenant_heartbeat_does_not_expose_agent(self, app_factory):
        """Failed cross-tenant heartbeat doesn't make the agent visible."""
        async with await app_factory() as client:
            r = await _register(client, "Hidden", tenant=TENANT_A)
            aid = r["id"]

            # Tenant B fails to heartbeat
            status = await _heartbeat(client, aid, tenant=TENANT_B)
            assert status == 404

            # Tenant B still cannot list Tenant A's agent
            agents = await _list(client, tenant=TENANT_B)
            names = [a["name"] for a in agents]
            assert "Hidden" not in names

    async def test_cross_tenant_unregister_preserves_agent(self, app_factory):
        """Failed cross-tenant unregister doesn't remove the agent."""
        async with await app_factory() as client:
            r = await _register(client, "Safe", tenant=TENANT_A)
            aid = r["id"]

            # Tenant B fails to unregister
            status = await _unregister(client, aid, tenant=TENANT_B)
            assert status == 404

            # Agent still exists for Tenant A
            card = await _get(client, aid, tenant=TENANT_A)
            assert card is not None
            assert card["name"] == "Safe"


class TestBackwardCompatibility:
    """Empty-string tenant backward compatibility."""
    pytestmark = pytest.mark.asyncio

    async def test_no_tenant_agent_is_accessible_without_filter(self, app_factory):
        """Agent without tenant is visible in unfiltered list."""
        async with await app_factory() as client:
            await _register(client, "LegacyBot")
            all_agents = await _list(client)
            names = [a["name"] for a in all_agents]
            assert "LegacyBot" in names

    async def test_tenant_a_cannot_see_no_tenant_agent(self, app_factory):
        """Tenant A's list includes no-tenant agents (backward compatibility:
        empty-string tenant agents are visible to all named tenants)."""
        async with await app_factory() as client:
            await _register(client, "Legacy")
            r = await _register(client, "Tenanted", tenant=TENANT_A)

            # Backward compat: Tenant A sees own agents + empty-tenant legacy agents
            agents = await _list(client, tenant=TENANT_A)
            names = [a["name"] for a in agents]
            assert "Tenanted" in names
            assert "Legacy" in names, (
                "Backward compat: Tenant A should see no-tenant legacy agents"
            )

            # Empty tenant filter is treated as admin scope (shows all)
            empty_agents = await _list(client, tenant="")
            empty_names = [a["name"] for a in empty_agents]
            assert "Legacy" in empty_names
            assert "Tenanted" in empty_names, (
                "Empty-string tenant = admin scope, shows all agents"
            )

    async def test_mixed_tenant_and_no_tenant_operations(self, app_factory):
        """Operations on no-tenant agents work for admin but not for named tenants."""
        async with await app_factory() as client:
            r = await _register(client, "Legacy")
            aid = r["id"]

            # Tenant A cannot heartbeat the no-tenant agent
            status = await _heartbeat(client, aid, tenant=TENANT_A)
            assert status == 404, "Tenant A should not heartbeat a no-tenant agent"

            # Tenant A cannot unregister the no-tenant agent
            status = await _unregister(client, aid, tenant=TENANT_A)
            assert status == 404, "Tenant A should not unregister a no-tenant agent"

            # Admin (no tenant) can still operate on the no-tenant agent
            status = await _heartbeat(client, aid)
            assert status == 203, f"Admin heartbeat of no-tenant agent failed: {status}"


class TestTenantListComposition:
    """Tenant filter composes with other filters."""
    pytestmark = pytest.mark.asyncio

    async def test_list_with_tenant_and_search(self, app_factory):
        """tenant + text search: returns only matching agents in that tenant."""
        async with await app_factory() as client:
            await _register(client, "Alpha-Billing", tenant=TENANT_A,
                          description="Billing service")
            await _register(client, "Alpha-Monitor", tenant=TENANT_A,
                          description="Monitoring service")
            await _register(client, "Beta-Billing", tenant=TENANT_B,
                          description="Billing service")

            # Tenant A searching for "Billing" should see only Alpha-Billing
            resp = await client.get("/v1/agents", params={
                "tenant": TENANT_A, "q": "Billing",
            })
            data = await resp.json()
            names = [a["name"] for a in data["agents"]]
            assert "Alpha-Billing" in names
            assert "Beta-Billing" not in names
            assert "Alpha-Monitor" not in names, (
                "Non-matching search result leaked in"
            )

    async def test_list_with_tenant_and_pagination(self, app_factory):
        """tenant + limit/offset works correctly."""
        async with await app_factory() as client:
            for i in range(5):
                await _register(client, f"A-Agent{i}", tenant=TENANT_A)
            await _register(client, "B-Agent", tenant=TENANT_B)

            resp = await client.get("/v1/agents", params={
                "tenant": TENANT_A, "limit": "2", "offset": "0",
            })
            data = await resp.json()
            assert len(data["agents"]) == 2
            assert data["total"] == 5

            # Page 2
            resp2 = await client.get("/v1/agents", params={
                "tenant": TENANT_A, "limit": "2", "offset": "2",
            })
            data2 = await resp2.json()
            assert len(data2["agents"]) == 2
            assert data2["total"] == 5