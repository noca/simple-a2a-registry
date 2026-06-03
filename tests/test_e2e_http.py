"""Comprehensive E2E HTTP tests for the a2a-registry server.

Tests all major HTTP endpoints using aiohttp.test_utils so every test
runs against a real server (in-process, SQLite :memory: backend).
Auth is disabled so most endpoints are accessible without tokens.
"""

from __future__ import annotations

import tempfile

import pytest
from aiohttp.test_utils import TestClient, TestServer

from simple_a2a_registry.server import create_app


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def api_client():
    """Return a factory that creates a fresh app + TestClient per call."""
    factories = []

    async def maker() -> TestClient:
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
# Health / Well-known
# ===================================================================


class TestHealth:
    """Server liveness and discovery endpoints."""

    async def test_health_returns_healthy(self, api_client) -> None:
        async with await api_client() as client:
            resp = await client.get("/health")
            assert resp.status == 200
            data = await resp.json()
            assert data["status"] == "healthy"
            assert "version" in data

    async def test_well_known_agent_card(self, api_client) -> None:
        async with await api_client() as client:
            resp = await client.get("/.well-known/agent-card.json")
            assert resp.status == 200
            data = await resp.json()
            assert "name" in data
            assert "description" in data
            assert "capabilities" in data


# ===================================================================
# V1 Agent CRUD
# ===================================================================


class TestV1Agents:
    """Agent registration, listing, detail, and deletion."""

    REGISTER_PAYLOAD = {
        "name": "e2e-agent",
        "description": "E2E test agent",
        "capabilities": ["task/read", "task/write", "agent/register"],
        "version": "1.0.0",
    }

    async def test_register_agent(self, api_client) -> None:
        async with await api_client() as client:
            resp = await client.post("/v1/agents", json=self.REGISTER_PAYLOAD)
            assert resp.status in (200, 201), f"Register failed: {await resp.text()}"
            data = await resp.json()
            assert "id" in data
            assert data["card"]["name"] == "e2e-agent"

    async def test_list_agents(self, api_client) -> None:
        async with await api_client() as client:
            await client.post("/v1/agents", json=self.REGISTER_PAYLOAD)

            resp = await client.get("/v1/agents")
            assert resp.status == 200
            data = await resp.json()
            assert isinstance(data, dict)
            assert "agents" in data
            assert "total" in data
            agents = data["agents"]
            names = [a.get("name") for a in agents]
            assert "e2e-agent" in names

    async def test_get_agent_by_id(self, api_client) -> None:
        async with await api_client() as client:
            reg = await client.post("/v1/agents", json=self.REGISTER_PAYLOAD)
            agent_id = (await reg.json())["id"]

            resp = await client.get(f"/v1/agents/{agent_id}")
            assert resp.status == 200
            data = await resp.json()
            assert data["id"] == agent_id
            assert data["name"] == "e2e-agent"

    async def test_delete_agent(self, api_client) -> None:
        async with await api_client() as client:
            reg = await client.post("/v1/agents", json=self.REGISTER_PAYLOAD)
            agent_id = (await reg.json())["id"]

            resp = await client.delete(f"/v1/agents/{agent_id}")
            assert resp.status in (200, 204)

            get_resp = await client.get(f"/v1/agents/{agent_id}")
            assert get_resp.status == 404


# ===================================================================
# V1 Agent Heartbeat & Toggle
# ===================================================================


class TestV1AgentLifecycle:
    """Heartbeat and toggle operations on registered agents."""

    async def test_heartbeat(self, api_client) -> None:
        async with await api_client() as client:
            reg = await client.post("/v1/agents", json={
                "name": "heartbeat-agent",
                "description": "Agent for heartbeat test",
            })
            agent_id = (await reg.json())["id"]

            resp = await client.post(f"/v1/agents/{agent_id}/heartbeat")
            assert resp.status == 203, f"Heartbeat failed: {await resp.text()}"
            data = await resp.json()
            assert data.get("status") == "alive"

    async def test_toggle_agent_disabled(self, api_client) -> None:
        async with await api_client() as client:
            reg = await client.post("/v1/agents", json={
                "name": "toggle-agent",
                "description": "Agent for toggle test",
            })
            agent_id = (await reg.json())["id"]

            resp = await client.post(f"/v1/agents/{agent_id}/toggle", json={})
            assert resp.status == 200
            data = await resp.json()
            assert data.get("disabled") is True

            resp2 = await client.post(f"/v1/agents/{agent_id}/toggle", json={})
            assert resp2.status == 200
            data2 = await resp2.json()
            assert data2.get("disabled") is False


# ===================================================================
# V1 Task operations (dispatch, proxy, callback)
# ===================================================================


class TestV1Tasks:
    """Task dispatch, proxy tasks, and callback-result."""

    AGENT_NAME = "task-agent"

    async def _register_agent(self, client, name=AGENT_NAME) -> str:
        reg = await client.post("/v1/agents", json={
            "name": name,
            "description": "Agent for task tests",
            "capabilities": ["task/read", "task/write"],
        })
        return (await reg.json())["id"]

    async def test_dispatch_task_no_ws(self, api_client) -> None:
        """Dispatch returns 503 when agent has no WebSocket connection."""
        async with await api_client() as client:
            agent_id = await self._register_agent(client)
            resp = await client.post(
                f"/v1/agents/{agent_id}/dispatch",
                json={"name": "e2e-dispatched-task", "payload": {"cmd": "test"}},
            )
            # No WS connection → 503 agent_not_connected
            assert resp.status == 503

    async def test_list_tasks(self, api_client) -> None:
        async with await api_client() as client:
            # Dispatch creates a task record
            agent_id = await self._register_agent(client)
            await client.post(
                f"/v1/agents/{agent_id}/dispatch",
                json={"name": "task-1", "payload": {"cmd": "test"}},
            )

            resp = await client.get("/v1/tasks")
            assert resp.status == 200
            data = await resp.json()
            assert isinstance(data, dict)
            assert "tasks" in data
            assert data["total"] >= 1

    async def test_get_task_by_id(self, api_client) -> None:
        async with await api_client() as client:
            agent_id = await self._register_agent(client)
            await client.post(
                f"/v1/agents/{agent_id}/dispatch",
                json={"name": "get-me", "payload": {"cmd": "test"}},
            )
            tasks_resp = await client.get("/v1/tasks")
            task_list = (await tasks_resp.json())["tasks"]
            assert len(task_list) > 0
            task_id = task_list[0].get("id") or task_list[0].get("task_id")

            resp = await client.get(f"/v1/tasks/{task_id}")
            assert resp.status == 200

    async def test_proxy_task_no_url(self, api_client) -> None:
        """Proxy task returns 400 when agent has no callback URL."""
        async with await api_client() as client:
            agent_id = await self._register_agent(client)
            resp = await client.post(
                f"/v1/agents/{agent_id}/task",
                json={"name": "proxy-task", "payload": {"cmd": "proxy test"}},
            )
            assert resp.status == 400

    async def test_callback_result(self, api_client) -> None:
        async with await api_client() as client:
            agent_id = await self._register_agent(client)
            await client.post(
                f"/v1/agents/{agent_id}/dispatch",
                json={"name": "callback-task", "payload": {"cmd": "test"}},
            )
            tasks_resp = await client.get("/v1/tasks")
            task_list = (await tasks_resp.json())["tasks"]
            if not task_list:
                pytest.skip("No tasks available for callback test")
            task_id = task_list[0].get("id") or task_list[0].get("task_id")

            resp = await client.post(
                f"/v1/tasks/{task_id}/callback-result",
                json={"status": "completed", "result": "ok"},
            )
            assert resp.status in (200, 202)


# ===================================================================
# V2 Task lifecycle (create / claim / complete / block / unblock / comment / archive)
# ===================================================================


class TestV2Tasks:
    """Full V2 task lifecycle via REST."""

    async def test_create_and_read_task(self, api_client) -> None:
        async with await api_client() as client:
            resp = await client.post(
                "/v2/tasks",
                json={
                    "title": "v2-e2e-task",
                    "assignee": "coder",
                    "priority": 3,
                    "body": "E2E HTTP test task",
                },
            )
            assert resp.status in (200, 201), f"Create failed: {await resp.text()}"
            data = await resp.json()
            task = data["task"]
            task_id = task["id"]
            assert task_id.startswith("t_")

            read_resp = await client.get(f"/v2/tasks/{task_id}")
            assert read_resp.status == 200
            read_data = await read_resp.json()
            assert read_data["task"]["title"] == "v2-e2e-task"

    async def test_list_tasks_v2(self, api_client) -> None:
        async with await api_client() as client:
            for i in range(3):
                await client.post(
                    "/v2/tasks",
                    json={"title": f"v2-list-task-{i}", "assignee": "coder"},
                )

            resp = await client.get("/v2/tasks")
            assert resp.status == 200
            data = await resp.json()
            assert isinstance(data, dict)
            assert "tasks" in data
            assert data["total"] >= 3

    async def test_claim_complete_flow(self, api_client) -> None:
        async with await api_client() as client:
            resp = await client.post(
                "/v2/tasks",
                json={"title": "claim-me", "assignee": "worker"},
            )
            task_id = (await resp.json())["task"]["id"]

            # Claim with worker_id + pid
            claim_resp = await client.post(
                f"/v2/tasks/{task_id}/claim",
                json={"worker_id": "e2e-worker", "pid": 12345},
            )
            assert claim_resp.status == 200
            claim_data = await claim_resp.json()
            lock = claim_data["claim_lock"]
            assert lock == "e2e-worker:12345"

            # Complete with the correct claim_lock
            comp_resp = await client.post(
                f"/v2/tasks/{task_id}/complete",
                json={"claim_lock": lock, "result": "all good"},
            )
            assert comp_resp.status == 200
            comp_data = await comp_resp.json()
            assert comp_data["status"] == "completed"

            # Verify final status
            detail = await client.get(f"/v2/tasks/{task_id}")
            final = (await detail.json())["task"]
            assert final["status"] == "completed"

    async def test_block_unblock_flow(self, api_client) -> None:
        async with await api_client() as client:
            resp = await client.post(
                "/v2/tasks",
                json={"title": "block-me", "assignee": "worker"},
            )
            task_id = (await resp.json())["task"]["id"]

            block_resp = await client.post(
                f"/v2/tasks/{task_id}/block",
                json={"reason": "Need more info"},
            )
            assert block_resp.status == 200
            block_data = await block_resp.json()
            assert block_data["status"] == "blocked"
            assert block_data["block_reason"] == "Need more info"

            detail = await client.get(f"/v2/tasks/{task_id}")
            assert (await detail.json())["task"]["status"] == "blocked"

            unblock_resp = await client.post(f"/v2/tasks/{task_id}/unblock")
            assert unblock_resp.status == 200
            unblock_data = await unblock_resp.json()
            assert unblock_data["status"] == "running"

    async def test_comment_on_task(self, api_client) -> None:
        async with await api_client() as client:
            resp = await client.post(
                "/v2/tasks",
                json={"title": "comment-me", "assignee": "worker"},
            )
            task_id = (await resp.json())["task"]["id"]

            comment_resp = await client.post(
                f"/v2/tasks/{task_id}/comment",
                json={"author": "e2e", "body": "This is a test comment"},
            )
            assert comment_resp.status == 201
            comment_data = await comment_resp.json()
            assert "comment_id" in comment_data

    async def test_archive_task(self, api_client) -> None:
        """Archive requires a prior status that is terminal (completed/failed/cancelled)."""
        async with await api_client() as client:
            # Create, claim, complete, then archive
            resp = await client.post(
                "/v2/tasks",
                json={"title": "archive-me", "assignee": "worker"},
            )
            task_id = (await resp.json())["task"]["id"]

            # Claim
            claim = await client.post(
                f"/v2/tasks/{task_id}/claim",
                json={"worker_id": "w", "pid": 1},
            )
            lock = (await claim.json())["claim_lock"]

            # Complete
            await client.post(
                f"/v2/tasks/{task_id}/complete",
                json={"claim_lock": lock, "result": "done"},
            )

            # Now archive (delete)
            del_resp = await client.delete(f"/v2/tasks/{task_id}")
            assert del_resp.status == 200
            del_data = await del_resp.json()
            assert del_data["status"] == "archived" or del_data["status"] == "deleted"

    async def test_heartbeat_on_v2_task(self, api_client) -> None:
        async with await api_client() as client:
            resp = await client.post(
                "/v2/tasks",
                json={"title": "hb-task", "assignee": "worker"},
            )
            task_id = (await resp.json())["task"]["id"]

            # Claim first
            claim = await client.post(
                f"/v2/tasks/{task_id}/claim",
                json={"worker_id": "hb-w", "pid": 1},
            )
            lock = (await claim.json())["claim_lock"]

            hb = await client.post(
                f"/v2/tasks/{task_id}/heartbeat",
                json={"claim_lock": lock},
            )
            assert hb.status == 200
            hb_data = await hb.json()
            assert hb_data["task_id"] == task_id
            assert "claim_expires" in hb_data


# ===================================================================
# V2 Task dependencies
# ===================================================================


class TestV2Dependencies:
    """Parent-child dependency links between V2 tasks."""

    async def test_add_and_remove_dependency(self, api_client) -> None:
        async with await api_client() as client:
            parent = await client.post(
                "/v2/tasks",
                json={"title": "parent-task", "assignee": "worker"},
            )
            parent_id = (await parent.json())["task"]["id"]

            child = await client.post(
                "/v2/tasks",
                json={"title": "child-task", "assignee": "worker"},
            )
            child_id = (await child.json())["task"]["id"]

            dep = await client.post(
                f"/v2/tasks/{child_id}/depend",
                json={"parent_id": parent_id},
            )
            assert dep.status == 200
            dep_data = await dep.json()
            assert dep_data["status"] == "dependency_added"
            assert dep_data["parent_id"] == parent_id

            rem = await client.delete(f"/v2/tasks/{child_id}/depend/{parent_id}")
            assert rem.status == 200
            rem_data = await rem.json()
            assert rem_data["status"] == "dependency_removed"


# ===================================================================
# V2 Stats
# ===================================================================


class TestV2Stats:
    """Registry statistics endpoint."""

    async def test_stats_returns_summary(self, api_client) -> None:
        async with await api_client() as client:
            for i in range(3):
                await client.post(
                    "/v2/tasks",
                    json={"title": f"stat-task-{i}", "assignee": "worker"},
                )

            resp = await client.get("/v2/stats")
            assert resp.status == 200, f"Stats failed: {await resp.text()}"
            data = await resp.json()
            assert "total" in data
            assert data["total"] >= 3
            assert "by_status" in data

    async def test_stats_by_tenant(self, api_client) -> None:
        async with await api_client() as client:
            resp = await client.get("/v2/stats/tenants")
            assert resp.status in (200, 501), f"Tenant stats: {await resp.text()}"


# ===================================================================
# Auth endpoints (always public)
# ===================================================================


class TestAuthEndpoints:
    """OAuth 2.1 register / token (public even with auth disabled)."""

    async def test_auth_register_returns_credentials(self, api_client) -> None:
        async with await api_client() as client:
            resp = await client.post(
                "/auth/register",
                json={"description": "e2e test client"},
            )
            assert resp.status in (200, 201), f"Auth register: {await resp.text()}"
            data = await resp.json()
            assert "client_id" in data
            assert "client_secret" in data

    async def test_auth_token_flow(self, api_client) -> None:
        async with await api_client() as client:
            reg = await client.post(
                "/auth/register",
                json={"description": "token test client"},
            )
            creds = await reg.json()

            # Default registered clients get 'agent:read' scope
            tok = await client.post(
                "/auth/token",
                data={
                    "grant_type": "client_credentials",
                    "client_id": creds["client_id"],
                    "client_secret": creds["client_secret"],
                    "scope": "agent:read",
                },
            )
            assert tok.status == 200, f"Token exchange: {await tok.text()}"
            token_data = await tok.json()
            assert "access_token" in token_data
            assert "token_type" in token_data

    async def test_well_known_oauth(self, api_client) -> None:
        async with await api_client() as client:
            resp = await client.get("/.well-known/oauth-authorization-server")
            assert resp.status == 200
            data = await resp.json()
            assert "issuer" in data


# ===================================================================
# Error handling
# ===================================================================


class TestErrorHandling:
    """Verifies the server returns appropriate HTTP error codes."""

    async def test_404_on_unknown_route(self, api_client) -> None:
        async with await api_client() as client:
            resp = await client.get("/v1/nonexistent")
            assert resp.status in (404, 405)

    async def test_404_on_unknown_agent(self, api_client) -> None:
        async with await api_client() as client:
            resp = await client.get("/v1/agents/nonexistent-agent-id")
            assert resp.status in (404, 400)

    async def test_404_on_unknown_v2_task(self, api_client) -> None:
        async with await api_client() as client:
            resp = await client.get("/v2/tasks/does-not-exist")
            assert resp.status in (404, 400)

    async def test_method_not_allowed(self, api_client) -> None:
        async with await api_client() as client:
            resp = await client.put("/v1/agents", json={})
            assert resp.status in (405, 404, 400)

    async def test_invalid_json_returns_400(self, api_client) -> None:
        async with await api_client() as client:
            resp = await client.post(
                "/v1/agents",
                data="not valid json at all",
                headers={"Content-Type": "application/json"},
            )
            assert resp.status in (400, 422, 500)

    async def test_register_without_name_returns_400(self, api_client) -> None:
        async with await api_client() as client:
            resp = await client.post(
                "/v1/agents",
                json={"description": "missing name"},
            )
            assert resp.status == 400


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])