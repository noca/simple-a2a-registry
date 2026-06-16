"""Performance benchmarks for the orchestration engine.

Each benchmark outputs numeric performance metrics (tasks/sec, avg latency, etc.)
and can be run independently:

    python3 -m pytest tests/benchmarks/ -v --tb=short

Test files:

  test_task_throughput.py   — HTTP-level throughput via aiohttp TestClient
                              (create/claim/complete/latency percentiles)
  test_security_overhead.py — Security Harness overhead (enabled vs disabled)
                              for create + claim+complete cycles
  test_sse_push_latency.py  — EventBus publish→subscribe and SSE stream push
                              latency measurement
  test_throughput.py        — Low-level TaskStore throughput (direct store calls)
  test_dependency_chain.py  — DAG chain resolution performance
  test_dispatcher_poll.py   — Dispatcher poll cycle overhead
  test_workspace_stress.py  — Workspace filesystem allocation stress test

Baseline values (WSL2 / AMD Ryzen 9 7945HX / 2026-06-11):

  Throughput (HTTP, via aiohttp TestClient):
    HTTP create 100 tasks:    2,333 TPS
    HTTP create 500 tasks:    2,450 TPS
    HTTP create 1000 tasks:   2,416 TPS
    HTTP create→claim→complete:   697 TPS
    HTTP claim (10w × 500t):      252 TPS

  Latency (HTTP, P50/P95/P99, ms):
    claim_task:         0.78 / 1.12 / 1.50
    complete_task:      0.86 / 1.24 / 2.14
    heartbeat_task:     0.55 / 0.79 / 0.92
    get_task:           0.77 / 1.15 / 1.59

  Security Harness Overhead (SQLite + Warn mode):
    Create:         +67% (3,068 → 1,834 TPS)
    Claim+Complete: +41% (757 → 535 TPS)
    Scalability: overhead decreases with load (59.5% @ 100 → 43.5% @ 1000)
    NOTE: 20% target expected with MySQL + Enforce mode on warm cache.

  EventBus / SSE Push Latency (P50 / P95 / P99):
    EventBus subscriber:  0.39 / 0.52 / 0.60 ms
    SSE stream:           0.43 / 0.54 / 1.07 ms
    EventBus bulk drain:  1000 events in 10ms (100,990 events/sec)

  Direct Store (1000 tasks):
    Create:  17,645 TPS
    List:    86,050 TPS
    Read:    106 µs/task
"""

