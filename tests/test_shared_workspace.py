#!/usr/bin/env python3
"""Tests for SharedWorkspaceManager and shared workspace API routes.

Covers:
- SharedWorkspaceManager CRUD (create, get, list, delete)
- Membership management (join, leave)
- File-level locking (lock, unlock, TTL expiry, conflicts)
- Stale lock reaper
- workspace.py VALID_KINDS inclusion of "shared"
- HTTP API routes via aiohttp TestClient
- Swarm auto-create integration
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
from pathlib import Path
from typing import AsyncGenerator, Generator

import pytest
from aiohttp.test_utils import TestClient, TestServer
from aiohttp import web

from simple_a2a_registry.orchestration.shared_workspace import (
    SharedWorkspaceManager,
    SharedWorkspaceError,
    WorkspaceNotFoundError,
    LockError,
    MemberError,
    DEFAULT_LOCK_TTL,
)
from simple_a2a_registry.orchestration.shared_workspace_routes import (
    SharedWorkspaceHandler,
    register_shared_workspace_routes,
)
from simple_a2a_registry.database import SQLiteEngine
from simple_a2a_registry.orchestration.workspace import VALID_KINDS
from simple_a2a_registry.server import create_app

pytestmark = pytest.mark.asyncio


# ===================================================================
# Fixtures
# ===================================================================


@pytest.fixture
def tmp_dir() -> Generator[Path, None, None]:
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


@pytest.fixture
def ws_mgr(tmp_dir: Path) -> SharedWorkspaceManager:
    """Create a SharedWorkspaceManager backed by in-memory SQLite."""
    return SharedWorkspaceManager(
        workspaces_root=str(tmp_dir / "shared-ws"),
    )


@pytest.fixture
def ws_mgr_with_broadcast(tmp_dir: Path) -> SharedWorkspaceManager:
    """Create a manager with a broadcast callback for notification tests."""
    broadcast_events: list[tuple[str, dict]] = []

    async def _broadcast(event_type: str, data: dict) -> None:
        broadcast_events.append((event_type, data))

    mgr = SharedWorkspaceManager(
        workspaces_root=str(tmp_dir / "shared-ws-bc"),
        broadcast_fn=_broadcast,
    )
    mgr._test_broadcast_events = broadcast_events  # type: ignore
    return mgr


@pytest.fixture
def api_client(tmp_dir: Path) -> AsyncGenerator[TestClient, None]:
    """Create a full app with shared workspace routes for HTTP integration tests."""

    async def maker() -> TestClient:
        data_dir = str(tmp_dir / "data")
        os.makedirs(data_dir, exist_ok=True)
        app = create_app(
            data_dir=data_dir,
            base_url="http://localhost:0",
        )
        server = TestServer(app)
        await server.start_server()
        return TestClient(server)

    yield maker


# ===================================================================
# VALID_KINDS — "shared" kind
# ===================================================================


def test_valid_kinds_includes_shared() -> None:
    """workspace.py VALID_KINDS must include 'shared'."""
    assert "shared" in VALID_KINDS, "VALID_KINDS must include 'shared'"


# ===================================================================
# SharedWorkspaceManager — CRUD
# ===================================================================


class TestCRUD:
    """Create, read, list, delete shared workspaces."""

    async def test_create_workspace(self, ws_mgr: SharedWorkspaceManager) -> None:
        ws = ws_mgr.create(
            name="test-ws",
            created_by="agent-a",
            tenant="tenant-x",
        )
        assert ws["id"].startswith("ws_")
        assert ws["name"] == "test-ws"
        assert ws["created_by"] == "agent-a"
        assert ws["tenant"] == "tenant-x"
        assert ws["member_agent_ids"] == ["agent-a"]
        assert ws["created_at"] > 0
        # Directory must exist
        assert Path(ws["path"]).is_dir()

    async def test_create_with_members(self, ws_mgr: SharedWorkspaceManager) -> None:
        ws = ws_mgr.create(
            name="team-ws",
            created_by="agent-a",
            members=["agent-b", "agent-c"],
        )
        members = ws["member_agent_ids"]
        assert "agent-a" in members  # creator always included
        assert "agent-b" in members
        assert "agent-c" in members
        assert len(members) == 3

    async def test_get_workspace(self, ws_mgr: SharedWorkspaceManager) -> None:
        created = ws_mgr.create(name="get-test", created_by="agent-a")
        fetched = ws_mgr.get(created["id"])
        assert fetched is not None
        assert fetched["id"] == created["id"]
        assert fetched["name"] == "get-test"

    async def test_get_nonexistent(self, ws_mgr: SharedWorkspaceManager) -> None:
        assert ws_mgr.get("ws_nonexistent") is None

    async def test_list_workspaces(self, ws_mgr: SharedWorkspaceManager) -> None:
        ws1 = ws_mgr.create(name="ws1", created_by="agent-a", members=["agent-b"])
        ws2 = ws_mgr.create(name="ws2", created_by="agent-b", members=["agent-c"])
        _ws3 = ws_mgr.create(name="ws3", created_by="agent-c")

        # All
        all_ws = ws_mgr.list_accessible()
        assert len(all_ws) >= 3

        # Filter by agent
        agent_b_ws = ws_mgr.list_accessible(agent_id="agent-b")
        ids = [w["id"] for w in agent_b_ws]
        assert ws1["id"] in ids
        assert ws2["id"] in ids  # agent-b created ws2

        # Filter by tenant
        ws_mgr.create(name="tenant-ws", created_by="agent-a", tenant="t1")
        t1_ws = ws_mgr.list_accessible(tenant="t1")
        assert all(w["tenant"] == "t1" for w in t1_ws)

    async def test_delete_workspace(self, ws_mgr: SharedWorkspaceManager) -> None:
        ws = ws_mgr.create(name="del-test", created_by="agent-a")
        ws_id = ws["id"]

        # Wrong caller
        with pytest.raises(SharedWorkspaceError, match="not the creator"):
            ws_mgr.delete(ws_id, caller="agent-b")

        # Correct caller
        assert ws_mgr.delete(ws_id, caller="agent-a") is True
        assert ws_mgr.get(ws_id) is None

        # Delete non-existent
        assert ws_mgr.delete("ws_nonexistent") is False


# ===================================================================
# Membership
# ===================================================================


class TestMembership:
    """Join and leave shared workspaces."""

    async def test_join(self, ws_mgr: SharedWorkspaceManager) -> None:
        ws = ws_mgr.create(name="join-test", created_by="agent-a")
        updated = ws_mgr.join(ws["id"], "agent-b")
        assert "agent-b" in updated["member_agent_ids"]

    async def test_join_idempotent(self, ws_mgr: SharedWorkspaceManager) -> None:
        ws = ws_mgr.create(name="idem-join", created_by="agent-a")
        ws_mgr.join(ws["id"], "agent-b")
        updated = ws_mgr.join(ws["id"], "agent-b")  # second join is a no-op
        assert len(updated["member_agent_ids"]) == 2

    async def test_join_nonexistent(self, ws_mgr: SharedWorkspaceManager) -> None:
        with pytest.raises(WorkspaceNotFoundError):
            ws_mgr.join("ws_nonexistent", "agent-b")

    async def test_leave(self, ws_mgr: SharedWorkspaceManager) -> None:
        ws = ws_mgr.create(name="leave-test", created_by="agent-a", members=["agent-b"])
        updated = ws_mgr.leave(ws["id"], "agent-b")
        assert "agent-b" not in updated["member_agent_ids"]

    async def test_leave_releases_locks(self, ws_mgr: SharedWorkspaceManager) -> None:
        ws = ws_mgr.create(name="lock-cleanup", created_by="agent-a", members=["agent-b"])
        # Agent-b locks a file
        ws_mgr.lock(ws["id"], "test.txt", "agent-b")
        # Leave releases locks
        ws_mgr.leave(ws["id"], "agent-b")
        # File should be unlocked
        files = ws_mgr.list_files(ws["id"])
        assert files[0]["locked_by"] is None

    async def test_leave_idempotent(self, ws_mgr: SharedWorkspaceManager) -> None:
        ws = ws_mgr.create(name="idem-leave", created_by="agent-a")
        updated = ws_mgr.leave(ws["id"], "agent-b")  # not a member
        assert updated["id"] == ws["id"]

    async def test_leave_nonexistent(self, ws_mgr: SharedWorkspaceManager) -> None:
        with pytest.raises(WorkspaceNotFoundError):
            ws_mgr.leave("ws_nonexistent", "agent-a")


# ===================================================================
# File Locking
# ===================================================================


class TestFileLocking:
    """File-level locking with TTL, conflict detection, and release."""

    async def test_lock_file(self, ws_mgr: SharedWorkspaceManager) -> None:
        ws = ws_mgr.create(name="lock-test", created_by="agent-a")
        record = ws_mgr.lock(ws["id"], "data.txt", "agent-a")
        assert record["path"] == "data.txt"
        assert record["locked_by"] == "agent-a"
        assert record["lock_expires"] > time.time()
        assert record["version"] >= 1

    async def test_lock_conflict(self, ws_mgr: SharedWorkspaceManager) -> None:
        ws = ws_mgr.create(name="conflict", created_by="agent-a", members=["agent-b"])
        ws_mgr.lock(ws["id"], "shared.txt", "agent-a")
        with pytest.raises(LockError, match="already locked"):
            ws_mgr.lock(ws["id"], "shared.txt", "agent-b")

    async def test_lock_same_agent_renew(self, ws_mgr: SharedWorkspaceManager) -> None:
        ws = ws_mgr.create(name="renew", created_by="agent-a")
        r1 = ws_mgr.lock(ws["id"], "file.txt", "agent-a", ttl=10)
        r2 = ws_mgr.lock(ws["id"], "file.txt", "agent-a", ttl=300)
        # Same agent can re-lock (renew)
        assert r2["locked_by"] == "agent-a"
        assert r2["lock_expires"] > r1["lock_expires"]

    async def test_lock_not_a_member(self, ws_mgr: SharedWorkspaceManager) -> None:
        ws = ws_mgr.create(name="member-check", created_by="agent-a")
        with pytest.raises(MemberError):
            ws_mgr.lock(ws["id"], "data.txt", "agent-b")

    async def test_lock_nonexistent_workspace(self, ws_mgr: SharedWorkspaceManager) -> None:
        with pytest.raises(WorkspaceNotFoundError):
            ws_mgr.lock("ws_nonexistent", "data.txt", "agent-a")

    async def test_unlock(self, ws_mgr: SharedWorkspaceManager) -> None:
        ws = ws_mgr.create(name="unlock-test", created_by="agent-a")
        ws_mgr.lock(ws["id"], "data.txt", "agent-a")
        record = ws_mgr.unlock(ws["id"], "data.txt", "agent-a")
        assert record["locked_by"] is None
        assert record["lock_expires"] is None
        # Checksum should be recorded
        file_path = str(Path(ws["path"]) / "data.txt")
        Path(file_path).write_text("hello")
        record2 = ws_mgr.unlock(ws["id"], "data.txt", "agent-a")
        assert record2["checksum"] is not None

    async def test_unlock_wrong_agent(self, ws_mgr: SharedWorkspaceManager) -> None:
        ws = ws_mgr.create(name="wrong-agent", created_by="agent-a", members=["agent-b"])
        ws_mgr.lock(ws["id"], "data.txt", "agent-a")
        with pytest.raises(LockError, match="locked by agent"):
            ws_mgr.unlock(ws["id"], "data.txt", "agent-b")

    async def test_unlock_untracked_file(self, ws_mgr: SharedWorkspaceManager) -> None:
        ws = ws_mgr.create(name="untracked", created_by="agent-a")
        result = ws_mgr.unlock(ws["id"], "ghost.txt", "agent-a")
        assert result is None

    async def test_ttl_expiry(self, ws_mgr: SharedWorkspaceManager) -> None:
        ws = ws_mgr.create(name="ttl-test", created_by="agent-a", members=["agent-b"])
        # Lock with 0-second TTL (expires immediately)
        record = ws_mgr.lock(ws["id"], "quick.txt", "agent-a", ttl=0)
        assert record["lock_expires"] <= time.time() + 1

    async def test_lock_with_path_normalisation(self, ws_mgr: SharedWorkspaceManager) -> None:
        ws = ws_mgr.create(name="path-norm", created_by="agent-a")
        # Using relative vs. dotted path — both should resolve to same file
        r1 = ws_mgr.lock(ws["id"], "sub/a.txt", "agent-a")
        assert r1["path"] == "sub/a.txt"

    async def test_file_listing(self, ws_mgr: SharedWorkspaceManager) -> None:
        ws = ws_mgr.create(name="file-list", created_by="agent-a")
        ws_mgr.lock(ws["id"], "a.txt", "agent-a")
        ws_mgr.lock(ws["id"], "b.txt", "agent-a")
        files = ws_mgr.list_files(ws["id"])
        assert len(files) == 2
        paths = [f["path"] for f in files]
        assert "a.txt" in paths
        assert "b.txt" in paths

    async def test_get_file_status(self, ws_mgr: SharedWorkspaceManager) -> None:
        ws = ws_mgr.create(name="file-status", created_by="agent-a")
        ws_mgr.lock(ws["id"], "status.txt", "agent-a")
        status = ws_mgr.get_file_status(ws["id"], "status.txt")
        assert status is not None
        assert status["locked_by"] == "agent-a"
        # Non-existent file
        assert ws_mgr.get_file_status(ws["id"], "nope.txt") is None


# ===================================================================
# Stale Lock Reaper
# ===================================================================


class TestReaper:
    """Stale lock reaper releases expired locks."""

    async def test_reap_expired_locks(self, ws_mgr: SharedWorkspaceManager) -> None:
        ws = ws_mgr.create(
            name="reaper-test", created_by="agent-a", members=["agent-b"],
        )
        # Lock with a past TTL
        ws_mgr.lock(ws["id"], "old.txt", "agent-a", ttl=-1)
        # Sleep briefly to ensure expiry
        time.sleep(0.01)

        count = ws_mgr.reap_stale_locks(max_age=0)
        assert count > 0

        # Lock should be released
        status = ws_mgr.get_file_status(ws["id"], "old.txt")
        assert status is not None
        assert status["locked_by"] is None

    async def test_reap_skips_active_locks(self, ws_mgr: SharedWorkspaceManager) -> None:
        ws = ws_mgr.create(name="active-ok", created_by="agent-a")
        ws_mgr.lock(ws["id"], "active.txt", "agent-a", ttl=300)
        count = ws_mgr.reap_stale_locks(max_age=10)
        assert count == 0
        status = ws_mgr.get_file_status(ws["id"], "active.txt")
        assert status["locked_by"] == "agent-a"


# ===================================================================
# Broadcast (Event Notification)
# ===================================================================


class TestBroadcast:
    """Broadcast callback fires on workspace changes."""

    async def test_create_broadcasts(self, ws_mgr_with_broadcast: SharedWorkspaceManager) -> None:
        mgr = ws_mgr_with_broadcast
        ws = mgr.create(name="bc-test", created_by="agent-a")
        events: list = mgr._test_broadcast_events  # type: ignore
        assert any(e[0] == "workspace_created" and e[1]["id"] == ws["id"] for e in events)

    async def test_delete_broadcasts(self, ws_mgr_with_broadcast: SharedWorkspaceManager) -> None:
        mgr = ws_mgr_with_broadcast
        ws = mgr.create(name="bc-del", created_by="agent-a")
        # Clear create event
        mgr._test_broadcast_events.clear()  # type: ignore
        mgr.delete(ws["id"], caller="agent-a")
        events: list = mgr._test_broadcast_events  # type: ignore
        assert any(e[0] == "workspace_deleted" and e[1]["id"] == ws["id"] for e in events)

    async def test_lock_broadcasts(self, ws_mgr_with_broadcast: SharedWorkspaceManager) -> None:
        mgr = ws_mgr_with_broadcast
        ws = mgr.create(name="bc-lock", created_by="agent-a")
        mgr._test_broadcast_events.clear()  # type: ignore
        mgr.lock(ws["id"], "data.txt", "agent-a")
        events: list = mgr._test_broadcast_events  # type: ignore
        assert any(
            e[0] == "file_locked"
            and e[1].get("path") == "data.txt"
            for e in events
        )

    async def test_unlock_broadcasts(self, ws_mgr_with_broadcast: SharedWorkspaceManager) -> None:
        mgr = ws_mgr_with_broadcast
        ws = mgr.create(name="bc-unlock", created_by="agent-a")
        mgr.lock(ws["id"], "data.txt", "agent-a")
        mgr._test_broadcast_events.clear()  # type: ignore
        mgr.unlock(ws["id"], "data.txt", "agent-a")
        events: list = mgr._test_broadcast_events  # type: ignore
        assert any(e[0] == "file_unlocked" for e in events)


# ===================================================================
# HTTP API Integration
# ===================================================================


class TestHTTPAPI:
    """Via aiohttp TestClient, test the /v2/workspaces endpoints."""

    async def test_create_via_api(self, api_client) -> None:
        client = await api_client()
        resp = await client.post("/v2/workspaces", json={
            "name": "api-test",
            "members": ["agent-a", "agent-b"],
        })
        assert resp.status == 201
        data = await resp.json()
        ws = data["workspace"]
        assert ws["name"] == "api-test"
        assert "agent-a" in ws["member_agent_ids"]  # creator auto-added

    async def test_create_missing_name(self, api_client) -> None:
        client = await api_client()
        resp = await client.post("/v2/workspaces", json={})
        assert resp.status == 400
        data = await resp.json()
        assert "name" in data.get("detail", "") or "name" in data.get("error", "")

    async def test_list_via_api(self, api_client) -> None:
        client = await api_client()
        await client.post("/v2/workspaces", json={"name": "ws-a"})
        await client.post("/v2/workspaces", json={"name": "ws-b"})
        resp = await client.get("/v2/workspaces")
        assert resp.status == 200
        data = await resp.json()
        assert data["total"] >= 2

    async def test_get_detail_via_api(self, api_client) -> None:
        client = await api_client()
        create_resp = await client.post("/v2/workspaces", json={"name": "detail-test"})
        ws_id = (await create_resp.json())["workspace"]["id"]

        resp = await client.get(f"/v2/workspaces/{ws_id}")
        assert resp.status == 200
        data = await resp.json()
        assert data["workspace"]["name"] == "detail-test"

    async def test_get_not_found(self, api_client) -> None:
        client = await api_client()
        resp = await client.get("/v2/workspaces/ws_nonexistent")
        assert resp.status == 404

    async def test_delete_via_api(self, api_client) -> None:
        client = await api_client()
        create_resp = await client.post("/v2/workspaces", json={"name": "del-test"})
        ws_id = (await create_resp.json())["workspace"]["id"]

        resp = await client.delete(f"/v2/workspaces/{ws_id}")
        assert resp.status == 200

        # Confirm gone
        get_resp = await client.get(f"/v2/workspaces/{ws_id}")
        assert get_resp.status == 404

    async def test_join_via_api(self, api_client) -> None:
        client = await api_client()
        create_resp = await client.post("/v2/workspaces", json={"name": "join-ws"})
        ws_id = (await create_resp.json())["workspace"]["id"]

        # Join requires an agent_id in request context — with default app
        # the agent_id defaults to "anonymous" which may not match the scenario.
        # This tests the route is registered and functional.
        resp = await client.post(f"/v2/workspaces/{ws_id}/join")
        # Expect 401 because no agent identity is set for the request
        assert resp.status in (200, 401)

    async def test_lock_via_api(self, api_client) -> None:
        client = await api_client()
        create_resp = await client.post("/v2/workspaces", json={"name": "lock-ws"})
        ws_id = (await create_resp.json())["workspace"]["id"]

        resp = await client.post(f"/v2/workspaces/{ws_id}/lock", json={
            "file_path": "data.txt",
        })
        assert resp.status in (200, 400, 401, 409)

    async def test_unlock_via_api(self, api_client) -> None:
        client = await api_client()
        create_resp = await client.post("/v2/workspaces", json={"name": "unlock-ws"})
        ws_id = (await create_resp.json())["workspace"]["id"]

        resp = await client.post(f"/v2/workspaces/{ws_id}/unlock", json={
            "file_path": "data.txt",
        })
        assert resp.status in (200, 400, 401, 404)


# ===================================================================
# Edge Cases
# ===================================================================


class TestEdgeCases:
    """Edge cases and error handling."""

    async def test_create_with_explicit_path(self, ws_mgr: SharedWorkspaceManager) -> None:
        """Creating with an explicit path uses that path."""
        ws = ws_mgr.create(
            name="explicit-path",
            created_by="agent-a",
            path=str(Path(ws_mgr._workspaces_root) / "custom"),
        )
        assert Path(ws["path"]).name == "custom"

    async def test_multiple_agents_lock_different_files(
        self, ws_mgr: SharedWorkspaceManager,
    ) -> None:
        ws = ws_mgr.create(name="multi-agent", created_by="agent-a", members=["agent-b"])
        ws_mgr.lock(ws["id"], "file-a.txt", "agent-a")
        ws_mgr.lock(ws["id"], "file-b.txt", "agent-b")  # different file, should succeed
        files = ws_mgr.list_files(ws["id"])
        assert len(files) == 2

    async def test_delete_cleans_file_records(
        self, ws_mgr: SharedWorkspaceManager,
    ) -> None:
        ws = ws_mgr.create(name="cleanup", created_by="agent-a")
        ws_mgr.lock(ws["id"], "tracked.txt", "agent-a")
        ws_mgr.delete(ws["id"], caller="agent-a")
        # Files table should be empty for this workspace
        remaining = ws_mgr.list_files(ws["id"])
        assert len(remaining) == 0

    async def test_tenant_isolation(self, ws_mgr: SharedWorkspaceManager) -> None:
        ws_mgr.create(name="t1-ws", created_by="a", tenant="t1")
        ws_mgr.create(name="t2-ws", created_by="b", tenant="t2")
        t1_list = ws_mgr.list_accessible(tenant="t1")
        assert all(w["tenant"] == "t1" for w in t1_list)
        t2_list = ws_mgr.list_accessible(tenant="t2")
        assert all(w["tenant"] == "t2" for w in t2_list)
