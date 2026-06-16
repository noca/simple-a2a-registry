"""Tests for SYNC_CALL 同步直通路由 — POST /v2/sync-call.

Covers:
  - Happy path: agent connected, sync_call succeeds
  - Agent not connected (503)
  - Validation errors (missing agent_id, missing skill)
  - Timeout (3s default)
  - Security barrier (APE deny)
  - Error response from agent
  - Exit barrier hook
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from typing import Any, Dict, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer, TestClient

from simple_a2a_registry.server import create_app
from simple_a2a_registry.orchestration.contract import (
    InteractionMode,
    TaskEnvelope,
)
from simple_a2a_registry.orchestration.sync_routes import (
    SyncCallHandler,
    register_sync_routes,
    register_exit_barrier,
    DEFAULT_SYNC_TIMEOUT_SECONDS,
    handle_ws_sync_response,
    _pending_requests,
    _resolve_pending,
    _exit_barriers,
)
from simple_a2a_registry.registry_handler import WSContext
from simple_a2a_registry.security.ape import (
    AuthorizationPolicyEngine,
    APEConfig,
)
from simple_a2a_registry.security.events import SecurityEventStore, SecurityEventType

pytestmark = pytest.mark.asyncio


# ===================================================================
# Fixtures
# ===================================================================


@pytest.fixture
def app_factory():
    """Create a fresh TestClient per test."""
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


@pytest.fixture(autouse=True)
def reset_pending():
    """Clear pending requests and exit barriers between tests."""
    _pending_requests.clear()
    _exit_barriers.clear()
    yield
    _pending_requests.clear()
    _exit_barriers.clear()


# ===================================================================
# Unit tests — SyncCallHandler
# ===================================================================


class TestSyncCallHandlerValidate:
    """Validation: missing agent_id, skill, invalid JSON."""

    async def test_missing_agent_id(self):
        handler = SyncCallHandler(ws_connections={})
        request = MagicMock(spec=web.Request)
        request.json = AsyncMock(return_value={"skill": "code"})
        request.get = MagicMock(return_value="")
        request.__getitem__ = MagicMock(side_effect=KeyError)  # for request["tenant"] access
        request.headers = {"X-Request-Id": "test-req"}
        resp = await handler.handle_sync_call(request)
        assert resp.status == 400
        data = json.loads(resp.body.decode())
        assert data["error"] == "validation_error"
        assert "agent_id" in data["detail"]

    async def test_missing_skill(self):
        handler = SyncCallHandler(ws_connections={})
        request = MagicMock(spec=web.Request)
        request.json = AsyncMock(return_value={"agent_id": "test-agent"})
        request.get = MagicMock(return_value="")
        request.__getitem__ = MagicMock(side_effect=KeyError)
        request.headers = {"X-Request-Id": "test-req"}
        resp = await handler.handle_sync_call(request)
        assert resp.status == 400
        data = json.loads(resp.body.decode())
        assert data["error"] == "validation_error"
        assert "skill" in data["detail"]

    async def test_invalid_json(self):
        handler = SyncCallHandler(ws_connections={})
        request = MagicMock(spec=web.Request)
        request.json = AsyncMock(side_effect=json.JSONDecodeError("bad", "", 0))
        request.get = MagicMock(return_value="")
        request.__getitem__ = MagicMock(side_effect=KeyError)
        request.headers = {"X-Request-Id": "test-req"}
        resp = await handler.handle_sync_call(request)
        assert resp.status == 400
        data = json.loads(resp.body.decode())
        assert data["error"] == "invalid_json"


class TestSyncCallHandlerAgentOffline:
    """Agent not connected via WebSocket → 503."""

    async def test_agent_not_connected(self):
        handler = SyncCallHandler(ws_connections={})
        request = MagicMock(spec=web.Request)
        request.json = AsyncMock(return_value={
            "agent_id": "offline-agent",
            "skill": "code",
        })
        request.get = MagicMock(return_value="")
        request.__getitem__ = MagicMock(side_effect=KeyError)
        request.headers = {"X-Request-Id": "test-req"}
        resp = await handler.handle_sync_call(request)
        assert resp.status == 503
        data = json.loads(resp.body.decode())
        assert data["error"] == "agent_not_connected"

    async def test_ws_connection_closed(self):
        mock_ws = MagicMock()
        mock_ws.closed = True
        handler = SyncCallHandler(ws_connections={"closed-agent": mock_ws})
        request = MagicMock(spec=web.Request)
        request.json = AsyncMock(return_value={
            "agent_id": "closed-agent",
            "skill": "code",
        })
        request.get = MagicMock(return_value="")
        request.__getitem__ = MagicMock(side_effect=KeyError)
        request.headers = {"X-Request-Id": "test-req"}
        resp = await handler.handle_sync_call(request)
        assert resp.status == 503
        data = json.loads(resp.body.decode())
        assert data["error"] == "agent_not_connected"


class TestSyncCallHandlerHappyPath:
    """Agent connected, sync_call succeeds."""

    async def test_successful_sync_call(self):
        # Create mock WS connection
        mock_ws = AsyncMock()
        mock_ws.closed = False
        mock_ws.send_json = AsyncMock()

        handler = SyncCallHandler(
            ws_connections={"test-agent": mock_ws},
        )

        request = MagicMock(spec=web.Request)
        request.json = AsyncMock(return_value={
            "agent_id": "test-agent",
            "skill": "code",
            "input": {"prompt": "hello"},
        })
        request.get = MagicMock(return_value="")
        request.__getitem__ = MagicMock(side_effect=KeyError)
        request.headers = {"X-Request-Id": "test-req"}

        # Simulate WS response arriving (async)
        async def resolve_after_timeout():
            await asyncio.sleep(0.05)
            # Find the request_id that was registered
            for rid, fut in list(_pending_requests.items()):
                _resolve_pending(rid, "success", {"output": "world"})

        async def run():
            task = asyncio.create_task(resolve_after_timeout())
            resp = await handler.handle_sync_call(request)
            await task
            return resp

        resp = await run()
        assert resp.status == 200
        data = json.loads(resp.body.decode())
        assert data["status"] == "success"
        assert data["result"] == {"output": "world"}
        assert "request_id" in data

        # Verify WS message was sent
        sent_args = mock_ws.send_json.call_args[0][0]
        assert sent_args["type"] == "sync_call"
        assert sent_args["skill"] == "code"

    async def test_sync_call_with_security_context(self):
        mock_ws = AsyncMock()
        mock_ws.closed = False
        mock_ws.send_json = AsyncMock()

        handler = SyncCallHandler(
            ws_connections={"test-agent": mock_ws},
        )

        request = MagicMock(spec=web.Request)
        request.json = AsyncMock(return_value={
            "agent_id": "test-agent",
            "skill": "code",
            "input": {"prompt": "hello"},
            "security_context": {
                "effective_scope": "agent:admin",
                "delegation_depth": 1,
            },
        })
        request.get = MagicMock(return_value="")
        request.__getitem__ = MagicMock(side_effect=KeyError)
        request.headers = {"X-Request-Id": "test-req"}

        async def resolve():
            await asyncio.sleep(0.05)
            for rid, fut in list(_pending_requests.items()):
                _resolve_pending(rid, "success", {"output": "world"})

        async def run():
            task = asyncio.create_task(resolve())
            resp = await handler.handle_sync_call(request)
            await task
            return resp

        resp = await run()
        assert resp.status == 200

        sent_args = mock_ws.send_json.call_args[0][0]
        assert sent_args["security_context"]["effective_scope"] == "agent:admin"
        assert sent_args["security_context"]["delegation_depth"] == 1


class TestSyncCallHandlerTimeout:
    """3s timeout → 504."""

    async def test_timeout_returns_504(self):
        mock_ws = AsyncMock()
        mock_ws.closed = False
        mock_ws.send_json = AsyncMock()

        handler = SyncCallHandler(
            ws_connections={"slow-agent": mock_ws},
        )

        request = MagicMock(spec=web.Request)
        request.json = AsyncMock(return_value={
            "agent_id": "slow-agent",
            "skill": "code",
            # Very short timeout for test
            "timeout_seconds": 0.05,
        })
        request.get = MagicMock(return_value="")
        request.__getitem__ = MagicMock(side_effect=KeyError)
        request.headers = {"X-Request-Id": "test-req"}

        resp = await handler.handle_sync_call(request)
        assert resp.status == 504
        data = json.loads(resp.body.decode())
        assert data["error"] == "sync_call_timeout"


class TestSyncCallHandlerError:
    """Agent returns error status."""

    async def test_agent_returns_error(self):
        mock_ws = AsyncMock()
        mock_ws.closed = False
        mock_ws.send_json = AsyncMock()

        handler = SyncCallHandler(
            ws_connections={"failing-agent": mock_ws},
        )

        request = MagicMock(spec=web.Request)
        request.json = AsyncMock(return_value={
            "agent_id": "failing-agent",
            "skill": "code",
        })
        request.get = MagicMock(return_value="")
        request.__getitem__ = MagicMock(side_effect=KeyError)
        request.headers = {"X-Request-Id": "test-req"}

        async def resolve():
            await asyncio.sleep(0.05)
            for rid, fut in list(_pending_requests.items()):
                _resolve_pending(rid, "error", None, "Something went wrong")

        async def run():
            task = asyncio.create_task(resolve())
            resp = await handler.handle_sync_call(request)
            await task
            return resp

        resp = await run()
        assert resp.status == 502
        data = json.loads(resp.body.decode())
        assert data["error"] == "sync_call_failed"
        assert data["extra"]["status"] == "error"


class TestSyncCallHandlerExitBarrier:
    """Exit security barrier hook blocks the response."""

    async def test_exit_barrier_blocks_response(self):
        mock_ws = AsyncMock()
        mock_ws.closed = False
        mock_ws.send_json = AsyncMock()

        handler = SyncCallHandler(
            ws_connections={"test-agent": mock_ws},
        )

        # Register exit barrier that blocks
        async def blocking_barrier(request, agent_id, envelope, response_data):
            return web.json_response(
                {"error": "exit_blocked", "detail": "Blocked by exit fence"},
                status=403,
            )

        register_exit_barrier(blocking_barrier)

        request = MagicMock(spec=web.Request)
        request.json = AsyncMock(return_value={
            "agent_id": "test-agent",
            "skill": "code",
        })
        request.get = MagicMock(return_value="")
        request.__getitem__ = MagicMock(side_effect=KeyError)
        request.headers = {"X-Request-Id": "test-req"}

        async def resolve():
            await asyncio.sleep(0.05)
            for rid, fut in list(_pending_requests.items()):
                _resolve_pending(rid, "success", {"output": "world"})

        async def run():
            task = asyncio.create_task(resolve())
            resp = await handler.handle_sync_call(request)
            await task
            return resp

        resp = await run()
        assert resp.status == 403
        data = json.loads(resp.body.decode())
        assert data["error"] == "exit_blocked"

    async def test_exit_barrier_passing(self):
        mock_ws = AsyncMock()
        mock_ws.closed = False
        mock_ws.send_json = AsyncMock()

        handler = SyncCallHandler(
            ws_connections={"test-agent": mock_ws},
        )

        # Register a passing barrier
        async def passing_barrier(request, agent_id, envelope, response_data):
            return None

        register_exit_barrier(passing_barrier)

        request = MagicMock(spec=web.Request)
        request.json = AsyncMock(return_value={
            "agent_id": "test-agent",
            "skill": "code",
        })
        request.get = MagicMock(return_value="")
        request.__getitem__ = MagicMock(side_effect=KeyError)
        request.headers = {"X-Request-Id": "test-req"}

        async def resolve():
            await asyncio.sleep(0.05)
            for rid, fut in list(_pending_requests.items()):
                _resolve_pending(rid, "success", {"output": "world"})

        async def run():
            task = asyncio.create_task(resolve())
            resp = await handler.handle_sync_call(request)
            await task
            return resp

        resp = await run()
        assert resp.status == 200
        data = json.loads(resp.body.decode())
        assert data["status"] == "success"


# ===================================================================
# Integration tests — POST /v2/sync-call via full app
# ===================================================================


class TestV2SyncCallIntegration:
    """Full app integration test with WS-connected agent."""

    async def test_agent_not_connected_returns_503(self, app_factory):
        async with await app_factory() as client:
            resp = await client.post("/v2/sync-call", json={
                "agent_id": "nonexistent-agent",
                "skill": "code",
                "input": {"prompt": "hello"},
            })
            assert resp.status == 503
            data = await resp.json()
            assert data["error"] == "agent_not_connected"

    async def test_missing_agent_id_returns_400(self, app_factory):
        async with await app_factory() as client:
            resp = await client.post("/v2/sync-call", json={
                "skill": "code",
            })
            assert resp.status == 400
            data = await resp.json()
            assert data["error"] == "validation_error"

    async def test_missing_skill_returns_400(self, app_factory):
        async with await app_factory() as client:
            resp = await client.post("/v2/sync-call", json={
                "agent_id": "some-agent",
            })
            assert resp.status == 400
            data = await resp.json()
            assert data["error"] == "validation_error"


# ===================================================================
# WS handler tests
# ===================================================================


class TestWsSyncResponseHandler:
    """handle_ws_sync_response resolves pending requests."""

    async def test_resolves_pending_request(self):
        fut = asyncio.get_running_loop().create_future()
        _pending_requests["req_test_1"] = fut

        ctx = WSContext(agent_id="test-agent")
        await handle_ws_sync_response(
            MagicMock(),
            {
                "type": "sync_call_response",
                "request_id": "req_test_1",
                "status": "success",
                "result": {"output": "world"},
            },
            ctx,
        )

        assert fut.done()
        status, result, error = fut.result()
        assert status == "success"
        assert result == {"output": "world"}
        assert error is None

    async def test_resolves_error_response(self):
        fut = asyncio.get_running_loop().create_future()
        _pending_requests["req_test_2"] = fut

        ctx = WSContext(agent_id="test-agent")
        await handle_ws_sync_response(
            MagicMock(),
            {
                "type": "sync_call_response",
                "request_id": "req_test_2",
                "status": "error",
                "result": None,
                "error": "Something failed",
            },
            ctx,
        )

        assert fut.done()
        status, result, error = fut.result()
        assert status == "error"
        assert error == "Something failed"

    async def test_ignores_missing_request_id(self):
        fut = asyncio.get_running_loop().create_future()
        _pending_requests["req_test_3"] = fut

        ctx = WSContext(agent_id="test-agent")
        # No request_id → should not resolve anything
        await handle_ws_sync_response(
            MagicMock(),
            {
                "type": "sync_call_response",
                "status": "success",
            },
            ctx,
        )

        # fut should still be pending
        assert not fut.done()