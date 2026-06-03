"""YAML-defined declarative workflow engine — parse workflow YAML, create task topology.

Allows users to define complete multi-task workflows as YAML files, with
DAG dependencies, conditional branching, and per-task assignee/priority.

The workflow engine validates the YAML spec, then creates all tasks in the
correct dependency order, returning the mapping of logical task ids to
persistent task UUIDs.

Typical usage::

    from simple_a2a_registry.orchestration.workflow import (
        WorkflowSpec,
        apply_workflow,
        validate_workflow,
    )

    spec = WorkflowSpec.from_yaml_path("examples/conditional-workflow.yaml")
    errors = validate_workflow(spec)
    if errors:
        print("Validation errors:", errors)
    else:
        result = apply_workflow(store, spec)
        print("Created", len(result.task_ids), "tasks")

YAML format::

    name: "My Workflow"                    # required, human-friendly name
    description: "..."                     # optional
    tenant: "my-tenant"                    # optional, applied to all tasks
    created_by: "admin"                    # optional, defaults to "workflow-engine"

    tasks:
      - id: fetch-data                     # required, unique within workflow
        title: "Fetch data from source"    # required
        body: "..."                        # optional
        assignee: "coder"                  # required
        priority: 5                        # optional, default 0
        max_runtime_seconds: 600           # optional
        max_retries: 3                     # optional
        workspace_kind: "scratch"          # optional
        workspace_path: "/tmp/ws"          # optional

        depends_on:                        # optional, list of parent tasks
          - task: validate-input           # parent task id (required)
            condition: "success"           # optional condition string

      - id: validate-input
        title: "Validate input"
        body: "Check input integrity"
        assignee: "verifier"
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

import yaml

from simple_a2a_registry.orchestration.models import TaskStatus
from simple_a2a_registry.orchestration.store import TaskStore

logger = logging.getLogger("a2a_registry.orchestration.workflow")

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class WorkflowDependency:
    """A single dependency edge in the workflow DAG."""

    task: str  # parent task id (logical id within the workflow)
    condition: Optional[str] = None  # e.g. "success", "failure"


@dataclass
class WorkflowTaskSpec:
    """Specification for a single task within a workflow YAML definition."""

    id: str
    title: str
    body: Optional[str] = None
    assignee: Optional[str] = None
    priority: int = 0
    max_runtime_seconds: Optional[int] = None
    max_retries: Optional[int] = None
    workspace_kind: Optional[str] = None
    workspace_path: Optional[str] = None
    depends_on: List[WorkflowDependency] = field(default_factory=list)


@dataclass
class WorkflowSpec:
    """Complete workflow specification parsed from a YAML file."""

    name: str
    description: Optional[str] = None
    tenant: Optional[str] = None
    created_by: str = "workflow-engine"
    tasks: List[WorkflowTaskSpec] = field(default_factory=list)

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    @classmethod
    def from_yaml_str(cls, yaml_text: str) -> WorkflowSpec:
        """Parse a workflow definition from a raw YAML string."""
        data = yaml.safe_load(yaml_text)
        return cls._from_dict(data)

    @classmethod
    def from_yaml_path(cls, path: str) -> WorkflowSpec:
        """Read and parse a workflow definition from a YAML file path."""
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return cls._from_dict(data)

    @classmethod
    def _from_dict(cls, data: dict) -> WorkflowSpec:
        """Convert a parsed YAML dict into a :class:`WorkflowSpec`."""
        if not data or not isinstance(data, dict):
            raise ValueError("Workflow definition must be a non-empty YAML mapping")

        name = (data.get("name") or "").strip()
        if not name:
            raise ValueError("Workflow 'name' is required")

        description = data.get("description")
        tenant = data.get("tenant")
        created_by = data.get("created_by", "workflow-engine")

        raw_tasks = data.get("tasks", [])
        if not raw_tasks or not isinstance(raw_tasks, list):
            raise ValueError("Workflow must contain at least one task in 'tasks' list")

        task_specs: List[WorkflowTaskSpec] = []
        for i, raw in enumerate(raw_tasks):
            if not isinstance(raw, dict):
                raise ValueError(f"Task at index {i} must be a mapping, got {type(raw).__name__}")

            task_id = (raw.get("id") or "").strip()
            if not task_id:
                raise ValueError(f"Task at index {i} is missing required 'id' field")

            title = (raw.get("title") or "").strip()
            if not title:
                raise ValueError(f"Task '{task_id}' is missing required 'title' field")

            body = raw.get("body")
            assignee = raw.get("assignee")
            priority = raw.get("priority", 0)
            max_runtime_seconds = raw.get("max_runtime_seconds")
            max_retries = raw.get("max_retries")
            workspace_kind = raw.get("workspace_kind")
            workspace_path = raw.get("workspace_path")

            depends_on_raw = raw.get("depends_on", [])
            depends_on: List[WorkflowDependency] = []
            if depends_on_raw:
                if not isinstance(depends_on_raw, list):
                    raise ValueError(
                        f"Task '{task_id}' 'depends_on' must be a list"
                    )
                for j, dep in enumerate(depends_on_raw):
                    if isinstance(dep, str):
                        depends_on.append(WorkflowDependency(task=dep))
                    elif isinstance(dep, dict):
                        dep_task = (dep.get("task") or "").strip()
                        if not dep_task:
                            raise ValueError(
                                f"Task '{task_id}' depends_on[{j}] missing "
                                "required 'task' field"
                            )
                        depends_on.append(
                            WorkflowDependency(
                                task=dep_task,
                                condition=dep.get("condition"),
                            )
                        )
                    else:
                        raise ValueError(
                            f"Task '{task_id}' depends_on[{j}] must be "
                            f"a string or mapping, got {type(dep).__name__}"
                        )

            task_specs.append(
                WorkflowTaskSpec(
                    id=task_id,
                    title=title,
                    body=body,
                    assignee=assignee,
                    priority=priority,
                    max_runtime_seconds=max_runtime_seconds,
                    max_retries=max_retries,
                    workspace_kind=workspace_kind,
                    workspace_path=workspace_path,
                    depends_on=depends_on,
                )
            )

        return cls(
            name=name,
            description=description,
            tenant=tenant,
            created_by=created_by,
            tasks=task_specs,
        )

    def to_dict(self) -> dict:
        """Export the workflow spec back to a dict (for serialization)."""
        return {
            "name": self.name,
            "description": self.description,
            "tenant": self.tenant,
            "created_by": self.created_by,
            "tasks": [
                {
                    "id": t.id,
                    "title": t.title,
                    "body": t.body,
                    "assignee": t.assignee,
                    "priority": t.priority,
                    "max_runtime_seconds": t.max_runtime_seconds,
                    "max_retries": t.max_retries,
                    "workspace_kind": t.workspace_kind,
                    "workspace_path": t.workspace_path,
                    "depends_on": [
                        {"task": d.task, "condition": d.condition}
                        if d.condition
                        else d.task
                        for d in t.depends_on
                    ],
                }
                for t in self.tasks
            ],
        }

    def to_yaml(self) -> str:
        """Export the workflow spec back to a YAML string."""
        return yaml.safe_dump(self.to_dict(), allow_unicode=True, sort_keys=False)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_workflow(spec: WorkflowSpec) -> List[str]:
    """Validate a workflow spec for correctness.

    Checks performed:
    - All task IDs are unique
    - All dependency references point to existing tasks
    - No circular dependencies in the DAG

    Returns:
        A list of error messages.  Empty list means the workflow is valid.
    """
    errors: List[str] = []

    if not spec.name:
        errors.append("Workflow name is required")

    if not spec.tasks:
        errors.append("Workflow must contain at least one task")
        return errors

    # Check unique IDs
    seen_ids: Set[str] = set()
    for t in spec.tasks:
        if t.id in seen_ids:
            errors.append(f"Duplicate task id '{t.id}'")
        seen_ids.add(t.id)

    # Build id -> task mapping for dependency lookup
    id_map: Dict[str, WorkflowTaskSpec] = {t.id: t for t in spec.tasks}

    # Check dependency references and cycles
    for t in spec.tasks:
        for dep in t.depends_on:
            if dep.task not in id_map:
                errors.append(
                    f"Task '{t.id}' depends on '{dep.task}' "
                    f"which does not exist in the workflow"
                )

    # Hard cycle detection (only if no conflicting refs)
    if not errors:
        cycle_errors = _detect_workflow_cycles(spec.tasks)
        errors.extend(cycle_errors)

    return errors


def _detect_workflow_cycles(tasks: List[WorkflowTaskSpec]) -> List[str]:
    """Detect cycles in the workflow DAG using DFS.

    Returns a list of error messages, empty if no cycles.
    """
    id_map: Dict[str, WorkflowTaskSpec] = {t.id: t for t in tasks}
    WHITE, GRAY, BLACK = 0, 1, 2
    color: Dict[str, int] = {t.id: WHITE for t in tasks}
    parent: Dict[str, Optional[str]] = {t.id: None for t in tasks}
    cycle_found: List[str] = []

    def dfs(node_id: str) -> None:
        color[node_id] = GRAY
        task = id_map.get(node_id)
        if task is None:
            color[node_id] = BLACK
            return
        for dep in task.depends_on:
            dep_id = dep.task
            if dep_id not in color:
                continue  # referenced id but not in our task set (handled earlier)
            if color[dep_id] == GRAY:
                # Cycle detected — reconstruct path
                path = [dep_id]
                cur: Optional[str] = node_id
                while cur is not None and cur != dep_id:
                    path.append(cur)
                    cur = parent.get(cur)
                path.append(dep_id)
                path.reverse()
                cycle_found.append(
                    f"Circular dependency detected: {' → '.join(path)}"
                )
            elif color[dep_id] == WHITE:
                parent[dep_id] = node_id
                dfs(dep_id)
        color[node_id] = BLACK

    for t in tasks:
        if color[t.id] == WHITE:
            dfs(t.id)

    return cycle_found


# ---------------------------------------------------------------------------
# Application (create tasks in store)
# ---------------------------------------------------------------------------


@dataclass
class WorkflowResult:
    """Result of applying a workflow to the store."""

    name: str
    task_ids: Dict[str, str]  # logical id → persistent task id
    created_count: int
    errors: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "task_ids": self.task_ids,
            "created_count": self.created_count,
            "errors": self.errors,
        }


def apply_workflow(
    store: TaskStore,
    spec: WorkflowSpec,
    *,
    dry_run: bool = False,
) -> WorkflowResult:
    """Apply a workflow spec to the task store, creating all tasks and dependencies.

    Tasks are created in topological order (roots first, then dependents).
    Dependencies with conditions are preserved as ``{parent_id, condition}``
    specs passed to ``create_task(parents=[...])``.

    Args:
        store: The :class:`TaskStore` to create tasks in.
        spec: The parsed workflow specification.
        dry_run:
            If ``True``, validate the workflow and report what would be created
            without actually creating anything.

    Returns:
        A :class:`WorkflowResult` with the mapping of logical ids to persistent
        task ids and any errors encountered.
    """
    # Validate first
    validation_errors = validate_workflow(spec)
    if validation_errors:
        return WorkflowResult(
            name=spec.name,
            task_ids={},
            created_count=0,
            errors=validation_errors,
        )

    id_map: Dict[str, WorkflowTaskSpec] = {t.id: t for t in spec.tasks}

    # Topological sort: roots first (tasks with no deps), then dependents
    sorted_tasks = _topological_sort(spec.tasks)

    if dry_run:
        # Return what would be created
        return WorkflowResult(
            name=spec.name,
            task_ids={t.id: f"(would create: {t.id})" for t in sorted_tasks},
            created_count=len(sorted_tasks),
        )

    # Create tasks in topological order
    created: Dict[str, str] = {}  # logical id → persistent task id
    errors: List[str] = []

    for task_spec in sorted_tasks:
        try:
            # Build parents list: resolve logical ids to persistent task ids
            parents: List = []
            for dep in task_spec.depends_on:
                parent_persistent_id = created.get(dep.task)
                if parent_persistent_id is None:
                    errors.append(
                        f"Task '{task_spec.id}': parent '{dep.task}' "
                        "not yet created (topological ordering issue)"
                    )
                    continue
                if dep.condition:
                    parents.append({
                        "parent_id": parent_persistent_id,
                        "condition": dep.condition,
                    })
                else:
                    parents.append({
                        "parent_id": parent_persistent_id,
                    })

            # Determine initial status metadata
            body = task_spec.body or ""
            if spec.description:
                body = f"# {spec.name}\n\n{spec.description}\n\n---\n\n" + body

            task = store.create_task(
                title=task_spec.title,
                body=body if body else None,
                assignee=task_spec.assignee,
                priority=task_spec.priority,
                parents=parents if parents else None,
                workspace_kind=task_spec.workspace_kind,
                workspace_path=task_spec.workspace_path,
                max_runtime_seconds=task_spec.max_runtime_seconds,
                max_retries=task_spec.max_retries,
                tenant=spec.tenant,
                created_by=spec.created_by,
            )
            created[task_spec.id] = task.id
            logger.info(
                "Workflow '%s': created task '%s' (persistent id=%s, assignee=%s, status=%s)",
                spec.name, task_spec.id, task.id, task.assignee, task.status,
            )

        except ValueError as e:
            errors.append(f"Task '{task_spec.id}': {e}")
        except Exception as e:
            errors.append(f"Task '{task_spec.id}': unexpected error: {e}")
            logger.exception("Unexpected error creating task '%s'", task_spec.id)

    if errors:
        logger.warning(
            "Workflow '%s' completed with %d error(s): %s",
            spec.name, len(errors), "; ".join(errors),
        )

    return WorkflowResult(
        name=spec.name,
        task_ids=created,
        created_count=len(created),
        errors=errors,
    )


# ---------------------------------------------------------------------------
# Topological sort
# ---------------------------------------------------------------------------


def _topological_sort(tasks: List[WorkflowTaskSpec]) -> List[WorkflowTaskSpec]:
    """Return tasks sorted in dependency order (roots first).

    Uses Kahn's algorithm (BFS-based).  If there are cycles, returns tasks
    in a best-effort order (cycles are caught earlier by validation).
    """
    id_map: Dict[str, WorkflowTaskSpec] = {t.id: t for t in tasks}
    in_degree: Dict[str, int] = {t.id: 0 for t in tasks}

    # Count incoming edges
    for t in tasks:
        for dep in t.depends_on:
            if dep.task in in_degree:
                in_degree[t.id] += 1

    # Start with zero in-degree nodes (roots)
    queue: List[str] = [tid for tid, deg in in_degree.items() if deg == 0]
    sorted_ids: List[str] = []

    while queue:
        node_id = queue.pop(0)
        sorted_ids.append(node_id)
        # Decrement in-degree for dependents
        for t in tasks:
            for dep in t.depends_on:
                if dep.task == node_id and t.id in in_degree:
                    in_degree[t.id] -= 1
                    if in_degree[t.id] == 0:
                        queue.append(t.id)

    # If we didn't visit all nodes, there's a cycle — append remaining
    if len(sorted_ids) < len(tasks):
        remaining = [t.id for t in tasks if t.id not in sorted_ids]
        sorted_ids.extend(remaining)

    return [id_map[sid] for sid in sorted_ids]