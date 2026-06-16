"""HTTP-level throughput benchmarks via aiohttp TestClient.

Measures end-to-end through the full API stack (middleware, serialization,
store) without the dispatcher running (background noise suppressed).

Usage:
    pytest tests/benchmarks/test_task_throughput.py -v --benchmark-only
    pytest tests/benchmarks/ -v --benchmark-only
"""
from __future__ import annotations

import asyncio
import os
import statistics
import tempfile
import time
from typing import Any, Dict, List, Optional, Tuple

import pytest

from simple_a2a_registry.server import create_app
from simple_a2a_registry.config import (
    Config,
    DatabaseConfig,
    SecurityHarnessConfig,
    OrchestrationConfig,
    ServerConfig,
)
from aiohttp.test_utils import TestClient, TestServer

pytestmark = pytest.mark.asyncio

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _percentile(data: List[float], p: float) -> float:
    if not data:
        return 0.0
    sorted_data = sorted(data)
    k = (len(sorted_data) - 1) * p / 100.0
    f = int(k)
    c = f + 1 if f + 1 < len(sorted_data) else f
    return sorted_data[f] + (k - f) * (sorted_data[c] - sorted_data[f])


def _report(label: str, latencies: List[float]) -> None:
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


def _tps_report(label: str, n: int, elapsed: float) -> None:
    tps = n / elapsed if elapsed > 0 else 0
    print(f"  {label}:  {n} ops in {elapsed:.3f}s  ->  {tps:,.0f} TPS")


# ---------------------------------------------------------------------------
# Benchmark fixture — full HTTP stack, no dispatcher
# ---------------------------------------------------------------------------


class BenchCtx:
    """aiohttp TestClient wrapper with convenience methods.

    Dispatcher is disabled to avoid background noise; the EventBus and
    middleware are live, giving realistic end-to-end numbers.
    """

    def __init__(self) -> None:
        self.client: TestClient
        self.app: Any = None
        self.data_dir: str = ""
        self._server: Optional[TestServer] = None
        self._task_store: Any = None

    @classmethod
    async def create(
        cls,
        security_enabled: bool = False,
    ) -> BenchCtx:
        self = cls()
        self.data_dir = tempfile.mkdtemp(prefix="bench_")
        board_path = os.path.join(self.data_dir, "board.db")

        if security_enabled:
            # Full config with Security Harness enabled (warn mode)
            cfg = Config(
                server=ServerConfig(host="127.0.0.1", port=0),
                database=DatabaseConfig(
                    driver="sqlite",
                    sqlite_path=os.path.join(self.data_dir, "registry.db"),
                ),
                orchestration=OrchestrationConfig(
                    dispatcher_enabled=False,
                    dispatcher_interval=3600,
                    claim_ttl=900,
                    failure_limit=3,
                    board_path=board_path,
                ),
                security_harness=SecurityHarnessConfig(
                    enabled=True,
                    mode="warn",
                    default_delegation_policy="open",
                    delegation_token_ttl_seconds=300,
                    max_delegation_depth=10,
                ),
            )
            app = create_app(
                data_dir=self.data_dir,
                base_url="http://bench:8321",
                config=cfg,
                board_path=board_path,
                dispatcher_enabled=False,
                claim_ttl=900,
                failure_limit=3,
                dispatcher_interval=3600,
            )
        else:
            app = create_app(
                data_dir=self.data_dir,
                base_url="http://bench:8321",
                board_path=board_path,
                dispatcher_enabled=False,
                claim_ttl=900,
                failure_limit=3,
                dispatcher_interval=3600,
            )

        self._server = TestServer(app)
        await self._server.start_server()
        self.client = TestClient(self._server)
        self.app = app
        self._task_store = app["task_store"]
        return self

    async def close(self) -> None:
        await self.client.close()
        if self._server:
            await self._server.close()
        import shutil
        shutil.rmtree(self.data_dir, ignore_errors=True)

    # --- Convenience wrappers ---

    async def create_task(self, **kwargs: Any) -> Tuple[float, Dict[str, Any]]:
        body = {"title": kwargs.pop("title", "bench"), **kwargs}
        start = time.perf_counter()
        resp = await self.client.post("/v2/tasks", json=body)
        elapsed = time.perf_counter() - start
        data = await resp.json()
        return elapsed, data

    async def claim_task(
        self, task_id: str, worker_id: str = "bench-worker", pid: int = 1,
    ) -> Tuple[float, Dict[str, Any]]:
        start = time.perf_counter()
        resp = await self.client.post(
            f"/v2/tasks/{task_id}/claim",
            json={"worker_id": worker_id, "pid": pid},
        )
        elapsed = time.perf_counter() - start
        data = await resp.json()
        return elapsed, data

    async def complete_task(
        self, task_id: str, claim_lock: str,
    ) -> Tuple[float, Dict[str, Any]]:
        start = time.perf_counter()
        resp = await self.client.post(
            f"/v2/tasks/{task_id}/complete",
            json={"claim_lock": claim_lock, "summary": "bench done"},
        )
        elapsed = time.perf_counter() - start
        data = await resp.json()
        return elapsed, data

    async def heartbeat_task(
        self, task_id: str, claim_lock: str,
    ) -> Tuple[float, Dict[str, Any]]:
        start = time.perf_counter()
        resp = await self.client.post(
            f"/v2/tasks/{task_id}/heartbeat",
            json={"claim_lock": claim_lock},
        )
        elapsed = time.perf_counter() - start
        data = await resp.json()
        return elapsed, data

    async def get_task(self, task_id: str) -> Tuple[float, Dict[str, Any]]:
        start = time.perf_counter()
        resp = await self.client.get(f"/v2/tasks/{task_id}")
        elapsed = time.perf_counter() - start
        data = await resp.json()
        return elapsed, data

    async def create_n_tasks(self, n: int, **kwargs: Any) -> List[str]:
        created: List[str] = []

        async def _create(idx: int) -> None:
            _, data = await self.create_task(
                title=f"bench-{idx}", **kwargs,
            )
            created.append(data["task"]["id"])

        await asyncio.gather(*[_create(i) for i in range(n)])
        return created


# ===================================================================
# Throughput benchmarks
# ===================================================================


class TestTaskThroughput:
    """End-to-end HTTP throughput benchmarks — Task create, claim, complete."""

    async def test_create_100_tasks(self) -> None:
        """Throughput: create 100 tasks via HTTP POST /v2/tasks."""
        ctx = await BenchCtx.create()
        try:
            n = 100
            start = time.perf_counter()
            tids = await ctx.create_n_tasks(n)
            elapsed = time.perf_counter() - start
            _tps_report("HTTP create 100 tasks", n, elapsed)
            assert len(tids) == n
        finally:
            await ctx.close()

    async def test_create_500_tasks(self) -> None:
        """Throughput: create 500 tasks via HTTP POST /v2/tasks."""
        ctx = await BenchCtx.create()
        try:
            n = 500
            start = time.perf_counter()
            tids = await ctx.create_n_tasks(n)
            elapsed = time.perf_counter() - start
            _tps_report("HTTP create 500 tasks", n, elapsed)
            assert len(tids) == n
        finally:
            await ctx.close()

    async def test_create_1000_tasks(self) -> None:
        """Throughput: create 1000 tasks via HTTP POST /v2/tasks."""
        ctx = await BenchCtx.create()
        try:
            n = 1000
            start = time.perf_counter()
            tids = await ctx.create_n_tasks(n)
            elapsed = time.perf_counter() - start
            _tps_report("HTTP create 1000 tasks", n, elapsed)
            assert len(tids) == n
        finally:
            await ctx.close()

    async def test_claim_complete_cycle(self) -> None:
        """Throughput: create -> claim -> complete cycle for 500 tasks."""
        ctx = await BenchCtx.create()
        try:
            n = 500
            tids = await ctx.create_n_tasks(n)

            start = time.perf_counter()
            for tid in tids:
                _, claim_data = await ctx.claim_task(tid, "cycle-w", 1)
                lock = claim_data.get("claim_lock", "")
                await ctx.complete_task(tid, lock)
            elapsed = time.perf_counter() - start

            _tps_report("HTTP create→claim→complete", n, elapsed)
        finally:
            await ctx.close()

    async def test_concurrent_claim_stress(self) -> None:
        """Stress: 10 workers racing to claim 500 tasks."""
        ctx = await BenchCtx.create()
        try:
            n_tasks = 500
            n_workers = 10
            tids = await ctx.create_n_tasks(n_tasks)

            claimed: List[str] = []
            lock = asyncio.Lock()

            async def _worker(wid: int) -> None:
                for tid in tids:
                    _, data = await ctx.claim_task(
                        tid, worker_id=f"worker-{wid}", pid=wid,
                    )
                    if "claim_lock" in data:
                        async with lock:
                            claimed.append(tid)

            start = time.perf_counter()
            workers = [asyncio.create_task(_worker(w))
                       for w in range(n_workers)]
            await asyncio.gather(*workers)
            elapsed = time.perf_counter() - start

            _tps_report("HTTP claim (10 workers × 500 tasks)",
                        len(claimed), elapsed)
            assert len(claimed) == n_tasks
            assert len(set(claimed)) == n_tasks
        finally:
            await ctx.close()

    async def test_latency_p50_p95_p99(self) -> None:
        """Latency percentiles for core operations (sampled, not benchmarked)."""
        ctx = await BenchCtx.create()
        try:
            n = 200
            tids = await ctx.create_n_tasks(n)

            claim_lat: List[float] = []
            complete_lat: List[float] = []
            heartbeat_lat: List[float] = []
            get_lat: List[float] = []

            for tid in tids:
                # Claim
                c_el, claim_data = await ctx.claim_task(tid, "lat-w", 1)
                claim_lat.append(c_el)
                lock = claim_data.get("claim_lock", "")

                # Complete
                co_el, _ = await ctx.complete_task(tid, lock)
                complete_lat.append(co_el)

                # Heartbeat (re-claim first)
                h_el, h_data = await ctx.claim_task(tid, "lat-w", 1)
                h_lock = h_data.get("claim_lock", "")
                _, _ = await ctx.heartbeat_task(tid, h_lock)
                heartbeat_lat.append(h_el)

                # Get
                g_el, _ = await ctx.get_task(tid)
                get_lat.append(g_el)

            print("\n  --- Latency Percentiles (HTTP) ---")
            _report("claim_task", claim_lat)
            _report("complete_task", complete_lat)
            _report("heartbeat_task", heartbeat_lat)
            _report("get_task", get_lat)

            # Baseline sanity
            p99_claim = _percentile(claim_lat, 99)
            assert p99_claim < 1.0, f"P99 claim latency exceeded 1s: {p99_claim:.3f}s"
        finally:
            await ctx.close()
