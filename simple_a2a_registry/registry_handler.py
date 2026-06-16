"""WS Message Router — protocol-level message dispatch for agent WebSocket connections.

Provides an extensible :class:`WSMessageRouter` that replaces the raw if-elif chain
inside :meth:`RegistryHandler.handle_ws` with a handler-map-based dispatch.  Adding
a new message type is a single ``router.register(...)`` call.

Protocol reference: ``docs/ws-task-lifecycle.md §2.1``
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, Optional

# Timeout callback signatures
TimeoutResetFn = Callable[[str], None]
TimeoutCancelFn = Callable[[str], None]

from aiohttp import web

from simple_a2a_registry.orchestration.store import TaskStatus
from simple_a2a_registry.orchestration.store import TaskStore

logger = logging.getLogger("a2a_registry.registry_handler")

# ---------------------------------------------------------------------------
# P3.3: Per-agent state_sync rate limiter — max 1 per 30 seconds
# ---------------------------------------------------------------------------
_STATE_SYNC_COOLDOWN = 30.0  # seconds
_state_sync_last: Dict[str, float] = {}  # agent_id -> last state_sync timestamp


def _check_state_sync_rate_limit(agent_id: str) -> tuple[bool, float]:
    """Check and record a state_sync rate limit for *agent_id*.

    Returns:
        ``(allowed, retry_after)`` — ``allowed`` is ``True`` if the
        request passes the rate limit.  When ``allowed`` is ``False``,
        ``retry_after`` is the number of seconds to wait before retrying.
    """
    now = time.time()
    last = _state_sync_last.get(agent_id, 0.0)
    elapsed = now - last
    if elapsed < _STATE_SYNC_COOLDOWN and last > 0:
        retry_after = _STATE_SYNC_COOLDOWN - elapsed
        return False, retry_after
    _state_sync_last[agent_id] = now
    return True, 0.0


def _reset_state_sync_rate_limiter() -> None:
    """Clear all rate-limiter state (for test isolation)."""
    _state_sync_last.clear()


# ---------------------------------------------------------------------------
# Context bag – what every handler receives
# ---------------------------------------------------------------------------


@dataclass
class WSContext:
    """Carries references the handlers need to mutate server state.

    Attributes:
        agent_id:    The registered agent name for this WS connection.
        tasks:       In-memory task dict from ``RegistryHandler._tasks``.
        connections: Agent WS connections dict from ``RegistryHandler._ws_connections``.
        task_store:  Kanban ``TaskStore`` (V2) for status reconciliation (may be ``None``).
        ws_tenant:   Tenant string extracted from JWT or query parameter.
        store:       Registry ``Store`` for agent registration lookups (may be ``None``).
    """
    agent_id: str
    tasks: Dict[str, Any] = field(default_factory=dict)
    connections: Dict[str, web.WebSocketResponse] = field(default_factory=dict)
    task_store: Optional[TaskStore] = None
    _dispatched_ws_tasks: Optional[Dict[str, str]] = None
    ws_tenant: str = ""
    # Timeout management callbacks (wired through by the RegistryHandler adapter)
    reset_task_timeout: Optional[TimeoutResetFn] = None
    cancel_task_timeout: Optional[TimeoutCancelFn] = None
    # Registry store for agent registration lookups (P3.3 security)
    store: Optional[Any] = None
    # Callback to forward task_progress to admin WS hub subscribers.
    # Signature: async fn(task_id: str, progress: float, message: str | None, status: str)
    broadcast_progress_fn: Any = None
    # P1.3: Cancel the dangling grace-period timer for this agent (reconnect healing).
    cancel_dangling_timer: Optional[TimeoutCancelFn] = None


# Handler signature
WSHandler = Callable[
    [web.WebSocketResponse, Dict[str, Any], WSContext],
    Awaitable[None],
]

# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


class WSMessageRouter:
    """Extensible WebSocket message router with a handler-map.

    Usage::

        router = WSMessageRouter()
        router.register("ping", handle_ping)
        router.register("task_ack", handle_task_ack)
        # ...

        # In the WS listen loop:
        msg_type = data.get("type", "")
        await router.dispatch(ws, data, ctx)

    Unknown message types are silently ignored (backward-compatible with
    agents that don't send the extended protocol).
    """

    def __init__(self) -> None:
        self._handlers: Dict[str, WSHandler] = {}

    # ------------------------------------------------------------------
    # Registration API
    # ------------------------------------------------------------------

    def register(self, msg_type: str, handler: WSHandler) -> WSHandler:
        """Register *handler* for *msg_type*.

        Returns *handler* so it can be used as a decorator::

            @router.register("task_ack")
            async def handle_task_ack(ws, data, ctx): ...
        """
        self._handlers[msg_type] = handler
        return handler

    def unregister(self, msg_type: str) -> None:
        """Remove the handler for *msg_type* (no-op if absent)."""
        self._handlers.pop(msg_type, None)

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    async def dispatch(
        self,
        ws: web.WebSocketResponse,
        data: Dict[str, Any],
        ctx: WSContext,
    ) -> None:
        """Route *data* to the appropriate handler.

        Args:
            ws:   The agent's WebSocket connection.
            data: Parsed JSON message dict.
            ctx:  Context bag with server references.
        """
        msg_type = data.get("type", "")
        handler = self._handlers.get(msg_type)
        if handler is None:
            # Unknown / unregistered type → silently ignore (backward compat)
            logger.debug("Ignoring unregistered WS message type '%s' from agent '%s'",
                         msg_type, ctx.agent_id)
            return
        try:
            await handler(ws, data, ctx)
        except Exception:
            logger.exception("Handler '%s' failed for agent '%s'",
                             msg_type, ctx.agent_id)


# ---------------------------------------------------------------------------
# Default handlers — implement the protocol defined in ws-task-lifecycle.md
# ---------------------------------------------------------------------------

# ── ping ────────────────────────────────────────────────────────────────


@dataclass
class _PingState:
    last_active: float = 0.0


_ping_states: Dict[str, _PingState] = {}


async def handle_ping(
    ws: web.WebSocketResponse,
    data: Dict[str, Any],
    ctx: WSContext,
) -> None:
    """Respond to agent keep-alive.

    If the ping carries ``active_task`` / ``task_status`` / ``task_progress``
    fields the in-memory task record is updated accordingly.

    The pong response includes ``pending_tasks`` — a list of tasks that are
    assigned to this agent but not yet started (state ``pending`` / ``dispatched``),
    giving the agent a push-based pull mechanism for new work.
    """
    _ping_states[ctx.agent_id] = _PingState(last_active=time.time())

    active_task_id = data.get("active_task")
    if active_task_id and active_task_id in ctx.tasks:
        task_status = data.get("task_status")
        task_progress = data.get("task_progress")
        task = ctx.tasks[active_task_id]
        if task_status:
            task["state"] = task_status
        if task_progress is not None:
            task["progress"] = task_progress
        task["updated_at"] = time.time()

    # Build pending_tasks list — non-terminal tasks assigned to this agent
    pending_tasks: list[dict] = []
    for task_id, task in list(ctx.tasks.items()):
        task_agent = task.get("agent_id", "")
        if not task_agent:
            continue
        if task_agent != ctx.agent_id:
            continue
        state = task.get("state", "")
        if state in ("completed", "failed", "canceled"):
            continue
        pending_tasks.append({
            "id": task_id,
            "title": task.get("title", ""),
            "state": state,
            "progress": task.get("progress"),
            "body": task.get("body", ""),
        })

    await ws.send_json({
        "type": "pong",
        "ts": int(time.time()),
        "pending_tasks": pending_tasks,
    })


# ── task_ack ────────────────────────────────────────────────────────────


async def handle_task_ack(
    ws: web.WebSocketResponse,
    data: Dict[str, Any],
    ctx: WSContext,
) -> None:
    """Agent acknowledges a dispatched task → mark as ``accepted``.

    Expected payload::

        {"type": "task_ack", "id": "task-uuid",
         "status": "accepted", "started_at": 1717000000}
    """
    task_id = data.get("id", "")
    if not task_id:
        logger.warning("task_ack without 'id' from agent '%s'", ctx.agent_id)
        return

    task = ctx.tasks.get(task_id)
    if task:
        task["state"] = data.get("status", "accepted")
        task["started_at"] = data.get("started_at", time.time())
        task["updated_at"] = time.time()
        logger.info("Task %s accepted by agent '%s'", task_id, ctx.agent_id)
    else:
        # Auto-create entry for externally managed tasks
        now = time.time()
        ctx.tasks[task_id] = {
            "id": task_id,
            "agent_id": ctx.agent_id,
            "state": data.get("status", "accepted"),
            "result": None,
            "error": None,
            "started_at": data.get("started_at", now),
            "created_at": now,
            "updated_at": now,
        }
        logger.info("Task %s accepted (auto-created) by agent '%s'",
                    task_id, ctx.agent_id)

    # Reconcile with kanban TaskStore
    _reconcile_task_store(
        ctx.task_store,
        ctx._dispatched_ws_tasks,
        task_id,
        TaskStatus.RUNNING.value,
    )

    # P1.2: Agent acknowledged → reset the timeout timer
    if ctx.reset_task_timeout is not None:
        ctx.reset_task_timeout(task_id)


# ── task_progress ───────────────────────────────────────────────────────


async def handle_task_progress(
    ws: web.WebSocketResponse,
    data: Dict[str, Any],
    ctx: WSContext,
) -> None:
    """Agent reports progress on a dispatched task.

    Expected payload::

        {"type": "task_progress", "id": "task-uuid",
         "status": "working", "progress": 0.5, "message": "Compiling..."}
    """
    task_id = data.get("id", "")
    if not task_id:
        return

    task = ctx.tasks.get(task_id)
    if task:
        task["state"] = data.get("status", "working")
        task["updated_at"] = time.time()
        if data.get("progress") is not None:
            task["progress"] = data["progress"]
        if data.get("message"):
            task["message"] = data["message"]
    else:
        now = time.time()
        ctx.tasks[task_id] = {
            "id": task_id,
            "agent_id": ctx.agent_id,
            "state": data.get("status", "working"),
            "progress": data.get("progress"),
            "message": data.get("message"),
            "result": None,
            "error": None,
            "created_at": now,
            "updated_at": now,
        }
        logger.info("Task %s progress received (auto-created) from agent '%s'",
                    task_id, ctx.agent_id)

    # P1.2: Progress received → reset the timeout timer (continue waiting)
    if ctx.reset_task_timeout is not None:
        ctx.reset_task_timeout(task_id)

    # P2.2: Forward progress to Admin WS hub subscribers
    if ctx.broadcast_progress_fn is not None:
        try:
            await ctx.broadcast_progress_fn(
                task_id,
                progress=float(data.get("progress", 0)),
                message=data.get("message"),
                status=data.get("status", "working"),
            )
        except Exception:
            logger.debug("broadcast_progress_fn failed for task '%s'", task_id, exc_info=True)


# ── task_complete ───────────────────────────────────────────────────────


async def handle_task_complete(
    ws: web.WebSocketResponse,
    data: Dict[str, Any],
    ctx: WSContext,
) -> None:
    """Agent reports a task finished successfully.

    Expected payload::

        {"type": "task_complete", "id": "task-uuid",
         "status": "completed",
         "result": {...},
         "metrics": {"duration": 12.5, "output_size": 1024}}
    """
    task_id = data.get("id", "")
    if not task_id:
        logger.warning("task_complete without 'id' from agent '%s'", ctx.agent_id)
        return

    task = ctx.tasks.get(task_id)
    if task:
        task["state"] = "completed"
        task["result"] = data.get("result", {})
        task["error"] = None
        task["metrics"] = data.get("metrics")
        task["updated_at"] = time.time()
        logger.info("Task %s completed by agent '%s'", task_id, ctx.agent_id)
    else:
        now = time.time()
        ctx.tasks[task_id] = {
            "id": task_id,
            "agent_id": ctx.agent_id,
            "state": "completed",
            "result": data.get("result", {}),
            "error": None,
            "metrics": data.get("metrics"),
            "created_at": now,
            "updated_at": now,
        }
        logger.info("Task %s completed (auto-created) by agent '%s'",
                    task_id, ctx.agent_id)

    # Reconcile with kanban TaskStore
    _reconcile_task_store(
        ctx.task_store,
        ctx._dispatched_ws_tasks,
        task_id,
        TaskStatus.COMPLETED.value,
        result=data.get("result"),
    )

    # P1.2: Task completed → cancel the timeout timer
    if ctx.cancel_task_timeout is not None:
        ctx.cancel_task_timeout(task_id)


# ── task_fail ───────────────────────────────────────────────────────────


async def handle_task_fail(
    ws: web.WebSocketResponse,
    data: Dict[str, Any],
    ctx: WSContext,
) -> None:
    """Agent reports a task has failed.

    Expected payload::

        {"type": "task_fail", "id": "task-uuid",
         "status": "failed", "error": "Timeout", "code": "ERR_TIMEOUT"}
    """
    task_id = data.get("id", "")
    if not task_id:
        logger.warning("task_fail without 'id' from agent '%s'", ctx.agent_id)
        return

    error_detail = data.get("error", "Unknown error")
    error_code = data.get("code", "")

    task = ctx.tasks.get(task_id)
    if task:
        task["state"] = "failed"
        task["error"] = error_detail
        task["error_code"] = error_code
        task["updated_at"] = time.time()
        logger.info("Task %s failed by agent '%s': %s",
                    task_id, ctx.agent_id, error_detail)
    else:
        now = time.time()
        ctx.tasks[task_id] = {
            "id": task_id,
            "agent_id": ctx.agent_id,
            "state": "failed",
            "error": error_detail,
            "error_code": error_code,
            "result": None,
            "created_at": now,
            "updated_at": now,
        }
        logger.info("Task %s failed (auto-created) by agent '%s': %s",
                    task_id, ctx.agent_id, error_detail)

    # Reconcile with kanban TaskStore
    _reconcile_task_store(
        ctx.task_store,
        ctx._dispatched_ws_tasks,
        task_id,
        TaskStatus.FAILED.value,
        error=error_detail,
    )

    # P1.2: Task failed → cancel the timeout timer
    if ctx.cancel_task_timeout is not None:
        ctx.cancel_task_timeout(task_id)


# ── task_result (backward-compatible) ───────────────────────────────────


async def handle_task_result(
    ws: web.WebSocketResponse,
    data: Dict[str, Any],
    ctx: WSContext,
) -> None:
    """Legacy ``task_result`` — superseded by ``task_complete`` / ``task_fail``.

    Still handled for backward compatibility with agents that send the old
    v1.0 ``task_result`` message.
    """
    task_id = data.get("id", "")
    if not task_id:
        return

    status = data.get("status", "completed")
    task = ctx.tasks.get(task_id)

    if task:
        task["state"] = status
        task["result"] = data.get("result", {})
        task["error"] = data.get("error")
        task["updated_at"] = time.time()
    else:
        now = time.time()
        ctx.tasks[task_id] = {
            "id": task_id,
            "agent_id": ctx.agent_id,
            "state": status,
            "result": data.get("result", {}),
            "error": data.get("error"),
            "created_at": now,
            "updated_at": now,
        }
        logger.info("Task %s result received (auto-created) from agent '%s'",
                    task_id, ctx.agent_id)

    _reconcile_task_store(
        ctx.task_store,
        ctx._dispatched_ws_tasks,
        task_id,
        status,
        result=data.get("result"),
        error=data.get("error"),
    )

    # P1.2: Legacy task_result → cancel the timeout timer (terminal state)
    if ctx.cancel_task_timeout is not None:
        ctx.cancel_task_timeout(task_id)


# ── state_sync ──────────────────────────────────────────────────────────


async def handle_state_sync(
    ws: web.WebSocketResponse,
    data: Dict[str, Any],
    ctx: WSContext,
) -> None:
    """Reconnection state-sync from an agent — bidirectional state merge.

    Expected payload::

        {"type": "state_sync", "agent_id": "uuid",
         "active_tasks": [
             {"id": "task-uuid", "status": "working", "started_at": 1717000000}
         ]}

    Merge algorithm (docs/ws-task-lifecycle.md §2.4):

    1. Query DB (TaskStore) for tasks dispatched to this agent but not yet
       terminal — restore any missing from in-memory ``ctx.tasks``.
    2. Merge agent-reported ``active_tasks`` into in-memory state.
    3. Detect orphaned tasks: server considers them completed/failed but
       the agent didn't report them → send ``state_sync_reply``.
    4. Agent-completed but server-unknown tasks are logged; the agent is
       expected to retransmit ``task_complete``.
    """
    agent_id = data.get("agent_id", "")
    active_tasks = data.get("active_tasks", [])

    # ------------------------------------------------------------------
    # P3.3 Security boundary checks
    # ------------------------------------------------------------------

    # 1 — agent_id validation: message agent_id must match WS connection owner
    if agent_id and agent_id != ctx.agent_id:
        logger.warning(
            "state_sync REJECTED from agent '%s': message claims agent_id '%s' — mismatch",
            ctx.agent_id, agent_id,
        )
        await ws.send_json({
            "type": "error",
            "detail": (
                f"state_sync agent_id '{agent_id}' does not match "
                f"connection owner '{ctx.agent_id}'"
            ),
        })
        return

    # Use the authenticated connection owner as authoritative agent_id
    agent_id = ctx.agent_id

    # 2 — Rate limiting: max 1 state_sync per 30 seconds per agent
    allowed, retry_after = _check_state_sync_rate_limit(agent_id)
    if not allowed:
        logger.warning(
            "state_sync REJECTED from agent '%s': rate limited, retry in %.1fs",
            agent_id, retry_after,
        )
        await ws.send_json({
            "type": "error",
            "detail": f"state_sync rate limited: retry after {retry_after:.0f}s",
            "retry_after": round(retry_after, 1),
        })
        return

    # 3 — Agent registration check: only accept registered agents
    if ctx.store is not None:
        card = ctx.store.get_agent(agent_id, tenant=ctx.ws_tenant or None)
        if card is None:
            logger.warning(
                "state_sync REJECTED from unregistered agent '%s'",
                agent_id,
            )
            await ws.send_json({
                "type": "error",
                "detail": f"Agent '{agent_id}' is not registered",
            })
            return
        if card.get("disabled", False):
            logger.warning(
                "state_sync REJECTED from disabled agent '%s'",
                agent_id,
            )
            await ws.send_json({
                "type": "error",
                "detail": f"Agent '{agent_id}' is disabled",
            })
            return

    # 4 — Log all accepted state_sync requests
    logger.info(
        "state_sync from agent '%s': %d active task(s) reported",
        agent_id, len(active_tasks),
    )

    # Build a set of task ids the agent thinks are active
    agent_active_ids = {t["id"] for t in active_tasks if "id" in t}

    # ------------------------------------------------------------------
    # Step 1 — Query DB for tasks dispatched to this agent
    # ------------------------------------------------------------------
    # _dispatched_ws_tasks: {task_id: assignee} — tracks which kanban
    # board tasks were WS-dispatched.  We need the DB (TaskStore) for
    # the authoritative status of those tasks.
    db_dispatched: dict[str, dict] = {}  # task_id → {status, result, error}
    if ctx.task_store is not None and ctx._dispatched_ws_tasks is not None:
        for task_id, assignee in list(ctx._dispatched_ws_tasks.items()):
            if assignee != agent_id:
                continue
            try:
                task_obj = ctx.task_store.get_task(task_id)
                if task_obj is not None:
                    db_dispatched[task_id] = {
                        "id": task_obj.id,
                        "status": task_obj.status,
                        "result": task_obj.result,
                    }
            except Exception:
                logger.exception(
                    "state_sync: failed to query TaskStore for task '%s'",
                    task_id,
                )

    # Restore tasks that exist in DB but are missing from in-memory state
    restored_count = 0
    for task_id, db_info in db_dispatched.items():
        if task_id not in ctx.tasks:
            db_status = db_info["status"]
            if db_status in ("completed", "failed"):
                # Terminal task — no need to restore, just note for orphan reply
                continue
            now = time.time()
            ctx.tasks[task_id] = {
                "id": task_id,
                "agent_id": agent_id,
                "state": db_status,
                "result": None,
                "error": None,
                "created_at": now,
                "updated_at": now,
            }
            restored_count += 1

    if restored_count:
        logger.info(
            "state_sync: restored %d non-terminal task(s) from DB for agent '%s'",
            restored_count, agent_id,
        )

    # ------------------------------------------------------------------
    # Step 2 — Merge agent-reported active_tasks into in-memory state
    #
    # NOTE: Tasks the agent reports as terminal (completed / failed / success)
    # that do NOT exist in ctx.tasks are intentionally NOT auto-created here.
    # They will be caught by Step 4 below — the agent should retransmit a
    # proper task_complete / task_fail message so the server can record the
    # result and reconcile with the kanban TaskStore.
    # ------------------------------------------------------------------
    # P1.3: Count healed tasks for dangling-timer cancellation
    healed_count = 0
    for at in active_tasks:
        at_id = at.get("id")
        at_status = at.get("status")
        if not at_id or not at_status:
            continue

        server_task = ctx.tasks.get(at_id)
        if server_task:
            server_task["state"] = at_status
            server_task["updated_at"] = time.time()
        elif at_status not in ("completed", "failed", "success", "done"):
            # Only auto-create non-terminal tasks
            ctx.tasks[at_id] = {
                "id": at_id,
                "agent_id": agent_id,
                "state": at_status,
                "started_at": at.get("started_at", time.time()),
                "result": None,
                "error": None,
                "created_at": time.time(),
                "updated_at": time.time(),
            }

        # P1.3: DANGLING→RUNNING healing — DB says dangling, agent says active
        if (at_status in ("working", "running", "accepted")
                and ctx.task_store is not None
                and ctx._dispatched_ws_tasks is not None
                and at_id in ctx._dispatched_ws_tasks):
            try:
                task_obj = ctx.task_store.get_task(at_id)
                if task_obj is not None and task_obj.status == TaskStatus.DANGLING.value:
                    ctx.task_store.update_task_status(
                        at_id, TaskStatus.RUNNING.value,
                    )
                    healed_count += 1
                    logger.info(
                        "state_sync: healed task %s → running (was dangling) "
                        "for agent '%s'", at_id, agent_id,
                    )
            except Exception:
                logger.exception(
                    "state_sync: failed to heal dangling task '%s'", at_id,
                )

    # P1.3: Cancel dangling timer if tasks were healed
    if healed_count and ctx.cancel_dangling_timer is not None:
        ctx.cancel_dangling_timer(agent_id)
        logger.info(
            "state_sync: healed %d dangling task(s) for agent '%s' — timer cancelled",
            healed_count, agent_id,
        )

    # ------------------------------------------------------------------
    # Step 3 — Detect server-completed/agent-unknown orphaned tasks
    # ------------------------------------------------------------------
    reply: list[dict] = []
    reply_seen: set[str] = set()  # dedup guard

    # 3a — Check in-memory tasks
    for task_id, task in list(ctx.tasks.items()):
        if task.get("agent_id") != agent_id:
            continue
        if task_id in agent_active_ids:
            continue
        state = task.get("state", "")
        if state in ("completed", "failed"):
            reply.append({
                "id": task_id,
                "status": state,
                "result": task.get("result"),
                "error": task.get("error"),
            })
            reply_seen.add(task_id)

    # 3b — Also check DB for terminal tasks missed by in-memory state
    #     (e.g. tasks completed/failed after the agent disconnected)
    for task_id, db_info in db_dispatched.items():
        if task_id in reply_seen:
            continue
        if task_id in agent_active_ids:
            continue
        db_status = db_info["status"]
        if db_status in ("completed", "failed"):
            reply.append({
                "id": task_id,
                "status": db_status,
                "result": db_info.get("result"),
                "error": None,
            })

    # ------------------------------------------------------------------
    # Step 4 — Log agent-completed/server-unknown tasks
    #          (agent is expected to retransmit task_complete)
    # ------------------------------------------------------------------
    agent_completed_unknown = [
        t for t in active_tasks
        if t.get("status") in ("completed", "success", "done")
        and t.get("id") not in ctx.tasks
    ]
    if agent_completed_unknown:
        for t in agent_completed_unknown:
            logger.info(
                "state_sync: agent '%s' reports task '%s' as %s "
                "— server has no record, waiting for task_complete",
                agent_id, t["id"], t["status"],
            )

    # Always send state_sync_reply
    await ws.send_json({
        "type": "state_sync_reply",
        "orphaned_tasks": reply,
    })


# ── close ───────────────────────────────────────────────────────────────


async def handle_close(
    ws: web.WebSocketResponse,
    data: Dict[str, Any],
    ctx: WSContext,
) -> None:
    """Agent is voluntarily closing the WebSocket."""
    logger.info("Agent '%s' closing WebSocket", ctx.agent_id)
    # Signal the caller to break out of the listen loop
    # (the caller inspects the return value — see _wrap_with_close_handler)


# ── job_subtask_result (INV-3 stub) ──────────────────────────────────


async def handle_job_subtask_result(
    ws: web.WebSocketResponse,
    data: Dict[str, Any],
    ctx: WSContext,
) -> None:
    """Agent reports a sub-task result for a JOB mode interaction.

    Expected payload::

        {"type": "job_subtask_result", "job_task_id": "...",
         "subtask_id": "...", "status": "completed"|"failed",
         "result": {...} | None, "error": "..." | None}

    This is a **placeholder** for T6 which will implement full JOB
    sub-task lifecycle management (SCN-04).  Currently only logs the
    sub-task result and does not persist it.
    """
    job_task_id = data.get("job_task_id", "")
    subtask_id = data.get("subtask_id", "")
    status = data.get("status", "completed")

    logger.info(
        "JOB sub-task %s reported by agent '%s': job=%s status=%s (stub — T6)",
        subtask_id, ctx.agent_id, job_task_id, status,
    )

    # T6 TODO: persist sub-task result, reconcile job DAG state,
    # promote children, trigger event bus notifications


# ---------------------------------------------------------------------------
# Default router factory
# ---------------------------------------------------------------------------


def create_default_router(
    extra_handlers: Optional[Dict[str, WSHandler]] = None,
) -> WSMessageRouter:
    """Build a :class:`WSMessageRouter` pre-loaded with all protocol handlers.

    Args:
        extra_handlers: Optional dict of ``{msg_type: handler}`` to add
                        on top of the defaults (e.g. custom extensions).

    Returns:
        A fully configured router.
    """
    # Lazy import to break circular dependency:
    # orchestration.sync_routes → registry_handler → orchestration.sync_routes
    from simple_a2a_registry.orchestration.sync_routes import (  # noqa: PLC0415
        handle_ws_sync_response,
    )

    router = WSMessageRouter()

    router.register("ping", handle_ping)
    router.register("task_ack", handle_task_ack)
    router.register("task_progress", handle_task_progress)
    router.register("task_complete", handle_task_complete)
    router.register("task_fail", handle_task_fail)
    router.register("task_result", handle_task_result)
    router.register("state_sync", handle_state_sync)
    router.register("close", handle_close)

    # SYNC_CALL response handler (INV-3: direct result, no state machine)
    router.register("sync_call_response", handle_ws_sync_response)

    # JOB sub-task result handler (INV-3 stub — will be extended in T6)
    router.register("job_subtask_result", handle_job_subtask_result)

    if extra_handlers:
        for msg_type, handler in extra_handlers.items():
            router.register(msg_type, handler)

    return router


# ---------------------------------------------------------------------------
# Module-level bridge — called by server.py's inline listen loop
# ---------------------------------------------------------------------------

_router_instance: Optional[WSMessageRouter] = None


def _get_ws_handler(
    msg_type: str,
) -> Optional[Callable[[object, web.WebSocketResponse, dict, str], Awaitable[None]]]:
    """Look up *msg_type* in the singleton router and return an adapter.

    The returned adapter matches the call signature used in
    ``RegistryHandler.handle_ws``::

        await handler_fn(registry_handler, ws, data, agent_id)

    It internally wraps the real handler which expects ``(ws, data, ctx)``
    with a ``WSContext`` built from the RegistryHandler's attributes.

    Returns ``None`` for unregistered types (silently ignored).
    """
    global _router_instance
    if _router_instance is None:
        _router_instance = create_default_router()

    handler = _router_instance._handlers.get(msg_type)
    if handler is None:
        return None

    async def _adapter(
        registry_handler: object,
        ws: web.WebSocketResponse,
        data: dict,
        agent_id: str,
    ) -> None:
        ctx = WSContext(
            agent_id=agent_id,
            tasks=getattr(registry_handler, "_tasks", {}),
            connections=getattr(registry_handler, "_ws_connections", {}),
            task_store=getattr(registry_handler, "task_store", None),
            _dispatched_ws_tasks=getattr(registry_handler, "_dispatched_ws_tasks", None),
            ws_tenant="",
            reset_task_timeout=getattr(registry_handler, "_reset_task_timeout", None),
            cancel_task_timeout=getattr(registry_handler, "_cancel_task_timeout", None),
            cancel_dangling_timer=getattr(registry_handler, "_cancel_dangling_timer", None),
            store=getattr(registry_handler, "store", None),
            broadcast_progress_fn=getattr(registry_handler, "_broadcast_progress_fn", None),
        )
        await handler(ws, data, ctx)

    return _adapter


def _reconcile_task_store(
    task_store: Optional[TaskStore],
    dispatched_tasks: Optional[Dict[str, str]],
    task_id: str,
    status: str,
    result: Any = None,
    error: Optional[str] = None,
    flow_control: Optional[Any] = None,
) -> None:
    """If *task_id* was WS-dispatched from the kanban board, update its status.

    This mirrors the logic of ``server._maybe_update_kanban`` and keeps the
    router self-contained.
    """
    if task_store is None or dispatched_tasks is None:
        return
    if task_id not in dispatched_tasks:
        return

    assignee = dispatched_tasks.get(task_id)

    try:
        if status in (TaskStatus.RUNNING.value,):
            # task_ack → mark as running
            task_store.update_task_status(task_id, TaskStatus.RUNNING.value)
            logger.info("Kanban task %s → running (via task_ack)", task_id)
        elif status in (TaskStatus.COMPLETED.value, "success"):
            task_store.update_task_status(
                task_id, TaskStatus.COMPLETED.value,
                result=json.dumps(result) if isinstance(result, dict) else result,
            )
            # Notify flow control
            if flow_control is not None and assignee:
                flow_control.on_task_completed(assignee)
                flow_control.on_task_departed(assignee)
            logger.info("Kanban task %s → completed (via WS)", task_id)
        elif status in (TaskStatus.FAILED.value, "error"):
            task_store.update_task_status(
                task_id, TaskStatus.FAILED.value,
                result=error or str(result) if result else "Agent reported failure",
            )
            # Notify flow control
            if flow_control is not None and assignee:
                flow_control.on_task_failed(assignee)
                flow_control.on_task_departed(assignee)
            logger.info("Kanban task %s → failed (via WS)", task_id)
    except Exception:
        logger.exception("Failed to reconcile kanban task '%s' with status '%s'",
                         task_id, status)