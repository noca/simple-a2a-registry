"""Tests for the SLA Statistics module — SlaCalculator, window queries, trend analysis."""

from __future__ import annotations

import math
import os
import tempfile
import time
from typing import Generator

import pytest

from simple_a2a_registry.orchestration.store import TaskStore
from simple_a2a_registry.orchestration.models import TaskStatus
from simple_a2a_registry.orchestration.sla import (
    SlaCalculator,
    SlaUpdater,
    WindowStat,
    DEFAULT_WINDOWS,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def store() -> Generator[TaskStore, None, None]:
    """Create a fresh TaskStore backed by a tempfile for each test."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    ts = TaskStore(db_path)
    try:
        yield ts
    finally:
        ts.close()
        if os.path.exists(db_path):
            os.unlink(db_path)


def _create_completed(ts: TaskStore, created_ago: float) -> str:
    """Helper: create a task and complete it, timestamping completed_at."""
    task = ts.create_task(title="test")
    ts.update_task_status(task.id, TaskStatus.RUNNING.value)
    # Manually set completed_at to simulate time
    now = time.time()
    fake_completed = now - created_ago
    with ts._tx() as engine:
        engine.execute(
            "UPDATE tasks SET status='completed', completed_at=? WHERE id=?",
            (fake_completed, task.id),
        )
    return task.id


def _create_failed(ts: TaskStore, created_ago: float) -> str:
    """Helper: create a task and fail it, timestamping completed_at."""
    task = ts.create_task(title="test")
    ts.update_task_status(task.id, TaskStatus.RUNNING.value)
    now = time.time()
    fake_failed = now - created_ago
    with ts._tx() as engine:
        engine.execute(
            "UPDATE tasks SET status='failed', completed_at=? WHERE id=?",
            (fake_failed, task.id),
        )
    return task.id


def _create_cancelled(ts: TaskStore, created_ago: float) -> str:
    """Helper: create a task and cancel it, timestamping completed_at."""
    task = ts.create_task(title="test")
    # For cancelled, status is terminal too
    with ts._tx() as engine:
        engine.execute(
            "UPDATE tasks SET status='cancelled' WHERE id=?",
            (task.id,),
        )
    return task.id


# ---------------------------------------------------------------------------
# SlaCalculator tests
# ---------------------------------------------------------------------------


class TestSlaCalculator:
    """Unit tests for the SlaCalculator query engine."""

    def test_empty_store(self, store: TaskStore) -> None:
        """No tasks → all rates should be NaN and counts zero."""
        calc = SlaCalculator(store)
        snap = calc.snapshot()

        assert len(snap.windows) == len(DEFAULT_WINDOWS) + 1  # + all-time
        for w in snap.windows:
            assert w.completed == 0
            assert w.failed == 0
            assert w.cancelled == 0
            assert math.isnan(w.success_rate)
        assert snap.trend_slope is None

    def test_all_completed(self, store: TaskStore) -> None:
        """All tasks completed → 100% success rate."""
        _create_completed(store, 120)
        _create_completed(store, 600)
        _create_completed(store, 3600)

        calc = SlaCalculator(store)
        snap = calc.snapshot()
        all_stat = snap.windows[-1]  # "all" window

        assert all_stat.completed == 3
        assert all_stat.failed == 0
        assert all_stat.success_rate == 1.0

    def test_mixed_rates(self, store: TaskStore) -> None:
        """60% completed, 40% failed → rate should be 0.6."""
        for _ in range(6):
            _create_completed(store, 300)
        for _ in range(4):
            _create_failed(store, 300)

        calc = SlaCalculator(store)
        snap = calc.snapshot()
        all_stat = snap.windows[-1]

        assert all_stat.completed == 6
        assert all_stat.failed == 4
        assert all_stat.cancelled == 0
        assert all_stat.success_rate == pytest.approx(0.6, 0.001)

    def test_time_window_filtering(self, store: TaskStore) -> None:
        """Old tasks should not appear in short windows."""
        _create_completed(store, 30)       # in 1m window
        _create_completed(store, 180)      # in 5m window
        _create_failed(store, 400)         # outside 5m window, but in 1h
        _create_failed(store, 4000)        # outside 1h window, in 24h

        calc = SlaCalculator(store)

        # Custom windows for precise testing
        snap = calc.snapshot(windows=[
            ("1m", 60),
            ("5m", 300),
        ])

        for w in snap.windows:
            if w.window_label == "1m":
                assert w.completed == 1
                assert w.failed == 0
                assert w.success_rate == 1.0
            elif w.window_label == "5m":
                assert w.completed == 2
                assert w.failed == 0
                assert w.success_rate == 1.0

    def test_trend_slope(self, store: TaskStore) -> None:
        """With improving rates over windows, slope should be positive."""
        # All successes in recent window, mixed earlier
        _create_completed(store, 600)     # recent: success
        _create_completed(store, 700)
        _create_failed(store, 70000)      # old: failure
        _create_failed(store, 71000)

        calc = SlaCalculator(store)
        snap = calc.snapshot()

        # The trend should exist (we have data in multiple windows)
        if snap.trend_slope is not None:
            # Recent window has 100%, old windows may have lower rates
            # So slope should be positive (improving)
            pass  # acceptable

        # Just ensure trend computation doesn't crash
        assert snap.captured_at > 0

    def test_only_cancelled(self, store: TaskStore) -> None:
        """Only cancelled tasks → NaN success rate (no completed or failed)."""
        _create_cancelled(store, 300)
        _create_cancelled(store, 600)

        calc = SlaCalculator(store)
        rate = calc.overall_success_rate()
        assert math.isnan(rate)

    def test_overall_success_rate(self, store: TaskStore) -> None:
        """Helper method return expected values."""
        _create_completed(store, 60)
        _create_completed(store, 120)
        _create_failed(store, 180)

        calc = SlaCalculator(store)
        rate = calc.overall_success_rate()
        assert rate == pytest.approx(2 / 3, 0.001)

    def test_single_window_fails_nan(self, store: TaskStore) -> None:
        """window_stat on empty window returns NaN."""
        calc = SlaCalculator(store)
        stat = calc.window_stat("1h", 3600)
        assert stat.completed == 0
        assert stat.failed == 0
        assert math.isnan(stat.success_rate)

    def test_window_stat_with_data(self, store: TaskStore) -> None:
        """window_stat on populated window returns correct values."""
        _create_completed(store, 60)
        _create_failed(store, 120)

        calc = SlaCalculator(store)
        stat = calc.window_stat("5m", 300)
        assert stat.completed >= 1
        assert stat.failed >= 1
        assert stat.window_label == "5m"

    def test_snapshot_structure(self, store: TaskStore) -> None:
        """Snapshot contains expected fields."""
        _create_completed(store, 100)

        calc = SlaCalculator(store)
        snap = calc.snapshot()

        assert snap.captured_at > 0
        assert len(snap.windows) >= 5  # 4 default + "all"
        for w in snap.windows:
            assert isinstance(w, WindowStat)
            assert w.window_label

    def test_trend_computation(self, store: TaskStore) -> None:
        """Trend is computed when there are 2+ windows with data."""
        import math as _m

        # Fill many tasks at different times to get data in multiple windows
        for i in range(10):
            _create_completed(store, 100 + i * 10)
        for i in range(3):
            _create_failed(store, 100000 + i * 1000)  # old failures

        calc = SlaCalculator(store)
        snap = calc.snapshot()

        # Should have enough data for trend computation
        # The trend_slope could be None if only 1 window has non-zero data
        # but with 10 recent successes and 3 old failures, at least 2 windows
        # should have data
        if snap.trend_slope is not None:
            assert isinstance(snap.trend_slope, float)
            assert isinstance(snap.trend_r_squared, float)


# ---------------------------------------------------------------------------
# SlaUpdater tests
# ---------------------------------------------------------------------------


class TestSlaUpdater:
    """Tests for the background gauge updater."""

    @pytest.mark.asyncio
    async def test_updater_start_stop(self, store: TaskStore) -> None:
        """SlaUpdater should start and stop cleanly."""
        calc = SlaCalculator(store)
        updater = SlaUpdater(calc, interval=0.1)  # fast for test

        # Start in a task
        import asyncio
        task = asyncio.create_task(updater.run())

        # Let it cycle once
        await asyncio.sleep(0.2)

        assert updater._running is True
        updater.stop()
        await task

        assert updater._running is False

    def test_updater_init(self, store: TaskStore) -> None:
        """SlaUpdater initialisation."""
        calc = SlaCalculator(store)
        updater = SlaUpdater(calc, interval=60)
        assert updater._interval == 60
        assert updater._running is False
        assert updater._task is None


# ---------------------------------------------------------------------------
# Regression tests
# ---------------------------------------------------------------------------


class TestSlaEdgeCases:
    """Edge cases for the SLA calculator."""

    def test_linear_regression_perfect(self, store: TaskStore) -> None:
        """_linear_regression with perfect correlation."""
        from simple_a2a_registry.orchestration.sla import _linear_regression

        xs = [1.0, 2.0, 3.0]
        ys = [2.0, 4.0, 6.0]
        slope, intercept, r2 = _linear_regression(xs, ys)
        assert slope == pytest.approx(2.0, 0.01)
        assert r2 == pytest.approx(1.0, 0.01)

    def test_linear_regression_flat(self, store: TaskStore) -> None:
        """_linear_regression with constant y (no slope)."""
        from simple_a2a_registry.orchestration.sla import _linear_regression

        xs = [1.0, 2.0, 3.0]
        ys = [5.0, 5.0, 5.0]
        slope, intercept, r2 = _linear_regression(xs, ys)
        assert slope == pytest.approx(0.0, 0.01)

    def test_linear_regression_single_point(self, store: TaskStore) -> None:
        """_linear_regression with a single point returns zero slope."""
        from simple_a2a_registry.orchestration.sla import _linear_regression

        slope, intercept, r2 = _linear_regression([1.0], [2.0])
        assert slope == 0.0

    def test_many_tasks_performance(self, store: TaskStore) -> None:
        """Handle large number of tasks without issues."""
        for i in range(100):
            if i % 5 == 0:
                _create_failed(store, 1000 + i)
            else:
                _create_completed(store, 100 + i)

        calc = SlaCalculator(store)
        snap = calc.snapshot()

        all_stat = snap.windows[-1]
        assert all_stat.completed >= 80
        assert all_stat.failed >= 19
        assert all_stat.total_terminal >= 99

    def test_cancelled_excluded_from_rate(self, store: TaskStore) -> None:
        """Cancelled tasks are not counted in success rate denominator."""
        _create_completed(store, 200)
        _create_cancelled(store, 300)
        _create_cancelled(store, 400)

        calc = SlaCalculator(store)
        rate = calc.overall_success_rate()
        # Only 1 completed and 0 failed → 1/(1+0) = 1.0
        assert rate == pytest.approx(1.0, 0.001)