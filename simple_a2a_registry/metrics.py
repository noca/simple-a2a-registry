"""Prometheus metrics for A2A Registry — aiohttp middleware + metric definitions.

All metrics use the ``a2a_registry_`` prefix as required.
The ``/metrics`` endpoint is registered when ``monitoring.metrics_enabled``
is ``True`` in the config.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Callable

from aiohttp import web
from prometheus_client import (
    Counter,
    Gauge,
    Histogram,
    generate_latest,
    CONTENT_TYPE_LATEST,
)

logger = logging.getLogger("a2a_registry.metrics")

# ---------------------------------------------------------------------------
# Counters
# ---------------------------------------------------------------------------

requests_total = Counter(
    "a2a_registry_requests_total",
    "Total HTTP requests",
    labelnames=["endpoint", "method", "status"],
)

auth_operations_total = Counter(
    "a2a_registry_auth_operations_total",
    "Total authentication/authorization operations",
    labelnames=["operation", "success"],
)

# ---------------------------------------------------------------------------
# Gauges
# ---------------------------------------------------------------------------

agents_alive = Gauge(
    "a2a_registry_agents_alive",
    "Number of agents currently alive (heartbeating within timeout)",
)

agents_stale = Gauge(
    "a2a_registry_agents_stale",
    "Number of agents currently stale (past heartbeat timeout)",
)

ws_connections = Gauge(
    "a2a_registry_ws_connections",
    "Number of active WebSocket connections from agents",
)

db_pool_size = Gauge(
    "a2a_registry_db_pool_size",
    "Current database connection pool size",
)

# ---------------------------------------------------------------------------
# Histograms
# ---------------------------------------------------------------------------

request_duration_seconds = Histogram(
    "a2a_registry_request_duration_seconds",
    "HTTP request latency in seconds",
    labelnames=["endpoint", "method"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)

db_query_duration_seconds = Histogram(
    "a2a_registry_db_query_duration_seconds",
    "Database query latency in seconds",
    labelnames=["operation"],
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
)

# ---------------------------------------------------------------------------
# Middleware — HTTP request metrics
# ---------------------------------------------------------------------------


def metrics_middleware_factory() -> Callable:
    """Create an aiohttp middleware that instruments every HTTP request.

    Tracks:
    - ``requests_total`` with endpoint / method / status labels
    - ``request_duration_seconds`` histogram
    """

    @web.middleware
    async def _metrics_middleware(
        request: web.Request, handler: Callable,
    ) -> web.StreamResponse:
        # Skip metrics endpoint itself to avoid recursion
        if request.path == "/metrics":
            return await handler(request)

        start = time.monotonic()
        status = 500  # safe default for the finally block
        try:
            response = await handler(request)
            status = response.status
        except web.HTTPException as exc:
            status = exc.status
            raise
        except Exception:
            status = 500
            raise
        finally:
            duration = time.monotonic() - start
            endpoint = _normalize_endpoint(request.path)
            method = request.method
            requests_total.labels(endpoint=endpoint, method=method, status=str(status)).inc()
            request_duration_seconds.labels(endpoint=endpoint, method=method).observe(duration)

        return response

    return _metrics_middleware


def _normalize_endpoint(path: str) -> str:
    """Normalise an HTTP path for use as a Prometheus label.

    Replaces dynamic path segments (UUIDs, agent IDs, task IDs) with
    a placeholder to keep label cardinality bounded.

    Examples::
        /v1/agents/abc-123 → /v1/agents/{agent_id}
        /v2/tasks/t_xxx    → /v2/tasks/{id}
    """
    # v1 agent paths — /v1/agents/{agent_id}[/anything...]
    path = re.sub(r"/v1/agents/[^/]+", "/v1/agents/{agent_id}", path)
    # v1 tasks
    path = re.sub(r"/v1/tasks/[^/]+", "/v1/tasks/{task_id}", path)
    # v2 tasks
    path = re.sub(r"/v2/tasks/[^/]+", "/v2/tasks/{id}", path)
    # admin clients
    path = re.sub(r"/admin/clients/[^/]+", "/admin/clients/{client_id}", path)
    # Strip trailing /
    return path.rstrip("/") or "/"


# ---------------------------------------------------------------------------
# Metrics endpoint handler
# ---------------------------------------------------------------------------


async def handle_metrics(request: web.Request) -> web.Response:
    """GET /metrics — return Prometheus-format metrics text."""
    return web.Response(
        body=generate_latest(),
        headers={"Content-Type": CONTENT_TYPE_LATEST},
    )


# ---------------------------------------------------------------------------
# Gauge update helpers (called by app code)
# ---------------------------------------------------------------------------


def update_agent_gauges(alive: int, stale: int) -> None:
    """Update ``agents_alive`` and ``agents_stale`` gauges."""
    agents_alive.set(alive)
    agents_stale.set(stale)


def update_ws_connections(count: int) -> None:
    """Update ``ws_connections`` gauge."""
    ws_connections.set(count)


def update_db_pool_size(size: int) -> None:
    """Update ``db_pool_size`` gauge."""
    db_pool_size.set(size)


def record_auth_operation(operation: str, success: bool) -> None:
    """Increment ``auth_operations_total`` counter."""
    auth_operations_total.labels(
        operation=operation,
        success="true" if success else "false",
    ).inc()


def record_db_query_duration(operation: str, duration: float) -> None:
    """Record a histogram observation for ``db_query_duration_seconds``."""
    db_query_duration_seconds.labels(operation=operation).observe(duration)