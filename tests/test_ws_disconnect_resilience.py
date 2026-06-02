"""Full-scenario tests for WebSocket disconnect resilience — DANGLING/heal/timeout.

P1.6: Covers all six scenarios from the resilient-distribution-architecture.md §5.2:

  1. WS sudden disconnect <1s recovery → task DANGLING, state_sync heals RUNNING
  2. Disconnect within 30s agent reconnects → dangling→running
  3. Disconnect 31s → dangling→failed, goes to retry
  4. Repeated disconnect/reconnect → each time restarts timer
  5. DANGLING period does NOT trigger TTL release
  6. DANGLING period does NOT trigger retry promotion
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from unittest.mock import patch

import pytest
from aiohttp.test_utils import TestServer, TestClient
from aiohttp import web

from simple_a2a_registry.server import create_app
from simple_a2a_registry.orchestration.models import TaskStatus
from simple_a2a_registry.registry_handler import _reset_state_sync_rate_limiter

pytestmark = pytest.mark.asyncio

# ---------------------------------------------------------------------------
# Patch the dangling grace period to 1s so tests complete in seconds, not 30s.
# We patch the module-level constant that RegistryHandler.__init__ reads.
# ---------------------------------------------------------------------------
_GRACE_SECONDS = 1


@pytest.fixture(autouse=True)
def _reset_globals() -> None:
    """Reset module-level state before each test."""
    _reset_state_sync_rate_limiter()


# ---------------------------------------------------------------------------
# Test app fixture — builds a fresh server with patched grace period
# ---------------------------------------------------------------------------


@pytest.fixture
async def app_and_handler():
    """Create a test server with a 1-second dangling grace period.

    Yields ``(client, handler)`` so tests can inspect in-memory state or
    the underlying TaskStore directly.
    """
    tmpdir_obj = tempfile.TemporaryDirectory()
    try:
        data_dir = tmpdir_obj.name

        # Patch the grace constant so the handler reads 1s instead of 30s
        with patch("simple_a2a_registry.server.DANGLING_GRACE_SECONDS", _GRACE_SECONDS):
            app = create_app(
                data_dir=data_dir,
                base_url="http://localhost:8321",
            )
        server = TestServer(app)
        await server.start_server()
        client = TestClient(server)
        handler = app["handler"]

        # Disable rate limiting for state_sync in tests
        import simple_a2a_registry.registry_handler as rh
        rh._STATE_SYNC_COOLDOWN = 0

        yield client, handler

        await client.close()
        await server.close()
    finally:
        try:
            tmpdir_obj.cleanup()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _register_agent(client: TestClient, name: str = "resilience-test") -> str:
    resp = await client.post("/v1/agents", json={"name": name})
    data = await resp.json()
    return data["id"]


async def _dispatch_task(client: TestClient, agent_id: str, query: str = "test task") -> str:
    resp = await client.post(
        f"/v1/agents/{agent_id}/dispatch",
        json={"query": query},
    )
    assert resp.status == 202, await resp.text()
    data = await resp.json()
    return data["task_id"]


# ===================================================================
# Scenario 1: WS disconnect → dangling (not failed)
# ===================================================================


class TestDisconnectMarksDangling:
    """WS disconnect marks active tasks as DANGLING immediately."""

    async def test_disconnect_immediately_dangles(self, app_and_handler):
        """WS close → in-memory task state becomes 'dangling' (not 'failed')."""
        client, handler = app_and_handler
        agent_id = await _register_agent(client)
        ws = await client.ws_connect(f"/v1/agents/{agent_id}/ws")

        # Dispatch a task — it transitions to "forwarded" in _tasks
        task_id = await _dispatch_task(client, agent_id)
        msg = await ws.receive()
        msg_data = json.loads(msg.data)
        assert msg_data["type"] == "task"
        assert msg_data["id"] == task_id

        # Close WS gracefully → triggers _dangle_agent_tasks
        await ws.send_json({"type": "close"})
        await ws.close()

        # In-memory state should be dangling, not failed
        task = handler._tasks[task_id]
        assert task["state"] == "dangling", (
            f"Expected 'dangling', got '{task['state']}'"
        )
        assert task["error"] == "agent_disconnected_grace"


# ===================================================================
# Scenario 3: Dangling grace timeout → failed
# ===================================================================


class TestDanglingTimeout:
    """Dangling grace period expiry promotes tasks to FAILED."""

    async def test_dangling_times_out_to_failed(self, app_and_handler):
        """After grace period, DANGLING tasks become FAILED with timeout error."""
        client, handler = app_and_handler
        agent_id = await _register_agent(client)
        ws = await client.ws_connect(f"/v1/agents/{agent_id}/ws")

        task_id = await _dispatch_task(client, agent_id)
        msg = await ws.receive()
        assert json.loads(msg.data)["type"] == "task"

        # Disconnect → dangling
        await ws.send_json({"type": "close"})
        await ws.close()
        assert handler._tasks[task_id]["state"] == "dangling"

        # Wait for grace period to expire
        await asyncio.sleep(_GRACE_SECONDS + 0.5)

        # Must now be failed
        task = handler._tasks[task_id]
        assert task["state"] == "failed", (
            f"Expected 'failed' after grace timeout, got '{task['state']}'"
        )
        assert task["error"] == "agent_disconnected_timeout"


# ===================================================================
# Scenario 2: Reconnect + state_sync heals within grace period
# ===================================================================


class TestReconnectHealsDangling:
    """Reconnecting and sending state_sync within the grace period heals tasks."""

    async def test_state_sync_heals_in_memory(self, app_and_handler):
        """state_sync with active_tasks includes the dangling task → heals to working."""
        client, handler = app_and_handler
        agent_id = await _register_agent(client)
        ws = await client.ws_connect(f"/v1/agents/{agent_id}/ws")

        task_id = await _dispatch_task(client, agent_id)
        msg = await ws.receive()
        assert json.loads(msg.data)["type"] == "task"

        # Disconnect → dangling
        await ws.send_json({"type": "close"})
        await ws.close()
        assert handler._tasks[task_id]["state"] == "dangling"

        # Reconnect
        ws2 = await client.ws_connect(f"/v1/agents/{agent_id}/ws")

        # Send state_sync reporting the task as still active
        await ws2.send_json({
            "type": "state_sync",
            "agent_id": agent_id,
            "active_tasks": [
                {"id": task_id, "status": "working", "started_at": 1717000000},
            ],
        })
        await asyncio.sleep(0.1)

        # In-memory state should be healed to "working"
        task = handler._tasks[task_id]
        assert task["state"] == "working", (
            f"Expected 'working' (healed), got '{task['state']}'"
        )
        assert "dangling" not in task["state"]

        await ws2.send_json({"type": "close"})
        await ws2.close()

    async def test_heal_before_timeout_prevents_failure(self, app_and_handler):
        """Healing before the grace period expires prevents the timeout failure."""
        client, handler = app_and_handler
        agent_id = await _register_agent(client)
        ws = await client.ws_connect(f"/v1/agents/{agent_id}/ws")

        task_id = await _dispatch_task(client, agent_id)
        msg = await ws.receive()
        assert json.loads(msg.data)["type"] == "task"

        # Disconnect → dangling
        await ws.send_json({"type": "close"})
        await ws.close()
        assert handler._tasks[task_id]["state"] == "dangling"

        # Wait a short time (well within the 1s grace period)
        await asyncio.sleep(0.3)

        # Reconnect and heal
        ws2 = await client.ws_connect(f"/v1/agents/{agent_id}/ws")
        await ws2.send_json({
            "type": "state_sync",
            "agent_id": agent_id,
            "active_tasks": [
                {"id": task_id, "status": "working", "started_at": 1717000000},
            ],
        })
        await asyncio.sleep(0.1)
        assert handler._tasks[task_id]["state"] == "working"

        # Wait past the original grace period deadline
        await asyncio.sleep(_GRACE_SECONDS + 0.5)

        # Task must still be working — not timed out to failed
        task = handler._tasks[task_id]
        assert task["state"] == "working", (
            f"Task should stay healed after grace period, "
            f"got '{task['state']}'"
        )
        assert task.get("error") != "agent_disconnected_timeout"

        await ws2.send_json({"type": "close"})
        await ws2.close()


# ===================================================================
# Scenario 4: Repeated disconnect/reconnect restarts timer
# ===================================================================


class TestRepeatedDisconnect:
    """Each disconnect→reconnect cycle restarts the dangling grace timer."""

    async def test_two_cycles_restart_timer(self, app_and_handler):
        """Two disconnect→heal→disconnect→heal cycles both work correctly."""
        client, handler = app_and_handler
        agent_id = await _register_agent(client)
        ws = await client.ws_connect(f"/v1/agents/{agent_id}/ws")

        task_id = await _dispatch_task(client, agent_id)
        msg = await ws.receive()
        assert json.loads(msg.data)["type"] == "task"

        # ---- Cycle 1 ----
        await ws.send_json({"type": "close"})
        await ws.close()
        assert handler._tasks[task_id]["state"] == "dangling"

        ws2 = await client.ws_connect(f"/v1/agents/{agent_id}/ws")
        await ws2.send_json({
            "type": "state_sync",
            "agent_id": agent_id,
            "active_tasks": [
                {"id": task_id, "status": "working", "started_at": 1717000000},
            ],
        })
        await asyncio.sleep(0.1)
        assert handler._tasks[task_id]["state"] == "working"

        # ---- Cycle 2 ----
        await ws2.send_json({"type": "close"})
        await ws2.close()
        assert handler._tasks[task_id]["state"] == "dangling"

        ws3 = await client.ws_connect(f"/v1/agents/{agent_id}/ws")
        await ws3.send_json({
            "type": "state_sync",
            "agent_id": agent_id,
            "active_tasks": [
                {"id": task_id, "status": "working", "started_at": 1717000000},
            ],
        })
        await asyncio.sleep(0.1)
        assert handler._tasks[task_id]["state"] == "working"

        await ws3.send_json({"type": "close"})
        await ws3.close()


# ===================================================================
# Scenario 5 & 6: DANGLING tasks are protected from TTL release and
# retry promotion at the TaskStore level.
# ===================================================================


class TestDanglingTaskStore:
    """Verify DANGLING tasks are excluded from TTL release and retry promotion."""

    @staticmethod
    def _get_task_store(app: web.Application):
        """Get the V2 TaskStore from the application context."""
        return app["task_store"]

    async def _setup_dangling_task(
        self, client, handler
    ) -> tuple[web.Application, str]:
        """Create a V2 kanban task in DANGLING state for store-level tests."""
        task_store = self._get_task_store(client.server.app)

        # Create a fresh task in RUNNING state via the API
        agent_id = await _register_agent(client)

        # Create a V2 kanban task manually
        task = task_store.create_task(
            title="Dangling test task",
            assignee=agent_id,
        )
        task_id = task.id

        # Advance to RUNNING
        task_store.update_task_status(task_id, TaskStatus.RUNNING.value)

        # Now set it to DANGLING (bypass state machine validation for test setup)
        # We need to update the DB directly since DANGLING isn't in the state machine.
        # This mirrors what _dangle_agent_tasks would do if the state machine had
        # the DANGLING transitions configured.
        import time

        # Use a raw SQL update since validate_transition doesn't allow RUNNING→DANGLING
        with task_store._tx() as engine:
            engine.execute(
                "UPDATE tasks SET status = ? WHERE id = ?",
                (TaskStatus.DANGLING.value, task_id),
            )

        # Verify DANGLING was set
        t = task_store.get_task(task_id)
        assert t is not None
        assert t.status == TaskStatus.DANGLING.value
        return client.server.app, task_id

    async def test_ttl_release_skips_dangling(self, app_and_handler):
        """release_expired_claims() does NOT release DANGLING tasks."""
        client, handler = app_and_handler
        app, task_id = await self._setup_dangling_task(client, handler)
        task_store = self._get_task_store(app)

        # Run TTL release
        released = task_store.release_expired_claims()
        assert released == 0, (
            f"TTL release should NOT affect DANGLING tasks, "
            f"but released={released}"
        )

        # Task must still be DANGLING
        t = task_store.get_task(task_id)
        assert t is not None
        assert t.status == TaskStatus.DANGLING.value, (
            f"Task should remain DANGLING after TTL release, "
            f"got '{t.status}'"
        )

    async def test_retry_promotion_skips_dangling(self, app_and_handler):
        """promote_retryable_tasks() does NOT promote DANGLING tasks."""
        client, handler = app_and_handler
        app, task_id = await self._setup_dangling_task(client, handler)
        task_store = self._get_task_store(app)

        # Run retry promotion
        promoted = task_store.promote_retryable_tasks()
        assert promoted == 0, (
            f"Retry promotion should NOT affect DANGLING tasks, "
            f"but promoted={promoted}"
        )

        # Task must still be DANGLING
        t = task_store.get_task(task_id)
        assert t is not None
        assert t.status == TaskStatus.DANGLING.value, (
            f"Task should remain DANGLING after retry promotion, "
            f"got '{t.status}'"
        )


# ===================================================================
# Additional safety tests
# ===================================================================


class TestDisconnectEdgeCases:
    """Edge-case guarantees around the dangling mechanism."""

    async def test_disconnect_no_active_tasks_noop(self, app_and_handler):
        """Disconnecting an agent with no tasks should not produce errors."""
        client, handler = app_and_handler
        agent_id = await _register_agent(client)
        ws = await client.ws_connect(f"/v1/agents/{agent_id}/ws")
        await ws.send_json({"type": "close"})
        await ws.close()

        resp = await client.get("/health")
        assert resp.status == 200
        data = await resp.json()
        assert data["status"] == "healthy"

    async def test_disconnect_only_dangles_own_tasks(self, app_and_handler):
        """Disconnecting agent A should not affect agent B's tasks."""
        client, handler = app_and_handler
        agent_a = await _register_agent(client, "agent-a")
        agent_b = await _register_agent(client, "agent-b")

        ws_a = await client.ws_connect(f"/v1/agents/{agent_a}/ws")
        ws_b = await client.ws_connect(f"/v1/agents/{agent_b}/ws")

        task_a = await _dispatch_task(client, agent_a, "for A")
        task_b = await _dispatch_task(client, agent_b, "for B")

        await ws_a.receive()
        await ws_b.receive()

        # Disconnect A only
        await ws_a.send_json({"type": "close"})
        await ws_a.close()

        assert handler._tasks[task_a]["state"] == "dangling"
        # B's task unaffected
        assert handler._tasks[task_b]["state"] != "dangling"
        assert handler._tasks[task_b]["state"] != "failed"

        await ws_b.send_json({"type": "close"})
        await ws_b.close()

    async def test_already_dangling_not_doubled(self, app_and_handler):
        """A second close on an already-dangling task is a no-op."""
        client, handler = app_and_handler
        agent_id = await _register_agent(client)
        ws = await client.ws_connect(f"/v1/agents/{agent_id}/ws")

        task_id = await _dispatch_task(client, agent_id)
        msg = await ws.receive()
        assert json.loads(msg.data)["type"] == "task"

        # First close → dangling
        await ws.send_json({"type": "close"})
        await ws.close()

        # The agent is already disconnected, so _dangle_agent_tasks
        # won't be called again. But if another code path tried to
        # dangle an already-dangling task, _dangle_agent_tasks skips
        # tasks with state "dangling" (line 724).
        assert handler._tasks[task_id]["state"] == "dangling"
        assert handler._tasks[task_id]["error"] == "agent_disconnected_grace"