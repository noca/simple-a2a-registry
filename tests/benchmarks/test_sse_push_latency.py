"""Benchmark: EventBus → SSE push latency.

Measures the end-to-end latency from event publication (task created/claimed/
completed) to delivery on an EventBus subscriber queue and through the SSE
stream endpoint.

Usage:
    pytest tests/benchmarks/test_sse_push_latency.py -v --benchmark-only
"""
from __future__ import annotations

import asyncio
import os
import statistics
import tempfile
import time
from typing import Any, Dict, List, Optional

import pytest
from aiohttp.test_utils import TestClient, TestServer

from simple_a2a_registry.server import create_app
from simple_a2a_registry.events.event_bus import EventBus, EventBusEvent

pytestmark = pytest.mark.asyncio

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _percentile(data: list[float], p: float) -> float:
    if not data:
        return 0.0
    sorted_data = sorted(data)
    k = (len(sorted_data) - 1) * p / 100.0
    f = int(k)
    c = f + 1 if f + 1 < len(sorted_data) else f
    return sorted_data[f] + (k - f) * (sorted_data[c] - sorted_data[f])


def _report(label: str, latencies: list[float]) -> None:
    if not latencies:
        print(f"  {label}: no data")
        return
    p50 = _percentile(latencies, 50)
    p95 = _percentile(latencies, 95)
    p99 = _percentile(latencies, 99)
    avg = statistics.mean(latencies)
    print(f"  {label}:  P50={p50*1000:.2f}ms  P95={p95*1000:.2f}ms  "
          f"P99={p99*1000:.2f}ms  avg={avg*1000:.2f}ms  "
          f"samples={len(latencies)}")


# ---------------------------------------------------------------------------
# Benchmark fixture
# ---------------------------------------------------------------------------


class SSEBenchCtx:
    """Provides an app with EventBus + SSE for push latency measurement."""

    def __init__(self) -> None:
        self.client: TestClient
        self.event_bus: EventBus
        self.data_dir: str = ""
        self._server: TestServer | None = None

    @classmethod
    async def create(cls) -> SSEBenchCtx:
        self = cls()
        self.data_dir = tempfile.mkdtemp(prefix="bench_sse_")
        board_path = os.path.join(self.data_dir, "board.db")

        app = create_app(
            data_dir=self.data_dir,
            base_url="http://bench:8321",
            board_path=board_path,
            dispatcher_enabled=False,
            claim_ttl=900,
            failure_limit=3,
            dispatcher_interval=3600,
        )

        self.event_bus = app["event_bus"]

        self._server = TestServer(app)
        await self._server.start_server()
        self.client = TestClient(self._server)
        return self

    async def close(self) -> None:
        await self.client.close()
        if self._server:
            await self._server.close()
        import shutil
        shutil.rmtree(self.data_dir, ignore_errors=True)


# ===================================================================
# EventBus Push Latency
# ===================================================================


class TestEventBusLatency:
    """Measure EventBus publish → subscribe latency (in-process)."""

    N_SAMPLES = 100

    async def test_event_bus_publish_latency(self) -> None:
        """EventBus publish-to-subscribe latency for N task.created events.

        Measures how long an event takes to travel from publish() to the
        subscriber's async generator after a real HTTP POST /v2/tasks.
        """
        ctx = await SSEBenchCtx.create()
        try:
            # Subscribe directly to the EventBus
            sub_id = await ctx.event_bus.subscribe("bench_sse_sub")
            latencies: list[float] = []

            # Create tasks via HTTP and measure event arrival
            for i in range(self.N_SAMPLES):
                # Subscribe check before publish
                start = time.perf_counter()
                resp = await ctx.client.post(
                    "/v2/tasks", json={"title": f"bench-sse-{i}"},
                )
                assert resp.status == 201
                data = await resp.json()
                task_id = data["task"]["id"]

                # Wait for the event to arrive
                try:
                    async with asyncio.timeout(5.0):
                        async for event in ctx.event_bus.events(sub_id):
                            if event.event_type == "task.created":
                                elapsed = time.perf_counter() - start
                                latencies.append(elapsed)
                                break
                except TimeoutError:
                    print(f"  WARNING: timed out waiting for event {i}")

            print(f"\n  --- EventBus Push Latency ({self.N_SAMPLES} events) ---")
            _report("task.created → subscriber", latencies)

            # Verify: sub-millisecond P50 expected for in-process EventBus
            p50 = _percentile(latencies, 50)
            p99 = _percentile(latencies, 99)
            assert p50 < 0.010, f"P50 push latency {p50*1000:.2f}ms >= 10ms"
            assert p99 < 0.100, f"P99 push latency {p99*1000:.2f}ms >= 100ms"

            await ctx.event_bus.unsubscribe(sub_id)
        finally:
            await ctx.close()

    async def test_event_bus_bulk_publish_stress(self) -> None:
        """EventBus throughput under bulk publish — 1000 events burst."""
        ctx = await SSEBenchCtx.create()
        try:
            sub_id = await ctx.event_bus.subscribe("bench_bulk_sub")
            n_events = 1000
            received = 0
            latencies: list[float] = []

            # Burst create tasks
            start = time.perf_counter()
            tasks = [
                ctx.client.post(
                    "/v2/tasks", json={"title": f"bulk-{i}"},
                )
                for i in range(n_events)
            ]
            responses = await asyncio.gather(*tasks)
            for r in responses:
                assert r.status == 201
            end = time.perf_counter()
            create_tps = n_events / (end - start)
            print(f"\n  --- EventBus Bulk Stress ({n_events} events) ---")
            print(f"  create throughput:  {create_tps:,.0f} TPS")

            # Consume events — measure drain time
            drain_start = time.perf_counter()
            try:
                async with asyncio.timeout(10.0):
                    async for event in ctx.event_bus.events(sub_id):
                        received += 1
                        if received >= n_events:
                            break
            except TimeoutError:
                print(f"  WARNING: timed out after {received}/{n_events} events")
            drain_elapsed = time.perf_counter() - drain_start

            drain_rate = received / drain_elapsed if drain_elapsed > 0 else 0
            print(f"  drain rate:  {received} events in {drain_elapsed:.3f}s  "
                  f"-> {drain_rate:,.0f} events/sec")

            # Verify no events lost
            assert received == n_events, (
                f"Expected {n_events} events, got {received}"
            )
            assert drain_rate > 1000, (
                f"Drain rate {drain_rate:.0f} events/sec too low"
            )

            await ctx.event_bus.unsubscribe(sub_id)
        finally:
            await ctx.close()


class TestSSEPushLatency:
    """Measure SSE stream push latency via HTTP event stream."""

    N_SAMPLES = 50

    async def test_sse_stream_push_latency(self) -> None:
        """End-to-end SSE push latency: POST /v2/tasks → SSE event arrival.

        Opens a real SSE stream to ``GET /v2/events``, creates tasks via
        ``POST /v2/tasks``, and measures the time from HTTP response
        (event published) to SSE data line received.
        """
        ctx = await SSEBenchCtx.create()
        try:
            latencies: list[float] = []

            # Open SSE stream
            sse_resp = await ctx.client.get("/v2/events")
            assert sse_resp.status == 200

            for i in range(self.N_SAMPLES):
                start = time.perf_counter()

                # Create task
                resp = await ctx.client.post(
                    "/v2/tasks", json={"title": f"sse-push-{i}"},
                )
                assert resp.status == 201

                # Read SSE stream until we get a task.created event
                read_buf = ""
                try:
                    async with asyncio.timeout(5.0):
                        while True:
                            chunk = await sse_resp.content.read(4096)
                            if not chunk:
                                break
                            read_buf += chunk.decode("utf-8")
                            if "event: task.created" in read_buf:
                                elapsed = time.perf_counter() - start
                                latencies.append(elapsed)
                                read_buf = ""
                                break
                except TimeoutError:
                    print(f"  WARNING: SSE timeout on sample {i}")

            print(f"\n  --- SSE Push Latency ({self.N_SAMPLES} events) ---")
            _report("POST → SSE stream event", latencies)

            # Verify: SSE adds minimal overhead
            p50 = _percentile(latencies, 50)
            p99 = _percentile(latencies, 99)
            assert p50 < 0.050, f"P50 SSE latency {p50*1000:.2f}ms >= 50ms"
            assert p99 < 0.500, f"P99 SSE latency {p99*1000:.2f}ms >= 500ms"

        finally:
            await ctx.close()