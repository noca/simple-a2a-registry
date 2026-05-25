"""Shared fixtures for benchmarks: real TaskStore, WorkspaceManager, Dispatcher."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Generator

import pytest

from simple_a2a_registry.orchestration.store import TaskStore, DEFAULT_CLAIM_TTL
from simple_a2a_registry.orchestration.workspace import WorkspaceManager
from simple_a2a_registry.orchestration.dispatcher import (
    Dispatcher,
    DispatcherConfig,
)


@pytest.fixture
def db_path() -> Generator[str, None, None]:
    """Create a tempfile path for the SQLite DB (benchmark-sized)."""
    with tempfile.NamedTemporaryFile(suffix=".bench.db", delete=False) as f:
        path = f.name
    try:
        yield path
    finally:
        if os.path.exists(path):
            os.unlink(path)


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
def noop_dispatcher(
    store: TaskStore, ws_mgr: WorkspaceManager,
) -> Dispatcher:
    """Dispatcher with no worker_command — acts as pipeline promoter only."""
    config = DispatcherConfig(
        poll_interval=3600,
        claim_ttl=DEFAULT_CLAIM_TTL,
        failure_limit=3,
        dispatcher_id="bench-dispatcher",
    )
    return Dispatcher(store, ws_mgr, config)


@pytest.fixture
def claiming_dispatcher(
    store: TaskStore, ws_mgr: WorkspaceManager,
) -> Dispatcher:
    """Dispatcher with worker_command — measures full claim+spawn overhead."""
    config = DispatcherConfig(
        poll_interval=3600,
        claim_ttl=DEFAULT_CLAIM_TTL,
        failure_limit=3,
        dispatcher_id="bench-dispatcher",
        worker_command="echo",
    )
    return Dispatcher(store, ws_mgr, config)

