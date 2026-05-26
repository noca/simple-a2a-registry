"""Orchestration data models — Task, Run, Comment, Event types and enums.

All types use plain ``dataclasses`` (no Pydantic) following the project convention.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class TaskStatus(str, Enum):
    """Kanban task lifecycle — 8 states in progression order."""

    TODO = "todo"           # Created, waiting for assignee or parents to complete
    READY = "ready"         # All parents done, waiting for a worker to claim
    RUNNING = "running"     # Actively being worked on by a worker
    BLOCKED = "blocked"     # Blocked by human intervention (HITL)
    COMPLETED = "completed" # Finished successfully
    FAILED = "failed"       # Finished with failure (may retry)
    CANCELLED = "cancelled" # Cancelled before completion
    ARCHIVED = "archived"   # Terminal state — no further transitions


class TaskRunStatus(str, Enum):
    """Inner lifecycle of a single run attempt."""

    RUNNING = "running"
    DONE = "done"
    BLOCKED = "blocked"
    CRASHED = "crashed"
    TIMED_OUT = "timed_out"
    FAILED = "failed"
    RELEASED = "released"


class TaskRunOutcome(str, Enum):
    """High-level summary of how a run ended (for analytics / TTFB)."""

    COMPLETED = "completed"
    BLOCKED = "blocked"
    CRASHED = "crashed"
    TIMED_OUT = "timed_out"
    SPAWN_FAILED = "spawn_failed"
    GAVE_UP = "gave_up"
    RECLAIMED = "reclaimed"


class TaskEventKind(str, Enum):
    """Every meaningful transition or action on a task produces an event."""

    CREATED = "created"
    CLAIMED = "claimed"
    STARTED = "started"
    HEARTBEAT = "heartbeat"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"
    UNBLOCKED = "unblocked"
    COMMENTED = "commented"
    UPDATED = "updated"
    ARCHIVED = "archived"
    DEPENDENCY_PROMOTED = "dependency_promoted"
    CANCELLED = "cancelled"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class Task:
    """A single kanban task — the core unit of work in the orchestration engine.

    Maps directly to the ``tasks`` SQLite table.
    """

    id: str = ""
    title: str = ""
    body: Optional[str] = None
    assignee: Optional[str] = None
    status: str = TaskStatus.TODO.value
    priority: int = 0
    created_by: Optional[str] = None
    created_at: int = 0  # unix timestamp
    started_at: Optional[int] = None
    completed_at: Optional[int] = None
    workspace_kind: Optional[str] = None
    workspace_path: Optional[str] = None
    claim_lock: Optional[str] = None
    claim_expires: Optional[int] = None
    tenant: Optional[str] = None
    result: Optional[str] = None
    consecutive_failures: int = 0
    worker_pid: Optional[int] = None
    last_failure_error: Optional[str] = None
    max_runtime_seconds: Optional[int] = None
    last_heartbeat_at: Optional[int] = None
    current_run_id: Optional[int] = None
    max_retries: Optional[int] = None

    # Transient — loaded from related tables
    parents: List[dict] = field(default_factory=list)
    children: List[dict] = field(default_factory=list)
    runs: List[TaskRun] = field(default_factory=list)
    comments: List[TaskComment] = field(default_factory=list)
    events: List[TaskEvent] = field(default_factory=list)

    def ensure_id(self) -> str:
        """Generate a ``t_``+ short-UUID id if none is set."""
        if not self.id:
            self.id = "t_" + uuid.uuid4().hex[:8]
        return self.id

    def to_dict(self) -> Dict[str, Any]:
        """Return a plain dict, dropping ``None`` fields and transient lists."""
        d: Dict[str, Any] = {}
        for f_name in self.__dataclass_fields__:
            if f_name in ("parents", "children", "runs", "comments", "events"):
                continue
            val = getattr(self, f_name)
            if val is not None:
                d[f_name] = val
        # include status as string for JSON serialization
        d["status"] = self.status
        return d

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> Task:
        """Restore from a dict (inverse of ``to_dict``)."""
        # Filter to dataclass fields only
        valid = {k: v for k, v in data.items() if k in Task.__dataclass_fields__}
        return Task(**valid)


@dataclass
class TaskRun:
    """A single execution attempt for a task.

    Maps to the ``task_runs`` SQLite table.
    """

    id: int = 0
    task_id: str = ""
    profile: Optional[str] = None
    status: str = TaskRunStatus.RUNNING.value
    claim_lock: Optional[str] = None
    claim_expires: Optional[int] = None
    worker_pid: Optional[int] = None
    max_runtime_seconds: Optional[int] = None
    last_heartbeat_at: Optional[int] = None
    started_at: int = 0
    ended_at: Optional[int] = None
    outcome: Optional[str] = None
    summary: Optional[str] = None
    metadata: Optional[str] = None  # JSON blob
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {}
        for f_name in self.__dataclass_fields__:
            val = getattr(self, f_name)
            if val is not None:
                d[f_name] = val
        return d

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> TaskRun:
        valid = {k: v for k, v in data.items() if k in TaskRun.__dataclass_fields__}
        return TaskRun(**valid)


@dataclass
class TaskComment:
    """A human-readable comment attached to a task (HITL / collaboration).

    Maps to the ``task_comments`` SQLite table.
    """

    id: int = 0
    task_id: str = ""
    author: str = ""
    body: str = ""
    created_at: int = 0

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {}
        for f_name in self.__dataclass_fields__:
            val = getattr(self, f_name)
            if val is not None:
                d[f_name] = val
        return d

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> TaskComment:
        valid = {k: v for k, v in data.items() if k in TaskComment.__dataclass_fields__}
        return TaskComment(**valid)


@dataclass
class TaskEvent:
    """An immutable audit-log entry for every meaningful task change.

    Maps to the ``task_events`` SQLite table.
    """

    id: int = 0
    task_id: str = ""
    run_id: Optional[int] = None
    kind: str = ""
    payload: Optional[str] = None  # JSON blob
    created_at: int = 0

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {}
        for f_name in self.__dataclass_fields__:
            val = getattr(self, f_name)
            if val is not None:
                d[f_name] = val
        return d

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> TaskEvent:
        valid = {k: v for k, v in data.items() if k in TaskEvent.__dataclass_fields__}
        return TaskEvent(**valid)
