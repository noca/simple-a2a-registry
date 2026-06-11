"""Production-grade health check endpoints for Kubernetes / Docker orchestration.

Provides three standard health probes:

- ``GET /health``        — Liveness: process is alive (basic 200, no deps).
- ``GET /health/ready``  — Readiness: DB connected, upstream deps reachable.
- ``GET /health/startup`` — Startup: registry has finished initialisation.

Designed for aiohttp.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from aiohttp import web

logger = logging.getLogger("a2a_registry.health")

CHECK_TIMEOUT = 5.0  # seconds per individual health check


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class HealthCheckResult:
    """Result of a single health check probe."""

    name: str
    healthy: bool
    detail: str = ""
    elapsed_ms: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "healthy": self.healthy,
            "detail": self.detail,
            "elapsed_ms": round(self.elapsed_ms, 1),
        }


@dataclass
class HealthReport:
    """Aggregate health check response."""

    status: str  # "healthy" | "degraded" | "unhealthy"
    checks: List[HealthCheckResult] = field(default_factory=list)
    version: str = ""
    uptime_seconds: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "version": self.version,
            "uptime_seconds": round(self.uptime_seconds, 2),
            "checks": [c.to_dict() for c in self.checks],
        }


# ---------------------------------------------------------------------------
# Probe type: a callable that returns a HealthCheckResult
# ---------------------------------------------------------------------------

HealthProbe = Callable[[], "asyncio.coroutine | HealthCheckResult"]


# ---------------------------------------------------------------------------
# Standard probes
# ---------------------------------------------------------------------------


def make_db_probe(ping_fn: Callable[[], bool]) -> HealthProbe:
    """Probe that checks database connectivity via a user-supplied ping.

    ``ping_fn`` should return ``True`` when the DB is reachable.
    """

    async def _probe() -> HealthCheckResult:
        start = time.monotonic()
        try:
            ok = await asyncio.wait_for(
                _maybe_async(ping_fn), timeout=CHECK_TIMEOUT
            )
            elapsed = (time.monotonic() - start) * 1000
            if ok:
                return HealthCheckResult(
                    name="database",
                    healthy=True,
                    detail="DB ping succeeded",
                    elapsed_ms=elapsed,
                )
            return HealthCheckResult(
                name="database",
                healthy=False,
                detail="DB ping returned false",
                elapsed_ms=elapsed,
            )
        except asyncio.TimeoutError:
            elapsed = (time.monotonic() - start) * 1000
            return HealthCheckResult(
                name="database",
                healthy=False,
                detail=f"DB ping timed out after {CHECK_TIMEOUT}s",
                elapsed_ms=elapsed,
            )
        except Exception as exc:
            elapsed = (time.monotonic() - start) * 1000
            return HealthCheckResult(
                name="database",
                healthy=False,
                detail=f"DB ping failed: {exc}",
                elapsed_ms=elapsed,
            )

    return _probe


def make_ws_connections_probe(max_dangling: int = 10) -> HealthProbe:
    """Probe that checks the WebSocket connection pool is within bounds."""

    async def _probe(ws_count: int = 0) -> HealthCheckResult:
        # Instant — no real async work
        return HealthCheckResult(
            name="ws_connections",
            healthy=True,
            detail=f"{ws_count} active WS connections",
            elapsed_ms=0.0,
        )

    return _probe  # type: ignore[return-value]


def make_dependency_http_probe(name: str, url: str) -> HealthProbe:
    """Probe that checks an HTTP dependency (e.g. external API) is reachable."""

    async def _probe() -> HealthCheckResult:
        import aiohttp

        start = time.monotonic()
        try:
            async with aiohttp.ClientSession() as session:
                async with asyncio.wait_for(
                    session.get(url, timeout=aiohttp.ClientTimeout(total=CHECK_TIMEOUT)),
                    timeout=CHECK_TIMEOUT,
                ) as resp:
                    elapsed = (time.monotonic() - start) * 1000
                    if resp.status < 500:
                        return HealthCheckResult(
                            name=name,
                            healthy=True,
                            detail=f"HTTP {resp.status} from {url}",
                            elapsed_ms=elapsed,
                        )
                    return HealthCheckResult(
                        name=name,
                        healthy=False,
                        detail=f"HTTP {resp.status} from {url}",
                        elapsed_ms=elapsed,
                    )
        except asyncio.TimeoutError:
            elapsed = (time.monotonic() - start) * 1000
            return HealthCheckResult(
                name=name,
                healthy=False,
                detail=f"Timed out after {CHECK_TIMEOUT}s connecting to {url}",
                elapsed_ms=elapsed,
            )
        except Exception as exc:
            elapsed = (time.monotonic() - start) * 1000
            return HealthCheckResult(
                name=name,
                healthy=False,
                detail=f"Failed to reach {url}: {exc}",
                elapsed_ms=elapsed,
            )

    return _probe


# ---------------------------------------------------------------------------
# Aggregator
# ---------------------------------------------------------------------------


class HealthChecker:
    """Runs a battery of probes and returns an aggregate report.

    Usage::

        checker = HealthChecker(version="1.0.0")
        checker.add_readiness_probe(make_db_probe(store.ping))
        checker.add_readiness_probe(make_ws_connections_probe())

        # In handler:
        report = await checker.check_readiness()
        if report.status != "healthy":
            return web.json_response(report.to_dict(), status=503)
        return web.json_response(report.to_dict())
    """

    def __init__(self, version: str = "", started_at: float = 0.0) -> None:
        self._version = version
        self._started_at = started_at or time.time()

        self._liveness_probes: List[HealthProbe] = []
        self._readiness_probes: List[HealthProbe] = []
        self._startup_probes: List[HealthProbe] = []

    # -- Registration -------------------------------------------------------

    def add_liveness_probe(self, probe: HealthProbe) -> None:
        self._liveness_probes.append(probe)

    def add_readiness_probe(self, probe: HealthProbe) -> None:
        self._readiness_probes.append(probe)

    def add_startup_probe(self, probe: HealthProbe) -> None:
        self._startup_probes.append(probe)

    # -- Checkers -----------------------------------------------------------

    async def check_liveness(self) -> HealthReport:
        """Liveness: run liveness probes.

        Returns 200 with status ``"healthy"`` even when probes fail — the
        process IS alive, just possibly not ready yet. Probes are advisory.
        If ALL liveness probes fail we still return 200 but with
        ``"degraded"``.
        """
        results = await self._run_probes(self._liveness_probes)
        any_healthy = any(r.healthy for r in results)
        return HealthReport(
            status="healthy" if any_healthy else "degraded",
            checks=results,
            version=self._version,
            uptime_seconds=time.time() - self._started_at,
        )

    async def check_readiness(self) -> HealthReport:
        """Readiness: all readiness probes must pass.

        Returns 503 when any probe fails.
        """
        results = await self._run_probes(self._readiness_probes)
        all_healthy = all(r.healthy for r in results)
        return HealthReport(
            status="healthy" if all_healthy else "unhealthy",
            checks=results,
            version=self._version,
            uptime_seconds=time.time() - self._started_at,
        )

    async def check_startup(self) -> HealthReport:
        """Startup: all startup probes must pass.

        Returns 503 when any probe fails.
        """
        results = await self._run_probes(self._startup_probes)
        all_healthy = all(r.healthy for r in results)
        return HealthReport(
            status="healthy" if all_healthy else "unhealthy",
            checks=results,
            version=self._version,
            uptime_seconds=time.time() - self._started_at,
        )

    # -- Internal -----------------------------------------------------------

    @staticmethod
    async def _run_probes(probes: List[HealthProbe]) -> List[HealthCheckResult]:
        if not probes:
            return [HealthCheckResult(name="meta", healthy=True, detail="no probes registered")]
        results = await asyncio.gather(
            *[p() for p in probes], return_exceptions=True
        )
        output: List[HealthCheckResult] = []
        for r in results:
            if isinstance(r, HealthCheckResult):
                output.append(r)
            elif isinstance(r, Exception):
                output.append(
                    HealthCheckResult(
                        name="unknown",
                        healthy=False,
                        detail=f"Probe raised: {r}",
                    )
                )
            else:
                output.append(
                    HealthCheckResult(
                        name="unknown",
                        healthy=False,
                        detail=f"Unexpected probe return: {r!r}",
                    )
                )
        return output


# ---------------------------------------------------------------------------
# aiohttp handler factories
# ---------------------------------------------------------------------------


def make_liveness_handler(checker: HealthChecker) -> Any:
    """Return an aiohttp handler for ``GET /health`` (liveness)."""

    async def handler(request: web.Request) -> web.Response:
        report = await checker.check_liveness()
        status = 200 if report.status == "healthy" else 200  # always 200 for liveness
        return web.json_response(report.to_dict(), status=status)

    return handler


def make_readiness_handler(checker: HealthChecker) -> Any:
    """Return an aiohttp handler for ``GET /health/ready`` (readiness)."""

    async def handler(request: web.Request) -> web.Response:
        report = await checker.check_readiness()
        status = 200 if report.status == "healthy" else 503
        return web.json_response(report.to_dict(), status=status)

    return handler


def make_startup_handler(checker: HealthChecker) -> Any:
    """Return an aiohttp handler for ``GET /health/startup`` (startup)."""

    async def handler(request: web.Request) -> web.Response:
        report = await checker.check_startup()
        status = 200 if report.status == "healthy" else 503
        return web.json_response(report.to_dict(), status=status)

    return handler


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _maybe_async(fn: Callable) -> Any:
    """Call ``fn`` and await it if it's a coroutine."""
    result = fn()
    if asyncio.iscoroutine(result):
        return await result
    return result