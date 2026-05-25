"""Benchmark: high-concurrency task creation throughput (1000+ tasks)."""
from __future__ import annotations

import time

import pytest

from simple_a2a_registry.orchestration.store import TaskStore


pytestmark = pytest.mark.asyncio


def _bulk_create(store: TaskStore, count: int) -> float:
    """Create *count* tasks, returning elapsed seconds."""
    start = time.perf_counter()
    for i in range(count):
        store.create_task(
            title=f"bench-task-{i:06d}",
            assignee="bench-worker" if i % 3 == 0 else None,
            priority=i % 5,
        )
    return time.perf_counter() - start


class TestThroughputSnapshot:
    """Single-shot throughput numbers suitable for trend tracking."""

    N_TASKS = 1000

    async def test_throughput_create(self, store: TaskStore) -> None:
        """Throughput: {self.N_TASKS} task creates."""
        elapsed = _bulk_create(store, self.N_TASKS)
        tps = self.N_TASKS / elapsed
        print(f"THROUGHPUT_CREATE_{self.N_TASKS}={tps:.0f} tasks/sec")
        assert tps > 100, "Expect at least 100 tasks/sec"

    async def test_throughput_list(self, store: TaskStore) -> None:
        """Throughput: list {self.N_TASKS} tasks."""
        _bulk_create(store, self.N_TASKS)
        start = time.perf_counter()
        store.list_tasks(limit=self.N_TASKS)
        elapsed = time.perf_counter() - start
        print(f"THROUGHPUT_LIST_{self.N_TASKS}={self.N_TASKS/elapsed:.0f} tasks/sec")

    async def test_throughput_read(self, store: TaskStore) -> None:
        """Throughput: read {self.N_TASKS} tasks by ID."""
        _bulk_create(store, self.N_TASKS)
        tasks, _ = store.list_tasks(limit=self.N_TASKS)
        start = time.perf_counter()
        for t in tasks:
            store.get_task(t.id)
        elapsed = time.perf_counter() - start
        avg_us = elapsed / self.N_TASKS * 1_000_000
        print(f"READ_LATENCY_{self.N_TASKS}={avg_us:.0f} µs/task")

