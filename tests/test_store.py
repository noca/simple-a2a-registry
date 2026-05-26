"""Unit tests for A2A Registry Store (v1.0 AgentCard)."""
from __future__ import annotations

import asyncio
import tempfile
import time

from simple_a2a_registry.store import A2ARegistryStore


def test_empty_stats() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        store = A2ARegistryStore(tmpdir)
        s = store.stats()
        assert s["totalAgents"] == 0
        assert s["aliveAgents"] == 0
        assert s["staleAgents"] == 0


def test_register_and_get() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        store = A2ARegistryStore(tmpdir)
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
        store = A2ARegistryStore(tmpdir)
        assert store.get_agent("nobody") is None


def test_list_all() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        store = A2ARegistryStore(tmpdir)
        a1 = store.register_agent({"name": "A", "description": "Agent A"})
        a2 = store.register_agent({"name": "B", "description": "Agent B"})
        agents = store.list_agents()
        assert len(agents) == 2


def test_list_filter_skill() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        store = A2ARegistryStore(tmpdir)
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
        store = A2ARegistryStore(tmpdir)
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
        store = A2ARegistryStore(tmpdir)
        aid = store.register_agent({"name": "Heartbeat Agent", "description": "Test"})
        assert store.heartbeat(aid) is True
        assert store.heartbeat("nonexistent") is False


def test_unregister() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        store = A2ARegistryStore(tmpdir)
        aid = store.register_agent({"name": "Remove Me", "description": "Will be removed"})
        assert store.unregister(aid) is True
        assert store.get_agent(aid) is None
        assert store.unregister("nonexistent") is False


def test_purge_stale() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        store = A2ARegistryStore(tmpdir)
        aid = store.register_agent({"name": "Stale Agent", "description": "Will go stale"})
        store._heartbeats[aid] = time.time() - 9999
        purged = store.purge_stale()
        assert purged >= 1
        assert store.get_agent(aid) is None


def test_stats_counts() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        store = A2ARegistryStore(tmpdir)
        store.register_agent({"name": "E1", "description": "1"})
        store.register_agent({"name": "E2", "description": "2"})
        s = store.stats()
        assert s["totalAgents"] == 2
        assert s["aliveAgents"] == 2
        assert s["staleAgents"] == 0


def test_persistence() -> None:
    """Verify that data survives across store instances."""
    async def _run() -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store1 = A2ARegistryStore(tmpdir)
            aid = store1.register_agent({
                "name": "Persistent",
                "description": "Will persist",
                "supported_interfaces": [
                    {"url": "https://persist.test", "protocol_binding": "JSONRPC", "protocol_version": "1.0"},
                ],
            })
            await asyncio.sleep(0.05)

            store2 = A2ARegistryStore(tmpdir)
            assert store2.stats()["totalAgents"] == 1
            card = store2.get_agent(aid)
            assert card is not None
            assert card["name"] == "Persistent"

    asyncio.run(_run())