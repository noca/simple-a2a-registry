"""V2 REST API routes for the Cron Scheduled Task Scheduler.

Endpoints
---------
POST   /v2/cron               — create cron task
GET    /v2/cron               — list cron tasks
DELETE /v2/cron/{id}          — delete cron task
GET    /v2/cron/{id}/executions — execution history
PATCH  /v2/cron/{id}          — update cron task (enable/disable)
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from aiohttp import web

from simple_a2a_registry.auth import require_scope
from simple_a2a_registry.orchestration.cron import (
    CronTask,
    CronTaskStore,
    CronScheduler,
)

logger = logging.getLogger("a2a_registry.orchestration.cron_routes")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _json_error(status: int, error_code: str, detail: str) -> web.Response:
    return web.json_response(
        {"error": error_code, "detail": detail},
        status=status,
    )


# ---------------------------------------------------------------------------
# CronHandler
# ---------------------------------------------------------------------------


class CronHandler:
    """HTTP handler for /v2/cron/* endpoints."""

    def __init__(
        self,
        cron_store: CronTaskStore,
        scheduler: CronScheduler,
    ) -> None:
        self._cron_store = cron_store
        self._scheduler = scheduler

    # ----------------------------------------------------------
    # POST /v2/cron — Create
    # ----------------------------------------------------------

    async def handle_create(self, request: web.Request) -> web.Response:
        """POST /v2/cron — create a new cron task."""
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return _json_error(400, "invalid_json", "Invalid JSON body")

        name = (body.get("name") or "").strip()
        if not name:
            return _json_error(
                400, "validation_error", "Missing required 'name' field",
            )

        assignee = body.get("assignee", "").strip()
        if not assignee:
            return _json_error(
                400, "validation_error", "Missing required 'assignee' field",
            )

        cron_expr = body.get("cron", "").strip()
        if not cron_expr:
            return _json_error(
                400, "validation_error", "Missing required 'cron' field",
            )

        # Validate cron expression
        from simple_a2a_registry.orchestration.cron import compute_next_run
        if compute_next_run(cron_expr, 0) is None:
            return _json_error(
                400, "invalid_cron", f"Invalid cron expression: '{cron_expr}'",
            )

        # Extract created_by from token or body
        token_payload = request.get("token_payload", {})
        created_by = token_payload.get("sub", "") or body.get("created_by", "anonymous")

        task_template = body.get("task_template", {})
        if isinstance(task_template, dict):
            task_template_str = json.dumps(task_template)
        else:
            task_template_str = str(task_template)

        cron_task = CronTask(
            name=name,
            assignee=assignee,
            cron_expression=cron_expr,
            task_template=task_template_str,
            enabled=True,
            created_by=created_by,
            tenant=body.get("tenant"),
        )

        cron_id = self._cron_store.create_cron_task(cron_task)

        created = self._cron_store.get_cron_task(cron_id)
        return web.json_response(
            created.to_dict() if created else {"id": cron_id},
            status=201,
        )

    # ----------------------------------------------------------
    # GET /v2/cron — List
    # ----------------------------------------------------------

    async def handle_list(self, request: web.Request) -> web.Response:
        """GET /v2/cron — list all cron tasks."""
        enabled_only = request.query.get("enabled", "").lower() in ("1", "true", "yes")
        tasks = self._cron_store.list_cron_tasks(enabled_only=enabled_only)
        return web.json_response({
            "total": len(tasks),
            "cron_tasks": [t.to_dict() for t in tasks],
        })

    # ----------------------------------------------------------
    # DELETE /v2/cron/{id}
    # ----------------------------------------------------------

    async def handle_delete(self, request: web.Request) -> web.Response:
        """DELETE /v2/cron/{id} — delete a cron task and its executions."""
        cron_id = request.match_info["id"]
        deleted = self._cron_store.delete_cron_task(cron_id)
        if not deleted:
            return _json_error(404, "not_found", f"Cron task '{cron_id}' not found")
        return web.json_response({"message": "Cron task deleted", "id": cron_id})

    # ----------------------------------------------------------
    # PATCH /v2/cron/{id} — Update (enable/disable)
    # ----------------------------------------------------------

    async def handle_update(self, request: web.Request) -> web.Response:
        """PATCH /v2/cron/{id} — update cron task (enable/disable)."""
        cron_id = request.match_info["id"]
        existing = self._cron_store.get_cron_task(cron_id)
        if existing is None:
            return _json_error(404, "not_found", f"Cron task '{cron_id}' not found")

        try:
            body = await request.json()
        except json.JSONDecodeError:
            return _json_error(400, "invalid_json", "Invalid JSON body")

        if "enabled" in body:
            enabled = bool(body["enabled"])
            self._cron_store.set_enabled(cron_id, enabled)

        updated = self._cron_store.get_cron_task(cron_id)
        return web.json_response(updated.to_dict() if updated else {"id": cron_id})

    # ----------------------------------------------------------
    # GET /v2/cron/{id}/executions
    # ----------------------------------------------------------

    async def handle_executions(self, request: web.Request) -> web.Response:
        """GET /v2/cron/{id}/executions — execution history."""
        cron_id = request.match_info["id"]
        existing = self._cron_store.get_cron_task(cron_id)
        if existing is None:
            return _json_error(404, "not_found", f"Cron task '{cron_id}' not found")

        try:
            limit = min(int(request.query.get("limit", 50)), 200)
        except (ValueError, TypeError):
            limit = 50
        try:
            offset = max(int(request.query.get("offset", 0)), 0)
        except (ValueError, TypeError):
            offset = 0

        execs = self._cron_store.list_executions(
            cron_id, limit=limit, offset=offset,
        )
        return web.json_response({
            "total": len(execs),
            "limit": limit,
            "offset": offset,
            "executions": [e.to_dict() for e in execs],
        })


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------


def register_cron_routes(app: web.Application, handler: CronHandler) -> None:
    """Register all /v2/cron routes on *app*."""
    # POST /v2/cron — create cron task (task:write)
    app.router.add_post(
        "/v2/cron",
        require_scope("task:write")(handler.handle_create),
    )
    # GET /v2/cron — list cron tasks (task:read)
    app.router.add_get(
        "/v2/cron",
        require_scope("task:read")(handler.handle_list),
    )
    # DELETE /v2/cron/{id} — delete cron task (task:write)
    app.router.add_delete(
        "/v2/cron/{id}",
        require_scope("task:write")(handler.handle_delete),
    )
    # PATCH /v2/cron/{id} — update cron task (task:write)
    app.router.add_patch(
        "/v2/cron/{id}",
        require_scope("task:write")(handler.handle_update),
    )
    # GET /v2/cron/{id}/executions — execution history (task:read)
    app.router.add_get(
        "/v2/cron/{id}/executions",
        require_scope("task:read")(handler.handle_executions),
    )