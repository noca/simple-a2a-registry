"""Tests for AgentMemoryStore — CRUD, TTL, namespace isolation, concurrency."""

from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, Generator
from threading import Thread
from concurrent.futures import ThreadPoolExecutor, as_completed

import pytest

from simple_a2a_registry.database import SQLiteEngine
from simple_a2a_registry.orchestration.memory import (
    AgentMemoryStore,
    MemoryRecord,
    NAMESPACE_PERSONAL,
    NAMESPACE_SHARED,
    NAMESPACE_GLOBAL,
    _maybe_create_memory_schema,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def engine() -> Generator[SQLiteEngine, None, None]:
    """Create a fresh SQLiteEngine backed by a tempfile for each test."""
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    eng = SQLiteEngine(db_path)
    eng.connect()
    _maybe_create_memory_schema(eng)
    try:
        yield eng
    finally:
        eng.close()
        if os.path.exists(db_path):
            os.unlink(db_path)


@pytest.fixture
def store(engine: SQLiteEngine) -> Generator[AgentMemoryStore, None, None]:
    """Create an AgentMemoryStore on the temp engine."""
    ms = AgentMemoryStore(engine)
    yield ms


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


def test_set_and_get(store: AgentMemoryStore) -> None:
    """Set a value and retrieve it."""
    record = store.set("greeting", {"hello": "world"}, "agent-1")
    assert record is not None
    assert record.key == "greeting"
    assert record.agent_id == "agent-1"
    assert record.namespace == NAMESPACE_PERSONAL
    assert record.ttl == 0

    fetched = store.get("greeting", "agent-1")
    assert fetched is not None
    assert fetched.key == "greeting"
    assert json.loads(fetched.value) == {"hello": "world"}


def test_get_nonexistent(store: AgentMemoryStore) -> None:
    """Getting a nonexistent key returns None."""
    assert store.get("nope", "agent-1") is None


def test_update_existing(store: AgentMemoryStore) -> None:
    """Updating an existing key changes its value."""
    store.set("key1", "v1", "agent-1")
    store.set("key1", "v2", "agent-1")
    fetched = store.get("key1", "agent-1")
    assert fetched is not None
    assert json.loads(fetched.value) == "v2"


def test_delete(store: AgentMemoryStore) -> None:
    """Delete a key and verify it's gone."""
    store.set("tmp", "delete-me", "agent-1")
    assert store.get("tmp", "agent-1") is not None

    deleted = store.delete("tmp", "agent-1")
    assert deleted is True

    assert store.get("tmp", "agent-1") is None


def test_delete_nonexistent(store: AgentMemoryStore) -> None:
    """Deleting a nonexistent key returns False."""
    assert store.delete("nope", "agent-1") is False


# ---------------------------------------------------------------------------
# Namespace isolation
# ---------------------------------------------------------------------------


def test_namespace_isolation(store: AgentMemoryStore) -> None:
    """Same key in different namespaces are separate entries."""
    store.set("config", {"personal": True}, "agent-1", namespace=NAMESPACE_PERSONAL)
    store.set("config", {"shared": True}, "agent-1", namespace=NAMESPACE_SHARED)
    store.set("config", {"global": True}, "agent-1", namespace=NAMESPACE_GLOBAL)

    personal = store.get("config", "agent-1", namespace=NAMESPACE_PERSONAL)
    shared = store.get("config", "agent-1", namespace=NAMESPACE_SHARED)
    global_entry = store.get("config", "agent-1", namespace=NAMESPACE_GLOBAL)

    assert personal is not None
    assert shared is not None
    assert global_entry is not None

    assert json.loads(personal.value) == {"personal": True}
    assert json.loads(shared.value) == {"shared": True}
    assert json.loads(global_entry.value) == {"global": True}


def test_agent_isolation(store: AgentMemoryStore) -> None:
    """Different agents cannot see each other's personal entries."""
    store.set("secret", "agent-1-data", "agent-1")
    store.set("secret", "agent-2-data", "agent-2")

    a1 = store.get("secret", "agent-1")
    a2 = store.get("secret", "agent-2")

    assert a1 is not None
    assert a2 is not None
    assert json.loads(a1.value) == "agent-1-data"
    assert json.loads(a2.value) == "agent-2-data"


# ---------------------------------------------------------------------------
# TTL expiry
# ---------------------------------------------------------------------------


def test_ttl_expiry(store: AgentMemoryStore) -> None:
    """Entry with TTL should expire and not be returned."""
    store.set("temp", "short-lived", "agent-1", ttl=1)
    # Verify it exists
    assert store.get("temp", "agent-1") is not None

    # Wait for expiry
    time.sleep(1.1)
    assert store.get("temp", "agent-1") is None


def test_ttl_no_expiry(store: AgentMemoryStore) -> None:
    """Entry with ttl=0 never expires."""
    store.set("permanent", "forever", "agent-1", ttl=0)
    assert store.get("permanent", "agent-1") is not None


def test_purge_expired(store: AgentMemoryStore) -> None:
    """purge_expired() removes only expired entries."""
    store.set("temp1", "a", "agent-1", ttl=1)
    store.set("temp2", "b", "agent-1", ttl=1)
    store.set("perm", "c", "agent-1", ttl=0)

    time.sleep(1.1)

    purged = store.purge_expired()
    assert purged >= 2

    assert store.get("temp1", "agent-1") is None
    assert store.get("temp2", "agent-1") is None
    assert store.get("perm", "agent-1") is not None


# ---------------------------------------------------------------------------
# List keys
# ---------------------------------------------------------------------------


def test_list_keys(store: AgentMemoryStore) -> None:
    """list_keys returns all entries for an agent."""
    store.set("a", 1, "agent-1")
    store.set("b", 2, "agent-1")
    store.set("c", 3, "agent-1")
    store.set("x", 99, "agent-2")  # different agent

    entries = store.list_keys("agent-1")
    keys = sorted(e.key for e in entries)
    assert keys == ["a", "b", "c"]
    assert len(entries) == 3


def test_list_keys_with_prefix(store: AgentMemoryStore) -> None:
    """list_keys with prefix filter."""
    store.set("config:db", "mysql", "agent-1")
    store.set("config:port", 3306, "agent-1")
    store.set("other", "value", "agent-1")

    entries = store.list_keys("agent-1", prefix="config:")
    assert len(entries) == 2
    assert all(e.key.startswith("config:") for e in entries)


def test_list_keys_with_namespace(store: AgentMemoryStore) -> None:
    """list_keys with namespace filter."""
    store.set("k1", "personal", "agent-1", namespace=NAMESPACE_PERSONAL)
    store.set("k2", "shared", "agent-1", namespace=NAMESPACE_SHARED)

    personal = store.list_keys("agent-1", namespace=NAMESPACE_PERSONAL)
    assert len(personal) == 1
    assert personal[0].key == "k1"

    shared = store.list_keys("agent-1", namespace=NAMESPACE_SHARED)
    assert len(shared) == 1
    assert shared[0].key == "k2"


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


def test_search(store: AgentMemoryStore) -> None:
    """search finds entries by value content (case-insensitive)."""
    store.set("doc1", {"text": "Hello World"}, "agent-1")
    store.set("doc2", {"text": "Goodbye Moon"}, "agent-1")
    store.set("doc3", {"text": "Hello again"}, "agent-2")

    # Should find "hello" entries for agent-1
    results = store.search("hello", "agent-1")
    assert len(results) == 1
    assert results[0].key == "doc1"

    # Should find nothing on non-matching
    results = store.search("xyzzy", "agent-1")
    assert len(results) == 0


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------


def test_stats(store: AgentMemoryStore) -> None:
    """stats() returns meaningful counts."""
    store.set("k1", 1, "agent-1")
    store.set("k2", 2, "agent-1", namespace=NAMESPACE_SHARED)
    store.set("k3", 3, "agent-2")

    s = store.stats()
    assert s["total_entries"] == 3
    assert s["by_namespace"].get(NAMESPACE_PERSONAL, 0) == 2
    assert s["by_namespace"].get(NAMESPACE_SHARED, 0) == 1


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


def test_concurrent_set(store: AgentMemoryStore) -> None:
    """Concurrent writes to different keys should not corrupt state."""
    n_threads = 10
    with ThreadPoolExecutor(max_workers=n_threads) as pool:
        futures = [
            pool.submit(store.set, f"key-{i}", f"value-{i}", "agent-concurrent")
            for i in range(n_threads)
        ]
        for f in as_completed(futures):
            result = f.result()
            assert result is not None

    entries = store.list_keys("agent-concurrent")
    assert len(entries) == n_threads


def test_concurrent_read_write(store: AgentMemoryStore) -> None:
    """Concurrent read+write on same key is safe."""
    store.set("shared-key", "original", "agent-concurrent")

    def writer() -> None:
        for i in range(20):
            store.set("shared-key", f"write-{i}", "agent-concurrent")

    def reader() -> None:
        for _ in range(20):
            entry = store.get("shared-key", "agent-concurrent")
            assert entry is not None

    threads = [Thread(target=writer), Thread(target=reader)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    final = store.get("shared-key", "agent-concurrent")
    assert final is not None


# ---------------------------------------------------------------------------
# MemoryRecord helpers
# ---------------------------------------------------------------------------


def test_memory_record_to_dict() -> None:
    """to_dict() returns a clean dict without internal id."""
    record = MemoryRecord(
        id=42,
        agent_id="agent-1",
        key="test",
        value='{"a": 1}',
        namespace=NAMESPACE_PERSONAL,
        ttl=0,
        expires_at=None,
        created_at=1000.0,
        updated_at=1000.0,
    )
    d = record.to_dict()
    assert "id" not in d
    assert d["key"] == "test"
    assert d["value"] == {"a": 1}
    assert d["agent_id"] == "agent-1"
