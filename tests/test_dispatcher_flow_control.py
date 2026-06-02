"""Tests for P3.2: flow control (FlowController + integration in Dispatcher).

Tests cover:
1. FlowController unit tests:
   - max_concurrent_tasks gate
   - circuit breaker trip / auto-recovery / manual reset
   - retry backoff calculation
   - on_task_dispatched / on_task_arrived / on_task_failed / on_task_departed
2. Dispatcher integration tests:
   - max_concurrent limits WS dispatch
   - max_concurrent limits callback dispatch
   - circuit breaker blocks dispatch after consecutive failures
   - flow control stats in poll cycle
"""

from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path
from typing import Any, AsyncGenerator, Generator

import aiohttp
import pytest
from aiohttp import web

from simple_a2a_registry.store import Store as RegistryStore
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
from simple_a2a_registry.orchestration.flow_control import (
    FlowController,
    FlowControlConfig,
    AgentFlowState,
)


# ===================================================================
# FlowController Unit Tests
# ===================================================================


class TestFlowControllerConcurrency:
    """FlowController max_concurrent_tasks gate."""

    def test_initial_state_allows_dispatch(self) -> None:
        fc = FlowController(FlowControlConfig(max_concurrent_tasks=3))
        assert fc.can_dispatch("agent-1") is True

    def test_blocks_at_max_concurrent(self) -> None:
        fc = FlowController(FlowControlConfig(max_concurrent_tasks=2))
        # Fill both slots
        fc.on_task_dispatched("agent-1")
        fc.on_task_arrived("agent-1")
        fc.on_task_dispatched("agent-1")
        fc.on_task_arrived("agent-1")
        # Third dispatch should be blocked
        assert fc.can_dispatch("agent-1") is False

    def test_unlimited_allows_all(self) -> None:
        fc = FlowController(FlowControlConfig(max_concurrent_tasks=0))
        for _ in range(100):
            fc.on_task_dispatched("agent-1")
            fc.on_task_arrived("agent-1")
        assert fc.can_dispatch("agent-1") is True

    def test_depart_releases_slot(self) -> None:
        fc = FlowController(FlowControlConfig(max_concurrent_tasks=1))
        fc.on_task_dispatched("agent-1")
        fc.on_task_arrived("agent-1")
        assert fc.can_dispatch("agent-1") is False
        fc.on_task_departed("agent-1")
        assert fc.can_dispatch("agent-1") is True

    def test_multiple_agents_independent(self) -> None:
        fc = FlowController(FlowControlConfig(max_concurrent_tasks=1))
        fc.on_task_dispatched("agent-a")
        fc.on_task_arrived("agent-a")
        assert fc.can_dispatch("agent-a") is False
        assert fc.can_dispatch("agent-b") is True
        fc.on_task_departed("agent-a")
        assert fc.can_dispatch("agent-a") is True

    def test_concurrent_count(self) -> None:
        fc = FlowController(FlowControlConfig(max_concurrent_tasks=3))
        assert fc.get_concurrent_count("agent-1") == 0
        fc.on_task_dispatched("agent-1")
        fc.on_task_arrived("agent-1")
        assert fc.get_concurrent_count("agent-1") == 1
        fc.on_task_dispatched("agent-1")
        fc.on_task_arrived("agent-1")
        assert fc.get_concurrent_count("agent-1") == 2
        fc.on_task_departed("agent-1")
        assert fc.get_concurrent_count("agent-1") == 1


class TestFlowControllerCircuitBreaker:
    """Circuit breaker trip / recovery."""

    def test_trips_after_threshold(self) -> None:
        fc = FlowController(FlowControlConfig(
            circuit_breaker_threshold=3,
            circuit_breaker_cooldown=300,
        ))
        # Two failures — still ok
        fc.on_task_failed("agent-1")
        fc.on_task_departed("agent-1")
        assert fc.can_dispatch("agent-1") is True
        fc.on_task_failed("agent-1")
        fc.on_task_departed("agent-1")
        assert fc.can_dispatch("agent-1") is True
        # Third failure — circuit trips
        fc.on_task_failed("agent-1")
        fc.on_task_departed("agent-1")
        assert fc.is_circuit_tripped("agent-1") is True
        assert fc.can_dispatch("agent-1") is False

    def test_success_resets_failures(self) -> None:
        fc = FlowController(FlowControlConfig(circuit_breaker_threshold=3))
        fc.on_task_failed("agent-1")
        fc.on_task_arrived("agent-1")  # success resets
        assert fc.get_consecutive_failures("agent-1") == 0
        # One more failure doesn't trip
        fc.on_task_failed("agent-1")
        assert fc.is_circuit_tripped("agent-1") is False

    def test_auto_recovery_after_cooldown(self) -> None:
        fc = FlowController(FlowControlConfig(
            circuit_breaker_threshold=1,
            circuit_breaker_cooldown=0.01,  # very short cooldown
        ))
        fc.on_task_failed("agent-1")
        assert fc.is_circuit_tripped("agent-1") is True
        assert fc.can_dispatch("agent-1") is False
        # Wait for cooldown
        time.sleep(0.02)
        assert fc.can_dispatch("agent-1") is True  # auto-recovered
        assert fc.is_circuit_tripped("agent-1") is False

    def test_manual_reset(self) -> None:
        fc = FlowController(FlowControlConfig(circuit_breaker_threshold=1))
        fc.on_task_failed("agent-1")
        assert fc.is_circuit_tripped("agent-1") is True
        fc.reset_circuit("agent-1")
        assert fc.is_circuit_tripped("agent-1") is False
        assert fc.can_dispatch("agent-1") is True

    def test_remaining_cooldown(self) -> None:
        fc = FlowController(FlowControlConfig(
            circuit_breaker_threshold=1,
            circuit_breaker_cooldown=60,
        ))
        fc.on_task_failed("agent-1")
        remaining = fc.get_remaining_cooldown("agent-1")
        assert 55 <= remaining <= 60  # just tripped

    def test_remaining_cooldown_zero_when_not_tripped(self) -> None:
        fc = FlowController(FlowControlConfig())
        assert fc.get_remaining_cooldown("agent-1") == 0.0

    def test_reset_clears_all_state(self) -> None:
        fc = FlowController(FlowControlConfig(max_concurrent_tasks=1))
        fc.on_task_dispatched("agent-1")
        fc.on_task_arrived("agent-1")
        fc.on_task_failed("agent-2")
        fc.reset()
        assert fc.get_concurrent_count("agent-1") == 0
        assert fc.get_consecutive_failures("agent-2") == 0
        assert fc.can_dispatch("agent-1") is True


class TestFlowControllerRetryBackoff:
    """Retry backoff calculation."""

    def test_backoff_base(self) -> None:
        fc = FlowController(FlowControlConfig(retry_backoff_base=30))
        # First retry: 30s
        assert fc.get_retry_backoff(1) == 30.0

    def test_backoff_exponential(self) -> None:
        fc = FlowController(FlowControlConfig(
            retry_backoff_base=30,
        ))
        # 1st → 30, 2nd → 60, 3rd → 120
        assert fc.get_retry_backoff(1) == 30.0
        assert fc.get_retry_backoff(2) == 60.0
        assert fc.get_retry_backoff(3) == 120.0

    def test_backoff_capped(self) -> None:
        fc = FlowController(FlowControlConfig(
            retry_backoff_base=30,
            retry_backoff_max=100,
        ))
        # 3rd retry: 120, but capped at 100
        assert fc.get_retry_backoff(3) == 100.0

    def test_backoff_zero_base(self) -> None:
        fc = FlowController(FlowControlConfig(retry_backoff_base=0))
        assert fc.get_retry_backoff(5) == 0.0


# ===================================================================
# Dispatcher Integration Test Fixtures
# ===================================================================


@pytest.fixture
def db_path() -> Generator[str, None, None]:
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    try:
        yield path
    finally:
        if os.path.exists(path):
            os.unlink(path)


@pytest.fixture
def reg_store(db_path: str) -> Generator[RegistryStore, None, None]:
    with tempfile.TemporaryDirectory() as d:
        rs = RegistryStore(d)
        try:
            yield rs
        finally:
            rs.close()


@pytest.fixture
def store(db_path: str) -> Generator[TaskStore, None, None]:
    ts = TaskStore(db_path)
    try:
        yield ts
    finally:
        ts.close()


@pytest.fixture
def ws_mgr() -> Generator[WorkspaceManager, None, None]:
    with tempfile.TemporaryDirectory() as d:
        yield WorkspaceManager(str(Path(d) / "workspaces"))


@pytest.fixture
async def http_session() -> AsyncGenerator[aiohttp.ClientSession, None]:
    async with aiohttp.ClientSession() as session:
        yield session


# ===================================================================
# Dispatcher Flow Control Integration Tests
# ===================================================================


class TestDispatcherConcurrencyLimit:
    """Dispatcher should respect max_concurrent_tasks for WS dispatch."""

    async def test_ws_concurrent_limit_blocks_extra_tasks(
        self,
        store: TaskStore,
        ws_mgr: WorkspaceManager,
    ) -> None:
        """With max_concurrent_tasks=1, only one WS task is claimed per cycle."""
        ws_connections: dict[str, Any] = {}

        # Create a mock WebSocket connection
        class MockWS:
            closed = False
            async def send_json(self, msg: dict) -> None:
                pass

        ws_connections["test-agent"] = MockWS()

        config = DispatcherConfig(
            poll_interval=3600,
            claim_ttl=DEFAULT_CLAIM_TTL,
            failure_limit=3,
            dispatcher_id="test-concurrency",
            worker_command="echo",
            max_concurrent_tasks=1,
        )
        dispatcher = Dispatcher(
            store, ws_mgr, config,
            ws_connections=ws_connections,
        )

        # Create two ready tasks for the same agent
        t1 = store.create_task(title="task-1", assignee="test-agent")
        t2 = store.create_task(title="task-2", assignee="test-agent")
        assert t1.status == TaskStatus.READY.value
        assert t2.status == TaskStatus.READY.value

        stats = await dispatcher.trigger_poll_cycle()

        # Only 1 task should be claimed (max_concurrent_tasks=1)
        assert stats["tasks_claimed"] == 1, \
            f"Expected 1 claim, got {stats['tasks_claimed']}"

        refreshed_1 = store.get_task(t1.id)
        refreshed_2 = store.get_task(t2.id)
        assert refreshed_1 is not None
        assert refreshed_2 is not None

        # One should be running, the other still ready
        running_count = 0
        ready_count = 0
        for r in [refreshed_1, refreshed_2]:
            if r.status == TaskStatus.RUNNING.value:
                running_count += 1
            elif r.status == TaskStatus.READY.value:
                ready_count += 1
        assert running_count == 1, f"Expected 1 running, got {running_count}"
        assert ready_count == 1, f"Expected 1 ready (blocked by concurrency), got {ready_count}"

    async def test_ws_concurrent_limit_allows_up_to_max(
        self,
        store: TaskStore,
        ws_mgr: WorkspaceManager,
    ) -> None:
        """With max_concurrent_tasks=3, up to 3 tasks should be claimed."""
        ws_connections: dict[str, Any] = {}

        class MockWS:
            closed = False
            async def send_json(self, msg: dict) -> None:
                pass

        ws_connections["test-agent"] = MockWS()

        config = DispatcherConfig(
            poll_interval=3600,
            claim_ttl=DEFAULT_CLAIM_TTL,
            failure_limit=3,
            dispatcher_id="test-concurrency-3",
            worker_command="echo",
            max_concurrent_tasks=3,
        )
        dispatcher = Dispatcher(
            store, ws_mgr, config,
            ws_connections=ws_connections,
        )

        tasks = []
        for i in range(3):
            t = store.create_task(
                title=f"task-{i}", assignee="test-agent",
            )
            tasks.append(t)

        stats = await dispatcher.trigger_poll_cycle()
        assert stats["tasks_claimed"] == 3

        for t in tasks:
            r = store.get_task(t.id)
            assert r is not None
            assert r.status == TaskStatus.RUNNING.value


class TestDispatcherCallbackConcurrencyLimit:
    """Dispatcher should respect max_concurrent_tasks for callback dispatch."""

    async def test_callback_concurrent_limit(
        self,
        store: TaskStore,
        reg_store: RegistryStore,
        ws_mgr: WorkspaceManager,
        http_session: aiohttp.ClientSession,
    ) -> None:
        """Callback dispatch respects max_concurrent_tasks."""
        # Register callback agent
        agent_id = reg_store.register_agent({
            "name": "callback-agent-concurrency",
            "description": "Concurrency test agent",
            "preferred_channel": "callback",
            "callback_url": "http://localhost:19999/callback",
            "supported_interfaces": [{
                "url": "http://localhost:19999",
                "protocol_binding": "JSONRPC",
                "protocol_version": "1.0",
            }],
        })

        # Set up a callback server
        captured: list[dict] = []
        callback_app = web.Application()

        async def handle_callback(request: web.Request) -> web.Response:
            body = await request.json()
            captured.append(body)
            return web.json_response({"status": "ok"})

        callback_app.router.add_post("/callback", handle_callback)
        runner = web.AppRunner(callback_app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        real_url = f"http://127.0.0.1:{port}/callback"

        with reg_store._tx() as eng:
            eng.execute(
                "UPDATE agents SET callback_url=? WHERE id=?",
                (real_url, agent_id),
            )

        config = DispatcherConfig(
            poll_interval=3600,
            claim_ttl=DEFAULT_CLAIM_TTL,
            failure_limit=3,
            dispatcher_id="test-callback-concurrency",
            worker_command="echo",
            max_concurrent_tasks=1,
        )
        dispatcher = Dispatcher(
            store, ws_mgr, config,
            ws_connections={},
            registry_store=reg_store,
            http_session=http_session,
        )

        # Create two tasks
        t1 = store.create_task(title="cb-1", assignee=agent_id)
        t2 = store.create_task(title="cb-2", assignee=agent_id)

        stats = await dispatcher.trigger_poll_cycle()
        # Only 1 should be claimed
        assert stats["tasks_claimed"] == 1, \
            f"Expected 1 callback claim, got {stats['tasks_claimed']}"

        r1 = store.get_task(t1.id)
        r2 = store.get_task(t2.id)
        assert r1 is not None
        assert r2 is not None

        running = [r for r in [r1, r2] if r.status == TaskStatus.RUNNING.value]
        ready = [r for r in [r1, r2] if r.status == TaskStatus.READY.value]
        assert len(running) == 1, f"Expected 1 running, got {len(running)}"
        assert len(ready) == 1, f"Expected 1 ready (blocked), got {len(ready)}"

        # Second poll cycle should NOT claim another (first still running)
        stats2 = await dispatcher.trigger_poll_cycle()
        assert stats2["tasks_claimed"] == 0, \
            "Second cycle should claim nothing — agent already at limit"

        await runner.cleanup()


class TestDispatcherCircuitBreaker:
    """Dispatcher circuit breaker for WS dispatch."""

    async def test_circuit_breaker_blocks_after_consecutive_failures(
        self,
        store: TaskStore,
        ws_mgr: WorkspaceManager,
    ) -> None:
        """Consecutive WS send failures trip the circuit breaker."""
        ws_connections: dict[str, Any] = {}

        class FailingWS:
            closed = False
            call_count = 0

            async def send_json(self, msg: dict) -> None:
                self.call_count += 1
                raise ConnectionError(f"Simulated failure #{self.call_count}")

        failing_ws = FailingWS()
        ws_connections["broken-agent"] = failing_ws

        config = DispatcherConfig(
            poll_interval=3600,
            claim_ttl=DEFAULT_CLAIM_TTL,
            failure_limit=3,
            dispatcher_id="test-circuit-breaker",
            worker_command="echo",
            max_concurrent_tasks=5,
            circuit_breaker_threshold=2,  # trip after 2 failures
            circuit_breaker_cooldown=300,  # 5 min
        )
        dispatcher = Dispatcher(
            store, ws_mgr, config,
            ws_connections=ws_connections,
        )

        # Create tasks one at a time to trigger sequential failures
        t1 = store.create_task(title="fail-1", assignee="broken-agent")
        stats1 = await dispatcher.trigger_poll_cycle()
        assert stats1["tasks_claimed"] == 0  # claimed but failed due to WS error

        r1 = store.get_task(t1.id)
        assert r1 is not None
        assert r1.status == TaskStatus.FAILED.value

        # Second failure trips the circuit
        t2 = store.create_task(title="fail-2", assignee="broken-agent")
        stats2 = await dispatcher.trigger_poll_cycle()
        r2 = store.get_task(t2.id)
        assert r2 is not None
        assert r2.status == TaskStatus.FAILED.value

        # Circuit should be tripped now
        assert dispatcher._flow_control.is_circuit_tripped("broken-agent")

        # Third task should be blocked by circuit breaker
        t3 = store.create_task(title="fail-3", assignee="broken-agent")
        stats3 = await dispatcher.trigger_poll_cycle()
        r3 = store.get_task(t3.id)
        assert r3 is not None
        # Should still be READY (blocked by circuit breaker, never claimed)
        assert r3.status == TaskStatus.READY.value, \
            f"Third task should be READY (circuit blocked), got {r3.status}"


class TestDispatcherCircuitBreakerCallback:
    """Dispatcher circuit breaker for callback dispatch."""

    async def test_callback_circuit_breaker(
        self,
        store: TaskStore,
        reg_store: RegistryStore,
        ws_mgr: WorkspaceManager,
        http_session: aiohttp.ClientSession,
    ) -> None:
        """Consecutive callback failures trip the circuit breaker."""
        agent_id = reg_store.register_agent({
            "name": "callback-fail-agent",
            "description": "Failing callback agent",
            "preferred_channel": "callback",
            "callback_url": "http://localhost:19999/callback",
            "supported_interfaces": [{
                "url": "http://localhost:19999",
                "protocol_binding": "JSONRPC",
                "protocol_version": "1.0",
            }],
        })

        # Set up a callback server that always returns 500
        callback_app = web.Application()

        async def handle_fail(request: web.Request) -> web.Response:
            return web.json_response({"error": "internal"}, status=500)

        callback_app.router.add_post("/callback", handle_fail)
        runner = web.AppRunner(callback_app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        real_url = f"http://127.0.0.1:{port}/callback"

        with reg_store._tx() as eng:
            eng.execute(
                "UPDATE agents SET callback_url=? WHERE id=?",
                (real_url, agent_id),
            )

        config = DispatcherConfig(
            poll_interval=3600,
            claim_ttl=DEFAULT_CLAIM_TTL,
            failure_limit=3,
            dispatcher_id="test-cb-circuit",
            worker_command="echo",
            max_concurrent_tasks=5,
            circuit_breaker_threshold=2,  # trip after 2 failures
            circuit_breaker_cooldown=300,
        )
        dispatcher = Dispatcher(
            store, ws_mgr, config,
            ws_connections={},
            registry_store=reg_store,
            http_session=http_session,
        )

        # First task fails
        t1 = store.create_task(title="cb-fail-1", assignee=agent_id)
        await dispatcher.trigger_poll_cycle()
        r1 = store.get_task(t1.id)
        assert r1 is not None
        assert r1.status == TaskStatus.FAILED.value

        # Second failure trips circuit
        t2 = store.create_task(title="cb-fail-2", assignee=agent_id)
        await dispatcher.trigger_poll_cycle()
        r2 = store.get_task(t2.id)
        assert r2 is not None
        assert r2.status == TaskStatus.FAILED.value

        assert dispatcher._flow_control.is_circuit_tripped(agent_id)

        # Third task should be blocked
        t3 = store.create_task(title="cb-fail-3", assignee=agent_id)
        await dispatcher.trigger_poll_cycle()
        r3 = store.get_task(t3.id)
        assert r3 is not None
        assert r3.status == TaskStatus.READY.value, \
            f"Third callback task should be READY (circuit blocked), got {r3.status}"

        await runner.cleanup()


class TestDispatcherRetryBackoff:
    """Dispatcher sets next_retry_at on failure for backoff."""

    async def test_failure_sets_next_retry_at(
        self,
        store: TaskStore,
        ws_mgr: WorkspaceManager,
    ) -> None:
        """When a WS dispatch fails, next_retry_at should be set."""
        ws_connections: dict[str, Any] = {}

        class FailingWS:
            closed = False
            async def send_json(self, msg: dict) -> None:
                raise ConnectionError("Simulated failure")

        ws_connections["agent-retry"] = FailingWS()

        config = DispatcherConfig(
            poll_interval=3600,
            claim_ttl=DEFAULT_CLAIM_TTL,
            failure_limit=3,
            dispatcher_id="test-retry-backoff",
            worker_command="echo",
            max_concurrent_tasks=5,
            retry_backoff_base=30,
        )
        dispatcher = Dispatcher(
            store, ws_mgr, config,
            ws_connections=ws_connections,
        )

        task = store.create_task(
            title="retry-test", assignee="agent-retry",
            max_retries=3,
        )

        await dispatcher.trigger_poll_cycle()

        r = store.get_task(task.id)
        assert r is not None
        assert r.status == TaskStatus.FAILED.value
        assert r.next_retry_at is not None, \
            "next_retry_at should be set on failure"
        # Should be in the future (backoff is 30s for first failure)
        assert r.next_retry_at > int(time.time()) - 5, \
            f"next_retry_at ({r.next_retry_at}) should be in the future"


class TestFlowControlStats:
    """Poll cycle stats should include flow_blocked."""

    async def test_stats_shape_includes_flow_blocked(
        self,
        store: TaskStore,
        ws_mgr: WorkspaceManager,
    ) -> None:
        ws_connections: dict[str, Any] = {}

        class MockWS:
            closed = False
            async def send_json(self, msg: dict) -> None:
                pass

        ws_connections["agent"] = MockWS()

        config = DispatcherConfig(
            poll_interval=3600,
            max_concurrent_tasks=5,
        )
        dispatcher = Dispatcher(
            store, ws_mgr, config,
            ws_connections=ws_connections,
        )

        stats = await dispatcher.trigger_poll_cycle()
        assert "flow_blocked" in stats
        assert isinstance(stats["flow_blocked"], int)


class TestMaybeDispatchPendingFlowControl:
    """P3.2: _maybe_dispatch_pending should respect flow control."""

    async def test_flow_control_blocks_pending_dispatch(
        self,
        store: TaskStore,
    ) -> None:
        """When flow control says can_dispatch=False, pending stays blocked."""
        # Create a blocked task
        task = store.create_task(
            title="pending-task", assignee="agent-1",
        )
        store.update_task_status(task.id, TaskStatus.BLOCKED.value)

        # Flow controller at 1/1 — blocks any new dispatch
        fc = FlowController(FlowControlConfig(max_concurrent_tasks=1))
        fc.on_task_dispatched("agent-1")
        fc.on_task_arrived("agent-1")  # 1/1 — full

        class MockWS:
            closed = False
            def __init__(self):
                self.called = False

            async def send_json(self, msg: dict) -> None:
                self.called = True

        mock_ws = MockWS()

        from simple_a2a_registry.server import _maybe_dispatch_pending
        await _maybe_dispatch_pending(
            store, mock_ws, "agent-1", flow_control=fc,
        )
        # Task should still be BLOCKED
        r = store.get_task(task.id)
        assert r is not None
        assert r.status == TaskStatus.BLOCKED.value, \
            f"Expected BLOCKED, got {r.status}"
        assert mock_ws.called is False, "WS.send_json should not have been called"

    async def test_flow_control_allows_under_limit(
        self,
        store: TaskStore,
    ) -> None:
        """When flow control allows, pending tasks should be dispatched."""
        task = store.create_task(
            title="pending-task", assignee="agent-1",
        )
        store.update_task_status(task.id, TaskStatus.BLOCKED.value)

        fc = FlowController(FlowControlConfig(max_concurrent_tasks=5))

        class MockWSOK:
            closed = False
            def __init__(self):
                self.sent = None

            async def send_json(self, msg: dict) -> None:
                self.sent = msg

        mock_ws = MockWSOK()
        dispatched_tasks: dict[str, str] = {}

        from simple_a2a_registry.server import _maybe_dispatch_pending
        await _maybe_dispatch_pending(
            store, mock_ws, "agent-1",
            dispatched_ws_tasks=dispatched_tasks,
            flow_control=fc,
        )
        # Task should now be RUNNING
        r = store.get_task(task.id)
        assert r is not None
        assert r.status == TaskStatus.RUNNING.value, \
            f"Expected RUNNING, got {r.status}"
        assert mock_ws.sent is not None
        assert mock_ws.sent.get("id") == task.id
        # Flow control should have tracked the dispatch
        assert fc.get_concurrent_count("agent-1") == 1
        # dispatched_tasks should have the entry
        assert task.id in dispatched_tasks

    async def test_flow_control_notified_on_failure(
        self,
        store: TaskStore,
    ) -> None:
        """When re-dispatch fails, flow control gets on_task_failed."""
        task = store.create_task(
            title="pending-task", assignee="agent-1",
        )
        store.update_task_status(task.id, TaskStatus.BLOCKED.value)

        fc = FlowController(FlowControlConfig(max_concurrent_tasks=5))

        class FailingWS:
            closed = False
            call_count = 0

            async def send_json(self, msg: dict) -> None:
                self.call_count += 1
                raise ConnectionError(f"Fail #{self.call_count}")

        fail_ws = FailingWS()

        from simple_a2a_registry.server import _maybe_dispatch_pending
        await _maybe_dispatch_pending(
            store, fail_ws, "agent-1", flow_control=fc,
        )
        # Flow control should have recorded the failure + released permit
        assert fc.get_consecutive_failures("agent-1") == 1
        assert fc.get_concurrent_count("agent-1") == 0


class TestMaybeUpdateKanbanFlowControl:
    """P3.2: _maybe_update_kanban should notify FlowController."""

    def test_completion_notifies_flow_control(
        self,
        store: TaskStore,
    ) -> None:
        """When a task completes, flow control gets on_task_completed + departed."""
        task = store.create_task(
            title="test", assignee="agent-1",
        )
        store.update_task_status(task.id, TaskStatus.RUNNING.value)

        fc = FlowController(FlowControlConfig(max_concurrent_tasks=3))
        fc.on_task_dispatched("agent-1")  # permit taken by dispatch
        fc.on_task_arrived("agent-1")     # arrived
        assert fc.get_concurrent_count("agent-1") == 1

        dispatched: dict[str, str] = {task.id: "agent-1"}

        from simple_a2a_registry.server import _maybe_update_kanban
        _maybe_update_kanban(
            store, dispatched, task.id,
            "completed", result="ok", error=None,
            flow_control=fc,
        )
        # Flow control should have released the permit
        assert fc.get_concurrent_count("agent-1") == 0, \
            f"Expected concurrent=0 after completion, got {fc.get_concurrent_count('agent-1')}"
        assert fc.get_consecutive_failures("agent-1") == 0

    def test_failure_notifies_flow_control(
        self,
        store: TaskStore,
    ) -> None:
        """When a task fails, flow control gets on_task_failed + departed."""
        task = store.create_task(
            title="test", assignee="agent-1",
        )
        store.update_task_status(task.id, TaskStatus.RUNNING.value)

        fc = FlowController(FlowControlConfig(max_concurrent_tasks=3))
        fc.on_task_dispatched("agent-1")
        fc.on_task_arrived("agent-1")
        assert fc.get_concurrent_count("agent-1") == 1

        dispatched: dict[str, str] = {task.id: "agent-1"}

        from simple_a2a_registry.server import _maybe_update_kanban
        _maybe_update_kanban(
            store, dispatched, task.id,
            "failed", result=None, error="test error",
            flow_control=fc,
        )
        # Flow control should have released the permit + recorded failure
        assert fc.get_concurrent_count("agent-1") == 0
        assert fc.get_consecutive_failures("agent-1") == 1

    def test_unknown_task_skips_flow_control(
        self,
        store: TaskStore,
    ) -> None:
        """When a task is not in dispatched_tasks, flow_control is skipped."""
        fc = FlowController(FlowControlConfig(max_concurrent_tasks=3))
        dispatched: dict[str, str] = {"other-task": "agent-1"}

        from simple_a2a_registry.server import _maybe_update_kanban
        _maybe_update_kanban(
            store, dispatched, "unknown-task",
            "completed", result="ok", error=None,
            flow_control=fc,
        )
        # Flow control should be untouched
        assert fc.get_concurrent_count("agent-1") == 0


class TestReconcileTaskStoreFlowControl:
    """P3.2: _reconcile_task_store should notify FlowController."""

    def test_completion_notifies_flow_control(
        self,
        store: TaskStore,
    ) -> None:
        """When a task completes via WS, flow control gets notified."""
        task = store.create_task(
            title="test", assignee="agent-1",
        )
        store.update_task_status(task.id, TaskStatus.RUNNING.value)

        fc = FlowController(FlowControlConfig(max_concurrent_tasks=3))
        fc.on_task_dispatched("agent-1")
        fc.on_task_arrived("agent-1")
        assert fc.get_concurrent_count("agent-1") == 1

        dispatched: dict[str, str] = {task.id: "agent-1"}

        from simple_a2a_registry.registry_handler import _reconcile_task_store
        _reconcile_task_store(
            store, dispatched, task.id,
            TaskStatus.COMPLETED.value,
            result="ok",
            flow_control=fc,
        )
        # Permit released
        assert fc.get_concurrent_count("agent-1") == 0

    def test_failure_notifies_flow_control(
        self,
        store: TaskStore,
    ) -> None:
        """When a task fails via WS, flow control gets notified."""
        task = store.create_task(
            title="test", assignee="agent-1",
        )
        store.update_task_status(task.id, TaskStatus.RUNNING.value)

        fc = FlowController(FlowControlConfig(max_concurrent_tasks=3))
        fc.on_task_dispatched("agent-1")
        fc.on_task_arrived("agent-1")
        assert fc.get_concurrent_count("agent-1") == 1

        dispatched: dict[str, str] = {task.id: "agent-1"}

        from simple_a2a_registry.registry_handler import _reconcile_task_store
        _reconcile_task_store(
            store, dispatched, task.id,
            TaskStatus.FAILED.value,
            error="test error",
            flow_control=fc,
        )
        # Permit released + failure recorded
        assert fc.get_concurrent_count("agent-1") == 0
        assert fc.get_consecutive_failures("agent-1") == 1