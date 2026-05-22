"""Unit tests for A2A Registry Store."""
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
        assert s["discoveredAgents"] == 0
        assert s["externalAgents"] == 0
        assert s["aliveAgents"] == 0


def test_register_and_get() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        store = A2ARegistryStore(tmpdir)
        aid = store.register_agent({
            "name": "Test Agent",
            "description": "A test",
            "url": "https://test.agent",
            "tags": ["test", "demo"],
        })
        assert aid

        card = store.get_agent(aid)
        assert card is not None
        assert card["name"] == "Test Agent"
        assert card["status"] == "alive"


def test_get_nonexistent() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        store = A2ARegistryStore(tmpdir)
        assert store.get_agent("nobody") is None


def test_list_all() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        store = A2ARegistryStore(tmpdir)
        a1 = store.register_agent({"name": "A"})
        a2 = store.register_agent({"name": "B"})
        agents = store.list_agents()
        assert len(agents) == 2


def test_list_filter_skill() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        store = A2ARegistryStore(tmpdir)
        store.register_agent({
            "name": "With Skill",
            "capabilities": {
                "skills": [{"id": "s1", "name": "Data Analysis"}],
            },
        })
        store.register_agent({"name": "No Skill"})
        assert len(store.list_agents(skill="Data Analysis")) == 1
        assert len(store.list_agents(skill="Nonexistent")) == 0


def test_list_filter_tag() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        store = A2ARegistryStore(tmpdir)
        store.register_agent({"name": "Tagged", "tags": ["python", "test"]})
        store.register_agent({"name": "Untagged"})
        assert len(store.list_agents(tag="python")) == 1
        assert len(store.list_agents(tag="nonexistent")) == 0


def test_list_search() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        store = A2ARegistryStore(tmpdir)
        store.register_agent({
            "name": "Search Me",
            "description": "Find me by keyword",
        })
        store.register_agent({"name": "Other"})
        assert len(store.list_agents(q="keyword")) == 1
        assert len(store.list_agents(q="find")) == 1
        assert len(store.list_agents(q="nope")) == 0


def test_heartbeat() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        store = A2ARegistryStore(tmpdir)
        aid = store.register_agent({"name": "Heartbeat Agent"})
        assert store.heartbeat(aid) is True
        assert store.heartbeat("nonexistent") is False


def test_discovered_agents() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        store = A2ARegistryStore(tmpdir)
        store.set_discovered_agents([
            {"id": "a2a:test", "name": "Test Profile"},
        ])
        s = store.stats()
        assert s["discoveredAgents"] == 1
        assert s["totalAgents"] == 1

        # Discovered agents cannot be unregistered via the API
        assert store.unregister("a2a:test") is False


def test_unregister() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        store = A2ARegistryStore(tmpdir)
        aid = store.register_agent({"name": "Remove Me"})
        assert store.unregister(aid) is True
        assert store.get_agent(aid) is None
        assert store.unregister("nonexistent") is False


def test_unregister_protected() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        store = A2ARegistryStore(tmpdir)
        store.set_discovered_agents([
            {"id": "a2a:protected", "name": "Protected"},
        ])
        assert store.unregister("a2a:protected") is False


def test_purge_stale() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        store = A2ARegistryStore(tmpdir)
        aid = store.register_agent({"name": "Stale Agent"})
        store._heartbeats[aid] = time.time() - 9999
        purged = store.purge_stale()
        assert purged >= 1
        assert store.get_agent(aid) is None


def test_stats_counts() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        store = A2ARegistryStore(tmpdir)
        store.register_agent({"name": "E1"})
        store.register_agent({"name": "E2"})
        store.set_discovered_agents([
            {"id": "a2a:h1", "name": "H1"},
            {"id": "a2a:h2", "name": "H2"},
            {"id": "a2a:h3", "name": "H3"},
        ])
        s = store.stats()
        assert s["totalAgents"] == 5
        assert s["discoveredAgents"] == 3
        assert s["externalAgents"] == 2


def test_registered_at_metadata() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        store = A2ARegistryStore(tmpdir)
        aid = store.register_agent({"name": "Meta Test"})
        card = store.get_agent(aid)
        assert card is not None
        meta = card.get("metadata", {})
        assert "registeredAt" in meta


def test_persistence() -> None:
    """Verify that data survives across store instances."""
    async def _run() -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store1 = A2ARegistryStore(tmpdir)
            aid = store1.register_agent({
                "name": "Persistent",
                "url": "https://persist.test",
                "tags": ["persist"],
            })
            await asyncio.sleep(0.05)

            store2 = A2ARegistryStore(tmpdir)
            assert store2.stats()["externalAgents"] == 1
            card = store2.get_agent(aid)
            assert card is not None
            assert card["name"] == "Persistent"
            assert card["tags"] == ["persist"]

    asyncio.run(_run())