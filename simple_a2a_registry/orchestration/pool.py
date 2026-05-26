"""Subprocess Pool Manager — persistent worker pool for agent dispatch.

Manages a pool of long-lived subprocess workers, one per configured pool
assignee.  Workers receive task assignments as JSON-line messages on stdin.
Crash recovery is automatic.

This is an alternative dispatch path between WebSocket (P1) and legacy
worker_command (P3):

    P1  WebSocket dispatch        — agent connected via WS
    P1.5  Subprocess Pool dispatch — assignee in pool_assignees
    P2  Block disconnected agent   — agent known but not connected
    P3  Legacy worker_command      — one-shot subprocess per task
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, List, Optional

from simple_a2a_registry.orchestration.models import Task

logger = logging.getLogger("a2a_registry.orchestration.pool")


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class PoolManagerError(RuntimeError):
    """Raised when a pool operation fails."""


class WorkerStartError(PoolManagerError):
    """Raised when a pool worker subprocess fails to start."""


class WorkerDispatchError(PoolManagerError):
    """Raised when dispatching a task to a worker fails."""


# ---------------------------------------------------------------------------
# Pool Manager
# ---------------------------------------------------------------------------


class SubprocessPoolManager:
    """Manage a pool of persistent subprocess workers.

    Each configured pool assignee gets one long-lived subprocess.
    Tasks are dispatched to the appropriate worker as a JSON-line on stdin.
    Workers are automatically restarted on crash.

    Args:
        pool_assignees:
            List of assignee profile names that should use the pool.
        worker_command:
            Shell command template for starting a pool worker.
            ``{assignee}`` is substituted with the profile name.
            Example: ``hermes chat --profile {assignee} --pool-worker``
        store:
            The TaskStore instance for claiming / updating tasks.
        workspace_manager:
            The WorkspaceManager for allocating scratch workspaces.
    """

    def __init__(
        self,
        pool_assignees: List[str],
        worker_command: str,
        store: Any,  # TaskStore — avoid circular import at type level
        workspace_manager: Any,  # WorkspaceManager
    ) -> None:
        self._pool_assignees = list(pool_assignees)
        self._worker_command = worker_command
        self._store = store
        self._ws_mgr = workspace_manager

        # assignee -> subprocess.Process
        self._workers: Dict[str, asyncio.subprocess.Process] = {}

        # Background watch tasks for crash recovery
        self._watch_tasks: Dict[str, asyncio.Task] = {}

        # Shutdown flag — stop restart loop
        self._shutting_down = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def assignees(self) -> List[str]:
        """Return the list of configured pool assignees."""
        return list(self._pool_assignees)

    async def start(self) -> None:
        """Start pool workers for all configured assignees."""
        if not self._pool_assignees:
            logger.info("No pool assignees configured — skipping pool start")
            return

        logger.info(
            "Starting pool workers for %d assignee(s): %s",
            len(self._pool_assignees),
            self._pool_assignees,
        )
        for assignee in self._pool_assignees:
            try:
                await self._start_worker(assignee)
            except Exception:
                logger.exception("Failed to start pool worker for '%s'", assignee)

    async def dispatch(
        self,
        task: Task,
        workspace_path: str,
    ) -> bool:
        """Dispatch a claimed task to the pool worker for *task.assignee*.

        This does NOT claim the task — the caller (dispatcher._claim_and_spawn)
        must have already claimed it.  Steps performed here:

        1. Find the worker subprocess for *task.assignee*.
        2. Restart the worker if it has crashed.
        3. Send the task assignment as a JSON-line on stdin.
        4. Update the task's workspace_path in the store.

        Returns:
            ``True`` if the task was successfully dispatched,
            ``False`` if *task.assignee* is not a pool assignee.
        """
        if task.assignee not in self._pool_assignees:
            return False

        if self._shutting_down:
            logger.warning(
                "Pool is shutting down — cannot dispatch task '%s' to '%s'",
                task.id, task.assignee,
            )
            return False

        proc = self._workers.get(task.assignee)

        # Restart crashed worker
        if proc is None or proc.returncode is not None:
            logger.info(
                "Pool worker for '%s' is dead (rc=%s) — restarting",
                task.assignee, proc.returncode if proc else "never started",
            )
            try:
                await self._start_worker(task.assignee)
            except Exception as e:
                logger.exception("Failed to restart worker for '%s'", task.assignee)
                return False
            proc = self._workers.get(task.assignee)
            if proc is None:
                return False

        # Build the task assignment message
        msg_dict: Dict[str, Any] = {
            "type": "task",
            "task_id": task.id,
            "title": task.title,
            "body": task.body or "",
            "assignee": task.assignee,
            "workspace_path": workspace_path,
            "kanban": True,
        }
        msg = json.dumps(msg_dict) + "\n"

        try:
            assert proc.stdin is not None, "Worker process has no stdin"
            proc.stdin.write(msg.encode("utf-8"))
            await proc.stdin.drain()
            logger.info(
                "Dispatched task '%s' to pool worker '%s' (pid=%d)",
                task.id, task.assignee, proc.pid,
            )
            return True
        except Exception as e:
            logger.exception(
                "Failed to dispatch task '%s' to pool worker '%s'",
                task.id, task.assignee,
            )
            raise WorkerDispatchError(
                f"dispatch to {task.assignee} failed: {e}"
            ) from e

    async def shutdown(self) -> None:
        """Gracefully shut down all pool workers.

        Sends a shutdown message on stdin, then terminates workers that
        don't exit within the grace period.
        """
        self._shutting_down = True
        if not self._workers:
            logger.info("Pool already empty — nothing to shut down")
            return

        logger.info(
            "Shutting down %d pool worker(s)...", len(self._workers),
        )

        # Send shutdown message to each worker
        shutdown_msg = json.dumps({"type": "shutdown"}) + "\n"
        for assignee, proc in list(self._workers.items()):
            if proc.returncode is not None:
                continue
            try:
                assert proc.stdin is not None
                proc.stdin.write(shutdown_msg.encode("utf-8"))
                await proc.stdin.drain()
            except Exception:
                pass  # worker may already be dead

        # Cancel watch tasks
        for assignee, wt in list(self._watch_tasks.items()):
            wt.cancel()
            try:
                await wt
            except (asyncio.CancelledError, Exception):
                pass
        self._watch_tasks.clear()

        # Wait for graceful exit
        for assignee, proc in list(self._workers.items()):
            if proc.returncode is not None:
                continue
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                logger.warning(
                    "Pool worker '%s' (pid=%d) did not exit — killing",
                    assignee, proc.pid,
                )
                try:
                    proc.kill()
                    await proc.wait()
                except ProcessLookupError:
                    pass
            except ProcessLookupError:
                pass

        self._workers.clear()
        logger.info("All pool workers shut down")

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _start_worker(self, assignee: str) -> None:
        """Start a single pool worker subprocess.

        The worker command template is formatted with ``{assignee}`` replaced
        by the profile name.  The subprocess is started with ``stdin=PIPE``
        so the pool manager can send task JSON-lines.

        Raises:
            WorkerStartError: If the subprocess fails to start.
        """
        cmd = self._worker_command.format(assignee=assignee)
        logger.info("Starting pool worker for '%s': %s", assignee, cmd)

        try:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except Exception as e:
            raise WorkerStartError(
                f"Failed to start worker for {assignee}: {e}"
            ) from e

        self._workers[assignee] = proc
        logger.info(
            "Pool worker for '%s' started (pid=%d)",
            assignee, proc.pid,
        )

        # Start a background watch task for crash recovery
        watch = asyncio.create_task(
            self._watch_worker(assignee, proc),
            name=f"pool-watch-{assignee}",
        )
        self._watch_tasks[assignee] = watch

    async def _watch_worker(
        self,
        assignee: str,
        proc: asyncio.subprocess.Process,
    ) -> None:
        """Monitor a pool worker and restart on unexpected exit.

        Runs as a fire-and-forget background task.  When the worker exits
        non-zero (or is still the current worker for the assignee when
        it exits), it restarts the worker.
        """
        stderr = b""
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=86400,  # 24h max
            )
        except asyncio.TimeoutError:
            logger.warning(
                "Pool worker '%s' (pid=%d) timed out after 24h — killing",
                assignee, proc.pid,
            )
            try:
                proc.kill()
                await proc.wait()
            except ProcessLookupError:
                pass
        except asyncio.CancelledError:
            # Shutdown — exit cleanly
            return

        rc = proc.returncode
        stderr_text = (stderr or b"").decode("utf-8", errors="replace")[:200]

        if self._shutting_down:
            logger.info(
                "Pool worker '%s' exited (rc=%d) during shutdown",
                assignee, rc,
            )
            return

        if rc == 0:
            logger.info(
                "Pool worker '%s' exited cleanly (rc=0) — will not restart",
                assignee,
            )
            # Don't restart on clean exit — the worker chose to exit
            if self._workers.get(assignee) is proc:
                del self._workers[assignee]
            return

        # Non-zero exit — log and restart
        logger.warning(
            "Pool worker '%s' exited with rc=%d (stderr: %s) — restarting",
            assignee, rc, stderr_text,
        )

        # Only restart if we're still the current worker for this assignee
        # (avoid duelling restarts on rapid failures)
        if self._workers.get(assignee) is proc and not self._shutting_down:
            await asyncio.sleep(1)  # brief backoff before restart
            try:
                await self._start_worker(assignee)
            except Exception:
                logger.exception(
                    "Failed to restart pool worker '%s' — will retry next dispatch",
                    assignee,
                )