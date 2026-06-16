"""Shared Workspace REST API routes — multi-agent workspace management.

Endpoints
---------
POST   /v2/workspaces              — create shared workspace
GET    /v2/workspaces               — list accessible workspaces
GET    /v2/workspaces/{id}          — get workspace detail
DELETE /v2/workspaces/{id}          — delete workspace (creator only)
POST   /v2/workspaces/{id}/join     — join workspace
POST   /v2/workspaces/{id}/leave    — leave workspace
POST   /v2/workspaces/{id}/lock     — acquire file lock
POST   /v2/workspaces/{id}/unlock   — release file lock
GET    /v2/workspaces/{id}/files    — list tracked files
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from aiohttp import web

from simple_a2a_registry.auth import require_scope
from simple_a2a_registry.orchestration.shared_workspace import (
    SharedWorkspaceManager,
    WorkspaceNotFoundError,
    LockError,
    MemberError,
    SharedWorkspaceError,
)

logger = logging.getLogger("a2a_registry.orchestration.shared_workspace_routes")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _json_error(status: int, error_code: str, detail: str) -> web.Response:
    return web.json_response(
        {"error": error_code, "detail": detail},
        status=status,
    )


# ---------------------------------------------------------------------------
# SharedWorkspaceHandler
# ---------------------------------------------------------------------------


class SharedWorkspaceHandler:
    """HTTP handler for the Shared Workspace REST API.

    Args:
        ws_manager: The :class:`SharedWorkspaceManager` instance.
    """

    def __init__(self, ws_manager: SharedWorkspaceManager) -> None:
        self.ws_manager = ws_manager

    # ------------------------------------------------------------------
    # POST /v2/workspaces — Create shared workspace
    # ------------------------------------------------------------------

    async def handle_create(self, request: web.Request) -> web.Response:
        """POST /v2/workspaces — create a new shared workspace.

        Body:
            name (str, required):  Workspace name.
            members (list[str], optional): Initial member agent IDs.
            path (str, optional): Explicit filesystem path.
        """
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return _json_error(400, "invalid_json", "Invalid JSON body")

        name = (body.get("name") or "").strip()
        if not name:
            return _json_error(
                400, "validation_error", "Missing required 'name' field"
            )

        created_by = request.get("agent_id", "anonymous")
        tenant = request.get("tenant", "")
        members: list[str] = body.get("members", [])
        path: Optional[str] = body.get("path")

        try:
            record = self.ws_manager.create(
                name=name,
                created_by=created_by,
                tenant=tenant,
                members=members,
                path=path,
            )
        except SharedWorkspaceError as e:
            return _json_error(400, "workspace_error", str(e))
        except OSError as e:
            return _json_error(500, "filesystem_error", str(e))

        return web.json_response({"workspace": record}, status=201)

    # ------------------------------------------------------------------
    # GET /v2/workspaces — List accessible workspaces
    # ------------------------------------------------------------------

    async def handle_list(self, request: web.Request) -> web.Response:
        """GET /v2/workspaces — list workspaces the caller has access to.

        Query params:
            agent_id (str, optional): Filter by membership.
            tenant (str, optional): Filter by tenant.
        """
        agent_id = request.query.get("agent_id") or request.get("agent_id")
        qp_tenant = request.query.get("tenant")
        auth_tenant = request.get("tenant")

        # Auth tenant is authoritative; query param only applies when
        # auth tenant is empty (admin scope / backwards compat).
        if qp_tenant is not None and not auth_tenant:
            tenant_filter = qp_tenant
        else:
            tenant_filter = auth_tenant or None

        try:
            records = self.ws_manager.list_accessible(
                agent_id=agent_id,
                tenant=tenant_filter,
            )
        except SharedWorkspaceError as e:
            return _json_error(400, "workspace_error", str(e))

        return web.json_response({
            "total": len(records),
            "workspaces": records,
        })

    # ------------------------------------------------------------------
    # GET /v2/workspaces/{id} — Get workspace detail
    # ------------------------------------------------------------------

    async def handle_get(self, request: web.Request) -> web.Response:
        """GET /v2/workspaces/{id} — get full workspace detail."""
        ws_id = request.match_info["id"]
        try:
            record = self.ws_manager.get(ws_id)
        except SharedWorkspaceError as e:
            return _json_error(400, "workspace_error", str(e))

        if record is None:
            return _json_error(
                404, "workspace_not_found",
                f"Workspace '{ws_id}' not found",
            )
        return web.json_response({"workspace": record})

    # ------------------------------------------------------------------
    # DELETE /v2/workspaces/{id} — Delete workspace
    # ------------------------------------------------------------------

    async def handle_delete(self, request: web.Request) -> web.Response:
        """DELETE /v2/workspaces/{id} — delete, creator only."""
        ws_id = request.match_info["id"]
        caller = request.get("agent_id", "")

        try:
            deleted = self.ws_manager.delete(ws_id, caller=caller)
        except SharedWorkspaceError as e:
            return _json_error(403, "forbidden", str(e))

        if not deleted:
            return _json_error(
                404, "workspace_not_found",
                f"Workspace '{ws_id}' not found",
            )
        return web.json_response({
            "message": "Workspace deleted",
            "id": ws_id,
        })

    # ------------------------------------------------------------------
    # POST /v2/workspaces/{id}/join — Join workspace
    # ------------------------------------------------------------------

    async def handle_join(self, request: web.Request) -> web.Response:
        """POST /v2/workspaces/{id}/join — add caller as member."""
        ws_id = request.match_info["id"]
        agent_id = request.get("agent_id", "")
        if not agent_id:
            return _json_error(
                401, "authentication_error",
                "Agent identity required to join workspace",
            )

        try:
            record = self.ws_manager.join(ws_id, agent_id)
        except WorkspaceNotFoundError as e:
            return _json_error(404, "workspace_not_found", str(e))
        except SharedWorkspaceError as e:
            return _json_error(400, "workspace_error", str(e))

        return web.json_response({"workspace": record})

    # ------------------------------------------------------------------
    # POST /v2/workspaces/{id}/leave — Leave workspace
    # ------------------------------------------------------------------

    async def handle_leave(self, request: web.Request) -> web.Response:
        """POST /v2/workspaces/{id}/leave — remove caller from workspace."""
        ws_id = request.match_info["id"]
        agent_id = request.get("agent_id", "")
        if not agent_id:
            return _json_error(
                401, "authentication_error",
                "Agent identity required to leave workspace",
            )

        try:
            record = self.ws_manager.leave(ws_id, agent_id)
        except WorkspaceNotFoundError as e:
            return _json_error(404, "workspace_not_found", str(e))
        except SharedWorkspaceError as e:
            return _json_error(400, "workspace_error", str(e))

        return web.json_response({"workspace": record})

    # ------------------------------------------------------------------
    # POST /v2/workspaces/{id}/lock — Acquire file lock
    # ------------------------------------------------------------------

    async def handle_lock(self, request: web.Request) -> web.Response:
        """POST /v2/workspaces/{id}/lock — acquire exclusive file lock.

        Body:
            file_path (str, required): Relative path within workspace.
            ttl (int, optional): Lock TTL in seconds (default 300).
        """
        ws_id = request.match_info["id"]

        try:
            body = await request.json()
        except json.JSONDecodeError:
            return _json_error(400, "invalid_json", "Invalid JSON body")

        file_path = (body.get("file_path") or "").strip()
        if not file_path:
            return _json_error(
                400, "validation_error",
                "Missing required 'file_path' field",
            )

        agent_id = request.get("agent_id", "anonymous")
        ttl: Optional[int] = body.get("ttl")

        try:
            record = self.ws_manager.lock(
                ws_id, file_path, agent_id, ttl=ttl,
            )
        except WorkspaceNotFoundError as e:
            return _json_error(404, "workspace_not_found", str(e))
        except MemberError as e:
            return _json_error(403, "not_a_member", str(e))
        except LockError as e:
            return _json_error(409, "lock_conflict", str(e))
        except SharedWorkspaceError as e:
            return _json_error(400, "workspace_error", str(e))

        return web.json_response({"file": record})

    # ------------------------------------------------------------------
    # POST /v2/workspaces/{id}/unlock — Release file lock
    # ------------------------------------------------------------------

    async def handle_unlock(self, request: web.Request) -> web.Response:
        """POST /v2/workspaces/{id}/unlock — release a file lock.

        Body:
            file_path (str, required): Relative path within workspace.
        """
        ws_id = request.match_info["id"]

        try:
            body = await request.json()
        except json.JSONDecodeError:
            return _json_error(400, "invalid_json", "Invalid JSON body")

        file_path = (body.get("file_path") or "").strip()
        if not file_path:
            return _json_error(
                400, "validation_error",
                "Missing required 'file_path' field",
            )

        agent_id = request.get("agent_id", "anonymous")

        try:
            record = self.ws_manager.unlock(ws_id, file_path, agent_id)
        except WorkspaceNotFoundError as e:
            return _json_error(404, "workspace_not_found", str(e))
        except LockError as e:
            return _json_error(409, "lock_conflict", str(e))
        except SharedWorkspaceError as e:
            return _json_error(400, "workspace_error", str(e))

        if record is None:
            return _json_error(
                404, "file_not_found",
                f"File '{file_path}' is not tracked in workspace '{ws_id}'",
            )
        return web.json_response({"file": record})

    # ------------------------------------------------------------------
    # GET /v2/workspaces/{id}/files — List tracked files
    # ------------------------------------------------------------------

    async def handle_list_files(self, request: web.Request) -> web.Response:
        """GET /v2/workspaces/{id}/files — list tracked files with lock status."""
        ws_id = request.match_info["id"]

        try:
            files = self.ws_manager.list_files(ws_id)
        except SharedWorkspaceError as e:
            return _json_error(400, "workspace_error", str(e))

        return web.json_response({
            "total": len(files),
            "files": files,
        })


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------


def register_shared_workspace_routes(
    app: web.Application,
    handler: SharedWorkspaceHandler,
) -> None:
    """Register all shared workspace routes on *app*."""
    # POST /v2/workspaces — create (workspace:write)
    app.router.add_post(
        "/v2/workspaces",
        require_scope("workspace:write")(handler.handle_create),
    )
    # GET /v2/workspaces — list (workspace:read)
    app.router.add_get(
        "/v2/workspaces",
        require_scope("workspace:read")(handler.handle_list),
    )
    # GET /v2/workspaces/{id} — detail (workspace:read)
    app.router.add_get(
        "/v2/workspaces/{id}",
        require_scope("workspace:read")(handler.handle_get),
    )
    # DELETE /v2/workspaces/{id} — delete (workspace:write)
    app.router.add_delete(
        "/v2/workspaces/{id}",
        require_scope("workspace:write")(handler.handle_delete),
    )
    # POST /v2/workspaces/{id}/join — join (workspace:write)
    app.router.add_post(
        "/v2/workspaces/{id}/join",
        require_scope("workspace:write")(handler.handle_join),
    )
    # POST /v2/workspaces/{id}/leave — leave (workspace:write)
    app.router.add_post(
        "/v2/workspaces/{id}/leave",
        require_scope("workspace:write")(handler.handle_leave),
    )
    # POST /v2/workspaces/{id}/lock — lock (workspace:write)
    app.router.add_post(
        "/v2/workspaces/{id}/lock",
        require_scope("workspace:write")(handler.handle_lock),
    )
    # POST /v2/workspaces/{id}/unlock — unlock (workspace:write)
    app.router.add_post(
        "/v2/workspaces/{id}/unlock",
        require_scope("workspace:write")(handler.handle_unlock),
    )
    # GET /v2/workspaces/{id}/files — list files (workspace:read)
    app.router.add_get(
        "/v2/workspaces/{id}/files",
        require_scope("workspace:read")(handler.handle_list_files),
    )