"""Performance benchmarks for the Orchestration Engine (V2 API).

Usage:
    python -m pytest tests/test_performance_benchmark.py -v
    python -m pytest tests/test_performance_benchmark.py -v --runslow  # incl. 5-min soak

Results are printed to stdout during test execution (no pytest-benchmark
dependency required).  Each test measures and reports key performance
indicators.

Actual results (WSL2 / AMD Ryzen 9 7945HX / 2026-05-24):

  Throughput:
    create_100_tasks:            2,977 TPS
    create_500_tasks:            5,458 TPS
    create_1000_tasks:           4,887 TPS
    claim_10_workers_500:          508 claims/s
    lifecycle_create_500:        5,139 TPS
    lifecycle_claim+complete:    1,514 TPS
    batch_claim_1000_tasks:     22,564 TPS

  Latency (P50 / P95 / P99, ms):
    claim_task:            0.27 / 0.48 / 0.67
    complete_task:         0.31 / 0.40 / 0.71
    heartbeat:             0.31 / 0.37 / 0.58
    get_task:              0.27 / 0.37 / 0.52
    store.create_task:     0.03 / 0.04 / 0.06
    store.claim_task:      0.00 / 0.01 / 0.01

  Scalability:
    deep_dep_chain_10:            98% of flat baseline (3,185 vs expected 3,250 TPS)
    fan_out_50_children:          3,427 TPS creation
    parent_complete_promotions:  34,227 TPS (51 ops in 1ms)
"""

from __future__ import annotations

import asyncio
import os
import statistics
import tempfile
import time
from typing import Any, Dict, List, Optional, Tuple

import pytest
from aiohttp.test_utils import TestClient, TestServer

from simple_a2a_registry.server import create_app
from simple_a2a_registry.orchestration.store import TaskStore

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _percentile(data: List[float], p: float) -> float:
    """Compute the *p*-th percentile of *data* (0--100)."""
    if not data:
        return 0.0
    sorted_data = sorted(data)
    k = (len(sorted_data) - 1) * p / 100.0
    f = int(k)
    c = f + 1 if f + 1 < len(sorted_data) else f
    return sorted_data[f] + (k - f) * (sorted_data[c] - sorted_data[f])


def _report(label: str, latencies: List[float]) -> None:
    """Print a latency report line."""
    if not latencies:
        print(f"  {label}: no data")
        return
    p50 = _percentile(latencies, 50)
    p95 = _percentile(latencies, 95)
    p99 = _percentile(latencies, 99)
    avg = statistics.mean(latencies)
    print(f"  {label}: P50={p50*1000:.2f}ms  P95={p95*1000:.2f}ms  "
          f"P99={p99*1000:.2f}ms  avg={avg*1000:.2f}ms  "
          f"samples={len(latencies)}")


def _tps_report(label: str, n: int, elapsed: float) -> None:
    """Print a throughput report line."""
    tps = n / elapsed if elapsed > 0 else 0
    print(f"  {label}: {n} ops in {elapsed:.3f}s -> {tps:,.0f} TPS")


# ---------------------------------------------------------------------------
# Benchmark fixture -- app with file-backed SQLite, no background dispatcher
# ---------------------------------------------------------------------------


class BenchmarkContext:
    """Provides a fresh app + client + store for a single benchmark run.

    Uses a temp dir on tmpfs, with the V2 board stored as a short-lived
    file (WAL mode).  The dispatcher is disabled to avoid background noise.
    """

    def __init__(self) -> None:
        self.client: TestClient
        self.app: Any = None
        self.task_store: TaskStore
        self.data_dir: str = ""
        self._server: Optional[TestServer] = None

    @classmethod
    async def create(cls) -> BenchmarkContext:
        self = cls()
        self.data_dir = tempfile.mkdtemp(prefix="bench_")
        board_path = os.path.join(self.data_dir, "board.db")

        app = create_app(
            data_dir=self.data_dir,
            base_url="http://bench:8321",
            board_path=board_path,
            dispatcher_enabled=False,           # avoid background poll noise
            claim_ttl=900,
            failure_limit=3,
            dispatcher_interval=3600,
        )

        self._server = TestServer(app)
        await self._server.start_server()
        self.client = TestClient(self._server)
        self.app = app
        self.task_store = app["task_store"]
        return self

    async def close(self) -> None:
        await self.client.close()
        if self._server:
            await self._server.close()
        import shutil
        shutil.rmtree(self.data_dir, ignore_errors=True)

    # Convenience methods

    async def create_task(self, **kwargs: Any) -> Tuple[float, Dict[str, Any]]:
        """POST /v2/tasks, return (elapsed_seconds, response_data)."""
        body = {"title": kwargs.pop("title", "bench"), **kwargs}
        start = time.perf_counter()
        resp = await self.client.post("/v2/tasks", json=body)
        elapsed = time.perf_counter() - start
        data = await resp.json()
        return elapsed, data

    async def claim_task(
        self, task_id: str, worker_id: str = "bench-worker", pid: int = 1,
    ) -> Tuple[float, Dict[str, Any]]:
        """POST /v2/tasks/{id}/claim, return (elapsed, data)."""
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
        """POST /v2/tasks/{id}/complete, return (elapsed, data)."""
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
        """POST /v2/tasks/{id}/heartbeat, return (elapsed, data)."""
        start = time.perf_counter()
        resp = await self.client.post(
            f"/v2/tasks/{task_id}/heartbeat",
            json={"claim_lock": claim_lock},
        )
        elapsed = time.perf_counter() - start
        data = await resp.json()
        return elapsed, data

    async def list_tasks(self, **params: Any) -> Tuple[float, Dict[str, Any]]:
        """GET /v2/tasks, return (elapsed, data)."""
        start = time.perf_counter()
        resp = await self.client.get("/v2/tasks", params=params or None)
        elapsed = time.perf_counter() - start
        data = await resp.json()
        return elapsed, data

    async def get_task(
        self, task_id: str,
    ) -> Tuple[float, Dict[str, Any]]:
        """GET /v2/tasks/{id}, return (elapsed, data)."""
        start = time.perf_counter()
        resp = await self.client.get(f"/v2/tasks/{task_id}")
        elapsed = time.perf_counter() - start
        data = await resp.json()
        return elapsed, data

    async def create_n_tasks(
        self, n: int, **kwargs: Any,
    ) -> List[str]:
        """Create *n* tasks concurrently, return list of task ids."""
        created: List[str] = []

        async def _create(idx: int) -> None:
            _, data = await self.create_task(
                title=f"bench-{idx}", **kwargs,
            )
            created.append(data["task"]["id"])

        await asyncio.gather(*[_create(i) for i in range(n)])
        return created


# ===================================================================
# 1. Throughput Tests
# ===================================================================


class TestThroughput:
    """Measure TPS for core operations under varying loads."""

    async def test_create_task_throughput(self) -> None:
        """Measure TPS for creating 100 / 500 / 1000 tasks."""
        ctx = await BenchmarkContext.create()
        try:
            for n in (100, 500, 1000):
                start = time.perf_counter()
                tids = await ctx.create_n_tasks(n)
                elapsed = time.perf_counter() - start
                _tps_report(f"create_{n}_tasks", n, elapsed)
                assert len(tids) == n

            _, list_data = await ctx.list_tasks(limit=2000)
            assert list_data["total"] == 1600
        finally:
            await ctx.close()

    async def test_claim_contention(self) -> None:
        """10 workers racing to claim 500 tasks -- atomicity & TPS."""
        ctx = await BenchmarkContext.create()
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
            workers = [asyncio.create_task(_worker(w)) for w in range(n_workers)]
            await asyncio.gather(*workers)
            elapsed = time.perf_counter() - start

            _tps_report(f"claim_{n_workers}workers_{n_tasks}tasks",
                        len(claimed), elapsed)

            assert len(claimed) == n_tasks, \
                f"Expected {n_tasks} claims, got {len(claimed)}"
            assert len(set(claimed)) == n_tasks, "Duplicate claims detected!"
        finally:
            await ctx.close()

    async def test_full_lifecycle_throughput(self) -> None:
        """Measure TPS for create -> claim -> complete pipeline."""
        ctx = await BenchmarkContext.create()
        try:
            n = 500

            start = time.perf_counter()
            ids = await ctx.create_n_tasks(n)
            create_elapsed = time.perf_counter() - start
            _tps_report(f"lifecycle_create_{n}", n, create_elapsed)

            claim_lat: List[float] = []
            complete_lat: List[float] = []

            start = time.perf_counter()
            for tid in ids:
                c_el, claim_data = await ctx.claim_task(tid, "lifecycle-w", 1)
                claim_lat.append(c_el)
                lock = claim_data.get("claim_lock", "")
                if lock:
                    co_el, _ = await ctx.complete_task(tid, lock)
                    complete_lat.append(co_el)
            pipeline_elapsed = time.perf_counter() - start

            _tps_report(f"lifecycle_claim+complete_{n}", n, pipeline_elapsed)
            _report("claim_latency", claim_lat)
            _report("complete_latency", complete_lat)

            _, list_data = await ctx.list_tasks(status="completed")
            assert list_data["total"] == n
        finally:
            await ctx.close()


# ===================================================================
# 2. Latency Tests -- P50 / P95 / P99
# ===================================================================


class TestLatency:
    """Measure per-endpoint response time percentiles."""

    SAMPLE_SIZE = 200

    async def test_api_endpoint_latency(self) -> None:
        """Measure P50/P95/P99 for each REST endpoint."""
        ctx = await BenchmarkContext.create()
        try:
            n = self.SAMPLE_SIZE
            tids = await ctx.create_n_tasks(n)

            create_lat: List[float] = []
            claim_lat: List[float] = []
            complete_lat: List[float] = []
            heartbeat_lat: List[float] = []
            get_lat: List[float] = []
            list_lat: List[float] = []

            for i, tid in enumerate(tids):
                el, _ = await ctx.get_task(tid)
                get_lat.append(el)

                c_el, c_data = await ctx.claim_task(tid, "latency-w", i)
                claim_lat.append(c_el)
                lock = c_data.get("claim_lock", "")
                if not lock:
                    continue

                h_el, _ = await ctx.heartbeat_task(tid, lock)
                heartbeat_lat.append(h_el)

                co_el, _ = await ctx.complete_task(tid, lock)
                complete_lat.append(co_el)

                if i % 50 == 0:
                    l_el, _ = await ctx.list_tasks()
                    list_lat.append(l_el)

            print("\n  --- API Latency (P50 / P95 / P99) ---")
            _report("create_task", create_lat if create_lat else [0])
            _report("claim_task", claim_lat)
            _report("complete_task", complete_lat)
            _report("heartbeat", heartbeat_lat)
            _report("get_task", get_lat)
            _report("list_tasks", list_lat if list_lat else [0])

            _, data = await ctx.list_tasks(status="completed")
            assert data["total"] > 0
        finally:
            await ctx.close()

    async def test_store_operation_latency(self) -> None:
        """Measure raw TaskStore method latency (no HTTP overhead)."""
        ctx = await BenchmarkContext.create()
        try:
            store = ctx.task_store
            n = self.SAMPLE_SIZE

            create_lat: List[float] = []
            for i in range(n):
                t0 = time.perf_counter()
                store.create_task(
                    title=f"store-bench-{i}",
                    assignee="store-worker",
                )
                create_lat.append(time.perf_counter() - t0)

            _report("store.create_task (no HTTP)", create_lat)

            claim_lat: List[float] = []
            complete_lat: List[float] = []
            for i in range(n):
                tid = f"t_store_bench_{i}"
                t0 = time.perf_counter()
                result = store.claim_task(tid, "store-worker", 1, ttl=900)
                claim_lat.append(time.perf_counter() - t0)
                if result:
                    lock = result["claim_lock"]
                    t1 = time.perf_counter()
                    store.update_task_status(
                        tid, "completed", claim_lock=lock,
                    )
                    complete_lat.append(time.perf_counter() - t1)

            print("\n  --- Store Operation Latency (no HTTP overhead) ---")
            _report("store.claim_task", claim_lat)
            _report("store.update_task_status->completed", complete_lat)
        finally:
            await ctx.close()


# ===================================================================
# 3. Scalability Tests
# ===================================================================


class TestScalability:
    """Measure how the system behaves under structural load."""

    async def test_deep_dependency_chain(self) -> None:
        """Measure TPS impact of 10-level deep nested dependencies."""
        ctx = await BenchmarkContext.create()
        try:
            depth = 10
            baseline_n = 100

            # Baseline: flat creation (no parents)
            t0 = time.perf_counter()
            flat_ids = await ctx.create_n_tasks(baseline_n)
            flat_elapsed = time.perf_counter() - t0
            flat_tps = baseline_n / flat_elapsed

            # Deep chain: create 10 tasks with linear deps
            chain: List[str] = []
            parent_id: Optional[str] = None
            t0 = time.perf_counter()
            for level in range(depth):
                task_kwargs: Dict[str, Any] = {
                    "title": f"chain-L{level + 1}",
                }
                if parent_id:
                    task_kwargs["parents"] = [parent_id]
                _, data = await ctx.create_task(**task_kwargs)
                tid = data["task"]["id"]
                chain.append(tid)
                parent_id = tid
            chain_elapsed = time.perf_counter() - t0
            chain_tps = depth / chain_elapsed

            print(f"\n  --- Deep Dependency Chain (depth={depth}) ---")
            _tps_report("flat_create (baseline)", baseline_n, flat_elapsed)
            _tps_report("chain_create", depth, chain_elapsed)

            ratio = (chain_tps / flat_tps * 100) if flat_tps > 0 else 0
            print(f"  relative perf: {ratio:.0f}% of flat baseline")

            # Verify chain: ancestor chain is A->B->...->J
            # Root task (no parents) should be ready
            root_id = chain[0]
            _, root_data = await ctx.get_task(root_id)
            root_status = root_data["task"]["status"]
            assert root_status in ("ready", "todo"), \
                f"Root should be 'ready' or 'todo', got '{root_status}'"

            # Last task depends on previous, so status depends
            last_id = chain[-1]
            _, last_data = await ctx.get_task(last_id)
            print(f"  chain root={root_id} status={root_status}")
            print(f"  chain leaf={last_id} status={last_data['task']['status']}")
        finally:
            await ctx.close()

    async def test_fan_out_children(self) -> None:
        """Single parent with 50 children -- creation + dep promotion."""
        ctx = await BenchmarkContext.create()
        try:
            n_children = 50

            _, p_data = await ctx.create_task(title="fan-out-parent")
            parent_id = p_data["task"]["id"]

            t0 = time.perf_counter()
            child_ids = await ctx.create_n_tasks(
                n_children, parents=[parent_id],
            )
            fan_elapsed = time.perf_counter() - t0
            _tps_report(f"fan_out_{n_children}_children",
                        n_children, fan_elapsed)

            # Children should be in 'todo' (parent not done yet)
            for cid in child_ids:
                _, data = await ctx.get_task(cid)
                assert data["task"]["status"] == "todo", \
                    f"Child {cid} should be 'todo', " \
                    f"got '{data['task']['status']}'"

            # Complete the parent -> triggers dependency promotion
            t0 = time.perf_counter()
            c_el, c_data = await ctx.claim_task(
                parent_id, "fan-w", 1,
            )
            lock = c_data.get("claim_lock", "")
            assert lock, "Failed to claim parent"
            _, _ = await ctx.complete_task(parent_id, lock)
            promotion_elapsed = time.perf_counter() - t0

            _tps_report(
                f"parent_complete_{n_children}_promotions",
                1 + n_children, promotion_elapsed,
            )

            # Verify children promoted to 'ready'
            _, data = await ctx.list_tasks(status="ready")
            assert data["total"] >= n_children, \
                f"Expected {n_children} ready children, " \
                f"got {data['total']}"
        finally:
            await ctx.close()

    async def test_concurrent_poll_cycle(self) -> None:
        """1000 ready tasks -- measure store claim throughput."""
        ctx = await BenchmarkContext.create()
        try:
            n = 1000
            tids = await ctx.create_n_tasks(n)
            store = ctx.task_store

            t0 = time.perf_counter()
            claimed = 0
            for tid in tids:
                result = store.claim_task(
                    tid, "poll-w", 1, ttl=900,
                )
                if result:
                    claimed += 1
            elapsed = time.perf_counter() - t0

            _tps_report(f"batch_claim_{n}_tasks", claimed, elapsed)
            assert claimed == n, f"Expected {n} claims, got {claimed}"
        finally:
            await ctx.close()


# ===================================================================
# 4. Long-Running Tests (optional -- --runslow)
# ===================================================================


class TestLongRunning:
    """5-minute soak test -- run with --runslow to enable."""

    @pytest.mark.slow
    async def test_five_minute_soak(self) -> None:
        """5-minute dispatcher soak with low task load."""
        data_dir = tempfile.mkdtemp(prefix="soak_")
        board_path = os.path.join(data_dir, "board.db")

        app = create_app(
            data_dir=data_dir,
            base_url="http://soak:8321",
            board_path=board_path,
            dispatcher_enabled=True,
            claim_ttl=5,
            failure_limit=3,
            dispatcher_interval=1,
        )
        server = TestServer(app)
        await server.start_server()
        client = TestClient(server)

        try:
            tids: List[str] = []
            for i in range(100):
                resp = await client.post("/v2/tasks", json={
                    "title": f"soak-{i}",
                    "assignee": f"soak-w-{i % 5}",
                    "max_retries": 2,
                })
                data = await resp.json()
                tids.append(data["task"]["id"])

            assert len(tids) == 100

            dispatcher = app.get("dispatcher")
            assert dispatcher is not None
            dt = asyncio.create_task(dispatcher.run())

            start = time.time()
            while time.time() - start < 300:
                await asyncio.sleep(30)
                if dt.done():
                    break
                resp = await client.get("/v2/tasks")
                data = await resp.json()
                print(f"  [soak t={time.time()-start:.0f}s] "
                      f"tasks={data['total']}")

            dt.cancel()
            try:
                await dt
            except asyncio.CancelledError:
                pass

            assert not dt.exception(), \
                f"Dispatcher crashed: {dt.exception()}"
            print("\n  --- Soak Test Passed (5 min) ---")
        finally:
            await client.close()
            await server.close()
            import shutil
            shutil.rmtree(data_dir, ignore_errors=True)
