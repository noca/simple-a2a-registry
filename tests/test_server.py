"""Integration tests for the Simple A2A Registry HTTP Server."""
from __future__ import annotations

import json
import time
import tempfile

import pytest
from aiohttp.test_utils import TestServer, TestClient

from simple_a2a_registry.server import create_app
from simple_a2a_registry.store import HEARTBEAT_TIMEOUT

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


class TestHealth:
    async def test_health_returns_healthy(self, app_factory):
        async with await app_factory() as client:
            resp = await client.get("/health")
            assert resp.status == 200
            data = await resp.json()
            assert data["status"] == "healthy"
            assert "uptime_seconds" in data
            assert data["version"] == "1.0.0"

    async def test_health_includes_stats(self, app_factory):
        async with await app_factory() as client:
            await client.post("/v1/agents", json={"name": "Stats Test"})
            resp = await client.get("/health")
            data = await resp.json()
            assert data["stats"]["total_agents"] >= 1
            assert data["stats"]["alive_agents"] >= 1


class TestWellKnown:
    async def test_well_known_returns_card(self, app_factory):
        async with await app_factory() as client:
            resp = await client.get("/.well-known/agent-card.json")
            assert resp.status == 200
            data = await resp.json()
            assert data["id"] == "simple-a2a-registry"
            assert data["name"] == "Simple A2A Registry"

    async def test_well_known_cache_header(self, app_factory):
        async with await app_factory() as client:
            resp = await client.get("/.well-known/agent-card.json")
            cc = resp.headers.get("Cache-Control", "")
            assert "max-age=300" in cc


class TestListAgents:
    async def test_empty_list(self, app_factory):
        async with await app_factory() as client:
            resp = await client.get("/v1/agents")
            assert resp.status == 200
            data = await resp.json()
            assert data["total"] == 0
            assert data["agents"] == []

    async def test_list_with_registered_agents(self, app_factory):
        async with await app_factory() as client:
            await client.post("/v1/agents", json={"name": "Alpha"})
            await client.post("/v1/agents", json={"name": "Beta"})
            resp = await client.get("/v1/agents")
            data = await resp.json()
            assert data["total"] == 2

    async def test_list_filter_by_skill(self, app_factory):
        async with await app_factory() as client:
            await client.post("/v1/agents", json={
                "name": "Skilled",
                "description": "Has skills",
                "skills": [{"id": "s1", "name": "Data Analysis", "description": "desc", "tags": ["data"]}],
            })
            resp = await client.get("/v1/agents?skill=Data Analysis")
            data = await resp.json()
            assert data["total"] == 1

    async def test_list_filter_by_tag(self, app_factory):
        # AgentCard v1.0 no longer has tags — query param ignored
        # (backward compat: tag filter still checks old dict field)
        async with await app_factory() as client:
            await client.post("/v1/agents", json={"name": "Tagged", "description": "No tags in v1.0"})
            resp = await client.get("/v1/agents?tag=python")
            data = await resp.json()
            assert data["total"] == 0

    async def test_list_search(self, app_factory):
        async with await app_factory() as client:
            await client.post("/v1/agents", json={"name": "Searchable", "description": "keyword here"})
            resp = await client.get("/v1/agents?q=keyword")
            data = await resp.json()
            assert data["total"] == 1

    async def test_list_pagination(self, app_factory):
        async with await app_factory() as client:
            for i in range(5):
                await client.post("/v1/agents", json={"name": f"Agent-{i}"})
            resp = await client.get("/v1/agents?limit=2&offset=1")
            data = await resp.json()
            assert len(data["agents"]) == 2
            assert data["limit"] == 2
            assert data["offset"] == 1

    async def test_list_max_limit(self, app_factory):
        async with await app_factory() as client:
            resp = await client.get("/v1/agents?limit=9999")
            data = await resp.json()
            assert data["limit"] == 200

    async def test_list_have_status(self, app_factory):
        async with await app_factory() as client:
            await client.post("/v1/agents", json={"name": "Live Check"})
            resp = await client.get("/v1/agents")
            data = await resp.json()
            for agent in data["agents"]:
                assert "status" in agent


class TestGetAgent:
    async def test_get_existing_agent(self, app_factory):
        async with await app_factory() as client:
            post_resp = await client.post("/v1/agents", json={"name": "Find Me", "description": "Findable agent"})
            agent_id = (await post_resp.json())["id"]
            resp = await client.get(f"/v1/agents/{agent_id}")
            assert resp.status == 200
            data = await resp.json()
            assert data["name"] == "Find Me"
            assert data["description"] == "Findable agent"

    async def test_get_nonexistent_agent_404(self, app_factory):
        async with await app_factory() as client:
            resp = await client.get("/v1/agents/nobody-here")
            assert resp.status == 404
            data = await resp.json()
            assert data["error"] == "agent_not_found"


class TestRegisterAgent:
    async def test_register_success(self, app_factory):
        async with await app_factory() as client:
            resp = await client.post("/v1/agents", json={
                "name": "New Agent", "description": "A test agent",
            })
            assert resp.status == 201
            data = await resp.json()
            assert "id" in data
            assert data["message"] == "Agent registered successfully"

    async def test_register_missing_name_400(self, app_factory):
        async with await app_factory() as client:
            resp = await client.post("/v1/agents", json={"description": "No name"})
            assert resp.status == 400

    async def test_register_empty_name_400(self, app_factory):
        async with await app_factory() as client:
            resp = await client.post("/v1/agents", json={"name": ""})
            assert resp.status == 400

    async def test_register_invalid_json_400(self, app_factory):
        async with await app_factory() as client:
            resp = await client.post("/v1/agents", data=b"not json", headers={"Content-Type": "application/json"})
            assert resp.status == 400

    async def test_register_duplicate_name_409(self, app_factory):
        async with await app_factory() as client:
            await client.post("/v1/agents", json={"name": "Unique Name"})
            resp = await client.post("/v1/agents", json={"name": "Unique Name"})
            assert resp.status == 409

    async def test_register_with_full_capabilities(self, app_factory):
        async with await app_factory() as client:
            resp = await client.post("/v1/agents", json={
                "name": "Capable Agent",
                "description": "Has skills",
                "skills": [{"id": "s1", "name": "Skill One", "description": "desc", "tags": []},
                           {"id": "s2", "name": "Skill Two", "description": "desc", "tags": []}],
            })
            assert resp.status == 201
            data = await resp.json()
            assert len(data["card"]["skills"]) == 2


class TestHeartbeat:
    async def test_heartbeat_success(self, app_factory):
        async with await app_factory() as client:
            post_resp = await client.post("/v1/agents", json={"name": "Heartbeat Me"})
            agent_id = (await post_resp.json())["id"]
            resp = await client.post(f"/v1/agents/{agent_id}/heartbeat")
            assert resp.status == 203
            data = await resp.json()
            assert data["status"] == "alive"
            assert data["stale_timeout"] == HEARTBEAT_TIMEOUT

    async def test_heartbeat_not_found_404(self, app_factory):
        async with await app_factory() as client:
            resp = await client.post("/v1/agents/nobody/heartbeat")
            assert resp.status == 404

    async def test_heartbeat_stale_returns_410(self, app_factory):
        async with await app_factory() as client:
            post_resp = await client.post("/v1/agents", json={"name": "Stale Test"})
            agent_id = (await post_resp.json())["id"]
            app = client.server.app
            app["store"]._heartbeats[agent_id] = time.time() - HEARTBEAT_TIMEOUT - 10
            resp = await client.post(f"/v1/agents/{agent_id}/heartbeat")
            assert resp.status == 410


class TestUnregister:
    async def test_unregister_success(self, app_factory):
        async with await app_factory() as client:
            post_resp = await client.post("/v1/agents", json={"name": "Remove Me"})
            agent_id = (await post_resp.json())["id"]
            resp = await client.delete(f"/v1/agents/{agent_id}")
            assert resp.status == 200
            get_resp = await client.get(f"/v1/agents/{agent_id}")
            assert get_resp.status == 404

    async def test_unregister_not_found_404(self, app_factory):
        async with await app_factory() as client:
            resp = await client.delete("/v1/agents/nonexistent")
            assert resp.status == 404


class TestWebSocket:
    """WebSocket connection tests — agents register persistent connections."""

    async def test_ws_connect_and_ping_pong(self, app_factory):
        """Agent connects via WS, exchanges ping/pong."""
        async with await app_factory() as client:
            # Register first
            post_resp = await client.post("/v1/agents", json={"name": "WS Agent"})
            agent_id = (await post_resp.json())["id"]

            # Connect via WebSocket
            ws = await client.ws_connect(f"/v1/agents/{agent_id}/ws")
            assert not ws.closed

            await ws.send_json({"type": "ping"})
            msg = await ws.receive_json()
            assert msg["type"] == "pong"

            # Clean close
            await ws.send_json({"type": "close"})
            await ws.close()
            assert ws.closed

    async def test_ws_replaces_old_connection(self, app_factory):
        """Second WS connection replaces the first."""
        async with await app_factory() as client:
            post_resp = await client.post("/v1/agents", json={"name": "Reconnect Agent"})
            agent_id = (await post_resp.json())["id"]

            ws1 = await client.ws_connect(f"/v1/agents/{agent_id}/ws")
            ws2 = await client.ws_connect(f"/v1/agents/{agent_id}/ws")

            # First connection should receive close signal
            msg = await ws1.receive_json(timeout=5)
            assert msg["type"] == "close"
            assert msg["reason"] == "replaced"

            await ws1.close()
            await ws2.close()

    async def test_ws_marks_agent_alive_in_list(self, app_factory):
        """Connected WS agents show as alive in /v1/agents listing."""
        async with await app_factory() as client:
            post_resp = await client.post("/v1/agents", json={"name": "Live WS"})
            agent_id = (await post_resp.json())["id"]

            ws = await client.ws_connect(f"/v1/agents/{agent_id}/ws")

            resp = await client.get("/v1/agents")
            data = await resp.json()
            for a in data["agents"]:
                if a["id"] == agent_id:
                    assert a.get("connection") == "websocket"
                    assert a["status"] == "alive"

            await ws.send_json({"type": "close"})
            await ws.close()

    async def test_ws_unregistered_agent_404(self, app_factory):
        """Non-registered agent cannot connect via WS."""
        async with await app_factory() as client:
            resp = await client.get("/v1/agents/nobody/ws")
            assert resp.status == 404
            data = await resp.json()
            assert data["error"] == "agent_not_found"

    async def test_ws_agent_sends_task_result(self, app_factory):
        """Agent sends completed/failed task results back via WS."""
        async with await app_factory() as client:
            post_resp = await client.post("/v1/agents", json={"name": "Result Agent"})
            agent_id = (await post_resp.json())["id"]

            ws = await client.ws_connect(f"/v1/agents/{agent_id}/ws")

            # Simulate agent task completion
            task_id = "test-task-1"
            await ws.send_json({
                "type": "task_result",
                "id": task_id,
                "status": "completed",
                "result": {"text": "done"},
            })

            # Check registry stored it
            resp = await client.get(f"/v1/tasks/{task_id}")
            data = await resp.json()
            assert data["state"] == "completed"
            assert data["result"]["text"] == "done"

            await ws.send_json({"type": "close"})
            await ws.close()

    async def test_ws_agent_sends_task_progress(self, app_factory):
        """Agent sends progress updates via WS."""
        async with await app_factory() as client:
            post_resp = await client.post("/v1/agents", json={"name": "Progress Agent"})
            agent_id = (await post_resp.json())["id"]

            ws = await client.ws_connect(f"/v1/agents/{agent_id}/ws")

            task_id = "test-task-progress"
            await ws.send_json({
                "type": "task_progress",
                "id": task_id,
                "status": "working",
            })
            resp = await client.get(f"/v1/tasks/{task_id}")
            data = await resp.json()
            assert data["state"] == "working"

            await ws.send_json({
                "type": "task_result",
                "id": task_id,
                "status": "completed",
                "result": {"text": "progress done"},
            })
            resp = await client.get(f"/v1/tasks/{task_id}")
            data = await resp.json()
            assert data["state"] == "completed"

            await ws.send_json({"type": "close"})
            await ws.close()


class TestDispatch:
    """Task dispatch via Registry → Agent WebSocket."""

    async def test_dispatch_to_connected_agent(self, app_factory):
        """POST /v1/agents/{id}/dispatch forwards task via WS and returns task_id."""
        async with await app_factory() as client:
            post_resp = await client.post("/v1/agents", json={"name": "Dispatch Agent"})
            agent_id = (await post_resp.json())["id"]

            ws = await client.ws_connect(f"/v1/agents/{agent_id}/ws")

            # Dispatch task
            dispatcher_resp = await client.post(
                f"/v1/agents/{agent_id}/dispatch",
                json={"query": "Write hello world in Python"},
            )
            assert dispatcher_resp.status == 202
            data = await dispatcher_resp.json()
            assert "task_id" in data
            assert data["state"] == "forwarded"
            assert data["query"] == "Write hello world in Python"

            # Agent should receive task via WS
            ws_msg = await ws.receive_json(timeout=5)
            assert ws_msg["type"] == "task"
            assert ws_msg["id"] == data["task_id"]
            assert "Write hello world" in ws_msg["query"]

            # Simulate agent processing & result
            await ws.send_json({
                "type": "task_result",
                "id": data["task_id"],
                "status": "completed",
                "result": {"text": "print('hello world')"},
            })

            # Check task status via polling
            task_resp = await client.get(f"/v1/tasks/{data['task_id']}")
            task_data = await task_resp.json()
            assert task_data["state"] == "completed"
            assert task_data["result"]["text"] == "print('hello world')"

            await ws.send_json({"type": "close"})
            await ws.close()

    async def test_dispatch_to_disconnected_agent_503(self, app_factory):
        """POST /v1/agents/{id}/dispatch returns 503 when agent not connected."""
        async with await app_factory() as client:
            post_resp = await client.post("/v1/agents", json={"name": "Disconnected Agent"})
            agent_id = (await post_resp.json())["id"]

            resp = await client.post(
                f"/v1/agents/{agent_id}/dispatch",
                json={"query": "Test"},
            )
            assert resp.status == 503

    async def test_dispatch_to_nonexistent_agent_404(self, app_factory):
        """POST /v1/agents/{id}/dispatch returns 404 for unknown agent."""
        async with await app_factory() as client:
            resp = await client.post(
                "/v1/agents/nobody/dispatch",
                json={"query": "Test"},
            )
            assert resp.status == 404

    async def test_dispatch_empty_query_400(self, app_factory):
        """POST /v1/agents/{id}/dispatch returns 400 for empty query."""
        async with await app_factory() as client:
            post_resp = await client.post("/v1/agents", json={"name": "Empty Query Agent"})
            agent_id = (await post_resp.json())["id"]

            ws = await client.ws_connect(f"/v1/agents/{agent_id}/ws")

            resp = await client.post(
                f"/v1/agents/{agent_id}/dispatch",
                json={"query": ""},
            )
            assert resp.status == 400

            await ws.send_json({"type": "close"})
            await ws.close()

    async def test_dispatch_invalid_json_400(self, app_factory):
        """POST /v1/agents/{id}/dispatch returns 400 for invalid JSON."""
        async with await app_factory() as client:
            post_resp = await client.post("/v1/agents", json={"name": "Bad JSON Agent"})
            agent_id = (await post_resp.json())["id"]

            ws = await client.ws_connect(f"/v1/agents/{agent_id}/ws")

            resp = await client.post(
                f"/v1/agents/{agent_id}/dispatch",
                data=b"not json",
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 400

            await ws.send_json({"type": "close"})
            await ws.close()

    async def test_dispatch_with_session_id(self, app_factory):
        """Dispatch task with a session ID is forwarded to agent."""
        async with await app_factory() as client:
            post_resp = await client.post("/v1/agents", json={"name": "Session Agent"})
            agent_id = (await post_resp.json())["id"]

            ws = await client.ws_connect(f"/v1/agents/{agent_id}/ws")

            resp = await client.post(
                f"/v1/agents/{agent_id}/dispatch",
                json={"query": "test session", "sessionId": "my-session-123"},
            )
            assert resp.status == 202
            data = await resp.json()

            # Agent receives sessionId
            ws_msg = await ws.receive_json(timeout=5)
            assert ws_msg["sessionId"] == "my-session-123"

            await ws.send_json({"type": "close"})
            await ws.close()


class TestTaskStatus:
    """Task status polling via /v1/tasks/{task_id}."""

    async def test_get_unknown_task_404(self, app_factory):
        async with await app_factory() as client:
            resp = await client.get("/v1/tasks/nonexistent-task")
            assert resp.status == 404
            data = await resp.json()
            assert data["error"] == "task_not_found"

    async def test_task_created_by_dispatch(self, app_factory):
        """Task created via dispatch is queryable."""
        async with await app_factory() as client:
            post_resp = await client.post("/v1/agents", json={"name": "Task Query Agent"})
            agent_id = (await post_resp.json())["id"]

            ws = await client.ws_connect(f"/v1/agents/{agent_id}/ws")

            disp_resp = await client.post(
                f"/v1/agents/{agent_id}/dispatch",
                json={"query": "queryable task"},
            )
            task_id = (await disp_resp.json())["task_id"]

            resp = await client.get(f"/v1/tasks/{task_id}")
            data = await resp.json()
            assert data["id"] == task_id
            assert data["state"] == "forwarded"
            assert "created_at" in data
            assert "query" in data

            await ws.send_json({"type": "close"})
            await ws.close()


class TestHealthWebSocket:
    """Health endpoint shows WebSocket metric."""

    async def test_health_includes_ws_connected(self, app_factory):
        async with await app_factory() as client:
            post_resp = await client.post("/v1/agents", json={"name": "WS Health Agent"})
            agent_id = (await post_resp.json())["id"]

            resp = await client.get("/health")
            data = await resp.json()
            before = data["stats"]["connected_via_ws"]

            ws = await client.ws_connect(f"/v1/agents/{agent_id}/ws")

            resp = await client.get("/health")
            data = await resp.json()
            assert data["stats"]["connected_via_ws"] == before + 1

            await ws.send_json({"type": "close"})
            await ws.close()

            resp = await client.get("/health")
            data = await resp.json()
            assert data["stats"]["connected_via_ws"] == before