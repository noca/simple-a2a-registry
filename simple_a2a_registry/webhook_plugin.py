"""Webhook Plugin — connects A2A Registry events to the Webhook Delivery Engine.

Registers event hooks (:class:`Plugin` subclass) that forward events
(``task_created``, ``agent_registered``, etc.) to all matching webhook
subscriptions.

Also pushes webhook delivery status to the Admin WebSocket Hub.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import aiohttp
from aiohttp import web

from simple_a2a_registry.plugin import Plugin
from simple_a2a_registry.webhook_store import WebhookStore, WebhookDeliveryEngine, _maybe_create_webhook_schema
from simple_a2a_registry.webhook_routes import WebhookHandler, register_webhook_routes

logger = logging.getLogger("a2a_registry.webhook_plugin")


class WebhookPlugin(Plugin):
    """Plugin that forwards Registry events to webhook subscribers.

    Configuration (``config.yaml``)::

        plugins:
          webhook:
            module: simple_a2a_registry.webhook_plugin
            config: {}
    """

    def __init__(self) -> None:
        self._store: Optional[WebhookStore] = None
        self._engine: Optional[WebhookDeliveryEngine] = None
        self._http_session: Optional[aiohttp.ClientSession] = None
        self._handler: Optional[WebhookHandler] = None

    @property
    def name(self) -> str:
        return "webhook"

    @property
    def description(self) -> str:
        return "Webhook subscription engine — outbound event-driven push"

    @property
    def version(self) -> str:
        return "1.0.0"

    def load(self, config: Dict[str, Any]) -> None:
        """Validate config — nothing specific needed."""
        logger.info("WebhookPlugin loaded")

    async def init(self, app: web.Application) -> None:
        """Initialise the webhook store and delivery engine.

        Expects the aiohttp ``Application`` to have:
          - ``app["engine"]`` — the shared ``DatabaseEngine``
          - ``app["admin_ws_hub"]`` — optional ``AdminWSHub`` for delivery status
        """
        engine = app.get("engine") or app.get("_shared_engine")
        if engine is None:
            # Try to get from other known keys
            store = app.get("store")
            if store is not None and hasattr(store, "_engine"):
                engine = store._engine

        if engine is None:
            logger.warning("WebhookPlugin: no database engine found in app — skipping init")
            return

        # Create webhook schema tables
        try:
            _maybe_create_webhook_schema(engine)
            logger.info("WebhookPlugin schema initialised")
        except Exception as e:
            logger.error("WebhookPlugin schema creation failed: %s", e)
            return

        # Webhook store instance
        wh_store = WebhookStore(engine)
        self._store = wh_store
        app["webhook_store"] = wh_store

        # Admin WS hub for delivery status notifications
        admin_ws_hub = app.get("admin_ws_hub")

        # Delivery status callback -> Admin WS
        def _on_delivery_status(
            sub_id: str,
            event_type: str,
            status: str,
            detail: Dict[str, Any],
        ) -> None:
            if admin_ws_hub:
                try:
                    admin_ws_hub.broadcast_to_all({
                        "type": "webhook_delivery",
                        "subscription_id": sub_id,
                        "event_type": event_type,
                        "status": status,
                        "detail": detail,
                    })
                except Exception:
                    logger.debug("Webhook delivery WS broadcast failed", exc_info=True)

        # Create HTTP session for outbound calls
        self._http_session = aiohttp.ClientSession()

        # Delivery engine
        self._engine = WebhookDeliveryEngine(
            store=wh_store,
            http_session=self._http_session,
            on_delivery_status=_on_delivery_status,
        )

        # Webhook HTTP handler + routes
        wh_handler = WebhookHandler(wh_store)
        self._handler = wh_handler
        register_webhook_routes(app, wh_handler)

        logger.info("WebhookPlugin initialised with webhook routes")

    async def before_shutdown(self, app: web.Application) -> None:
        """Clean up the HTTP client session."""
        if self._http_session and not self._http_session.closed:
            await self._http_session.close()
            self._http_session = None
            logger.info("WebhookPlugin HTTP session closed")

    # ------------------------------------------------------------------
    # Event hooks — forward events to webhook subscribers
    # ------------------------------------------------------------------

    def _get_tenant(self, data: Optional[Dict[str, Any]]) -> str:
        """Extract tenant from event data if present."""
        if data:
            return data.get("tenant", data.get("tenant_id", ""))
        return ""

    async def _trigger_event(self, event_type: str, data: Optional[Dict[str, Any]] = None) -> None:
        """Deliver an event to matching webhook subscriptions."""
        if self._engine is None:
            return
        try:
            await self._engine.deliver_event(
                event_type=event_type,
                payload=data or {},
                tenant=self._get_tenant(data),
            )
        except Exception as e:
            logger.exception("Webhook delivery failed for event '%s': %s", event_type, e)

    async def on_agent_register(self, agent_id: str, card: Dict[str, Any]) -> None:
        await self._trigger_event("agent_registered", {"agent_id": agent_id, "card": card})

    async def on_agent_deregister(self, agent_id: str) -> None:
        await self._trigger_event("agent_deregistered", {"agent_id": agent_id})

    async def on_agent_heartbeat(self, agent_id: str) -> None:
        await self._trigger_event("agent_heartbeat", {"agent_id": agent_id})

    async def on_task_created(self, task_id: str, task_data: Dict[str, Any]) -> None:
        await self._trigger_event("task_created", {"task_id": task_id, **task_data})

    async def on_task_completed(self, task_id: str, result: Optional[Dict[str, Any]]) -> None:
        await self._trigger_event("task_completed", {"task_id": task_id, "result": result})

    async def on_token_issued(self, client_id: str, token: str) -> None:
        await self._trigger_event("token_issued", {"client_id": client_id})

    async def on_security_event(self, event) -> None:
        """Forward security events (P1 integration)."""
        await self._trigger_event("security_event", {
            "event_id": getattr(event, "event_id", ""),
            "event_type": getattr(event, "event_type", ""),
            "severity": getattr(event, "severity", ""),
            "agent_id": getattr(event, "agent_id", ""),
            "detail": getattr(event, "detail", ""),
        })