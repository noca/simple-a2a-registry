"""Unit tests for AnomalyScanner (P2.4).

Tests cover:
  - _detect_orphans: running tasks with disconnected agent → fail
  - _detect_timeouts: running tasks past max_runtime_seconds → fail
  - _scan_cycle: full scan integrates both detections
  - _fail_kanban_task: updates TaskStore status
  - Cooldown: recently-heartbeated tasks are not failed
  - No false positives: connected agents, tasks within limit
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, PropertyMock

import pytest

from simple_a2a_registry.orchestration.anomaly_scanner import AnomalyScanner
from simple_a2a_registry.orchestration.models import Task, TaskStatus

pytestmark = pytest.mark.asyncio


def _make_task(
    task_id: str = "t_001",
    assignee: str | None = "agent-a",
    status: str = TaskStatus.RUNNING.value,
    max_runtime_seconds: int | None = None,
    last_heartbeat_at: int | None = None,
    started_at: int | None = None,
    created_at: int | None = None,
) -> Task:
    now = int(time.time())
    return Task(
        id=task_id,
        title=f"Task {task_id}",
        assignee=assignee,
        status=status,
        max_runtime_seconds=max_runtime_seconds,
        last_heartbeat_at=last_heartbeat_at,
        started_at=started_at,
        created_at=created_at or now,
    )


@pytest.fixture
def scanner():
    """Create an AnomalyScanner with mock dependencies."""
    task_store = MagicMock()
    ws_connections: dict = {}
    scanner = AnomalyScanner(
        task_store=task_store,
        ws_connections=ws_connections,
        admin_ws_hub=MagicMock(),
        interval=60,
    )
    return scanner, task_store, ws_connections


# ---------------------------------------------------------------------------
# Orphan detection
# ---------------------------------------------------------------------------


class TestDetectOrphans:
    async def test_no_orphans_when_agent_connected(self, scanner):
        """Running task with a connected agent is not an orphan."""
        sc, task_store, ws_connections = scanner
        task = _make_task("t_001", assignee="agent-a")
        task_store.list_tasks.return_value = ([task], 1)

        # Simulate connected agent
        mock_ws = AsyncMock()
        mock_ws.closed = False
        ws_connections["agent-a"] = mock_ws

        orphans = await sc._detect_orphans()
        assert len(orphans) == 0

    async def test_no_orphans_when_no_running_tasks(self, scanner):
        """No orphans when there are no running tasks."""
        sc, task_store, _ = scanner
        task_store.list_tasks.return_value = ([], 0)

        orphans = await sc._detect_orphans()
        assert len(orphans) == 0

    async def test_orphan_detected_when_agent_disconnected(self, scanner):
        """Running task with disconnected agent is detected as orphan."""
        sc, task_store, ws_connections = scanner
        task = _make_task("t_001", assignee="agent-a")
        task_store.list_tasks.return_value = ([task], 1)

        # Agent not connected
        orphans = await sc._detect_orphans()
        assert len(orphans) == 1
        task_id, assignee, reason = orphans[0]
        assert task_id == "t_001"
        assert assignee == "agent-a"
        assert "disconnected" in reason.lower()

    async def test_orphan_when_ws_closed(self, scanner):
        """Orphan detected when WS connection exists but is closed."""
        sc, task_store, ws_connections = scanner
        task = _make_task("t_001", assignee="agent-a")
        task_store.list_tasks.return_value = ([task], 1)

        # Agent connection exists but is closed
        mock_ws = AsyncMock()
        # Use PropertyMock for 'closed' attribute
        type(mock_ws).closed = PropertyMock(return_value=True)
        ws_connections["agent-a"] = mock_ws

        orphans = await sc._detect_orphans()
        assert len(orphans) == 1

    async def test_orphan_cooldown_skips_recently_heartbeated(self, scanner):
        """Recently heartbeated tasks (within 120s) are skipped."""
        sc, task_store, ws_connections = scanner
        now = int(time.time())
        task = _make_task(
            "t_001", assignee="agent-a",
            last_heartbeat_at=now - 60,  # 60s ago — within 120s cooldown
        )
        task_store.list_tasks.return_value = ([task], 1)

        orphans = await sc._detect_orphans()
        assert len(orphans) == 0

    async def test_orphan_past_cooldown_is_detected(self, scanner):
        """Tasks heartbeated >120s ago are detected as orphans."""
        sc, task_store, ws_connections = scanner
        now = int(time.time())
        task = _make_task(
            "t_001", assignee="agent-a",
            last_heartbeat_at=now - 300,  # 5 min ago — past cooldown
        )
        task_store.list_tasks.return_value = ([task], 1)

        orphans = await sc._detect_orphans()
        assert len(orphans) == 1

    async def test_orphan_without_assignee_skipped(self, scanner):
        """Running task without an assignee is skipped."""
        sc, task_store, ws_connections = scanner
        task = _make_task("t_001", assignee=None)
        task_store.list_tasks.return_value = ([task], 1)

        orphans = await sc._detect_orphans()
        assert len(orphans) == 0

    async def test_orphan_multiple_tasks_same_agent(self, scanner):
        """Multiple tasks for a disconnected agent are all detected."""
        sc, task_store, ws_connections = scanner
        now = int(time.time())
        tasks = [
            _make_task("t_001", "agent-a",
                        last_heartbeat_at=now - 300),
            _make_task("t_002", "agent-a",
                        last_heartbeat_at=now - 300),
            _make_task("t_003", "agent-b",
                        last_heartbeat_at=now - 300),
        ]
        task_store.list_tasks.return_value = (tasks, 3)

        # agent-a is disconnected, agent-b is connected
        mock_ws = AsyncMock()
        mock_ws.closed = False
        ws_connections["agent-b"] = mock_ws

        orphans = await sc._detect_orphans()
        assert len(orphans) == 2  # t_001 and t_002
        orphan_ids = {o[0] for o in orphans}
        assert orphan_ids == {"t_001", "t_002"}


# ---------------------------------------------------------------------------
# Timeout detection
# ---------------------------------------------------------------------------


class TestDetectTimeouts:
    def test_no_timeout_within_limit(self, scanner):
        """Running task within max_runtime_seconds is not timed out."""
        sc, task_store, _ = scanner
        now = int(time.time())
        task = _make_task(
            "t_001", assignee="agent-a",
            max_runtime_seconds=300,
            started_at=now - 100,  # 100s elapsed, well within 300s limit
        )
        task_store.list_tasks.return_value = ([task], 1)

        timeouts = sc._detect_timeouts()
        assert len(timeouts) == 0

    def test_timeout_when_exceeded(self, scanner):
        """Running task past max_runtime_seconds is detected as timeout."""
        sc, task_store, _ = scanner
        now = int(time.time())
        task = _make_task(
            "t_001", assignee="agent-a",
            max_runtime_seconds=60,
            started_at=now - 300,  # 300s elapsed, well past 60s limit
        )
        task_store.list_tasks.return_value = ([task], 1)

        timeouts = sc._detect_timeouts()
        assert len(timeouts) == 1
        task_id, assignee, reason = timeouts[0]
        assert task_id == "t_001"
        assert "exceeded max_runtime" in reason

    def test_no_timeout_when_no_max_runtime(self, scanner):
        """Task without max_runtime_seconds is not timed out."""
        sc, task_store, _ = scanner
        now = int(time.time())
        task = _make_task(
            "t_001", assignee="agent-a",
            max_runtime_seconds=None,
            started_at=now - 10000,
        )
        task_store.list_tasks.return_value = ([task], 1)

        timeouts = sc._detect_timeouts()
        assert len(timeouts) == 0

    def test_timeout_uses_heartbeat_as_ref(self, scanner):
        """Timeout uses last_heartbeat_at as reference, not started_at."""
        sc, task_store, _ = scanner
        now = int(time.time())
        task = _make_task(
            "t_001", assignee="agent-a",
            max_runtime_seconds=60,
            started_at=now - 10000,  # very old start
            last_heartbeat_at=now - 30,  # recent heartbeat (30s ago)
        )
        task_store.list_tasks.return_value = ([task], 1)

        # 30s < 60s, so no timeout
        timeouts = sc._detect_timeouts()
        assert len(timeouts) == 0

    def test_timeout_with_no_started_at(self, scanner):
        """Timeout falls back to created_at when started_at is None."""
        sc, task_store, _ = scanner
        now = int(time.time())
        task = _make_task(
            "t_001", assignee="agent-a",
            max_runtime_seconds=60,
            started_at=None,
            created_at=now - 300,  # 300s elapsed
        )
        task_store.list_tasks.return_value = ([task], 1)

        timeouts = sc._detect_timeouts()
        assert len(timeouts) == 1

    def test_multiple_timeouts(self, scanner):
        """Multiple tasks past their timeout are all detected."""
        sc, task_store, _ = scanner
        now = int(time.time())
        tasks = [
            _make_task("t_001", "agent-a",
                        max_runtime_seconds=60, started_at=now - 300),
            _make_task("t_002", "agent-b",
                        max_runtime_seconds=120, started_at=now - 600),
            _make_task("t_003", "agent-c",
                        max_runtime_seconds=99999, started_at=now - 100),
        ]
        task_store.list_tasks.return_value = (tasks, 3)

        timeouts = sc._detect_timeouts()
        assert len(timeouts) == 2
        timeout_ids = {t[0] for t in timeouts}
        assert timeout_ids == {"t_001", "t_002"}


# ---------------------------------------------------------------------------
# Full scan cycle
# ---------------------------------------------------------------------------


class TestScanCycle:
    async def test_scan_orphans_and_timeouts(self, scanner):
        """Full scan detects both orphans and timeouts."""
        sc, task_store, ws_connections = scanner
        now = int(time.time())

        tasks = [
            _make_task("t_orphan", "agent-gone",
                        max_runtime_seconds=300,
                        last_heartbeat_at=now - 300),
            _make_task("t_timeout", "agent-alive",
                        max_runtime_seconds=60,
                        started_at=now - 300),
            _make_task("t_normal", "agent-alive",
                        max_runtime_seconds=300,
                        started_at=now - 30),
        ]
        task_store.list_tasks.return_value = (tasks, 3)

        # Only agent-alive is connected
        mock_ws = AsyncMock()
        mock_ws.closed = False
        ws_connections["agent-alive"] = mock_ws

        stats = await sc._scan_cycle()

        assert stats["orphans_failed"] == 1
        assert stats["timeouts_failed"] >= 1
        # update_task_status should have been called at least twice
        assert task_store.update_task_status.call_count >= 2

    async def test_scan_no_issues(self, scanner):
        """Scan with no issues produces zero failures."""
        sc, task_store, ws_connections = scanner
        now = int(time.time())
        task = _make_task(
            "t_normal", "agent-alive",
            max_runtime_seconds=300,
            started_at=now - 30,
        )
        task_store.list_tasks.return_value = ([task], 1)

        mock_ws = AsyncMock()
        mock_ws.closed = False
        ws_connections["agent-alive"] = mock_ws

        stats = await sc._scan_cycle()
        assert stats["orphans_failed"] == 0
        assert stats["timeouts_failed"] == 0
        task_store.update_task_status.assert_not_called()

    async def test_scan_empty(self, scanner):
        """Scan with no running tasks produces zero failures."""
        sc, task_store, _ = scanner
        task_store.list_tasks.return_value = ([], 0)

        stats = await sc._scan_cycle()
        assert stats["orphans_failed"] == 0
        assert stats["timeouts_failed"] == 0


# ---------------------------------------------------------------------------
# _fail_kanban_task
# ---------------------------------------------------------------------------


class TestFailKanbanTask:
    def test_fail_kanban_task_successful(self, scanner):
        """_fail_kanban_task calls TaskStore.update_task_status with failed."""
        sc, task_store, _ = scanner

        sc._fail_kanban_task("t_001", "Agent disconnected")

        task_store.update_task_status.assert_called_once_with(
            "t_001", TaskStatus.FAILED.value,
            result="Agent disconnected",
        )

    def test_fail_kanban_task_error_handled(self, scanner):
        """_fail_kanban_task handles TaskStore exceptions gracefully."""
        sc, task_store, _ = scanner
        task_store.update_task_status.side_effect = ValueError("Task not found")

        # Should not raise
        sc._fail_kanban_task("t_001", "some reason")

    async def test_orphan_also_triggers_timeout_if_applicable(self, scanner):
        """Orphan detection may also produce timeout for same task."""
        sc, task_store, ws_connections = scanner
        now = int(time.time())
        task = _make_task(
            "t_001", "agent-gone",
            max_runtime_seconds=60,
            started_at=now - 300,
            last_heartbeat_at=now - 300,
        )
        task_store.list_tasks.return_value = ([task], 1)

        stats = await sc._scan_cycle()
        # Should be detected as both orphan AND timeout
        assert stats["orphans_failed"] >= 1
        assert stats["timeouts_failed"] >= 1


# ---------------------------------------------------------------------------
# Broadcast
# ---------------------------------------------------------------------------


class TestBroadcast:
    async def test_broadcast_on_orphan(self, scanner):
        """Anomaly events are broadcast to AdminWSHub on orphan detection."""
        sc, task_store, ws_connections = scanner
        now = int(time.time())
        task = _make_task(
            "t_001", "agent-gone",
            last_heartbeat_at=now - 300,
        )
        task_store.list_tasks.return_value = ([task], 1)

        await sc._scan_cycle()

        admin_hub = sc._admin_ws_hub
        assert admin_hub.broadcast_to_all.called
        call_args = admin_hub.broadcast_to_all.call_args[0][0]
        assert call_args["type"] == "anomaly"
        assert call_args["anomaly_type"] in ("orphan", "timeout")

    async def test_no_broadcast_without_anomalies(self, scanner):
        """No broadcast when there are no anomalies."""
        sc, task_store, ws_connections = scanner
        task_store.list_tasks.return_value = ([], 0)

        await sc._scan_cycle()

        sc._admin_ws_hub.broadcast_to_all.assert_not_called()

    async def test_no_broadcast_without_hub(self, scanner):
        """No error when admin_ws_hub is None."""
        sc, task_store, ws_connections = scanner
        now = int(time.time())
        task = _make_task(
            "t_001", "agent-gone",
            last_heartbeat_at=now - 300,
        )
        task_store.list_tasks.return_value = ([task], 1)

        # Set admin_ws_hub to None
        sc._admin_ws_hub = None

        # Should not raise
        stats = await sc._scan_cycle()
        assert stats["orphans_failed"] == 1


# ---------------------------------------------------------------------------
# Disconnection compensation (integration-style)
# ---------------------------------------------------------------------------


class TestDisconnectionCompensation:
    async def test_scanner_recovers_from_store_exception(self, scanner):
        """Scanner handles TaskStore exceptions gracefully per step."""
        sc, task_store, _ = scanner
        # First call (orphan detection) raises
        task_store.list_tasks.side_effect = [
            RuntimeError("DB error"),   # _detect_orphans fails
            ([], 0),                     # _detect_timeouts succeeds (no tasks)
        ]

        stats = await sc._scan_cycle()
        assert stats["orphans_failed"] == 0  # skipped due to error
        assert stats["timeouts_failed"] == 0

    async def test_scanner_multiple_cycles(self, scanner):
        """Scanner can run multiple cycles without issues."""
        sc, task_store, _ = scanner
        task_store.list_tasks.return_value = ([], 0)

        stats1 = await sc._scan_cycle()
        stats2 = await sc._scan_cycle()
        assert stats1["orphans_failed"] == 0
        assert stats2["orphans_failed"] == 0