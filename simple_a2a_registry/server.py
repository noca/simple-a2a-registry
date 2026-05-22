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
    return make_agent_card(
        agent_id=REGISTRY_AGENT_ID,
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

    def __init__(self, store: A2ARegistryStore, base_url: str) -> None:
        self.store = store
        self.base_url = base_url.rstrip("/")
        self._started_at = time.time()

        # WebSocket connections: agent_id -> WebSocketResponse
        self._ws_connections: Dict[str, web.WebSocketResponse] = {}

        # Task store: task_id -> {"state":..., "query":..., "result":..., ...}
        self._tasks: Dict[str, Dict[str, Any]] = {}

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
        """POST /v1/agents"""
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

        return web.json_response(
            {
                "message": "Agent registered successfully",
                "id": agent_id,
                "card": card,
            },
            status=201,
        )

    async def handle_unregister(self, request: web.Request) -> web.Response:
        """DELETE /v1/agents/{agent_id}"""
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
        """
        agent_id = request.match_info["agent_id"]

        # Verify the agent is registered
        card = self.store.get_agent(agent_id)
        if card is None:
            return web.json_response(
                {"error": "agent_not_found", "detail": f"Agent '{agent_id}' not found"},
                status=404,
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

        target_url = card.get("url", "")
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
# App factory
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


def create_app(
    data_dir: str = "~/.simple-a2a-registry",
    base_url: str = "http://localhost:8321",
) -> web.Application:
    """Create and configure the aiohttp web application.

    Args:
        data_dir: Path to persistent data directory.
        base_url: Public base URL for this registry.

    Returns:
        Configured :class:`aiohttp.web.Application`.
    """
    store = A2ARegistryStore(data_dir)
    handler = RegistryHandler(store, base_url)

    app = web.Application()
    app["store"] = store
    app["handler"] = handler

    # Health / well-known
    app.router.add_get("/health", handler.handle_health)
    app.router.add_get("/.well-known/agent-card.json", handler.handle_well_known)

    # Agent CRUD
    app.router.add_get("/v1/agents", handler.handle_list_agents)
    app.router.add_get("/v1/agents/{agent_id}", handler.handle_get_agent)
    app.router.add_post("/v1/agents", handler.handle_register)
    app.router.add_delete("/v1/agents/{agent_id}", handler.handle_unregister)

    # Heartbeat
    app.router.add_post(
        "/v1/agents/{agent_id}/heartbeat", handler.handle_heartbeat
    )

    # WebSocket — persistent agent connection
    app.router.add_get(
        "/v1/agents/{agent_id}/ws", handler.handle_ws
    )

    # Task dispatch (via WS)
    app.router.add_post(
        "/v1/agents/{agent_id}/dispatch", handler.handle_dispatch
    )
    app.router.add_get(
        "/v1/tasks", handler.handle_list_tasks
    )
    app.router.add_get(
        "/v1/tasks/{task_id}", handler.handle_get_task
    )

    # Task proxy (fallback)
    app.router.add_post(
        "/v1/agents/{agent_id}/task", handler.handle_proxy_task
    )

    # Static dashboard
    static_dir = Path(__file__).parent / "static"
    if static_dir.is_dir():
        async def _dashboard(request: web.Request) -> web.StreamResponse:
            return web.FileResponse(static_dir / "index.html")
        app.router.add_get("/", _dashboard, name="dashboard")

    # Background cleanup
    cleanup_task_ref: list[asyncio.Task] = []
    async def _start_cleanup(app: web.Application) -> None:
        task = asyncio.create_task(_cleanup_task(app))
        cleanup_task_ref.append(task)
    async def _stop_cleanup(app: web.Application) -> None:
        if cleanup_task_ref:
            task = cleanup_task_ref[0]
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    app.on_startup.append(_start_cleanup)
    app.on_cleanup.append(_stop_cleanup)

    return app


def run_server(
    host: str = "0.0.0.0",
    port: int = 8321,
    data_dir: str = "~/.simple-a2a-registry",
) -> None:
    """Start the A2A Registry HTTP server.

    Args:
        host: Bind address.
        port: Bind port.
        data_dir: Persistent data directory.
    """
    app = create_app(data_dir=data_dir)
    logger.info(
        "Simple A2A Registry starting on %s:%s (data: %s)",
        host, port, data_dir,
    )
    web.run_app(app, host=host, port=port)