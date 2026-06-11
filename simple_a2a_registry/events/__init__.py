"""Event System — in-memory EventBus + SSE push for real-time event-driven distribution.

Provides:

- ``EventBus`` — asyncio.Queue based in-memory event bus with broadcast
  support, backpressure (discard/warn), and typed event types.
- ``SSEEventHandler`` — Server-Sent Events endpoint (``GET /v2/events``)
  for real-time event streaming to clients.
"""

from __future__ import annotations

from simple_a2a_registry.events.event_bus import (
    EventBus,
    EventBusEvent,
    EventTypes,
    DEFAULT_MAX_QUEUE_SIZE,
)

from simple_a2a_registry.events.sse_handler import (
    SSEEventHandler,
)

__all__ = [
    "EventBus",
    "EventBusEvent",
    "EventTypes",
    "DEFAULT_MAX_QUEUE_SIZE",
    "SSEEventHandler",
]