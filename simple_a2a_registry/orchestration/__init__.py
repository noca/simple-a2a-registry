"""Orchestration Engine — task lifecycle management with SQLite-backed Kanban."""

from simple_a2a_registry.orchestration.models import (
    Task,
    TaskComment,
    TaskEvent,
    TaskEventKind,
    TaskRun,
    TaskRunOutcome,
    TaskRunStatus,
    TaskStatus,
)
from simple_a2a_registry.orchestration.state_machine import (
    validate_transition,
    VALID_TRANSITIONS,
)
from simple_a2a_registry.orchestration.store import TaskStore, _maybe_create_schema
from simple_a2a_registry.orchestration.dependency import (
    detect_cycle,
    resolve_dependencies,
    promote_children,
)
from simple_a2a_registry.orchestration.routes import (
    OrchestrationHandler,
    register_v2_routes,
)
from simple_a2a_registry.orchestration.workspace import (
    WorkspaceManager,
    WorkspaceAllocationError,
    WorkspaceCleanupError,
    WorkspaceError,
)
from simple_a2a_registry.orchestration.dispatcher import (
    Dispatcher,
    DispatcherConfig,
)

__all__ = [
    "Task",
    "TaskRun",
    "TaskComment",
    "TaskEvent",
    "TaskStatus",
    "TaskRunStatus",
    "TaskRunOutcome",
    "TaskEventKind",
    "validate_transition",
    "VALID_TRANSITIONS",
    "TaskStore",
    "detect_cycle",
    "resolve_dependencies",
    "promote_children",
    "OrchestrationHandler",
    "register_v2_routes",
    "WorkspaceManager",
    "WorkspaceAllocationError",
    "WorkspaceCleanupError",
    "WorkspaceError",
    "Dispatcher",
    "DispatcherConfig",
]