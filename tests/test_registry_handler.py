"""Unit tests for registry_handler.py — WSMessageRouter and all protocol handlers."""

from __future__ import annotations

import json
import time
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aiohttp import web

from simple_a2a_registry.registry_handler import (
    WSMessageRouter,
    WSContext,
    create_default_router,
    _get_ws_handler,
    _reconcile_task_store,
    _check_state_sync_rate_limit,
    _reset_state_sync_rate_limiter,
    TimeoutResetFn,
    TimeoutCancelFn,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def ctx() -> WSContext:
    return WSContext(
        agent_id="test-agent",
        tasks={},
        connections={},
        task_store=None,
        _dispatched_ws_tasks=None,
    )


@pytest.fixture
def router() -> WSMessageRouter:
    return create_default_router()


@pytest.fixture
def ws() -> AsyncMock:
    return AsyncMock(spec=web.WebSocketResponse)


@pytest.fixture(autouse=True)
def _reset_rate_limiter() -> None:
    """Reset the module-level state_sync rate limiter before each test."""
    _reset_state_sync_rate_limiter()


# ---------------------------------------------------------------------------
# WSMessageRouter
# ---------------------------------------------------------------------------


class TestWSMessageRouter:
    def test_router_has_8_default_handlers(self, router: WSMessageRouter):
        expected = {
            "ping", "task_ack", "task_progress", "task_complete",
            "task_fail", "task_result", "state_sync", "close",
        }
        assert set(router._handlers.keys()) == expected

    def test_register_new_handler(self, router: WSMessageRouter):
        """Adding a new handler works."""
        calls = []

        async def my_handler(ws, data, ctx):
            calls.append(data)

        router.register("my_type", my_handler)
        assert "my_type" in router._handlers

    def test_replace_existing_handler(self, router: WSMessageRouter):
        """Registering same msg_type replaces the old handler."""
        calls = []

        async def replacement(ws, data, ctx):
            calls.append("replacement")

        router.register("ping", replacement)
        assert router._handlers["ping"] is replacement

    def test_unregister_handler(self, router: WSMessageRouter):
        router.unregister("ping")
        assert "ping" not in router._handlers

    def test_unknown_type_silently_ignored(self, router: WSMessageRouter, ws, ctx):
        """Unknown message types are silently ignored (backward compat)."""
        # Should not raise
        import asyncio
        asyncio.run(router.dispatch(ws, {"type": "nonexistent"}, ctx))
        # No exception means success

    def test_dispatch_calls_correct_handler(self, router: WSMessageRouter, ws, ctx):
        """dispatch routes to the right handler by msg_type."""
        calls = []

        async def handler_a(ws, data, ctx):
            calls.append(("a", data["val"]))

        async def handler_b(ws, data, ctx):
            calls.append(("b", data["val"]))

        router.register("type_a", handler_a)
        router.register("type_b", handler_b)

        import asyncio
        asyncio.run(router.dispatch(ws, {"type": "type_a", "val": 1}, ctx))
        asyncio.run(router.dispatch(ws, {"type": "type_b", "val": 2}, ctx))

        assert calls == [("a", 1), ("b", 2)]

    def test_handler_exception_logged_not_raised(self, router: WSMessageRouter, ws, ctx):
        """Exceptions inside handlers are logged, not propagated."""

        async def broken(ws, data, ctx):
            raise ValueError("oops")

        router.register("broken", broken)

        import asyncio
        # Should not raise — exception is caught inside dispatch
        asyncio.run(router.dispatch(ws, {"type": "broken"}, ctx))


# ---------------------------------------------------------------------------
# Handler: ping
# ---------------------------------------------------------------------------


class TestPingHandler:
    def test_ping_returns_pong(self, router: WSMessageRouter):
        """ping → pong response."""
        ws = AsyncMock()
        import asyncio
        asyncio.run(
            router.dispatch(
                ws, {"type": "ping", "ts": 1717000000},
                WSContext(agent_id="agent-a", tasks={}),
            )
        )
        ws.send_json.assert_called_once()
        call_args = ws.send_json.call_args[0][0]
        assert call_args["type"] == "pong"

    def test_ping_updates_active_task(self, router: WSMessageRouter):
        """ping with active_task updates in-memory task state."""
        ws = AsyncMock()
        tasks = {
            "t_abc": {"id": "t_abc", "state": "working", "progress": 0.3},
        }
        import asyncio
        asyncio.run(
            router.dispatch(
                ws,
                {
                    "type": "ping",
                    "active_task": "t_abc",
                    "task_status": "working",
                    "task_progress": 0.5,
                },
                WSContext(agent_id="agent-a", tasks=tasks),
            )
        )
        assert tasks["t_abc"]["state"] == "working"
        assert tasks["t_abc"]["progress"] == 0.5

    def test_ping_returns_pending_tasks_for_agent(self, router: WSMessageRouter):
        """pong includes pending_tasks — non-terminal tasks for this agent."""
        ws = AsyncMock()
        tasks = {
            "t_active": {"id": "t_active", "agent_id": "agent-a", "state": "working", "title": "Active task"},
            "t_pending": {"id": "t_pending", "agent_id": "agent-a", "state": "dispatched", "title": "Pending task"},
            "t_completed": {"id": "t_completed", "agent_id": "agent-a", "state": "completed", "title": "Done"},
            "t_other": {"id": "t_other", "agent_id": "agent-b", "state": "working", "title": "Other agent task"},
        }
        import asyncio
        asyncio.run(
            router.dispatch(
                ws, {"type": "ping"},
                WSContext(agent_id="agent-a", tasks=tasks),
            )
        )
        ws.send_json.assert_called_once()
        call_args = ws.send_json.call_args[0][0]
        assert call_args["type"] == "pong"
        pending = call_args.get("pending_tasks", [])
        assert len(pending) == 2, f"Expected 2 pending tasks, got {len(pending)}"
        pending_ids = {p["id"] for p in pending}
        assert "t_active" in pending_ids
        assert "t_pending" in pending_ids
        assert "t_completed" not in pending_ids  # Terminal state excluded
        assert "t_other" not in pending_ids  # Other agent excluded


# ---------------------------------------------------------------------------
# Handler: task_ack
# ---------------------------------------------------------------------------


class TestTaskAckHandler:
    def test_task_ack_updates_status(self, router: WSMessageRouter):
        """task_ack → task state becomes 'accepted'."""
        ws = AsyncMock()
        tasks = {
            "t_001": {"id": "t_001", "state": "dispatched"},
        }
        import asyncio
        asyncio.run(
            router.dispatch(
                ws,
                {
                    "type": "task_ack",
                    "id": "t_001",
                    "status": "accepted",
                    "started_at": 1717000000,
                },
                WSContext(agent_id="agent-a", tasks=tasks),
            )
        )
        assert tasks["t_001"]["state"] == "accepted"
        assert tasks["t_001"]["started_at"] == 1717000000

    def test_task_ack_auto_creates(self, router: WSMessageRouter):
        """task_ack for unknown task auto-creates entry."""
        ws = AsyncMock()
        tasks = {}
        import asyncio
        asyncio.run(
            router.dispatch(
                ws,
                {"type": "task_ack", "id": "t_new", "status": "accepted"},
                WSContext(agent_id="agent-a", tasks=tasks),
            )
        )
        assert "t_new" in tasks
        assert tasks["t_new"]["state"] == "accepted"
        assert tasks["t_new"]["agent_id"] == "agent-a"

    def test_task_ack_without_id(self, router: WSMessageRouter):
        """task_ack without 'id' is silently ignored."""
        ws = AsyncMock()
        tasks = {}
        import asyncio
        asyncio.run(
            router.dispatch(
                ws,
                {"type": "task_ack", "status": "accepted"},
                WSContext(agent_id="agent-a", tasks=tasks),
            )
        )
        # No crash, tasks unchanged


# ---------------------------------------------------------------------------
# Handler: task_complete
# ---------------------------------------------------------------------------


class TestTaskCompleteHandler:
    def test_task_complete_updates_status(self, router: WSMessageRouter):
        """task_complete → task marked completed, result stored."""
        ws = AsyncMock()
        tasks = {
            "t_001": {"id": "t_001", "state": "working"},
        }
        result = {"output": "done", "score": 0.95}
        import asyncio
        asyncio.run(
            router.dispatch(
                ws,
                {
                    "type": "task_complete",
                    "id": "t_001",
                    "status": "completed",
                    "result": result,
                    "metrics": {"duration": 12.5},
                },
                WSContext(agent_id="agent-a", tasks=tasks),
            )
        )
        assert tasks["t_001"]["state"] == "completed"
        assert tasks["t_001"]["result"] == result
        assert tasks["t_001"]["metrics"] == {"duration": 12.5}

    def test_task_complete_auto_creates(self, router: WSMessageRouter):
        """task_complete for unknown task auto-creates."""
        ws = AsyncMock()
        tasks = {}
        import asyncio
        asyncio.run(
            router.dispatch(
                ws,
                {"type": "task_complete", "id": "t_new", "result": {"ok": True}},
                WSContext(agent_id="agent-a", tasks=tasks),
            )
        )
        assert "t_new" in tasks
        assert tasks["t_new"]["state"] == "completed"

    def test_task_complete_no_id(self, router: WSMessageRouter):
        """task_complete without id is ignored."""
        ws = AsyncMock()
        tasks = {}
        import asyncio
        asyncio.run(
            router.dispatch(
                ws, {"type": "task_complete", "result": {}},
                WSContext(agent_id="agent-a", tasks=tasks),
            )
        )


# ---------------------------------------------------------------------------
# Handler: task_fail
# ---------------------------------------------------------------------------


class TestTaskFailHandler:
    def test_task_fail_updates_status(self, router: WSMessageRouter):
        """task_fail → task marked failed, error stored."""
        ws = AsyncMock()
        tasks = {
            "t_001": {"id": "t_001", "state": "working"},
        }
        import asyncio
        asyncio.run(
            router.dispatch(
                ws,
                {
                    "type": "task_fail",
                    "id": "t_001",
                    "status": "failed",
                    "error": "Timeout exceeded",
                    "code": "ERR_TIMEOUT",
                },
                WSContext(agent_id="agent-a", tasks=tasks),
            )
        )
        assert tasks["t_001"]["state"] == "failed"
        assert tasks["t_001"]["error"] == "Timeout exceeded"
        assert tasks["t_001"]["error_code"] == "ERR_TIMEOUT"

    def test_task_fail_auto_creates(self, router: WSMessageRouter):
        """task_fail for unknown task auto-creates."""
        ws = AsyncMock()
        tasks = {}
        import asyncio
        asyncio.run(
            router.dispatch(
                ws,
                {"type": "task_fail", "id": "t_new", "error": "Out of memory"},
                WSContext(agent_id="agent-a", tasks=tasks),
            )
        )
        assert "t_new" in tasks
        assert tasks["t_new"]["state"] == "failed"
        assert tasks["t_new"]["error"] == "Out of memory"

    def test_task_fail_no_id(self, router: WSMessageRouter):
        """task_fail without id is silently ignored."""
        ws = AsyncMock()
        tasks = {}
        import asyncio
        asyncio.run(
            router.dispatch(
                ws, {"type": "task_fail", "error": "fail"},
                WSContext(agent_id="agent-a", tasks=tasks),
            )
        )


# ---------------------------------------------------------------------------
# Handler: task_progress
# ---------------------------------------------------------------------------


class TestTaskProgressHandler:
    def test_task_progress_updates(self, router: WSMessageRouter):
        """task_progress updates progress and message."""
        ws = AsyncMock()
        tasks = {
            "t_001": {"id": "t_001", "state": "working", "progress": 0.3},
        }
        import asyncio
        asyncio.run(
            router.dispatch(
                ws,
                {
                    "type": "task_progress",
                    "id": "t_001",
                    "status": "working",
                    "progress": 0.75,
                    "message": "Compiling...",
                },
                WSContext(agent_id="agent-a", tasks=tasks),
            )
        )
        assert tasks["t_001"]["progress"] == 0.75
        assert tasks["t_001"]["message"] == "Compiling..."

    def test_task_progress_auto_creates(self, router: WSMessageRouter):
        """task_progress for unknown task auto-creates."""
        ws = AsyncMock()
        tasks = {}
        import asyncio
        asyncio.run(
            router.dispatch(
                ws,
                {"type": "task_progress", "id": "t_new", "progress": 0.5},
                WSContext(agent_id="agent-a", tasks=tasks),
            )
        )
        assert "t_new" in tasks
        assert tasks["t_new"]["progress"] == 0.5


# ---------------------------------------------------------------------------
# Handler: task_result (legacy)
# ---------------------------------------------------------------------------


class TestTaskResultHandler:
    def test_task_result_backward_compat(self, router: WSMessageRouter):
        """Legacy task_result still works."""
        ws = AsyncMock()
        tasks = {"t_001": {"id": "t_001", "state": "working"}}
        import asyncio
        asyncio.run(
            router.dispatch(
                ws,
                {
                    "type": "task_result",
                    "id": "t_001",
                    "status": "completed",
                    "result": {"ok": True},
                },
                WSContext(agent_id="agent-a", tasks=tasks),
            )
        )
        assert tasks["t_001"]["state"] == "completed"
        assert tasks["t_001"]["result"] == {"ok": True}


# ---------------------------------------------------------------------------
# Handler: state_sync
# ---------------------------------------------------------------------------


class TestStateSyncHandler:
    def test_state_sync_auto_creates_missing_tasks(self, router: WSMessageRouter):
        """state_sync auto-creates tasks the agent reports but server doesn't know."""
        ws = AsyncMock()
        tasks = {}
        import asyncio
        asyncio.run(
            router.dispatch(
                ws,
                {
                    "type": "state_sync",
                    "agent_id": "agent-a",
                    "active_tasks": [
                        {"id": "t_001", "status": "working", "started_at": 1717000000},
                    ],
                },
                WSContext(agent_id="agent-a", tasks=tasks),
            )
        )
        assert "t_001" in tasks
        assert tasks["t_001"]["state"] == "working"

    def test_state_sync_sends_reply_with_orphans(self, router: WSMessageRouter):
        """state_sync sends state_sync_reply with orphaned completed tasks."""
        ws = AsyncMock()
        tasks = {
            "t_001": {
                "id": "t_001", "agent_id": "agent-a",
                "state": "completed", "result": {"ok": True},
            },
        }
        import asyncio
        asyncio.run(
            router.dispatch(
                ws,
                {
                    "type": "state_sync",
                    "agent_id": "agent-a",
                    "active_tasks": [],  # empty — t_001 is orphaned
                },
                WSContext(agent_id="agent-a", tasks=tasks),
            )
        )
        ws.send_json.assert_called_once()
        reply = ws.send_json.call_args[0][0]
        assert reply["type"] == "state_sync_reply"
        assert len(reply["orphaned_tasks"]) == 1
        assert reply["orphaned_tasks"][0]["id"] == "t_001"

    def test_state_sync_updates_existing_tasks(self, router: WSMessageRouter):
        """state_sync updates existing task states from agent report."""
        ws = AsyncMock()
        tasks = {
            "t_001": {"id": "t_001", "agent_id": "agent-a", "state": "dispatched"},
        }
        import asyncio
        asyncio.run(
            router.dispatch(
                ws,
                {
                    "type": "state_sync",
                    "agent_id": "agent-a",
                    "active_tasks": [
                        {"id": "t_001", "status": "working"},
                    ],
                },
                WSContext(agent_id="agent-a", tasks=tasks),
            )
        )
        assert tasks["t_001"]["state"] == "working"


# ---------------------------------------------------------------------------
# Handler: state_sync with DB (TaskStore) reconciliation
# ---------------------------------------------------------------------------


class TestStateSyncHandlerDB:
    """Tests for state_sync with TaskStore DB reconciliation."""

    def test_state_sync_restores_db_dispatched_tasks(self, router: WSMessageRouter):
        """state_sync queries DB and restores missing non-terminal tasks."""
        ws = AsyncMock()
        tasks: dict = {}

        task_store = MagicMock()
        db_task = MagicMock()
        db_task.id = "t_db_001"
        db_task.status = "running"
        db_task.result = None
        task_store.get_task.return_value = db_task

        dispatched = {"t_db_001": "my-agent", "t_db_002": "other-agent"}

        import asyncio
        asyncio.run(
            router.dispatch(
                ws,
                {"type": "state_sync", "agent_id": "my-agent",
                 "active_tasks": []},
                WSContext(
                    agent_id="my-agent", tasks=tasks,
                    task_store=task_store,
                    _dispatched_ws_tasks=dispatched,
                ),
            )
        )

        assert "t_db_001" in tasks
        assert tasks["t_db_001"]["state"] == "running"
        assert tasks["t_db_001"]["agent_id"] == "my-agent"
        assert "t_db_002" not in tasks
        task_store.get_task.assert_called_once_with("t_db_001")

    def test_state_sync_does_not_restore_terminal_db_tasks(self, router: WSMessageRouter):
        """state_sync does NOT restore completed/failed tasks from DB into ctx.tasks."""
        ws = AsyncMock()
        tasks: dict = {}

        task_store = MagicMock()
        db_task = MagicMock()
        db_task.id = "t_done_001"
        db_task.status = "completed"
        db_task.result = '{"ok": true}'
        task_store.get_task.return_value = db_task

        dispatched = {"t_done_001": "my-agent"}

        import asyncio
        asyncio.run(
            router.dispatch(
                ws,
                {"type": "state_sync", "agent_id": "my-agent",
                 "active_tasks": []},
                WSContext(
                    agent_id="my-agent", tasks=tasks,
                    task_store=task_store,
                    _dispatched_ws_tasks=dispatched,
                ),
            )
        )

        assert "t_done_001" not in tasks
        ws.send_json.assert_called_once()
        reply = ws.send_json.call_args[0][0]
        assert reply["type"] == "state_sync_reply"
        assert len(reply["orphaned_tasks"]) == 1
        assert reply["orphaned_tasks"][0]["id"] == "t_done_001"
        assert reply["orphaned_tasks"][0]["status"] == "completed"

    def test_state_sync_db_orphan_includes_memory_and_db(self, router: WSMessageRouter):
        """state_sync includes both memory-based and DB-based orphans in reply."""
        ws = AsyncMock()
        tasks = {
            "t_mem_done": {"id": "t_mem_done", "agent_id": "my-agent",
                           "state": "completed", "result": {"memory": True}},
        }

        task_store = MagicMock()

        def mock_get_task(task_id):
            if task_id == "t_db_done":
                t = MagicMock()
                t.id = "t_db_done"; t.status = "completed"; t.result = '{"db": true}'
                return t
            if task_id == "t_db_running":
                t = MagicMock()
                t.id = "t_db_running"; t.status = "running"; t.result = None
                return t
            return None

        task_store.get_task.side_effect = mock_get_task

        dispatched = {"t_db_done": "my-agent", "t_db_running": "my-agent"}

        import asyncio
        asyncio.run(
            router.dispatch(
                ws,
                {"type": "state_sync", "agent_id": "my-agent",
                 "active_tasks": []},
                WSContext(
                    agent_id="my-agent", tasks=tasks,
                    task_store=task_store,
                    _dispatched_ws_tasks=dispatched,
                ),
            )
        )

        ws.send_json.assert_called_once()
        reply = ws.send_json.call_args[0][0]
        assert reply["type"] == "state_sync_reply"
        orphan_ids = {o["id"] for o in reply["orphaned_tasks"]}
        assert "t_mem_done" in orphan_ids
        assert "t_db_done" in orphan_ids
        assert "t_db_running" not in orphan_ids

    def test_state_sync_db_error_does_not_crash(self, router: WSMessageRouter):
        """TaskStore.get_task failure is caught; handler continues."""
        ws = AsyncMock()
        tasks = {"t_001": {"id": "t_001", "agent_id": "my-agent", "state": "working"}}

        task_store = MagicMock()
        task_store.get_task.side_effect = Exception("DB connection lost")
        dispatched = {"t_001": "my-agent", "t_bad": "my-agent"}

        import asyncio
        asyncio.run(
            router.dispatch(
                ws,
                {"type": "state_sync", "agent_id": "my-agent",
                 "active_tasks": []},
                WSContext(
                    agent_id="my-agent", tasks=tasks,
                    task_store=task_store,
                    _dispatched_ws_tasks=dispatched,
                ),
            )
        )

        ws.send_json.assert_called_once()
        reply = ws.send_json.call_args[0][0]
        assert reply["type"] == "state_sync_reply"

    def test_state_sync_skips_db_when_not_configured(self, router: WSMessageRouter):
        """state_sync works without task_store (backward compatible)."""
        ws = AsyncMock()
        tasks = {"t_001": {"id": "t_001", "agent_id": "agent-a", "state": "working"}}

        import asyncio
        asyncio.run(
            router.dispatch(
                ws,
                {"type": "state_sync", "agent_id": "agent-a",
                 "active_tasks": []},
                WSContext(agent_id="agent-a", tasks=tasks,
                          task_store=None, _dispatched_ws_tasks=None),
            )
        )

        ws.send_json.assert_called_once()
        reply = ws.send_json.call_args[0][0]
        assert reply["type"] == "state_sync_reply"

    def test_state_sync_agent_completed_unknown_logged(
            self, router: WSMessageRouter, caplog):
        """state_sync logs agent-reported completed tasks server doesn't know."""
        ws = AsyncMock()
        tasks: dict = {}
        caplog.set_level("INFO")

        import asyncio
        asyncio.run(
            router.dispatch(
                ws,
                {"type": "state_sync", "agent_id": "agent-a",
                 "active_tasks": [
                     {"id": "t_gone", "status": "completed"},
                 ]},
                WSContext(agent_id="agent-a", tasks=tasks,
                          task_store=None, _dispatched_ws_tasks=None),
            )
        )

        assert any(
            "waiting for task_complete" in record.message
            for record in caplog.records
        ), "agent-completed/server-unknown tasks should be logged"


# ---------------------------------------------------------------------------
# Module-level bridge
# ---------------------------------------------------------------------------


class TestGetWSHandler:
    def test_returns_callable_for_known_types(self):
        for t in ["ping", "task_ack", "task_complete", "task_fail",
                   "task_progress", "task_result", "state_sync", "close"]:
            h = _get_ws_handler(t)
            assert callable(h), f"{t} should return callable"

    def test_returns_none_for_unknown(self):
        assert _get_ws_handler("nonexistent") is None
        assert _get_ws_handler("") is None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class TestReconcileTaskStore:
    def test_skips_when_no_task_store(self):
        """No-op when task_store is None."""
        _reconcile_task_store(None, {}, "t_001", "completed")

    def test_skips_when_not_dispatched(self):
        """No-op when task not in dispatched_tasks."""
        task_store = MagicMock()
        _reconcile_task_store(task_store, {"t_other": "agent"}, "t_001", "completed")
        task_store.update_task_status.assert_not_called()

    @patch("simple_a2a_registry.registry_handler.TaskStatus")
    def test_calls_update_for_running(self, MockTaskStatus):
        """task_ack triggers TaskStore.RUNNING update."""
        task_store = MagicMock()
        MockTaskStatus.RUNNING.value = "running"
        MockTaskStatus.COMPLETED.value = "completed"
        MockTaskStatus.FAILED.value = "failed"

        _reconcile_task_store(task_store, {"t_001": "agent"}, "t_001", "running")
        task_store.update_task_status.assert_called_once_with(
            "t_001", "running",
        )

    @patch("simple_a2a_registry.registry_handler.TaskStatus")
    def test_calls_update_for_completed(self, MockTaskStatus):
        """task_complete triggers TaskStore.COMPLETED update."""
        task_store = MagicMock()
        MockTaskStatus.COMPLETED.value = "completed"

        _reconcile_task_store(
            task_store, {"t_001": "agent"}, "t_001", "completed",
            result={"ok": True},
        )
        task_store.update_task_status.assert_called_once()

    @patch("simple_a2a_registry.registry_handler.TaskStatus")
    def test_calls_update_for_failed(self, MockTaskStatus):
        """task_fail triggers TaskStore.FAILED update."""
        task_store = MagicMock()
        MockTaskStatus.FAILED.value = "failed"

        _reconcile_task_store(
            task_store, {"t_001": "agent"}, "t_001", "failed",
            error="Something broke",
        )
        task_store.update_task_status.assert_called_once()


# ---------------------------------------------------------------------------
# P1.2: Timeout callback integration
# ---------------------------------------------------------------------------


class TestTimeoutCallbacks:
    """Verify that handlers invoke the timeout callbacks wired through WSContext."""

    def test_task_ack_calls_reset_timeout(self, router: WSMessageRouter):
        """task_ack calls reset_task_timeout when provided."""
        ws = AsyncMock()
        tasks = {"t_001": {"id": "t_001", "state": "dispatched"}}
        reset_calls = []
        cancel_calls = []

        def reset_fn(tid):
            reset_calls.append(tid)

        def cancel_fn(tid):
            cancel_calls.append(tid)

        import asyncio
        asyncio.run(
            router.dispatch(
                ws,
                {"type": "task_ack", "id": "t_001", "status": "accepted"},
                WSContext(
                    agent_id="agent-a", tasks=tasks,
                    reset_task_timeout=reset_fn,
                    cancel_task_timeout=cancel_fn,
                ),
            )
        )
        assert reset_calls == ["t_001"]
        assert cancel_calls == []

    def test_task_progress_calls_reset_timeout(self, router: WSMessageRouter):
        """task_progress calls reset_task_timeout when provided."""
        ws = AsyncMock()
        tasks = {"t_001": {"id": "t_001", "state": "working"}}
        reset_calls = []
        cancel_calls = []

        def reset_fn(tid):
            reset_calls.append(tid)

        def cancel_fn(tid):
            cancel_calls.append(tid)

        import asyncio
        asyncio.run(
            router.dispatch(
                ws,
                {"type": "task_progress", "id": "t_001", "progress": 0.5},
                WSContext(
                    agent_id="agent-a", tasks=tasks,
                    reset_task_timeout=reset_fn,
                    cancel_task_timeout=cancel_fn,
                ),
            )
        )
        assert reset_calls == ["t_001"]
        assert cancel_calls == []

    def test_task_complete_calls_cancel_timeout(self, router: WSMessageRouter):
        """task_complete calls cancel_task_timeout when provided."""
        ws = AsyncMock()
        tasks = {"t_001": {"id": "t_001", "state": "working"}}
        reset_calls = []
        cancel_calls = []

        def reset_fn(tid):
            reset_calls.append(tid)

        def cancel_fn(tid):
            cancel_calls.append(tid)

        import asyncio
        asyncio.run(
            router.dispatch(
                ws,
                {"type": "task_complete", "id": "t_001", "result": {}},
                WSContext(
                    agent_id="agent-a", tasks=tasks,
                    reset_task_timeout=reset_fn,
                    cancel_task_timeout=cancel_fn,
                ),
            )
        )
        assert cancel_calls == ["t_001"]
        assert reset_calls == []

    def test_task_fail_calls_cancel_timeout(self, router: WSMessageRouter):
        """task_fail calls cancel_task_timeout when provided."""
        ws = AsyncMock()
        tasks = {"t_001": {"id": "t_001", "state": "working"}}
        cancel_calls = []

        def cancel_fn(tid):
            cancel_calls.append(tid)

        import asyncio
        asyncio.run(
            router.dispatch(
                ws,
                {"type": "task_fail", "id": "t_001", "error": "fail"},
                WSContext(
                    agent_id="agent-a", tasks=tasks,
                    reset_task_timeout=lambda tid: None,
                    cancel_task_timeout=cancel_fn,
                ),
            )
        )
        assert cancel_calls == ["t_001"]

    def test_handlers_noop_when_callbacks_none(self, router: WSMessageRouter):
        """Handlers work without crashing when timeout callbacks are None."""
        ws = AsyncMock()
        tasks = {"t_001": {"id": "t_001", "state": "dispatched"}}

        import asyncio
        asyncio.run(
            router.dispatch(
                ws,
                {"type": "task_ack", "id": "t_001", "status": "accepted"},
                WSContext(agent_id="agent-a", tasks=tasks),
            )
        )
        asyncio.run(
            router.dispatch(
                ws,
                {"type": "task_progress", "id": "t_001", "progress": 0.5},
                WSContext(agent_id="agent-a", tasks=tasks),
            )
        )
        asyncio.run(
            router.dispatch(
                ws,
                {"type": "task_complete", "id": "t_001", "result": {}},
                WSContext(agent_id="agent-a", tasks=tasks),
            )
        )
        asyncio.run(
            router.dispatch(
                ws,
                {"type": "task_fail", "id": "t_001", "error": "fail"},
                WSContext(agent_id="agent-a", tasks=tasks),
            )
        )
        assert tasks["t_001"]["state"] == "failed"


# ---------------------------------------------------------------------------
# P3.3: state_sync security boundaries — auth + rate limiting + registration
# ---------------------------------------------------------------------------


class TestStateSyncSecurity:
    """Security boundary tests for state_sync handler."""

    def test_state_sync_mismatched_agent_id_rejected(self, router: WSMessageRouter):
        """state_sync with agent_id != ctx.agent_id -> error response."""
        ws = AsyncMock()
        tasks = {"t_001": {"id": "t_001", "agent_id": "agent-a", "state": "working"}}
        import asyncio
        asyncio.run(
            router.dispatch(
                ws,
                {
                    "type": "state_sync",
                    "agent_id": "malicious-agent",
                    "active_tasks": [],
                },
                WSContext(agent_id="agent-a", tasks=tasks),
            )
        )
        ws.send_json.assert_called_once()
        call_data = ws.send_json.call_args[0][0]
        assert call_data["type"] == "error"
        assert "does not match" in call_data["detail"]

    def test_state_sync_without_agent_id_uses_ctx(self, router: WSMessageRouter):
        """state_sync without agent_id uses ctx.agent_id and passes."""
        ws = AsyncMock()
        tasks = {"t_001": {"id": "t_001", "agent_id": "agent-a", "state": "completed"}}
        import asyncio
        asyncio.run(
            router.dispatch(
                ws,
                {"type": "state_sync", "active_tasks": []},
                WSContext(agent_id="agent-a", tasks=tasks),
            )
        )
        ws.send_json.assert_called_once()
        call_data = ws.send_json.call_args[0][0]
        assert call_data["type"] == "state_sync_reply"

    def test_state_sync_rate_limit(self, router: WSMessageRouter):
        """state_sync within 30s -> rejected."""
        ws = AsyncMock()
        tasks: dict = {}
        import asyncio
        asyncio.run(
            router.dispatch(
                ws,
                {"type": "state_sync", "active_tasks": []},
                WSContext(agent_id="agent-a", tasks=tasks),
            )
        )
        ws.reset_mock()
        asyncio.run(
            router.dispatch(
                ws,
                {"type": "state_sync", "active_tasks": []},
                WSContext(agent_id="agent-a", tasks=tasks),
            )
        )
        ws.send_json.assert_called_once()
        call_data = ws.send_json.call_args[0][0]
        assert call_data["type"] == "error"
        assert "rate limited" in call_data["detail"]

    def test_state_sync_rate_limit_resets(self):
        """_check_state_sync_rate_limit allows after cooldown."""
        ok1, _ = _check_state_sync_rate_limit("rate-test-agent")
        assert ok1
        ok2, retry = _check_state_sync_rate_limit("rate-test-agent")
        assert not ok2
        assert retry > 0

    def test_state_sync_unregistered_agent_rejected(self, router: WSMessageRouter):
        """state_sync from unregistered agent -> error response."""
        ws = AsyncMock()
        tasks: dict = {}
        mock_store = MagicMock()
        mock_store.get_agent.return_value = None
        import asyncio
        asyncio.run(
            router.dispatch(
                ws,
                {"type": "state_sync", "active_tasks": []},
                WSContext(agent_id="unknown-agent", tasks=tasks, store=mock_store),
            )
        )
        ws.send_json.assert_called_once()
        call_data = ws.send_json.call_args[0][0]
        assert call_data["type"] == "error"
        assert "is not registered" in call_data["detail"]
        mock_store.get_agent.assert_called_once_with(
            "unknown-agent", tenant=None,
        )

    def test_state_sync_disabled_agent_rejected(self, router: WSMessageRouter):
        """state_sync from disabled agent -> error response."""
        ws = AsyncMock()
        tasks: dict = {}
        mock_store = MagicMock()
        mock_store.get_agent.return_value = {
            "id": "disabled-agent",
            "name": "Disabled Agent",
            "disabled": True,
        }
        import asyncio
        asyncio.run(
            router.dispatch(
                ws,
                {"type": "state_sync", "active_tasks": []},
                WSContext(agent_id="disabled-agent", tasks=tasks, store=mock_store),
            )
        )
        ws.send_json.assert_called_once()
        call_data = ws.send_json.call_args[0][0]
        assert call_data["type"] == "error"
        assert "is disabled" in call_data["detail"]

    def test_state_sync_registered_agent_passes(self, router: WSMessageRouter):
        """state_sync from registered, enabled agent -> state_sync_reply."""
        ws = AsyncMock()
        tasks = {"t_001": {"id": "t_001", "agent_id": "agent-a", "state": "completed"}}
        mock_store = MagicMock()
        mock_store.get_agent.return_value = {
            "id": "agent-a",
            "name": "Agent A",
            "disabled": False,
        }
        import asyncio
        asyncio.run(
            router.dispatch(
                ws,
                {"type": "state_sync", "active_tasks": []},
                WSContext(agent_id="agent-a", tasks=tasks, store=mock_store),
            )
        )
        ws.send_json.assert_called_once()
        call_data = ws.send_json.call_args[0][0]
        assert call_data["type"] == "state_sync_reply"

    def test_state_sync_skips_store_check_when_store_none(
            self, router: WSMessageRouter):
        """state_sync works when store is None (backward compatible)."""
        ws = AsyncMock()
        tasks = {"t_001": {"id": "t_001", "agent_id": "agent-a", "state": "completed"}}
        import asyncio
        asyncio.run(
            router.dispatch(
                ws,
                {"type": "state_sync", "active_tasks": []},
                WSContext(agent_id="agent-a", tasks=tasks, store=None),
            )
        )
        ws.send_json.assert_called_once()
        call_data = ws.send_json.call_args[0][0]
        assert call_data["type"] == "state_sync_reply"