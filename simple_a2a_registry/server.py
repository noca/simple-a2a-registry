"""A2A Registry HTTP server — aiohttp-based REST API."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from aiohttp import web

from simple_a2a_registry.discovery import discover_profiles
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
            make_agent_skill(
                "heartbeat", "Heartbeat", "Send keep-alive for an agent"
            ),
        ],
    )


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


class RegistryHandler:
    """HTTP handler methods for the A2A Registry."""

    def __init__(self, store: A2ARegistryStore, base_url: str) -> None:
        self.store = store
        self.base_url = base_url.rstrip("/")
        self._started_at = time.time()

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
                "external_agents": s["externalAgents"],
                "discovered_agents": s["discoveredAgents"],
            },
        })

    async def handle_well_known(self, request: web.Request) -> web.Response:
        """GET /.well-known/agent-card.json"""
        card = _registry_card(self.base_url)
        card["version"] = REGISTRY_VERSION
        return web.json_response(card, headers={
            "Cache-Control": "public, max-age=300",
        })

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

    async def handle_unregister(self, request: web.Request) -> web.Response:
        """DELETE /v1/agents/{agent_id} — unregister an agent."""
        agent_id = request.match_info["agent_id"]

        success = self.store.unregister(agent_id)
        if not success:
            return _json_error(404, "agent_not_found", f"Agent '{agent_id}' not found")

        return web.json_response(
            {
                "message": "Agent unregistered successfully",
                "id": agent_id,
            },
        )

    async def handle_discover(self, request: web.Request) -> web.Response:
        """POST /v1/discover — trigger filesystem discovery scan."""
        profiles_home = request.query.get("profiles_home") or request.app.get("profiles_home")
        if not profiles_home:
            return _json_error(400, "config_error", "No profiles_home configured for discovery")

        try:
            cards = discover_profiles(profiles_home)
            self.store.set_discovered_agents(cards)
            logger.info("Discovery scan found %d agent(s)", len(cards))
            return web.json_response({
                "message": "Discovery complete",
                "total_discovered": len(cards),
                "agents": cards,
            })
        except Exception as e:
            logger.error("Discovery scan failed: %s", e)
            return _json_error(500, "discovery_failed", str(e))

    async def handle_proxy_task(self, request: web.Request) -> web.Response:
        """POST /v1/agents/{agent_id}/task — proxy a task to an agent."""
        agent_id = request.match_info["agent_id"]

        card = self.store.get_agent(agent_id)
        if card is None:
            return _json_error(404, "agent_not_found", f"Agent '{agent_id}' not found")

        target_url = card.get("url", "")
        if not target_url:
            return _json_error(
                400,
                "agent_not_routable",
                f"Agent '{agent_id}' has no URL configured",
            )

        return web.json_response(
            {
                "message": f"Task proxied to agent '{agent_id}'",
                "agent_id": agent_id,
                "target_url": target_url,
            }
        )


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


async def _cleanup_task(app: web.Application) -> None:
    """Background task to purge stale agents."""
    store: A2ARegistryStore = app["store"]
    try:
        while True:
            await asyncio.sleep(CLEANUP_INTERVAL)
            try:
                purged = store.purge_stale()
                if purged:
                    logger.info("Cleanup: purged %d stale agent(s)", purged)
            except Exception:
                logger.exception("Cleanup task error")
    except asyncio.CancelledError:
        logger.debug("Cleanup task cancelled")
        raise


def create_app(
    data_dir: str = "~/.simple-a2a-registry",
    profiles_home: str | None = None,
    base_url: str = "http://localhost:8321",
) -> web.Application:
    """Create and configure the aiohttp web application.

    Args:
        data_dir: Path to persistent data directory.
        profiles_home: Optional path to scan for local agent profiles.
        base_url: Public base URL for this registry.

    Returns:
        Configured :class:`aiohttp.web.Application`.
    """
    store = A2ARegistryStore(data_dir)
    handler = RegistryHandler(store, base_url)

    app = web.Application()
    app["store"] = store
    app["profiles_home"] = profiles_home

    # Health / well-known
    app.router.add_get("/health", handler.handle_health)
    app.router.add_get("/.well-known/agent-card.json", handler.handle_well_known)

    # Agent CRUD
    app.router.add_get("/v1/agents", handler.handle_list_agents)
    app.router.add_get("/v1/agents/{agent_id}", handler.handle_get_agent)
    app.router.add_post("/v1/agents", handler.handle_register)
    app.router.add_delete("/v1/agents/{agent_id}", handler.handle_unregister)

    # Heartbeat
    app.router.add_post("/v1/agents/{agent_id}/heartbeat", handler.handle_heartbeat)

    # Discovery
    app.router.add_post("/v1/discover", handler.handle_discover)

    # Task proxy (experimental / future)
    app.router.add_post("/v1/agents/{agent_id}/task", handler.handle_proxy_task)

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