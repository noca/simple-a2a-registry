"""Tests for Webhook Subscription Engine — CRUD + delivery + signing + retry + disable.

Covers:
  - WebhookStore: subscription CRUD, delivery logging, event matching
  - WebhookDeliveryEngine: signed delivery, retry, auto-disable
  - HMAC-SHA256 signature helpers
  - WebhookHandler HTTP endpoints via TestClient
  - Integration with the full create_app
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import tempfile
import time
from typing import Any, Dict, List, Optional

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from simple_a2a_registry.database import SQLiteEngine
from simple_a2a_registry.webhook_routes import WebhookHandler
from simple_a2a_registry.webhook_store import (
    WebhookStore,
    WebhookDeliveryEngine,
    WebhookSubscription,
    _sign_payload,
    _generate_secret,
    _maybe_create_webhook_schema,
    MAX_CONSECUTIVE_FAILURES,
)


# ===========================================================================
# Fixtures
# ===========================================================================


@pytest.fixture
def wh_engine():
    """Create a fresh SQLite engine with webhook schema in a temp file."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    engine = SQLiteEngine(tmp.name)
    engine.connect()
    _maybe_create_webhook_schema(engine)
    yield engine
    engine.close()
    try:
        os.unlink(tmp.name)
    except OSError:
        pass


@pytest.fixture
def wh_store(wh_engine):
    """Create a WebhookStore backed by an in-memory database."""
    return WebhookStore(wh_engine)


@pytest.fixture
def wh_sub(wh_store) -> WebhookSubscription:
    """Create and return a test subscription."""
    return wh_store.create_subscription(
        url="https://example.com/hook",
        events=["task_created", "agent_registered"],
        secret="test-secret-123",
        tenant="test-tenant",
    )


# ===========================================================================
# Helper tests — _sign_payload and _generate_secret
# ===========================================================================


class TestSigningHelpers:
    """HMAC-SHA256 signing and secret generation."""

    def test_sign_payload_hexdigest(self) -> None:
        """_sign_payload returns the correct HMAC-SHA256 hex digest."""
        payload = b'{"event_type": "task_created", "payload": {}}'
        secret = "my-secret"
        sig = _sign_payload(payload, secret)

        expected = hmac.new(
            secret.encode("utf-8"),
            payload,
            hashlib.sha256,
        ).hexdigest()
        assert sig == expected
        assert isinstance(sig, str)
        assert len(sig) == 64  # SHA256 hex digest = 64 chars

    def test_sign_payload_differs_for_different_secrets(self) -> None:
        """Different secrets produce different signatures."""
        payload = b'test payload'
        sig_a = _sign_payload(payload, "secret-a")
        sig_b = _sign_payload(payload, "secret-b")
        assert sig_a != sig_b

    def test_sign_payload_differs_for_different_payloads(self) -> None:
        """Different payloads produce different signatures."""
        sig_a = _sign_payload(b'payload-a', "secret")
        sig_b = _sign_payload(b'payload-b', "secret")
        assert sig_a != sig_b

    def test_generate_secret_length(self) -> None:
        """_generate_secret returns a 64-char hex string (32 bytes)."""
        secret = _generate_secret()
        assert isinstance(secret, str)
        assert len(secret) == 64

    def test_generate_secret_randomness(self) -> None:
        """Successive calls to _generate_secret produce different values."""
        secrets = {_generate_secret() for _ in range(10)}
        assert len(secrets) == 10


# ===========================================================================
# WebhookStore — Subscription CRUD
# ===========================================================================


class TestWebhookStoreSubscriptions:
    """CRUD operations for webhook subscriptions."""

    def test_create_subscription_defaults(self, wh_store) -> None:
        """create_subscription with minimal args generates id/secret."""
        sub = wh_store.create_subscription(
            url="https://example.com/hook",
            events=["task_created"],
        )
        assert sub.id.startswith("wh_")
        assert sub.url == "https://example.com/hook"
        assert sub.events == ["task_created"]
        assert sub.secret  # auto-generated
        assert len(sub.secret) == 64
        assert sub.enabled is True
        assert sub.retry_count == 0
        assert sub.consecutive_failures == 0
        assert sub.last_failure_at is None
        assert sub.created_at > 0

    def test_create_subscription_with_secret(self, wh_store) -> None:
        """create_subscription honours an explicitly provided secret."""
        sub = wh_store.create_subscription(
            url="https://example.com/hook",
            events=["agent_registered"],
            secret="explicit-secret",
        )
        assert sub.secret == "explicit-secret"

    def test_create_subscription_with_tenant(self, wh_store) -> None:
        """create_subscription stores the tenant field."""
        sub = wh_store.create_subscription(
            url="https://example.com/hook",
            events=["task_created"],
            tenant="tenant-alpha",
        )
        assert sub.tenant == "tenant-alpha"

        sub = wh_store.create_subscription(
            url="https://example.com/hook",
            events=["task_created"],
        )
        assert sub.tenant == ""

    def test_get_subscription(self, wh_store, wh_sub) -> None:
        """get_subscription returns the correct subscription by id."""
        fetched = wh_store.get_subscription(wh_sub.id)
        assert fetched is not None
        assert fetched.id == wh_sub.id
        assert fetched.url == wh_sub.url
        assert fetched.events == wh_sub.events
        assert fetched.secret == wh_sub.secret
        assert fetched.tenant == "test-tenant"

    def test_get_nonexistent_subscription(self, wh_store) -> None:
        """get_subscription returns None for a missing id."""
        assert wh_store.get_subscription("wh_nonexistent") is None

    def test_list_subscriptions(self, wh_store, wh_sub) -> None:
        """list_subscriptions returns all subscriptions."""
        wh_store.create_subscription(url="https://a.com", events=["a"])
        wh_store.create_subscription(url="https://b.com", events=["b"])
        subs = wh_store.list_subscriptions()
        assert len(subs) >= 3

    def test_list_subscriptions_filters_by_tenant(self, wh_store) -> None:
        """list_subscriptions with tenant filters correctly."""
        wh_store.create_subscription(url="https://a.com", events=["a"], tenant="t1")
        wh_store.create_subscription(url="https://b.com", events=["b"], tenant="t2")
        wh_store.create_subscription(url="https://c.com", events=["c"], tenant="t1")

        t1_subs = wh_store.list_subscriptions(tenant="t1")
        assert len(t1_subs) == 2
        for s in t1_subs:
            assert s.tenant == "t1"

        t2_subs = wh_store.list_subscriptions(tenant="t2")
        assert len(t2_subs) == 1
        assert t2_subs[0].tenant == "t2"

    def test_delete_subscription(self, wh_store, wh_sub) -> None:
        """delete_subscription removes the subscription."""
        assert wh_store.delete_subscription(wh_sub.id) is True
        assert wh_store.get_subscription(wh_sub.id) is None

    def test_delete_nonexistent_subscription(self, wh_store) -> None:
        """delete_subscription returns False for a missing id."""
        assert wh_store.delete_subscription("wh_nonexistent") is False

    def test_delete_also_removes_deliveries(self, wh_store, wh_sub) -> None:
        """Deleting a subscription cascades to its delivery records."""
        wh_store.log_delivery(
            subscription_id=wh_sub.id,
            event_type="task_created",
            payload={"task_id": "t1"},
            status="success",
        )
        assert wh_store.count_deliveries(wh_sub.id) == 1

        wh_store.delete_subscription(wh_sub.id)
        assert wh_store.count_deliveries(wh_sub.id) == 0

    def test_disable_and_enable_subscription(self, wh_store, wh_sub) -> None:
        """disable and enable update the enabled flag."""
        assert wh_sub.enabled is True

        # Disable
        assert wh_store.disable_subscription(wh_sub.id) is True
        fetched = wh_store.get_subscription(wh_sub.id)
        assert fetched is not None
        assert fetched.enabled is False

        # Re-enable (resets consecutive failures)
        assert wh_store.enable_subscription(wh_sub.id) is True
        fetched = wh_store.get_subscription(wh_sub.id)
        assert fetched is not None
        assert fetched.enabled is True
        assert fetched.consecutive_failures == 0

    def test_disable_nonexistent(self, wh_store) -> None:
        """disable_subscription returns False for missing id."""
        assert wh_store.disable_subscription("wh_gone") is False

    def test_enable_nonexistent(self, wh_store) -> None:
        """enable_subscription returns False for missing id."""
        assert wh_store.enable_subscription("wh_gone") is False

    def test_get_subscriptions_for_event_matching(self, wh_store) -> None:
        """get_subscriptions_for_event returns only matching event types."""
        sub_a = wh_store.create_subscription(
            url="https://a.com", events=["task_created", "agent_registered"]
        )
        sub_b = wh_store.create_subscription(
            url="https://b.com", events=["blackboard_update"]
        )

        results = wh_store.get_subscriptions_for_event("task_created")
        ids = {s.id for s in results}
        assert sub_a.id in ids
        assert sub_b.id not in ids

    def test_get_subscriptions_for_event_excludes_disabled(self, wh_store) -> None:
        """get_subscriptions_for_event skips disabled subscriptions."""
        sub = wh_store.create_subscription(
            url="https://a.com", events=["task_created"]
        )
        wh_store.disable_subscription(sub.id)

        results = wh_store.get_subscriptions_for_event("task_created")
        assert sub.id not in {s.id for s in results}

    def test_get_subscriptions_for_event_tenant_isolation(self, wh_store) -> None:
        """get_subscriptions_for_event respects tenant filtering."""
        wh_store.create_subscription(
            url="https://a.com", events=["task_created"], tenant="tenant-a"
        )
        wh_store.create_subscription(
            url="https://b.com", events=["task_created"], tenant="tenant-b"
        )
        results_a = wh_store.get_subscriptions_for_event("task_created", tenant="tenant-a")
        results_b = wh_store.get_subscriptions_for_event("task_created", tenant="tenant-b")
        assert len(results_a) == 1
        assert len(results_b) == 1
        assert results_a[0].tenant == "tenant-a"
        assert results_b[0].tenant == "tenant-b"


# ===========================================================================
# WebhookStore — Delivery logging
# ===========================================================================


class TestWebhookStoreDeliveries:
    """Delivery record CRUD."""

    def test_log_delivery_pending(self, wh_store, wh_sub) -> None:
        """log_delivery with status=pending sets delivered_at=None."""
        d_id = wh_store.log_delivery(
            subscription_id=wh_sub.id,
            event_type="task_created",
            payload={"task_id": "abc"},
            status="pending",
        )
        assert d_id.startswith("d_")
        deliveries = wh_store.list_deliveries(wh_sub.id)
        assert len(deliveries) == 1
        d = deliveries[0]
        assert d.status == "pending"
        assert d.delivered_at is None

    def test_log_delivery_success(self, wh_store, wh_sub) -> None:
        """log_delivery with status=success sets delivered_at."""
        d_id = wh_store.log_delivery(
            subscription_id=wh_sub.id,
            event_type="agent_registered",
            payload={"agent_id": "a1"},
            status="success",
            response_code=200,
            attempt=1,
        )
        deliveries = wh_store.list_deliveries(wh_sub.id)
        assert len(deliveries) == 1
        d = deliveries[0]
        assert d.status == "success"
        assert d.response_code == 200
        assert d.attempt == 1
        assert d.delivered_at is not None

    def test_list_deliveries_pagination(self, wh_store, wh_sub) -> None:
        """list_deliveries supports limit and offset."""
        for i in range(10):
            wh_store.log_delivery(
                subscription_id=wh_sub.id,
                event_type="task_created",
                payload={"i": i},
                status="success",
                response_code=200,
            )
        assert wh_store.count_deliveries(wh_sub.id) == 10

        page = wh_store.list_deliveries(wh_sub.id, limit=3, offset=0)
        assert len(page) == 3

    def test_log_delivery_serialises_payload(self, wh_store, wh_sub) -> None:
        """Payload is JSON-serialised in the DB and deserialised on read."""
        payload = {"task_id": "t1", "data": {"nested": [1, 2, 3]}}
        wh_store.log_delivery(
            subscription_id=wh_sub.id,
            event_type="task_created",
            payload=payload,
            status="success",
        )
        deliveries = wh_store.list_deliveries(wh_sub.id)
        assert len(deliveries) == 1
        assert deliveries[0].payload == payload


# ===========================================================================
# WebhookDeliveryEngine — signed delivery, retry, disable
# ===========================================================================


class TestWebhookDeliveryEngine:
    """Delivery engine tests with a mock HTTP endpoint."""

    @staticmethod
    async def _make_mock_app(handler) -> web.Application:
        """Create a minimal aiohttp app with a single POST endpoint."""
        app = web.Application()

        async def _endpoint(request: web.Request) -> web.Response:
            return await handler(request)

        app.router.add_post("/hook", _endpoint)
        return app

    async def _check_signature(
        self, body: bytes, signature: str, secret: str
    ) -> bool:
        """Verify HMAC-SHA256 signature matches expected."""
        expected = hmac.new(
            secret.encode("utf-8"),
            body,
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(signature, expected)

    # ------------------------------------------------------------------
    # Successful delivery
    # ------------------------------------------------------------------

    async def test_deliver_success(self, wh_store) -> None:
        """Successful delivery is logged with status=success."""
        sub = wh_store.create_subscription(
            url="http://localhost:1/hook",  # will override below
            events=["test_event"],
            secret="test-secret",
        )

        received_requests: List[Dict[str, Any]] = []

        async def mock_handler(request: web.Request) -> web.Response:
            body = await request.read()
            received_requests.append({
                "body": body,
                "headers": dict(request.headers),
            })
            return web.json_response({"ok": True}, status=200)

        mock_app = await self._make_mock_app(mock_handler)
        server = TestServer(mock_app)
        await server.start_server()

        try:
            wh_store._engine.execute(
                "UPDATE webhook_subscriptions SET url = ? WHERE id = ?",
                (f"http://localhost:{server.port}/hook", sub.id),
            )
            wh_store._engine.commit()

            async with aiohttp.ClientSession() as session:
                engine = WebhookDeliveryEngine(
                    store=wh_store,
                    http_session=session,
                )

                results = await engine.deliver_event(
                    event_type="test_event",
                    payload={"msg": "hello"},
                )

            assert len(results) == 1
            assert results[0]["status"] == "success"
            assert results[0]["response_code"] == 200
            assert results[0]["subscription_id"] == sub.id

            # Check delivery was logged
            deliveries = wh_store.list_deliveries(sub.id)
            assert len(deliveries) >= 1
            assert deliveries[0].status == "success"
        finally:
            await server.close()

    # ------------------------------------------------------------------
    # HMAC signature verification
    # ------------------------------------------------------------------

    async def test_deliver_sets_signature_header(self, wh_store) -> None:
        """Delivery includes the X-A2A-Signature HMAC header."""
        sub = wh_store.create_subscription(
            url="http://localhost:1/hook",
            events=["test_event"],
            secret="verify-me",
        )

        received_headers: Dict[str, str] = {}

        async def mock_handler(request: web.Request) -> web.Response:
            nonlocal received_headers
            body = await request.read()
            received_headers = dict(request.headers)
            received_headers["_body"] = body.hex()
            return web.json_response({"ok": True}, status=200)

        mock_app = await self._make_mock_app(mock_handler)
        server = TestServer(mock_app)
        await server.start_server()

        try:
            wh_store._engine.execute(
                "UPDATE webhook_subscriptions SET url = ? WHERE id = ?",
                (f"http://localhost:{server.port}/hook", sub.id),
            )
            wh_store._engine.commit()

            async with aiohttp.ClientSession() as session:
                engine = WebhookDeliveryEngine(
                    store=wh_store,
                    http_session=session,
                )
                await engine.deliver_event("test_event", {"msg": "signed"})

            assert "X-A2A-Signature" in received_headers
            assert "Content-Type" in received_headers
            assert received_headers["Content-Type"] == "application/json"

            body_bytes = bytes.fromhex(received_headers["_body"])
            sig = received_headers["X-A2A-Signature"]
            assert await self._check_signature(body_bytes, sig, "verify-me")
        finally:
            await server.close()

    # ------------------------------------------------------------------
    # Retry on failure
    # ------------------------------------------------------------------

    async def test_retry_on_http_error(self, wh_store) -> None:
        """Delivery is retried on non-2xx responses."""
        sub = wh_store.create_subscription(
            url="http://localhost:1/hook",
            events=["test_event"],
            secret="retry-test",
        )

        attempt_count = 0

        async def mock_handler(request: web.Request) -> web.Response:
            nonlocal attempt_count
            attempt_count += 1
            return web.json_response({"error": "server error"}, status=500)

        mock_app = await self._make_mock_app(mock_handler)
        server = TestServer(mock_app)
        await server.start_server()

        try:
            wh_store._engine.execute(
                "UPDATE webhook_subscriptions SET url = ? WHERE id = ?",
                (f"http://localhost:{server.port}/hook", sub.id),
            )
            wh_store._engine.commit()

            async with aiohttp.ClientSession() as session:
                engine = WebhookDeliveryEngine(
                    store=wh_store,
                    http_session=session,
                )

                # Run with a timeout so test doesn't hang on retry delays
                results = await asyncio.wait_for(
                    engine.deliver_event("test_event", {"msg": "retry"}),
                    timeout=20,
                )

            assert len(results) == 1
            assert results[0]["status"] == "failed"

            # Should have attempted initial + all retries
            assert attempt_count >= 2

            # Each attempt logged as failed
            deliveries = wh_store.list_deliveries(sub.id)
            failed_count = sum(1 for d in deliveries if d.status == "failed")
            assert failed_count >= 2
        finally:
            await server.close()

    # ------------------------------------------------------------------
    # Auto-disable on consecutive failures
    # ------------------------------------------------------------------

    async def test_auto_disable_after_max_failures(self, wh_store) -> None:
        """Subscription auto-disables after MAX_CONSECUTIVE_FAILURES."""
        sub = wh_store.create_subscription(
            url="http://localhost:1/hook",
            events=["test_event"],
            secret="disable-test",
        )

        # Set consecutive_failures to just below the threshold
        # After one delivery_event (which increments once for all retries),
        # consecutive_failures will hit MAX_CONSECUTIVE_FAILURES and auto-disable.
        pre_failures = MAX_CONSECUTIVE_FAILURES - 1
        wh_store._engine.execute(
            "UPDATE webhook_subscriptions SET consecutive_failures = ?, retry_count = ? WHERE id = ?",
            (pre_failures, pre_failures, sub.id),
        )
        wh_store._engine.commit()

        async def fail_handler(request: web.Request) -> web.Response:
            return web.json_response({"error": "nope"}, status=500)

        mock_app = await self._make_mock_app(fail_handler)
        server = TestServer(mock_app)
        await server.start_server()

        try:
            wh_store._engine.execute(
                "UPDATE webhook_subscriptions SET url = ? WHERE id = ?",
                (f"http://localhost:{server.port}/hook", sub.id),
            )
            wh_store._engine.commit()

            async with aiohttp.ClientSession() as session:
                engine = WebhookDeliveryEngine(
                    store=wh_store,
                    http_session=session,
                )

                await asyncio.wait_for(
                    engine.deliver_event("test_event", {"msg": "disable"}),
                    timeout=25,
                )

            # Subscription should be disabled
            updated = wh_store.get_subscription(sub.id)
            assert updated is not None
            assert updated.enabled is False
            assert updated.consecutive_failures >= MAX_CONSECUTIVE_FAILURES
        finally:
            await server.close()

    # ------------------------------------------------------------------
    # Skip disabled subscriptions
    # ------------------------------------------------------------------

    async def test_skip_disabled_subscription(self, wh_store) -> None:
        """Disabled subscriptions are skipped (not included in event matching)."""
        sub = wh_store.create_subscription(
            url="https://should-never-be-called.example/hook",
            events=["test_event"],
            secret="skip-test",
        )
        wh_store.disable_subscription(sub.id)

        async with aiohttp.ClientSession() as session:
            engine = WebhookDeliveryEngine(
                store=wh_store,
                http_session=session,
            )

            results = await engine.deliver_event("test_event", {"msg": "skip"})
            # Disabled subscriptions are filtered out by get_subscriptions_for_event
            assert results == []

    # ------------------------------------------------------------------
    # No-op when no matching subscriptions
    # ------------------------------------------------------------------

    async def test_no_matching_subscriptions(self, wh_store) -> None:
        """deliver_event returns empty when no subscriptions match."""
        async with aiohttp.ClientSession() as session:
            engine = WebhookDeliveryEngine(
                store=wh_store,
                http_session=session,
            )

            results = await engine.deliver_event("nonexistent_event", {})
            assert results == []


# ===========================================================================
# WebhookHandler — HTTP endpoints
# ===========================================================================


class TestWebhookHTTP:
    """HTTP API endpoints for webhook subscription management."""

    @pytest.fixture
    async def http_client(self, wh_engine):
        """Create a test client with webhook routes registered.

        Routes are registered directly (without ``require_scope`` wrapper)
        so tests don't depend on auth middleware.
        """
        app = web.Application()
        wh_store = WebhookStore(wh_engine)
        handler = WebhookHandler(wh_store)
        app["webhook_store"] = wh_store

        # Register handler methods directly (no auth middleware in tests)
        app.router.add_post("/admin/webhooks", handler.handle_create)
        app.router.add_get("/admin/webhooks", handler.handle_list)
        app.router.add_delete("/admin/webhooks/{id}", handler.handle_delete)
        app.router.add_patch("/admin/webhooks/{id}/toggle", handler.handle_toggle)
        app.router.add_get("/admin/webhooks/{id}/deliveries", handler.handle_deliveries)

        server = TestServer(app)
        client = TestClient(server)
        await client.start_server()
        yield client, wh_store
        await client.close()

    async def test_create_subscription(self, http_client) -> None:
        """POST /admin/webhooks creates a subscription."""
        client, store = http_client
        resp = await client.post("/admin/webhooks", json={
            "url": "https://example.com/hook",
            "events": ["task_created"],
        })
        assert resp.status == 201
        data = await resp.json()
        assert data["id"].startswith("wh_")
        assert data["url"] == "https://example.com/hook"
        assert data["events"] == ["task_created"]
        assert data["enabled"] is True
        assert "secret" in data

    async def test_create_subscription_validation(self, http_client) -> None:
        """POST /admin/webhooks validates required fields."""
        client, store = http_client

        # Missing url
        resp = await client.post("/admin/webhooks", json={"events": ["e"]})
        assert resp.status == 400
        data = await resp.json()
        assert "url" in data.get("detail", "").lower()

        # Invalid events
        resp = await client.post("/admin/webhooks", json={
            "url": "https://example.com/hook",
            "events": [],
        })
        assert resp.status == 400

    async def test_list_subscriptions(self, http_client, wh_store) -> None:
        """GET /admin/webhooks lists all subscriptions."""
        client, store = http_client
        wh_store.create_subscription(url="https://a.com", events=["a"])
        wh_store.create_subscription(url="https://b.com", events=["b"])

        resp = await client.get("/admin/webhooks")
        assert resp.status == 200
        data = await resp.json()
        assert data["total"] >= 2

    async def test_delete_subscription(self, http_client, wh_store) -> None:
        """DELETE /admin/webhooks/{id} deletes a subscription."""
        client, store = http_client
        sub = wh_store.create_subscription(url="https://x.com", events=["x"])

        resp = await client.delete(f"/admin/webhooks/{sub.id}")
        assert resp.status == 200

        assert wh_store.get_subscription(sub.id) is None

    async def test_delete_nonexistent(self, http_client) -> None:
        """DELETE /admin/webhooks/{id} returns 404 for missing id."""
        client, store = http_client
        resp = await client.delete("/admin/webhooks/wh_nonexistent")
        assert resp.status == 404

    async def test_toggle_disable_enable(self, http_client, wh_store) -> None:
        """PATCH /admin/webhooks/{id}/toggle enables/disables."""
        client, store = http_client
        sub = wh_store.create_subscription(url="https://y.com", events=["y"])

        # Disable
        resp = await client.patch(f"/admin/webhooks/{sub.id}/toggle", json={"enabled": False})
        assert resp.status == 200
        fetched = wh_store.get_subscription(sub.id)
        assert fetched is not None
        assert fetched.enabled is False

        # Re-enable
        resp = await client.patch(f"/admin/webhooks/{sub.id}/toggle", json={"enabled": True})
        assert resp.status == 200
        fetched = wh_store.get_subscription(sub.id)
        assert fetched is not None
        assert fetched.enabled is True

    async def test_toggle_nonexistent(self, http_client) -> None:
        """PATCH toggle returns 404 for missing subscription."""
        client, store = http_client
        resp = await client.patch("/admin/webhooks/wh_gone/toggle", json={"enabled": False})
        assert resp.status == 404

    async def test_list_deliveries_via_http(self, http_client, wh_store) -> None:
        """GET /admin/webhooks/{id}/deliveries returns delivery logs."""
        client, store = http_client
        sub = wh_store.create_subscription(url="https://z.com", events=["z"])

        # Log some deliveries
        for i in range(3):
            wh_store.log_delivery(
                subscription_id=sub.id,
                event_type="z_event",
                payload={"i": i},
                status="success",
                response_code=200,
            )

        resp = await client.get(f"/admin/webhooks/{sub.id}/deliveries")
        assert resp.status == 200
        data = await resp.json()
        assert data["total"] == 3
        assert len(data["deliveries"]) == 3

    async def test_list_deliveries_for_missing_sub(self, http_client) -> None:
        """GET deliveries for a missing subscription returns 404."""
        client, store = http_client
        resp = await client.get("/admin/webhooks/wh_gone/deliveries")
        assert resp.status == 404


# ===========================================================================
# Database schema creation
# ===========================================================================


class TestWebhookSchema:
    """Webhook schema creation is idempotent."""

    def test_schema_creation_idempotent(self) -> None:
        """Calling _maybe_create_webhook_schema twice does not error."""
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        try:
            engine = SQLiteEngine(tmp.name)
            engine.connect()
            _maybe_create_webhook_schema(engine)
            _maybe_create_webhook_schema(engine)  # second call
            engine.close()
        finally:
            try:
                os.unlink(tmp.name)
            except OSError:
                pass

    def test_tables_exist_after_creation(self, wh_engine) -> None:
        """Created tables are queryable."""
        cur = wh_engine.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'webhook_%'"
        )
        tables = {row["name"] for row in cur.fetchall()}
        assert "webhook_subscriptions" in tables
        assert "webhook_deliveries" in tables
