"""V2 REST API routes for the Orchestration Engine.

Endpoints
---------
POST   /v2/tasks                        — create task
GET    /v2/tasks                        — list tasks
GET    /v2/tasks/{id}                   — task detail (parents, children, runs, comments, events)
POST   /v2/tasks/{id}/claim             — claim task
POST   /v2/tasks/{id}/complete          — complete task
POST   /v2/tasks/{id}/block             — block task (HITL)
POST   /v2/tasks/{id}/unblock           — unblock task
POST   /v2/tasks/{id}/heartbeat         — heartbeat (extend TTL)
POST   /v2/tasks/{id}/comment           — add comment
DELETE /v2/tasks/{id}                   — archive task
POST   /v2/tasks/{id}/depend            — add dependency
DELETE /v2/tasks/{id}/depend/{parent_id} — remove dependency
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from aiohttp import web

from simple_a2a_registry.auth import require_scope
from simple_a2a_registry.orchestration.store import DEFAULT_CLAIM_TTL, TaskStore
from simple_a2a_registry.orchestration.models import (
    TaskStatus,
    TaskRunStatus,
    TaskRunOutcome,
    TaskEventKind,
)
from simple_a2a_registry.orchestration.state_machine import (
    InvalidTransitionError,
)

logger = logging.getLogger("a2a_registry.orchestration.routes")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _json_error(status: int, error_code: str, detail: str) -> web.Response:
    return web.json_response(
        {"error": error_code, "detail": detail},
        status=status,
    )


def _task_to_brief(task) -> dict:
    """Compact task representation for list endpoints."""
    return {
        "id": task.id,
        "title": task.title,
        "status": task.status,
        "assignee": task.assignee,
        "priority": task.priority,
        "created_at": task.created_at,
        "started_at": task.started_at,
        "completed_at": task.completed_at,
        "tenant": task.tenant,
    }


def _task_to_detail(task) -> dict:
    """Full task representation for GET detail endpoint."""
    d = _task_to_brief(task)
    d.update({
        "body": task.body,
        "created_by": task.created_by,
        "workspace_kind": task.workspace_kind,
        "workspace_path": task.workspace_path,
        "claim_lock": task.claim_lock,
        "claim_expires": task.claim_expires,
        "max_runtime_seconds": task.max_runtime_seconds,
        "max_retries": task.max_retries,
        "consecutive_failures": task.consecutive_failures,
        "current_run_id": task.current_run_id,
        "result": json.loads(task.result) if task.result else None,
    })
    return d


def _run_to_dict(run) -> dict:
    d = run.to_dict()
    if d.get("metadata"):
        try:
            d["metadata"] = json.loads(d["metadata"])
        except (json.JSONDecodeError, TypeError):
            pass
    return d


# ---------------------------------------------------------------------------
# OrchestrationHandler
# ---------------------------------------------------------------------------


class OrchestrationHandler:
    """HTTP handler for the V2 Orchestration Engine REST API."""

    def __init__(self, store: TaskStore,
                 registry_store: Any = None) -> None:
        self.store = store
        self.registry_store = registry_store
        # Callback for broadcasting events to Admin UI WebSocket clients.
        # Wired up by create_app in server.py.
        self._broadcast_fn = None  # async callable(event_type: str, data: dict)

    # ----------------------------------------------------------
    # POST /v2/tasks — Create
    # ----------------------------------------------------------

    async def handle_create_task(self, request: web.Request) -> web.Response:
        """POST /v2/tasks — create a new task with optional parent dependencies."""
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return _json_error(400, "invalid_json", "Invalid JSON body")

        title = (body.get("title") or "").strip()
        if not title:
            return _json_error(
                400, "validation_error", "Missing required 'title' field"
            )

        # Normalize priority: allow int (5) or string ("normal"→0, "low"→1, "high"→10)
        raw_priority = body.get("priority", 0)
        _priority_map = {"low": 1, "normal": 0, "high": 10, "critical": 20}
        if isinstance(raw_priority, str):
            priority = _priority_map.get(raw_priority.lower(), 0)
        else:
            try:
                priority = int(raw_priority)
            except (ValueError, TypeError):
                priority = 0

        parents: Optional[List[str]] = body.get("parents")

        try:
            task = self.store.create_task(
                title=title,
                body=body.get("body"),
                assignee=body.get("assignee"),
                priority=priority,
                parents=parents,
                workspace_kind=body.get("workspace_kind"),
                workspace_path=body.get("workspace_path"),
                max_runtime_seconds=body.get("max_runtime_seconds"),
                max_retries=body.get("max_retries"),
                tenant=body.get("tenant"),
                created_by=body.get("created_by"),
            )
        except ValueError as e:
            msg = str(e)
            if "cycle" in msg.lower():
                return _json_error(400, "cycle_detected", msg)
            if "not found" in msg.lower():
                return _json_error(400, "parent_not_found", msg)
            return _json_error(400, "validation_error", msg)

        # Broadcast to Admin UI WebSocket clients
        if self._broadcast_fn:
            await self._broadcast_fn("created", _task_to_detail(task))

        return web.json_response(
            {"task": _task_to_detail(task)},
            status=201,
        )

    # ----------------------------------------------------------
    # PATCH /v2/tasks/{id} — Update metadata
    # ----------------------------------------------------------

    async def handle_update_task(self, request: web.Request) -> web.Response:
        """PATCH /v2/tasks/{id} — update title, body, assignee, priority, or status."""
        task_id = request.match_info["id"]

        try:
            body = await request.json()
        except json.JSONDecodeError:
            return _json_error(400, "invalid_json", "Invalid JSON body")

        valid_fields = ("title", "body", "assignee", "priority", "status")
        if not any(k in body for k in valid_fields):
            return _json_error(
                400, "validation_error",
                f"Must provide at least one of: {', '.join(valid_fields)}",
            )

        try:
            # Normalize priority for both create and update
            raw_priority = body.get("priority")
            if raw_priority is not None:
                _priority_map = {"low": 1, "normal": 0, "high": 10, "critical": 20}
                if isinstance(raw_priority, str):
                    priority = _priority_map.get(raw_priority.lower(), 0)
                else:
                    try:
                        priority = int(raw_priority)
                    except (ValueError, TypeError):
                        priority = 0
            else:
                priority = None

            # Status transition — delegates to update_task_status (state machine)
            if "status" in body:
                task = self.store.update_task_status(
                    task_id,
                    body["status"],
                )
            else:
                task = self.store.update_task(
                    task_id,
                    title=body.get("title"),
                    body=body.get("body"),
                    assignee=body.get("assignee"),
                    priority=priority,
                )
                # Auto-promote TODO → READY when assignee is set on a root task
                if (
                    "assignee" in body
                    and task.assignee
                    and task.status == TaskStatus.TODO.value
                ):
                    task_data = self.store.get_task(task_id)
                    if task_data and not task_data.parents:
                        task = self.store.update_task_status(
                            task_id, TaskStatus.READY.value,
                        )
        except ValueError as e:
            msg = str(e)
            if "not found" in msg.lower():
                return _json_error(404, "task_not_found", msg)
            return _json_error(400, "validation_error", msg)
        except Exception as e:
            # Wrap state machine errors (InvalidTransitionError etc.)
            return _json_error(400, "status_error", str(e))

        # Broadcast to Admin UI WebSocket clients
        event_type = "status_changed" if "status" in body else "updated"
        if self._broadcast_fn:
            await self._broadcast_fn(event_type, _task_to_detail(task))

        return web.json_response({"task": _task_to_detail(task)})

    # ----------------------------------------------------------
    # GET /v2/tasks — List
    # ----------------------------------------------------------

    async def handle_list_tasks(self, request: web.Request) -> web.Response:
        """GET /v2/tasks — list tasks with filtering, pagination, sorting."""
        try:
            limit = min(int(request.query.get("limit", 50)), 200)
        except (ValueError, TypeError):
            limit = 50
        try:
            offset = max(int(request.query.get("offset", 0)), 0)
        except (ValueError, TypeError):
            offset = 0

        status_filter = request.query.get("status") or None
        assignee_filter = request.query.get("assignee") or None
        tenant_filter = request.query.get("tenant") or None
        parent_id_filter = request.query.get("parent_id") or None
        q = request.query.get("q") or None
        sort = request.query.get("sort", "-created_at")

        tasks, total = self.store.list_tasks(
            status=status_filter,
            assignee=assignee_filter,
            tenant=tenant_filter,
            parent_id=parent_id_filter,
            q=q,
            limit=limit,
            offset=offset,
            sort=sort,
        )

        return web.json_response({
            "total": total,
            "limit": limit,
            "offset": offset,
            "tasks": [_task_to_brief(t) for t in tasks],
        })

    # ----------------------------------------------------------
    # GET /v2/tasks/{id} — Detail
    # ----------------------------------------------------------

    async def handle_get_task(self, request: web.Request) -> web.Response:
        """GET /v2/tasks/{id} — full detail with parents, children, runs, comments, events."""
        task_id = request.match_info["id"]
        task = self.store.get_task(task_id)
        if task is None:
            return _json_error(404, "task_not_found", f"Task '{task_id}' not found")

        # Load parents, children (already loaded by get_task if store loads them)
        parents = self.store.get_parents(task_id)
        children = self.store.get_children(task_id)
        runs = self.store.get_runs(task_id)
        comments = self.store.get_comments(task_id)
        events = self.store.get_events(task_id)

        return web.json_response({
            "task": _task_to_detail(task),
            "parents": parents,
            "children": children,
            "runs": [_run_to_dict(r) for r in runs],
            "comments": [c.to_dict() for c in comments],
            "events": [e.to_dict() for e in events],
        })

    # ----------------------------------------------------------
    # POST /v2/tasks/{id}/claim
    # ----------------------------------------------------------

    async def handle_claim(self, request: web.Request) -> web.Response:
        """POST /v2/tasks/{id}/claim — worker atomically claims a ready task."""
        task_id = request.match_info["id"]

        try:
            body = await request.json()
        except json.JSONDecodeError:
            return _json_error(400, "invalid_json", "Invalid JSON body")

        worker_id = body.get("worker_id", "unknown").strip()
        pid = body.get("pid", 0)
        ttl = body.get("ttl")  # optional claim TTL override

        result = self.store.claim_task(
            task_id, worker_id, pid,
            ttl=ttl if ttl is not None else DEFAULT_CLAIM_TTL,
        )
        if result is None:
            # Check if task exists to distinguish 404 from 409
            task = self.store.get_task(task_id)
            if task is None:
                return _json_error(
                    404, "task_not_found", f"Task '{task_id}' not found"
                )
            return _json_error(
                409, "claim_conflict",
                f"Task '{task_id}' is not ready or already claimed",
            )

        # Broadcast to Admin UI WebSocket clients
        if self._broadcast_fn:
            task_data = self.store.get_task(task_id)
            if task_data:
                await self._broadcast_fn("status_changed", _task_to_detail(task_data))

        return web.json_response(result)

    # ----------------------------------------------------------
    # POST /v2/tasks/{id}/complete
    # ----------------------------------------------------------

    async def handle_complete(self, request: web.Request) -> web.Response:
        """POST /v2/tasks/{id}/complete — mark a task as completed."""
        task_id = request.match_info["id"]

        try:
            body = await request.json()
        except (json.JSONDecodeError, TypeError):
            body = {}

        claim_lock = body.get("claim_lock")
        summary = body.get("summary")
        result = body.get("result")
        metadata = body.get("metadata")

        # Serialize result if it's a dict/object
        result_str: Optional[str] = None
        if result is not None:
            if isinstance(result, dict):
                result_str = json.dumps(result)
            else:
                result_str = str(result)

        try:
            task = self.store.update_task_status(
                task_id,
                TaskStatus.COMPLETED.value,
                claim_lock=claim_lock,
                result=result_str,
                summary=summary,
                metadata=metadata,
            )
        except InvalidTransitionError as e:
            return _json_error(400, "invalid_status", str(e))
        except PermissionError as e:
            return _json_error(403, "claim_mismatch", str(e))
        except ValueError as e:
            if "not found" in str(e).lower():
                return _json_error(404, "task_not_found", str(e))
            return _json_error(400, "validation_error", str(e))

        # Broadcast to Admin UI WebSocket clients
        if self._broadcast_fn:
            task_data = self.store.get_task(task_id)
            if task_data:
                await self._broadcast_fn("status_changed", _task_to_detail(task_data))

        return web.json_response({
            "task_id": task_id,
            "status": TaskStatus.COMPLETED.value,
        })

    # ----------------------------------------------------------
    # POST /v2/tasks/{id}/block
    # ----------------------------------------------------------

    async def handle_block(self, request: web.Request) -> web.Response:
        """POST /v2/tasks/{id}/block — block a running task (HITL)."""
        task_id = request.match_info["id"]

        try:
            body = await request.json()
        except (json.JSONDecodeError, TypeError):
            body = {}

        claim_lock = body.get("claim_lock")
        reason = body.get("reason", "Blocked by human-in-the-loop")

        try:
            task = self.store.update_task_status(
                task_id,
                TaskStatus.BLOCKED.value,
                claim_lock=claim_lock,
            )
        except InvalidTransitionError as e:
            return _json_error(400, "invalid_status", str(e))
        except PermissionError as e:
            return _json_error(403, "claim_mismatch", str(e))
        except ValueError as e:
            if "not found" in str(e).lower():
                return _json_error(404, "task_not_found", str(e))
            return _json_error(400, "validation_error", str(e))

        # Add an automatic comment with the block reason
        if reason:
            try:
                self.store.add_comment(
                    task_id, "system", f"Block reason: {reason}"
                )
            except Exception:
                pass

        # Broadcast to Admin UI WebSocket clients
        if self._broadcast_fn:
            task_data = self.store.get_task(task_id)
            if task_data:
                await self._broadcast_fn("status_changed", _task_to_detail(task_data))

        return web.json_response({
            "task_id": task_id,
            "status": TaskStatus.BLOCKED.value,
            "block_reason": reason,
        })

    # ----------------------------------------------------------
    # POST /v2/tasks/{id}/unblock
    # ----------------------------------------------------------

    async def handle_unblock(self, request: web.Request) -> web.Response:
        """POST /v2/tasks/{id}/unblock — unblock a blocked task."""
        task_id = request.match_info["id"]

        try:
            body = await request.json()
        except (json.JSONDecodeError, TypeError):
            body = {}

        reason = body.get("reason", "Unblocked")

        try:
            task = self.store.update_task_status(
                task_id, TaskStatus.RUNNING.value,
            )
        except InvalidTransitionError as e:
            return _json_error(400, "invalid_status", str(e))
        except ValueError as e:
            if "not found" in str(e).lower():
                return _json_error(404, "task_not_found", str(e))
            return _json_error(400, "validation_error", str(e))

        # Add a comment
        if reason:
            try:
                self.store.add_comment(
                    task_id, "system", f"Unblock reason: {reason}"
                )
            except Exception:
                pass

        # Broadcast to Admin UI WebSocket clients
        if self._broadcast_fn:
            task_data = self.store.get_task(task_id)
            if task_data:
                await self._broadcast_fn("status_changed", _task_to_detail(task_data))

        return web.json_response({
            "task_id": task_id,
            "status": TaskStatus.RUNNING.value,
        })

    # ----------------------------------------------------------
    # POST /v2/tasks/{id}/heartbeat
    # ----------------------------------------------------------

    async def handle_heartbeat(self, request: web.Request) -> web.Response:
        """POST /v2/tasks/{id}/heartbeat — extend claim TTL."""
        task_id = request.match_info["id"]

        try:
            body = await request.json()
        except (json.JSONDecodeError, TypeError):
            body = {}

        claim_lock = body.get("claim_lock", "")

        ok = self.store.heartbeat(task_id, claim_lock)
        if not ok:
            task = self.store.get_task(task_id)
            if task is None:
                return _json_error(
                    404, "task_not_found", f"Task '{task_id}' not found"
                )
            return _json_error(
                403, "claim_mismatch",
                f"Heartbeat rejected: claim_lock '{claim_lock}' does not match",
            )

        # Fetch current claim_expires from the db
        task = self.store.get_task(task_id)
        return web.json_response({
            "task_id": task_id,
            "claim_expires": task.claim_expires if task else 0,
        })

    # ----------------------------------------------------------
    # POST /v2/tasks/{id}/comment
    # ----------------------------------------------------------

    async def handle_add_comment(self, request: web.Request) -> web.Response:
        """POST /v2/tasks/{id}/comment — add a comment to a task."""
        task_id = request.match_info["id"]

        try:
            body = await request.json()
        except json.JSONDecodeError:
            return _json_error(400, "invalid_json", "Invalid JSON body")

        author = (body.get("author") or "anonymous").strip()
        comment_body = (body.get("body") or "").strip()
        if not comment_body:
            return _json_error(
                400, "validation_error", "Missing required 'body' field"
            )

        try:
            comment = self.store.add_comment(task_id, author, comment_body)
        except ValueError as e:
            if "not found" in str(e).lower():
                return _json_error(404, "task_not_found", str(e))
            return _json_error(400, "validation_error", str(e))

        # Broadcast to Admin UI WebSocket clients
        if self._broadcast_fn:
            task_data = self.store.get_task(task_id)
            if task_data:
                await self._broadcast_fn("comment_added", {
                    **{"comment": {"id": comment.id, "author": author, "body": comment_body, "created_at": comment.created_at}},
                    **_task_to_detail(task_data),
                })

        return web.json_response(
            {"comment_id": comment.id, "created_at": comment.created_at},
            status=201,
        )

    # ----------------------------------------------------------
    # DELETE /v2/tasks/{id} — Archive
    # ----------------------------------------------------------

    async def handle_delete_task(self, request: web.Request) -> web.Response:
        """DELETE /v2/tasks/{id} — archive a completed or failed task."""
        task_id = request.match_info["id"]

        task = self.store.get_task(task_id)
        if task is None:
            return _json_error(404, "task_not_found", f"Task '{task_id}' not found")

        if task.status not in (
            TaskStatus.COMPLETED.value,
            TaskStatus.FAILED.value,
            TaskStatus.CANCELLED.value,
        ):
            return _json_error(
                400, "invalid_status",
                f"Task '{task_id}' is '{task.status}'; only completed, failed, "
                "or cancelled tasks can be archived",
            )

        try:
            self.store.update_task_status(task_id, TaskStatus.ARCHIVED.value)

            # Clean up workspace
            try:
                ws_mgr = request.app.get("ws_mgr")
                if ws_mgr:
                    ws_mgr.cleanup(task)
            except Exception:
                logger.exception("Workspace cleanup failed for task '%s'", task_id)

        except InvalidTransitionError as e:
            return _json_error(400, "invalid_status", str(e))

        # Broadcast to Admin UI WebSocket clients
        if self._broadcast_fn:
            await self._broadcast_fn("deleted", {"id": task_id, "title": task.title, "status": TaskStatus.ARCHIVED.value})

        return web.json_response({
            "task_id": task_id,
            "status": TaskStatus.ARCHIVED.value,
        })

    # ----------------------------------------------------------
    # POST /v2/tasks/{id}/depend — Add dependency
    # ----------------------------------------------------------

    async def handle_add_dependency(self, request: web.Request) -> web.Response:
        """POST /v2/tasks/{id}/depend — add a parent dependency to a task."""
        task_id = request.match_info["id"]

        try:
            body = await request.json()
        except json.JSONDecodeError:
            return _json_error(400, "invalid_json", "Invalid JSON body")

        parent_id = body.get("parent_id", "").strip()
        if not parent_id:
            return _json_error(
                400, "validation_error", "Missing required 'parent_id' field"
            )

        try:
            self.store.add_dependency(task_id, parent_id)
        except ValueError as e:
            msg = str(e)
            if "cycle" in msg.lower():
                return _json_error(400, "cycle_detected", msg)
            if "not found" in msg.lower():
                # Determine which one wasn't found
                if parent_id in msg:
                    return _json_error(400, "parent_not_found", msg)
                return _json_error(404, "task_not_found", msg)
            return _json_error(400, "validation_error", msg)

        # Broadcast to Admin UI WebSocket clients
        if self._broadcast_fn:
            task_data = self.store.get_task(task_id)
            if task_data:
                await self._broadcast_fn("updated", _task_to_detail(task_data))

        return web.json_response({
            "task_id": task_id,
            "parent_id": parent_id,
            "status": "dependency_added",
        })

    # ----------------------------------------------------------
    # DELETE /v2/tasks/{id}/depend/{parent_id} — Remove dependency
    # ----------------------------------------------------------

    async def handle_remove_dependency(
        self, request: web.Request
    ) -> web.Response:
        """DELETE /v2/tasks/{id}/depend/{parent_id} — remove a parent dependency."""
        task_id = request.match_info["id"]
        parent_id = request.match_info["parent_id"]

        removed = self.store.remove_dependency(task_id, parent_id)
        if not removed:
            return _json_error(
                404, "dependency_not_found",
                f"Dependency parent='{parent_id}' → child='{task_id}' not found",
            )

        # Broadcast to Admin UI WebSocket clients
        if self._broadcast_fn:
            task_data = self.store.get_task(task_id)
            if task_data:
                await self._broadcast_fn("updated", _task_to_detail(task_data))

        return web.json_response({
            "task_id": task_id,
            "parent_id": parent_id,
            "status": "dependency_removed",
        })

    # ----------------------------------------------------------
    # Stats
    # ----------------------------------------------------------

    async def handle_stats(self, request: web.Request) -> web.Response:
        """GET /v2/stats — return summary statistics across all tasks."""
        stats = self.store.stats()
        return web.json_response(stats)

    async def handle_stats_by_tenant(self, request: web.Request) -> web.Response:
        """GET /v2/stats/tenants — return statistics grouped by tenant.

        Returns both task stats and (if registry_store is available) agent stats.
        """
        result: dict = {
            "task_stats": self.store.stats_by_tenant(),
        }
        if self.registry_store:
            raw = self.registry_store.stats_by_tenant()
            tenants: Dict[str, Dict[str, int]] = {}
            total = 0
            for tid, s in raw.items():
                t = tid if tid else ""
                cnt = s.get("totalAgents", 0)
                tenants[t] = {"total": cnt}
                total += cnt
            result["agent_stats"] = {"total": total, "tenants": tenants}
        return web.json_response(result)


# ---------------------------------------------------------------------------
# Route registration helper
# ---------------------------------------------------------------------------


def register_v2_routes(app: web.Application, handler: OrchestrationHandler) -> None:
    """Register all V2 orchestration routes on *app*."""
    # POST /v2/tasks — create task (task:write)
    app.router.add_post("/v2/tasks", require_scope("task:write")(handler.handle_create_task))
    # PATCH /v2/tasks/{id} — update metadata (task:write)
    app.router.add_patch("/v2/tasks/{id}", require_scope("task:write")(handler.handle_update_task))
    # GET /v2/tasks — list tasks (task:read)
    app.router.add_get("/v2/tasks", require_scope("task:read")(handler.handle_list_tasks))
    # GET /v2/tasks/{id} — detail (task:read)
    app.router.add_get("/v2/tasks/{id}", require_scope("task:read")(handler.handle_get_task))
    # POST /v2/tasks/{id}/claim — claim (task:write)
    app.router.add_post("/v2/tasks/{id}/claim", require_scope("task:write")(handler.handle_claim))
    # POST /v2/tasks/{id}/complete — complete (task:write)
    app.router.add_post("/v2/tasks/{id}/complete", require_scope("task:write")(handler.handle_complete))
    # POST /v2/tasks/{id}/block — block (task:write)
    app.router.add_post("/v2/tasks/{id}/block", require_scope("task:write")(handler.handle_block))
    # POST /v2/tasks/{id}/unblock — unblock (task:write)
    app.router.add_post("/v2/tasks/{id}/unblock", require_scope("task:write")(handler.handle_unblock))
    # POST /v2/tasks/{id}/heartbeat — heartbeat (task:write)
    app.router.add_post("/v2/tasks/{id}/heartbeat", require_scope("task:write")(handler.handle_heartbeat))
    # POST /v2/tasks/{id}/comment — comment (task:write)
    app.router.add_post("/v2/tasks/{id}/comment", require_scope("task:write")(handler.handle_add_comment))
    # DELETE /v2/tasks/{id} — archive (task:write)
    app.router.add_delete("/v2/tasks/{id}", require_scope("task:write")(handler.handle_delete_task))
    # POST /v2/tasks/{id}/depend — add dependency (task:write)
    app.router.add_post("/v2/tasks/{id}/depend", require_scope("task:write")(handler.handle_add_dependency))
    # DELETE /v2/tasks/{id}/depend/{parent_id} — remove dependency (task:write)
    app.router.add_delete(
        "/v2/tasks/{id}/depend/{parent_id}",
        require_scope("task:write")(handler.handle_remove_dependency),
    )
    # GET /v2/stats — registry statistics (registry:admin)
    app.router.add_get("/v2/stats", require_scope("registry:admin")(handler.handle_stats))
    # GET /v2/stats/tenants — per-tenant statistics (registry:admin)
    app.router.add_get(
        "/v2/stats/tenants",
        require_scope("registry:admin")(handler.handle_stats_by_tenant),
    )