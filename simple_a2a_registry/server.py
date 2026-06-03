"""A2A Registry HTTP server — aiohttp-based REST API with WebSocket dispatch."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import socket
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from aiohttp import web, WSMsgType
from aiohttp.web_middlewares import middleware
import aiohttp
import ssl

from simple_a2a_registry.errors import (
    json_error,
    error_middleware as _unified_error_middleware,
    timeout_middleware,
)
from simple_a2a_registry.log import log_key_event, request_id_middleware_factory
from simple_a2a_registry.metrics import (
    metrics_middleware_factory,
    handle_metrics,
    update_agent_gauges,
    update_ws_connections,
    update_db_pool_size,
)
from simple_a2a_registry.rate_limiter import rate_limit_middleware_factory
from simple_a2a_registry.models import make_agent_card, make_agent_skill
from simple_a2a_registry.validation import (
    validate_agent_card,
    sanitize_card_output,
)
from simple_a2a_registry.config import Config
from simple_a2a_registry.database import create_engine, SQLiteEngine
from simple_a2a_registry.store import Store, HEARTBEAT_TIMEOUT
from simple_a2a_registry.auth import (
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
from simple_a2a_registry.audit import (
    AuditStore,
    EventType,
    _maybe_create_audit_schema,
)
from simple_a2a_registry.users import (
    UserStore,
    SessionManager,
    UserHandler,
    user_session_middleware_factory,
    require_role,
)
from simple_a2a_registry.store import Store, HEARTBEAT_TIMEOUT, _maybe_create_schema as _maybe_create_registry_schema
from simple_a2a_registry.orchestration import (
    TaskStore,
    TaskStatus,
    OrchestrationHandler,
    register_v2_routes,
    Dispatcher,
    DispatcherConfig,
    WorkspaceManager,
    _maybe_create_schema as _maybe_create_board_schema,
    SwarmHandler,
    register_swarm_routes,
    AnomalyScanner,
    SlaCalculator,
    SlaUpdater,
)
from simple_a2a_registry.ws_admin import AdminWSHub
from simple_a2a_registry.registry_handler import (
    WSContext,
    create_default_router,
)
from simple_a2a_registry.registry_handler import WSMessageRouter
from simple_a2a_registry.registry_handler import _get_ws_handler

logger = logging.getLogger("a2a_registry.server")

REGISTRY_VERSION = "1.0.0"
REGISTRY_AGENT_ID = "simple-a2a-registry"
REGISTRY_AGENT_NAME = "Simple A2A Registry"
REGISTRY_AGENT_DESCRIPTION = (
    "A lightweight Agent-to-Agent registry"
)

CLEANUP_INTERVAL = 60  # seconds between stale-agent purges
DANGLING_GRACE_SECONDS = 30  # WS disconnect grace period before failing tasks


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# NOTE: ``json_error()`` is imported from ``errors.py`` — use that
# instead of creating inline helpers.


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
                "Dispatch a task to an agent via WebSocket or HTTP callback",
            ),
        ],
    )


# ---------------------------------------------------------------------------
# RegistryHandler
# ---------------------------------------------------------------------------


class RegistryHandler:
    """HTTP + WebSocket handler methods for the A2A Registry."""

    def __init__(self, store: Store, base_url: str,
                 auth_store: Optional[Store] = None,
                 audit_store: Optional[AuditStore] = None) -> None:
        self.store = store
        self.base_url = base_url.rstrip("/")
        self._started_at = time.time()
        self.audit_store = audit_store

        # HTTP client session for callback dispatch
        self._session: Optional[aiohttp.ClientSession] = None

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

        # Task timeout management
        self._task_timeout: int = 300  # Default 300s, overridden via config
        self._task_timers: Dict[str, asyncio.Task] = {}  # task_id -> asyncio timer task

        # DANGLING state management — graceful WS disconnect window
        self._dangling_timers: Dict[str, asyncio.Task] = {}  # agent_id -> asyncio timer task
        self._dangling_grace_seconds: int = DANGLING_GRACE_SECONDS

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

        # Query param ?tenant= overrides auth-injected tenant ONLY when
        # the auth-injected tenant is empty (admin scope / backwards compat).
        # When a non-admin token has a bound tenant, the query param cannot
        # override it — this prevents ?tenant= spoofing.
        # An empty ?tenant= value is treated as "no filter" (admin scope),
        # consistent with list_agents(tenant=None) semantics.
        auth_tenant = request["tenant"] if "tenant" in request else None
        qp_tenant = request.query.get("tenant", None)
        if qp_tenant is not None and qp_tenant != "" and (not auth_tenant):
            tenant = qp_tenant
        else:
            # auth_tenant is None (no auth), "" (admin -> see all), or "tenant-a" (normal)
            tenant = auth_tenant or None
        all_agents = self.store.list_agents(skill=skill, tag=tag, q=q, tenant=tenant)
        total = len(all_agents)
        page = all_agents[offset:offset + limit]

        # Annotate with WebSocket connection info
        for agent in page:
            aid = agent.get("id", "")
            if aid in self._ws_connections:
                agent["connection"] = "websocket"
                agent["status"] = "alive"

        # Sanitize output for XSS prevention
        page = [sanitize_card_output(a) for a in page]

        return web.json_response({
            "total": total,
            "limit": limit,
            "offset": offset,
            "agents": page,
        })

    async def handle_get_agent(self, request: web.Request) -> web.Response:
        """GET /v1/agents/{agent_id}"""
        agent_id = request.match_info["agent_id"]
        # JWT tenant from auth middleware is authoritative; fall back to
        # ?tenant= query param, then X-Tenant-ID header.
        # When auth_tenant is set, ?tenant= cannot override it (anti-spoofing).
        auth_tenant = request["tenant"] if "tenant" in request else None
        qp_tenant = request.query.get("tenant", None)
        if qp_tenant is not None and (not auth_tenant):
            tenant = qp_tenant
        elif auth_tenant:
            tenant = auth_tenant
        elif auth_tenant is not None:  # auth is empty string (admin scope)
            tenant = None
        else:
            tenant = request.headers.get("X-Tenant-ID", None)
        card = self.store.get_agent(agent_id, tenant=tenant)
        if card is None:
            return json_error(404, "agent_not_found", f"Agent '{agent_id}' not found")
        if agent_id in self._ws_connections:
            card["connection"] = "websocket"
            card["status"] = "alive"
        # Sanitize output for XSS prevention
        card = sanitize_card_output(card)
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
            return json_error(400, "invalid_json", "Invalid JSON body")

        name = body.get("name", "").strip()
        if not name:
            return json_error(400, "validation_error", "Agent requires a 'name'")

        # Validate agent card fields (lengths, types, XSS prevention)
        validation_errors = validate_agent_card(body)
        if validation_errors:
            details = "; ".join(e.detail for e in validation_errors[:5])
            return json_error(400, "validation_error", details)

        # Auth middleware injects request['tenant'] (JWT); fall back to body tenant.
        # Admin scope (registry:admin) sets request['tenant']='' so registration
        # creates an agent in the default (empty) tenant.
        # Auth middleware always sets request['tenant'] ('' when JWT has no tenant).
        # When auth tenant is empty, fall back to the body's tenant field so that
        # tokens without a tenant claim can still register agents under a tenant.
        tenant = request["tenant"] if request.get("tenant") else body.get("tenant", "")
        existing = self.store.list_agents(tenant=tenant if tenant else None)
        for agent in existing:
            if agent.get("name", "").strip().lower() == name.lower():
                return json_error(
                    409, "agent_exists", f"Agent '{name}' already exists"
                )
        tenant = request["tenant"] if request.get("tenant") else body.get("tenant", "")
        agent_id = self.store.register_agent(body, tenant=tenant)
        card = self.store.get_agent(agent_id)
        # Sanitize output for XSS prevention
        card = sanitize_card_output(card)

        response = {
            "message": "Agent registered successfully",
            "id": agent_id,
            "card": card,
        }

        # Auto-create OAuth client if the agent card includes security_schemes
        security_schemes = body.get("security_schemes", {})
        if security_schemes and self.auth_store:
            # Determine scopes from all OAuth flows
            oauth_scopes = set()
            for _scheme_name, scheme in security_schemes.items():
                if isinstance(scheme, dict) and scheme.get("scheme_type") == "oauth2":
                    oauth_flows = scheme.get("oauth2", {}).get("flows", {})
                    for _flow_name, flow in oauth_flows.items():
                        if isinstance(flow, dict):
                            oauth_scopes.update(flow.get("scopes", {}).keys())
            allowed_scopes = list(oauth_scopes) if oauth_scopes else None

            client_result = self.auth_store.register_client(
                agent_card_id=agent_id,
                allowed_scopes=allowed_scopes,
                description=f"Auto-created client for agent '{name}'",
                tenant=tenant if tenant else "",
            )

            client = self.auth_store.get_client(client_result["client_id"])
            response["client"] = {
                "client_id": client_result["client_id"],
                "client_secret": client_result["client_secret"],
                "scopes": client.allowed_scopes if client else (allowed_scopes or list(SCOPES.keys())),
                "tenant": client.tenant if client else (tenant or ""),
            }

        if self.audit_store is not None:
            self.audit_store.log(
                event_type=EventType.AGENT_REGISTER.value,
                actor=request.remote or "unknown",
                target=agent_id,
                detail=f"name={name}",
                success=True,
            )

        # Broadcast agent registration event to Admin UI
        admin_ws_hub = request.app.get("admin_ws_hub")
        if admin_ws_hub:
            admin_ws_hub.broadcast_to_all({"type": "agent_registered", "agent_id": agent_id, "name": name})

        return web.json_response(response, status=201)

    async def handle_unregister(self, request: web.Request) -> web.Response:
        """DELETE /v1/agents/{agent_id} — unregister an agent.

        Closes the agent's WebSocket connection and removes it from the registry.

        Auth middleware injects request['tenant'] (JWT); falls back to
        ?tenant= query param.  When auth is disabled (no request['tenant']),
        the query param is used directly.

        Query params:
            tenant: Optional tenant filter (only when auth is disabled or
                JWT tenant is empty, e.g. admin scope).
        """
        agent_id = request.match_info["agent_id"]
        # Auth middleware injects request['tenant']; fall back to ?tenant= query param.
        # When request['tenant'] is set, it's authoritative (from JWT or admin override).
        tenant = request["tenant"] if "tenant" in request else (request.query.get("tenant", "") or "")

        # Close WS connection if open
        ws = self._ws_connections.pop(agent_id, None)
        if ws and not ws.closed:
            try:
                await ws.send_json({"type": "close"})
                await ws.close()
            except Exception:
                pass

        success = self.store.unregister(agent_id, tenant=tenant)
        if not success:
            return json_error(404, "agent_not_found", f"Agent '{agent_id}' not found")

        if self.audit_store is not None:
            self.audit_store.log(
                event_type=EventType.AGENT_DEREGISTER.value,
                actor=request.remote or "unknown",
                target=agent_id,
                detail=f"unregistered via {'WebSocket disconnect' if request.path.endswith('/ws') else 'DELETE endpoint'}",
                success=True,
            )

        # Broadcast agent unregistration event to Admin UI
        admin_ws_hub = request.app.get("admin_ws_hub")
        if admin_ws_hub:
            admin_ws_hub.broadcast_to_all({"type": "agent_removed", "agent_id": agent_id})

        return web.json_response({
            "message": "Agent unregistered successfully",
            "id": agent_id,
        })

    # ------------------------------------------------------------------
    # Heartbeat
    # ------------------------------------------------------------------

    async def handle_heartbeat(self, request: web.Request) -> web.Response:
        """POST /v1/agents/{agent_id}/heartbeat

        Auth middleware injects request['tenant']; falls back to ?tenant= query param.
        The heartbeat only succeeds if the agent belongs to the caller's tenant.
        """
        agent_id = request.match_info["agent_id"]
        # Auth middleware sets request['tenant']='' for admin scope;
        # convert '' to None so get_agent doesn't filter by empty tenant.
        auth_tenant = request["tenant"] if "tenant" in request else None
        qp_tenant = request.query.get("tenant", None)
        if qp_tenant is not None and (not auth_tenant):
            tenant = qp_tenant
        else:
            tenant = auth_tenant or None  # "" (admin) → None (all tenants)
        card = self.store.get_agent(agent_id, tenant=tenant)
        if card is None:
            return json_error(404, "agent_not_found", f"Agent '{agent_id}' not found")

        if card["status"] == "stale":
            return json_error(
                410, "agent_stale", f"Agent '{agent_id}' is stale and cannot heartbeat"
            )

        success = self.store.heartbeat(agent_id, tenant=tenant or "")
        if not success:
            return json_error(404, "agent_not_found", f"Agent '{agent_id}' not found")

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

    async def handle_toggle_agent(self, request: web.Request) -> web.Response:
        """POST /v1/agents/{agent_id}/toggle — toggle agent disabled status.

        Query params:
            tenant: Optional tenant filter.  If set, only toggles if the
                agent belongs to this tenant.

        Requires agent:admin scope.
        """
        agent_id = request.match_info["agent_id"]
        # Auth middleware injects request['tenant']; fall back to ?tenant= query param
        tenant = request["tenant"] if "tenant" in request else (request.query.get("tenant", "") or "")
        result = self.store.toggle_agent(agent_id, tenant=tenant)
        if result is None:
            return json_error(404, "agent_not_found", f"Agent '{agent_id}' not found")
        card = self.store.get_agent(agent_id)
        return web.json_response(
            {
                "id": agent_id,
                "status": card["status"] if card else "unknown",
                "disabled": card["disabled"] if card else False,
            }
        )
    # ------------------------------------------------------------------
    # Admin WebSocket — real-time status broadcast to Admin UI
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Admin WebSocket — real-time status broadcast to Admin UI
    # ------------------------------------------------------------------

    async def handle_admin_ws(self, request: web.Request) -> web.WebSocketResponse:
        """GET /v2/ws/admin — delegate to AdminWSHub.register().

        The AdminWSHub is stored in app['admin_ws_hub'] and provides
        subscription-based WebSocket management with heartbeat support.
        """
        admin_ws_hub = request.app.get("admin_ws_hub")
        if admin_ws_hub is None:
            return web.json_response(
                {"error": "service_unavailable", "detail": "Admin WebSocket hub not initialized"},
                status=503,
            )
        return await admin_ws_hub.register(request)

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

        # Extract tenant from WebSocket query param (or later from JWT token).
        # The auth_middleware skips WS paths, so request["tenant"] is not set here.
        ws_tenant = request.query.get("tenant", None)

        # Verify the agent is registered FIRST (needed for sub check below)
        card = self.store.get_agent(agent_id, tenant=ws_tenant)
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
            #   - an OAuth client_id whose agent_card_id matches this agent's
            #     name OR whose agent_card_id is a suffix/substring of the name
            #     (e.g. agent_card_id="coder-agent" matches name="Hermes Coder Agent")
            token_sub = payload.get("sub", "")
            if token_sub not in (agent_id, "simple-a2a-registry"):
                sub_ok = False
                if self.auth_store:
                    client = self.auth_store.get_client(token_sub)
                    if client and client.agent_card_id:
                        card_name = (card.get("name") or "").lower()
                        card_id = client.agent_card_id.lower()
                        # Normalize hyphens/underscores to spaces for matching
                        # e.g. "coder-agent" ↔ "hermes coder agent"
                        card_id_norm = card_id.replace("-", " ").replace("_", " ")
                        sub_ok = (
                            card_id == card_name
                            or card_id in card_name
                            or card_name.endswith(card_id)
                            or card_id_norm in card_name
                            or card_name.endswith(card_id_norm)
                            # agent_card_id may be the agent UUID (auto-created client)
                            or card_id == agent_id.lower()
                        )
                if not sub_ok:
                    return web.json_response(
                        {"error": "forbidden", "detail":
                         f"Token subject '{token_sub}' does not match agent '{agent_id}'"},
                        status=403,
                    )

            # JWT tenant is authoritative — override ws_tenant from token payload.
            # Admin scope (registry:admin) bypasses tenant isolation.
            token_tenant = payload.get("tenant", None)
            token_scopes = payload.get("scope", "").split()
            if "registry:admin" in token_scopes:
                ws_tenant = ""
            elif token_tenant is not None:
                ws_tenant = token_tenant

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
        update_ws_connections(len(self._ws_connections))
        logger.info("Agent '%s' connected via WebSocket (%d active)",
                     agent_id, len(self._ws_connections))

        # Mark alive in store
        self.store.heartbeat(agent_id, tenant=ws_tenant or "")

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

                    # --- Fast-path handlers (no routing overhead) ---
                    if msg_type == "ping":
                        await ws.send_json({"type": "pong"})

                    elif msg_type == "close":
                        logger.info("Agent '%s' closing WebSocket", agent_id)
                        break

                    # --- Handler-map dispatch (extensible) ---
                    else:
                        handler_fn = _get_ws_handler(msg_type)
                        if handler_fn is not None:
                            await handler_fn(self, ws, data, agent_id)
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
            update_ws_connections(len(self._ws_connections))
            logger.info("Agent '%s' disconnected via WebSocket (%d active)",
                         agent_id, len(self._ws_connections))

            # P1.2: WS 断连标记任务为 DANGLING + 启动宽限计时器
            try:
                await self._dangle_agent_tasks(agent_id)
            except Exception:
                logger.exception(
                    "Failed to auto-fail tasks for disconnected agent '%s'",
                    agent_id,
                )

            # P1.3: Broadcast disconnection event to Admin UI
            admin_ws_hub = request.app.get("admin_ws_hub")
            if admin_ws_hub:
                try:
                    admin_ws_hub.broadcast_to_all({
                        "type": "agent_disconnected",
                        "agent_id": agent_id,
                    })
                except Exception:
                    logger.exception(
                        "Failed to broadcast disconnection event for '%s'",
                        agent_id,
                    )

        return ws

    async def _dangle_agent_tasks(self, agent_id: str) -> None:
        """Mark all active tasks for a disconnected agent as DANGLING.

        Instead of immediately failing tasks, enters a grace period
        (``_dangling_grace_seconds``) during which the agent may reconnect.
        If the agent reconnects in time, state_sync will reconcile the
        tasks back to RUNNING; otherwise ``_dangle_timeout`` promotes
        DANGLING → FAILED.

        Scans ``self._tasks`` for tasks belonging to *agent_id* with
        non-terminal states.  For WS-dispatched kanban tasks, also updates
        the V2 ``TaskStore`` to reflect the DANGLING state.
        """
        now = time.time()
        dangled_ids: list[str] = []

        for task_id, task in list(self._tasks.items()):
            if task.get("agent_id") != agent_id:
                continue
            state = task.get("state", "")
            if state in ("completed", "failed", "cancelled", "dangling"):
                continue

            task["state"] = "dangling"
            task["error"] = "agent_disconnected_grace"
            task["updated_at"] = now
            dangled_ids.append(task_id)

            # Cancel the per-task timeout timer while dangling
            self._cancel_task_timeout(task_id)

            logger.info(
                "Task %s for agent '%s' → dangling (WS disconnect, %ds grace)",
                task_id, agent_id, self._dangling_grace_seconds,
            )

        if not dangled_ids:
            return

        # Sync with V2 TaskStore for WS-dispatched kanban tasks
        if self.task_store is not None and self._dispatched_ws_tasks is not None:
            for task_id in dangled_ids:
                if task_id in self._dispatched_ws_tasks:
                    try:
                        self.task_store.update_task_status(
                            task_id, TaskStatus.DANGLING.value,
                            result="Agent disconnected — grace period",
                        )
                        logger.info(
                            "Kanban task %s → dangling (agent WS disconnect)",
                            task_id,
                        )
                    except Exception:
                        logger.exception(
                            "Failed to dangle kanban task '%s' from WS disconnect",
                            task_id,
                        )

        # Cancel any existing dangling timer for this agent and schedule new one
        old_timer = self._dangling_timers.pop(agent_id, None)
        if old_timer is not None and not old_timer.done():
            old_timer.cancel()
            logger.debug("Cancelled stale dangling timer for agent '%s'", agent_id)

        timer = asyncio.create_task(
            self._dangle_timeout(agent_id),
            name=f"dangle-timeout-{agent_id}",
        )
        self._dangling_timers[agent_id] = timer
        logger.info(
            "Scheduled dangling timeout for agent '%s' in %ds (%d tasks dangled)",
            agent_id, self._dangling_grace_seconds, len(dangled_ids),
        )

    async def _dangle_timeout(self, agent_id: str) -> None:
        """Grace-period timeout callback: promote DANGLING → FAILED.

        Called ``_dangling_grace_seconds`` after ``_dangle_agent_tasks``.
        If the agent reconnected in the interim its tasks will have been
        reconciled back to RUNNING by state_sync (P1.3), so this method
        only acts on tasks still in the DANGLING state.
        """
        try:
            await asyncio.sleep(self._dangling_grace_seconds)
        except asyncio.CancelledError:
            # Agent reconnected and timer was cancelled — nothing to do
            return

        now = time.time()
        failed_ids: list[str] = []

        for task_id, task in list(self._tasks.items()):
            if task.get("agent_id") != agent_id:
                continue
            state = task.get("state", "")
            if state != "dangling":
                continue

            task["state"] = "failed"
            task["error"] = "agent_disconnected_timeout"
            task["updated_at"] = now
            failed_ids.append(task_id)

            logger.info(
                "Task %s for agent '%s' → failed (dangling grace period expired)",
                task_id, agent_id,
            )

        if not failed_ids:
            logger.debug("Dangling timeout for agent '%s': no dangling tasks remain", agent_id)

        # Sync with V2 TaskStore
        if self.task_store is not None and self._dispatched_ws_tasks is not None:
            for task_id in failed_ids:
                if task_id in self._dispatched_ws_tasks:
                    try:
                        self.task_store.update_task_status(
                            task_id, TaskStatus.FAILED.value,
                            result="Dangling grace period expired",
                        )
                        logger.info(
                            "Kanban task %s → failed (dangling timeout)",
                            task_id,
                        )
                    except Exception:
                        logger.exception(
                            "Failed to fail kanban task '%s' from dangling timeout",
                            task_id,
                        )

        # Clean up the timer entry
        self._dangling_timers.pop(agent_id, None)

    # ------------------------------------------------------------------
    # P1.2: Task timeout management
    # ------------------------------------------------------------------

    def _schedule_task_timeout(self, task_id: str) -> None:
        """Schedule a timeout timer for *task_id*.

        Creates an ``asyncio.Task`` that waits ``_task_timeout`` seconds and
        then fires the timeout logic:

        1. Mark the task as ``failed("timeout")``
        2. Send a ``task_cancel`` WebSocket message to the agent
        3. Clean up the timer entry
        4. Reconcile with the kanban TaskStore (if applicable)
        """
        # Cancel any existing timer first (safe if None)
        self._cancel_task_timeout(task_id)

        task_obj = self._tasks.get(task_id)
        if task_obj is None:
            logger.warning("Cannot schedule timeout for unknown task '%s'", task_id)
            return

        async def _timeout_worker() -> None:
            try:
                await asyncio.sleep(self._task_timeout)
            except asyncio.CancelledError:
                return

            task = self._tasks.get(task_id)
            if task is None:
                self._task_timers.pop(task_id, None)
                return

            state = task.get("state", "")
            if state in ("completed", "failed", "cancelled"):
                self._task_timers.pop(task_id, None)
                return

            logger.warning(
                "Task %s for agent '%s' timed out after %ds (state: %s)",
                task_id, task.get("agent_id", "?"),
                self._task_timeout, state,
            )

            task["state"] = "failed"
            task["error"] = "timeout"
            task["updated_at"] = time.time()

            # Send task_cancel to the agent
            agent_id = task.get("agent_id", "")
            ws = self._ws_connections.get(agent_id)
            if ws is not None and not ws.closed:
                try:
                    await ws.send_json({
                        "type": "task_cancel",
                        "id": task_id,
                        "reason": "timeout",
                        "dispatched_at": task.get("dispatched_at", 0),
                    })
                    logger.info("Sent task_cancel to agent '%s' for timed-out task '%s'",
                                agent_id, task_id)
                except Exception:
                    logger.exception(
                        "Failed to send task_cancel to agent '%s' for task '%s'",
                        agent_id, task_id,
                    )

            # Reconcile with kanban TaskStore
            if self.task_store is not None and self._dispatched_ws_tasks is not None:
                if task_id in self._dispatched_ws_tasks:
                    try:
                        self.task_store.update_task_status(
                            task_id, TaskStatus.FAILED.value,
                            result=f"Task timed out after {self._task_timeout}s",
                        )
                        logger.info("Kanban task %s -> failed (timeout via WS)", task_id)
                    except Exception:
                        logger.exception(
                            "Failed to fail kanban task '%s' from timeout",
                            task_id,
                        )

            self._task_timers.pop(task_id, None)

        timer = asyncio.create_task(_timeout_worker())
        self._task_timers[task_id] = timer
        logger.debug("Scheduled timeout timer for task '%s' (%ds)",
                     task_id, self._task_timeout)

    def _cancel_task_timeout(self, task_id: str) -> None:
        """Cancel the timeout timer for *task_id* (no-op if absent)."""
        timer = self._task_timers.pop(task_id, None)
        if timer is not None and not timer.done():
            timer.cancel()
            logger.debug("Cancelled timeout timer for task '%s'", task_id)

    def _cancel_dangling_timer(self, agent_id: str) -> None:
        """Cancel the dangling grace-period timer for *agent_id* (no-op if absent).

        Called when an agent reconnects and its tasks are healed back to RUNNING
        via state_sync (P1.3).
        """
        timer = self._dangling_timers.pop(agent_id, None)
        if timer is not None and not timer.done():
            timer.cancel()
            logger.debug("Cancelled dangling timer for agent '%s'", agent_id)

    def _reset_task_timeout(self, task_id: str) -> None:
        """Cancel and re-schedule the timeout for *task_id*.

        Called when the agent sends ``task_ack`` or ``task_progress`` -
        each act of progress resets the clock.
        """
        self._schedule_task_timeout(task_id)

    # ------------------------------------------------------------------
    # Task dispatch - client -> Registry -> Agent (via WS)
    # ------------------------------------------------------------------

    async def handle_dispatch(self, request: web.Request) -> web.Response:
        """POST /v1/agents/{agent_id}/dispatch — submit a task to an agent.

        Dispatches via:
        - WebSocket if the agent is connected (preferred_channel == "ws")
        - HTTP POST callback if the agent has preferred_channel == "callback"
          and a callback_url.

        Returns 503 if neither channel is available.
        """
        agent_id = request.match_info["agent_id"]

        # Tenant filter from auth middleware; fall back to query param
        tenant = request["tenant"] if "tenant" in request else request.query.get("tenant", None)

        # Look up agent card for callback info (and existence check)
        card = self.store.get_agent(agent_id, tenant=tenant)
        if card is None:
            return json_error(404, "agent_not_found",
                               f"Agent '{agent_id}' not found")

        preferred_channel = card.get("preferred_channel", "ws") or "ws"
        callback_url = (card.get("callback_url", "") or "").strip()

        # Check WebSocket connection for WS-mode agents
        ws = None
        if preferred_channel == "ws":
            ws = self._ws_connections.get(agent_id)

        if preferred_channel == "callback":
            if not callback_url:
                return json_error(503, "callback_not_configured",
                                   f"Agent '{agent_id}' has preferred_channel='callback' but no callback_url")

        elif not ws or ws.closed:
            # WS-mode agent not connected
            return json_error(503, "agent_not_connected",
                               f"Agent '{agent_id}' is not connected via WebSocket")

        try:
            body = await request.json()
        except json.JSONDecodeError:
            return json_error(400, "invalid_json", "Invalid JSON body")

        query = (body.get("query") or "").strip()
        if not query:
            return json_error(400, "validation_error", "Missing 'query' field")

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
            "tenant": tenant or "",
        }
        self._tasks[task_id] = task

        # --- Dispatch logic ---

        if preferred_channel == "callback":
            # HTTP POST callback dispatch
            try:
                callback_token = card.get("callback_token", "") or ""
                headers = {
                    "Content-Type": "application/json",
                }
                if callback_token:
                    headers["Authorization"] = f"Bearer {callback_token}"

                payload = {
                    "type": "task",
                    "id": task_id,
                    "query": query,
                    "sessionId": session_id,
                    "callbackUrl": f"{request.scheme}://{request.host}/v1/tasks/{task_id}/callback-result",
                }

                async with self._session.post(
                    callback_url, json=payload, headers=headers,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    if resp.status >= 400:
                        text = await resp.text()
                        logger.error(
                            "Callback dispatch to '%s' failed: HTTP %d: %s",
                            callback_url, resp.status, text,
                        )
                        task["state"] = "failed"
                        task["error"] = f"Callback returned HTTP {resp.status}"
                        return json_error(502, "callback_failed",
                                           f"Callback to agent '{agent_id}' failed: HTTP {resp.status}")

                task["state"] = "forwarded"
                logger.info(
                    "Dispatched task %s to agent '%s' via callback (%s)",
                    task_id, agent_id, callback_url,
                )
            except asyncio.TimeoutError:
                task["state"] = "failed"
                task["error"] = "Callback timed out after 30s"
                logger.error("Callback dispatch to agent '%s' timed out", agent_id)
                return json_error(502, "callback_timeout",
                                   f"Callback to agent '{agent_id}' timed out")
            except Exception as e:
                task["state"] = "failed"
                task["error"] = f"Callback dispatch failed: {e}"
                logger.error("Callback dispatch to agent '%s' failed: %s", agent_id, e)
                return json_error(502, "callback_failed",
                                   f"Failed to callback agent '{agent_id}': {e}")

        else:
            # WebSocket dispatch (existing behavior)
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
                return json_error(502, "dispatch_failed",
                                   f"Failed to dispatch task to agent '{agent_id}': {e}")

        # Audit log
        if self.audit_store is not None:
            self.audit_store.log(
                event_type=EventType.TASK_DISPATCH.value,
                actor=request.remote or "unknown",
                target=task_id,
                detail=f"agent_id={agent_id} channel={preferred_channel} query_len={len(query)}",
                success=True,
            )

        # Start timeout timer after successful dispatch
        self._schedule_task_timeout(task_id)

        return web.json_response({
            "task_id": task_id,
            "agent_id": agent_id,
            "state": task["state"],
            "query": query,
            "channel": preferred_channel,
            "created_at": task["created_at"],
        }, status=202)

    async def handle_get_task(self, request: web.Request) -> web.Response:
        """GET /v1/tasks/{task_id} — get task status and result."""
        task_id = request.match_info["task_id"]
        tenant = request["tenant"] if "tenant" in request else None
        task = self._tasks.get(task_id)
        if task is None:
            return json_error(404, "task_not_found",
                               f"Task '{task_id}' not found")
        # Tenant isolation: if tenant is set, only show tasks for that tenant
        task_tenant = task.get("tenant", "")
        if tenant and task_tenant and task_tenant != tenant:
            return json_error(404, "task_not_found",
                               f"Task '{task_id}' not found")
        return web.json_response(task)

    async def handle_callback_result(self, request: web.Request) -> web.Response:
        """POST /v1/tasks/{task_id}/callback-result — receive callback result.

        Called by callback-mode agents to submit their task results.
        Expects ``callback_token`` as Bearer token in Authorization header.
        """
        task_id = request.match_info["task_id"]
        task = self._tasks.get(task_id)
        if task is None:
            return json_error(404, "task_not_found",
                               f"Task '{task_id}' not found")

        if task.get("state") in ("completed", "failed", "cancelled"):
            return json_error(400, "task_already_finalized",
                               f"Task '{task_id}' is already in state '{task['state']}'")

        # Verify callback token against the assigned agent
        agent_id = task.get("agent_id", "")
        card = self.store.get_agent(agent_id)
        if card is None:
            return json_error(404, "agent_not_found",
                               f"Agent '{agent_id}' not found")

        expected_token = (card.get("callback_token", "") or "")
        if expected_token:
            auth_header = request.headers.get("Authorization", "")
            if not auth_header.startswith("Bearer "):
                return json_error(401, "missing_token", "Missing Bearer token")
            actual_token = auth_header[len("Bearer "):].strip()
            if actual_token != expected_token:
                return json_error(403, "invalid_token", "Invalid callback token")

        try:
            body = await request.json()
        except json.JSONDecodeError:
            return json_error(400, "invalid_json", "Invalid JSON body")

        result = body.get("result")
        error = body.get("error")
        state = body.get("state", "completed")

        if state == "failed" or error:
            task["state"] = "failed"
            task["error"] = error or "Callback reported failure"
        else:
            task["state"] = "completed"
            task["result"] = result

        task["updated_at"] = time.time()

        logger.info(
            "Callback result for task %s from agent '%s': state=%s",
            task_id, agent_id, task["state"],
        )

        # Cancel the timeout timer now that we have a result
        self._cancel_task_timeout(task_id)

        # Reconcile with kanban TaskStore if this was a dispatched task
        if task["state"] == "completed":
            _maybe_update_kanban(
                self.task_store, self._dispatched_ws_tasks,
                task_id, "completed",
                result=result,
                error=None,
            )
        else:
            _maybe_update_kanban(
                self.task_store, self._dispatched_ws_tasks,
                task_id, "failed",
                result=None,
                error=error or "Callback reported failure",
            )

        return web.json_response({"status": "ok", "task_id": task_id, "state": task["state"]})

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
        tenant = request["tenant"] if "tenant" in request else None

        all_tasks = list(self._tasks.values())
        # Sort newest first
        all_tasks.sort(key=lambda t: t.get("created_at", 0), reverse=True)

        # Apply filters
        if tenant:
            # Tenant isolation: only show tasks for this tenant
            all_tasks = [t for t in all_tasks if t.get("tenant", "") == tenant]
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

        # Tenant filter from auth middleware; fall back to query param
        tenant = request["tenant"] if "tenant" in request else request.query.get("tenant", None)

        card = self.store.get_agent(agent_id, tenant=tenant)
        if card is None:
            return json_error(404, "agent_not_found",
                               f"Agent '{agent_id}' not found")

        # v1.0 AgentCard: URL is in supported_interfaces (top-level `url` is removed)
        target_url = card.get("url") or (
            card.get("supported_interfaces", [{}])[0].get("url", "") if card.get("supported_interfaces") else ""
        )
        if not target_url:
            return json_error(
                400, "agent_not_routable",
                f"Agent '{agent_id}' has no URL configured",
            )

        return web.json_response({
            "message": f"Task proxied to agent '{agent_id}'",
            "agent_id": agent_id,
            "target_url": target_url,
        })


# NOTE: Error handling middleware is imported from errors.py as
# ``_unified_error_middleware``.  The ``timeout_middleware`` is also
# available from the same module for request-level timeout control.
#
# The old inline ``_error_middleware`` and ``_json_error`` have been
# replaced by the unified implementation in errors.py, which returns
# the enhanced format ``{error, detail, request_id, timestamp, extra}``.


# ---------------------------------------------------------------------------
# AdminHandler — Admin REST API (requires registry:admin scope)
# ---------------------------------------------------------------------------


class AdminHandler:
    """Handler for ``/admin/*`` endpoints — admin operations on OAuth clients and audit log.

    All endpoints require ``registry:admin`` scope.
    Only registered when ``auth_enabled=True``.
    """

    def __init__(self, auth_store: Store,
                 audit_store: Optional[AuditStore] = None) -> None:
        self.auth_store = auth_store
        self.audit_store = audit_store

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

        if self.audit_store is not None:
            self.audit_store.log(
                event_type=EventType.CLIENT_CREATE.value,
                actor=request.remote or "unknown",
                target=result["client_id"],
                detail=f"agent_card_id={agent_card_id} description={description}",
                success=True,
            )

        client = self.auth_store.get_client(result["client_id"])
        return web.json_response(
            {
                "client_id": result["client_id"],
                "client_secret": result["client_secret"],
                "agent_card_id": client.agent_card_id if client else agent_card_id,
                "scopes": client.allowed_scopes if client else (allowed_scopes or list(SCOPES.keys())),
                "tenant": client.tenant if client else "",
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
        tenant = request.query.get("tenant", None)
        clients = self.auth_store.list_clients(tenant=tenant)
        return web.json_response(clients)

    async def handle_list_audit(self, request: web.Request) -> web.Response:
        """GET /admin/audit — query audit log with optional filters.

        Query params:
            event_type: Filter by event type (CLIENT_CREATE, AGENT_REGISTER, etc.)
            actor:      Filter by actor (substring match, case-insensitive)
            since:      Unix timestamp — only events at or after this time
            until:      Unix timestamp — only events before this time
            limit:      Max results (default 100, max 1000)
            offset:     Pagination offset (default 0)

        Returns::

            {
                "total": 42,
                "limit": 100,
                "offset": 0,
                "events": [...],
                "stats": {...}
            }
        """
        if self.audit_store is None:
            return web.json_response(
                {"error": "audit_disabled", "detail": "Audit logging is not configured"},
                status=404,
            )

        try:
            limit = min(int(request.query.get("limit", 100)), 1000)
        except (ValueError, TypeError):
            limit = 100
        try:
            offset = max(int(request.query.get("offset", 0)), 0)
        except (ValueError, TypeError):
            offset = 0

        event_type = request.query.get("event_type") or None
        actor = request.query.get("actor") or None

        since = None
        raw_since = request.query.get("since")
        if raw_since:
            try:
                since = float(raw_since)
            except (ValueError, TypeError):
                pass

        until = None
        raw_until = request.query.get("until")
        if raw_until:
            try:
                until = float(raw_until)
            except (ValueError, TypeError):
                pass

        events = self.audit_store.query(
            event_type=event_type,
            actor=actor,
            since=since,
            until=until,
            limit=limit,
            offset=offset,
        )

        total = self.audit_store.count(
            event_type=event_type,
            actor=actor,
            since=since,
            until=until,
        )

        stats = self.audit_store.stats() if not (event_type or actor or since or until) else {}

        return web.json_response({
            "total": total,
            "limit": limit,
            "offset": offset,
            "events": [e.to_dict() for e in events],
            "stats": stats,
        })

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

        if self.audit_store is not None:
            self.audit_store.log(
                event_type=EventType.CLIENT_DELETE.value,
                actor=request.remote or "unknown",
                target=client_id,
                detail="Deleted by admin",
                success=True,
            )

        return web.json_response({
            "message": "Client deleted successfully",
            "client_id": client_id,
        })

    async def handle_get_agent_stats(self, request: web.Request) -> web.Response:
        """GET /admin/agent-stats — get agent statistics, optionally filtered by tenant.

        Query params:
            tenant: If set, only return stats for this tenant.
                When omitted (or empty), return stats for all agents.

        Returns::

            {
                "tenant": "tenant1" | null,
                "totalAgents": 42,
                "aliveAgents": 38,
                "staleAgents": 4
            }
        """
        # Explicit ?tenant query param; None/empty means all tenants
        tenant = request.query.get("tenant", None)
        stats = self.auth_store.stats(tenant=tenant)
        response = {
            "totalAgents": stats["totalAgents"],
            "aliveAgents": stats["aliveAgents"],
            "staleAgents": stats["staleAgents"],
        }
        if tenant:
            response["tenant"] = tenant
        return web.json_response(response)


# ---------------------------------------------------------------------------


async def _cleanup_task(app: web.Application) -> None:
    """Background task to purge stale agents, audit events, and update Prometheus gauges."""
    store: Store = app["store"]
    handler: RegistryHandler = app.get("handler")
    audit_store: Optional[AuditStore] = app.get("audit_store")
    try:
        while True:
            await asyncio.sleep(CLEANUP_INTERVAL)
            try:
                purged = store.purge_stale()
                if purged:
                    logger.info("Cleanup: purged %d stale agent(s)", purged)

                # Purge old audit events
                if audit_store:
                    audit_purged = audit_store.purge_old()
                    if audit_purged:
                        logger.info("Cleanup: purged %d old audit event(s)", audit_purged)

                # Update agent Prometheus gauges
                stats = store.stats()
                update_agent_gauges(stats["aliveAgents"], stats["staleAgents"])

                # Update WS connection gauge
                if handler:
                    update_ws_connections(len(handler._ws_connections))

                # Also clean up stale WS connections
                if handler:
                    stale_ws = [
                        aid for aid, ws in handler._ws_connections.items()
                        if ws.closed
                    ]
                    for aid in stale_ws:
                        handler._ws_connections.pop(aid, None)
                        logger.info("Cleaned up stale WS for '%s'", aid)
                    # Re-update after cleanup
                    if stale_ws:
                        update_ws_connections(len(handler._ws_connections))
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
    flow_control: Optional[Any] = None,
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

    assignee = dispatched_tasks.get(task_id)

    if status in ("completed", "success"):
        try:
            task_store.update_task_status(
                task_id, TaskStatus.COMPLETED.value,
                result=json.dumps(result) if isinstance(result, dict) else result,
            )
            # Notify flow control
            if flow_control is not None and assignee:
                flow_control.on_task_completed(assignee)
                flow_control.on_task_departed(assignee)
            logger.info("Kanban task %s → completed (via WS task_result)", task_id)
        except Exception:
            logger.exception("Failed to complete kanban task '%s' from WS result", task_id)
    elif status in ("failed", "error"):
        try:
            task_store.update_task_status(
                task_id, TaskStatus.FAILED.value,
                result=error or str(result) if result else "Agent reported failure",
            )
            # Notify flow control
            if flow_control is not None and assignee:
                flow_control.on_task_failed(assignee)
                flow_control.on_task_departed(assignee)
            logger.info("Kanban task %s → failed (via WS task_result)", task_id)
        except Exception:
            logger.exception("Failed to fail kanban task '%s' from WS result", task_id)


async def _maybe_dispatch_pending(
    task_store: Optional[TaskStore],
    ws: web.WebSocketResponse,
    agent_id: str,
    dispatched_ws_tasks: Optional[Dict[str, str]] = None,
    flow_control: Optional[Any] = None,
) -> None:
    """On reconnection, find blocked tasks assigned to *agent_id*
    and re-dispatch them over the fresh WebSocket.

    Args:
        task_store: Kanban TaskStore (V2).
        ws: The agent's WebSocket connection.
        agent_id: Registered agent name.
        dispatched_ws_tasks: Optional shared dict from the Dispatcher
            so re-dispatched tasks are tracked for result reconciliation.
        flow_control: Optional FlowController to check before re-dispatching.
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
            # Flow control gate
            if flow_control is not None and not flow_control.can_dispatch(agent_id):
                logger.debug(
                    "Flow control blocked re-dispatch of task '%s' to agent '%s'",
                    task.id, agent_id,
                )
                return

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
                # Notify flow control
                if flow_control is not None:
                    flow_control.on_task_dispatched(agent_id)
                    flow_control.on_task_arrived(agent_id)
                logger.info(
                    "Re-dispatched pending task '%s' to reconnected agent '%s'",
                    task.id, agent_id,
                )
            except Exception as e:
                # Notify flow control about failure
                if flow_control is not None:
                    flow_control.on_task_failed(agent_id)
                    flow_control.on_task_departed(agent_id)
                logger.error("Failed to re-dispatch task '%s': %s", task.id, e)
    except Exception:
        logger.exception("Error checking pending tasks for agent '%s'", agent_id)


# ---------------------------------------------------------------------------
# CORS middleware
# ---------------------------------------------------------------------------


def cors_middleware_factory(allowed_origins: str = "*") -> callable:
    """Create a CORS middleware that handles cross-origin requests.

    Adds Access-Control-* headers to every response and short-circuits
    OPTIONS preflight requests.

    Args:
        allowed_origins: Comma-separated list of origins, or ``"*"`` for all.
    """
    origins = [o.strip() for o in allowed_origins.split(",") if o.strip()]

    @middleware
    async def _cors_middleware(request: web.Request, handler: callable) -> web.StreamResponse:
        # Determine origin for this request
        request_origin = request.headers.get("Origin", "")
        header_origin = "*" if "*" in origins or not request_origin else (
            request_origin if request_origin in origins else origins[0] if origins else "*"
        )

        # Handle preflight
        if request.method == "OPTIONS":
            resp = web.Response(status=204, headers={
                "Access-Control-Allow-Origin": header_origin,
                "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, PATCH, OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type, Authorization, X-Request-ID",
                "Access-Control-Max-Age": "86400",
            })
            return resp

        try:
            response = await handler(request)
        except web.HTTPException as exc:
            response = exc

        # Add CORS headers to the response (only add_headers-capable responses)
        if hasattr(response, "headers"):
            response.headers["Access-Control-Allow-Origin"] = header_origin
            response.headers["Access-Control-Expose-Headers"] = "X-Request-ID, Retry-After"
            response.headers["Vary"] = "Origin"

        return response

    return _cors_middleware


def create_app(
    data_dir: str = "~/.simple-a2a-registry",
    base_url: str = "http://localhost:8321",
    board_path: Optional[str] = None,
    *,
    config: Optional[Config] = None,
    auth_enabled: bool = False,
    bootstrap_secret: Optional[str] = None,
    dispatcher_enabled: bool = True,
    dispatcher_interval: int = 5,
    claim_ttl: int = 900,
    failure_limit: int = 3,
    workspaces_root: Optional[str] = None,
    host: str = "0.0.0.0",
    port: int = 8321,
    user_session_enabled: bool = False,
) -> web.Application:
    """Create and configure the aiohttp web application.

    Args:
        data_dir: Path to persistent data directory.
        base_url: Public base URL for this registry.
        board_path: Database path for the V2 orchestration board.
            Defaults to ``<data_dir>/board.db``.
        config: Optional :class:`Config` instance.  When provided, uses
            ``create_engine(config.database)`` to initialise *both* the
            registry ``Store`` and orchestration ``TaskStore`` with the
            configured database engine (SQLite or MySQL).  When omitted,
            the legacy ``data_dir`` path is used for both stores.
        dispatcher_enabled: Whether to start the background Dispatcher.
        dispatcher_interval: Dispatcher poll interval in seconds.
        claim_ttl: Claim lock TTL in seconds (default 900 / 15 min).
        failure_limit: Global default retry limit.
        workspaces_root: Root directory for scratch workspaces.
            Defaults to ``<data_dir>/workspaces``.

    Returns:
        Configured :class:`aiohttp.web.Application`.
    """
    # Create shared engine (Config-driven) or fall back to legacy data_dir
    if config is not None:
        engine = create_engine(config.database)
        engine.connect()
        _shared_engine = engine
        logger.info(
            "Using database engine: %s (driver=%s)",
            config.database.sqlite_path if config.database.driver == "sqlite" else config.database.mysql_dsn,
            config.database.driver,
        )
        update_db_pool_size(config.database.pool_size)

        # Create schema tables
        _maybe_create_registry_schema(_shared_engine)
        _maybe_create_board_schema(_shared_engine)

        # Audit logging schema and store
        retention_days = config.audit.retention_days if config is not None else 90
        _maybe_create_audit_schema(_shared_engine, retention_days)
        audit_store = AuditStore(_shared_engine, retention_days=retention_days)
        logger.info("Audit store initialised (retention=%d days)", retention_days)
    else:
        _shared_engine = None
        resolved = Path(data_dir).expanduser().resolve()
        _legacy_engine = SQLiteEngine(str(resolved / "registry.db"))
        _legacy_engine.connect()
        _maybe_create_audit_schema(_legacy_engine, 90)
        audit_store = AuditStore(_legacy_engine, retention_days=90)
        logger.info("Audit store initialised (retention=90 days, legacy path)")

    store = Store(_shared_engine if _shared_engine is not None else data_dir, bootstrap_secret=bootstrap_secret)
    handler = RegistryHandler(store, base_url, audit_store=audit_store)

    # OAuth 2.1 Authentication — generate keys EARLY so UserHandler can create Bearer tokens on login
    # Generate RSA key pair at startup (RS256 primary, HS256 dev fallback)
    if auth_enabled:
        private_key, public_key = _generate_rsa_keypair()
        algorithm = "RS256"
        logger.info("OAuth 2.1 auth enabled — generated RS256 key pair")
    else:
        private_key, public_key = "dev-secret-not-for-production", "dev-secret-not-for-production"
        algorithm = "HS256"
        logger.info("OAuth 2.1 auth disabled — dev mode (HS256 fallback)")

    # User authentication — password login, session management, user CRUD
    if _shared_engine is not None:
        user_store = UserStore(_shared_engine)
    else:
        # Share the same SQLite database file as the main Store
        resolved = Path(data_dir).expanduser().resolve()
        _user_engine = SQLiteEngine(str(resolved / "registry.db"))
        _user_engine.connect()
        user_store = UserStore(_user_engine)
    session_manager = SessionManager()
    user_handler = UserHandler(user_store, session_manager, audit_store=audit_store,
                                auth_private_key=private_key, auth_algorithm=algorithm)

    # V2 Orchestration Engine
    if _shared_engine is not None:
        task_store = TaskStore(_shared_engine)
    else:
        if board_path is None:
            board_path = str(Path(data_dir).expanduser() / "board.db")
        task_store = TaskStore(board_path)
    orch_handler = OrchestrationHandler(task_store, registry_store=store)

    # V2 Workspace Manager
    if workspaces_root is None:
        workspaces_root = str(Path(data_dir).expanduser() / "workspaces")
    ws_mgr = WorkspaceManager(workspaces_root)

    auth_handler = AuthHandler(
        store,  # same Store instance handles both registry and auth persistence
        private_key=private_key,
        algorithm=algorithm,
        base_url=base_url,
        audit_store=audit_store,
    )

    # Wire store to handler for WebSocket client auth and admin endpoints
    handler.auth_store = store
    handler._auth_public_key = public_key
    handler._auth_algorithm = algorithm
    handler._auth_enabled = auth_enabled

    # Admin WebSocket Hub — live task updates for the Admin SPA
    admin_ws_hub = AdminWSHub(task_store=task_store)
    admin_ws_hub._auth_public_key = public_key
    admin_ws_hub._auth_algorithm = algorithm
    admin_ws_hub._auth_enabled = auth_enabled
    logger.info("Admin WebSocket Hub ready (%d task subscriptions)",
                admin_ws_hub.active_connections)

    # Wire OrchestrationHandler broadcast callback to AdminWSHub
    async def _broadcast_task_event(event_type: str, data: dict) -> None:
        """Bridge orch_handler._broadcast_fn → AdminWSHub.broadcast_task_update."""
        task_id = data.get("id", "")
        await admin_ws_hub.broadcast_task_update(task_id, event_type, data)

    orch_handler._broadcast_fn = _broadcast_task_event
    logger.info("OrchestrationHandler broadcast callback wired to AdminWSHub")

    # Wire RegistryHandler (agent WS) progress callback to AdminWSHub
    async def _broadcast_agent_progress(
        task_id: str,
        progress: float,
        message: Optional[str] = None,
        status: str = "working",
    ) -> None:
        """Bridge agent task_progress → AdminWSHub.broadcast_task_progress."""
        await admin_ws_hub.broadcast_task_progress(
            task_id, progress, message, status,
        )

    handler._broadcast_progress_fn = _broadcast_agent_progress
    logger.info("RegistryHandler agent progress callback wired to AdminWSHub")

    # P2.4: Anomaly Scanner — background orphan/timeout detection every 60s
    anomaly_scanner = AnomalyScanner(
        task_store=task_store,
        ws_connections=handler._ws_connections,
        admin_ws_hub=admin_ws_hub,
        interval=60,
    )

    # P5a.2: SLA Statistics — periodic success-rate gauge updates every 60s
    sla_calculator = SlaCalculator(task_store=task_store)
    sla_updater = SlaUpdater(calculator=sla_calculator, interval=60)

    # V2 Dispatcher (background worker dispatch)
    disp_config = DispatcherConfig(
        poll_interval=dispatcher_interval,
        claim_ttl=claim_ttl,
        failure_limit=failure_limit,
    )
    dispatcher = Dispatcher(task_store, ws_mgr, disp_config,
                               ws_connections=handler._ws_connections,
                               registry_store=store,
                               http_session=handler._session) if dispatcher_enabled else None

    # Wire up cross-references for WebSocket ↔ Kanban integration
    handler.task_store = task_store
    if dispatcher:
        handler._dispatched_ws_tasks = dispatcher._dispatched_ws_tasks

    app = web.Application(middlewares=[
        cors_middleware_factory(
            allowed_origins=config.server.cors_origins if config is not None else "*",
        ),
        request_id_middleware_factory(),
        _unified_error_middleware,
        timeout_middleware,
        _auth_middleware_factory(
            store,
            enabled=auth_enabled,
            public_key=public_key,
            algorithm=algorithm,
            audit_store=audit_store,
            session_manager=session_manager,
        ),
        metrics_middleware_factory(),
        rate_limit_middleware_factory(
            enabled=config.rate_limit.enabled if config is not None else False,
            default_unauthenticated=config.rate_limit.default_unauthenticated if config is not None else 60,
            default_authenticated=config.rate_limit.default_authenticated if config is not None else 300,
            storage=config.rate_limit.storage if config is not None else "memory",
            whitelist=config.rate_limit.whitelist if config is not None else [],
            engine=_shared_engine,
        ),
        user_session_middleware_factory(session_manager, enabled=user_session_enabled),
    ])
    app["store"] = store
    app["handler"] = handler
    app["auth_store"] = store
    app["auth_handler"] = auth_handler
    app["user_store"] = user_store
    app["session_manager"] = session_manager
    app["user_handler"] = user_handler
    app["task_store"] = task_store
    app["orch_handler"] = orch_handler
    app["ws_mgr"] = ws_mgr
    app["dispatcher"] = dispatcher
    app["config"] = config
    app["audit_store"] = audit_store
    app["admin_ws_hub"] = admin_ws_hub
    app["anomaly_scanner"] = anomaly_scanner
    app["sla_calculator"] = sla_calculator
    app["sla_updater"] = sla_updater

    # HTTP client session for callback dispatch (created on startup, closed on cleanup)
    async def _init_client_session(app_: web.Application) -> None:
        handler._session = aiohttp.ClientSession()

    async def _close_client_session(app_: web.Application) -> None:
        if handler._session is not None:
            await handler._session.close()
            handler._session = None

    # P2.4: Start/stop the anomaly scanner background loop
    async def _start_anomaly_scanner(app_: web.Application) -> None:
        scanner: AnomalyScanner = app_["anomaly_scanner"]
        asyncio.create_task(scanner.run())
        logger.info("AnomalyScanner background task started")

    async def _stop_anomaly_scanner(app_: web.Application) -> None:
        scanner: AnomalyScanner = app_["anomaly_scanner"]
        scanner.stop()
        logger.info("AnomalyScanner background task stopped")

    # P5a.2: Start/stop the SLA updater background loop
    async def _start_sla_updater(app_: web.Application) -> None:
        updater: SlaUpdater = app_["sla_updater"]
        asyncio.create_task(updater.run())
        logger.info("SlaUpdater background task started")

    async def _stop_sla_updater(app_: web.Application) -> None:
        updater: SlaUpdater = app_["sla_updater"]
        updater.stop()
        logger.info("SlaUpdater background task stopped")

    app.on_startup.append(_init_client_session)
    app.on_startup.append(_start_anomaly_scanner)
    app.on_cleanup.append(_stop_anomaly_scanner)
    app.on_startup.append(_start_sla_updater)
    app.on_cleanup.append(_stop_sla_updater)
    app.on_cleanup.append(_close_client_session)

    # Health / well-known
    app.router.add_get("/health", handler.handle_health)
    app.router.add_get("/.well-known/agent-card.json", handler.handle_well_known)

    # Prometheus metrics endpoint (conditional on config)
    if config is not None and config.monitoring.metrics_enabled:
        app.router.add_get(config.monitoring.metrics_path, handle_metrics)
        logger.info("Metrics endpoint enabled at %s", config.monitoring.metrics_path)

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

    # Toggle agent disabled status — requires agent:admin
    app.router.add_post(
        "/v1/agents/{agent_id}/toggle",
        require_scope("agent:admin")(handler.handle_toggle_agent),
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

    # Callback result endpoint — called by callback-mode agents
    app.router.add_post(
        "/v1/tasks/{task_id}/callback-result",
        handler.handle_callback_result,
    )

    # Static dashboard — a2a-admin React SPA
    static_dir = Path(__file__).parent.parent / "data" / "web"
    if static_dir.is_dir():
        # Serve built assets (JS, CSS, images)
        assets_dir = static_dir / "assets"
        if assets_dir.is_dir():
            app.router.add_static("/assets", assets_dir, name="static_assets")

        async def _dashboard(request: web.Request) -> web.StreamResponse:
            html_path = static_dir / "index.html"
            html = html_path.read_text(encoding="utf-8")
            if auth_enabled:
                # Inject auth config: client checks sessionStorage for token
                config_script = (
                    "<script>\n"
                    "window.__AUTH_CONFIG = { enabled: true };\n"
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

    # V2 Swarm routes (parallel topology on top of v2/tasks)
    swarm_handler = SwarmHandler(task_store)
    register_swarm_routes(app, swarm_handler)
    app["swarm_handler"] = swarm_handler
    # Wire SwarmHandler broadcast callback to AdminWSHub (shares the same bridge)
    swarm_handler._broadcast_fn = _broadcast_task_event

    # Admin WebSocket — real-time task updates via AdminWSHub
    app.router.add_get("/v2/ws/admin", admin_ws_hub.register)

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
        admin_handler = AdminHandler(store, audit_store=audit_store)
        app.router.add_post(
            "/admin/clients",
            require_scope("registry:admin")(admin_handler.handle_create_client),
        )
        app.router.add_get(
            "/admin/clients",
            require_scope("registry:admin")(admin_handler.handle_list_clients),
        )
        app.router.add_get(
            "/admin/audit",
            require_scope("registry:admin")(admin_handler.handle_list_audit),
        )
        app.router.add_delete(
            "/admin/clients/{client_id}",
            require_scope("registry:admin")(admin_handler.handle_delete_client),
        )
        app.router.add_get(
            "/admin/agent-stats",
            require_scope("registry:admin")(
                admin_handler.handle_get_agent_stats
            ),
        )

        # P5a.2: SLA statistics endpoint
        async def _handle_sla_stats(request: web.Request) -> web.Response:
            calc: SlaCalculator = app["sla_calculator"]
            snap = calc.snapshot()
            return web.json_response({
                "captured_at": snap.captured_at,
                "windows": [
                    {
                        "window": w.window_label,
                        "completed": w.completed,
                        "failed": w.failed,
                        "cancelled": w.cancelled,
                        "total_terminal": w.total_terminal,
                        "success_rate": w.success_rate,
                    }
                    for w in snap.windows
                ],
                "trend": {
                    "slope": snap.trend_slope,
                    "intercept": snap.trend_intercept,
                    "r_squared": snap.trend_r_squared,
                },
            })

        app.router.add_get(
            "/admin/sla",
            require_scope("registry:admin")(_handle_sla_stats),
        )

    # User authentication routes — login/logout + user management
    app.router.add_get("/login", user_handler.handle_login_page)
    app.router.add_post("/api/login", user_handler.handle_login)
    app.router.add_post("/api/logout", user_handler.handle_logout)
    app.router.add_get("/api/me", user_handler.handle_get_current_user)
    app.router.add_get(
        "/admin/users",
        require_role("admin")(user_handler.handle_list_users),
    )
    app.router.add_post(
        "/admin/users",
        require_role("admin")(user_handler.handle_create_user),
    )
    app.router.add_put(
        "/admin/users/{username}",
        require_role("admin")(user_handler.handle_update_user),
    )
    app.router.add_delete(
        "/admin/users/{username}",
        require_role("admin")(user_handler.handle_delete_user),
    )

    # Background cleanup + Dispatcher
    cleanup_task_ref: list[asyncio.Task] = []

    async def _startup_checks(app: web.Application) -> None:
        """启动前置检查：DB连接、端口可用性、RSA密钥。

        在 on_startup 中执行，确保启动前所有依赖就绪。
        """
        logger.info("Running startup preflight checks…")

        # 1. DB connectivity — execute a simple query
        store: Store = app.get("store")
        if store:
            try:
                stats = store.stats()
                logger.info("  [OK] DB connectivity — %d agent(s) registered", stats["totalAgents"])
            except Exception as e:
                logger.error("  [FAIL] DB connectivity check: %s", e)
                raise RuntimeError(f"Startup check failed: DB unreachable — {e}") from e

        # 2. Port availability (warning only — the actual bind will fail if taken)
        host = app.get("_host", "0.0.0.0")
        port = app.get("_port", 8321)
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(1)
            result = sock.connect_ex((host, port))
            sock.close()
            if result == 0:
                logger.warning("Port %s:%d is already in use — startup may fail", host, port)
            else:
                logger.info("  [OK] Port %s:%d is available", host, port)
        except OSError:
            logger.warning("  [?] Port check skipped (host=%s)", host)

        # 3. Auth / RSA check
        handler: RegistryHandler = app.get("handler")
        if handler and handler._auth_enabled:
            if handler._auth_public_key:
                logger.info("  [OK] RSA key pair generated (%d bits)", 2048)
            else:
                logger.warning("  [?] Auth enabled but public key empty — may affect JWKS endpoint")

        logger.info("Preflight checks complete — starting server.")

    async def _start_background(app: web.Application) -> None:
        # Run preflight checks first
        await _startup_checks(app)

        # V1 cleanup task
        task = asyncio.create_task(_cleanup_task(app))
        cleanup_task_ref.append(task)
        # V2 Dispatcher
        disp: Optional[Dispatcher] = app.get("dispatcher")
        if disp:
            asyncio.create_task(disp.run())

    async def _stop_background(app: web.Application) -> None:
        """优雅关闭：完成进行中的WS消息、关闭连接池、Flush日志。"""
        logger.info("Initiating graceful shutdown…")

        # 1. Notify all connected WebSocket agents
        handler: RegistryHandler = app.get("handler")
        ws_connections = getattr(handler, "_ws_connections", {})
        close_tasks = []
        for aid, ws in list(ws_connections.items()):
            if ws and not ws.closed:
                close_tasks.append(
                    ws.send_json({"type": "close", "reason": "registry_shutdown"})
                )
        if close_tasks:
            done, _ = await asyncio.wait(close_tasks, timeout=2.0)
            logger.info(
                "  Notified %d/%d WS agents (%d timed out)",
                len(done), len(close_tasks), len(close_tasks) - len(done),
            )

        # 2. Cancel cleanup task
        if cleanup_task_ref:
            task = cleanup_task_ref[0]
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            logger.info("  Cleanup task cancelled")

        # 3. Stop Dispatcher
        disp: Optional[Dispatcher] = app.get("dispatcher")
        if disp:
            disp.stop()
            logger.info("  Dispatcher stopped")

        # 4. Close Admin WebSocket Hub (notify all connected admin clients)
        admin_hub: Optional[AdminWSHub] = app.get("admin_ws_hub")
        if admin_hub:
            await admin_hub.close_all()
            logger.info("  Admin WebSocket Hub closed")

        # 5. Close TaskStore
        ts: TaskStore = app.get("task_store")
        if ts:
            try:
                ts.close()
                logger.info("  TaskStore closed")
            except Exception as e:
                logger.warning("  TaskStore close error: %s", e)

        # 6. Close Store (DB connection pool)
        store: Store = app.get("store")
        if store:
            try:
                store.close()
                logger.info("  Store DB connection closed")
            except Exception as e:
                logger.warning("  Store close error: %s", e)

        # 7. Close WS connections
        for aid, ws in list(ws_connections.items()):
            if ws and not ws.closed:
                try:
                    await ws.close()
                except Exception:
                    pass
        ws_connections.clear()

        # 8. Flush log handlers
        for h in logger.handlers:
            try:
                h.flush()
            except Exception:
                pass
        import logging as _logging
        _logging.shutdown()

        logger.info("Graceful shutdown complete.")

    app.on_startup.append(_start_background)
    app.on_cleanup.append(_stop_background)

    # Bootstrap default admin account on first start
    if user_store:
        admin_pw = user_store.bootstrap_admin()
        if admin_pw:
            logger.info("  [BOOTSTRAP] Default admin account initialised — password logged above")
        else:
            logger.info("  [OK] Users already bootstrapped — skipping default admin creation")

    # Store host/port for startup check
    app["_host"] = host
    app["_port"] = port

    return app


# ---------------------------------------------------------------------------
# TLS / HTTPS Helpers
# ---------------------------------------------------------------------------


def _validate_tls_pair(tls_cert: str | None, tls_key: str | None) -> None:
    """Validate TLS cert/key pair completeness — both or neither.

    Raises:
        ValueError: If exactly one of *tls_cert* or *tls_key* is provided.
    """
    if bool(tls_cert) != bool(tls_key):
        if tls_cert:
            msg = "--tls-cert requires --tls-key (both or neither)"
        else:
            msg = "--tls-key requires --tls-cert (both or neither)"
        raise ValueError(msg)


def _resolve_tls_path(path: str) -> str:
    """Expand ``~``/``$HOME`` in a TLS certificate/key path."""
    return str(Path(path).expanduser().resolve())


def _create_redirect_app(host: str, tls_port: int) -> web.Application:
    """Create a minimal aiohttp app that redirects all HTTP to HTTPS.

    Every request receives a 301 redirect to ``https://<host>:<tls_port><path>``.
    """
    async def _redirect_handler(request: web.Request) -> web.Response:
        location = f"https://{host}:{tls_port}{request.rel_url.path_qs}"
        return web.Response(
            status=301,
            headers={"Location": location},
        )

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", _redirect_handler)
    return app


def _build_ssl_context(cert_path: str, key_path: str) -> ssl.SSLContext:
    """Build a hardened SSL context for the HTTPS server.

    Enforces:
    - TLS 1.2 minimum (TLS 1.3 allowed)
    - Secure cipher suites only (no NULL/MD5/DES/RC4)
    """
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(cert_path, key_path)

    # Enforce TLS 1.2 minimum
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2

    # Restrict to secure cipher suites
    ctx.set_ciphers(
        "ECDHE+AESGCM:ECDHE+CHACHA20:DHE+AESGCM:DHE+CHACHA20"
        ":!aNULL:!eNULL:!MD5:!DES:!RC4:!3DES:!PSK:!SRP"
    )

    # Enable modern TLS options
    ctx.options |= ssl.OP_NO_COMPRESSION
    if hasattr(ssl, "OP_NO_TICKET"):
        ctx.options |= ssl.OP_NO_TICKET

    return ctx


def _run_with_tls(
    app: web.Application,
    host: str,
    http_port: int,
    tls_port: int,
    tls_cert: str,
    tls_key: str,
) -> None:
    """Run aiohttp app with dual listeners:

    - HTTP on *http_port* (redirects 301 → HTTPS)
    - HTTPS on *tls_port* (serves the real app)
    """
    import asyncio

    ssl_ctx = _build_ssl_context(tls_cert, tls_key)
    redirect_app = _create_redirect_app(host, tls_port)

    async def _start() -> None:
        loop = asyncio.get_running_loop()

        # Main app runner (HTTPS)
        app_runner = web.AppRunner(app)
        await app_runner.setup()
        https_site = web.TCPSite(app_runner, host, tls_port, ssl_context=ssl_ctx)
        await https_site.start()
        logger.info("HTTPS server started on https://%s:%d", host, tls_port)

        # Redirect app runner (HTTP)
        redirect_runner = web.AppRunner(redirect_app)
        await redirect_runner.setup()
        http_site = web.TCPSite(redirect_runner, host, http_port)
        await http_site.start()
        logger.info(
            "HTTP redirect server started on http://%s:%d → https://%s:%d",
            host, http_port, host, tls_port,
        )

        # Handle graceful shutdown on SIGINT/SIGTERM
        stop = asyncio.Future()

        def _signal_handler() -> None:
            if not stop.done():
                logger.info("Shutdown signal received — stopping servers…")
                stop.set_result(None)

        try:
            loop.add_signal_handler(signal.SIGINT, _signal_handler)
            loop.add_signal_handler(signal.SIGTERM, _signal_handler)
        except NotImplementedError:
            pass  # Windows / some environments

        await stop

        # Graceful shutdown
        await app_runner.cleanup()
        await redirect_runner.cleanup()

    try:
        asyncio.run(_start())
    except KeyboardInterrupt:
        pass


def run_server(
    host: str = "0.0.0.0",
    port: int = 8321,
    data_dir: str = "~/.simple-a2a-registry",
    auth_enabled: bool = False,
    bootstrap_secret: Optional[str] = None,
    board_path: Optional[str] = None,
    dispatcher_enabled: bool = True,
    dispatcher_interval: int = 5,
    claim_ttl: int = 900,
    failure_limit: int = 3,
    workspaces_root: Optional[str] = None,
    config: Optional[Config] = None,
    tls_cert: Optional[str] = None,
    tls_key: Optional[str] = None,
) -> None:
    """Start the A2A Registry HTTP server.

    Args:
        host: Bind address.
        port: Bind port for HTTPS when TLS is enabled, or
            direct HTTP when TLS is disabled.
            HTTP redirects (301 → HTTPS) on *port+1* when TLS is active.
        data_dir: Persistent data directory.
        auth_enabled: Enable OAuth 2.1 authentication middleware.
        board_path: Database path for the V2 orchestration board.
        dispatcher_enabled: Whether to start the background Dispatcher.
        dispatcher_interval: Dispatcher poll interval in seconds.
        claim_ttl: Claim lock TTL in seconds.
        failure_limit: Global default retry limit.
        workspaces_root: Root directory for scratch workspaces.
        config: Optional :class:`Config` instance.  When provided, the
            ``config.database`` section is used to initialise the engine
            shared by ``Store`` and ``TaskStore``.
        tls_cert: Path to TLS certificate file (PEM).  When provided
            with *tls_key*, enables HTTPS on a separate port (port+1).
            HTTP on *port* redirects (301) to HTTPS.
        tls_key: Path to TLS private key file (PEM).
    """
    app = create_app(
        data_dir=data_dir,
        host=host,
        port=port,
        config=config,
        auth_enabled=auth_enabled,
        bootstrap_secret=bootstrap_secret,
        board_path=board_path,
        dispatcher_enabled=dispatcher_enabled,
        dispatcher_interval=dispatcher_interval,
        claim_ttl=claim_ttl,
        failure_limit=failure_limit,
        workspaces_root=workspaces_root,
        user_session_enabled=True,
    )

    # Validate TLS pair completeness — both or neither
    _validate_tls_pair(tls_cert, tls_key)

    if tls_cert and tls_key:
        tls_port = port
        http_redirect_port = port + 1
        tls_cert_path = _resolve_tls_path(tls_cert)
        tls_key_path = _resolve_tls_path(tls_key)

        logger.info(
            "TLS/HTTPS enabled — HTTP redirect on %s:%d → HTTPS on %s:%d",
            host, http_redirect_port, host, tls_port,
        )
        _run_with_tls(app, host, http_redirect_port, tls_port, tls_cert_path, tls_key_path)
    else:
        auth_status = "enabled" if auth_enabled else "disabled (dev)"
        logger.info(
            "Simple A2A Registry starting on %s:%s (data: %s) auth=%s",
            host, port, data_dir, auth_status,
        )
        web.run_app(app, host=host, port=port)