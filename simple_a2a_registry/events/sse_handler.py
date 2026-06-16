"""SSE (Server-Sent Events) handler — real-time event streaming endpoint.

Provides a ``GET /v2/events`` endpoint that streams EventBus events
to connected clients via Server-Sent Events (SSE).

Features:
- **Event type filtering**: client can filter by ``event_type`` query param.
- **Tenant isolation**: client can filter by ``tenant`` query param.
- **Heartbeat**: server sends keep-alive comments every 30 seconds.
- **Auto-cleanup**: disconnected clients are automatically unsubscribed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any, Dict, Optional, Set

from aiohttp import web

from simple_a2a_registry.events.event_bus import EventBus

logger = logging.getLogger("a2a_registry.events.sse_handler")

# Heartbeat interval — server sends a keep-alive comment every 30 seconds.
SSE_HEARTBEAT_INTERVAL = 30


# ---------------------------------------------------------------------------
# SSE Helper
# ---------------------------------------------------------------------------


def _sse_format(event_type: str, data: str, event_id: str = "") -> str:
    """Format a Server-Sent Events message string.

    See https://html.spec.whatwg.org/multipage/server-sent-events.html
    """
    lines = [f"event: {event_type}"]
    if event_id:
        lines.append(f"id: {event_id}")
    for line in data.split("\n"):
        lines.append(f"data: {line}")
    lines.append("")  # Two trailing newlines = end of event
    return "\n".join(lines)


def _sse_comment(text: str) -> str:
    """Format a comment line (ignored by the client, useful for heartbeats)."""
    return f": {text}\n\n"


# ---------------------------------------------------------------------------
# SSEEventHandler
# ---------------------------------------------------------------------------


class SSEEventHandler:
    """HTTP handler for the SSE event stream endpoint.

    Usage::

        handler = SSEEventHandler(event_bus)
        app.router.add_get("/v2/events", handler.handle_sse_stream)

    Query params:
        event_type: Comma-separated list of event types to include.
                    Omit or empty = all event types.
        tenant:     Only include events matching this tenant.
                    Omit or empty = all tenants.
    """

    def __init__(self, event_bus: EventBus) -> None:
        self._bus = event_bus

    # ------------------------------------------------------------------
    # GET /v2/events — SSE stream
    # ------------------------------------------------------------------

    async def handle_sse_stream(self, request: web.Request) -> web.StreamResponse:
        """``GET /v2/events`` — establish an SSE connection.

        The client receives a stream of Server-Sent Events representing
        EventBus events.  A heartbeat comment (``: keepalive``) is sent
        every 30 seconds to keep the connection alive.

        Query parameters:
            event_type (str, optional): Comma-separated event type filters.
            tenant (str, optional): Tenant filter for multi-tenant isolation.
        """
        # Parse filters from query params
        event_type_filter: Optional[Set[str]] = None
        raw_types = request.query.get("event_type", "")
        if raw_types:
            event_type_filter = set(t.strip() for t in raw_types.split(",") if t.strip())

        tenant_filter: Optional[str] = request.query.get("tenant", None)
        if tenant_filter is not None and not tenant_filter:
            tenant_filter = None

        # Register a subscriber on the EventBus
        subscriber_id = "sse_" + uuid.uuid4().hex[:12]
        try:
            await self._bus.subscribe(subscriber_id)
        except RuntimeError:
            return web.json_response(
                {"error": "event_bus_closed", "detail": "EventBus is closed"},
                status=503,
            )

        # Prepare SSE response
        resp = web.StreamResponse(
            status=200,
            reason="OK",
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",  # Disable nginx buffering
            },
        )

        try:
            await resp.prepare(request)
        except ConnectionResetError:
            await self._bus.unsubscribe(subscriber_id)
            return resp

        logger.info(
            "SSE connected: subscriber=%s filters=(types=%s, tenant=%s)",
            subscriber_id, event_type_filter, tenant_filter,
        )

        # Heartbeat task
        hb_task = asyncio.create_task(
            self._heartbeat_loop(resp, subscriber_id)
        )

        try:
            async for event in self._bus.events(subscriber_id):
                # Apply filters
                if event_type_filter is not None and event.event_type not in event_type_filter:
                    continue
                if tenant_filter is not None and event.tenant != tenant_filter:
                    continue

                payload = event.to_dict()
                data_str = json.dumps(payload)
                sse_msg = _sse_format(
                    event_type=event.event_type,
                    data=data_str,
                    event_id=event.event_id,
                )

                try:
                    await resp.write(sse_msg.encode("utf-8"))
                except (ConnectionResetError, ConnectionAbortedError):
                    logger.debug("SSE client disconnected (write error): %s", subscriber_id)
                    break
                except Exception as e:
                    logger.warning("SSE write error for '%s': %s", subscriber_id, e)
                    break
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error("SSE stream error for '%s': %s", subscriber_id, e)
        finally:
            hb_task.cancel()
            try:
                await hb_task
            except (asyncio.CancelledError, Exception):
                pass
            await self._bus.unsubscribe(subscriber_id)
            logger.info("SSE disconnected: subscriber=%s", subscriber_id)

        return resp

    # ------------------------------------------------------------------
    # Heartbeat
    # ------------------------------------------------------------------

    async def _heartbeat_loop(
        self,
        resp: web.StreamResponse,
        subscriber_id: str,
    ) -> None:
        """Send a keep-alive comment every 30 seconds."""
        try:
            while True:
                await asyncio.sleep(SSE_HEARTBEAT_INTERVAL)
                try:
                    await resp.write(_sse_comment("keepalive").encode("utf-8"))
                except (ConnectionResetError, ConnectionAbortedError):
                    logger.debug("SSE heartbeat — client disconnected: %s", subscriber_id)
                    break
                except Exception as e:
                    logger.debug("SSE heartbeat error for '%s': %s", subscriber_id, e)
                    break
        except asyncio.CancelledError:
            pass