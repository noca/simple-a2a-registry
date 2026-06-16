"""Locust load test script for Simple A2A Registry.

Simulates realistic agent workloads against the deployed registry:

- Agent registration / lookup
- Task creation, contention, claim, complete
- WebSocket connection management

Usage:
    # Standalone (no Docker — runs from wd)::
    locust -f locustfile.py --headless -u 10 -r 2 --run-time 60s \\
        --host http://localhost:8321

    # Web UI (browse to http://localhost:8089)::
    locust -f locustfile.py --host http://localhost:8321

Requires locust: pip install locust
"""
from __future__ import annotations

import json
import random
from typing import Any, Dict

from locust import HttpUser, task, between, events


class RegistryUser(HttpUser):
    """Simulates an agent interacting with the A2A Registry.

    Wait 1–3 seconds between task executions to simulate
    real-world agent processing time.
    """
    wait_time = between(1, 3)

    # Shared state across all users for this host
    _agent_id: str = ""
    _known_tasks: list[str] = []

    def on_start(self) -> None:
        """Register the agent on startup and create initial tasks."""
        self._agent_id = f"locust-agent-{random.randint(1000, 9999)}"

        # Register the agent (v1)
        with self.client.post(
            "/agents",
            json={
                "agent_id": self._agent_id,
                "display_name": f"Locust {self._agent_id}",
                "capabilities": ["task.execution"],
            },
            catch_response=True,
        ) as resp:
            if resp.status_code == 201:
                resp.success()
            elif resp.status_code == 409:
                # Already registered — acceptable
                resp.success()
            else:
                resp.failure(f"Agent register failed: {resp.status_code}")
                self._agent_id = ""
                return

    # ------------------------------------------------------------------
    # Task workload
    # ------------------------------------------------------------------

    @task(3)
    def create_task(self) -> None:
        """Create a task via the V1 API."""
        if not self._agent_id:
            return
        payload = {
            "title": f"locust-{random.randint(10000, 99999)}",
            "assignee": self._agent_id,
            "priority": random.randint(0, 20),
            "description": "Load test task",
        }
        with self.client.post(
            "/v2/tasks",
            json=payload,
            catch_response=True,
        ) as resp:
            if resp.status_code == 201:
                data = resp.json()
                task_id = data.get("task", {}).get("id")
                if task_id:
                    RegistryUser._known_tasks.append(task_id)
                resp.success()
            elif resp.status_code == 429:
                resp.success()  # Rate limited — acceptable under load
            else:
                resp.failure(f"Create task: {resp.status_code}")

    @task(2)
    def claim_and_complete(self) -> None:
        """Claim a known task and complete it."""
        if not RegistryUser._known_tasks:
            return
        task_id = random.choice(RegistryUser._known_tasks)

        # Claim
        with self.client.post(
            f"/v2/tasks/{task_id}/claim",
            json={"worker_id": self._agent_id, "pid": random.randint(1000, 9999)},
            catch_response=True,
        ) as resp:
            if resp.status_code == 200:
                data = resp.json()
                claim_lock = data.get("claim_lock", "")
                if claim_lock:
                    # Complete
                    with self.client.post(
                        f"/v2/tasks/{task_id}/complete",
                        json={
                            "claim_lock": claim_lock,
                            "summary": "locust completed",
                        },
                        catch_response=True,
                    ) as comp_resp:
                        if comp_resp.status_code == 200:
                            comp_resp.success()
                        else:
                            comp_resp.failure(
                                f"Complete task {task_id}: {comp_resp.status_code}"
                            )
                resp.success()
            elif resp.status_code == 409:
                resp.success()  # Already claimed — expected under contention
            elif resp.status_code == 404:
                # Task may have been claimed/deleted — remove from known list
                RegistryUser._known_tasks = [
                    t for t in RegistryUser._known_tasks if t != task_id
                ]
                resp.success()
            else:
                resp.failure(f"Claim task {task_id}: {resp.status_code}")

    @task(1)
    def list_tasks(self) -> None:
        """List tasks — various filters."""
        params = {}
        if random.random() < 0.3:
            params["status"] = random.choice(["pending", "running", "completed"])
        if random.random() < 0.2:
            params["limit"] = str(random.choice([10, 50, 100]))

        with self.client.get(
            "/v2/tasks",
            params=params,
            catch_response=True,
        ) as resp:
            if resp.status_code == 200:
                resp.success()
            else:
                resp.failure(f"List tasks: {resp.status_code}")

    @task(1)
    def get_task_detail(self) -> None:
        """Fetch a single task's full detail."""
        if not RegistryUser._known_tasks:
            return
        task_id = random.choice(RegistryUser._known_tasks)
        with self.client.get(
            f"/v2/tasks/{task_id}",
            catch_response=True,
        ) as resp:
            if resp.status_code == 200:
                resp.success()
            elif resp.status_code == 404:
                RegistryUser._known_tasks = [
                    t for t in RegistryUser._known_tasks if t != task_id
                ]
                resp.success()
            else:
                resp.failure(f"Get task {task_id}: {resp.status_code}")

    @task(1)
    def health_check(self) -> None:
        """Check liveness."""
        with self.client.get("/health", catch_response=True) as resp:
            if resp.status_code == 200:
                resp.success()
            else:
                resp.failure(f"Health: {resp.status_code}")


#
# WebSocket workload (optional — requires aiohttp-ws / admin_ws_hub)
#

# Uncomment if the Admin WebSocket hub is available:
#
# import asyncio
# import aiohttp
#
# class WebSocketUser(HttpUser):
#     wait_time = between(5, 15)
#
#     @task
#     def ws_connect(self) -> None:
#         url = self.host.replace("http://", "ws://").replace("https://", "wss://")
#         url += "/ws/admin"
#
#         async def _run():
#             async with aiohttp.ClientSession() as session:
#                 async with session.ws_connect(url, timeout=aiohttp.ClientWSTimeout(ws_close=5)) as ws:
#                     # Subscribe to all events
#                     await ws.send_json({"action": "subscribe", "events": ["*"]})
#                     # Listen for a few seconds
#                     await asyncio.sleep(5)
#
#         asyncio.run(_run())
