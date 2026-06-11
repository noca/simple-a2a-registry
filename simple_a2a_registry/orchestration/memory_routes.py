"""V2 Memory REST API routes — persistent agent memory CRUD.

Endpoints
---------
GET    /v2/memory/{agent_id}          — list memory entries (query: ?namespace=&prefix=)
POST   /v2/memory/{agent_id}          — write a memory entry {key, value, ttl?, namespace?}
DELETE /v2/memory/{agent_id}/{key}    — delete a specific key
GET    /v2/memory/{agent_id}/search?q= — search memory by value content

Scopes: memory:read (GET), memory:write (POST/DELETE)
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from aiohttp import web

from simple_a2a_registry.auth import require_scope
from simple_a2a_registry.orchestration.memory import (
    AgentMemoryStore,
    NAMESPACE_PERSONAL,
    NAMESPACE_SHARED,
    NAMESPACE_GLOBAL,
    VALID_NAMESPACES,
)

logger = logging.getLogger("a2a_registry.memory.routes")


def _json_error(status: int, error_code: str, detail: str) -> web.Response:
    return web.json_response(
        {"error": error_code, "detail": detail},
        status=status,
    )


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


class MemoryHandler:
    """HTTP handler for the V2 Memory REST API.

    All endpoints require either ``memory:read`` (GET) or ``memory:write``
    (POST/DELETE) scope.  The requesting agent is identified via the JWT
    token stored by the auth middleware (``request["agent_id"]``).
    """

    def __init__(self, memory_store: AgentMemoryStore) -> None:
        self.memory = memory_store

    @staticmethod
    def _resolve_agent_id(request: web.Request, agent_id: str) -> Optional[str]:
        """Resolve the effective agent performing the request.

        If the caller has ``registry:admin`` scope, they can act on any
        agent_id.  Otherwise, the caller is restricted to their own
        ``request["agent_id"]`` from the JWT.

        Returns the effective agent_id, or ``None`` if the caller is not
        authorised to act on the requested agent.
        """
        caller_agent = request.get("agent_id", "")
        caller_scopes = request.get("token_scopes", "")

        # Admin can act on any agent
        if "registry:admin" in caller_scopes:
            return agent_id

        # Non-admin: must match their own agent_id
        if caller_agent == agent_id:
            return agent_id

        return None

    async def handle_list(self, request: web.Request) -> web.Response:
        """GET /v2/memory/{agent_id} — list memory entries.

        Query params:
            namespace (str): Filter by namespace (personal, shared, global).
            prefix (str):    Only return keys starting with this prefix.
            limit (int):     Max results (default 100, max 1000).
            offset (int):    Pagination offset.
        """
        agent_id = request.match_info["agent_id"]
        effective = self._resolve_agent_id(request, agent_id)
        if effective is None:
            return _json_error(403, "forbidden", "Not authorised for this agent")

        namespace = request.query.get("namespace")
        prefix = request.query.get("prefix", "")
        try:
            limit = min(int(request.query.get("limit", 100)), 1000)
        except (ValueError, TypeError):
            limit = 100
        try:
            offset_val = max(int(request.query.get("offset", 0)), 0)
        except (ValueError, TypeError):
            offset_val = 0

        entries = self.memory.list_keys(
            effective,
            prefix=prefix,
            namespace=namespace,
            limit=limit,
            offset=offset_val,
        )

        return web.json_response({
            "agent_id": effective,
            "namespace": namespace or "all",
            "prefix": prefix,
            "limit": limit,
            "offset": offset_val,
            "total": len(entries),
            "entries": [e.to_dict() for e in entries],
        })

    async def handle_get(self, request: web.Request) -> web.Response:
        """GET /v2/memory/{agent_id}/{key} — get a single memory entry.

        Query params:
            namespace (str): Namespace (default: personal).
        """
        agent_id = request.match_info["agent_id"]
        key = request.match_info["key"]
        effective = self._resolve_agent_id(request, agent_id)
        if effective is None:
            return _json_error(403, "forbidden", "Not authorised for this agent")

        namespace = request.query.get("namespace", NAMESPACE_PERSONAL)

        entry = self.memory.get(key, effective, namespace=namespace)
        if entry is None:
            return _json_error(404, "not_found", f"Key '{key}' not found in namespace '{namespace}'")

        return web.json_response(entry.to_dict())

    async def handle_set(self, request: web.Request) -> web.Response:
        """POST /v2/memory/{agent_id} — write a memory entry.

        Body: {key: str, value: any, ttl?: int (seconds), namespace?: str}
        """
        agent_id = request.match_info["agent_id"]
        effective = self._resolve_agent_id(request, agent_id)
        if effective is None:
            return _json_error(403, "forbidden", "Not authorised for this agent")

        try:
            body = await request.json()
        except json.JSONDecodeError:
            return _json_error(400, "bad_request", "Invalid JSON body")

        key = body.get("key")
        if not key or not isinstance(key, str):
            return _json_error(400, "bad_request", "Missing or invalid 'key' (must be a non-empty string)")

        value = body.get("value")
        ttl = body.get("ttl", 0)
        namespace = body.get("namespace", NAMESPACE_PERSONAL)

        if namespace not in VALID_NAMESPACES:
            return _json_error(
                400, "bad_request",
                f"Invalid namespace '{namespace}'. Valid: {', '.join(sorted(VALID_NAMESPACES))}",
            )

        try:
            ttl = int(ttl)
        except (ValueError, TypeError):
            ttl = 0

        if ttl < 0:
            ttl = 0

        entry = self.memory.set(
            key, value, effective,
            ttl=ttl,
            namespace=namespace,
        )

        return web.json_response(entry.to_dict(), status=201)

    async def handle_delete(self, request: web.Request) -> web.Response:
        """DELETE /v2/memory/{agent_id}/{key} — delete a memory entry.

        Query params:
            namespace (str): Namespace (default: personal).
        """
        agent_id = request.match_info["agent_id"]
        key = request.match_info["key"]
        effective = self._resolve_agent_id(request, agent_id)
        if effective is None:
            return _json_error(403, "forbidden", "Not authorised for this agent")

        namespace = request.query.get("namespace", NAMESPACE_PERSONAL)

        deleted = self.memory.delete(key, effective, namespace=namespace)
        if not deleted:
            return _json_error(404, "not_found", f"Key '{key}' not found in namespace '{namespace}'")

        return web.json_response({"deleted": True, "key": key, "namespace": namespace})

    async def handle_search(self, request: web.Request) -> web.Response:
        """GET /v2/memory/{agent_id}/search?q=... — search memory by value content.

        Query params:
            q (str):       REQUIRED — substring to search in value.
            namespace (str): Optional namespace filter.
            limit (int):   Max results (default 100, max 1000).
            offset (int):  Pagination offset.
        """
        agent_id = request.match_info["agent_id"]
        effective = self._resolve_agent_id(request, agent_id)
        if effective is None:
            return _json_error(403, "forbidden", "Not authorised for this agent")

        query = request.query.get("q", "").strip()
        if not query:
            return _json_error(400, "bad_request", "Missing 'q' query parameter")

        namespace = request.query.get("namespace")
        try:
            limit = min(int(request.query.get("limit", 100)), 1000)
        except (ValueError, TypeError):
            limit = 100
        try:
            offset_val = max(int(request.query.get("offset", 0)), 0)
        except (ValueError, TypeError):
            offset_val = 0

        entries = self.memory.search(
            query, effective,
            namespace=namespace,
            limit=limit,
            offset=offset_val,
        )

        return web.json_response({
            "agent_id": effective,
            "query": query,
            "namespace": namespace or "all",
            "limit": limit,
            "offset": offset_val,
            "total": len(entries),
            "entries": [e.to_dict() for e in entries],
        })


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------


def register_memory_routes(app: web.Application, handler: MemoryHandler) -> None:
    """Register all V2 memory routes on *app*."""
    # GET /v2/memory/{agent_id} — list (memory:read)
    app.router.add_get(
        "/v2/memory/{agent_id}",
        require_scope("memory:read")(handler.handle_list),
    )
    # GET /v2/memory/{agent_id}/search — search (memory:read)
    app.router.add_get(
        "/v2/memory/{agent_id}/search",
        require_scope("memory:read")(handler.handle_search),
    )
    # GET /v2/memory/{agent_id}/{key} — get single entry (memory:read)
    app.router.add_get(
        "/v2/memory/{agent_id}/{key}",
        require_scope("memory:read")(handler.handle_get),
    )
    # POST /v2/memory/{agent_id} — set entry (memory:write)
    app.router.add_post(
        "/v2/memory/{agent_id}",
        require_scope("memory:write")(handler.handle_set),
    )
    # DELETE /v2/memory/{agent_id}/{key} — delete entry (memory:write)
    app.router.add_delete(
        "/v2/memory/{agent_id}/{key}",
        require_scope("memory:write")(handler.handle_delete),
    )