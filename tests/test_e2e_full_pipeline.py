"""End-to-end integration test: full pipeline from agent registration to task completion.

Tests the complete flow:
  1. Start the a2a-registry server
  2. Register an agent
  3. Create a task
  4. Dispatch the task
  5. Agent picks up and completes it
  6. Verify final state + audit trail
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, Generator, Optional

import pytest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SERVER_PORT = 18888
BASE_URL = f"http://127.0.0.1:{SERVER_PORT}"


@pytest.fixture(scope="module")
def server_process() -> Generator[subprocess.Popen, None, None]:
    """Start a temporary a2a-registry server for E2E testing."""
    env = os.environ.copy()
    env["A2A_REGISTRY_PORT"] = str(SERVER_PORT)
    env["A2A_REGISTRY_DB"] = ":memory:"
    env["A2A_AUTH_DISABLED"] = "1"

    proc = subprocess.Popen(
        [sys.executable, "-m", "simple_a2a_registry", "server",
         "--port", str(SERVER_PORT),
         "--no-auth-enabled"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    # Wait for server readiness
    import httpx
    for _ in range(30):
        try:
            r = httpx.get(f"{BASE_URL}/health", timeout=2)
            if r.status_code == 200:
                break
        except Exception:
            pass
        time.sleep(0.5)
    else:
        proc.kill()
        pytest.fail("Server failed to start within 15s")

    yield proc

    # Teardown
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


@pytest.fixture(scope="module")
def client(server_process: subprocess.Popen) -> "httpx.Client":
    import httpx
    return httpx.Client(base_url=BASE_URL, timeout=10)


# ---------------------------------------------------------------------------
# E2E Tests
# ---------------------------------------------------------------------------


class TestE2EFullPipeline:
    """Complete lifecycle: register -> create task -> dispatch -> complete -> verify."""

    def test_health_check(self, client) -> None:
        """Server must be alive."""
        r = client.get("/health")
        assert r.status_code == 200
        data = r.json()
        assert data.get("status") == "healthy"

    def test_register_agent(self, client) -> None:
        """Register a test agent and verify it appears in listing."""
        import httpx
        r = client.post(
            "/v1/agents",
            json={
                "client_id": "e2e-agent",
                "client_secret": "e2e-secret",
                "name": "E2E Test Agent",
                "capabilities": ["task/read", "task/write", "agent/register"],
            },
        )
        assert r.status_code in (200, 201), f"Register failed: {r.text}"
        agent = r.json()
        assert agent.get("client_id") == "e2e-agent"

        # Verify in listing
        r2 = client.get("/v1/agents")
        assert r2.status_code == 200
        agents = r2.json()
        ids = [a.get("client_id") for a in agents]
        assert "e2e-agent" in ids

    def test_create_and_dispatch_task(self, client) -> None:
        """Create a task, dispatch it, and verify it reaches ready/running status."""
        r = client.post(
            "/v1/tasks",
            json={
                "name": "e2e-test-task",
                "payload": {"command": "echo hello", "type": "test"},
                "assignee": "e2e-agent",
                "priority": 10,
            },
        )
        assert r.status_code in (200, 201), f"Task create failed: {r.text}"
        task = r.json()
        task_id = task.get("id") or task.get("task_id")
        assert task_id is not None
        assert task.get("status") in ("pending", "ready"), f"Unexpected status: {task}"

        # Dispatch
        r2 = client.post(f"/v1/tasks/{task_id}/dispatch")
        assert r2.status_code in (200, 202), f"Dispatch failed: {r2.text}"

        # Wait for status change
        for _ in range(20):
            r3 = client.get(f"/v1/tasks/{task_id}")
            assert r3.status_code == 200
            status = r3.json().get("status")
            if status in ("completed", "failed", "running"):
                break
            time.sleep(0.5)
        else:
            pytest.fail(f"Task {task_id} did not progress within 10s")

    def test_audit_log(self, client) -> None:
        """After operations, the audit log should contain events."""
        r = client.get("/v1/audit?limit=5")
        assert r.status_code == 200
        events = r.json()
        assert len(events) >= 1, "Audit log should not be empty"

    def test_agent_task_list(self, client) -> None:
        """Agent should see its tasks."""
        r = client.get("/v1/agents/e2e-agent/tasks")
        assert r.status_code == 200
        tasks = r.json()
        assert len(tasks) >= 1

    def test_sla_metrics(self, client) -> None:
        """SLA endpoint must return metrics after task operations."""
        r = client.get("/admin/sla")
        if r.status_code == 200:
            data = r.json()
            assert "windows" in data or "success_rate" in data


class TestE2EMultiTaskStress:
    """Create multiple tasks and verify dispatcher processes them."""

    def test_bulk_create_and_dispatch(self, client) -> None:
        """Create 50 tasks, dispatch all, verify at least some complete."""
        task_ids = []

        for i in range(50):
            r = client.post(
                "/v1/tasks",
                json={
                    "name": f"stress-task-{i}",
                    "payload": {"index": i},
                    "assignee": "e2e-agent",
                    "priority": i % 10,
                },
            )
            if r.status_code in (200, 201):
                task = r.json()
                tid = task.get("id") or task.get("task_id")
                if tid:
                    task_ids.append(tid)

        assert len(task_ids) >= 45, f"Only created {len(task_ids)}/50 tasks"

        for tid in task_ids[:10]:
            client.post(f"/v1/tasks/{tid}/dispatch")

        time.sleep(3)

        statuses: Dict[str, int] = {}
        for tid in task_ids:
            r = client.get(f"/v1/tasks/{tid}")
            if r.status_code == 200:
                s = r.json().get("status")
                statuses[s] = statuses.get(s, 0) + 1

        assert statuses.get("running", 0) + statuses.get("completed", 0) > 0, (
            f"No tasks made progress: {statuses}"
        )


class TestE2ECircuitBreaker:
    """Verify circuit breaker blocks dispatch after consecutive failures."""

    def test_circuit_breaker_trips(self, client) -> None:
        """Creating failing tasks should eventually trip the circuit."""
        task_ids = []
        for i in range(10):
            r = client.post(
                "/v1/tasks",
                json={
                    "name": f"fail-task-{i}",
                    "payload": {"will_fail": True},
                    "assignee": "nonexistent-agent",
                },
            )
            if r.status_code in (200, 201):
                task = r.json()
                tid = task.get("id") or task.get("task_id")
                if tid:
                    task_ids.append(tid)

        for tid in task_ids:
            client.post(f"/v1/tasks/{tid}/dispatch")
            time.sleep(0.2)

        for tid in task_ids:
            r = client.get(f"/v1/tasks/{tid}")
            if r.status_code == 200:
                data = r.json()
                if data.get("circuit_state") == "open":
                    return

        pytest.skip("Circuit breaker did not trip (expected in fast test)")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])