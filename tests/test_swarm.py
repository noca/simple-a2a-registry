"""Tests for the Swarm topology module (swarm.py + swarm_routes.py)."""

from __future__ import annotations

import json
import os
import tempfile
from typing import Generator

import pytest
from aiohttp.test_utils import TestServer, TestClient

from simple_a2a_registry.orchestration.swarm import (
    SwarmWorkerSpec,
    BLACKBOARD_PREFIX,
    create_swarm,
    post_blackboard,
    read_blackboard,
    get_swarm_status,
)
from simple_a2a_registry.orchestration.store import TaskStore
from simple_a2a_registry.orchestration.models import TaskStatus
from simple_a2a_registry.server import create_app

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def store() -> Generator[TaskStore, None, None]:
    """Create a fresh TaskStore backed by a tempfile for each test."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    ts = TaskStore(db_path)
    try:
        yield ts
    finally:
        ts.close()
        if os.path.exists(db_path):
            os.unlink(db_path)


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
# create_swarm unit tests
# ===================================================================


class TestCreateSwarm:
    def test_create_full_swarm(self, store: TaskStore) -> None:
        """Create a full 2-worker swarm with verifier and synthesizer."""
        result = create_swarm(
            store,
            goal="Research market trends",
            workers=[
                SwarmWorkerSpec(profile="researcher-a", title="Macro research"),
                SwarmWorkerSpec(profile="researcher-b", title="Competitor research"),
            ],
            verifier_profile="reviewer",
            synthesizer_profile="writer",
            root_title="Swarm: Market Research",
            tenant="test-org",
        )
        assert result.root_id.startswith("t_")
        assert len(result.worker_ids) == 2
        assert result.verifier_id.startswith("t_")
        assert result.synthesizer_id.startswith("t_")

        # Root is completed
        root = store.get_task(result.root_id)
        assert root is not None
        assert root.status == TaskStatus.COMPLETED.value

        # Workers are ready (root done -> promote)
        for wid in result.worker_ids:
            w = store.get_task(wid)
            assert w is not None
            assert w.status == TaskStatus.READY.value

        # Verifier is todo (waiting for workers)
        v = store.get_task(result.verifier_id)
        assert v is not None
        assert v.status == TaskStatus.TODO.value

        # Synthesizer is todo (waiting for verifier)
        s = store.get_task(result.synthesizer_id)
        assert s is not None
        assert s.status == TaskStatus.TODO.value

    def test_swarm_context_injected(self, store: TaskStore) -> None:
        """Worker/Verifier/Synth bodies include swarm context."""
        result = create_swarm(
            store,
            goal="Analyze data",
            workers=[SwarmWorkerSpec(profile="worker-a", title="Worker 1", body="Do stuff")],
            verifier_profile="reviewer",
            synthesizer_profile="writer",
        )
        w = store.get_task(result.worker_ids[0])
        assert w is not None
        assert w.body is not None
        assert "Swarm 协议" in w.body
        assert result.root_id in w.body
        assert "Analyze data" in w.body

    def test_swarm_blackboard_written(self, store: TaskStore) -> None:
        """Topology info is written to blackboard after creation."""
        result = create_swarm(
            store,
            goal="Test",
            workers=[SwarmWorkerSpec(profile="w", title="W1")],
            verifier_profile="v",
            synthesizer_profile="s",
        )
        bb = read_blackboard(store, result.root_id)
        assert "topology" in bb
        assert bb["topology"]["goal"] == "Test"
        assert len(bb["topology"]["worker_ids"]) == 1

    def test_no_workers_raises(self, store: TaskStore) -> None:
        """At least one worker is required."""
        with pytest.raises(ValueError, match="At least one worker"):
            create_swarm(
                store,
                goal="Empty",
                workers=[],
                verifier_profile="v",
                synthesizer_profile="s",
            )

    def test_worker_missing_profile(self, store: TaskStore) -> None:
        """Worker without profile raises."""
        with pytest.raises(ValueError, match="profile"):
            create_swarm(
                store,
                goal="Test",
                workers=[SwarmWorkerSpec(profile="", title="W1")],
                verifier_profile="v",
                synthesizer_profile="s",
            )


# ===================================================================
# Blackboard unit tests
# ===================================================================


class TestBlackboard:
    def test_post_and_read(self, store: TaskStore) -> None:
        """Write a blackboard entry and read it back."""
        # First create a task to be the swarm root
        task = store.create_task(title="Swarm Root", body="Root")
        root_id = task.id
        store.update_task_status(root_id, TaskStatus.RUNNING.value)
        store.update_task_status(root_id, TaskStatus.COMPLETED.value)

        # Write
        comment = post_blackboard(
            store, root_id,
            author="researcher-a",
            key="phase1",
            value={"summary": "GDP at 3%"},
        )
        assert comment.id > 0

        # Read
        bb = read_blackboard(store, root_id)
        assert bb["phase1"]["summary"] == "GDP at 3%"
        assert bb["_authors"]["phase1"] == "researcher-a"

    def test_later_write_overwrites(self, store: TaskStore) -> None:
        """Later writes for the same key overwrite earlier ones."""
        task = store.create_task(title="Root")
        root_id = task.id
        store.update_task_status(root_id, TaskStatus.RUNNING.value)
        store.update_task_status(root_id, TaskStatus.COMPLETED.value)

        post_blackboard(store, root_id, author="a1", key="result", value="v1")
        post_blackboard(store, root_id, author="a2", key="result", value="v2")

        bb = read_blackboard(store, root_id)
        assert bb["result"] == "v2"
        assert bb["_authors"]["result"] == "a2"

    def test_compatible_with_hermes_format(self, store: TaskStore) -> None:
        """Reads both [swarm:blackboard] and [swarm:blackboard ] formats."""
        task = store.create_task(title="Root")
        root_id = task.id
        store.update_task_status(root_id, TaskStatus.RUNNING.value)
        store.update_task_status(root_id, TaskStatus.COMPLETED.value)

        # Registry format (no trailing space)
        post_blackboard(store, root_id, author="registry", key="r_key", value="r_val")

        # Hermes format (with trailing space) - directly via add_comment
        store.add_comment(
            root_id, "hermes",
            f"{BLACKBOARD_PREFIX} {{\"key\": \"h_key\", \"value\": \"h_val\"}}",
        )

        bb = read_blackboard(store, root_id)
        assert bb["r_key"] == "r_val"
        assert bb["h_key"] == "h_val"

    def test_multiple_keys(self, store: TaskStore) -> None:
        """Multiple keys are all readable."""
        task = store.create_task(title="Root")
        root_id = task.id
        store.update_task_status(root_id, TaskStatus.RUNNING.value)
        store.update_task_status(root_id, TaskStatus.COMPLETED.value)

        post_blackboard(store, root_id, author="a1", key="key1", value=1)
        post_blackboard(store, root_id, author="a1", key="key2", value="two")
        post_blackboard(store, root_id, author="a2", key="key3", value=[3])

        bb = read_blackboard(store, root_id)
        assert bb["key1"] == 1
        assert bb["key2"] == "two"
        assert bb["key3"] == [3]


# ===================================================================
# get_swarm_status unit tests
# ===================================================================


class TestGetSwarmStatus:
    def test_full_status(self, store: TaskStore) -> None:
        """Returns full swarm status with workers, verifier, synth, blackboard."""
        result = create_swarm(
            store,
            goal="Test status",
            workers=[
                SwarmWorkerSpec(profile="a", title="W1"),
                SwarmWorkerSpec(profile="b", title="W2"),
            ],
            verifier_profile="v",
            synthesizer_profile="s",
        )
        status = get_swarm_status(store, result.root_id)
        assert status is not None
        assert status["swarm"]["root_id"] == result.root_id
        assert status["swarm"]["status"] == TaskStatus.COMPLETED.value
        assert len(status["workers"]) == 2
        assert status["verifier"] is not None
        assert status["synthesizer"] is not None
        assert "topology" in status["blackboard"]

    def test_nonexistent_root(self, store: TaskStore) -> None:
        """Returns None for nonexistent root."""
        assert get_swarm_status(store, "t_nonexistent") is None


# ===================================================================
# Swarm API integration tests
# ===================================================================


@pytest.mark.asyncio
class TestSwarmAPI:
    async def test_post_swarm(self, api_client):
        """POST /v2/swarm creates a full swarm topology."""
        async with await api_client() as client:
            resp = await client.post("/v2/swarm", json={
                "goal": "Research market trends",
                "workers": [
                    {"profile": "researcher-a", "title": "Macro research"},
                    {"profile": "researcher-b", "title": "Competitor research"},
                ],
                "verifier": {"profile": "reviewer", "title": "Check results"},
                "synthesizer": {"profile": "writer", "title": "Write report"},
                "root_title": "Swarm: Market Research",
                "tenant": "test-org",
            })
            assert resp.status == 201
            data = await resp.json()
            assert "swarm" in data
            assert "topology" in data
            s = data["swarm"]
            assert s["root_id"].startswith("t_")
            assert len(s["worker_ids"]) == 2
            assert s["verifier_id"].startswith("t_")
            assert s["synthesizer_id"].startswith("t_")

            t = data["topology"]
            assert t["root"]["status"] == "completed"
            assert len(t["workers"]) == 2
            assert t["workers"][0]["status"] == "ready"
            assert t["verifier"]["status"] == "todo"
            assert t["synthesizer"]["status"] == "todo"

    async def test_get_swarm(self, api_client):
        """GET /v2/swarm/{root_id} returns full status."""
        async with await api_client() as client:
            # Create swarm
            r1 = await client.post("/v2/swarm", json={
                "goal": "Test",
                "workers": [{"profile": "w", "title": "Worker 1"}],
                "verifier": {"profile": "v"},
                "synthesizer": {"profile": "s"},
            })
            root_id = (await r1.json())["swarm"]["root_id"]

            # Get status
            r2 = await client.get(f"/v2/swarm/{root_id}")
            assert r2.status == 200
            data = await r2.json()
            assert data["swarm"]["root_id"] == root_id
            assert len(data["workers"]) == 1

    async def test_get_swarm_not_found(self, api_client):
        """GET /v2/swarm/nonexistent returns 404."""
        async with await api_client() as client:
            resp = await client.get("/v2/swarm/t_nonexistent")
            assert resp.status == 404

    async def test_post_comment(self, api_client):
        """POST /v2/swarm/{root_id}/comment writes to blackboard."""
        async with await api_client() as client:
            # Create swarm
            r1 = await client.post("/v2/swarm", json={
                "goal": "Test comment",
                "workers": [{"profile": "w", "title": "W1"}],
                "verifier": {"profile": "v"},
                "synthesizer": {"profile": "s"},
            })
            root_id = (await r1.json())["swarm"]["root_id"]

            # Write comment
            r2 = await client.post(
                f"/v2/swarm/{root_id}/comment",
                json={
                    "author": "researcher-a",
                    "key": "my_result",
                    "value": {"found": "interesting data"},
                },
            )
            assert r2.status == 201
            assert (await r2.json())["key"] == "my_result"

            # Read back via blackboard endpoint
            r3 = await client.get(f"/v2/swarm/{root_id}/blackboard")
            assert r3.status == 200
            bb = await r3.json()
            assert bb["my_result"]["found"] == "interesting data"
            assert bb["_authors"]["my_result"] == "researcher-a"

    async def test_post_comment_missing_key(self, api_client):
        """POST /v2/swarm/{root_id}/comment without key returns 400."""
        async with await api_client() as client:
            r1 = await client.post("/v2/swarm", json={
                "goal": "Test",
                "workers": [{"profile": "w", "title": "W1"}],
                "verifier": {"profile": "v"},
                "synthesizer": {"profile": "s"},
            })
            root_id = (await r1.json())["swarm"]["root_id"]

            r2 = await client.post(
                f"/v2/swarm/{root_id}/comment",
                json={"author": "a", "value": 1},
            )
            assert r2.status == 400

    async def test_post_swarm_missing_goal(self, api_client):
        """POST /v2/swarm without goal returns 400."""
        async with await api_client() as client:
            resp = await client.post("/v2/swarm", json={
                "workers": [{"profile": "w", "title": "W1"}],
                "verifier": {"profile": "v"},
                "synthesizer": {"profile": "s"},
            })
            assert resp.status == 400

    async def test_post_swarm_missing_workers(self, api_client):
        """POST /v2/swarm without workers returns 400."""
        async with await api_client() as client:
            resp = await client.post("/v2/swarm", json={
                "goal": "Test",
                "workers": [],
                "verifier": {"profile": "v"},
                "synthesizer": {"profile": "s"},
            })
            assert resp.status == 400