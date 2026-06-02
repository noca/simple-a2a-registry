"""Tests for P3.1: callback-mode agent dispatch in the Dispatcher.

Verifies that the Dispatcher correctly handles callback-mode agents
by dispatching tasks via HTTP POST (instead of WebSocket).
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, Generator

import aiohttp
import pytest
from aiohttp import web

from simple_a2a_registry.store import Store as RegistryStore
from simple_a2a_registry.orchestration.models import (
    Task,
    TaskStatus,
    TaskRunStatus,
)
from simple_a2a_registry.orchestration.store import TaskStore, DEFAULT_CLAIM_TTL
from simple_a2a_registry.orchestration.workspace import WorkspaceManager
from simple_a2a_registry.orchestration.dispatcher import (
    Dispatcher,
    DispatcherConfig,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path() -> Generator[str, None, None]:
    """Create a tempfile path for the SQLite DB."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    try:
        yield path
    finally:
        if os.path.exists(path):
            os.unlink(path)


@pytest.fixture
def reg_store(db_path: str) -> Generator[RegistryStore, None, None]:
    """Create a fresh Registry Store backed by a tempfile."""
    # RegistryStore accepts a path string; use a temp dir
    with tempfile.TemporaryDirectory() as d:
        rs = RegistryStore(d)
        try:
            yield rs
        finally:
            rs.close()


@pytest.fixture
def store(db_path: str) -> Generator[TaskStore, None, None]:
    """Create a fresh TaskStore backed by a tempfile."""
    ts = TaskStore(db_path)
    try:
        yield ts
    finally:
        ts.close()


@pytest.fixture
def ws_mgr() -> Generator[WorkspaceManager, None, None]:
    """Create a WorkspaceManager with a temp root."""
    with tempfile.TemporaryDirectory() as d:
        yield WorkspaceManager(str(Path(d) / "workspaces"))


@pytest.fixture
async def http_session() -> AsyncGenerator[aiohttp.ClientSession, None]:
    """Create an HTTP client session."""
    async with aiohttp.ClientSession() as session:
        yield session


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _make_callback_server(
    status_code: int = 200,
    response_body: dict | None = None,
    expect_token: str = "",
) -> AsyncGenerator[tuple[str, list[dict], web.Application], None]:
    """Create a minimal aiohttp server that captures incoming callback payloads.

    Yields:
        (server_url, captured_payloads, app) — captured_payloads is a list
        that the test can inspect to verify callback dispatch contents.
    """
    captured: list[dict] = []
    received_token: list[str] = []

    async def handle_callback(request: web.Request) -> web.Response:
        body = await request.json()
        captured.append(body)
        auth = request.headers.get("Authorization", "")
        if auth:
            received_token.append(auth)
        return web.json_response(response_body or {"status": "ok"}, status=status_code)

    app = web.Application()
    app.router.add_post("/callback", handle_callback)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()

    # Discover the actual port
    port = site._server.sockets[0].getsockname()[1]
    url = f"http://127.0.0.1:{port}/callback"

    try:
        yield url, captured, received_token, app
    finally:
        await runner.cleanup()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestCallbackDispatch:
    """Dispatcher should dispatch tasks to callback-mode agents via HTTP POST."""

    async def test_dispatches_to_callback_agent(
        self,
        store: TaskStore,
        reg_store: RegistryStore,
        ws_mgr: WorkspaceManager,
        http_session: aiohttp.ClientSession,
    ) -> None:
        """A callback-mode agent should receive tasks via HTTP POST."""
        # 1. Register a callback-mode agent in the registry store
        agent_id = reg_store.register_agent({
            "name": "callback-agent-1",
            "description": "A callback-mode agent",
            "preferred_channel": "callback",
            "callback_url": "http://localhost:19999/callback",
            "supported_interfaces": [{
                "url": "http://localhost:19999",
                "protocol_binding": "JSONRPC",
                "protocol_version": "1.0",
            }],
        })

        card = reg_store.get_agent(agent_id)
        assert card is not None
        assert card.get("preferred_channel") == "callback"
        callback_url = card.get("callback_url", "")
        callback_token = card.get("callback_token", "")
        assert callback_url == "http://localhost:19999/callback"
        assert callback_token, "Callback token should be auto-generated"

        # 2. Set up a real callback server
        captured_payloads: list[dict] = []
        received_headers: list[dict] = []

        async def handle_callback(request: web.Request) -> web.Response:
            body = await request.json()
            captured_payloads.append(body)
            received_headers.append(dict(request.headers))
            return web.json_response({"status": "ok"})

        callback_app = web.Application()
        callback_app.router.add_post("/callback", handle_callback)
        runner = web.AppRunner(callback_app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        real_url = f"http://127.0.0.1:{port}/callback"

        # 3. Update the agent's callback_url to point to the real server
        # (We can't use register_agent's update path, so we directly update via SQL)
        with reg_store._tx() as eng:
            eng.execute(
                "UPDATE agents SET callback_url=? WHERE id=?",
                (real_url, agent_id),
            )

        # 4. Create a Dispatcher with registry_store and http_session
        config = DispatcherConfig(
            poll_interval=3600,
            claim_ttl=DEFAULT_CLAIM_TTL,
            failure_limit=3,
            dispatcher_id="test-callback-dispatcher",
            worker_command="echo",
        )
        dispatcher = Dispatcher(
            store, ws_mgr, config,
            ws_connections={},
            registry_store=reg_store,
            http_session=http_session,
        )

        # 5. Create a ready task assigned to this agent
        task = store.create_task(
            title="callback-task",
            assignee=agent_id,
        )
        assert task.status == TaskStatus.READY.value

        # 6. Trigger a poll cycle
        stats = await dispatcher.trigger_poll_cycle()
        assert stats["tasks_claimed"] >= 1, \
            f"Task should be claimed via callback, got stats={stats}"

        # 7. Verify the task is now running
        refreshed = store.get_task(task.id)
        assert refreshed is not None
        assert refreshed.status == TaskStatus.RUNNING.value

        # 8. Verify the callback server received the payload
        assert len(captured_payloads) == 1, \
            f"Expected 1 callback, got {len(captured_payloads)}: {captured_payloads}"
        payload = captured_payloads[0]
        assert payload["type"] == "task"
        assert payload["id"] == task.id
        assert payload["title"] == "callback-task"
        assert payload["kanban"] is True
        assert "workspace_path" in payload

        # 9. Verify Bearer token was sent
        assert len(received_headers) == 1
        auth_header = received_headers[0].get("Authorization", "")
        assert auth_header.startswith("Bearer ")
        assert auth_header == f"Bearer {callback_token}", \
            f"Expected Bearer {callback_token}, got {auth_header}"

        # 10. Verify the task is tracked in dispatched_ws_tasks
        assert task.id in dispatcher._dispatched_ws_tasks

        await runner.cleanup()

    async def test_callback_dispatch_fails_on_http_error(
        self,
        store: TaskStore,
        reg_store: RegistryStore,
        ws_mgr: WorkspaceManager,
        http_session: aiohttp.ClientSession,
    ) -> None:
        """If the callback returns HTTP 400+, the task should be marked failed."""
        # Register callback agent
        agent_id = reg_store.register_agent({
            "name": "callback-agent-2",
            "description": "Failing callback agent",
            "preferred_channel": "callback",
            "callback_url": "http://localhost:19999/callback",
            "supported_interfaces": [{
                "url": "http://localhost:19999",
                "protocol_binding": "JSONRPC",
                "protocol_version": "1.0",
            }],
        })

        # Set up a callback server that returns 500
        callback_app = web.Application()

        async def handle_fail(request: web.Request) -> web.Response:
            return web.json_response({"error": "internal"}, status=500)

        callback_app.router.add_post("/callback", handle_fail)
        runner = web.AppRunner(callback_app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        real_url = f"http://127.0.0.1:{port}/callback"

        with reg_store._tx() as eng:
            eng.execute(
                "UPDATE agents SET callback_url=? WHERE id=?",
                (real_url, agent_id),
            )

        config = DispatcherConfig(
            poll_interval=3600,
            claim_ttl=DEFAULT_CLAIM_TTL,
            failure_limit=3,
            dispatcher_id="test-callback-fail-dispatcher",
            worker_command="echo",
        )
        dispatcher = Dispatcher(
            store, ws_mgr, config,
            ws_connections={},
            registry_store=reg_store,
            http_session=http_session,
        )

        task = store.create_task(
            title="callback-fail-task",
            assignee=agent_id,
        )

        stats = await dispatcher.trigger_poll_cycle()
        # Task should NOT be claimed (callback failed, status set to failed)
        refreshed = store.get_task(task.id)
        assert refreshed is not None
        assert refreshed.status == TaskStatus.FAILED.value, \
            f"Task should be failed after HTTP error, got {refreshed.status}"

        await runner.cleanup()

    async def test_registry_store_missing_falls_through_to_worker_command(
        self,
        store: TaskStore,
        ws_mgr: WorkspaceManager,
    ) -> None:
        """Without registry_store, the dispatcher falls through to worker_command."""
        config = DispatcherConfig(
            poll_interval=3600,
            claim_ttl=DEFAULT_CLAIM_TTL,
            failure_limit=3,
            dispatcher_id="test-no-registry",
            worker_command="echo",
        )
        dispatcher = Dispatcher(
            store, ws_mgr, config,
            ws_connections=None,
            registry_store=None,
            http_session=None,
        )

        task = store.create_task(
            title="no-registry-task",
            assignee="worker-1",
        )

        stats = await dispatcher.trigger_poll_cycle()
        # Without registry_store and without ws_connections, the dispatcher
        # falls through to the worker_command path (backward compatible).
        refreshed = store.get_task(task.id)
        assert refreshed is not None
        assert refreshed.status == TaskStatus.RUNNING.value

    async def test_callback_dispatch_no_http_session(
        self,
        store: TaskStore,
        reg_store: RegistryStore,
        ws_mgr: WorkspaceManager,
    ) -> None:
        """Without http_session, the dispatcher should create a temporary one."""
        agent_id = reg_store.register_agent({
            "name": "callback-agent-3",
            "description": "No session agent",
            "preferred_channel": "callback",
            "callback_url": "http://localhost:19999/callback",
            "supported_interfaces": [{
                "url": "http://localhost:19999",
                "protocol_binding": "JSONRPC",
                "protocol_version": "1.0",
            }],
        })

        captured_payloads: list[dict] = []
        callback_app = web.Application()

        async def handle_callback(request: web.Request) -> web.Response:
            body = await request.json()
            captured_payloads.append(body)
            return web.json_response({"status": "ok"})

        callback_app.router.add_post("/callback", handle_callback)
        runner = web.AppRunner(callback_app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        real_url = f"http://127.0.0.1:{port}/callback"

        with reg_store._tx() as eng:
            eng.execute(
                "UPDATE agents SET callback_url=? WHERE id=?",
                (real_url, agent_id),
            )

        config = DispatcherConfig(
            poll_interval=3600,
            claim_ttl=DEFAULT_CLAIM_TTL,
            failure_limit=3,
            dispatcher_id="test-no-session",
            worker_command="echo",
        )
        dispatcher = Dispatcher(
            store, ws_mgr, config,
            ws_connections={},
            registry_store=reg_store,
            http_session=None,  # test without session
        )

        task = store.create_task(
            title="no-session-task",
            assignee=agent_id,
        )

        stats = await dispatcher.trigger_poll_cycle()
        assert stats["tasks_claimed"] >= 1

        refreshed = store.get_task(task.id)
        assert refreshed is not None
        assert refreshed.status == TaskStatus.RUNNING.value

        assert len(captured_payloads) == 1
        assert captured_payloads[0]["id"] == task.id

        await runner.cleanup()