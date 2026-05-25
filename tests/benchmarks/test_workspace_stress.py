"""Benchmark: Workspace Manager filesystem stress test."""
from __future__ import annotations

import os
import time
from typing import List

import pytest

from simple_a2a_registry.orchestration.models import Task
from simple_a2a_registry.orchestration.workspace import WorkspaceManager


pytestmark = pytest.mark.asyncio


def _make_task(task_id: str = "") -> Task:
    """Create a minimal Task for workspace allocation."""
    import uuid
    tid = task_id or f"t_bench_{uuid.uuid4().hex[:8]}"
    return Task(
        id=tid,
        title="bench-task",
        assignee="bench-worker",
        status="ready",
        workspace_kind="scratch",
    )


class TestWorkspaceSnapshot:
    """Single-shot workspace metrics for trend tracking."""

    async def test_ws_allocate_100(self, ws_mgr: WorkspaceManager) -> None:
        """Workspace: allocate 100 scratch dirs."""
        tasks: List[Task] = [_make_task() for _ in range(100)]
        start = time.perf_counter()
        for t in tasks:
            path = ws_mgr.allocate_for_claim(t)
            assert path is not None
            assert os.path.isdir(path)
        elapsed = time.perf_counter() - start
        avg_ms = elapsed / 100 * 1000
        print(f"WS_ALLOCATE_100={elapsed*1000:.2f}ms total, {avg_ms:.3f}ms/ws")
        for t in tasks:
            ws_mgr.cleanup(t)
        assert avg_ms < 100, "Expected <100ms per workspace allocation"

    async def test_ws_cleanup_100(self, ws_mgr: WorkspaceManager) -> None:
        """Workspace: cleanup 100 scratch dirs."""
        tasks: List[Task] = [_make_task() for _ in range(100)]
        for t in tasks:
            ws_mgr.allocate_for_claim(t)
        start = time.perf_counter()
        for t in tasks:
            ws_mgr.cleanup(t)
        elapsed = time.perf_counter() - start
        avg_ms = elapsed / 100 * 1000
        print(f"WS_CLEANUP_100={elapsed*1000:.2f}ms total, {avg_ms:.3f}ms/ws")
        # Verify removed
        any_left = any(
            t.workspace_path is not None and os.path.isdir(t.workspace_path)
            for t in tasks
        )
        assert not any_left, "All workspaces should be removed"

