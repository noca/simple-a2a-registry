"""Tests for GET /admin/security-events API (P1-C)."""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest.mock
from typing import Any, Optional

import pytest
from aiohttp.test_utils import TestServer, TestClient

from simple_a2a_registry.auth import create_token, _generate_rsa_keypair
from simple_a2a_registry.config import (
    Config, AuthConfig, SecurityHarnessConfig, DatabaseConfig,
)
from simple_a2a_registry.security import SecurityEventStore
from simple_a2a_registry.database import SQLiteEngine
from simple_a2a_registry.server import create_app

pytestmark = pytest.mark.asyncio

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def shared_keypair():
    return _generate_rsa_keypair()


async def _build_app(
    keypair: Optional[tuple] = None,
    harness_enabled: bool = True,
) -> TestClient:
    """Create a TestClient with SecurityHarness + auth enabled."""
    if keypair is None:
        keypair = _generate_rsa_keypair()
    priv, pub = keypair

    tmpdir = tempfile.TemporaryDirectory()
    db_path = os.path.join(tmpdir.name, "registry.db")

    cfg = Config(
        database=DatabaseConfig(driver="sqlite", sqlite_path=db_path),
        auth=AuthConfig(enabled=True),
        security_harness=SecurityHarnessConfig(
            enabled=harness_enabled,
            mode="enforce",
            default_delegation_policy="open",
        ),
    )

    import simple_a2a_registry.server as srv_mod
    with unittest.mock.patch.object(
        srv_mod, "_generate_rsa_keypair", return_value=(priv, pub)
    ):
        app = create_app(
            data_dir=tmpdir.name,
            base_url="http://localhost:8321",
            config=cfg,
            auth_enabled=True,
            bootstrap_secret="test-bootstrap-secret",
        )

    server = TestServer(app)
    await server.start_server()
    client = TestClient(server)
    client._tmpdir = tmpdir
    client._priv = priv
    client._pub = pub
    return client


async def _get_admin_token(
    client: TestClient,
    tenant: Optional[str] = None,
) -> str:
    """Create a JWT with registry:admin scope."""
    return create_token(
        sub="admin",
        private_key=client._priv,
        algorithm="RS256",
        scope="registry:admin agent:read agent:register task:write task:read",
        tenant=tenant,
    )


async def _seed_events(
    client: TestClient,
    event_store: SecurityEventStore,
    count: int = 3,
) -> list[dict]:
    """Insert *count* security events into the store and return their dicts."""
    events = []
    for i in range(count):
        ev = event_store.record(
            event_type="AUTH_FAILURE",
            actor=f"user-{i}",
            target=f"resource-{i}",
            decision="deny",
            tenant=f"tenant-{i % 2}",
            reason=f"test event {i}",
            scope_used="task:read",
            task_id=f"t_task_{i}" if i > 0 else None,
        )
        events.append(ev.to_dict())
    return events


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSecurityEventsAPI:
    """GET /admin/security-events — full lifecycle tests."""

    async def test_no_events_returns_empty(self, shared_keypair):
        """No events in store → GET returns {total:0, events:[]}."""
        client = await _build_app(keypair=shared_keypair)
        try:
            token = await _get_admin_token(client)
            resp = await client.get(
                "/admin/security-events",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert resp.status == 200, f"Expected 200, got {resp.status}: {await resp.text()}"
            data = await resp.json()
            assert data["total"] == 0
            assert data["events"] == []
            assert data["limit"] == 50
            assert data["offset"] == 0
        finally:
            client._tmpdir.cleanup()

    async def test_returns_seeded_events(self, shared_keypair):
        """Seeded events appear in GET response."""
        client = await _build_app(keypair=shared_keypair)
        try:
            # Access the event_store from app
            event_store = client.server.app.get("event_store")
            assert event_store is not None, "event_store not found in app"

            seed = await _seed_events(client, event_store, 3)

            token = await _get_admin_token(client)
            resp = await client.get(
                "/admin/security-events",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert resp.status == 200, f"Expected 200, got {resp.status}: {await resp.text()}"
            data = await resp.json()
            assert data["total"] == 3
            assert len(data["events"]) == 3
            # Events are newest first; check we have all 3
            returned_ids = {e["event_id"] for e in data["events"]}
            expected_ids = {e["event_id"] for e in seed}
            assert returned_ids == expected_ids
        finally:
            client._tmpdir.cleanup()

    async def test_limit_and_offset(self, shared_keypair):
        """limit and offset paginate correctly."""
        client = await _build_app(keypair=shared_keypair)
        try:
            event_store = client.server.app["event_store"]
            await _seed_events(client, event_store, 5)

            token = await _get_admin_token(client)

            # limit=2, offset=0 → 2 events
            resp = await client.get(
                "/admin/security-events",
                params={"limit": "2", "offset": "0"},
                headers={"Authorization": f"Bearer {token}"},
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["total"] == 5
            assert len(data["events"]) == 2
            assert data["limit"] == 2
            assert data["offset"] == 0

            # offset=2 → skip first 2
            resp2 = await client.get(
                "/admin/security-events",
                params={"limit": "2", "offset": "2"},
                headers={"Authorization": f"Bearer {token}"},
            )
            assert resp2.status == 200
            data2 = await resp2.json()
            assert len(data2["events"]) == 2
            # IDs at offset 2 should differ from offset 0
            assert data["events"][0]["event_id"] != data2["events"][0]["event_id"]
        finally:
            client._tmpdir.cleanup()

    async def test_filter_by_event_type(self, shared_keypair):
        """?event_type filters correctly."""
        client = await _build_app(keypair=shared_keypair)
        try:
            event_store = client.server.app["event_store"]
            # Record mixed types
            event_store.record("AUTH_FAILURE", "user-a", "res-a", "deny", tenant="t1")
            event_store.record("SCOPE_DENIED", "user-b", "res-b", "deny", tenant="t1")
            event_store.record("AUTH_FAILURE", "user-c", "res-c", "deny", tenant="t1")

            token = await _get_admin_token(client)

            resp = await client.get(
                "/admin/security-events",
                params={"event_type": "AUTH_FAILURE"},
                headers={"Authorization": f"Bearer {token}"},
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["total"] == 2
            for e in data["events"]:
                assert e["event_type"] == "AUTH_FAILURE"

            resp = await client.get(
                "/admin/security-events",
                params={"event_type": "SCOPE_DENIED"},
                headers={"Authorization": f"Bearer {token}"},
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["total"] == 1
        finally:
            client._tmpdir.cleanup()

    async def test_filter_by_actor(self, shared_keypair):
        """?actor filters correctly."""
        client = await _build_app(keypair=shared_keypair)
        try:
            event_store = client.server.app["event_store"]
            event_store.record("AUTH_FAILURE", "alpha", "res", "deny", tenant="t1")
            event_store.record("AUTH_FAILURE", "beta", "res", "deny", tenant="t1")

            token = await _get_admin_token(client)

            resp = await client.get(
                "/admin/security-events",
                params={"actor": "alpha"},
                headers={"Authorization": f"Bearer {token}"},
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["total"] == 1
            assert data["events"][0]["actor"] == "alpha"
        finally:
            client._tmpdir.cleanup()

    async def test_filter_by_tenant_and_task_id(self, shared_keypair):
        """?tenant and ?task_id filters work."""
        client = await _build_app(keypair=shared_keypair)
        try:
            event_store = client.server.app["event_store"]
            event_store.record("AUTH_FAILURE", "u1", "r1", "deny",
                               tenant="tenant-a", task_id="t_abc")
            event_store.record("AUTH_FAILURE", "u2", "r2", "deny",
                               tenant="tenant-b", task_id="t_def")
            event_store.record("AUTH_FAILURE", "u3", "r3", "deny",
                               tenant="tenant-a", task_id="t_ghi")

            token = await _get_admin_token(client)

            # Filter by tenant
            resp = await client.get(
                "/admin/security-events",
                params={"tenant": "tenant-a"},
                headers={"Authorization": f"Bearer {token}"},
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["total"] == 2
            for e in data["events"]:
                assert e["tenant"] == "tenant-a"

            # Filter by task_id
            resp = await client.get(
                "/admin/security-events",
                params={"task_id": "t_abc"},
                headers={"Authorization": f"Bearer {token}"},
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["total"] == 1
            assert data["events"][0]["task_id"] == "t_abc"
        finally:
            client._tmpdir.cleanup()

    async def test_filter_by_time_range(self, shared_keypair):
        """?since and ?until window correctly."""
        client = await _build_app(keypair=shared_keypair)
        try:
            event_store = client.server.app["event_store"]
            now = time.time()
            # Insert events with explicit time by manipulating via direct SQL
            from simple_a2a_registry.security import SecurityEvent
            import uuid, json

            # Record first event (past)
            past = now - 100
            event_store._engine.execute(
                """INSERT OR IGNORE INTO security_events
                   (event_id, event_type, timestamp, actor, target, tenant,
                    decision, reason, scope_used, task_id, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                ("sev_past", "AUTH_FAILURE", past, "user-p", "res-p", "t1",
                 "deny", "past event", "", None, past),
            )
            event_store._engine.commit()

            # Record second event (now)
            event_store.record("AUTH_FAILURE", "user-n", "res-n", "deny",
                               tenant="t1", reason="now event")

            # Record third event (future relative to past)
            future = now + 100
            event_store._engine.execute(
                """INSERT OR IGNORE INTO security_events
                   (event_id, event_type, timestamp, actor, target, tenant,
                    decision, reason, scope_used, task_id, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                ("sev_future", "AUTH_FAILURE", future, "user-f", "res-f", "t1",
                 "deny", "future event", "", None, future),
            )
            event_store._engine.commit()

            token = await _get_admin_token(client)

            # since = now → only the future event
            resp = await client.get(
                "/admin/security-events",
                params={"since": str(now - 50)},
                headers={"Authorization": f"Bearer {token}"},
            )
            assert resp.status == 200
            data = await resp.json()
            # Should include "now" event (timestamp >= now-50) and future event
            assert data["total"] >= 2
            seen_ids = {e["event_id"] for e in data["events"]}
            assert "sev_past" not in seen_ids

            # until = now → only the past event
            resp = await client.get(
                "/admin/security-events",
                params={"until": str(now + 50)},
                headers={"Authorization": f"Bearer {token}"},
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["total"] >= 1
            seen_ids = {e["event_id"] for e in data["events"]}
            assert "sev_future" not in seen_ids
        finally:
            client._tmpdir.cleanup()

    async def test_requires_admin_scope(self, shared_keypair):
        """Non-admin token → 403."""
        client = await _build_app(keypair=shared_keypair)
        try:
            # Token without registry:admin scope
            token = create_token(
                sub="user",
                private_key=client._priv,
                algorithm="RS256",
                scope="agent:read",
            )
            resp = await client.get(
                "/admin/security-events",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert resp.status in (401, 403), (
                f"Expected 401/403, got {resp.status}: {await resp.text()}"
            )
        finally:
            client._tmpdir.cleanup()

    async def test_disabled_returns_404(self, shared_keypair):
        """When security_harness disabled → GET returns 404."""
        client = await _build_app(keypair=shared_keypair, harness_enabled=False)
        try:
            token = await _get_admin_token(client)
            resp = await client.get(
                "/admin/security-events",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert resp.status == 404
            data = await resp.json()
            assert data["error"] == "events_disabled"
        finally:
            client._tmpdir.cleanup()

    async def test_no_auth_returns_401(self, shared_keypair):
        """No Authorization header → 401."""
        client = await _build_app(keypair=shared_keypair)
        try:
            resp = await client.get("/admin/security-events")
            assert resp.status == 401
        finally:
            client._tmpdir.cleanup()

    async def test_invalid_limit_clamped(self, shared_keypair):
        """Invalid limit → defaults to 50."""
        client = await _build_app(keypair=shared_keypair)
        try:
            event_store = client.server.app["event_store"]
            await _seed_events(client, event_store, 2)

            token = await _get_admin_token(client)
            resp = await client.get(
                "/admin/security-events",
                params={"limit": "invalid"},
                headers={"Authorization": f"Bearer {token}"},
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["limit"] == 50
        finally:
            client._tmpdir.cleanup()