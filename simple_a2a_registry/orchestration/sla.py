"""SLA Statistics Engine — task success rate calculation, windowed rates, and trend analysis.

Queries the TaskStore's ``tasks`` table to compute:

- **Success rate**: ``completed / (completed + failed)`` over multiple time windows
- **Trend**: linear regression slope over the last N windows to detect degradation/improvement
- **Breakdown**: per-assignee / per-window task counts

All methods are thread-safe via the TaskStore's own locking.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from simple_a2a_registry.orchestration.store import TaskStore

logger = logging.getLogger("a2a_registry.orchestration.sla")

# ---------------------------------------------------------------------------
# Typed data
# ---------------------------------------------------------------------------


@dataclass
class WindowStat:
    """SLA stats for a single time window."""

    window_label: str         # e.g. "1h", "6h", "24h", "7d", "all"
    completed: int            # tasks that reached COMPLETED status
    failed: int               # tasks that reached FAILED status
    cancelled: int            # tasks that reached CANCELLED status
    total_terminal: int       # completed + failed + cancelled
    success_rate: float       # completed / (completed + failed), NaN if none
    window_start: float       # epoch seconds
    window_end: float         # epoch seconds


@dataclass
class SlaSnapshot:
    """Complete SLA snapshot for the registry at a point in time."""

    windows: List[WindowStat] = field(default_factory=list)
    trend_slope: Optional[float] = None   # slope: positive = improving
    trend_intercept: Optional[float] = None
    trend_r_squared: Optional[float] = None
    captured_at: float = 0.0


# ---------------------------------------------------------------------------
# SQL helpers
# ---------------------------------------------------------------------------

_SLA_COUNTS_SQL = """
SELECT
    SUM(CASE WHEN t.status = 'completed' THEN 1 ELSE 0 END)  AS completed,
    SUM(CASE WHEN t.status = 'failed'     THEN 1 ELSE 0 END)  AS failed,
    SUM(CASE WHEN t.status = 'cancelled'  THEN 1 ELSE 0 END)  AS cancelled,
    SUM(CASE WHEN t.status IN ('completed','failed','cancelled') THEN 1 ELSE 0 END) AS terminal
FROM tasks t
WHERE t.completed_at >= ? AND t.completed_at < ?
"""

_SLA_COUNTS_ALL_SQL = """
SELECT
    SUM(CASE WHEN t.status = 'completed' THEN 1 ELSE 0 END)  AS completed,
    SUM(CASE WHEN t.status = 'failed'     THEN 1 ELSE 0 END)  AS failed,
    SUM(CASE WHEN t.status = 'cancelled'  THEN 1 ELSE 0 END)  AS cancelled,
    SUM(CASE WHEN t.status IN ('completed','failed','cancelled') THEN 1 ELSE 0 END) AS terminal
FROM tasks t
"""

# ---------------------------------------------------------------------------
# SLA Calculator
# ---------------------------------------------------------------------------


class SlaCalculator:
    """Compute task success-rate SLA statistics from a TaskStore.

    Args:
        task_store: The orchestration ``TaskStore`` instance.
    """

    def __init__(self, task_store: TaskStore) -> None:
        self._task_store = task_store

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def snapshot(self, windows: Optional[List[tuple[str, int]]] = None) -> SlaSnapshot:
        """Take a full SLA snapshot across multiple time windows.

        Args:
            windows: List of ``(label, seconds)`` tuples. Defaults to
                ``[("1h", 3600), ("6h", 21600), ("24h", 86400), ("7d", 604800)]``.

        Returns:
            ``SlaSnapshot`` with per-window stats + trend.
        """
        if windows is None:
            windows = DEFAULT_WINDOWS

        now = time.time()
        captured = now

        window_stats: list[WindowStat] = []
        last_success_rates: list[tuple[float, float]] = []  # (window_end, rate)

        for label, seconds in windows:
            ws = now - seconds
            stat = self._window_stat(label, ws, now)
            window_stats.append(stat)
            if not math.isnan(stat.success_rate) and stat.completed + stat.failed > 0:
                last_success_rates.append((stat.window_end, stat.success_rate))

        # Trend over the last non-empty windows
        trend_slope: Optional[float] = None
        trend_intercept: Optional[float] = None
        trend_r2: Optional[float] = None

        if len(last_success_rates) >= 2:
            trend_slope, trend_intercept, trend_r2 = _linear_regression(
                [x for x, _ in last_success_rates],
                [y for _, y in last_success_rates],
            )

        # All-time window (for overall rate)
        all_stat = self._window_stat("all", 0, now)
        window_stats.append(all_stat)

        return SlaSnapshot(
            windows=window_stats,
            trend_slope=trend_slope,
            trend_intercept=trend_intercept,
            trend_r_squared=trend_r2,
            captured_at=captured,
        )

    def window_stat(self, label: str, window_seconds: int) -> WindowStat:
        """Convenience: compute a single window stat.

        Args:
            label: Human label for the window (e.g. ``"1h"``).
            window_seconds: How far back to look.

        Returns:
            ``WindowStat`` for the given window.
        """
        now = time.time()
        return self._window_stat(label, now - window_seconds, now)

    def overall_success_rate(self) -> float:
        """Compute the overall (all-time) success rate.

        Returns:
            ``completed / (completed + failed)``, or ``float('nan')`` if none.
        """
        counts = self._query_counts_all()
        denom = counts["completed"] + counts["failed"]
        if denom == 0:
            return float("nan")
        return counts["completed"] / denom

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _window_stat(self, label: str, start_ts: float, end_ts: float) -> WindowStat:
        """Compute stats for a single time window."""
        if start_ts <= 0:
            # All-time query
            counts = self._query_counts_all()
        else:
            counts = self._query_counts(start_ts, end_ts)

        completed = counts["completed"]
        failed = counts["failed"]
        cancelled = counts["cancelled"]

        denom = completed + failed
        rate = completed / denom if denom > 0 else float("nan")

        return WindowStat(
            window_label=label,
            completed=completed,
            failed=failed,
            cancelled=cancelled,
            total_terminal=counts["terminal"],
            success_rate=rate,
            window_start=start_ts,
            window_end=end_ts,
        )

    def _query_counts(self, start_ts: float, end_ts: float) -> dict[str, int]:
        """Query task status counts within a completed_at range."""
        with self._task_store._tx("DEFERRED") as engine:
            result = engine.execute(_SLA_COUNTS_SQL, (start_ts, end_ts))
            row = result.fetchone()
        return {
            "completed": row["completed"] or 0,
            "failed": row["failed"] or 0,
            "cancelled": row["cancelled"] or 0,
            "terminal": row["terminal"] or 0,
        }

    def _query_counts_all(self) -> dict[str, int]:
        """Query all-time task status counts."""
        with self._task_store._tx("DEFERRED") as engine:
            result = engine.execute(_SLA_COUNTS_ALL_SQL)
            row = result.fetchone()
        return {
            "completed": row["completed"] or 0,
            "failed": row["failed"] or 0,
            "cancelled": row["cancelled"] or 0,
            "terminal": row["terminal"] or 0,
        }


# ---------------------------------------------------------------------------
# Trend helpers
# ---------------------------------------------------------------------------


def _linear_regression(
    xs: list[float], ys: list[float],
) -> tuple[float, float, float]:
    """Simple linear regression via ordinary least squares.

    Args:
        xs: Independent variable (time in epoch seconds).
        ys: Dependent variable (success rate).

    Returns:
        ``(slope, intercept, r_squared)``.
    """
    n = len(xs)
    if n < 2:
        return 0.0, 0.0, 0.0

    mean_x = sum(xs) / n
    mean_y = sum(ys) / n

    num = 0.0
    den_xx = 0.0
    den_yy = 0.0

    for x, y in zip(xs, ys):
        dx = x - mean_x
        dy = y - mean_y
        num += dx * dy
        den_xx += dx * dx
        den_yy += dy * dy

    if den_xx == 0:
        return 0.0, mean_y, 0.0

    slope = num / den_xx
    intercept = mean_y - slope * mean_x

    # R² (coefficient of determination)
    r2 = (num ** 2) / (den_xx * den_yy) if den_yy > 0 else 0.0

    return slope, intercept, r2


# ---------------------------------------------------------------------------
# SLA background updater (periodically updates Prometheus gauges)
# ---------------------------------------------------------------------------


class SlaUpdater:
    """Periodic background task that updates SLA Prometheus gauges.

    Wired into ``create_app`` via ``on_startup`` / ``on_cleanup``, similar
    to ``AnomalyScanner``.

    Attributes:
        _calculator: ``SlaCalculator`` instance.
        _interval: Seconds between updates (default 60).
        _running: Whether the loop is active.
        _task: The asyncio task handle.
    """

    def __init__(
        self, calculator: SlaCalculator, interval: int = 60,
    ) -> None:
        self._calculator = calculator
        self._interval = interval
        self._running = False
        self._task: Optional[Any] = None

    async def run(self) -> None:
        """Enter the update loop until ``stop()`` is called."""
        self._running = True
        logger.info("SlaUpdater started (interval=%ds)", self._interval)

        # Fire once immediately
        try:
            self._update_gauges()
        except Exception:
            logger.exception("SlaUpdater initial gauge update failed")

        while self._running:
            await asyncio_sleep(self._interval)
            try:
                self._update_gauges()
            except Exception:
                logger.exception("SlaUpdater gauge update failed")

        logger.info("SlaUpdater stopped")

    def stop(self) -> None:
        """Signal the loop to exit."""
        self._running = False
        logger.info("SlaUpdater stopping...")

    def _update_gauges(self) -> None:
        """Take a snapshot and push values to Prometheus gauges."""
        # Lazy import to avoid circular dependency at module level
        from simple_a2a_registry.metrics import (
            sla_task_success_rate,
            sla_tasks_total,
        )

        snap = self._calculator.snapshot()

        for w in snap.windows:
            sla_task_success_rate.labels(window=w.window_label).set(
                w.success_rate if not math.isnan(w.success_rate) else -1.0,
            )
            sla_tasks_total.labels(status="completed", window=w.window_label).set(w.completed)
            sla_tasks_total.labels(status="failed", window=w.window_label).set(w.failed)
            sla_tasks_total.labels(status="cancelled", window=w.window_label).set(w.cancelled)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_WINDOWS: list[tuple[str, int]] = [
    ("1h", 3600),
    ("6h", 21600),
    ("24h", 86400),
    ("7d", 604800),
]

# ---------------------------------------------------------------------------
# asyncio.sleep — top-level function so it can be patched in tests
# ---------------------------------------------------------------------------

async def asyncio_sleep(seconds: float) -> None:
    """Thin wrapper around ``asyncio.sleep`` so tests can monkey-patch it."""
    import asyncio
    await asyncio.sleep(seconds)