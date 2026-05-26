"""A2A Registry HTTP server — aiohttp-based REST API with WebSocket dispatch."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from aiohttp import web, WSMsgType

from simple_a2a_registry.models import make_agent_card, make_agent_skill
from simple_a2a_registry.store import A2ARegistryStore, HEARTBEAT_TIMEOUT
from simple_a2a_registry.auth import (
    AuthStore,
    AuthHandler,
    _generate_rsa_keypair,
    _auth_middleware_factory,
    make_jwks_endpoint,
    verify_token,
    require_scope,
    create_token,
    ISSUER,
    SCOPES,
)
from simple_a2a_registry.orchestration import (
    TaskStore,
    TaskStatus,
    OrchestrationHandler,
    register_v2_routes,
    Dispatcher,
    DispatcherConfig,
    WorkspaceManager,
)

logger = logging.getLogger("a2a_registry.server")

REGISTRY_VERSION = "1.0.0"
REGISTRY_AGENT_ID = "simple-a2a-registry"
REGISTRY_AGENT_NAME = "Simple A2A Registry"
REGISTRY_AGENT_DESCRIPTION = (
    "A lightweight Agent-to-Agent registry"
)

CLEANUP_INTERVAL = 60  # seconds between stale-agent purges


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _json_error(status: int, error_code: str, detail: str) -> web.Response:
    return web.json_response(
        {"error": error_code, "detail": detail},
        status=status,
    )


def _registry_card(base_url: str) -> Dict:
    """Build a v1.0 Agent Card for the Registry itself.

    Returns a v1.0-style card with ``supported_interfaces`` and top-level
    ``skills`` (not nested in ``capabilities.skills``).
    """
    return make_agent_card(
        name=REGISTRY_AGENT_NAME,
        description=REGISTRY_AGENT_DESCRIPTION,
        url=f"{base_url}/",
        skills=[
            make_agent_skill("register", "Register Agent", "Register a new agent"),
            make_agent_skill("discover", "Discover Agents", "List/search agents"),
            make_agent_skill("heartbeat", "Heartbeat", "Send keep-alive for an agent"),
            make_agent_skill(
                "dispatch", "Dispatch Task",
                "Dispatch a task to an agent via persistent connection",
            ),
        ],
    )


# ---------------------------------------------------------------------------
# RegistryHandler
# ---------------------------------------------------------------------------


class RegistryHandler:
    """HTTP + WebSocket handler methods for the A2A Registry."""

    def __init__(self, store: A2ARegistryStore, base_url: str,
                 auth_store: Optional[AuthStore] = None) -> None:
        self.store = store
        self.base_url = base_url.rstrip("/")
        self._started_at = time.time()

        # WebSocket connections: agent_id -> WebSocketResponse
        self._ws_connections: Dict[str, web.WebSocketResponse] = {}

        # Task store: task_id -> {"state":..., "query":..., "result":..., ...}
        self._tasks: Dict[str, Dict[str, Any]] = {}

        # Kanban integration (wired up by create_app)
        self.task_store: Optional[TaskStore] = None
        self._dispatched_ws_tasks: Optional[Dict[str, str]] = None

        # OAuth 2.1 Auth store for admin / WebSocket auth
        self.auth_store = auth_store

        # Auth state (wired up by create_app)
        self._auth_public_key: str = ""
        self._auth_algorithm: str = "HS256"
        self._auth_enabled: bool = False

    # ------------------------------------------------------------------
    # Health / meta
    # ------------------------------------------------------------------

    async def handle_health(self, request: web.Request) -> web.Response:
        """GET /health"""
        s = self.store.stats()
        return web.json_response({
            "status": "healthy",
            "version": REGISTRY_VERSION,
            "uptime_seconds": round(time.time() - self._started_at, 2),
            "stats": {
                "total_agents": s["totalAgents"],
                "alive_agents": s["aliveAgents"],
                "stale_agents": s["staleAgents"],
                "connected_via_ws": len(self._ws_connections),
            },
        })

    async def handle_well_known(self, request: web.Request) -> web.Response:
        """GET /.well-known/agent-card.json"""
        card = _registry_card(self.base_url)
        card["id"] = REGISTRY_AGENT_ID
        card["version"] = REGISTRY_VERSION
        return web.json_response(card, headers={
            "Cache-Control": "public, max-age=300",
        })

    # ------------------------------------------------------------------
    # Agent CRUD
    # ------------------------------------------------------------------

    async def handle_list_agents(self, request: web.Request) -> web.Response:
        """GET /v1/agents"""
        skill = request.query.get("skill")
        tag = request.query.get("tag")
        q = request.query.get("q")
        try:
            limit = min(int(request.query.get("limit", 50)), 200)
        except (ValueError, TypeError):
            limit = 50
        try:
            offset = max(int(request.query.get("offset", 0)), 0)
        except (ValueError, TypeError):
            offset = 0

        all_agents = self.store.list_agents(skill=skill, tag=tag, q=q)
        total = len(all_agents)
        page = all_agents[offset:offset + limit]

        # Annotate with WebSocket connection info
        for agent in page:
            aid = agent.get("id", "")
            if aid in self._ws_connections:
                agent["connection"] = "websocket"
                agent["status"] = "alive"

        return web.json_response({
            "total": total,
            "limit": limit,
            "offset": offset,
            "agents": page,
        })

    async def handle_get_agent(self, request: web.Request) -> web.Response:
        """GET /v1/agents/{agent_id}"""
        agent_id = request.match_info["agent_id"]
        card = self.store.get_agent(agent_id)
        if card is None:
            return _json_error(404, "agent_not_found", f"Agent '{agent_id}' not found")
        if agent_id in self._ws_connections:
            card["connection"] = "websocket"
            card["status"] = "alive"
        return web.json_response(card)

    async def handle_register(self, request: web.Request) -> web.Response:
        """POST /v1/agents — register an agent.

        If the agent's AgentCard includes an ``OAuth2SecurityScheme`` in
        ``security_schemes``, the Registry auto-creates a client and returns
        ``client_id``/``client_secret`` in the response.

        Body::

            {
                "name": "...",
                "description": "...",
                "security_schemes": {
                    "my-oauth": {
                        "scheme_type": "oauth2",
                        "description": "OAuth 2.1 client credentials",
                        "oauth2": {
                            "flows": {
                                "client_credentials": {
                                    "token_url": "http://localhost:8321/auth/token",
                                    "scopes": {"task:read": "Read tasks"}
                                }
                            }
                        }
                    }
                },
                ...
            }
        """
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return _json_error(400, "invalid_json", "Invalid JSON body")

        name = body.get("name", "").strip()
        if not name:
            return _json_error(400, "validation_error", "Agent requires a 'name'")

        # Check for duplicate name among external agents
        existing = self.store.list_agents()
        for agent in existing:
            if agent.get("name", "").strip().lower() == name.lower():
                return _json_error(
                    409, "agent_exists", f"Agent '{name}' already exists"
                )

        agent_id = self.store.register_agent(body)
        card = self.store.get_agent(agent_id)

        response = {
            "message": "Agent registered successfully",
            "id": agent_id,
            "card": card,
        }

        return web.json_response(response, status=201)

    async def handle_unregister(self, request: web.Request) -> web.Response:
        """DELETE /v1/agents/{agent_id} — unregister an agent.

        Closes the agent's WebSocket connection and removes it from the registry.
        """
        agent_id = request.match_info["agent_id"]

        # Close WS connection if open
        ws = self._ws_connections.pop(agent_id, None)
        if ws and not ws.closed:
            try:
                await ws.send_json({"type": "close"})
                await ws.close()
            except Exception:
                pass

        success = self.store.unregister(agent_id)
        if not success:
            return _json_error(404, "agent_not_found", f"Agent '{agent_id}' not found")

        return web.json_response({
            "message": "Agent unregistered successfully",
            "id": agent_id,
        })

    # ------------------------------------------------------------------
    # Heartbeat
    # ------------------------------------------------------------------

    async def handle_heartbeat(self, request: web.Request) -> web.Response:
        """POST /v1/agents/{agent_id}/heartbeat"""
        agent_id = request.match_info["agent_id"]
        card = self.store.get_agent(agent_id)
        if card is None:
            return _json_error(404, "agent_not_found", f"Agent '{agent_id}' not found")

        if card["status"] == "stale":
            return _json_error(
                410, "agent_stale", f"Agent '{agent_id}' is stale and cannot heartbeat"
            )

        success = self.store.heartbeat(agent_id)
        if not success:
            return _json_error(404, "agent_not_found", f"Agent '{agent_id}' not found")

        now = time.time()
        return web.json_response(
            {
                "id": agent_id,
                "status": "alive",
                "last_heartbeat": now,
                "expires_at": now + HEARTBEAT_TIMEOUT,
                "stale_timeout": HEARTBEAT_TIMEOUT,
            },
            status=203,
        )

    # ------------------------------------------------------------------
    # WebSocket — persistent agent connection
    # ------------------------------------------------------------------

    async def handle_ws(self, request: web.Request) -> web.WebSocketResponse:
        """GET /v1/agents/{agent_id}/ws — WebSocket endpoint for agents.

        Agents connect here instead of (or on top of) HTTP heartbeat.
        The Registry can dispatch tasks directly to connected agents via WS.

        Authentication:
        - When auth is disabled: no token required
        - When auth is enabled: pass ``?token=xxx`` query parameter
          (WS upgrade doesn't support Bearer header from browser contexts)
        """
        agent_id = request.match_info["agent_id"]

        # Verify the agent is registered FIRST (needed for sub check below)
        card = self.store.get_agent(agent_id)
        if card is None:
            return web.json_response(
                {"error": "agent_not_found", "detail": f"Agent '{agent_id}' not found"},
                status=404,
            )

        # WebSocket token validation via query parameter
        if self._auth_enabled:
            token = request.query.get("token", "")
            if not token:
                return web.json_response(
                    {"error": "unauthorized", "detail": "WebSocket upgrade requires ?token= query parameter"},
                    status=401,
                )
            payload = verify_token(
                token,
                public_key=self._auth_public_key,
                algorithm=self._auth_algorithm,
                issuer=ISSUER,
            )
            if payload is None:
                return web.json_response(
                    {"error": "invalid_token", "detail": "Token expired or invalid"},
                    status=401,
                )
            # Verify the token's sub matches:
            #   - the agent's own ID, or
            #   - "simple-a2a-registry" (registry service account), or
            #   - an OAuth client_id whose agent_card_id matches this agent's name
            token_sub = payload.get("sub", "")
            if token_sub not in (agent_id, "simple-a2a-registry"):
                sub_ok = False
                if self.auth_store:
                    client = self.auth_store.get_client(token_sub)
                    if client and client.agent_card_id == card.get("name"):
                        sub_ok = True
                if not sub_ok:
                    return web.json_response(
                        {"error": "forbidden", "detail":
                         f"Token subject '{token_sub}' does not match agent '{agent_id}'"},
                        status=403,
                    )

        ws = web.WebSocketResponse(max_msg_size=0)  # no size limit
        await ws.prepare(request)

        # Replace any existing connection for this agent
        old = self._ws_connections.pop(agent_id, None)
        if old and not old.closed:
            try:
                await old.send_json({"type": "close", "reason": "replaced"})
                await old.close()
            except Exception:
                pass

        self._ws_connections[agent_id] = ws
        logger.info("Agent '%s' connected via WebSocket (%d active)",
                     agent_id, len(self._ws_connections))

        # Mark alive in store
        self.store.heartbeat(agent_id)

        # On reconnection, dispatch any pending tasks blocked for this agent
        await _maybe_dispatch_pending(self.task_store, ws, agent_id, self._dispatched_ws_tasks)

        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                    except json.JSONDecodeError:
                        await ws.send_json({
                            "type": "error",
                            "detail": "Invalid JSON",
                        })
                        continue

                    msg_type = data.get("type", "")

                    if msg_type == "ping":
                        await ws.send_json({"type": "pong"})

                    elif msg_type == "task_result":
                        # Agent reports task completion
                        task_id = data.get("id", "")
                        task = self._tasks.get(task_id)
                        if task:
                            task["state"] = data.get("status", "completed")
                            task["result"] = data.get("result", {})
                            task["error"] = data.get("error")
                            task["updated_at"] = time.time()
                            logger.info("Task %s completed by agent '%s': %s",
                                        task_id, agent_id, task["state"])
                        else:
                            # Auto-create task entry for externally reported results
                            now = time.time()
                            self._tasks[task_id] = {
                                "id": task_id,
                                "agent_id": agent_id,
                                "state": data.get("status", "completed"),
                                "result": data.get("result", {}),
                                "error": data.get("error"),
                                "created_at": now,
                                "updated_at": now,
                            }
                            logger.info("Task %s result received (auto-created) from agent '%s'",
                                        task_id, agent_id)

                        # Reconcile with kanban TaskStore if this is a WS-dispatched kanban task
                        _maybe_update_kanban(
                            self.task_store,
                            self._dispatched_ws_tasks,
                            task_id,
                            data.get("status", "completed"),
                            data.get("result"),
                            data.get("error"),
                        )

                    elif msg_type == "task_progress":
                        task_id = data.get("id", "")
                        task = self._tasks.get(task_id)
                        if task:
                            task["state"] = data.get("status", "working")
                            task["updated_at"] = time.time()
                        else:
                            # Auto-create task entry for externally reported progress
                            now = time.time()
                            self._tasks[task_id] = {
                                "id": task_id,
                                "agent_id": agent_id,
                                "state": data.get("status", "working"),
                                "result": None,
                                "error": None,
                                "created_at": now,
                                "updated_at": now,
                            }
                            logger.info("Task %s progress received (auto-created) from agent '%s'",
                                        task_id, agent_id)

                    elif msg_type == "close":
                        logger.info("Agent '%s' closing WebSocket", agent_id)
                        break

                    else:
                        logger.debug("Unknown WS message from %s: %s",
                                     agent_id, msg_type)

                elif msg.type == WSMsgType.ERROR:
                    logger.error("WS error for agent '%s': %s",
                                 agent_id, ws.exception())

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error("WS handler error for agent '%s': %s", agent_id, e)
        finally:
            if self._ws_connections.get(agent_id) is ws:
                self._ws_connections.pop(agent_id, None)
            if not ws.closed:
                await ws.close()
            logger.info("Agent '%s' disconnected via WebSocket (%d active)",
                         agent_id, len(self._ws_connections))

        return ws

    # ------------------------------------------------------------------
    # Task dispatch — client → Registry → Agent (via WS)
    # ------------------------------------------------------------------

    async def handle_dispatch(self, request: web.Request) -> web.Response:
        """POST /v1/agents/{agent_id}/dispatch — submit a task to an agent.

        If the agent is connected via WebSocket, the task is forwarded
        immediately.  Otherwise returns 503 (agent unreachable).

        Body:
        ```json
        {"query": "...", "sessionId": "..."}
        ```
        """
        agent_id = request.match_info["agent_id"]

        # Check WebSocket connection
        ws = self._ws_connections.get(agent_id)
        if not ws or ws.closed:
            # Check if agent exists but isn't connected
            card = self.store.get_agent(agent_id)
            if card is None:
                return _json_error(404, "agent_not_found",
                                   f"Agent '{agent_id}' not found")
            return _json_error(503, "agent_not_connected",
                               f"Agent '{agent_id}' is not connected via WebSocket")

        try:
            body = await request.json()
        except json.JSONDecodeError:
            return _json_error(400, "invalid_json", "Invalid JSON body")

        query = (body.get("query") or "").strip()
        if not query:
            return _json_error(400, "validation_error", "Missing 'query' field")

        task_id = str(uuid.uuid4())
        session_id = body.get("sessionId", "")
        now = time.time()

        task = {
            "id": task_id,
            "agent_id": agent_id,
            "query": query,
            "session_id": session_id,
            "state": "dispatched",
            "result": None,
            "error": None,
            "created_at": now,
            "updated_at": now,
            "dispatched_at": now,
        }
        self._tasks[task_id] = task

        # Forward to agent via WebSocket
        try:
            await ws.send_json({
                "type": "task",
                "id": task_id,
                "query": query,
                "sessionId": session_id,
            })
            task["state"] = "forwarded"
            logger.info("Dispatched task %s to agent '%s'", task_id, agent_id)
        except Exception as e:
            task["state"] = "failed"
            task["error"] = f"Dispatch failed: {e}"
            logger.error("Dispatch to agent '%s' failed: %s", agent_id, e)
            return _json_error(502, "dispatch_failed",
                               f"Failed to dispatch task to agent '{agent_id}': {e}")

        return web.json_response({
            "task_id": task_id,
            "agent_id": agent_id,
            "state": task["state"],
            "query": query,
            "created_at": task["created_at"],
        }, status=202)

    async def handle_get_task(self, request: web.Request) -> web.Response:
        """GET /v1/tasks/{task_id} — get task status and result."""
        task_id = request.match_info["task_id"]
        task = self._tasks.get(task_id)
        if task is None:
            return _json_error(404, "task_not_found",
                               f"Task '{task_id}' not found")
        return web.json_response(task)

    async def handle_list_tasks(self, request: web.Request) -> web.Response:
        """GET /v1/tasks — list all tasks with optional filters.

        Query params:
            agent_id: filter by agent ID (partial match)
            state: filter by state (dispatched, forwarded, working, completed, failed)
            limit: max results (default 50, max 200)
            offset: pagination offset (default 0)
        """
        try:
            limit = min(int(request.query.get("limit", 50)), 200)
        except (ValueError, TypeError):
            limit = 50
        try:
            offset = max(int(request.query.get("offset", 0)), 0)
        except (ValueError, TypeError):
            offset = 0

        agent_filter = request.query.get("agent_id", "").strip()
        state_filter = request.query.get("state", "").strip()

        all_tasks = list(self._tasks.values())
        # Sort newest first
        all_tasks.sort(key=lambda t: t.get("created_at", 0), reverse=True)

        # Apply filters
        if agent_filter:
            af = agent_filter.lower()
            all_tasks = [t for t in all_tasks if af in t.get("agent_id", "").lower()]
        if state_filter:
            sf = state_filter.lower()
            all_tasks = [t for t in all_tasks if t.get("state", "").lower() == sf]

        total = len(all_tasks)
        page = all_tasks[offset:offset + limit]

        return web.json_response({
            "total": total,
            "limit": limit,
            "offset": offset,
            "tasks": page,
        })

    # ------------------------------------------------------------------
    # Task proxy — forward tasks to agent by its URL
    # ------------------------------------------------------------------

    async def handle_proxy_task(self, request: web.Request) -> web.Response:
        """POST /v1/agents/{agent_id}/task — proxy a task to an agent's URL."""
        agent_id = request.match_info["agent_id"]

        card = self.store.get_agent(agent_id)
        if card is None:
            return _json_error(404, "agent_not_found",
                               f"Agent '{agent_id}' not found")

        # v1.0 AgentCard: URL is in supported_interfaces (top-level `url` is removed)
        target_url = card.get("url") or (
            card.get("supported_interfaces", [{}])[0].get("url", "") if card.get("supported_interfaces") else ""
        )
        if not target_url:
            return _json_error(
                400, "agent_not_routable",
                f"Agent '{agent_id}' has no URL configured",
            )

        return web.json_response({
            "message": f"Task proxied to agent '{agent_id}'",
            "agent_id": agent_id,
            "target_url": target_url,
        })


# ---------------------------------------------------------------------------
# Error handling middleware
# ---------------------------------------------------------------------------


@web.middleware
async def _error_middleware(
    request: web.Request, handler: Any
) -> web.StreamResponse:
    """Catch unhandled exceptions and return a consistent JSON error response.

    All API endpoints (V1 and V2) use ``{"error": str, "detail": str}`` as their
    error envelope.  This middleware ensures even an unexpected 500 follows the
    same shape.
    """
    try:
        response = await handler(request)
        return response
    except web.HTTPException as exc:
        # aiohttp-native HTTP exceptions (404, 405, etc.)
        return web.json_response(
            {
                "error": _status_to_error_code(exc.status),
                "detail": exc.reason or str(exc),
            },
            status=exc.status,
        )
    except json.JSONDecodeError:
        return _json_error(400, "invalid_json", "Invalid JSON body")
    except Exception:
        logger.exception("Unhandled error handling %s %s", request.method, request.path)
        return _json_error(500, "internal_error", "Internal server error")


def _status_to_error_code(status: int) -> str:
    """Map an HTTP status code to a canonical error code string."""
    _MAP = {
        400: "bad_request",
        401: "unauthorized",
        403: "forbidden",
        404: "not_found",
        405: "method_not_allowed",
        409: "conflict",
        410: "gone",
        422: "unprocessable_entity",
        429: "too_many_requests",
        500: "internal_error",
        502: "bad_gateway",
        503: "service_unavailable",
    }
    return _MAP.get(status, f"http_{status}")


# ---------------------------------------------------------------------------
# AdminHandler — Admin REST API (requires registry:admin scope)
# ---------------------------------------------------------------------------


class AdminHandler:
    """Handler for ``/admin/*`` endpoints — admin operations on OAuth clients.

    All endpoints require ``registry:admin`` scope.
    Only registered when ``auth_enabled=True``.
    """

    def __init__(self, auth_store: AuthStore) -> None:
        self.auth_store = auth_store

    async def handle_create_client(self, request: web.Request) -> web.Response:
        """POST /admin/clients — create a new OAuth client.

        Body (JSON)::

            {
                "agent_card_id": "...",
                "description": "My client",
                "allowed_scopes": ["task:read", "task:write"]
            }

        Returns::

            {
                "client_id": "client-abc123",
                "client_secret": "secret-xyz...",
                "agent_card_id": "...",
                "scopes": ["task:read", "task:write"],
                "created_at": 1234567890.0
            }
        """
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response(
                {"error": "invalid_json", "detail": "Invalid JSON body"},
                status=400,
            )

        agent_card_id = body.get("agent_card_id", "")
        description = body.get("description", "")
        allowed_scopes = body.get("allowed_scopes")

        # Validate scopes if provided
        if allowed_scopes is not None:
            if not isinstance(allowed_scopes, list) or not allowed_scopes:
                return web.json_response(
                    {"error": "invalid_scope", "detail": "allowed_scopes must be a non-empty list"},
                    status=400,
                )
            valid_scopes = set(SCOPES.keys())
            for s in allowed_scopes:
                if s not in valid_scopes:
                    return web.json_response(
                        {"error": "invalid_scope", "detail": f"Unknown scope: {s}"},
                        status=400,
                    )

        result = self.auth_store.register_client(
            agent_card_id=agent_card_id,
            allowed_scopes=allowed_scopes,
            description=description,
        )

        client = self.auth_store.get_client(result["client_id"])
        return web.json_response(
            {
                "client_id": result["client_id"],
                "client_secret": result["client_secret"],
                "agent_card_id": client.agent_card_id if client else agent_card_id,
                "scopes": client.allowed_scopes if client else (allowed_scopes or list(SCOPES.keys())),
                "created_at": client.created_at if client else 0.0,
            },
            status=201,
        )

    async def handle_list_clients(self, request: web.Request) -> web.Response:
        """GET /admin/clients — list all registered OAuth clients.

        Returns::

            [
                {
                    "client_id": "client-abc123",
                    "agent_card_id": "...",
                    "description": "My client",
                    "scopes": ["task:read", "task:write"],
                    "token_count": 3,
                    "created_at": 1234567890.0
                },
                ...
            ]
        """
        clients = self.auth_store.list_clients()
        return web.json_response(clients)

    async def handle_delete_client(self, request: web.Request) -> web.Response:
        """DELETE /admin/clients/{client_id} — delete a client and revoke all its tokens."""
        client_id = request.match_info["client_id"]

        # Protect the bootstrap registry service account
        if client_id == "simple-a2a-registry":
            return web.json_response(
                {"error": "protected", "detail": "Cannot delete the registry service account"},
                status=403,
            )

        if not self.auth_store.delete_client(client_id):
            return web.json_response(
                {"error": "not_found", "detail": f"Client '{client_id}' not found"},
                status=404,
            )

        return web.json_response({
            "message": "Client deleted successfully",
            "client_id": client_id,
        })


# ---------------------------------------------------------------------------


async def _cleanup_task(app: web.Application) -> None:
    """Background task to purge stale agents."""
    store: A2ARegistryStore = app["store"]
    handler: RegistryHandler = app.get("handler")
    try:
        while True:
            await asyncio.sleep(CLEANUP_INTERVAL)
            try:
                purged = store.purge_stale()
                if purged:
                    logger.info("Cleanup: purged %d stale agent(s)", purged)
                # Also clean up stale WS connections
                if handler:
                    stale_ws = [
                        aid for aid, ws in handler._ws_connections.items()
                        if ws.closed
                    ]
                    for aid in stale_ws:
                        handler._ws_connections.pop(aid, None)
                        logger.info("Cleaned up stale WS for '%s'", aid)
            except Exception:
                logger.exception("Cleanup task error")
    except asyncio.CancelledError:
        logger.debug("Cleanup task cancelled")
        raise


# ------------------------------------------------------------------
# WebSocket ↔ Kanban bridge helpers
# ------------------------------------------------------------------


def _maybe_update_kanban(
    task_store: Optional[TaskStore],
    dispatched_tasks: Optional[Dict[str, str]],
    task_id: str,
    status: str,
    result: Optional[str],
    error: Optional[str],
) -> None:
    """If *task_id* was WS-dispatched from the kanban board,
    update its status in the TaskStore.

    Only completed/failed/error statuses are written back; progress
    updates are ignored (the TaskStore has its own lifecycle model).
    """
    if task_store is None or dispatched_tasks is None:
        return
    if task_id not in dispatched_tasks:
        return

    if status in ("completed", "success"):
        try:
            task_store.update_task_status(
                task_id, TaskStatus.COMPLETED.value,
                result=json.dumps(result) if isinstance(result, dict) else result,
            )
            logger.info("Kanban task %s → completed (via WS task_result)", task_id)
        except Exception:
            logger.exception("Failed to complete kanban task '%s' from WS result", task_id)
    elif status in ("failed", "error"):
        try:
            task_store.update_task_status(
                task_id, TaskStatus.FAILED.value,
                result=error or str(result) if result else "Agent reported failure",
            )
            logger.info("Kanban task %s → failed (via WS task_result)", task_id)
        except Exception:
            logger.exception("Failed to fail kanban task '%s' from WS result", task_id)


async def _maybe_dispatch_pending(
    task_store: Optional[TaskStore],
    ws: web.WebSocketResponse,
    agent_id: str,
    dispatched_ws_tasks: Optional[Dict[str, str]] = None,
) -> None:
    """On reconnection, find blocked tasks assigned to *agent_id*
    and re-dispatch them over the fresh WebSocket.

    Args:
        task_store: Kanban TaskStore (V2).
        ws: The agent's WebSocket connection.
        agent_id: Registered agent name.
        dispatched_ws_tasks: Optional shared dict from the Dispatcher
            so re-dispatched tasks are tracked for result reconciliation.
    """
    if task_store is None:
        return
    try:
        blocked, _ = task_store.list_tasks(
            status=TaskStatus.BLOCKED.value,
            assignee=agent_id,
            limit=50,
            sort="-priority",
        )
        if not blocked:
            return

        for task in blocked:
            try:
                task_store.update_task_status(
                    task.id, TaskStatus.RUNNING.value,
                )
                task_msg = {
                    "type": "task",
                    "id": task.id,
                    "title": task.title,
                    "body": task.body or "",
                    "assignee": agent_id,
                    "priority": task.priority,
                    "workspace_path": task.workspace_path or "",
                    "kanban": True,
                }
                await ws.send_json(task_msg)
                # Record in shared tracking dict so _maybe_update_kanban can
                # reconcile results when the agent reports completion
                if dispatched_ws_tasks is not None:
                    dispatched_ws_tasks[task.id] = agent_id
                logger.info(
                    "Re-dispatched pending task '%s' to reconnected agent '%s'",
                    task.id, agent_id,
                )
            except Exception as e:
                logger.error("Failed to re-dispatch task '%s': %s", task.id, e)
    except Exception:
        logger.exception("Error checking pending tasks for agent '%s'", agent_id)


def create_app(
    data_dir: str = "~/.simple-a2a-registry",
    base_url: str = "http://localhost:8321",
    board_path: Optional[str] = None,
    *,
    auth_enabled: bool = False,
    dispatcher_enabled: bool = True,
    dispatcher_interval: int = 5,
    claim_ttl: int = 900,
    failure_limit: int = 3,
    workspaces_root: Optional[str] = None,
) -> web.Application:
    """Create and configure the aiohttp web application.

    Args:
        data_dir: Path to persistent data directory.
        base_url: Public base URL for this registry.
        board_path: SQLite path for the V2 orchestration board.
            Defaults to ``<data_dir>/board.db``.
        dispatcher_enabled: Whether to start the background Dispatcher.
        dispatcher_interval: Dispatcher poll interval in seconds.
        claim_ttl: Claim lock TTL in seconds (default 900 / 15 min).
        failure_limit: Global default retry limit.
        workspaces_root: Root directory for scratch workspaces.
            Defaults to ``<data_dir>/workspaces``.

    Returns:
        Configured :class:`aiohttp.web.Application`.
    """
    store = A2ARegistryStore(data_dir)
    handler = RegistryHandler(store, base_url)

    # V2 Orchestration Engine
    if board_path is None:
        board_path = str(Path(data_dir).expanduser() / "board.db")
    task_store = TaskStore(board_path)
    orch_handler = OrchestrationHandler(task_store)

    # V2 Workspace Manager
    if workspaces_root is None:
        workspaces_root = str(Path(data_dir).expanduser() / "workspaces")
    ws_mgr = WorkspaceManager(workspaces_root)

    # OAuth 2.1 Authentication
    # Generate RSA key pair at startup (RS256 primary, HS256 dev fallback)
    auth_store = AuthStore(data_dir)
    if auth_enabled:
        private_key, public_key = _generate_rsa_keypair()
        algorithm = "RS256"
        logger.info("OAuth 2.1 auth enabled — generated RS256 key pair")
    else:
        private_key, public_key = "dev-secret-not-for-production", "dev-secret-not-for-production"
        algorithm = "HS256"
        logger.info("OAuth 2.1 auth disabled — dev mode (HS256 fallback)")

    auth_handler = AuthHandler(
        auth_store,
        private_key=private_key,
        algorithm=algorithm,
        base_url=base_url,
    )

    # Wire auth_store to handler for WebSocket client auth and admin endpoints
    handler.auth_store = auth_store
    handler._auth_public_key = public_key
    handler._auth_algorithm = algorithm
    handler._auth_enabled = auth_enabled

    # V2 Dispatcher (background worker dispatch)
    disp_config = DispatcherConfig(
        poll_interval=dispatcher_interval,
        claim_ttl=claim_ttl,
        failure_limit=failure_limit,
    )
    dispatcher = Dispatcher(task_store, ws_mgr, disp_config,
                               ws_connections=handler._ws_connections) if dispatcher_enabled else None

    # Wire up cross-references for WebSocket ↔ Kanban integration
    handler.task_store = task_store
    if dispatcher:
        handler._dispatched_ws_tasks = dispatcher._dispatched_ws_tasks

    app = web.Application(middlewares=[
        _error_middleware,
        _auth_middleware_factory(
            auth_store,
            enabled=auth_enabled,
            public_key=public_key,
            algorithm=algorithm,
        ),
    ])
    app["store"] = store
    app["handler"] = handler
    app["auth_store"] = auth_store
    app["auth_handler"] = auth_handler
    app["task_store"] = task_store
    app["orch_handler"] = orch_handler
    app["ws_mgr"] = ws_mgr
    app["dispatcher"] = dispatcher

    # Health / well-known
    app.router.add_get("/health", handler.handle_health)
    app.router.add_get("/.well-known/agent-card.json", handler.handle_well_known)

    # Agent CRUD
    # V1 GET /v1/agents — requires agent:read scope
    app.router.add_get("/v1/agents", require_scope("agent:read")(handler.handle_list_agents))
    # V1 GET /v1/agents/{agent_id} — requires agent:read scope
    app.router.add_get(
        "/v1/agents/{agent_id}",
        require_scope("agent:read")(handler.handle_get_agent),
    )
    # V1 POST /v1/agents — requires agent:register scope
    app.router.add_post(
        "/v1/agents",
        require_scope("agent:register")(handler.handle_register),
    )
    # V1 DELETE /v1/agents/{agent_id} — requires agent:admin scope
    app.router.add_delete(
        "/v1/agents/{agent_id}",
        require_scope("agent:admin")(handler.handle_unregister),
    )

    # Heartbeat — requires agent:read (agent must know its own ID to call this)
    app.router.add_post(
        "/v1/agents/{agent_id}/heartbeat",
        require_scope("agent:read")(handler.handle_heartbeat),
    )

    # WebSocket — no scope check here (query param token validated in handler)
    app.router.add_get(
        "/v1/agents/{agent_id}/ws", handler.handle_ws
    )

    # Task dispatch (via WS) — requires task:write
    app.router.add_post(
        "/v1/agents/{agent_id}/dispatch",
        require_scope("task:write")(handler.handle_dispatch),
    )
    app.router.add_get(
        "/v1/tasks",
        require_scope("task:read")(handler.handle_list_tasks),
    )
    app.router.add_get(
        "/v1/tasks/{task_id}",
        require_scope("task:read")(handler.handle_get_task),
    )

    # Task proxy (fallback) — requires task:write
    app.router.add_post(
        "/v1/agents/{agent_id}/task",
        require_scope("task:write")(handler.handle_proxy_task),
    )

    # Static dashboard
    static_dir = Path(__file__).parent / "static"
    if static_dir.is_dir():
        async def _dashboard(request: web.Request) -> web.StreamResponse:
            html_path = static_dir / "index.html"
            html = html_path.read_text(encoding="utf-8")
            if auth_enabled:
                # Generate a dashboard-specific token with all scopes
                dash_token = create_token(
                    sub="dashboard",
                    private_key=private_key,
                    algorithm=algorithm,
                    scope=" ".join(SCOPES.keys()),
                )
                # Inject auth config before closing </head>
                config_script = (
                    "<script>\n"
                    f"window.__AUTH_CONFIG = {{ enabled: true, token: {json.dumps(dash_token)} }};\n"
                    "</script>\n</head>"
                )
                html = html.replace("</head>", config_script)
            return web.Response(
                text=html,
                content_type="text/html",
                charset="utf-8",
                headers={
                    "Cache-Control": "no-cache, no-store, must-revalidate",
                    "Pragma": "no-cache",
                    "Expires": "0",
                },
            )
        app.router.add_get("/", _dashboard, name="dashboard")

    # V2 Orchestration routes
    register_v2_routes(app, orch_handler)

    # OAuth 2.1 Token / Registration endpoints (always registered, public)
    app.router.add_post("/auth/token", auth_handler.handle_token)
    app.router.add_post("/auth/register", auth_handler.handle_register)
    app.router.add_get(
        "/.well-known/oauth-authorization-server",
        auth_handler.handle_well_known_oauth,
    )
    if auth_enabled:
        # JWKS endpoint only when RS256 keys are generated
        try:
            jwks_handler = make_jwks_endpoint(public_key)
            app.router.add_get("/.well-known/jwks.json", jwks_handler)
        except Exception as e:
            logger.warning("Failed to create JWKS endpoint (non-fatal): %s", e)

        # Admin REST API — create/list/delete OAuth clients
        admin_handler = AdminHandler(auth_store)
        app.router.add_post(
            "/admin/clients",
            require_scope("registry:admin")(admin_handler.handle_create_client),
        )
        app.router.add_get(
            "/admin/clients",
            require_scope("registry:admin")(admin_handler.handle_list_clients),
        )
        app.router.add_delete(
            "/admin/clients/{client_id}",
            require_scope("registry:admin")(admin_handler.handle_delete_client),
        )

    # Background cleanup + Dispatcher
    cleanup_task_ref: list[asyncio.Task] = []
    async def _start_background(app: web.Application) -> None:
        # V1 cleanup task
        task = asyncio.create_task(_cleanup_task(app))
        cleanup_task_ref.append(task)
        # V2 Dispatcher
        disp: Optional[Dispatcher] = app.get("dispatcher")
        if disp:
            asyncio.create_task(disp.run())

    async def _stop_background(app: web.Application) -> None:
        # Stop Dispatcher
        disp: Optional[Dispatcher] = app.get("dispatcher")
        if disp:
            disp.stop()
        # Cancel cleanup task
        if cleanup_task_ref:
            task = cleanup_task_ref[0]
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        # Close TaskStore
        ts: TaskStore = app.get("task_store")
        if ts:
            ts.close()

    app.on_startup.append(_start_background)
    app.on_cleanup.append(_stop_background)

    return app


def run_server(
    host: str = "0.0.0.0",
    port: int = 8321,
    data_dir: str = "~/.simple-a2a-registry",
    auth_enabled: bool = False,
    board_path: Optional[str] = None,
    dispatcher_enabled: bool = True,
    dispatcher_interval: int = 5,
    claim_ttl: int = 900,
    failure_limit: int = 3,
    workspaces_root: Optional[str] = None,
) -> None:
    """Start the A2A Registry HTTP server.

    Args:
        host: Bind address.
        port: Bind port.
        data_dir: Persistent data directory.
        auth_enabled: Enable OAuth 2.1 authentication middleware.
        board_path: SQLite database path for the V2 orchestration board.
        dispatcher_enabled: Whether to start the background Dispatcher.
        dispatcher_interval: Dispatcher poll interval in seconds.
        claim_ttl: Claim lock TTL in seconds.
        failure_limit: Global default retry limit.
        workspaces_root: Root directory for scratch workspaces.
    """
    app = create_app(
        data_dir=data_dir,
        auth_enabled=auth_enabled,
        board_path=board_path,
        dispatcher_enabled=dispatcher_enabled,
        dispatcher_interval=dispatcher_interval,
        claim_ttl=claim_ttl,
        failure_limit=failure_limit,
        workspaces_root=workspaces_root,
    )
    auth_status = "enabled" if auth_enabled else "disabled (dev)"
    logger.info(
        "Simple A2A Registry starting on %s:%s (data: %s) auth=%s",
        host, port, data_dir, auth_status,
    )
    web.run_app(app, host=host, port=port)