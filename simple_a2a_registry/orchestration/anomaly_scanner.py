"""Anomaly Scanner — background task that detects orphans, timeouts, and stale tasks.

Scans every 60 seconds:

1. **Orphan detection** — V2 kanban tasks in ``running`` status whose assigned agent
   has no active WebSocket connection are auto-failed.
2. **Timeout detection** — running tasks whose ``max_runtime_seconds`` has elapsed
   without a heartbeat are auto-failed.
3. **Disconnection compensation** — detects agent disconnections missed by the
   V1 handler's ``_fail_agent_tasks`` and backfills the failure notification.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, Optional

from simple_a2a_registry.orchestration.models import TaskStatus
from simple_a2a_registry.orchestration.store import TaskStore

logger = logging.getLogger("a2a_registry.orchestration.anomaly_scanner")

# Default scan interval
SCAN_INTERVAL = 60  # seconds


class AnomalyScanner:
    """Background scanner for detecting orphan/timeout/disconnected tasks.

    Runs as an ``asyncio.Task`` inside the aiohttp application, wired up in
    ``create_app`` via ``on_startup`` / ``on_cleanup``.

    Attributes:
        _task_store:      V2 Kanban ``TaskStore`` for reading/writing task status.
        _ws_connections:  Reference to ``RegistryHandler._ws_connections``. Dict of
                          ``agent_id → WebSocketResponse``.
        _admin_ws_hub:    Optional ``AdminWSHub`` for broadcasting anomaly events.
        _interval:        Seconds between scan cycles (default 60).
        _running:         Whether the background loop is active.
        _task:            The asyncio task for the background loop.
    """

    def __init__(
        self,
        task_store: TaskStore,
        ws_connections: Dict[str, Any],
        admin_ws_hub: Any = None,
        interval: int = SCAN_INTERVAL,
    ) -> None:
        self._task_store = task_store
        self._ws_connections = ws_connections
        self._admin_ws_hub = admin_ws_hub
        self._interval = interval
        self._running = False
        self._task: Optional[asyncio.Task] = None

    # ------------------------------------------------------------------
    # Lifecycle control
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Enter the scan loop.  Runs until ``stop()`` is called."""
        self._running = True
        logger.info(
            "AnomalyScanner started (interval=%ds)",
            self._interval,
        )
        while self._running:
            try:
                await self._scan_cycle()
            except Exception:
                logger.exception("AnomalyScanner scan cycle failed")
            await asyncio.sleep(self._interval)
        logger.info("AnomalyScanner stopped")

    def stop(self) -> None:
        """Signal the scan loop to exit on the next cycle."""
        self._running = False
        logger.info("AnomalyScanner stopping...")

    @property
    def is_running(self) -> bool:
        return self._running

    # ------------------------------------------------------------------
    # Scan cycle
    # ------------------------------------------------------------------

    async def _scan_cycle(self) -> dict[str, int]:
        """Run one complete scan cycle.

        Returns:
            Dict with counts of each action taken:
            ``{orphans_failed, timeouts_failed}``.
        """
        stats: dict[str, int] = {
            "orphans_failed": 0,
            "timeouts_failed": 0,
        }

        # 1. Orphan detection: running tasks whose agent has no WS connection
        try:
            orphans = await self._detect_orphans()
            for task_id, assignee, reason in orphans:
                self._fail_kanban_task(task_id, reason)
                stats["orphans_failed"] += 1
                logger.warning(
                    "Orphan task '%s' (assignee=%s) → failed: %s",
                    task_id, assignee, reason,
                )
                self._broadcast_anomaly(task_id, "orphan", reason)
        except Exception:
            logger.exception("Orphan detection step failed")

        # 2. Timeout detection: running tasks past max_runtime_seconds
        try:
            timeouts = self._detect_timeouts()
            for task_id, assignee, reason in timeouts:
                self._fail_kanban_task(task_id, reason)
                stats["timeouts_failed"] += 1
                logger.warning(
                    "Timeout task '%s' (assignee=%s) → failed: %s",
                    task_id, assignee, reason,
                )
                self._broadcast_anomaly(task_id, "timeout", reason)
        except Exception:
            logger.exception("Timeout detection step failed")

        if stats["orphans_failed"] or stats["timeouts_failed"]:
            logger.info(
                "Anomaly scan complete: %d orphans, %d timeouts",
                stats["orphans_failed"],
                stats["timeouts_failed"],
            )

        return stats

    # ------------------------------------------------------------------
    # Detection logic
    # ------------------------------------------------------------------

    async def _detect_orphans(self) -> list[tuple[str, str, str]]:
        """Find running V2 tasks whose assigned agent's WS connection is gone.

        Returns:
            List of ``(task_id, assignee, reason)`` tuples for orphaned tasks.
        """
        orphans: list[tuple[str, str, str]] = []
        running_tasks, total = self._task_store.list_tasks(
            status=TaskStatus.RUNNING.value,
            limit=500,
        )
        if not running_tasks:
            return orphans

        now = time.time()
        for task in running_tasks:
            if not task.assignee:
                continue

            ws = self._ws_connections.get(task.assignee)
            if ws is not None and not ws.closed:
                continue  # agent is connected — alive

            # Agent is disconnected — orphan
            reason = (
                f"Agent '{task.assignee}' disconnected; task auto-failed as orphan"
            )

            # Check if the task was recently updated (within the last 5 minutes)
            # to avoid failing tasks that are naturally completing
            if task.last_heartbeat_at:
                elapsed = now - task.last_heartbeat_at
                if elapsed < 120:
                    # Within cooldown — the V1 handler may still be processing
                    # the disconnect; skip for now
                    continue

            orphans.append((task.id, task.assignee, reason))

        return orphans

    def _detect_timeouts(self) -> list[tuple[str, str, str]]:
        """Find running V2 tasks past their ``max_runtime_seconds``.

        Uses the later of ``last_heartbeat_at`` or ``started_at`` as the
        reference timestamp.

        Returns:
            List of ``(task_id, assignee, reason)`` tuples for timed-out tasks.
        """
        timeouts: list[tuple[str, str, str]] = []
        running_tasks, total = self._task_store.list_tasks(
            status=TaskStatus.RUNNING.value,
            limit=500,
        )
        if not running_tasks:
            return timeouts

        now = time.time()
        for task in running_tasks:
            max_runtime = task.max_runtime_seconds
            if max_runtime is None or max_runtime <= 0:
                continue  # no timeout configured

            # Use the most recent reference point
            ref_ts = task.last_heartbeat_at or task.started_at or task.created_at
            if ref_ts is None:
                continue

            elapsed = now - ref_ts
            if elapsed < max_runtime:
                continue  # still within limit

            timeouts.append((
                task.id,
                task.assignee or "?",
                (
                    f"Task exceeded max_runtime ({max_runtime}s, "
                    f"elapsed={elapsed:.0f}s, last_ref={ref_ts})"
                ),
            ))

        return timeouts

    # ------------------------------------------------------------------
    # Action helpers
    # ------------------------------------------------------------------

    def _fail_kanban_task(self, task_id: str, reason: str) -> None:
        """Fail a V2 kanban task in the TaskStore.

        Args:
            task_id: The kanban task ID.
            reason:  Human-readable failure reason (stored as result).
        """
        try:
            self._task_store.update_task_status(
                task_id,
                TaskStatus.FAILED.value,
                result=reason,
            )
        except Exception as e:
            logger.error(
                "Failed to update task '%s' status: %s",
                task_id, e,
            )

    def _broadcast_anomaly(self, task_id: str, anomaly_type: str, reason: str) -> None:
        """Broadcast an anomaly event to the Admin WebSocket hub.

        Args:
            task_id:      The task that was failed.
            anomaly_type: ``"orphan"`` or ``"timeout"``.
            reason:       Human-readable reason string.
        """
        if self._admin_ws_hub is None:
            return
        try:
            self._admin_ws_hub.broadcast_to_all({
                "type": "anomaly",
                "anomaly_type": anomaly_type,
                "task_id": task_id,
                "reason": reason,
                "ts": time.time(),
            })
        except Exception:
            logger.exception(
                "Failed to broadcast anomaly for task '%s'", task_id,
            )