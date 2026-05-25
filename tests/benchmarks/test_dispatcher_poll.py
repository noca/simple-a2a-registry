"""Benchmark: Dispatcher poll cycle overhead (CPU / time)."""
from __future__ import annotations

import os
import time

import pytest

from simple_a2a_registry.orchestration.store import TaskStore
from simple_a2a_registry.orchestration.dispatcher import Dispatcher


pytestmark = pytest.mark.asyncio


def _mk_expired(store: TaskStore, count: int) -> None:
    """Create *count* tasks with expired claim lock."""
    for i in range(count):
        t = store.create_task(title=f"expired-{i:06d}", assignee="zombie-worker", max_retries=0)
        store.claim_task(t.id, "zombie", os.getpid(), ttl=1)
    time.sleep(1.5)


def _mk_failed(store: TaskStore, count: int) -> None:
    """Create *count* failed-but-retryable tasks."""
    for i in range(count):
        t = store.create_task(title=f"retry-{i:06d}", assignee="retry-worker", max_retries=999)
        store.claim_task(t.id, "dispatcher", os.getpid(), ttl=3600)
        store.update_task_status(t.id, "failed")
        with store._tx() as cur:
            cur.execute("UPDATE tasks SET consecutive_failures=1 WHERE id=?", (t.id,))


def _mk_ready(store: TaskStore, count: int) -> None:
    """Create *count* ready tasks with assignee."""
    for i in range(count):
        store.create_task(title=f"ready-{i:06d}", assignee="ready-worker")


class TestPollSnapshot:
    """Single-shot poll metrics for trend tracking."""

    async def test_poll_empty_baseline(self, store: TaskStore, noop_dispatcher: Dispatcher) -> None:
        """Poll overhead: empty store."""
        start = time.perf_counter()
        await noop_dispatcher.trigger_poll_cycle()
        elapsed = time.perf_counter() - start
        print(f"POLL_EMPTY={elapsed*1000:.3f}ms")
        assert elapsed < 1.0, "Empty poll should complete in <1s"

    async def test_poll_100_expired(self, store: TaskStore, noop_dispatcher: Dispatcher) -> None:
        """Poll overhead: 100 expired claims."""
        _mk_expired(store, 100)
        start = time.perf_counter()
        stats = await noop_dispatcher.trigger_poll_cycle()
        elapsed = time.perf_counter() - start
        print(f"POLL_100_EXPIRED={elapsed*1000:.3f}ms (released={stats['ttl_released']})")

    async def test_poll_100_retryable(self, store: TaskStore, noop_dispatcher: Dispatcher) -> None:
        """Poll overhead: 100 retryable tasks."""
        _mk_failed(store, 100)
        start = time.perf_counter()
        stats = await noop_dispatcher.trigger_poll_cycle()
        elapsed = time.perf_counter() - start
        print(f"POLL_100_RETRYABLE={elapsed*1000:.3f}ms (promoted={stats['retry_promoted']})")

    async def test_claim_50(self, store: TaskStore, claiming_dispatcher: Dispatcher) -> None:
        """Claim+spawn 50 ready tasks."""
        _mk_ready(store, 50)
        start = time.perf_counter()
        stats = await claiming_dispatcher.trigger_poll_cycle()
        elapsed = time.perf_counter() - start
        per_ms = elapsed / max(stats['tasks_claimed'], 1) * 1000
        print(f"CLAIM_50={elapsed*1000:.2f}ms total, {per_ms:.2f}ms/task (claimed={stats['tasks_claimed']})")

