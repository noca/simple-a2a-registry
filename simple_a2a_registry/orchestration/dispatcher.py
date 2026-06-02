"""Worker Dispatcher — background asyncio loop that manages the task lifecycle.

Responsibilities (architecture-v2.md §7):

1. **TTL Release** — find tasks with expired claim locks, mark them ``failed``
2. **Retry Promotion** — promote failed tasks below retry limit back to ``ready``
3. **Claim + Spawn** — atomically claim ``ready`` tasks, allocate workspaces,
   and spawn worker processes
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

import aiohttp

from simple_a2a_registry.orchestration.models import (
    Task,
    TaskStatus,
    TaskRunStatus,
)
from simple_a2a_registry.orchestration.store import TaskStore
from simple_a2a_registry.orchestration.workspace import (
    WorkspaceManager,
    WorkspaceAllocationError,
)

logger = logging.getLogger("a2a_registry.orchestration.dispatcher")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass
class DispatcherConfig:
    """Configuration for the Dispatcher poll loop.

    Attributes:
        poll_interval: Seconds between poll cycles (default 5).
        claim_ttl: Seconds a claim lock is valid without heartbeat (default 900).
        failure_limit: Global default retry limit (default 3).
        worker_command: Shell command template for spawning workers.
            ``{task_id}``, ``{assignee}``, ``{workspace_path}`` are substituted.
            If ``None``, the dispatcher skips claiming entirely and acts only
            as a pipeline promoter (todo → ready), relying on external polling
            workers to claim and execute tasks.
        board_slug: Board slug to inject as ``KANBAN_BOARD`` env var.
        dispatcher_id: Unique identifier for this dispatcher instance.
        tenant: If set, the dispatcher only processes tasks from this tenant.
    """

    poll_interval: int = 5
    claim_ttl: int = 900
    failure_limit: int = 3
    worker_command: Optional[str] = None
    board_slug: str = "default"
    dispatcher_id: str = "dispatcher-1"
    tenant: Optional[str] = None


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


class Dispatcher:
    """Background loop that polls for ready tasks and manages the lifecycle.

    Designed to run as an ``asyncio.Task`` inside the aiohttp application,
    started in ``on_startup`` and cancelled in ``on_cleanup``.

    Usage::

        store = TaskStore("board.db")
        ws_mgr = WorkspaceManager("/tmp/workspaces")
        dispatcher = Dispatcher(store, ws_mgr)

        # Inside aiohttp app:
        app.on_startup.append(lambda app: asyncio.create_task(dispatcher.run()))
        app.on_cleanup.append(lambda app: dispatcher.stop())
    """

    def __init__(
        self,
        store: TaskStore,
        workspace_manager: WorkspaceManager,
        config: Optional[DispatcherConfig] = None,
        ws_connections: Optional[Dict[str, Any]] = None,
        registry_store: Optional[Any] = None,
        http_session: Optional[aiohttp.ClientSession] = None,
    ) -> None:
        self.store = store
        self.ws_mgr = workspace_manager
        self.config = config or DispatcherConfig()
        self.ws_connections = ws_connections
        self.registry_store = registry_store
        self.http_session = http_session
        self._running = False
        self._task: Optional[asyncio.Task] = None
        # Track kanban task_ids dispatched via WebSocket so we can reconcile
        # results from agents (the V2 TaskStore has its own lifecycle, but
        # the V1 WS handler needs to know which tasks to update).
        self._dispatched_ws_tasks: Dict[str, str] = {}  # task_id -> assignee

    # ------------------------------------------------------------------
    # Lifecycle control
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Enter the poll loop.  Runs until ``stop()`` is called."""
        self._running = True
        logger.info(
            "Dispatcher started (interval=%ds, ttl=%ds, limit=%d)",
            self.config.poll_interval,
            self.config.claim_ttl,
            self.config.failure_limit,
        )
        while self._running:
            try:
                await self._poll_cycle()
            except Exception:
                logger.exception("Dispatcher poll cycle failed")
            await asyncio.sleep(self.config.poll_interval)
        logger.info("Dispatcher stopped")

    def stop(self) -> None:
        """Signal the poll loop to exit on the next cycle."""
        self._running = False
        logger.info("Dispatcher stopping...")

    def is_running(self) -> bool:
        return self._running

    # ------------------------------------------------------------------
    # Poll cycle (one iteration)
    # ------------------------------------------------------------------

    async def _poll_cycle(self) -> dict[str, int]:
        """Run one complete dispatcher cycle.

        Returns:
            Dict with counts of each action taken:
            ``{ttl_released, retry_promoted, tasks_claimed}``.
        """
        stats: dict[str, int] = {
            "ttl_released": 0,
            "retry_promoted": 0,
            "tasks_claimed": 0,
        }

        # 1. TTL Release — expired claims → failed
        try:
            released = self.store.release_expired_claims(tenant=self.config.tenant)
            if released:
                logger.info("TTL release: %d task(s) marked failed", released)
            stats["ttl_released"] = released
        except Exception:
            logger.exception("TTL release step failed")

        # 2. Retry Promotion — failed below limit → ready
        try:
            promoted = self.store.promote_retryable_tasks(tenant=self.config.tenant)
            if promoted:
                logger.info("Retry promotion: %d task(s) promoted to ready", promoted)
            stats["retry_promoted"] = promoted
        except Exception:
            logger.exception("Retry promotion step failed")

        # 3. Claim + Spawn — ready tasks → running
        try:
            claimed = await self._claim_and_spawn()
            stats["tasks_claimed"] = claimed
        except Exception:
            logger.exception("Claim+spawn step failed")

        return stats

    # ------------------------------------------------------------------
    # Claim + Spawn
    # ------------------------------------------------------------------

    async def _claim_and_spawn(self) -> int:
        """Find ready tasks with an assignee, claim them, allocate workspace,
        and spawn worker processes.

        Returns:
            Number of successfully claimed-and-spawned tasks.
        """
        ready_tasks, _ = self.store.list_tasks(
            status=TaskStatus.READY.value,
            limit=200,
            sort="-priority",
            tenant=self.config.tenant,
        )

        if not ready_tasks:
            return 0

        claimed_count = 0
        for task in ready_tasks:
            if not task.assignee:
                # Skip tasks without an assignee — they can't be dispatched
                continue

            # Priority 1: WebSocket dispatch to connected agent
            assignee = task.assignee
            ws = self.ws_connections.get(assignee) if self.ws_connections else None

            if ws is not None and not ws.closed:
                # Agent is connected via WebSocket → dispatch the task
                claim_result = self.store.claim_task(
                    task_id=task.id,
                    worker_id=f"ws-{assignee}",
                    pid=os.getpid(),
                    ttl=self.config.claim_ttl,
                )
                if claim_result is None:
                    continue  # another worker claimed it first

                # Allocate workspace
                try:
                    ws_path = self.ws_mgr.allocate_for_claim(task)
                    self.store._update_workspace_path(task.id, ws_path)
                except Exception as e:
                    logger.error("WS workspace alloc failed for '%s': %s — releasing", task.id, e)
                    self.store.update_task_status(
                        task.id, TaskStatus.FAILED.value, result=str(e),
                    )
                    continue

                # Send A2A-style task message over WebSocket
                task_msg = {
                    "type": "task",
                    "id": task.id,
                    "title": task.title,
                    "body": task.body or "",
                    "assignee": assignee,
                    "priority": task.priority,
                    "workspace_path": ws_path,
                    "kanban": True,  # flag so agent knows this is a kanban-backed task
                }
                try:
                    await ws.send_json(task_msg)
                    self._dispatched_ws_tasks[task.id] = assignee
                    claimed_count += 1
                    logger.info(
                        "Dispatched task '%s' via WS to agent '%s' (ws=%s)",
                        task.id, assignee, ws_path,
                    )
                except Exception as e:
                    logger.error("WS send failed for task '%s': %s — releasing claim", task.id, e)
                    self.store.update_task_status(
                        task.id, TaskStatus.FAILED.value, result=str(e),
                    )
                continue

            # Priority 2: Callback dispatch for callback-mode agents
            # Check if the assignee is a callback-mode agent via registry store
            is_callback_agent = False
            callback_url = ""
            callback_token = ""
            if self.registry_store is not None:
                try:
                    agent_card = self.registry_store.get_agent(assignee)
                    if agent_card:
                        pref_channel = agent_card.get("preferred_channel", "ws") or "ws"
                        if pref_channel == "callback":
                            callback_url = (agent_card.get("callback_url", "") or "").strip()
                            callback_token = agent_card.get("callback_token", "") or ""
                            is_callback_agent = bool(callback_url)
                except Exception:
                    logger.exception("Failed to lookup agent '%s' in registry store", assignee)

            if is_callback_agent:
                # Callback-mode agent → dispatch via HTTP POST
                claim_result = self.store.claim_task(
                    task_id=task.id,
                    worker_id=f"callback-{assignee}",
                    pid=os.getpid(),
                    ttl=self.config.claim_ttl,
                )
                if claim_result is None:
                    continue  # another worker claimed it first

                # Allocate workspace
                try:
                    ws_path = self.ws_mgr.allocate_for_claim(task)
                    self.store._update_workspace_path(task.id, ws_path)
                except Exception as e:
                    logger.error("Callback workspace alloc failed for '%s': %s — releasing", task.id, e)
                    self.store.update_task_status(
                        task.id, TaskStatus.FAILED.value, result=str(e),
                    )
                    continue

                # Build callback payload
                payload = {
                    "type": "task",
                    "id": task.id,
                    "title": task.title,
                    "body": task.body or "",
                    "assignee": assignee,
                    "priority": task.priority,
                    "workspace_path": ws_path,
                    "kanban": True,
                }

                headers: dict[str, str] = {
                    "Content-Type": "application/json",
                }
                if callback_token:
                    headers["Authorization"] = f"Bearer {callback_token}"

                try:
                    session = self.http_session
                    if session is None or session.closed:
                        session = aiohttp.ClientSession()

                    async with session.post(
                        callback_url, json=payload, headers=headers,
                        timeout=aiohttp.ClientTimeout(total=30),
                    ) as resp:
                        if resp.status >= 400:
                            text = await resp.text()
                            logger.error(
                                "Callback dispatch to '%s' failed: HTTP %d: %s",
                                callback_url, resp.status, text,
                            )
                            self.store.update_task_status(
                                task.id, TaskStatus.FAILED.value,
                                result=f"Callback returned HTTP {resp.status}",
                            )
                            continue

                    self._dispatched_ws_tasks[task.id] = assignee
                    claimed_count += 1
                    logger.info(
                        "Dispatched task '%s' via callback to agent '%s' (%s)",
                        task.id, assignee, callback_url,
                    )
                except asyncio.TimeoutError:
                    logger.error("Callback dispatch to agent '%s' timed out", assignee)
                    self.store.update_task_status(
                        task.id, TaskStatus.FAILED.value,
                        result="Callback timed out after 30s",
                    )
                except Exception as e:
                    logger.error("Callback dispatch to agent '%s' failed: %s", assignee, e)
                    self.store.update_task_status(
                        task.id, TaskStatus.FAILED.value,
                        result=f"Callback dispatch failed: {e}",
                    )
                continue

            # Priority 3: Assignee is NOT connected and NOT a callback agent → block
            if self.ws_connections:
                logger.info(
                    "Blocking task '%s' (assignee=%s) — agent not connected via WebSocket and no callback configured",
                    task.id, assignee,
                )
                try:
                    self.store.update_task_status(
                        task.id, TaskStatus.BLOCKED.value,
                    )
                    self.store.add_comment(
                        task.id, "dispatcher",
                        f"assignee '{assignee}' is not connected via WebSocket and no callback_url configured",
                    )
                except Exception:
                    logger.exception("Failed to block task '%s'" , task.id)
                continue

            # Priority 3 (legacy): worker_command mode
            # Only claim tasks when a worker_command is configured.
            # Without worker_command, the dispatcher can't spawn a real
            # worker process, so claiming would strand the task in
            # "running" forever.  In this mode the dispatcher acts purely
            # as a pipeline promoter (todo → ready) and relies on
            # external polling workers to claim and execute tasks.
            if not self.config.worker_command:
                logger.info(
                    "Skipping claim for task '%s' (assignee=%s) — "
                    "no worker_command configured; leaving in 'ready' "
                    "for polling workers.",
                    task.id, task.assignee,
                )
                continue

            # Sanity check — only claim if it's still ready
            fresh = self.store.get_task(task.id)
            if fresh is None or fresh.status != TaskStatus.READY.value:
                continue

            # Atomic claim
            claim_result = self.store.claim_task(
                task_id=task.id,
                worker_id=self.config.dispatcher_id,
                pid=os.getpid(),
                ttl=self.config.claim_ttl,
            )
            if claim_result is None:
                # Another worker claimed it first
                continue

            # Allocate workspace
            task.workspace_path = claim_result.get("workspace_path")
            try:
                ws_path = self.ws_mgr.allocate_for_claim(task)

                # Update the workspace_path in the DB
                self.store._update_workspace_path(task.id, ws_path)

                # Store the updated path in the claim result
                claim_result["workspace_path"] = ws_path

                # Spawn worker
                await self._spawn_worker(task, ws_path)

                claimed_count += 1
                logger.info(
                    "Claimed + spawned task '%s' (assignee=%s, ws=%s)",
                    task.id, task.assignee, ws_path,
                )

            except WorkspaceAllocationError as e:
                logger.error(
                    "Workspace allocation failed for task '%s': %s — releasing claim",
                    task.id, e,
                )
                # Release the claim by marking as failed
                try:
                    self.store.update_task_status(
                        task.id, TaskStatus.FAILED.value,
                        result=str(e),
                    )
                except Exception:
                    logger.exception("Failed to release claim for task '%s'", task.id)

            except Exception as e:
                logger.exception(
                    "Failed to spawn worker for task '%s': %s",
                    task.id, e,
                )
                # Release the claim
                try:
                    self.store.update_task_status(
                        task.id, TaskStatus.FAILED.value,
                        result=str(e),
                    )
                except Exception:
                    logger.exception("Failed to release claim for task '%s'", task.id)

        return claimed_count

    # ------------------------------------------------------------------
    # Worker spawn
    # ------------------------------------------------------------------

    async def _spawn_worker(self, task: Task, workspace_path: str) -> None:
        """Spawn a worker subprocess for *task*.

        If ``worker_command`` is configured, uses it as a shell template with
        ``{task_id}``, ``{assignee}``, ``{workspace_path}`` substitution.
        Otherwise logs a warning.

        The environment is populated with:
        - ``KANBAN_TASK`` = task.id
        - ``KANBAN_BOARD`` = board_slug
        - ``WORKSPACE_PATH`` = workspace_path
        """
        if not self.config.worker_command:
            logger.warning(
                "No worker_command configured — skipping worker spawn "
                "for task '%s' (assignee=%s). Set worker_command in "
                "DispatcherConfig to enable automatic dispatch.",
                task.id, task.assignee,
            )
            return

        # Build env
        env = os.environ.copy()
        env["KANBAN_TASK"] = task.id
        env["KANBAN_BOARD"] = self.config.board_slug
        env["WORKSPACE_PATH"] = workspace_path
        if task.assignee:
            env["KANBAN_ASSIGNEE"] = task.assignee

        # Build command
        cmd = self.config.worker_command.format(
            task_id=task.id,
            assignee=task.assignee or "default",
            workspace_path=workspace_path,
        )

        try:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=workspace_path,
            )

            # Log the pid for auditing
            task.worker_pid = proc.pid
            self.store._set_worker_pid(task.id, proc.pid)

            logger.info(
                "Spawned worker for task '%s' (pid=%d, cmd='%s')",
                task.id, proc.pid, cmd[:120],
            )

            # Don't await — let it run in the background
            # We create a fire-and-forget task to collect exit status
            asyncio.create_task(
                self._watch_worker(task.id, proc)
            )

        except Exception as e:
            logger.error(
                "Failed to spawn worker for task '%s': %s",
                task.id, e,
            )
            raise

    @staticmethod
    async def _watch_worker(task_id: str, proc: asyncio.subprocess.Process) -> None:
        """Watch a worker subprocess and handle its exit.

        This runs as a fire-and-forget background task.  When the process
        exits, we log the outcome.  The task lifecycle (heartbeat, complete,
        block) is managed by the worker itself via the REST API, so we don't
        force any transitions here — just observe.
        """
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=86400  # 24h max
            )
            rc = proc.returncode
            if rc == 0:
                logger.info(
                    "Worker for task '%s' exited cleanly (rc=0)", task_id
                )
            else:
                stderr_text = (stderr or b"").decode(
                    "utf-8", errors="replace"
                )[:500]
                logger.warning(
                    "Worker for task '%s' exited with rc=%d: %s",
                    task_id, rc, stderr_text,
                )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            logger.warning(
                "Worker for task '%s' timed out (24h) — killed", task_id
            )
        except Exception as e:
            logger.exception(
                "Error watching worker for task '%s': %s", task_id, e
            )

    # ------------------------------------------------------------------
    # Manual cycle trigger (for testing)
    # ------------------------------------------------------------------

    async def trigger_poll_cycle(self) -> dict[str, int]:
        """Run a single poll cycle immediately.  Useful in tests."""
        return await self._poll_cycle()