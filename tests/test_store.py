"""Unit tests for A2A Registry Store (v1.0 AgentCard)."""
from __future__ import annotations

import hashlib
import tempfile
import time

from simple_a2a_registry.store import Store


def test_empty_stats() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        store = Store(tmpdir)
        s = store.stats()
        assert s["totalAgents"] == 0
        assert s["aliveAgents"] == 0
        assert s["staleAgents"] == 0


def test_register_and_get() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        store = Store(tmpdir)
        aid = store.register_agent({
            "name": "Test Agent",
            "description": "A test",
            "supported_interfaces": [
                {"url": "https://test.agent", "protocol_binding": "JSONRPC", "protocol_version": "1.0"},
            ],
        })
        assert aid

        card = store.get_agent(aid)
        assert card is not None
        assert card["name"] == "Test Agent"
        assert card["status"] == "alive"
        assert "id" in card
        assert card["id"] == aid


def test_get_nonexistent() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        store = Store(tmpdir)
        assert store.get_agent("nobody") is None


def test_list_all() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        store = Store(tmpdir)
        a1 = store.register_agent({"name": "A", "description": "Agent A"})
        a2 = store.register_agent({"name": "B", "description": "Agent B"})
        agents = store.list_agents()
        assert len(agents) == 2


def test_list_filter_skill() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        store = Store(tmpdir)
        store.register_agent({
            "name": "With Skill",
            "description": "Has a skill",
            "skills": [{"id": "s1", "name": "Data Analysis", "description": "Analyze data", "tags": ["data"]}],
        })
        store.register_agent({"name": "No Skill", "description": "No skills"})
        assert len(store.list_agents(skill="Data Analysis")) == 1
        assert len(store.list_agents(skill="Nonexistent")) == 0


def test_list_search() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        store = Store(tmpdir)
        store.register_agent({
            "name": "Search Me",
            "description": "Find me by keyword",
        })
        store.register_agent({"name": "Other", "description": "Boring"})
        assert len(store.list_agents(q="keyword")) == 1
        assert len(store.list_agents(q="find")) == 1
        assert len(store.list_agents(q="nope")) == 0


def test_heartbeat() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        store = Store(tmpdir)
        aid = store.register_agent({"name": "Heartbeat Agent", "description": "Test"})
        assert store.heartbeat(aid) is True
        assert store.heartbeat("nonexistent") is False


def test_unregister() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        store = Store(tmpdir)
        aid = store.register_agent({"name": "Remove Me", "description": "Will be removed"})
        assert store.unregister(aid) is True
        assert store.get_agent(aid) is None
        assert store.unregister("nonexistent") is False


def test_purge_stale() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        store = Store(tmpdir)
        aid = store.register_agent({"name": "Stale Agent", "description": "Will go stale"})
        # Manually set a very old heartbeat via the engine
        store._engine.execute(
            "UPDATE agents SET heartbeat_at=? WHERE id=?",
            (time.time() - 9999, aid),
        )
        store._engine.commit()
        purged = store.purge_stale()
        assert purged >= 1
        assert store.get_agent(aid) is None


def test_stats_counts() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        store = Store(tmpdir)
        store.register_agent({"name": "E1", "description": "1"})
        store.register_agent({"name": "E2", "description": "2"})
        s = store.stats()
        assert s["totalAgents"] == 2
        assert s["aliveAgents"] == 2
        assert s["staleAgents"] == 0


def test_persistence() -> None:
    """Verify that data survives across store instances."""
    with tempfile.TemporaryDirectory() as tmpdir:
        store1 = Store(tmpdir)
        aid = store1.register_agent({
            "name": "Persistent",
            "description": "Will persist",
            "supported_interfaces": [
                {"url": "https://persist.test", "protocol_binding": "JSONRPC", "protocol_version": "1.0"},
            ],
        })
        store1.close()

        store2 = Store(tmpdir)
        assert store2.stats()["totalAgents"] == 1
        card = store2.get_agent(aid)
        assert card is not None
        assert card["name"] == "Persistent"
        store2.close()


def test_stats_by_tenant_empty() -> None:
    """stats_by_tenant returns empty stats when no agents exist."""
    with tempfile.TemporaryDirectory() as tmpdir:
        store = Store(tmpdir)
        result = store.stats_by_tenant()
        assert result == {}


def test_stats_by_tenant_with_agents() -> None:
    """stats_by_tenant groups agents correctly by tenant_id."""
    with tempfile.TemporaryDirectory() as tmpdir:
        store = Store(tmpdir)
        store.register_agent({"name": "A1", "description": "t1 agent"}, tenant="tenant1")
        store.register_agent({"name": "A2", "description": "t1 agent 2"}, tenant="tenant1")
        store.register_agent({"name": "B1", "description": "t2 agent"}, tenant="tenant2")
        store.register_agent({"name": "C1", "description": "no tenant"})

        result = store.stats_by_tenant()

        assert set(result.keys()) == {"tenant1", "tenant2", ""}
        assert result["tenant1"]["totalAgents"] == 2
        assert result["tenant1"]["aliveAgents"] == 2
        assert result["tenant2"]["totalAgents"] == 1
        assert result[""]["totalAgents"] == 1


def test_stats_with_tenant_filter() -> None:
    """Store.stats(tenant=...) filters correctly by tenant."""
    with tempfile.TemporaryDirectory() as tmpdir:
        store = Store(tmpdir)
        store.register_agent({"name": "A1", "description": "t1"}, tenant="tenant1")
        store.register_agent({"name": "A2", "description": "t1 b"}, tenant="tenant1")
        store.register_agent({"name": "B1", "description": "t2"}, tenant="tenant2")
        store.register_agent({"name": "G1", "description": "global"})

        # Global stats (no filter) should include all 4
        g = store.stats()
        assert g["totalAgents"] == 4

        # Filtered by tenant1 -> 2 agents
        t1 = store.stats(tenant="tenant1")
        assert t1["totalAgents"] == 2
        assert t1["aliveAgents"] == 2
        assert t1["staleAgents"] == 0

        # Filtered by tenant2 -> 1 agent
        t2 = store.stats(tenant="tenant2")
        assert t2["totalAgents"] == 1

        # Filtered by nonexistent -> 0 agents
        nx = store.stats(tenant="nonexistent")
        assert nx["totalAgents"] == 0

        # Empty string tenant filter -> same as global (4 agents)
        e = store.stats(tenant="")
        assert e["totalAgents"] == 4