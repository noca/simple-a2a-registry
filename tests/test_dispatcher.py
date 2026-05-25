"""Integration tests for the Dispatcher (dispatcher.py).

Tests cover TTL release, retry promotion, and claim+spawn flows
using a real TaskStore (SQLite in-memory/tempfile).
"""

from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path
from typing import AsyncGenerator, Generator

import pytest

from simple_a2a_registry.orchestration.models import (
    Task,
    TaskStatus,
    TaskRunStatus,
)
from simple_a2a_registry.orchestration.store import TaskStore, DEFAULT_CLAIM_TTL
from simple_a2a_registry.orchestration.workspace import WorkspaceManager
from simple_a2a_registry.orchestration.dispatcher import (
    Dispatcher,
    DispatcherConfig,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path() -> Generator[str, None, None]:
    """Create a tempfile path for the SQLite DB."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    try:
        yield path
    finally:
        if os.path.exists(path):
            os.unlink(path)


@pytest.fixture
def store(db_path: str) -> Generator[TaskStore, None, None]:
    """Create a fresh TaskStore backed by a tempfile."""
    ts = TaskStore(db_path)
    try:
        yield ts
    finally:
        ts.close()


@pytest.fixture
def ws_mgr() -> Generator[WorkspaceManager, None, None]:
    """Create a WorkspaceManager with a temp root."""
    with tempfile.TemporaryDirectory() as d:
        yield WorkspaceManager(str(Path(d) / "workspaces"))


@pytest.fixture
def dispatcher(
    store: TaskStore, ws_mgr: WorkspaceManager,
) -> Dispatcher:
    """Create a Dispatcher with a minimal config for testing."""
    config = DispatcherConfig(
        poll_interval=3600,  # long interval so manual trigger doesn't race
        claim_ttl=DEFAULT_CLAIM_TTL,
        failure_limit=3,
        dispatcher_id="test-dispatcher",
        worker_command="echo",  # need a non-None command so _claim_and_spawn() runs claim
    )
    return Dispatcher(store, ws_mgr, config)


# ===================================================================
# TTL Release via poll cycle
# ===================================================================


class TestTTLRelease:
    async def test_releases_expired_claims(
        self, store: TaskStore, dispatcher: Dispatcher,
    ) -> None:
        """A task with an expired claim lock should be released to failed."""
        task = store.create_task(
            title="expired-test", assignee="worker-1",
            max_retries=0,  # prevent retry promotion
        )

        # Manually claim it
        store.claim_task(task.id, "worker-1", 12345, ttl=1)
        # Wait briefly for TTL to expire
        time.sleep(1.5)

        stats = await dispatcher.trigger_poll_cycle()
        assert stats["ttl_released"] >= 1

        refreshed = store.get_task(task.id)
        assert refreshed is not None
        assert refreshed.status == TaskStatus.FAILED.value

    async def test_no_expired_skips(
        self, store: TaskStore, dispatcher: Dispatcher,
    ) -> None:
        """If no claims are expired, TTL release should do nothing."""
        task = store.create_task(title="fresh-task", assignee="worker-1")
        store.claim_task(task.id, "worker-1", 12345, ttl=3600)

        stats = await dispatcher.trigger_poll_cycle()
        assert stats["ttl_released"] == 0

        refreshed = store.get_task(task.id)
        assert refreshed is not None
        assert refreshed.status == TaskStatus.RUNNING.value


# ===================================================================
# Retry Promotion
# ===================================================================


class TestRetryPromotion:
    async def test_promotes_failed_below_limit(
        self, store: TaskStore, dispatcher: Dispatcher,
    ) -> None:
        """A failed task below max_retries should be promoted back to ready."""
        task = store.create_task(
            title="retry-me",
            # No assignee so claim+spawn skips it
            max_retries=3,
        )

        # Manually assign to bypass the assignee check
        claim_result = store.claim_task(task.id, "test", 1, ttl=3600)
        assert claim_result is not None
        store.update_task_status(task.id, TaskStatus.FAILED.value)
        # Ensure consecutive_failures is > 0 for retry logic
        with store._tx() as cur:
            cur.execute(
                "UPDATE tasks SET consecutive_failures=1 WHERE id=?",
                (task.id,),
            )

        stats = await dispatcher.trigger_poll_cycle()
        assert stats["retry_promoted"] >= 1

        refreshed = store.get_task(task.id)
        assert refreshed is not None
        assert refreshed.status == TaskStatus.READY.value

    async def test_does_not_promote_above_limit(
        self, store: TaskStore, dispatcher: Dispatcher,
    ) -> None:
        """A failed task above max_retries should stay failed."""
        task = store.create_task(
            title="give-up", assignee="worker-1",
            max_retries=2,
        )

        claim_result = store.claim_task(task.id, "test", 1, ttl=3600)
        assert claim_result is not None
        store.update_task_status(task.id, TaskStatus.FAILED.value)
        with store._tx() as cur:
            cur.execute(
                "UPDATE tasks SET consecutive_failures=3 WHERE id=?",
                (task.id,),
            )

        stats = await dispatcher.trigger_poll_cycle()
        assert stats["retry_promoted"] == 0

        refreshed = store.get_task(task.id)
        assert refreshed is not None
        assert refreshed.status == TaskStatus.FAILED.value


# ===================================================================
# Claim + Spawn
# ===================================================================


class TestClaimAndSpawn:
    async def test_claims_ready_task(
        self, store: TaskStore,
        ws_mgr: WorkspaceManager,
        dispatcher: Dispatcher,
    ) -> None:
        """A ready task with an assignee should be claimed."""
        task = store.create_task(title="claim-me", assignee="worker-1")

        assert task.status == TaskStatus.READY.value

        stats = await dispatcher.trigger_poll_cycle()
        assert stats["tasks_claimed"] >= 1

        refreshed = store.get_task(task.id)
        assert refreshed is not None
        assert refreshed.status == TaskStatus.RUNNING.value
        assert refreshed.workspace_path is not None
        assert os.path.isdir(refreshed.workspace_path)

    async def test_skips_task_without_assignee(
        self, store: TaskStore, dispatcher: Dispatcher,
    ) -> None:
        """A ready task without an assignee should be skipped."""
        task = store.create_task(title="no-assignee")  # no assignee

        stats = await dispatcher.trigger_poll_cycle()
        assert stats["tasks_claimed"] == 0

        refreshed = store.get_task(task.id)
        assert refreshed is not None
        assert refreshed.status == TaskStatus.READY.value  # unchanged

    async def test_claim_with_todo_task(
        self, store: TaskStore, dispatcher: Dispatcher,
    ) -> None:
        """A child task should become ready when its parent completes, then get claimed."""
        # Create parent
        parent = store.create_task(title="parent")  # no assignee — stays put, not claimed
        # Create child that depends on parent
        child = store.create_task(
            title="child", assignee="worker-1",
            parents=[parent.id],
        )
        # Child should be todo initially
        assert child.status == TaskStatus.TODO.value

        # Claim + complete the parent to trigger child promotion
        store.claim_task(parent.id, "dummy", 1, ttl=3600)
        store.update_task_status(parent.id, TaskStatus.COMPLETED.value)
        child_refreshed = store.get_task(child.id)
        assert child_refreshed is not None
        assert child_refreshed.status == TaskStatus.READY.value

        # Dispatcher should claim it
        stats = await dispatcher.trigger_poll_cycle()
        assert stats["tasks_claimed"] >= 1

    async def test_no_ready_tasks(
        self, store: TaskStore, dispatcher: Dispatcher,
    ) -> None:
        """With no ready tasks, claim should do nothing."""
        parent = store.create_task(title="parent")  # no assignee — stays put, not claimed
        task = store.create_task(
            title="deferred", assignee="worker-1",
            parents=[parent.id],
        )
        # In todo because parent hasn't completed yet
        assert task.status == TaskStatus.TODO.value

        stats = await dispatcher.trigger_poll_cycle()
        assert stats["tasks_claimed"] == 0
        # Should still be todo, not claimed
        refreshed = store.get_task(task.id)
        assert refreshed is not None
        assert refreshed.status == TaskStatus.TODO.value
# ===================================================================


class TestFullCycle:
    async def test_full_cycle_stats_shape(
        self, store: TaskStore, dispatcher: Dispatcher,
    ) -> None:
        """The poll cycle should return the expected stats dict."""
        stats = await dispatcher.trigger_poll_cycle()
        assert isinstance(stats, dict)
        assert "ttl_released" in stats
        assert "retry_promoted" in stats
        assert "tasks_claimed" in stats

    async def test_claim_allocates_scratch_workspace(
        self, store: TaskStore,
        ws_mgr: WorkspaceManager,
        dispatcher: Dispatcher,
    ) -> None:
        """Claimed task should have a scratch workspace created."""
        task = store.create_task(
            title="ws-check", assignee="worker-1",
            workspace_kind="scratch",
        )

        await dispatcher.trigger_poll_cycle()

        refreshed = store.get_task(task.id)
        assert refreshed is not None
        assert refreshed.status == TaskStatus.RUNNING.value
        assert refreshed.workspace_path is not None
        assert os.path.isdir(refreshed.workspace_path)

        # Cleanup the workspace
        ws_mgr.cleanup(refreshed)

    async def test_dispatcher_lifecycle(
        self, store: TaskStore, ws_mgr: WorkspaceManager,
    ) -> None:
        """Dispatcher run/stop lifecycle should not raise."""
        config = DispatcherConfig(poll_interval=3600)
        disp = Dispatcher(store, ws_mgr, config)
        assert not disp.is_running()

        # Start and immediately stop
        asyncio_task = None
        import asyncio
        try:
            asyncio_task = asyncio.create_task(disp.run())
            await asyncio.sleep(0.01)  # let it start
            assert disp.is_running()
        finally:
            disp.stop()
            if asyncio_task:
                try:
                    await asyncio.wait_for(asyncio_task, timeout=5)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    pass