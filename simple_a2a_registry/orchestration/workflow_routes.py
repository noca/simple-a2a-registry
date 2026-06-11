"""REST API routes for YAML/JSON workflow submission and query.

Endpoints
---------
POST /v2/workflows              — submit YAML/JSON workflow definition
GET  /v2/workflows/{id}         — query workflow status (all task summaries)
GET  /v2/workflows/{id}/tasks   — list all tasks in a workflow
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, Dict, Optional

from aiohttp import web

from simple_a2a_registry.auth import require_scope
from simple_a2a_registry.orchestration.store import TaskStore
from simple_a2a_registry.orchestration.workflow import (
    WorkflowSpec,
    apply_workflow,
    validate_workflow,
)

logger = logging.getLogger("a2a_registry.orchestration.workflow_routes")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _json_error(status: int, error_code: str, detail: str) -> web.Response:
    return web.json_response(
        {"error": error_code, "detail": detail},
        status=status,
    )


# ---------------------------------------------------------------------------
# WorkflowHandler
# ---------------------------------------------------------------------------


class WorkflowHandler:
    """HTTP handler for the V2 Workflow REST API.

    Workflow metadata is kept in-memory (``_workflows`` dict) because the
    individual tasks are already durable in the SQLite-backed TaskStore;
    the workflow abstraction is purely an API grouping concept.
    """

    def __init__(self, store: TaskStore) -> None:
        self.store = store
        # workflow_id -> {name, task_ids: {logical_id: persistent_id}, created_at, ...}
        self._workflows: Dict[str, Dict[str, Any]] = {}
        # Callback for broadcasting events to Admin UI WebSocket clients.
        self._broadcast_fn: Any = None  # async callable(event_type: str, data: dict)

    # ------------------------------------------------------------------
    # POST /v2/workflows — Submit a workflow
    # ------------------------------------------------------------------

    async def handle_create_workflow(self, request: web.Request) -> web.Response:
        """POST /v2/workflows — submit YAML/JSON workflow definition.

        Accepts either a YAML string (``yaml`` field) or inline JSON
        workflow definition (``name``, ``tasks``, etc.).
        """
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return _json_error(400, "invalid_json", "Invalid JSON body")

        # Determine input format: ``yaml`` field or inline JSON definition
        yaml_text = body.get("yaml")
        if yaml_text:
            # Parse from YAML string
            if not isinstance(yaml_text, str) or not yaml_text.strip():
                return _json_error(
                    400, "validation_error",
                    "'yaml' field must be a non-empty string",
                )
            try:
                spec = WorkflowSpec.from_yaml_str(yaml_text)
            except ValueError as e:
                return _json_error(400, "validation_error", str(e))
        else:
            # Parse from inline JSON definition
            name = (body.get("name") or "").strip()
            if not name:
                return _json_error(
                    400, "validation_error",
                    "Missing required 'name' field (or provide 'yaml' field for YAML)",
                )
            try:
                spec = WorkflowSpec._from_dict(body)
            except ValueError as e:
                return _json_error(400, "validation_error", str(e))

        # Validate
        validation_errors = validate_workflow(spec)
        if validation_errors:
            return _json_error(
                400, "validation_error",
                "; ".join(validation_errors),
            )

        # Apply to store
        try:
            result = apply_workflow(self.store, spec)
        except ValueError as e:
            return _json_error(400, "validation_error", str(e))

        if result.errors:
            # Partial success — some tasks created, some failed
            workflow_id = _generate_workflow_id()
            self._workflows[workflow_id] = _make_wf_meta(
                spec, result, partial=True,
            )
            return web.json_response({
                "workflow_id": workflow_id,
                "name": result.name,
                "task_ids": result.task_ids,
                "created_count": result.created_count,
                "errors": result.errors,
            }, status=201)

        workflow_id = _generate_workflow_id()
        self._workflows[workflow_id] = _make_wf_meta(
            spec, result, partial=False,
        )

        response = {
            "workflow_id": workflow_id,
            "name": result.name,
            "task_ids": result.task_ids,
            "created_count": result.created_count,
        }

        # Broadcast to Admin UI WebSocket clients
        if self._broadcast_fn:
            try:
                await self._broadcast_fn("workflow_created", {
                    "workflow_id": workflow_id,
                    "name": spec.name,
                    "task_count": len(result.task_ids),
                })
            except Exception:
                logger.exception("Failed to broadcast workflow_created event")

        return web.json_response(response, status=201)

    # ------------------------------------------------------------------
    # GET /v2/workflows/{id} — Query workflow status
    # ------------------------------------------------------------------

    async def handle_get_workflow(self, request: web.Request) -> web.Response:
        """GET /v2/workflows/{id} — query workflow status summary.

        Returns overall workflow status derived from individual task
        statuses.
        """
        wf_id = request.match_info["id"]
        meta = self._workflows.get(wf_id)
        if meta is None:
            return _json_error(
                404, "workflow_not_found",
                f"Workflow '{wf_id}' not found",
            )

        # Enrich each task with current status from the store
        tasks_status = []
        statuses_seen = set()
        for logical_id, persistent_id in meta["task_ids"].items():
            task = self.store.get_task(persistent_id)
            if task is not None:
                statuses_seen.add(task.status)
                tasks_status.append({
                    "logical_id": logical_id,
                    "task_id": persistent_id,
                    "title": task.title,
                    "status": task.status,
                    "assignee": task.assignee,
                })
            else:
                statuses_seen.add("deleted")
                tasks_status.append({
                    "logical_id": logical_id,
                    "task_id": persistent_id,
                    "status": "deleted",
                })

        # Derive overall workflow status
        workflow_status = _derive_workflow_status(statuses_seen)

        return web.json_response({
            "workflow_id": wf_id,
            "name": meta["name"],
            "description": meta.get("description"),
            "created_at": meta["created_at"],
            "status": workflow_status,
            "task_count": len(meta["task_ids"]),
            "tasks": tasks_status,
        })

    # ------------------------------------------------------------------
    # GET /v2/workflows/{id}/tasks — List all tasks
    # ------------------------------------------------------------------

    async def handle_get_workflow_tasks(self, request: web.Request) -> web.Response:
        """GET /v2/workflows/{id}/tasks — list all tasks in a workflow."""
        wf_id = request.match_info["id"]
        meta = self._workflows.get(wf_id)
        if meta is None:
            return _json_error(
                404, "workflow_not_found",
                f"Workflow '{wf_id}' not found",
            )

        # Gather full task details from the store
        tasks = []
        for logical_id, persistent_id in meta["task_ids"].items():
            task = self.store.get_task(persistent_id)
            if task is not None:
                tasks.append(_task_to_full(logical_id, task))
            else:
                tasks.append({
                    "logical_id": logical_id,
                    "task_id": persistent_id,
                    "status": "deleted",
                })

        return web.json_response({
            "workflow_id": wf_id,
            "name": meta["name"],
            "total": len(tasks),
            "tasks": tasks,
        })


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _generate_workflow_id() -> str:
    """Generate a unique workflow ID prefixed with ``wf_``."""
    return "wf_" + uuid.uuid4().hex[:12]


def _make_wf_meta(
    spec: WorkflowSpec,
    result: Any,
    partial: bool = False,
) -> Dict[str, Any]:
    """Build the internal metadata dict for a submitted workflow."""
    return {
        "name": spec.name,
        "description": spec.description,
        "tenant": spec.tenant,
        "created_by": spec.created_by,
        "task_ids": result.task_ids,
        "created_count": result.created_count,
        "created_at": time.time(),
        "partial": partial,
    }


def _derive_workflow_status(statuses: set) -> str:
    """Derive an overall workflow status from individual task statuses.

    ``running`` if any task is running/claimed/started,
    ``completed`` if all tasks are in terminal states,
    ``failed`` if any task failed,
    ``partially_completed`` if some completed but not all,
    ``pending`` if all are todo/ready.
    """
    terminal = {"completed", "failed", "cancelled", "archived"}
    running = {"running", "dangling", "blocked"}
    waiting = {"todo", "ready"}

    if statuses & running:
        return "running"
    if "failed" in statuses:
        return "failed"
    if statuses.issubset(terminal):
        if statuses == {"completed"} or (statuses - {"completed"} == set()):
            return "completed"
        return "partially_completed"
    if statuses.issubset(waiting | terminal):
        if statuses & waiting:
            return "pending"
    return "unknown"


def _task_to_full(logical_id: str, task: Any) -> Dict[str, Any]:
    """Build a full task representation for the workflow tasks endpoint."""
    return {
        "logical_id": logical_id,
        "task_id": task.id,
        "title": task.title,
        "body": task.body,
        "status": task.status,
        "assignee": task.assignee,
        "priority": task.priority,
        "created_by": task.created_by,
        "created_at": task.created_at,
        "started_at": task.started_at,
        "completed_at": task.completed_at,
        "tenant": task.tenant,
        "workspace_kind": task.workspace_kind,
        "workspace_path": task.workspace_path,
    }


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------


def register_workflow_routes(app: web.Application, handler: WorkflowHandler) -> None:
    """Register all V2 workflow routes on *app*."""
    # POST /v2/workflows — create workflow (task:write)
    app.router.add_post(
        "/v2/workflows",
        require_scope("task:write")(handler.handle_create_workflow),
    )
    # GET /v2/workflows/{id} — workflow status (task:read)
    app.router.add_get(
        "/v2/workflows/{id}",
        require_scope("task:read")(handler.handle_get_workflow),
    )
    # GET /v2/workflows/{id}/tasks — list workflow tasks (task:read)
    app.router.add_get(
        "/v2/workflows/{id}/tasks",
        require_scope("task:read")(handler.handle_get_workflow_tasks),
    )