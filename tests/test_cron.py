"""Tests for the Cron Scheduled Task Scheduler — cron.py + cron_routes.py.

Covers:
- CronTaskStore CRUD (cron_tasks table)
- CronScheduler logic (_load_due, _fire, tick)
- CronExecution recording
- Cron expression parsing (compute_next_run)
- REST API endpoints (via aiohttp TestClient)
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from typing import AsyncGenerator, Generator

import pytest
from aiohttp.test_utils import TestServer, TestClient

from simple_a2a_registry.database import SQLiteEngine
from simple_a2a_registry.orchestration.cron import (
    CronTask,
    CronExecution,
    CronTaskStore,
    CronScheduler,
    _maybe_create_schema,
    compute_next_run,
    SCHEDULER_INTERVAL,
)
from simple_a2a_registry.orchestration.store import TaskStore
from simple_a2a_registry.server import create_app


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
def engine(db_path: str) -> Generator[SQLiteEngine, None, None]:
    """Create a fresh SQLiteEngine connected to the temp DB."""
    eng = SQLiteEngine(db_path)
    eng.connect()
    _maybe_create_schema(eng)
    try:
        yield eng
    finally:
        eng.close()


@pytest.fixture
def task_store(db_path: str) -> Generator[TaskStore, None, None]:
    """Create a fresh TaskStore backed by a tempfile."""
    ts = TaskStore(db_path)
    try:
        yield ts
    finally:
        ts.close()


@pytest.fixture
def cron_store(engine: SQLiteEngine) -> Generator[CronTaskStore, None, None]:
    """Create a fresh CronTaskStore backed by the engine."""
    yield CronTaskStore(engine)


# ---------------------------------------------------------------------------
# API test fixture — uses create_app for full integration
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
# CronTask — dataclass helpers
# ===================================================================


class TestCronTaskDataclass:
    """CronTask dataclass: ensure_id, to_dict, from_dict."""

    def test_ensure_id_generates(self):
        t = CronTask(name="test", assignee="w1", cron_expression="* * * * *")
        tid = t.ensure_id()
        assert tid.startswith("cron_")
        assert len(tid) > 5
        # Calling again returns the same id
        assert t.ensure_id() == tid

    def test_to_dict_serializes(self):
        t = CronTask(
            id="cron_abc",
            name="Nightly",
            assignee="bot",
            cron_expression="0 2 * * *",
            task_template='{"title":"Nightly Job","body":"run nightly"}',
            enabled=True,
            created_by="admin",
            created_at=1000,
        )
        d = t.to_dict()
        assert d["id"] == "cron_abc"
        assert d["name"] == "Nightly"
        assert d["enabled"] == 1
        assert d["task_template"]["title"] == "Nightly Job"

    def test_to_dict_omits_none(self):
        t = CronTask(name="test", assignee="w1", cron_expression="* * * * *")
        d = t.to_dict()
        assert "last_run" not in d
        assert "next_run" not in d

    def test_from_dict_restores(self):
        d = {
            "id": "cron_xyz",
            "name": "Hourly",
            "assignee": "worker",
            "cron_expression": "0 * * * *",
            "task_template": {"title": "Hourly Task"},
            "enabled": True,
            "created_by": "test",
            "created_at": 2000,
            "tenant": "team-a",
        }
        t = CronTask.from_dict(d)
        assert t.id == "cron_xyz"
        assert t.enabled is True
        assert isinstance(t.task_template, str)
        assert "Hourly Task" in t.task_template

    def test_from_dict_bool_enabled(self):
        d = {
            "id": "cron_1", "name": "T", "assignee": "w",
            "cron_expression": "* * * * *",
            "enabled": 0, "created_by": "u", "created_at": 0,
        }
        t = CronTask.from_dict(d)
        assert t.enabled is False


# ===================================================================
# CronTaskStore — CRUD
# ===================================================================


class TestCronTaskStoreCRUD:
    """CronTaskStore: create, read, list, delete, update operations."""

    def test_create_and_get(self, cron_store: CronTaskStore):
        cron = CronTask(
            name="Test Cron",
            assignee="tester",
            cron_expression="*/5 * * * *",
            task_template='{"title":"Test","body":"hello"}',
            enabled=True,
            created_by="admin",
            tenant="default",
        )
        cron_id = cron_store.create_cron_task(cron)
        assert cron_id.startswith("cron_")

        fetched = cron_store.get_cron_task(cron_id)
        assert fetched is not None
        assert fetched.name == "Test Cron"
        assert fetched.assignee == "tester"
        assert fetched.cron_expression == "*/5 * * * *"
        assert fetched.enabled is True
        assert fetched.created_by == "admin"
        assert fetched.tenant == "default"
        assert fetched.next_run is not None  # computed by create

    def test_list(self, cron_store: CronTaskStore):
        # Create two tasks
        a = CronTask(name="A", assignee="w1", cron_expression="* * * * *", created_by="u")
        b = CronTask(name="B", assignee="w2", cron_expression="0 * * * *", created_by="u")
        cron_store.create_cron_task(a)
        cron_store.create_cron_task(b)

        all_tasks = cron_store.list_cron_tasks()
        assert len(all_tasks) >= 2

        names = {t.name for t in all_tasks}
        assert "A" in names
        assert "B" in names

    def test_list_enabled_only(self, cron_store: CronTaskStore):
        a = CronTask(name="Enabled", assignee="w1", cron_expression="* * * * *",
                     enabled=True, created_by="u")
        b = CronTask(name="Disabled", assignee="w2", cron_expression="* * * * *",
                     enabled=False, created_by="u")
        cron_store.create_cron_task(a)
        cron_store.create_cron_task(b)

        enabled = cron_store.list_cron_tasks(enabled_only=True)
        assert len(enabled) == 1
        assert enabled[0].name == "Enabled"

    def test_delete(self, cron_store: CronTaskStore):
        cron = CronTask(name="Del", assignee="w", cron_expression="* * * * *",
                        created_by="u")
        cron_id = cron_store.create_cron_task(cron)
        assert cron_store.get_cron_task(cron_id) is not None

        deleted = cron_store.delete_cron_task(cron_id)
        assert deleted is True
        assert cron_store.get_cron_task(cron_id) is None

    def test_delete_nonexistent(self, cron_store: CronTaskStore):
        deleted = cron_store.delete_cron_task("cron_nonexistent")
        assert deleted is False

    def test_set_enabled(self, cron_store: CronTaskStore):
        cron = CronTask(name="Toggle", assignee="w", cron_expression="* * * * *",
                        created_by="u")
        cron_id = cron_store.create_cron_task(cron)

        updated = cron_store.set_enabled(cron_id, False)
        assert updated is True
        fetched = cron_store.get_cron_task(cron_id)
        assert fetched is not None
        assert fetched.enabled is False

    def test_update_next_run(self, cron_store: CronTaskStore):
        cron = CronTask(name="Next", assignee="w", cron_expression="* * * * *",
                        created_by="u")
        cron_id = cron_store.create_cron_task(cron)
        now = int(time.time())
        next_r = now + 3600

        cron_store.update_next_run(cron_id, last_run=now, next_run=next_r)
        fetched = cron_store.get_cron_task(cron_id)
        assert fetched is not None
        assert fetched.last_run == now
        assert fetched.next_run == next_r


# ===================================================================
# CronExecution — recording
# ===================================================================


class TestCronExecution:
    """CronExecution CRUD on the executions table."""

    def test_create_execution(self, cron_store: CronTaskStore):
        # First create a cron task
        cron = CronTask(name="Exec Test", assignee="w",
                        cron_expression="* * * * *", created_by="u")
        cron_id = cron_store.create_cron_task(cron)
        assert cron_id.startswith("cron_")

        exec_rec = CronExecution(
            cron_task_id=cron_id,
            task_id="t_abc123",
            scheduled_at=1000,
            started_at=1000,
            status="running",
        )
        exec_id = cron_store.create_execution(exec_rec)
        assert exec_id > 0

    def test_update_execution_status(self, cron_store: CronTaskStore):
        cron = CronTask(name="Exec Upd", assignee="w",
                        cron_expression="* * * * *", created_by="u")
        cron_id = cron_store.create_cron_task(cron)

        exec_rec = CronExecution(
            cron_task_id=cron_id,
            task_id="t_def456",
            scheduled_at=2000,
            started_at=2000,
            status="running",
        )
        exec_id = cron_store.create_execution(exec_rec)
        assert exec_id > 0

        now = int(time.time())
        cron_store.update_execution_status(exec_id, "completed", completed_at=now)

        execs = cron_store.list_executions(cron_id)
        assert len(execs) == 1
        assert execs[0].status == "completed"

    def test_list_executions_pagination(self, cron_store: CronTaskStore):
        cron = CronTask(name="Exec Page", assignee="w",
                        cron_expression="* * * * *", created_by="u")
        cron_id = cron_store.create_cron_task(cron)

        for i in range(5):
            cron_store.create_execution(CronExecution(
                cron_task_id=cron_id,
                task_id=f"t_{i}",
                scheduled_at=1000 + i,
                started_at=1000 + i,
                status="completed" if i < 4 else "running",
            ))

        # Full list
        all_execs = cron_store.list_executions(cron_id, limit=10, offset=0)
        assert len(all_execs) == 5

        # Paginated
        page = cron_store.list_executions(cron_id, limit=2, offset=0)
        assert len(page) == 2

        page2 = cron_store.list_executions(cron_id, limit=2, offset=2)
        assert len(page2) == 2


# ===================================================================
# Cron expression parsing
# ===================================================================


class TestComputeNextRun:
    """compute_next_run utility function."""

    def test_every_minute(self):
        result = compute_next_run("* * * * *", 0)
        assert result is not None
        assert result > 0
        assert result <= 60  # first minute boundary

    def test_daily_at_midnight(self):
        result = compute_next_run("0 0 * * *", 0)
        assert result is not None
        assert result == 86400  # next midnight

    def test_every_hour(self):
        result = compute_next_run("0 * * * *", 0)
        assert result is not None
        assert 3600 <= result <= 3600 * 2  # next hour boundary

    def test_invalid_expression(self):
        result = compute_next_run("not a cron", 0)
        assert result is None

    def test_invalid_fields(self):
        result = compute_next_run("a b c d", 0)
        assert result is None

    def test_from_specific_time(self):
        # Every 5 minutes starting at a specific time
        result = compute_next_run("*/5 * * * *", 100)
        assert result is not None
        assert result > 100

    def test_weekly(self):
        result = compute_next_run("0 0 * * 0", 0)  # Sunday midnight
        assert result is not None
        assert result == 259200  # 3 days = Thursday 1970-01-01 → Sunday at midnight


# ===================================================================
# CronScheduler — tick logic
# ===================================================================


class TestCronScheduler:
    """CronScheduler: _load_due, _fire, _tick."""

    def test_load_due_single(self, cron_store: CronTaskStore, task_store: TaskStore):
        scheduler = CronScheduler(cron_store, task_store, interval=3600)
        cron = CronTask(name="Due", assignee="w1", cron_expression="* * * * *",
                        enabled=True, created_by="test")
        cron_id = cron_store.create_cron_task(cron)

        # Manually set next_run in the past
        now = int(time.time())
        cron_store.update_next_run(cron_id, last_run=0, next_run=now - 60)

        due = scheduler._load_due_cron_tasks(now)
        assert len(due) == 1
        assert due[0].id == cron_id

    def test_load_due_not_yet(self, cron_store: CronTaskStore, task_store: TaskStore):
        scheduler = CronScheduler(cron_store, task_store, interval=3600)
        cron = CronTask(name="Future", assignee="w1", cron_expression="0 0 * * *",
                        enabled=True, created_by="test")
        cron_store.create_cron_task(cron)

        now = int(time.time())
        due = scheduler._load_due_cron_tasks(now)
        # next_run would be in the future (next midnight), so shouldn't be due
        for c in due:
            if c.name == "Future":
                pytest.fail("Future cron should not be due")
        # That's fine — the assertion is that "Future" is not in due

    def test_load_due_disabled(self, cron_store: CronTaskStore, task_store: TaskStore):
        scheduler = CronScheduler(cron_store, task_store, interval=3600)
        cron = CronTask(name="Disabled", assignee="w1", cron_expression="* * * * *",
                        enabled=False, created_by="test")
        cron_id = cron_store.create_cron_task(cron)

        now = int(time.time())
        cron_store.update_next_run(cron_id, last_run=0, next_run=now - 60)

        due = scheduler._load_due_cron_tasks(now)
        for c in due:
            if c.name == "Disabled":
                pytest.fail("Disabled cron should not be loaded")

    def test_load_due_multiple(self, cron_store: CronTaskStore, task_store: TaskStore):
        scheduler = CronScheduler(cron_store, task_store, interval=3600)
        now = int(time.time())

        for i in range(3):
            c = CronTask(name=f"Cron{i}", assignee="w",
                         cron_expression="* * * * *", enabled=True, created_by="test")
            c_id = cron_store.create_cron_task(c)
            cron_store.update_next_run(c_id, last_run=0, next_run=now - 60)

        due = scheduler._load_due_cron_tasks(now)
        assert len(due) == 3

    def test_fire_creates_task(self, cron_store: CronTaskStore, task_store: TaskStore):
        import asyncio

        scheduler = CronScheduler(cron_store, task_store, interval=3600)
        now = int(time.time())

        cron = CronTask(
            name="FireTest",
            assignee="worker",
            cron_expression="* * * * *",
            task_template=json.dumps({"title": "Fired Task", "priority": 5}),
            enabled=True,
            created_by="test",
        )
        cron_id = cron_store.create_cron_task(cron)

        # Fire it (async)
        fire_now = int(time.time())
        cron_task = cron_store.get_cron_task(cron_id)
        assert cron_task is not None
        asyncio.run(scheduler._fire_cron_task(cron_task, fire_now))

        # Verify a kanban task was created
        all_tasks, total = task_store.list_tasks(limit=10)
        titles = [t.title for t in all_tasks]
        assert "Fired Task" in titles

        # Verify execution was recorded
        execs = cron_store.list_executions(cron_id)
        assert len(execs) == 1
        assert execs[0].status == "completed"

        # Verify next_run was advanced
        updated = cron_store.get_cron_task(cron_id)
        assert updated is not None
        assert updated.next_run > fire_now

    def test_tick_creates_multiple(self, cron_store: CronTaskStore, task_store: TaskStore):
        import asyncio

        scheduler = CronScheduler(cron_store, task_store, interval=3600)
        now = int(time.time())

        ids = []
        for i in range(3):
            c = CronTask(name=f"Tick{i}", assignee="w",
                         cron_expression="* * * * *", enabled=True,
                         task_template=json.dumps({"title": f"Tick Task {i}"}),
                         created_by="test")
            c_id = cron_store.create_cron_task(c)
            cron_store.update_next_run(c_id, last_run=0, next_run=now - 60)
            ids.append(c_id)

        created = asyncio.run(scheduler._tick())
        assert created == 3

        # Verify all tasks created
        all_tasks, total = task_store.list_tasks(limit=10)
        titles = [t.title for t in all_tasks]
        for i in range(3):
            assert f"Tick Task {i}" in titles

    def test_tick_no_due(self, cron_store: CronTaskStore, task_store: TaskStore):
        scheduler = CronScheduler(cron_store, task_store, interval=3600)
        created = scheduler._tick()
        assert created == 0

    def test_stop(self, cron_store: CronTaskStore, task_store: TaskStore):
        scheduler = CronScheduler(cron_store, task_store, interval=60)
        assert scheduler._running is False
        # Start it briefly, then stop
        scheduler._running = True
        scheduler.stop()
        assert scheduler._running is False

    def test_parse_template(self, cron_store: CronTaskStore, task_store: TaskStore):
        scheduler = CronScheduler(cron_store, task_store, interval=3600)

        # Empty
        assert scheduler._parse_template("") == {}
        assert scheduler._parse_template(None) == {}

        # JSON string
        result = scheduler._parse_template('{"title":"Hello"}')
        assert result == {"title": "Hello"}

        # Already a dict
        result = scheduler._parse_template({"key": "val"})
        assert result == {"key": "val"}

        # Invalid JSON
        result = scheduler._parse_template("not json")
        assert result == {}

    def test_disabled_on_invalid_cron(self, cron_store: CronTaskStore, task_store: TaskStore):
        import asyncio

        """When a cron expression becomes invalid, the scheduler should disable it."""
        scheduler = CronScheduler(cron_store, task_store, interval=3600)
        now = int(time.time())

        cron = CronTask(name="InvalidExpr", assignee="w",
                        cron_expression="* * * * *", enabled=True,
                        created_by="test")
        cron_id = cron_store.create_cron_task(cron)

        # Set a valid next_run shortly in the past so it fires
        cron_store.update_next_run(cron_id, last_run=0, next_run=now - 10)

        # Manually craft an invalid expression before firing
        cron_task = cron_store.get_cron_task(cron_id)
        assert cron_task is not None
        cron_task.cron_expression = "blah blah"

        # The task should still fire (task creation), but next_run advance should fail
        # and the task should be disabled
        asyncio.run(scheduler._fire_cron_task(cron_task, now))

        updated = cron_store.get_cron_task(cron_id)
        assert updated is not None
        assert updated.enabled is False  # disabled due to invalid expression


# ===================================================================
# Integration test via REST API
# ===================================================================


class TestCronAPI:
    """REST API integration tests for /v2/cron endpoints."""

    pytestmark = pytest.mark.asyncio

    async def test_create_cron_task(self, api_client):
        async with await api_client() as client:
            resp = await client.post("/v2/cron", json={
                "name": "API Cron",
                "assignee": "bot",
                "cron": "*/5 * * * *",
                "task_template": {"title": "API Task", "body": "created via API"},
            })
            assert resp.status == 201
            data = await resp.json()
            assert data["name"] == "API Cron"
            assert data["assignee"] == "bot"
            assert data["cron_expression"] == "*/5 * * * *"
            assert data["enabled"] == 1
            assert data["id"].startswith("cron_")
            # task_template should be serialized back
            tt = data["task_template"]
            assert isinstance(tt, dict)
            assert tt["title"] == "API Task"

    async def test_create_cron_missing_name(self, api_client):
        async with await api_client() as client:
            resp = await client.post("/v2/cron", json={
                "assignee": "bot",
                "cron": "* * * * *",
            })
            assert resp.status == 400
            data = await resp.json()
            assert data["error"] == "validation_error"

    async def test_create_cron_missing_assignee(self, api_client):
        async with await api_client() as client:
            resp = await client.post("/v2/cron", json={
                "name": "NoAssignee",
                "cron": "* * * * *",
            })
            assert resp.status == 400
            data = await resp.json()
            assert data["error"] == "validation_error"

    async def test_create_cron_missing_cron(self, api_client):
        async with await api_client() as client:
            resp = await client.post("/v2/cron", json={
                "name": "NoCron",
                "assignee": "bot",
            })
            assert resp.status == 400
            data = await resp.json()
            assert data["error"] == "validation_error"

    async def test_create_cron_invalid_expression(self, api_client):
        async with await api_client() as client:
            resp = await client.post("/v2/cron", json={
                "name": "Bad Cron",
                "assignee": "bot",
                "cron": "not valid",
            })
            assert resp.status == 400
            data = await resp.json()
            assert data["error"] == "invalid_cron"

    async def test_create_cron_invalid_json(self, api_client):
        async with await api_client() as client:
            resp = await client.post(
                "/v2/cron",
                data="not json{{{",
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 400
            data = await resp.json()
            assert data["error"] == "invalid_json"

    async def test_list_cron_tasks(self, api_client):
        async with await api_client() as client:
            # Create two
            await client.post("/v2/cron", json={
                "name": "C1", "assignee": "w1", "cron": "* * * * *",
            })
            await client.post("/v2/cron", json={
                "name": "C2", "assignee": "w2", "cron": "0 * * * *",
            })

            resp = await client.get("/v2/cron")
            assert resp.status == 200
            data = await resp.json()
            assert data["total"] >= 2
            names = {t["name"] for t in data["cron_tasks"]}
            assert "C1" in names
            assert "C2" in names

    async def test_list_cron_tasks_enabled_only(self, api_client):
        async with await api_client() as client:
            await client.post("/v2/cron", json={
                "name": "Enabled", "assignee": "w", "cron": "* * * * *",
            })

            resp = await client.get("/v2/cron?enabled=1")
            assert resp.status == 200
            data = await resp.json()
            assert data["total"] >= 1

    async def test_get_executions(self, api_client):
        async with await api_client() as client:
            # Create a cron task
            r = await client.post("/v2/cron", json={
                "name": "ExecTest", "assignee": "w", "cron": "* * * * *",
            })
            cron_id = (await r.json())["id"]

            # Get executions (should be empty initially)
            r = await client.get(f"/v2/cron/{cron_id}/executions")
            assert r.status == 200
            data = await r.json()
            assert data["total"] == 0

    async def test_get_executions_nonexistent(self, api_client):
        async with await api_client() as client:
            resp = await client.get("/v2/cron/cron_nonexist/executions")
            assert resp.status == 404
            data = await resp.json()
            assert data["error"] == "not_found"

    async def test_delete_cron_task(self, api_client):
        async with await api_client() as client:
            r = await client.post("/v2/cron", json={
                "name": "DeleteMe", "assignee": "w", "cron": "* * * * *",
            })
            cron_id = (await r.json())["id"]

            resp = await client.delete(f"/v2/cron/{cron_id}")
            assert resp.status == 200
            data = await resp.json()
            assert data["message"] == "Cron task deleted"

            # Verify gone
            resp = await client.delete(f"/v2/cron/{cron_id}")
            assert resp.status == 404

    async def test_delete_cron_task_nonexistent(self, api_client):
        async with await api_client() as client:
            resp = await client.delete("/v2/cron/cron_nonexist")
            assert resp.status == 404
            data = await resp.json()
            assert data["error"] == "not_found"

    async def test_patch_enable_disable(self, api_client):
        async with await api_client() as client:
            r = await client.post("/v2/cron", json={
                "name": "Toggle", "assignee": "w", "cron": "* * * * *",
            })
            cron_id = (await r.json())["id"]

            # Disable
            r = await client.patch(f"/v2/cron/{cron_id}", json={"enabled": False})
            assert r.status == 200
            data = await r.json()
            assert data["enabled"] == 0

            # Re-enable
            r = await client.patch(f"/v2/cron/{cron_id}", json={"enabled": True})
            assert r.status == 200
            data = await r.json()
            assert data["enabled"] == 1

    async def test_patch_nonexistent(self, api_client):
        async with await api_client() as client:
            resp = await client.patch("/v2/cron/cron_nonexist", json={"enabled": False})
            assert resp.status == 404
            data = await resp.json()
            assert data["error"] == "not_found"
