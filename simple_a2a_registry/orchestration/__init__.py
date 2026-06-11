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
from simple_a2a_registry.orchestration.swarm import (
    SwarmWorkerSpec,
    SwarmCreated,
    create_swarm,
    post_blackboard,
    read_blackboard,
    get_swarm_status,
)
from simple_a2a_registry.orchestration.swarm_routes import (
    SwarmHandler,
    register_swarm_routes,
)
from simple_a2a_registry.orchestration.anomaly_scanner import (
    AnomalyScanner,
)
from simple_a2a_registry.orchestration.sla import (
    SlaCalculator,
    SlaUpdater,
    SlaSnapshot,
    WindowStat,
)
from simple_a2a_registry.orchestration.memory import (
    AgentMemoryStore,
    _maybe_create_memory_schema,
)
from simple_a2a_registry.orchestration.memory_routes import (
    MemoryHandler,
    register_memory_routes,
)
from simple_a2a_registry.orchestration.blackboard_store import (
    BlackboardStore,
    OptimisticLockError,
    KeyNotFoundError,
)
from simple_a2a_registry.orchestration.cron import (
    CronTask,
    CronExecution,
    CronTaskStore,
    CronScheduler,
    _maybe_create_schema as _maybe_create_cron_schema,
)
from simple_a2a_registry.orchestration.cron_routes import (
    CronHandler,
    register_cron_routes,
)
from simple_a2a_registry.orchestration.workflow_routes import (
    WorkflowHandler,
    register_workflow_routes,
)
from simple_a2a_registry.orchestration.shared_workspace import (
    SharedWorkspaceManager,
)
from simple_a2a_registry.orchestration.shared_workspace_routes import (
    SharedWorkspaceHandler,
    register_shared_workspace_routes,
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
    "AnomalyScanner",
    "SlaCalculator",
    "SlaUpdater",
    "SlaSnapshot",
    "WindowStat",
    "SwarmWorkerSpec",
    "SwarmCreated",
    "create_swarm",
    "post_blackboard",
    "read_blackboard",
    "get_swarm_status",
    "SwarmHandler",
    "register_swarm_routes",
    # P0-A: Agent Memory
    "AgentMemoryStore",
    "_maybe_create_memory_schema",
    "MemoryHandler",
    "register_memory_routes",
    # P2-F: Blackboard Store
    "BlackboardStore",
    "OptimisticLockError",
    "KeyNotFoundError",
    # P2-F: Cron Scheduler
    "CronTask",
    "CronExecution",
    "CronTaskStore",
    "CronScheduler",
    "_maybe_create_cron_schema",
    "CronHandler",
    "register_cron_routes",
    # P2-F: Workflow
    "WorkflowHandler",
    "register_workflow_routes",
    # P2-F: Shared Workspace
    "SharedWorkspaceManager",
    "SharedWorkspaceHandler",
    "register_shared_workspace_routes",
]