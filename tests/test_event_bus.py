"""Tests for EventBus — in-memory event bus.

Covers:
- Publish / subscribe (broadcast)
- Multiple subscribers
- Backpressure (queue full → drop oldest)
- Tenant isolation
- Unsubscribe / close
- Error handling
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List

import pytest

from simple_a2a_registry.events.event_bus import (
    EventBus,
    EventBusEvent,
    EventTypes,
    BackpressurePolicy,
)

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def bus() -> EventBus:
    b = EventBus()
    yield b
    await b.close()


async def _collect_events(
    bus: EventBus,
    sub_id: str,
    max_events: int = 5,
    timeout: float = 2.0,
) -> List[Dict[str, Any]]:
    """Helper: subscribe and collect *max_events* from the bus."""
    results: List[Dict[str, Any]] = []

    try:
        async for event in bus.events(sub_id):
            results.append({"event_type": event.event_type, "data": event.data, "tenant": event.tenant})
            if len(results) >= max_events:
                break
    except asyncio.TimeoutError:
        pass

    return results


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestEventBusPublishSubscribe:
    """Core publish/subscribe broadcast behaviour."""

    async def test_subscribe_and_receive(self, bus: EventBus) -> None:
        """Subscribe, publish, and receive an event."""
        sub_id = await bus.subscribe()
        assert sub_id.startswith("sub_")

        await bus.publish("task.created", {"task_id": "t_001", "title": "Test"})

        events = await _collect_events(bus, sub_id, max_events=1, timeout=1.0)
        assert len(events) == 1
        assert events[0]["event_type"] == "task.created"
        assert events[0]["data"]["task_id"] == "t_001"
        assert events[0]["data"]["title"] == "Test"

    async def test_broadcast_to_multiple_subscribers(self, bus: EventBus) -> None:
        """All subscribers receive every published event (broadcast)."""
        sub_a = await bus.subscribe()
        sub_b = await bus.subscribe()

        await bus.publish("task.created", {"id": "t_001"})

        events_a = await _collect_events(bus, sub_a, max_events=1, timeout=1.0)
        events_b = await _collect_events(bus, sub_b, max_events=1, timeout=1.0)

        assert len(events_a) == 1
        assert len(events_b) == 1
        assert events_a[0]["data"]["id"] == "t_001"
        assert events_b[0]["data"]["id"] == "t_001"

    async def test_unsubscribe_stops_receiving(self, bus: EventBus) -> None:
        """After unsubscribing, the subscriber no longer receives events."""
        sub_id = await bus.subscribe()
        await bus.publish("task.created", {"id": "t_001"})
        events_before = await _collect_events(bus, sub_id, max_events=1, timeout=0.5)
        assert len(events_before) == 1

        await bus.unsubscribe(sub_id)

        # After unsubscribe, the subscriber queue is removed — events() raises ValueError
        with pytest.raises(ValueError, match="not found"):
            async for _ in bus.events(sub_id):
                pass

    async def test_event_auto_id_and_timestamp(self, bus: EventBus) -> None:
        """Events get auto-generated IDs and timestamps."""
        sub_id = await bus.subscribe()
        await bus.publish("test.event", {"msg": "hello"})

        # Get the raw EventBusEvent to check fields
        q = bus._queues[sub_id]
        event: EventBusEvent = await asyncio.wait_for(q.get(), timeout=1.0)
        q.task_done()

        assert event.event_id.startswith("evt_")
        assert event.timestamp > 0
        assert event.event_type == "test.event"
        assert event.data["msg"] == "hello"

    async def test_tenant_isolation(self, bus: EventBus) -> None:
        """Events carry tenant info; filtering is client-side."""
        sub_id = await bus.subscribe()

        await bus.publish("task.created", {"id": "t_001"}, tenant="tenant-a")
        await bus.publish("task.created", {"id": "t_002"}, tenant="tenant-b")

        events = await _collect_events(bus, sub_id, max_events=2, timeout=1.0)
        assert len(events) == 2
        # Both events arrive (bus is broadcast-only; filtering is done by consumer)
        tenants = [e["tenant"] for e in events]
        assert "tenant-a" in tenants
        assert "tenant-b" in tenants

    async def test_publish_after_close(self, bus: EventBus) -> None:
        """Publishing after close is a no-op (no crash)."""
        await bus.close()
        assert bus.is_closed

        # This should not raise
        await bus.publish("task.created", {"id": "t_001"})


class TestEventBusBackpressure:
    """Backpressure handling when subscriber queues are full."""

    async def test_drop_oldest_backpressure(self) -> None:
        """When queue is full and backpressure=DROP_OLDEST, oldest event is dropped."""
        bus = EventBus(max_queue_size=2, backpressure=BackpressurePolicy.DROP_OLDEST)
        sub_id = await bus.subscribe()

        # Publish 3 events (queue size is 2)
        await bus.publish("task.created", {"id": "t_001"})
        await bus.publish("task.created", {"id": "t_002"})
        await bus.publish("task.created", {"id": "t_003"})

        # Read both events — the oldest (t_001) should have been dropped
        q = bus._queues[sub_id]
        ev1: EventBusEvent = await asyncio.wait_for(q.get(), timeout=1.0)
        q.task_done()
        ev2: EventBusEvent = await asyncio.wait_for(q.get(), timeout=1.0)
        q.task_done()

        assert ev1.data["id"] == "t_002"
        assert ev2.data["id"] == "t_003"
        assert q.empty()

        await bus.close()

    async def test_drop_newest_backpressure(self) -> None:
        """When queue is full and backpressure=DROP_NEWEST, new event is dropped."""
        bus = EventBus(max_queue_size=2, backpressure=BackpressurePolicy.DROP_NEWEST)
        sub_id = await bus.subscribe()

        await bus.publish("task.created", {"id": "t_001"})
        await bus.publish("task.created", {"id": "t_002"})
        # Third publish is dropped
        await bus.publish("task.created", {"id": "t_003"})

        q = bus._queues[sub_id]
        ev1: EventBusEvent = await asyncio.wait_for(q.get(), timeout=1.0)
        q.task_done()
        ev2: EventBusEvent = await asyncio.wait_for(q.get(), timeout=1.0)
        q.task_done()

        assert ev1.data["id"] == "t_001"
        assert ev2.data["id"] == "t_002"
        assert q.empty()

        await bus.close()

    async def test_subscriber_count(self, bus: EventBus) -> None:
        """Subscriber count is accurate."""
        assert bus.subscriber_count == 0

        s1 = await bus.subscribe()
        assert bus.subscriber_count == 1

        s2 = await bus.subscribe()
        assert bus.subscriber_count == 2

        await bus.unsubscribe(s1)
        assert bus.subscriber_count == 1

        await bus.unsubscribe(s2)
        assert bus.subscriber_count == 0


class TestEventTypes:
    """EventTypes constants consistency."""

    def test_task_event_types(self) -> None:
        assert EventTypes.TASK_CREATED == "task.created"
        assert EventTypes.TASK_CLAIMED == "task.claimed"
        assert EventTypes.TASK_COMPLETED == "task.completed"
        assert EventTypes.TASK_BLOCKED == "task.blocked"
        assert EventTypes.SECURITY_VIOLATION == "security.violation"
        assert EventTypes.SYSTEM_AGENT_REGISTERED == "system.agent_registered"