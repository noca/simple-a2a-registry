"""Swarm REST API routes — create, query, blackboard read/write.

Endpoints
---------
POST   /v2/swarm                     — create Swarm topology
GET    /v2/swarm/{root_id}           — get Swarm topology status
POST   /v2/swarm/{root_id}/comment   — write to Swarm blackboard
GET    /v2/swarm/{root_id}/blackboard — read Swarm blackboard
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from aiohttp import web

from simple_a2a_registry.auth import require_scope
from simple_a2a_registry.orchestration.swarm import (
    SwarmWorkerSpec,
    create_swarm,
    get_swarm_status,
    post_blackboard,
    read_blackboard,
)
from simple_a2a_registry.orchestration.store import TaskStore

logger = logging.getLogger("a2a_registry.orchestration.swarm_routes")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _json_error(status: int, error_code: str, detail: str) -> web.Response:
    return web.json_response(
        {"error": error_code, "detail": detail},
        status=status,
    )


# ---------------------------------------------------------------------------
# SwarmHandler
# ---------------------------------------------------------------------------


class SwarmHandler:
    """HTTP handler for the Swarm REST API."""

    def __init__(self, store: TaskStore) -> None:
        self.store = store
        # Callback for broadcasting events to Admin UI WebSocket clients.
        # Wired up by create_app in server.py.
        self._broadcast_fn = None  # async callable(event_type: str, data: dict)

    # ------------------------------------------------------------------
    # POST /v2/swarm — Create Swarm topology
    # ------------------------------------------------------------------

    async def handle_create_swarm(self, request: web.Request) -> web.Response:
        """POST /v2/swarm — create a full Swarm topology."""
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return _json_error(400, "invalid_json", "Invalid JSON body")

        goal = (body.get("goal") or "").strip()
        if not goal:
            return _json_error(
                400, "validation_error", "Missing required 'goal' field"
            )

        workers_raw = body.get("workers", [])
        if not isinstance(workers_raw, list) or not workers_raw:
            return _json_error(
                400, "validation_error", "At least one 'workers' entry is required"
            )

        verifier_raw = body.get("verifier", {})
        synthesizer_raw = body.get("synthesizer", {})

        verifier_profile = (verifier_raw.get("profile") or "").strip()
        if not verifier_profile:
            return _json_error(
                400, "validation_error",
                "Verifier must have a 'profile' field",
            )

        synthesizer_profile = (synthesizer_raw.get("profile") or "").strip()
        if not synthesizer_profile:
            return _json_error(
                400, "validation_error",
                "Synthesizer must have a 'profile' field",
            )

        # Parse worker specs.
        workers: list[SwarmWorkerSpec] = []
        for i, w in enumerate(workers_raw):
            if not isinstance(w, dict):
                return _json_error(
                    400, "validation_error",
                    f"Worker at index {i}: must be a JSON object",
                )
            profile = (w.get("profile") or "").strip()
            title = (w.get("title") or "").strip()
            if not profile:
                return _json_error(
                    400, "validation_error",
                    f"Worker at index {i}: missing required 'profile'",
                )
            if not title:
                return _json_error(
                    400, "validation_error",
                    f"Worker at index {i}: missing required 'title'",
                )
            workers.append(SwarmWorkerSpec(
                profile=profile,
                title=title,
                body=(w.get("body") or "").strip(),
                skills=w.get("skills", []),
                priority=w.get("priority", 0),
                max_runtime_seconds=w.get("max_runtime_seconds"),
            ))

        root_title = (body.get("root_title") or "").strip() or None
        verifier_title = (verifier_raw.get("title") or "").strip() or "Verify swarm outputs"
        synthesizer_title = (synthesizer_raw.get("title") or "").strip() or "Synthesize swarm outputs"
        priority = body.get("priority", 0)
        tenant = body.get("tenant")

        try:
            result = create_swarm(
                self.store,
                goal=goal,
                workers=workers,
                verifier_profile=verifier_profile,
                synthesizer_profile=synthesizer_profile,
                root_title=root_title,
                verifier_title=verifier_title,
                synthesizer_title=synthesizer_title,
                tenant=tenant,
                priority=priority,
            )
        except ValueError as e:
            return _json_error(400, "validation_error", str(e))

        # Build response with detailed topology.
        def _task_brief(tid: str) -> Optional[dict]:
            t = self.store.get_task(tid)
            if t is None:
                return None
            return {
                "id": t.id,
                "status": t.status,
                "assignee": t.assignee,
            }

        root = _task_brief(result.root_id)
        workers_detail = [
            _task_brief(wid) for wid in result.worker_ids
            if _task_brief(wid) is not None
        ]
        verifier_detail = _task_brief(result.verifier_id)
        synthesizer_detail = _task_brief(result.synthesizer_id)

        return web.json_response({
            "swarm": {
                "root_id": result.root_id,
                "worker_ids": result.worker_ids,
                "verifier_id": result.verifier_id,
                "synthesizer_id": result.synthesizer_id,
            },
            "topology": {
                "root": root,
                "workers": workers_detail,
                "verifier": verifier_detail,
                "synthesizer": synthesizer_detail,
            },
        }, status=201)

    # ------------------------------------------------------------------
    # GET /v2/swarm/{root_id} — Get Swarm topology status
    # ------------------------------------------------------------------

    async def handle_get_swarm(self, request: web.Request) -> web.Response:
        """GET /v2/swarm/{root_id} — full swarm status with blackboard."""
        root_id = request.match_info["root_id"]
        status = get_swarm_status(self.store, root_id)
        if status is None:
            return _json_error(
                404, "task_not_found",
                f"Swarm root task '{root_id}' not found",
            )
        return web.json_response(status)

    # ------------------------------------------------------------------
    # POST /v2/swarm/{root_id}/comment — Write to blackboard
    # ------------------------------------------------------------------

    async def handle_comment(self, request: web.Request) -> web.Response:
        """POST /v2/swarm/{root_id}/comment — write a structured blackboard entry."""
        root_id = request.match_info["root_id"]

        # Verify root exists.
        root = self.store.get_task(root_id)
        if root is None:
            return _json_error(
                404, "task_not_found",
                f"Task '{root_id}' not found — cannot comment on non-existent root",
            )

        try:
            body = await request.json()
        except json.JSONDecodeError:
            return _json_error(400, "invalid_json", "Invalid JSON body")

        author = (body.get("author") or "anonymous").strip()
        key = body.get("key", "").strip()
        value = body.get("value")

        if not key:
            return _json_error(
                400, "validation_error",
                "Missing required 'key' field",
            )
        if value is None:
            return _json_error(
                400, "validation_error",
                "Missing required 'value' field",
            )

        try:
            comment = post_blackboard(
                self.store,
                root_id,
                author=author,
                key=key,
                value=value,
            )
        except ValueError as e:
            if "not found" in str(e).lower():
                return _json_error(404, "task_not_found", str(e))
            return _json_error(400, "validation_error", str(e))

        # Broadcast comment_added to Admin UI WebSocket clients
        if self._broadcast_fn:
            root_data = self.store.get_task(root_id)
            if root_data:
                await self._broadcast_fn("comment_added", {
                    "id": root_id,
                    "key": key,
                    "value": value,
                    "author": author,
                    "comment_id": comment.id,
                    "created_at": comment.created_at,
                })

        return web.json_response({
            "comment_id": comment.id,
            "key": key,
            "created_at": comment.created_at,
        }, status=201)

    # ------------------------------------------------------------------
    # GET /v2/swarm/{root_id}/blackboard — Read blackboard
    # ------------------------------------------------------------------

    async def handle_read_blackboard(self, request: web.Request) -> web.Response:
        """GET /v2/swarm/{root_id}/blackboard — read aggregated blackboard."""
        root_id = request.match_info["root_id"]

        # Verify root exists.
        root = self.store.get_task(root_id)
        if root is None:
            return _json_error(
                404, "task_not_found",
                f"Task '{root_id}' not found",
            )

        blackboard = read_blackboard(self.store, root_id)
        return web.json_response(blackboard)


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------


def register_swarm_routes(
    app: web.Application,
    handler: SwarmHandler,
) -> None:
    """Register all Swarm routes on *app*."""
    # POST /v2/swarm — create swarm (task:write)
    app.router.add_post(
        "/v2/swarm",
        require_scope("task:write")(handler.handle_create_swarm),
    )
    # GET /v2/swarm/{root_id} — swarm status (task:read)
    app.router.add_get(
        "/v2/swarm/{root_id}",
        require_scope("task:read")(handler.handle_get_swarm),
    )
    # POST /v2/swarm/{root_id}/comment — blackboard write (task:write)
    app.router.add_post(
        "/v2/swarm/{root_id}/comment",
        require_scope("task:write")(handler.handle_comment),
    )
    # GET /v2/swarm/{root_id}/blackboard — blackboard read (task:read)
    app.router.add_get(
        "/v2/swarm/{root_id}/blackboard",
        require_scope("task:read")(handler.handle_read_blackboard),
    )