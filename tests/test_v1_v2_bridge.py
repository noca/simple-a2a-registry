"""Tests for the V1/V2 bridge (P2.5).

Verifies that V2-dispatched kanban tasks create and update entries in
the V1 _tasks dict so they appear on GET /v1/tasks.
"""
from __future__ import annotations

import os
import tempfile
import time

import pytest
from aiohttp.test_utils import TestServer, TestClient

from simple_a2a_registry.orchestration.dispatcher import (
    Dispatcher,
    DispatcherConfig,
)
from simple_a2a_registry.orchestration.models import (
    Task,
    TaskStatus,
)
from simple_a2a_registry.orchestration.store import TaskStore, DEFAULT_CLAIM_TTL
from simple_a2a_registry.orchestration.workspace import WorkspaceManager
from simple_a2a_registry.server import create_app, _maybe_update_kanban


# ---------------------------------------------------------------------------
# Synchronous test helper
# ---------------------------------------------------------------------------


def _make_store_and_disp(v1_tasks=None):
    """Create a TaskStore + Dispatcher backed by a tempfile.

    Returns (store, dispatcher, db_path).  Caller must close the store
    and clean up the db_path.
    """
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    store = TaskStore(db_path)
    ws_mgr = WorkspaceManager(tempfile.mkdtemp())
    config = DispatcherConfig(
        poll_interval=3600,
        claim_ttl=DEFAULT_CLAIM_TTL,
        failure_limit=3,
        dispatcher_id="test-bridge",
        worker_command="echo",
    )
    disp = Dispatcher(store, ws_mgr, config, v1_tasks=v1_tasks)
    return store, disp, db_path


# ---------------------------------------------------------------------------
# Fixtures for async API tests
# ---------------------------------------------------------------------------


@pytest.fixture
def api_client():
    """Create a fresh app+client for each test, backed by a temp dir."""
    factories = []

    async def maker():
        tmpdir_obj = tempfile.TemporaryDirectory()
        factories.append(tmpdir_obj)
        data_dir = tmpdir_obj.name
        app = create_app(
            data_dir=data_dir,
            base_url="http://localhost:8321",
        )
        server = TestServer(app)
        await server.start_server()
        client = TestClient(server)
        return client

    yield maker

    for f in factories:
        try:
            f.cleanup()
        except Exception:
            pass


# ===================================================================
# Test: Dispatcher bridge helpers (synchronous)
# ===================================================================


class TestDispatcherV1BridgeSync:
    """Test that the Dispatcher bridge helpers correctly populate the
    V1 _tasks dict when V2 tasks are dispatched.

    These are synchronous tests — they don't need an aiohttp app.
    """

    def test_create_v1_entry_creates_entry(self):
        """_create_v1_entry should create a well-formed V1 dict entry."""
        v1_tasks: dict = {}
        store, disp, db_path = _make_store_and_disp(v1_tasks=v1_tasks)
        try:
            task = store.create_task(
                title="bridge test",
                assignee="agent-a",
                body="body text",
                tenant="t1",
            )

            assert task.id not in v1_tasks
            disp._create_v1_entry(task)
            assert task.id in v1_tasks

            entry = v1_tasks[task.id]
            assert entry["id"] == task.id
            assert entry["agent_id"] == "agent-a"
            assert entry["title"] == "bridge test"
            assert entry["body"] == "body text"
            assert entry["state"] == "dispatched"
            assert entry["result"] is None
            assert entry["error"] is None
            assert entry["tenant"] == "t1"
            assert "created_at" in entry
            assert "updated_at" in entry
        finally:
            store.close()
            os.unlink(db_path)

    def test_create_v1_entry_noop_if_none_wired(self):
        """_create_v1_entry should be a no-op when v1_tasks is None."""
        store, disp, db_path = _make_store_and_disp(v1_tasks=None)
        try:
            task = store.create_task(title="noop", assignee="agent-a")
            disp._create_v1_entry(task)  # should not raise
        finally:
            store.close()
            os.unlink(db_path)

    def test_create_v1_entry_noop_if_already_exists(self):
        """_create_v1_entry should not overwrite an existing entry."""
        v1_tasks: dict = {}
        store, disp, db_path = _make_store_and_disp(v1_tasks=v1_tasks)
        try:
            task = store.create_task(title="dup", assignee="agent-a")

            disp._create_v1_entry(task)
            original_ts = v1_tasks[task.id]["updated_at"]

            time.sleep(0.01)
            disp._create_v1_entry(task)
            assert v1_tasks[task.id]["updated_at"] == original_ts
        finally:
            store.close()
            os.unlink(db_path)

    def test_update_v1_entry_updates(self):
        """_update_v1_entry should update state and error."""
        v1_tasks: dict = {}
        store, disp, db_path = _make_store_and_disp(v1_tasks=v1_tasks)
        try:
            task = store.create_task(title="update", assignee="agent-a")

            disp._create_v1_entry(task)
            assert v1_tasks[task.id]["state"] == "dispatched"

            disp._update_v1_entry(task.id, "completed")
            assert v1_tasks[task.id]["state"] == "completed"
            assert v1_tasks[task.id]["error"] is None

            disp._update_v1_entry(task.id, "failed", error="oops")
            assert v1_tasks[task.id]["state"] == "failed"
            assert v1_tasks[task.id]["error"] == "oops"
        finally:
            store.close()
            os.unlink(db_path)

    def test_update_v1_entry_noop_if_no_v1_tasks(self):
        """_update_v1_entry should be a no-op when no dict is wired."""
        store, disp, db_path = _make_store_and_disp(v1_tasks=None)
        try:
            disp._update_v1_entry("nonexistent", "completed")
        finally:
            store.close()
            os.unlink(db_path)

    def test_update_v1_entry_noop_on_missing_entry(self):
        """_update_v1_entry should be a no-op for unknown task IDs."""
        v1_tasks: dict = {}
        store, disp, db_path = _make_store_and_disp(v1_tasks=v1_tasks)
        try:
            disp._update_v1_entry("nonexistent", "completed")
        finally:
            store.close()
            os.unlink(db_path)


# ===================================================================
# Test: _maybe_update_kanban V1 bridge (synchronous)
# ===================================================================


class TestMaybeUpdateKanbanV1BridgeSync:
    """Test that _maybe_update_kanban updates V1 entries."""

    def test_updates_v1_on_completed(self):
        """_maybe_update_kanban should mark V1 entry as completed."""
        v1_tasks: dict = {}
        store, disp, db_path = _make_store_and_disp(v1_tasks=v1_tasks)
        try:
            task = store.create_task(title="complete", assignee="agent-a")
            disp._create_v1_entry(task)

            # Ensure dispatched_tasks knows about it
            disp._dispatched_ws_tasks[task.id] = "agent-a"

            _maybe_update_kanban(
                task_store=store,
                dispatched_tasks=disp._dispatched_ws_tasks,
                task_id=task.id,
                status="completed",
                result="ok",
                error=None,
                v1_tasks=v1_tasks,
            )

            assert v1_tasks[task.id]["state"] == "completed"
            assert v1_tasks[task.id]["result"] == "ok"
        finally:
            store.close()
            os.unlink(db_path)

    def test_updates_v1_on_failed(self):
        """_maybe_update_kanban should mark V1 entry as failed."""
        v1_tasks: dict = {}
        store, disp, db_path = _make_store_and_disp(v1_tasks=v1_tasks)
        try:
            task = store.create_task(title="fail", assignee="agent-a")
            disp._create_v1_entry(task)
            disp._dispatched_ws_tasks[task.id] = "agent-a"

            _maybe_update_kanban(
                task_store=store,
                dispatched_tasks=disp._dispatched_ws_tasks,
                task_id=task.id,
                status="failed",
                result=None,
                error="something broke",
                v1_tasks=v1_tasks,
            )

            assert v1_tasks[task.id]["state"] == "failed"
            assert v1_tasks[task.id]["error"] == "something broke"
        finally:
            store.close()
            os.unlink(db_path)

    def test_noop_if_not_in_dispatched_tasks(self):
        """_maybe_update_kanban should not update V1 if not in dispatched."""
        v1_tasks: dict = {}
        store, disp, db_path = _make_store_and_disp(v1_tasks=v1_tasks)
        try:
            task = store.create_task(title="noop", assignee="agent-a")
            disp._create_v1_entry(task)
            # deliberately NOT adding to dispatched_ws_tasks

            _maybe_update_kanban(
                task_store=store,
                dispatched_tasks=disp._dispatched_ws_tasks,
                task_id=task.id,
                status="completed",
                result="ok",
                error=None,
                v1_tasks=v1_tasks,
            )

            # V1 entry should remain "dispatched"
            assert v1_tasks[task.id]["state"] == "dispatched"
        finally:
            store.close()
            os.unlink(db_path)


# ===================================================================
# Test: End-to-end - V1 entry appears via the API
# ===================================================================


class TestV1V2BridgeAPI:
    """Test that V2 task lifecycle is visible via V1 /v1/tasks endpoint."""

    @pytest.mark.asyncio
    async def test_v2_task_not_on_v1_until_dispatched(self, api_client):
        """A newly created V2 task should NOT appear on /v1/tasks until
        the Dispatcher creates the V1 bridge entry."""
        async with await api_client() as client:
            resp = await client.post("/v2/tasks", json={
                "title": "bridge api test",
                "assignee": "coder",
            })
            assert resp.status == 201
            data = await resp.json()
            task_id = data["task"]["id"]

            v1_resp = await client.get("/v1/tasks")
            assert v1_resp.status == 200
            v1_data = await v1_resp.json()
            v1_ids = [t["id"] for t in v1_data.get("tasks", [])]
            assert task_id not in v1_ids, (
                f"Task {task_id} should not appear in V1 until dispatched"
            )

    @pytest.mark.asyncio
    async def test_v2_task_visible_on_v1_after_dispatch(self, api_client):
        """Simulate the Dispatcher creating a V1 entry after dispatching."""
        async with await api_client() as client:
            resp = await client.post("/v2/tasks", json={
                "title": "dispatch test",
                "assignee": "coder",
            })
            assert resp.status == 201
            data = await resp.json()
            task_id = data["task"]["id"]
            task = data["task"]

            app = client.server.app
            disp: Dispatcher = app["dispatcher"]
            assert disp is not None

            t = Task(
                id=task_id,
                title=task["title"],
                body=task.get("body", ""),
                assignee=task["assignee"],
                priority=task.get("priority", 0),
                tenant=task.get("tenant", ""),
                status=TaskStatus.READY.value,
            )
            disp._create_v1_entry(t)

            v1_resp = await client.get("/v1/tasks")
            assert v1_resp.status == 200
            v1_data = await v1_resp.json()
            v1_ids = [t["id"] for t in v1_data.get("tasks", [])]
            assert task_id in v1_ids, (
                f"Task {task_id} should appear in V1 after dispatch"
            )

    @pytest.mark.asyncio
    async def test_v2_task_updated_v1_on_completion(self, api_client):
        """When a V2 task completes, its V1 entry should be updated."""
        async with await api_client() as client:
            resp = await client.post("/v2/tasks", json={
                "title": "complete test",
                "assignee": "coder",
            })
            assert resp.status == 201
            data = await resp.json()
            task_id = data["task"]["id"]
            task = data["task"]

            app = client.server.app
            disp: Dispatcher = app["dispatcher"]
            handler = app["handler"]

            t = Task(
                id=task_id,
                title=task["title"],
                body=task.get("body", ""),
                assignee=task["assignee"],
                priority=task.get("priority", 0),
                tenant=task.get("tenant", ""),
                status=TaskStatus.READY.value,
            )
            disp._create_v1_entry(t)

            # Track in dispatched_tasks so _maybe_update_kanban can reconcile
            disp._dispatched_ws_tasks[task_id] = "coder"

            # Verify V1 shows "dispatched"
            v1_resp = await client.get(f"/v1/tasks/{task_id}")
            assert v1_resp.status == 200
            v1_entry = await v1_resp.json()
            assert v1_entry["state"] == "dispatched"

            # Simulate task completion via _maybe_update_kanban
            _maybe_update_kanban(
                task_store=app["task_store"],
                dispatched_tasks=disp._dispatched_ws_tasks,
                task_id=task_id,
                status="completed",
                result='{"output": "done"}',
                error=None,
                v1_tasks=handler._tasks,
            )

            v1_resp2 = await client.get(f"/v1/tasks/{task_id}")
            assert v1_resp2.status == 200
            v1_entry2 = await v1_resp2.json()
            assert v1_entry2["state"] == "completed"
            assert v1_entry2["result"] == '{"output": "done"}'