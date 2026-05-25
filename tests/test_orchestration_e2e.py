"""E2E tests for the Orchestration Engine.

Exercises full end-to-end scenarios defined in architecture-v2.md §13.4.

Unlike integration tests that manually trigger dispatcher poll cycles, these
E2E tests let the real dispatcher run as a background asyncio task and simulate
worker processes that interact via the REST API.

Scenarios:
  1. Complete Pipeline — multi-stage task chain with auto-dispatch
  2. Fault Recovery — worker timeout → dispatcher detects → retry → success
  3. HITL Insertion — worker blocks → comment → unblock → complete
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, AsyncGenerator, Optional

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from simple_a2a_registry.server import create_app

pytestmark = pytest.mark.asyncio

# Default claim TTL for dispatcher — short so tests don't sleep forever
E2E_CLAIM_TTL = 5  # 5 seconds
E2E_DISPATCHER_INTERVAL = 1  # poll every 1 second


# ---------------------------------------------------------------------------
# Helper: create app with running dispatcher background task
# ---------------------------------------------------------------------------


async def _build_app_with_dispatcher(
    claim_ttl: int = E2E_CLAIM_TTL,
    dispatcher_interval: int = E2E_DISPATCHER_INTERVAL,
) -> tuple[TestClient, web.Application, asyncio.Task]:
    """Create a test app with the real dispatcher running in background.

    Returns (client, app, dispatcher_task). Caller MUST cancel the
    dispatcher_task in a finally block.
    """
    tmpdir = tempfile.mkdtemp()
    app = create_app(
        data_dir=tmpdir,
        base_url="http://localhost:8321",
        dispatcher_enabled=True,
        claim_ttl=claim_ttl,
        failure_limit=3,
        dispatcher_interval=dispatcher_interval,
    )
    server = TestServer(app)
    await server.start_server()
    client = TestClient(server)

    dispatcher = app.get("dispatcher")
    assert dispatcher is not None
    dt = asyncio.create_task(dispatcher.run())

    return client, app, dt


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class SimulatedWorker:
    """Simulates a worker that polls for ready tasks and executes them.

    This is the E2E equivalent of a real agent: it polls the /v2/tasks
    endpoint, claims any task assigned to it, executes the work, and
    completes it.
    """

    def __init__(
        self, client: TestClient, worker_id: str, pid: int = 1,
        claim_ttl: Optional[int] = None,
    ) -> None:
        self.client = client
        self.worker_id = worker_id
        self.pid = pid
        self.claim_ttl = claim_ttl  # None = use server default
        self.completed_tasks: list[str] = []
        self.failed_tasks: list[str] = []

    async def poll_and_execute(
        self, max_iterations: int = 20,
    ) -> None:
        """Poll for tasks assigned to this worker and execute them."""
        for _ in range(max_iterations):
            resp = await self.client.get(
                "/v2/tasks",
                params={"status": "ready", "assignee": self.worker_id},
            )
            data = await resp.json()
            tasks = data.get("tasks", [])

            if not tasks:
                await asyncio.sleep(0.5)
                continue

            for task in tasks:
                tid = task["id"]
                # Claim (pass optional TTL)
                claim_body: dict[str, Any] = {
                    "worker_id": self.worker_id, "pid": self.pid,
                }
                if self.claim_ttl is not None:
                    claim_body["ttl"] = self.claim_ttl
                claim = await self.client.post(
                    f"/v2/tasks/{tid}/claim",
                    json=claim_body,
                )
                if claim.status != 200:
                    continue

                lock = (await claim.json())["claim_lock"]

                # Execute (simulate work)
                await asyncio.sleep(0.1)

                # Complete
                comp = await self.client.post(
                    f"/v2/tasks/{tid}/complete",
                    json={
                        "claim_lock": lock,
                        "summary": f"Executed by {self.worker_id}",
                        "result": {"worker": self.worker_id},
                    },
                )
                if comp.status == 200:
                    self.completed_tasks.append(tid)
                else:
                    self.failed_tasks.append(tid)


# ---------------------------------------------------------------------------
# E2E Test 1: Complete Pipeline
# ---------------------------------------------------------------------------


class TestCompletePipeline:
    """§13.4 — Create multi-stage chain → dispatcher auto-dispatch →
    simulated workers execute → all complete."""

    async def test_three_stage_pipeline_e2e(self) -> None:
        """Complete Pipeline: A → B → C, each stage claimed by a different worker."""
        client, app, dt = await _build_app_with_dispatcher()
        try:
            await asyncio.sleep(0.5)  # Let dispatcher settle

            # Create 3 workers
            worker_a = SimulatedWorker(client, "worker-a", pid=101)
            worker_b = SimulatedWorker(client, "worker-b", pid=102)
            worker_c = SimulatedWorker(client, "worker-c", pid=103)

            # Create pipeline: A → B → C
            r_a = await client.post("/v2/tasks", json={
                "title": "E2E Stage A — Data Prep",
                "assignee": "worker-a",
            })
            a_id = (await r_a.json())["task"]["id"]

            r_b = await client.post("/v2/tasks", json={
                "title": "E2E Stage B — Processing",
                "assignee": "worker-b",
                "parents": [a_id],
            })
            b_id = (await r_b.json())["task"]["id"]

            r_c = await client.post("/v2/tasks", json={
                "title": "E2E Stage C — Output",
                "assignee": "worker-c",
                "parents": [b_id],
            })
            c_id = (await r_c.json())["task"]["id"]

            # Verify initial states
            d_a = await client.get(f"/v2/tasks/{a_id}")
            assert (await d_a.json())["task"]["status"] == "ready"

            d_b = await client.get(f"/v2/tasks/{b_id}")
            assert (await d_b.json())["task"]["status"] == "todo"

            d_c = await client.get(f"/v2/tasks/{c_id}")
            assert (await d_c.json())["task"]["status"] == "todo"

            # Workers execute in parallel
            await asyncio.gather(
                worker_a.poll_and_execute(),
                worker_b.poll_and_execute(),
                worker_c.poll_and_execute(),
            )

            # All three should be completed
            for tid, name in [(a_id, "A"), (b_id, "B"), (c_id, "C")]:
                d = await client.get(f"/v2/tasks/{tid}")
                status = (await d.json())["task"]["status"]
                assert status == "completed", \
                    f"{name} should be completed, got {status}"

            # Verify each worker completed its task
            assert a_id in worker_a.completed_tasks
            assert b_id in worker_b.completed_tasks
            assert c_id in worker_c.completed_tasks

        finally:
            dt.cancel()
            await client.close()


# ---------------------------------------------------------------------------
# E2E Test 2: Fault Recovery
# ---------------------------------------------------------------------------


class TestFaultRecovery:
    """§13.4 — Worker crashes → dispatcher detects timeout → retries →
    a subsequent worker completes the task successfully."""

    async def test_worker_timeout_retry(self) -> None:
        """Fault Recovery: claim a task with short TTL, let it expire,
        dispatcher marks as failed, promotes to ready, worker completes."""
        client, app, dt = await _build_app_with_dispatcher(claim_ttl=2)
        try:
            await asyncio.sleep(0.5)

            # Create a task with retries
            resp = await client.post("/v2/tasks", json={
                "title": "Fault Recovery Task",
                "assignee": "fault-worker",
                "max_retries": 2,
            })
            tid = (await resp.json())["task"]["id"]
            store = app["task_store"]

            # Claim the task manually (simulates a crashed worker that
            # claimed it but never completed, triggering the TTL expiry)
            claim = await client.post(
                f"/v2/tasks/{tid}/claim",
                json={"worker_id": "crashed-worker", "pid": 999, "ttl": 2},
            )
            assert claim.status == 200, f"Claim failed: {claim.status}"

            d = await client.get(f"/v2/tasks/{tid}")
            status = (await d.json())["task"]["status"]
            assert status == "running", \
                f"Expected running after claim, got {status}"

            # Wait for TTL to expire (2s TTL)
            await asyncio.sleep(3)

            # Task should be transitioning or already promoted
            d = await client.get(f"/v2/tasks/{tid}")
            data = await d.json()
            assert data["task"]["status"] in ("failed", "running", "ready"), \
                f"Expected lifecycle transition, got {data['task']['status']}"

            # Wait for retry promotion + re-claim
            await asyncio.sleep(3)

            # Now a real worker should be able to complete it
            worker = SimulatedWorker(client, "fault-worker", pid=201)
            await worker.poll_and_execute(max_iterations=15)

            d = await client.get(f"/v2/tasks/{tid}")
            status = (await d.json())["task"]["status"]
            assert status == "completed", \
                f"Expected completed after retry, got {status}"

            # Verify events contain released/failed
            d = await client.get(f"/v2/tasks/{tid}")
            events = (await d.json())["events"]
            event_kinds = [e["kind"] for e in events]
            assert "released" in event_kinds or "failed" in event_kinds, \
                f"Expected released/failed event, got {event_kinds}"

        finally:
            dt.cancel()
            await client.close()

    async def test_retry_exhaustion(self) -> None:
        """After exhausting max_retries, task should stay failed."""
        client, app, dt = await _build_app_with_dispatcher(claim_ttl=1)
        try:
            await asyncio.sleep(0.5)

            # Create task with max_retries=0 (no retries allowed)
            resp = await client.post("/v2/tasks", json={
                "title": "Exhaust Retry",
                "assignee": "exhaust-worker",
                "max_retries": 0,
            })
            tid = (await resp.json())["task"]["id"]

            # Claim the task manually (simulates a crashed worker)
            claim = await client.post(
                f"/v2/tasks/{tid}/claim",
                json={"worker_id": "crashed-worker", "pid": 999, "ttl": 1},
            )
            assert claim.status == 200, f"Claim failed: {claim.status}"

            # Wait for TTL to expire + dispatcher to fail + notice no retries
            await asyncio.sleep(5)

            d = await client.get(f"/v2/tasks/{tid}")
            data = await d.json()
            # After no retries, should be failed and stay failed
            assert data["task"]["status"] == "failed", \
                f"Expected failed after exhaustion, got {data['task']['status']}"

        finally:
            dt.cancel()
            await client.close()


# ---------------------------------------------------------------------------
# E2E Test 3: HITL Insertion
# ---------------------------------------------------------------------------


class TestHITLInsertion:
    """§13.4 — Worker executes → blocks for review → admin comments →
    unblocks → worker continues → completes."""

    async def test_hitl_insertion_e2e(self) -> None:
        """Full HITL flow with dispatcher + simulated worker."""
        client, app, dt = await _build_app_with_dispatcher(claim_ttl=30)
        try:
            await asyncio.sleep(0.5)

            # Create task
            resp = await client.post("/v2/tasks", json={
                "title": "HITL E2E — Code Review Needed",
                "assignee": "hitl-worker",
            })
            tid = (await resp.json())["task"]["id"]

            # Claim the task manually (simulating a polling worker)
            claim = await client.post(
                f"/v2/tasks/{tid}/claim",
                json={"worker_id": "hitl-worker", "pid": 301},
            )
            assert claim.status == 200, f"Claim failed: {claim.status}"

            d = await client.get(f"/v2/tasks/{tid}")
            task = (await d.json())["task"]
            assert task["status"] == "running"
            lock = task["claim_lock"]

            # Worker blocks for review
            block = await client.post(
                f"/v2/tasks/{tid}/block",
                json={"claim_lock": lock, "reason": "需要代码审查"},
            )
            assert block.status == 200
            assert (await block.json())["status"] == "blocked"

            # Reviewer adds comment
            comment = await client.post(
                f"/v2/tasks/{tid}/comment",
                json={"author": "reviewer", "body": "LGTM, 合并后补单元测试"},
            )
            assert comment.status == 201

            # Reviewer unblocks
            unblock = await client.post(
                f"/v2/tasks/{tid}/unblock",
                json={"reason": "代码审查通过"},
            )
            assert unblock.status == 200
            assert (await unblock.json())["status"] == "running"

            # Worker completes
            comp = await client.post(
                f"/v2/tasks/{tid}/complete",
                json={"claim_lock": lock, "summary": "HITL flow completed"},
            )
            assert comp.status == 200

            # Verify final state
            d = await client.get(f"/v2/tasks/{tid}")
            data = await d.json()
            assert data["task"]["status"] == "completed"

            # Verify events contain the full HITL sequence
            event_kinds = [e["kind"] for e in data["events"]]
            for expected in ("blocked", "commented", "unblocked", "completed"):
                assert expected in event_kinds, \
                    f"Missing '{expected}' event in {event_kinds}"

            # Verify comments include block reason + reviewer comment
            comments = data["comments"]
            authors = [c["author"] for c in comments]
            assert "system" in authors, "Missing system comment for block reason"
            assert "reviewer" in authors, "Missing reviewer comment"

        finally:
            dt.cancel()
            await client.close()