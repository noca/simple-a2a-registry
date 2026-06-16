"""In-memory EventBus — asyncio.Queue based broadcast event bus.

Provides typed event types, multiple subscriber support (fan-out),
backpressure handling (drop-oldest when queue is full), and an
async iterator interface for each subscriber.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, AsyncIterator, Dict, List, Optional, Set

logger = logging.getLogger("a2a_registry.events.event_bus")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_MAX_QUEUE_SIZE: int = 1024
"""Default max items per subscriber queue before backpressure kicks in."""


# ---------------------------------------------------------------------------
# Event types — canonical event type strings
# ---------------------------------------------------------------------------


class EventTypes:
    """Canonical event type constants published on the EventBus.

    These mirror the AdminWSHub event types and add security/system events.
    """

    # --- Task lifecycle ---
    TASK_CREATED: str = "task.created"
    TASK_UPDATED: str = "task.updated"
    TASK_DELETED: str = "task.deleted"
    TASK_CLAIMED: str = "task.claimed"
    TASK_COMPLETED: str = "task.completed"
    TASK_BLOCKED: str = "task.blocked"
    TASK_UNBLOCKED: str = "task.unblocked"
    TASK_HEARTBEAT: str = "task.heartbeat"
    TASK_STATUS_CHANGED: str = "task.status_changed"
    TASK_COMMENT_ADDED: str = "task.comment_added"
    TASK_BATCH_STATUS: str = "task.batch_status"
    TASK_BATCH_DELETE: str = "task.batch_delete"
    TASK_DEPENDENCY_ADDED: str = "task.dependency_added"
    TASK_DEPENDENCY_REMOVED: str = "task.dependency_removed"

    # --- Security events ---
    SECURITY_VIOLATION: str = "security.violation"
    SECURITY_AUDIT: str = "security.audit"
    SECURITY_WARN: str = "security.warn"
    SECURITY_AUTH_FAILURE: str = "security.auth_failure"
    SECURITY_SCOPE_DENIED: str = "security.scope_denied"

    # --- System events ---
    SYSTEM_AGENT_REGISTERED: str = "system.agent_registered"
    SYSTEM_AGENT_REMOVED: str = "system.agent_removed"
    SYSTEM_DISPATCHER_HEARTBEAT: str = "system.dispatcher_heartbeat"
    SYSTEM_HEALTH_CHECK: str = "system.health_check"


# ---------------------------------------------------------------------------
# Event payload dataclass
# ---------------------------------------------------------------------------


@dataclass
class EventBusEvent:
    """A single event published on the EventBus.

    Attributes:
        event_type: Canonical event type (e.g. ``"task.created"``).
        data:       Arbitrary JSON-serialisable payload dict.
        event_id:   Unique event identifier (auto-generated).
        timestamp:  Unix timestamp when the event was created.
        tenant:     Optional tenant name for multi-tenant isolation.
    """

    event_type: str
    data: Dict[str, Any]
    event_id: str = ""
    timestamp: float = 0.0
    tenant: str = ""

    def __post_init__(self) -> None:
        if not self.event_id:
            self.event_id = "evt_" + uuid.uuid4().hex[:12]
        if not self.timestamp:
            self.timestamp = time.time()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "timestamp": self.timestamp,
            "tenant": self.tenant,
            "data": self.data,
        }


# ---------------------------------------------------------------------------
# Backpressure policy
# ---------------------------------------------------------------------------

import enum as _enum


class BackpressurePolicy(_enum.Enum):
    """What to do when a subscriber's queue is full."""

    DROP_OLDEST = "drop_oldest"
    """Drop the oldest event from the subscriber's queue to make room."""

    DROP_NEWEST = "drop_newest"
    """Drop the new event entirely (don't enqueue)."""

    BLOCK = "block"
    """Block the publisher until there's room (dangerous — can stall the
    whole event bus)."""


# ---------------------------------------------------------------------------
# EventBus
# ---------------------------------------------------------------------------


class EventBus:
    """Asyncio-based in-memory event bus with broadcast support.

    Usage::

        bus = EventBus()
        sub_id = await bus.subscribe()  # get subscriber ID and async iterator
        async for event in bus.events(sub_id):
            print(event.event_type, event.data)

        # Elsewhere:
        await bus.publish("task.created", {"task_id": "t_xxx", ...})

    Features:

    - **Broadcast**: every subscriber receives every published event
      (filtering is done on the subscriber side or via SSE query params).
    - **Backpressure**: ``BackpressurePolicy.DROP_OLDEST`` (default) drops
      the oldest event from a slow subscriber's queue so the publisher
      never blocks.
    - **Tenant isolation**: events carry a ``tenant`` field; SSE handler
      can filter by tenant.
    - **Graceful unsubscribe**: removes the subscriber's queue and cancels
      its pending items.
    """

    def __init__(
        self,
        max_queue_size: int = DEFAULT_MAX_QUEUE_SIZE,
        backpressure: BackpressurePolicy = BackpressurePolicy.DROP_OLDEST,
    ) -> None:
        self._max_queue_size = max_queue_size
        self._backpressure = backpressure
        self._queues: Dict[str, asyncio.Queue] = {}
        self._lock = asyncio.Lock()
        self._closed = False
        logger.info(
            "EventBus initialised: max_queue_size=%d, backpressure=%s",
            max_queue_size, backpressure.value,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def subscriber_count(self) -> int:
        """Number of currently connected subscribers."""
        return len(self._queues)

    @property
    def is_closed(self) -> bool:
        return self._closed

    async def subscribe(
        self,
        subscriber_id: Optional[str] = None,
    ) -> str:
        """Register a subscriber and return its unique ID.

        The caller should then iterate via ``events(subscriber_id)``
        to receive events.

        Args:
            subscriber_id: Optional explicit ID. Auto-generated when omitted.

        Returns:
            The subscriber ID.
        """
        if self._closed:
            raise RuntimeError("EventBus is closed")
        if subscriber_id is None:
            subscriber_id = "sub_" + uuid.uuid4().hex[:12]
        async with self._lock:
            if subscriber_id in self._queues:
                logger.warning("Subscriber '%s' already registered — overwriting", subscriber_id)
            self._queues[subscriber_id] = asyncio.Queue(maxsize=self._max_queue_size)
        logger.debug("Subscriber '%s' registered (%d total)", subscriber_id, self.subscriber_count)
        return subscriber_id

    async def unsubscribe(self, subscriber_id: str) -> None:
        """Remove a subscriber and discard its pending queue."""
        async with self._lock:
            q = self._queues.pop(subscriber_id, None)
            if q is not None and not self._closed:
                # Drain the queue to allow GC
                while not q.empty():
                    try:
                        q.get_nowait()
                        q.task_done()
                    except asyncio.QueueEmpty:
                        break
        logger.debug("Subscriber '%s' unsubscribed (%d remaining)", subscriber_id, self.subscriber_count)

    async def publish(
        self,
        event_type: str,
        data: Dict[str, Any],
        *,
        tenant: str = "",
    ) -> None:
        """Publish an event to all subscribers.

        Args:
            event_type: Canonical event type string.
            data:       Event payload dict.
            tenant:     Optional tenant for multi-tenant isolation.
        """
        if self._closed:
            logger.warning("EventBus is closed — dropping event '%s'", event_type)
            return

        event = EventBusEvent(
            event_type=event_type,
            data=data,
            tenant=tenant,
        )

        async with self._lock:
            stale: List[str] = []
            for sub_id, q in list(self._queues.items()):
                try:
                    q.put_nowait(event)
                except asyncio.QueueFull:
                    if self._backpressure == BackpressurePolicy.DROP_OLDEST:
                        # Drop the oldest item to make room
                        try:
                            q.get_nowait()
                            q.task_done()
                            q.put_nowait(event)
                        except (asyncio.QueueEmpty, asyncio.QueueFull):
                            pass  # Give up — drop this event for this subscriber
                    elif self._backpressure == BackpressurePolicy.DROP_NEWEST:
                        pass  # Silently drop the new event for this subscriber
                    else:  # BLOCK — shouldn't happen, but log a warning
                        logger.warning(
                            "EventBus queue full for '%s' — blocking publish is not safe",
                            sub_id,
                        )
                except Exception:
                    stale.append(sub_id)
                    logger.exception("Error publishing to subscriber '%s'", sub_id)

        logger.debug("Published '%s' event_id=%s to %d subscribers",
                      event_type, event.event_id, len(self._queues))

    async def events(self, subscriber_id: str) -> AsyncIterator[EventBusEvent]:
        """Async generator that yields events for *subscriber_id*.

        Usage::

            async for event in bus.events(sub_id):
                print(event.event_type, event.data)

        Yields:
            ``EventBusEvent`` instances in FIFO order.
        """
        q = self._queues.get(subscriber_id)
        if q is None:
            raise ValueError(f"Subscriber '{subscriber_id}' not found")

        try:
            while not self._closed:
                try:
                    event = await asyncio.wait_for(q.get(), timeout=1.0)
                    q.task_done()
                    yield event
                except asyncio.TimeoutError:
                    continue  # Loop back to check _closed
        except asyncio.CancelledError:
            pass

    def _publish_sync(self, event_type: str, data: Dict[str, Any],
                      *, tenant: str = "") -> None:
        """Synchronous publish — for use in non-async contexts.

        Creates an asyncio task in the current loop.
        """
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.ensure_future(self.publish(event_type, data, tenant=tenant))
            else:
                logger.warning("No running event loop — dropping event '%s'", event_type)
        except RuntimeError:
            logger.warning("No event loop available — dropping event '%s'", event_type)

    async def close(self) -> None:
        """Close the EventBus, discarding all subscribers and pending events."""
        self._closed = True
        async with self._lock:
            for sub_id, q in list(self._queues.items()):
                while not q.empty():
                    try:
                        q.get_nowait()
                        q.task_done()
                    except asyncio.QueueEmpty:
                        break
            self._queues.clear()
        logger.info("EventBus closed")
