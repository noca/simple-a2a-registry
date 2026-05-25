"""Benchmark: dependency chain resolution performance (depth > 10)."""
from __future__ import annotations

import time
from typing import List

import pytest

from simple_a2a_registry.orchestration.store import TaskStore


pytestmark = pytest.mark.asyncio


def _build_chain(store: TaskStore, depth: int) -> List[str]:
    """Create a linear parent->child chain of *depth* tasks.
    Returns [root, child1, ..., leaf] task ids.
    """
    ids: List[str] = []
    for i in range(depth):
        if i == 0:
            t = store.create_task(title=f"chain-root-{i:03d}", assignee="chain-worker")
            ids.append(t.id)
        else:
            t = store.create_task(title=f"chain-link-{i:03d}", parents=[ids[-1]])
            ids.append(t.id)
    return ids


def _resolve_chain(store: TaskStore, ids: List[str]) -> float:
    """Claim then complete each task in the chain."""
    start = time.perf_counter()
    for tid in ids:
        store.claim_task(tid, "bench", 1, ttl=3600)
        store.update_task_status(tid, "completed")
    return time.perf_counter() - start


class TestDepChainSnapshot:
    """Single-shot dependency chain metrics for trend tracking."""

    async def test_depchain_depth_10(self, store: TaskStore) -> None:
        """Dependency chain: 10 links."""
        ids = _build_chain(store, 10)
        elapsed = _resolve_chain(store, ids)
        avg_us = elapsed / len(ids) * 1_000_000
        print(f"DEPCHAIN_DEPTH_10={elapsed*1000:.2f}ms total, {avg_us:.0f} µs/link")
        assert avg_us < 50_000, "Expected <50ms per link"

    async def test_depchain_depth_50(self, store: TaskStore) -> None:
        """Dependency chain: 50 links."""
        ids = _build_chain(store, 50)
        elapsed = _resolve_chain(store, ids)
        avg_us = elapsed / len(ids) * 1_000_000
        print(f"DEPCHAIN_DEPTH_50={elapsed*1000:.2f}ms total, {avg_us:.0f} µs/link")

    async def test_fan_out_fan_in(self, store: TaskStore) -> None:
        """Fan-out/in: root -> [10 children] -> grandchild."""
        root = store.create_task(title="fan-root", assignee="fan-worker")
        children: List[str] = []
        for i in range(10):
            c = store.create_task(title=f"fan-child-{i:03d}", parents=[root.id])
            children.append(c.id)
        grandchild = store.create_task(title="fan-grandchild", parents=children)

        start = time.perf_counter()
        store.claim_task(root.id, "bench", 1, ttl=3600)
        store.update_task_status(root.id, "completed")
        for cid in children:
            store.claim_task(cid, "bench", 1, ttl=3600)
            store.update_task_status(cid, "completed")
        elapsed = time.perf_counter() - start

        gc = store.get_task(grandchild.id)
        assert gc is not None and gc.status == "ready"
        print(f"FAN_OUT_FAN_IN={elapsed*1000:.2f}ms total")

