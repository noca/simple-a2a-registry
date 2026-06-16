"""Tests for SSE event stream endpoint — GET /v2/events.

Covers:
- SSE connection establishment
- Receiving events via SSE stream
- Event type filtering
- Tenant filtering
- Heartbeat keepalive
- Connection cleanup on disconnect
- Integration with EventBus + create_app
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncGenerator, Dict, Optional

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from simple_a2a_registry.events import (
    EventBus,
    SSEEventHandler,
    EventTypes,
)
from simple_a2a_registry.server import create_app

pytestmark = pytest.mark.asyncio

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def app_and_client() -> AsyncGenerator:
    """Create a test app + client via create_app (no auth, no dispatcher)."""
    # Use a temp data dir
    client = _create_test_client()
    yield client
    await client.close()


def _create_test_client() -> TestClient:
    """Helper: build a test client with create_app."""
    import tempfile
    import os

    tmpdir = tempfile.mkdtemp(prefix="sse_test_")
    app = create_app(
        data_dir=tmpdir,
        auth_enabled=False,
        dispatcher_enabled=False,
    )
    return TestClient(TestServer(app))


# ---------------------------------------------------------------------------
# Pure SSE unit tests (standalone EventBus + SSEEventHandler)
# ---------------------------------------------------------------------------


class TestSSEHandlerUnit:
    """Direct unit tests for SSEEventHandler with a standalone EventBus."""

    @pytest.fixture
    def bus(self) -> EventBus:
        return EventBus()

    @pytest.fixture
    def sse_app(self, bus: EventBus) -> web.Application:
        """A minimal aiohttp app with just the SSE endpoint."""
        handler = SSEEventHandler(bus)
        app = web.Application()
        app.router.add_get("/events", handler.handle_sse_stream)
        return app

    async def test_sse_receives_event(
        self, bus: EventBus, sse_app: web.Application,
    ) -> None:
        """Connect to SSE, publish an event, verify it arrives."""
        client = TestClient(TestServer(sse_app))
        await client.start_server()

        try:
            # Connect to SSE using raw aiohttp for streaming
            async with client.session.get(
                client.make_url("/events"),
                headers={"Accept": "text/event-stream"},
            ) as resp:
                assert resp.status == 200
                assert resp.headers["Content-Type"] == "text/event-stream"

                # Publish an event
                await bus.publish("task.created", {"id": "t_001", "title": "Test"})

                # Read SSE event: event -> id -> data (blank line terminates)
                event_line = await asyncio.wait_for(resp.content.readline(), timeout=3.0)
                assert event_line.decode("utf-8").strip().startswith("event: task.created")

                # Skip id: line
                id_line = await asyncio.wait_for(resp.content.readline(), timeout=3.0)
                assert id_line.decode("utf-8").strip().startswith("id: evt_")

                # Read data line
                data_line = await asyncio.wait_for(resp.content.readline(), timeout=3.0)
                data_text = data_line.decode("utf-8").strip()
                assert data_text.startswith("data: ")
                payload = json.loads(data_text[6:])  # strip "data: "
                assert payload["event_type"] == "task.created"
                assert payload["data"]["id"] == "t_001"
        finally:
            await client.close()

    async def test_sse_event_type_filter(
        self, bus: EventBus, sse_app: web.Application,
    ) -> None:
        """Client can filter by event_type query parameter."""
        client = TestClient(TestServer(sse_app))
        await client.start_server()

        try:
            url = client.make_url("/events?event_type=task.completed")
            async with client.session.get(url) as resp:
                assert resp.status == 200

                # Publish events of different types
                await bus.publish("task.created", {"id": "t_001"})
                await bus.publish("task.completed", {"id": "t_001"})

                # Should only receive task.completed (task.created is filtered out)
                line = await asyncio.wait_for(resp.content.readline(), timeout=3.0)
                assert line.decode("utf-8").strip() == "event: task.completed"

                # Skip id: line
                await asyncio.wait_for(resp.content.readline(), timeout=3.0)

                data_line = await asyncio.wait_for(resp.content.readline(), timeout=3.0)
                data_text = data_line.decode("utf-8").strip()
                payload = json.loads(data_text[6:])
                assert payload["data"]["id"] == "t_001"
        finally:
            await client.close()

    async def test_sse_multiple_event_type_filter(
        self, bus: EventBus, sse_app: web.Application,
    ) -> None:
        """Client can filter by multiple event types (comma-separated)."""
        client = TestClient(TestServer(sse_app))
        await client.start_server()

        try:
            url = client.make_url("/events?event_type=task.created,task.blocked")
            async with client.session.get(url) as resp:
                assert resp.status == 200

                # Publish events of different types
                await bus.publish("task.created", {"id": "t_001"})
                await bus.publish("task.completed", {"id": "t_001"})
                await bus.publish("task.blocked", {"id": "t_001"})

                # Read two events (created, blocked) from stream
                events = []
                for _ in range(2):
                    evt_line = await asyncio.wait_for(resp.content.readline(), timeout=3.0)
                    evt_type = evt_line.decode("utf-8").strip().replace("event: ", "")
                    # Skip id: line
                    await asyncio.wait_for(resp.content.readline(), timeout=3.0)
                    data_line = await asyncio.wait_for(resp.content.readline(), timeout=3.0)
                    events.append(evt_type)

                assert "task.created" in events
                assert "task.blocked" in events
                assert "task.completed" not in events
        finally:
            await client.close()

    async def test_sse_tenant_filter(
        self, bus: EventBus, sse_app: web.Application,
    ) -> None:
        """Client can filter by tenant query parameter."""
        client = TestClient(TestServer(sse_app))
        await client.start_server()

        try:
            url = client.make_url("/events?tenant=tenant-a")
            async with client.session.get(url) as resp:
                assert resp.status == 200

                # Publish events with different tenants
                await bus.publish("task.created", {"id": "t_001"}, tenant="tenant-a")
                await bus.publish("task.created", {"id": "t_002"}, tenant="tenant-b")

                # Should only receive tenant-a events
                line = await asyncio.wait_for(resp.content.readline(), timeout=3.0)
                assert line.decode("utf-8").strip() == "event: task.created"

                # Skip id: line
                await asyncio.wait_for(resp.content.readline(), timeout=3.0)

                data_line = await asyncio.wait_for(resp.content.readline(), timeout=3.0)
                data_text = data_line.decode("utf-8").strip()
                payload = json.loads(data_text[6:])
                assert payload["data"]["id"] == "t_001"
                assert payload["tenant"] == "tenant-a"
        finally:
            await client.close()

    async def test_sse_heartbeat_keepalive(
        self, bus: EventBus, sse_app: web.Application,
    ) -> None:
        """Heartbeat comments are sent periodically."""
        # Override heartbeat interval to be short for testing
        import simple_a2a_registry.events.sse_handler as sse_mod
        orig_interval = sse_mod.SSE_HEARTBEAT_INTERVAL
        sse_mod.SSE_HEARTBEAT_INTERVAL = 1  # 1 second for fast test

        try:
            client = TestClient(TestServer(sse_app))
            await client.start_server()

            async with client.session.get(
                client.make_url("/events"),
            ) as resp:
                assert resp.status == 200

                # Read a heartbeat comment (should come within 2 seconds)
                line = await asyncio.wait_for(resp.content.readline(), timeout=3.0)
                line_text = line.decode("utf-8").strip()
                assert line_text == ": keepalive"
        finally:
            sse_mod.SSE_HEARTBEAT_INTERVAL = orig_interval
            await client.close()


# ---------------------------------------------------------------------------
# Integration test — SSE endpoint through create_app
# ---------------------------------------------------------------------------


class TestSSEIntegration:
    """Test that the SSE endpoint works through the full create_app."""

    @pytest.fixture
    async def client(self) -> TestClient:
        cl = _create_test_client()
        yield cl
        await cl.close()

    async def test_integration_sse_endpoint(
        self, client: TestClient,
    ) -> None:
        """SSE endpoint returns text/event-stream and can receive events."""
        await client.start_server()

        try:
            async with client.get(
                "/v2/events",
                headers={"Accept": "text/event-stream"},
            ) as resp:
                assert resp.status == 200
                assert resp.headers["Content-Type"] == "text/event-stream"

                # Publish an event through the app's EventBus
                bus: EventBus = client.app["event_bus"]
                await bus.publish("task.created", {"id": "t_001", "title": "SSE Integration Test"})

                # Read from SSE stream
                line = await asyncio.wait_for(resp.content.readline(), timeout=3.0)
                assert line.decode("utf-8").strip() == "event: task.created"

                # Skip id: line
                await asyncio.wait_for(resp.content.readline(), timeout=3.0)

                data_line = await asyncio.wait_for(resp.content.readline(), timeout=3.0)
                data_text = data_line.decode("utf-8").strip()
                payload = json.loads(data_text[6:])
                assert payload["data"]["title"] == "SSE Integration Test"
        finally:
            pass

    async def test_integration_broadcast_from_orch_handler(
        self, client: TestClient,
    ) -> None:
        """Orchestration broadcast events flow through SSE via the EventBus bridge."""
        await client.start_server()

        try:
            async with client.get("/v2/events") as resp:
                assert resp.status == 200

                # Broadcast via orch_handler's bridge (using test data)
                orch_handler = client.app["orch_handler"]
                await orch_handler._broadcast_fn("created", {"id": "t_001", "title": "Bridged Task"})

                # Read from SSE stream
                line = await asyncio.wait_for(resp.content.readline(), timeout=3.0)
                assert line.decode("utf-8").strip() == "event: task.created"

                # Skip id: line
                await asyncio.wait_for(resp.content.readline(), timeout=3.0)

                data_line = await asyncio.wait_for(resp.content.readline(), timeout=3.0)
                data_text = data_line.decode("utf-8").strip()
                payload = json.loads(data_text[6:])
                assert payload["data"]["title"] == "Bridged Task"
        finally:
            pass


# ---------------------------------------------------------------------------
# Edge cases and error handling
# ---------------------------------------------------------------------------


class TestSSEEdgeCases:
    """Edge cases for the SSE handler."""

    async def test_event_bus_closed_returns_503(self) -> None:
        """If the EventBus is closed, SSE returns 503."""
        bus = EventBus()
        await bus.close()

        handler = SSEEventHandler(bus)
        app = web.Application()
        app.router.add_get("/events", handler.handle_sse_stream)
        client = TestClient(TestServer(app))
        await client.start_server()

        try:
            async with client.get("/events") as resp:
                assert resp.status == 503
                body = await resp.json()
                assert body["error"] == "event_bus_closed"
        finally:
            await client.close()