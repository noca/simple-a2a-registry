"""Webhook Admin REST API routes.

Endpoints
---------
POST   /admin/webhooks              — create subscription
GET    /admin/webhooks               — list subscriptions
DELETE /admin/webhooks/{id}          — delete subscription
PATCH  /admin/webhooks/{id}/toggle   — enable/disable subscription
GET    /admin/webhooks/{id}/deliveries — delivery log
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from aiohttp import web

from simple_a2a_registry.webhook_store import WebhookStore

logger = logging.getLogger("a2a_registry.webhook_routes")


def _json_error(status: int, code: str, detail: str) -> web.Response:
    return web.json_response({"error": code, "detail": detail}, status=status)


class WebhookHandler:
    """HTTP handler for webhook admin endpoints."""

    def __init__(self, store: WebhookStore) -> None:
        self._store = store

    # ------------------------------------------------------------------
    # CRUD: subscriptions
    # ------------------------------------------------------------------

    async def handle_create(self, request: web.Request) -> web.Response:
        """POST /admin/webhooks — create a subscription.

        Body::
            {
                "url": "https://example.com/webhook",
                "events": ["task_created", "agent_registered"],
                "secret": "my-secret"  # optional, auto-generated if omitted
            }
        """
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return _json_error(400, "invalid_json", "Invalid JSON body")

        url = (body.get("url") or "").strip()
        if not url:
            return _json_error(400, "validation_error", "'url' is required")

        events = body.get("events", [])
        if not isinstance(events, list) or not events:
            return _json_error(400, "validation_error", "'events' must be a non-empty list")

        secret = body.get("secret")
        if secret is not None and not isinstance(secret, str):
            return _json_error(400, "validation_error", "'secret' must be a string")

        # Validate event types against known list (basic check)
        allowed_events = {
            "task_created", "task_completed", "task_failed",
            "agent_registered", "agent_deregistered", "agent_heartbeat",
            "blackboard_update", "token_issued", "security_event",
        }
        for ev in events:
            if not isinstance(ev, str) or not ev:
                return _json_error(400, "validation_error", f"Invalid event type: {ev!r}")

        tenant = request.get("tenant", "")

        sub = self._store.create_subscription(
            url=url,
            events=events,
            secret=secret,
            tenant=tenant,
        )

        return web.json_response({
            "id": sub.id,
            "url": sub.url,
            "events": sub.events,
            "secret": sub.secret,
            "enabled": sub.enabled,
            "created_at": sub.created_at,
        }, status=201)

    async def handle_list(self, request: web.Request) -> web.Response:
        """GET /admin/webhooks — list subscriptions.

        Query params:
            tenant: Optional tenant filter.
        """
        tenant = request.query.get("tenant", "")
        subs = self._store.list_subscriptions(tenant=tenant)

        return web.json_response({
            "total": len(subs),
            "subscriptions": [
                {
                    "id": s.id,
                    "url": s.url,
                    "events": s.events,
                    "enabled": s.enabled,
                    "consecutive_failures": s.consecutive_failures,
                    "created_at": s.created_at,
                    "tenant": s.tenant,
                }
                for s in subs
            ],
        })

    async def handle_delete(self, request: web.Request) -> web.Response:
        """DELETE /admin/webhooks/{id} — delete a subscription."""
        sub_id = request.match_info["id"]
        deleted = self._store.delete_subscription(sub_id)
        if not deleted:
            return _json_error(404, "not_found", f"Subscription '{sub_id}' not found")
        return web.json_response({"message": "Deleted", "id": sub_id})

    async def handle_toggle(self, request: web.Request) -> web.Response:
        """PATCH /admin/webhooks/{id}/toggle — enable/disable a subscription.

        Body::
            {"enabled": true}
        """
        sub_id = request.match_info["id"]
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return _json_error(400, "invalid_json", "Invalid JSON body")

        enabled = body.get("enabled")
        if enabled is None or not isinstance(enabled, bool):
            return _json_error(400, "validation_error", "'enabled' (bool) is required")

        if enabled:
            ok = self._store.enable_subscription(sub_id)
        else:
            ok = self._store.disable_subscription(sub_id)

        if not ok:
            return _json_error(404, "not_found", f"Subscription '{sub_id}' not found")

        return web.json_response({"id": sub_id, "enabled": enabled})

    async def handle_deliveries(self, request: web.Request) -> web.Response:
        """GET /admin/webhooks/{id}/deliveries — list delivery logs.

        Query params:
            limit:  Max results (default 50).
            offset: Pagination offset (default 0).
        """
        sub_id = request.match_info["id"]
        try:
            limit = min(int(request.query.get("limit", 50)), 200)
        except (ValueError, TypeError):
            limit = 50
        try:
            offset = max(int(request.query.get("offset", 0)), 0)
        except (ValueError, TypeError):
            offset = 0

        # Verify subscription exists
        sub = self._store.get_subscription(sub_id)
        if sub is None:
            return _json_error(404, "not_found", f"Subscription '{sub_id}' not found")

        deliveries = self._store.list_deliveries(sub_id, limit=limit, offset=offset)
        total = self._store.count_deliveries(sub_id)

        return web.json_response({
            "total": total,
            "limit": limit,
            "offset": offset,
            "deliveries": [
                {
                    "id": d.id,
                    "subscription_id": d.subscription_id,
                    "event_type": d.event_type,
                    "status": d.status,
                    "response_code": d.response_code,
                    "attempt": d.attempt,
                    "error_message": d.error_message,
                    "delivered_at": d.delivered_at,
                }
                for d in deliveries
            ],
        })


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------


def register_webhook_routes(app: web.Application, handler: WebhookHandler) -> None:
    """Register webhook admin routes on the aiohttp application.

    Args:
        app: The aiohttp ``web.Application``.
        handler: The :class:`WebhookHandler` instance.
    """
    from simple_a2a_registry.auth import require_scope

    app.router.add_post(
        "/admin/webhooks",
        require_scope("registry:admin")(handler.handle_create),
    )
    app.router.add_get(
        "/admin/webhooks",
        require_scope("registry:admin")(handler.handle_list),
    )
    app.router.add_delete(
        "/admin/webhooks/{id}",
        require_scope("registry:admin")(handler.handle_delete),
    )
    app.router.add_patch(
        "/admin/webhooks/{id}/toggle",
        require_scope("registry:admin")(handler.handle_toggle),
    )
    app.router.add_get(
        "/admin/webhooks/{id}/deliveries",
        require_scope("registry:admin")(handler.handle_deliveries),
    )