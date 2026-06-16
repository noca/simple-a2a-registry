"""Integration tests for the V2 Workflow REST API — POST/GET workflow endpoints."""

from __future__ import annotations

import json
import tempfile

import pytest
from aiohttp.test_utils import TestServer, TestClient

from simple_a2a_registry.server import create_app

pytestmark = pytest.mark.asyncio


@pytest.fixture
def app_factory():
    """Return a callable that creates a fresh TestClient for each test."""
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


# ---------------------------------------------------------------------------
# POST /v2/workflows
# ---------------------------------------------------------------------------


class TestCreateWorkflow:
    """POST /v2/workflows — submit a YAML/JSON workflow definition."""

    YAML_PAYLOAD = {
        "yaml": (
            "name: Test Workflow\n"
            "tasks:\n"
            "  - id: build\n"
            "    title: Build\n"
            "    assignee: builder\n"
            "  - id: test\n"
            "    title: Test\n"
            "    assignee: tester\n"
            "    depends_on:\n"
            "      - task: build\n"
            "        condition: success\n"
        ),
    }

    JSON_PAYLOAD = {
        "name": "JSON Workflow",
        "tasks": [
            {"id": "fetch", "title": "Fetch Data", "assignee": "coder"},
            {
                "id": "process",
                "title": "Process Data",
                "assignee": "processor",
                "depends_on": [{"task": "fetch", "condition": "success"}],
            },
        ],
    }

    async def test_create_via_yaml(self, app_factory):
        """Submit a workflow via the ``yaml`` field."""
        async with await app_factory() as client:
            resp = await client.post("/v2/workflows", json=self.YAML_PAYLOAD)
            assert resp.status == 201
            data = await resp.json()
            assert "workflow_id" in data
            assert data["workflow_id"].startswith("wf_")
            assert data["name"] == "Test Workflow"
            assert data["created_count"] == 2
            assert len(data["task_ids"]) == 2
            assert "build" in data["task_ids"]
            assert "test" in data["task_ids"]

    async def test_create_via_json(self, app_factory):
        """Submit a workflow as an inline JSON definition."""
        async with await app_factory() as client:
            resp = await client.post("/v2/workflows", json=self.JSON_PAYLOAD)
            assert resp.status == 201
            data = await resp.json()
            assert "workflow_id" in data
            assert data["name"] == "JSON Workflow"
            assert data["created_count"] == 2
            assert "fetch" in data["task_ids"]
            assert "process" in data["task_ids"]

    async def test_create_with_validation_error(self, app_factory):
        """Invalid workflow returns 400 with error details."""
        async with await app_factory() as client:
            resp = await client.post("/v2/workflows", json={
                "yaml": "name: Bad\ntasks: []",
            })
            assert resp.status == 400
            data = await resp.json()
            assert data["error"] == "validation_error"

    async def test_create_missing_name_and_yaml(self, app_factory):
        """Missing both ``name`` and ``yaml`` fields returns 400."""
        async with await app_factory() as client:
            resp = await client.post("/v2/workflows", json={"tasks": []})
            assert resp.status == 400
            data = await resp.json()
            assert data["error"] == "validation_error"

    async def test_create_invalid_json_body(self, app_factory):
        """Malformed JSON body returns 400."""
        async with await app_factory() as client:
            # Send raw malformed text rather than JSON
            resp = await client.post(
                "/v2/workflows",
                data="not-json{{{",
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 400
            data = await resp.json()
            assert data["error"] == "invalid_json"

    async def test_create_empty_yaml_field(self, app_factory):
        """Empty ``yaml`` field returns 400."""
        async with await app_factory() as client:
            resp = await client.post("/v2/workflows", json={"yaml": ""})
            assert resp.status == 400
            data = await resp.json()
            assert data["error"] == "validation_error"

    async def test_create_workflow_with_cycle(self, app_factory):
        """Cyclic workflow definition returns 400."""
        async with await app_factory() as client:
            resp = await client.post("/v2/workflows", json={
                "yaml": (
                    "name: Cyclic\n"
                    "tasks:\n"
                    "  - id: a\n"
                    "    title: A\n"
                    "    assignee: w1\n"
                    "    depends_on:\n"
                    "      - task: c\n"
                    "  - id: b\n"
                    "    title: B\n"
                    "    assignee: w2\n"
                    "    depends_on:\n"
                    "      - task: a\n"
                    "  - id: c\n"
                    "    title: C\n"
                    "    assignee: w3\n"
                    "    depends_on:\n"
                    "      - task: b\n"
                ),
            })
            assert resp.status == 400
            data = await resp.json()
            assert data["error"] == "validation_error"


# ---------------------------------------------------------------------------
# GET /v2/workflows/{id}
# ---------------------------------------------------------------------------


class TestGetWorkflow:
    """GET /v2/workflows/{id} — query workflow status."""

    async def _create_workflow(self, client) -> str:
        """Helper: create a simple workflow and return the workflow_id."""
        resp = await client.post("/v2/workflows", json={
            "yaml": (
                "name: Workflow\n"
                "tasks:\n"
                "  - id: step1\n"
                "    title: Step 1\n"
                "    assignee: w1\n"
            ),
        })
        data = await resp.json()
        return data["workflow_id"]

    async def test_get_existing_workflow(self, app_factory):
        """GET an existing workflow returns its status."""
        async with await app_factory() as client:
            wf_id = await self._create_workflow(client)
            resp = await client.get(f"/v2/workflows/{wf_id}")
            assert resp.status == 200
            data = await resp.json()
            assert data["workflow_id"] == wf_id
            assert data["name"] == "Workflow"
            assert data["status"] in ("pending", "running", "completed")
            assert data["task_count"] == 1
            assert len(data["tasks"]) == 1
            assert data["tasks"][0]["logical_id"] == "step1"
            assert data["tasks"][0]["status"] is not None

    async def test_get_nonexistent_workflow(self, app_factory):
        """GET a non-existent workflow returns 404."""
        async with await app_factory() as client:
            resp = await client.get("/v2/workflows/wf_nonexistent")
            assert resp.status == 404
            data = await resp.json()
            assert data["error"] == "workflow_not_found"

    async def test_get_workflow_with_multiple_tasks(self, app_factory):
        """Workflow with multiple tasks shows all statuses."""
        async with await app_factory() as client:
            resp = await client.post("/v2/workflows", json=TestCreateWorkflow.JSON_PAYLOAD)
            assert resp.status == 201
            wf_id = (await resp.json())["workflow_id"]

            resp = await client.get(f"/v2/workflows/{wf_id}")
            assert resp.status == 200
            data = await resp.json()
            assert data["task_count"] == 2
            assert data["name"] == "JSON Workflow"
            logical_ids = {t["logical_id"] for t in data["tasks"]}
            assert logical_ids == {"fetch", "process"}


# ---------------------------------------------------------------------------
# GET /v2/workflows/{id}/tasks
# ---------------------------------------------------------------------------


class TestGetWorkflowTasks:
    """GET /v2/workflows/{id}/tasks — list all tasks in a workflow."""

    async def _create_workflow(self, client) -> str:
        resp = await client.post("/v2/workflows", json={
            "yaml": (
                "name: Multi Task\n"
                "tasks:\n"
                "  - id: a\n"
                "    title: Task A\n"
                "    assignee: w1\n"
                "  - id: b\n"
                "    title: Task B\n"
                "    assignee: w2\n"
                "    depends_on:\n"
                "      - task: a\n"
            ),
        })
        data = await resp.json()
        return data["workflow_id"]

    async def test_list_tasks(self, app_factory):
        """Listing tasks returns full task details."""
        async with await app_factory() as client:
            wf_id = await self._create_workflow(client)
            resp = await client.get(f"/v2/workflows/{wf_id}/tasks")
            assert resp.status == 200
            data = await resp.json()
            assert data["workflow_id"] == wf_id
            assert data["name"] == "Multi Task"
            assert data["total"] == 2
            assert len(data["tasks"]) == 2

            # Check task details
            task_map = {t["logical_id"]: t for t in data["tasks"]}
            assert "a" in task_map
            assert "b" in task_map
            assert task_map["a"]["title"] == "Task A"
            assert task_map["a"]["assignee"] == "w1"

    async def test_list_tasks_nonexistent_workflow(self, app_factory):
        """Listing tasks for a non-existent workflow returns 404."""
        async with await app_factory() as client:
            resp = await client.get("/v2/workflows/wf_bad/tasks")
            assert resp.status == 404
            data = await resp.json()
            assert data["error"] == "workflow_not_found"

    async def test_list_tasks_full_details(self, app_factory):
        """Task details include body, status, assignee, priority, etc."""
        async with await app_factory() as client:
            resp = await client.post("/v2/workflows", json={
                "yaml": (
                    "name: Detailed\n"
                    "tasks:\n"
                    "  - id: x\n"
                    "    title: Task X\n"
                    "    body: Some body\n"
                    "    assignee: worker\n"
                    "    priority: 5\n"
                ),
            })
            assert resp.status == 201
            wf_id = (await resp.json())["workflow_id"]

            resp = await client.get(f"/v2/workflows/{wf_id}/tasks")
            assert resp.status == 200
            data = await resp.json()
            assert data["total"] == 1
            task = data["tasks"][0]
            assert task["logical_id"] == "x"
            assert task["title"] == "Task X"
            assert task["body"] is not None
            assert "Some body" in task["body"]
            assert task["assignee"] == "worker"
            assert task["priority"] == 5
            assert task["status"] is not None
            assert "task_id" in task