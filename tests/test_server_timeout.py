"""Unit tests for RegistryHandler task timeout management (P1.2).

Tests cover:
  - _schedule_task_timeout: creates asyncio timer
  - _cancel_task_timeout: cancels an existing timer
  - _reset_task_timeout: cancel + restart
  - Timeout trigger: task failed("timeout") + task_cancel WS message
  - Timer cleanup on terminal states
  - Timer cancellation in _fail_agent_tasks
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from simple_a2a_registry.server import RegistryHandler

pytestmark = pytest.mark.asyncio


@pytest.fixture
def handler():
    """Create a RegistryHandler with minimal dependencies."""
    store = MagicMock()
    h = RegistryHandler(store=store, base_url="http://test:8321")
    h._task_timeout = 0.01  # 10ms for fast test
    h._tasks = {}
    h._task_timers = {}
    h._ws_connections = {}
    h.task_store = None
    h._dispatched_ws_tasks = None
    return h


# ---------------------------------------------------------------------------
# Timer scheduling / cancellation
# ---------------------------------------------------------------------------


class TestScheduleTaskTimeout:
    async def test_schedule_creates_timer_in_dict(self, handler):
        """_schedule_task_timeout creates an asyncio.Task and stores it."""
        handler._tasks["t_001"] = {"id": "t_001", "agent_id": "agent-a",
                                    "state": "dispatched", "dispatched_at": time.time()}
        assert "t_001" not in handler._task_timers

        handler._schedule_task_timeout("t_001")

        assert "t_001" in handler._task_timers
        assert not handler._task_timers["t_001"].done()

    async def test_schedule_replaces_existing_timer(self, handler):
        """Calling _schedule_task_timeout twice replaces the old timer."""
        handler._tasks["t_001"] = {"id": "t_001", "agent_id": "agent-a",
                                    "state": "dispatched", "dispatched_at": time.time()}
        handler._schedule_task_timeout("t_001")
        old_timer = handler._task_timers["t_001"]

        handler._schedule_task_timeout("t_001")

        assert handler._task_timers["t_001"] is not old_timer

    async def test_schedule_unknown_task_warns(self, handler):
        """_schedule_task_timeout for unknown task logs a warning and returns."""
        handler._schedule_task_timeout("t_nonexistent")
        assert "t_nonexistent" not in handler._task_timers

    async def test_cancel_removes_timer(self, handler):
        """_cancel_task_timeout removes the timer from the dict."""
        handler._tasks["t_001"] = {"id": "t_001", "agent_id": "agent-a",
                                    "state": "dispatched", "dispatched_at": time.time()}
        handler._schedule_task_timeout("t_001")
        assert "t_001" in handler._task_timers

        handler._cancel_task_timeout("t_001")

        assert "t_001" not in handler._task_timers

    async def test_cancel_noop_for_absent(self, handler):
        """_cancel_task_timeout for unknown task is a no-op (no crash)."""
        handler._cancel_task_timeout("t_nonexistent")

    async def test_reset_cancel_and_restart(self, handler):
        """_reset_task_timeout cancels old timer and creates a new one."""
        handler._tasks["t_001"] = {"id": "t_001", "agent_id": "agent-a",
                                    "state": "dispatched", "dispatched_at": time.time()}
        handler._schedule_task_timeout("t_001")
        old_timer = handler._task_timers["t_001"]

        handler._reset_task_timeout("t_001")

        assert "t_001" in handler._task_timers
        assert handler._task_timers["t_001"] is not old_timer
        # Give the cancelled task a moment to finish
        import asyncio
        await asyncio.sleep(0)
        assert old_timer.cancelled()  # was cancelled


# ---------------------------------------------------------------------------
# Timeout trigger logic
# ---------------------------------------------------------------------------


class TestTimeoutTrigger:
    async def test_timeout_triggers_task_failed(self, handler):
        """Timer firing marks the task as failed('timeout')."""
        handler._tasks["t_001"] = {"id": "t_001", "agent_id": "agent-a",
                                    "state": "forwarded", "dispatched_at": time.time()}
        handler._schedule_task_timeout("t_001")

        import asyncio
        await asyncio.sleep(0.03)

        task = handler._tasks["t_001"]
        assert task["state"] == "failed"
        assert task["error"] == "timeout"
        assert "t_001" not in handler._task_timers

    async def test_timeout_sends_task_cancel(self, handler):
        """Timer firing sends task_cancel WS message to the agent."""
        mock_ws = AsyncMock()
        mock_ws.closed = False
        handler._ws_connections["agent-a"] = mock_ws
        handler._tasks["t_001"] = {"id": "t_001", "agent_id": "agent-a",
                                    "state": "forwarded", "dispatched_at": time.time()}

        handler._schedule_task_timeout("t_001")

        import asyncio
        await asyncio.sleep(0.03)

        mock_ws.send_json.assert_called_once()
        call_args = mock_ws.send_json.call_args[0][0]
        assert call_args["type"] == "task_cancel"
        assert call_args["id"] == "t_001"
        assert call_args["reason"] == "timeout"

    async def test_timeout_does_not_fail_terminal_task(self, handler):
        """Timer does not fail a task that is already in a terminal state."""
        handler._tasks["t_001"] = {"id": "t_001", "agent_id": "agent-a",
                                    "state": "completed", "dispatched_at": time.time()}
        handler._schedule_task_timeout("t_001")

        import asyncio
        await asyncio.sleep(0.03)

        assert handler._tasks["t_001"]["state"] == "completed"

    async def test_ack_resets_timer(self, handler):
        """task_ack resets the timer, preventing premature timeout."""
        mock_ws = AsyncMock()
        mock_ws.closed = False
        handler._ws_connections["agent-a"] = mock_ws
        handler._tasks["t_001"] = {"id": "t_001", "agent_id": "agent-a",
                                    "state": "dispatched", "dispatched_at": time.time()}
        handler._task_timeout = 0.05  # 50ms

        handler._schedule_task_timeout("t_001")

        import asyncio
        await asyncio.sleep(0.03)
        handler._reset_task_timeout("t_001")

        await asyncio.sleep(0.03)
        assert handler._tasks["t_001"]["state"] == "dispatched", \
            "Task should still be alive after ack reset"

        await asyncio.sleep(0.06)
        assert handler._tasks["t_001"]["state"] == "failed"

    async def test_progress_resets_timer(self, handler):
        """task_progress resets the timer, preventing premature timeout."""
        mock_ws = AsyncMock()
        mock_ws.closed = False
        handler._ws_connections["agent-a"] = mock_ws
        handler._tasks["t_001"] = {"id": "t_001", "agent_id": "agent-a",
                                    "state": "accepted", "dispatched_at": time.time()}
        handler._task_timeout = 0.05  # 50ms

        handler._schedule_task_timeout("t_001")

        import asyncio
        await asyncio.sleep(0.03)
        handler._reset_task_timeout("t_001")

        await asyncio.sleep(0.03)
        assert handler._tasks["t_001"]["state"] == "accepted"

        await asyncio.sleep(0.06)
        assert handler._tasks["t_001"]["state"] == "failed"

    async def test_complete_cancels_timer(self, handler):
        """task_complete cancels the timeout timer."""
        mock_ws = AsyncMock()
        mock_ws.closed = False
        handler._ws_connections["agent-a"] = mock_ws
        handler._tasks["t_001"] = {"id": "t_001", "agent_id": "agent-a",
                                    "state": "working", "dispatched_at": time.time()}

        handler._schedule_task_timeout("t_001")
        handler._cancel_task_timeout("t_001")

        import asyncio
        await asyncio.sleep(0.03)

        assert handler._tasks["t_001"]["state"] == "working"
        assert "t_001" not in handler._task_timers

    async def test_fail_agent_tasks_cancels_timers(self, handler):
        """_fail_agent_tasks cancels timeout timers for failed tasks."""
        handler._tasks["t_001"] = {"id": "t_001", "agent_id": "agent-a",
                                    "state": "forwarded", "dispatched_at": time.time()}
        handler._tasks["t_002"] = {"id": "t_002", "agent_id": "agent-a",
                                    "state": "working", "dispatched_at": time.time()}

        handler._schedule_task_timeout("t_001")
        handler._schedule_task_timeout("t_002")
        assert "t_001" in handler._task_timers
        assert "t_002" in handler._task_timers

        await handler._fail_agent_tasks("agent-a")

        assert "t_001" not in handler._task_timers
        assert "t_002" not in handler._task_timers

    async def test_timeout_does_not_crash_without_ws(self, handler):
        """Timer firing works gracefully when agent's WS connection is gone."""
        handler._tasks["t_001"] = {"id": "t_001", "agent_id": "agent-gone",
                                    "state": "forwarded", "dispatched_at": time.time()}

        handler._schedule_task_timeout("t_001")

        import asyncio
        await asyncio.sleep(0.03)

        assert handler._tasks["t_001"]["state"] == "failed"
        assert handler._tasks["t_001"]["error"] == "timeout"

    async def test_timeout_syncs_kanban(self, handler):
        """Timer firing reconciles with TaskStore if task was WS-dispatched."""
        task_store = MagicMock()
        handler.task_store = task_store
        handler._dispatched_ws_tasks = {"t_001": "agent-a"}
        handler._tasks["t_001"] = {"id": "t_001", "agent_id": "agent-a",
                                    "state": "forwarded", "dispatched_at": time.time()}

        handler._schedule_task_timeout("t_001")

        import asyncio
        await asyncio.sleep(0.03)

        task_store.update_task_status.assert_called_once_with(
            "t_001", "failed",
            result="Task timed out after 0.01s",
        )

    async def test_timeout_skips_kanban_for_undispatched(self, handler):
        """Timer does not sync with TaskStore for non-WS-dispatched tasks."""
        task_store = MagicMock()
        handler.task_store = task_store
        handler._dispatched_ws_tasks = {"t_other": "agent-a"}
        handler._tasks["t_001"] = {"id": "t_001", "agent_id": "agent-a",
                                    "state": "forwarded", "dispatched_at": time.time()}

        handler._schedule_task_timeout("t_001")

        import asyncio
        await asyncio.sleep(0.03)

        task_store.update_task_status.assert_not_called()
