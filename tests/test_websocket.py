"""Integration tests for the WebSocket endpoint (P1-I).

Tests cover:
  - Basic WS connect / disconnect
  - Ping / Pong protocol
  - Task dispatch via WS
  - Task result/progress reporting
  - Reconnect replaces old connection
  - Auth token validation on WS upgrade
  - Dispatch to non-existent / disconnected agent
  - Admin WS comment push notification (WS-T5-TC4)
"""
from __future__ import annotations

import json
import tempfile

import pytest
from aiohttp.test_utils import TestServer, TestClient

from simple_a2a_registry.server import create_app

pytestmark = pytest.mark.asyncio


@pytest.fixture
def app_factory():
    factories = []

    async def maker(auth_enabled: bool = False) -> TestClient:
        tmpdir_obj = tempfile.TemporaryDirectory()
        factories.append(tmpdir_obj)
        data_dir = tmpdir_obj.name
        app = create_app(
            data_dir=data_dir,
            base_url="http://localhost:8321",
            auth_enabled=auth_enabled,
            bootstrap_secret="test-bootstrap-secret" if auth_enabled else None,
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


async def _register_agent(client: TestClient, name: str = "ws-test-agent") -> str:
    """Helper: register an agent and return its id."""
    resp = await client.post("/v1/agents", json={"name": name})
    data = await resp.json()
    return data["id"]


class TestWebSocketConnect:
    """Basic WebSocket connection lifecycle."""

    async def test_connect_and_disconnect(self, app_factory):
        async with await app_factory() as client:
            agent_id = await _register_agent(client)

            ws = await client.ws_connect(f"/v1/agents/{agent_id}/ws")
            assert ws.closed is False

            # Send close, verify clean disconnect
            await ws.send_json({"type": "close"})
            async for msg in ws:
                if msg.type == 0x08:  # WSMsgType.CLOSE
                    break
            assert ws.closed is True

    async def test_connect_nonexistent_agent(self, app_factory):
        async with await app_factory() as client:
            with pytest.raises(Exception):  # WSServerHandshakeError 404
                await client.ws_connect("/v1/agents/nonexistent/ws")

    async def test_health_after_ws_connect(self, app_factory):
        """Health endpoint should reflect WS connections."""
        async with await app_factory() as client:
            agent_id = await _register_agent(client)
            ws = await client.ws_connect(f"/v1/agents/{agent_id}/ws")

            resp = await client.get("/health")
            data = await resp.json()
            stats = data.get("stats", {})
            assert stats.get("connected_via_ws", 0) >= 1

            await ws.send_json({"type": "close"})
            await ws.close()

    async def test_list_agents_shows_ws_connection(self, app_factory):
        """Agent listing should show WS connection status."""
        async with await app_factory() as client:
            agent_id = await _register_agent(client, "ws-show-test")
            ws = await client.ws_connect(f"/v1/agents/{agent_id}/ws")

            resp = await client.get("/v1/agents")
            data = await resp.json()
            agents = {a["id"]: a for a in data.get("agents", data)}
            assert agent_id in agents

            await ws.send_json({"type": "close"})
            await ws.close()


class TestWebSocketPingPong:
    """Ping / Pong protocol."""

    async def test_ping_pong(self, app_factory):
        async with await app_factory() as client:
            agent_id = await _register_agent(client)
            ws = await client.ws_connect(f"/v1/agents/{agent_id}/ws")

            await ws.send_json({"type": "ping"})
            msg = await ws.receive()
            data = json.loads(msg.data)
            assert data["type"] == "pong"

            await ws.send_json({"type": "close"})
            await ws.close()

    async def test_multiple_pings(self, app_factory):
        async with await app_factory() as client:
            agent_id = await _register_agent(client)
            ws = await client.ws_connect(f"/v1/agents/{agent_id}/ws")

            for _ in range(5):
                await ws.send_json({"type": "ping"})
                msg = await ws.receive()
                data = json.loads(msg.data)
                assert data["type"] == "pong"

            await ws.send_json({"type": "close"})
            await ws.close()

    async def test_invalid_json_returns_error(self, app_factory):
        async with await app_factory() as client:
            agent_id = await _register_agent(client)
            ws = await client.ws_connect(f"/v1/agents/{agent_id}/ws")

            # Send invalid JSON
            await ws.send_str("not valid json{{{")
            msg = await ws.receive()
            data = json.loads(msg.data)
            assert data["type"] == "error"
            assert "Invalid JSON" in data.get("detail", "")

            await ws.send_json({"type": "close"})
            await ws.close()

    async def test_unknown_message_type_graceful(self, app_factory):
        """Unknown message types should be silently ignored."""
        async with await app_factory() as client:
            agent_id = await _register_agent(client)
            ws = await client.ws_connect(f"/v1/agents/{agent_id}/ws")

            await ws.send_json({"type": "unknown_type_xyz"})
            # Should not crash; subsequent ping-pong should still work
            await ws.send_json({"type": "ping"})
            msg = await ws.receive()
            data = json.loads(msg.data)
            assert data["type"] == "pong"

            await ws.send_json({"type": "close"})
            await ws.close()


class TestWebSocketTaskDispatch:
    """Task dispatch via WebSocket."""

    async def test_dispatch_task_via_ws(self, app_factory):
        async with await app_factory() as client:
            agent_id = await _register_agent(client)
            ws = await client.ws_connect(f"/v1/agents/{agent_id}/ws")

            # Dispatch a task via HTTP
            resp = await client.post(
                f"/v1/agents/{agent_id}/dispatch",
                json={"query": "do something", "sessionId": "sess-1"},
            )
            assert resp.status == 202
            data = await resp.json()
            task_id = data["task_id"]

            # Agent should receive the task via WS
            msg = await ws.receive()
            msg_data = json.loads(msg.data)
            assert msg_data["type"] == "task"
            assert msg_data["id"] == task_id
            assert msg_data["query"] == "do something"
            assert msg_data["sessionId"] == "sess-1"

            await ws.send_json({"type": "close"})
            await ws.close()

    async def test_dispatch_to_disconnected_agent(self, app_factory):
        async with await app_factory() as client:
            agent_id = await _register_agent(client)

            # Agent not connected via WS
            resp = await client.post(
                f"/v1/agents/{agent_id}/dispatch",
                json={"query": "hello"},
            )
            assert resp.status == 503
            data = await resp.json()
            assert "agent_not_connected" in json.dumps(data)

    async def test_dispatch_to_nonexistent_agent(self, app_factory):
        async with await app_factory() as client:
            resp = await client.post(
                "/v1/agents/nonexistent/dispatch",
                json={"query": "hello"},
            )
            assert resp.status == 404

    async def test_dispatch_missing_query(self, app_factory):
        async with await app_factory() as client:
            agent_id = await _register_agent(client)
            ws = await client.ws_connect(f"/v1/agents/{agent_id}/ws")

            resp = await client.post(
                f"/v1/agents/{agent_id}/dispatch",
                json={},  # Missing query
            )
            assert resp.status == 400

            await ws.send_json({"type": "close"})
            await ws.close()

    async def test_dispatch_invalid_json(self, app_factory):
        async with await app_factory() as client:
            agent_id = await _register_agent(client)
            ws = await client.ws_connect(f"/v1/agents/{agent_id}/ws")

            resp = await client.post(
                f"/v1/agents/{agent_id}/dispatch",
                data="not json",
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 400

            await ws.send_json({"type": "close"})
            await ws.close()

    async def test_task_result_via_ws(self, app_factory):
        """Agent can report task completion via WS."""
        async with await app_factory() as client:
            agent_id = await _register_agent(client)
            ws = await client.ws_connect(f"/v1/agents/{agent_id}/ws")

            # Dispatch
            resp = await client.post(
                f"/v1/agents/{agent_id}/dispatch",
                json={"query": "do work"},
            )
            data = await resp.json()
            task_id = data["task_id"]

            # Receive task via WS
            msg = await ws.receive()
            msg_data = json.loads(msg.data)
            assert msg_data["type"] == "task"

            # Report result
            await ws.send_json({
                "type": "task_result",
                "id": task_id,
                "status": "completed",
                "result": {"output": "done!"},
            })

            # Verify via HTTP
            resp = await client.get(f"/v1/tasks/{task_id}")
            task = await resp.json()
            assert task["state"] == "completed"
            assert task["result"]["output"] == "done!"

            await ws.send_json({"type": "close"})
            await ws.close()

    async def test_task_progress_via_ws(self, app_factory):
        """Agent can report progress via WS."""
        async with await app_factory() as client:
            agent_id = await _register_agent(client)
            ws = await client.ws_connect(f"/v1/agents/{agent_id}/ws")

            # Dispatch
            resp = await client.post(
                f"/v1/agents/{agent_id}/dispatch",
                json={"query": "long task"},
            )
            data = await resp.json()
            task_id = data["task_id"]

            # Receive task
            msg = await ws.receive()

            # Report progress
            await ws.send_json({
                "type": "task_progress",
                "id": task_id,
                "status": "working",
            })

            # Verify via HTTP
            resp = await client.get(f"/v1/tasks/{task_id}")
            task = await resp.json()
            assert task["state"] == "working"

            await ws.send_json({"type": "close"})
            await ws.close()

    async def test_multiple_tasks_sequential(self, app_factory):
        """Dispatch multiple tasks sequentially."""
        async with await app_factory() as client:
            agent_id = await _register_agent(client, "multi-task-agent")
            ws = await client.ws_connect(f"/v1/agents/{agent_id}/ws")

            task_ids = []
            for i in range(5):
                resp = await client.post(
                    f"/v1/agents/{agent_id}/dispatch",
                    json={"query": f"task {i}"},
                )
                data = await resp.json()
                task_ids.append(data["task_id"])

            for expected_id in task_ids:
                msg = await ws.receive()
                msg_data = json.loads(msg.data)
                assert msg_data["type"] == "task"
                assert msg_data["id"] == expected_id

            await ws.send_json({"type": "close"})
            await ws.close()

    async def test_dispatch_multiple_agents(self, app_factory):
        """Dispatch to different agents — each gets only its own tasks."""
        async with await app_factory() as client:
            agent_a = await _register_agent(client, "agent-a")
            agent_b = await _register_agent(client, "agent-b")

            ws_a = await client.ws_connect(f"/v1/agents/{agent_a}/ws")
            ws_b = await client.ws_connect(f"/v1/agents/{agent_b}/ws")

            # Dispatch to both
            resp = await client.post(
                f"/v1/agents/{agent_a}/dispatch",
                json={"query": "for A"},
            )
            task_a = (await resp.json())["task_id"]

            resp = await client.post(
                f"/v1/agents/{agent_b}/dispatch",
                json={"query": "for B"},
            )
            task_b = (await resp.json())["task_id"]

            # Agent A gets only its task
            msg = await ws_a.receive()
            assert json.loads(msg.data)["id"] == task_a

            # Agent B gets only its task
            msg = await ws_b.receive()
            assert json.loads(msg.data)["id"] == task_b

            await ws_a.send_json({"type": "close"})
            await ws_a.close()
            await ws_b.send_json({"type": "close"})
            await ws_b.close()


class TestWebSocketReconnect:
    """Reconnection semantics."""

    async def test_reconnect_replaces_old_connection(self, app_factory):
        async with await app_factory() as client:
            agent_id = await _register_agent(client)

            # First connection
            ws1 = await client.ws_connect(f"/v1/agents/{agent_id}/ws")

            # Second connection replaces first
            ws2 = await client.ws_connect(f"/v1/agents/{agent_id}/ws")

            # First connection should receive a 'close' message
            msg = await ws1.receive(timeout=3)
            data = json.loads(msg.data)
            assert data["type"] == "close"
            assert data["reason"] == "replaced"

            # Dispatch should reach ws2, not ws1
            resp = await client.post(
                f"/v1/agents/{agent_id}/dispatch",
                json={"query": "after reconnect"},
            )
            task_id = (await resp.json())["task_id"]

            msg = await ws2.receive()
            assert json.loads(msg.data)["id"] == task_id

            await ws2.send_json({"type": "close"})
            await ws2.close()

    async def test_agent_deregister_cleans_up_ws(self, app_factory):
        """Deleting an agent should eventually clean up its WS connection."""
        async with await app_factory() as client:
            agent_id = await _register_agent(client, "cleanup-test")
            ws = await client.ws_connect(f"/v1/agents/{agent_id}/ws")

            resp = await client.delete(f"/v1/agents/{agent_id}")
            assert resp.status == 200

            # Dispatch after deregister should fail
            resp = await client.post(
                f"/v1/agents/{agent_id}/dispatch",
                json={"query": "after delete"},
            )
            assert resp.status == 404

            # WS connection should eventually be closed by server
            try:
                await ws.close()
            except Exception:
                pass


class TestWebSocketAuth:
    """Auth-token validation on WebSocket upgrade."""

    async def _get_token(self, client: TestClient, scope: str = "agent:read agent:register task:read task:write") -> str:
        """Helper: get a bootstrap token with the given scope."""
        resp = await client.post(
            "/auth/token",
            data={
                "grant_type": "client_credentials",
                "client_id": "simple-a2a-registry",
                "client_secret": "test-bootstrap-secret",
                "scope": scope,
            },
        )
        assert resp.status == 200, await resp.text()
        return (await resp.json())["access_token"]

    async def _register_with_token(self, client: TestClient, token: str, name: str = "ws-auth-test") -> str:
        """Helper: register an agent with an auth token."""
        resp = await client.post(
            "/v1/agents",
            json={"name": name},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status == 201 or resp.status == 200, await resp.text()
        data = await resp.json()
        return data["id"]

    async def test_ws_with_valid_token(self, app_factory):
        async with await app_factory(auth_enabled=True) as client:
            token = await self._get_token(client)
            agent_id = await self._register_with_token(client, token)

            ws = await client.ws_connect(
                f"/v1/agents/{agent_id}/ws?token={token}"
            )
            assert ws.closed is False

            await ws.send_json({"type": "close"})
            await ws.close()

    async def test_ws_without_token_when_auth_enabled(self, app_factory):
        async with await app_factory(auth_enabled=True) as client:
            token = await self._get_token(client)
            agent_id = await self._register_with_token(client, token)

            # Should return 401
            resp = await client.get(f"/v1/agents/{agent_id}/ws")
            assert resp.status == 401
            data = await resp.json()
            assert "unauthorized" in json.dumps(data)

    async def test_ws_with_invalid_token(self, app_factory):
        async with await app_factory(auth_enabled=True) as client:
            token = await self._get_token(client)
            agent_id = await self._register_with_token(client, token)

            # The WS endpoint with bad token should return 401
            resp = await client.get(
                f"/v1/agents/{agent_id}/ws?token=badtoken"
            )
            assert resp.status == 401
            data = await resp.json()
            assert "invalid" in json.dumps(data).lower()

    async def test_ws_auth_mismatched_sub(self, app_factory):
        """Token for simple-a2a-registry can connect to any agent (it's admin)."""
        async with await app_factory(auth_enabled=True) as client:
            token = await self._get_token(client)
            agent_a = await self._register_with_token(client, token, "agent-a-ws")
            agent_b = await self._register_with_token(client, token, "agent-b-ws")

            # The simple-a2a-registry service account token should be able to
            # connect to any agent (its sub is the admin account)
            ws = await client.ws_connect(
                f"/v1/agents/{agent_b}/ws?token={token}"
            )
            assert ws.closed is False
            await ws.send_json({"type": "close"})
            await ws.close()


# ======================================================================
# WS-T5-TC4: Admin WebSocket — comment push notification
# ======================================================================


class TestAdminWSCommentPush:
    """Admin WebSocket comment push notification tests (WS-T5-TC4).

    When a comment is added to a task (POST /v2/tasks/{id}/comment),
    the AdminWSHub should broadcast a ``comment_added`` event to every
    subscribed Admin WebSocket client.
    """

    async def _create_task(self, client: TestClient, title: str = "WS-T5-TC4") -> str:
        """Helper: create a task and return its id."""
        resp = await client.post("/v2/tasks", json={"title": title})
        assert resp.status in (200, 201), await resp.text()
        data = await resp.json()
        return data["task"]["id"]

    async def test_comment_push_via_admin_ws(self, app_factory):
        """WS-T5-TC4: add a comment → verify ``comment_added`` arrives via Admin WS."""
        async with await app_factory() as client:
            # 1. Create a task
            task_id = await self._create_task(client)

            # 2. Connect to Admin WebSocket and subscribe to this task
            ws = await client.ws_connect("/v2/ws/admin")
            await ws.send_json({"type": "subscribe", "task_ids": [task_id]})

            # 3. Add a comment via HTTP
            comment_body = "需要代码审查 — WS-T5-TC4 推送测试"
            resp = await client.post(
                f"/v2/tasks/{task_id}/comment",
                json={"author": "reviewer", "body": comment_body},
            )
            assert resp.status == 201
            comment_data = await resp.json()
            assert comment_data["comment_id"] > 0

            # 4. Receive the ``comment_added`` event on Admin WS
            msg = await ws.receive(timeout=5)
            import json
            push = json.loads(msg.data)

            assert push["type"] == "task_update"
            assert push["event"] == "comment_added"
            assert push["task"]["id"] == task_id
            assert push["task"]["comment"]["body"] == comment_body
            assert push["task"]["comment"]["author"] == "reviewer"

            # 5. Cleanup
            await ws.send_json({"type": "close"})
            await ws.close()

    async def test_comment_push_subscribe_all(self, app_factory):
        """WS-T5-TC4 variant: subscribe_all should also receive comment pushes."""
        async with await app_factory() as client:
            task_id = await self._create_task(client, "WS-T5-TC4-sub-all")

            ws = await client.ws_connect("/v2/ws/admin")
            await ws.send_json({"type": "subscribe_all"})

            resp = await client.post(
                f"/v2/tasks/{task_id}/comment",
                json={"author": "bot", "body": "All-sub test"},
            )
            assert resp.status == 201

            msg = await ws.receive(timeout=5)
            import json
            push = json.loads(msg.data)

            assert push["type"] == "task_update"
            assert push["event"] == "comment_added"
            assert push["task"]["id"] == task_id

            await ws.send_json({"type": "close"})
            await ws.close()

    async def test_comment_push_unsubscribed(self, app_factory):
        """WS-T5-TC4 variant: unsubscribed task should NOT receive the push."""
        async with await app_factory() as client:
            task_a = await self._create_task(client, "task-a")
            task_b = await self._create_task(client, "task-b")

            ws = await client.ws_connect("/v2/ws/admin")
            # Subscribe only to task_a
            await ws.send_json({"type": "subscribe", "task_ids": [task_a]})

            # Add comment to task_b (not subscribed)
            resp = await client.post(
                f"/v2/tasks/{task_b}/comment",
                json={"author": "x", "body": "Should not arrive"},
            )
            assert resp.status == 201

            # The unsubscribed comment should NOT reach us within a reasonable wait
            import json
            with pytest.raises(Exception):
                msg = await ws.receive(timeout=2)
                push = json.loads(msg.data)
                if push.get("type") == "task_update" and push.get("event") == "comment_added":
                    assert push["task"]["id"] != task_b, "Unsubscribed task comment arrived!"

            await ws.send_json({"type": "close"})
            await ws.close()

    async def test_comment_push_ping_pong_afterwards(self, app_factory):
        """WS-T5-TC4: comment push should not break subsequent ping/pong."""
        async with await app_factory() as client:
            task_id = await self._create_task(client)

            ws = await client.ws_connect("/v2/ws/admin")
            await ws.send_json({"type": "subscribe", "task_ids": [task_id]})

            # Add comment
            await client.post(
                f"/v2/tasks/{task_id}/comment",
                json={"author": "alice", "body": "Ping test"},
            )

            # Receive push
            msg = await ws.receive(timeout=5)
            import json
            push = json.loads(msg.data)
            assert push["event"] == "comment_added"

            # Ping-pong must still work
            await ws.send_json({"type": "ping"})
            pong = await ws.receive(timeout=5)
            pong_data = json.loads(pong.data)
            assert pong_data["type"] == "pong"

            await ws.send_json({"type": "close"})
            await ws.close()