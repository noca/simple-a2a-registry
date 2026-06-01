"""Swarm topology management — create, query, blackboard.

Swarm is a parallel multi-agent coordination topology built on top of the
existing v2/tasks infrastructure.  It reuses the Registry's dependency engine,
task store, and dispatcher without introducing new tables or modifying state
machines.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from simple_a2a_registry.orchestration.models import (
    TaskComment,
    TaskStatus,
)
from simple_a2a_registry.orchestration.store import TaskStore

logger = logging.getLogger("a2a_registry.orchestration.swarm")

# Blackboard prefix — compatible with Hermes Kanban Swarm (trailing space).
BLACKBOARD_PREFIX = "[swarm:blackboard]"


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SwarmWorkerSpec:
    """Specification for a single swarm worker task."""

    profile: str
    title: str
    body: str = ""
    skills: list[str] = field(default_factory=list)
    priority: int = 0
    max_runtime_seconds: Optional[int] = None


@dataclass(frozen=True)
class SwarmCreated:
    """Result of a successful swarm creation."""

    root_id: str
    worker_ids: list[str]
    verifier_id: str
    synthesizer_id: str


# ---------------------------------------------------------------------------
# Blackboard helpers
# ---------------------------------------------------------------------------


def _build_swarm_context(root_id: str, goal: str) -> str:
    """Build the Swarm protocol context suffix appended to every task body."""
    return (
        "\n\n## Swarm 协议\n"
        f"- Swarm 根任务 / 共享黑板：`{root_id}`\n"
        "- 所有 Worker 并行执行。通过根任务的结构化评论分享中间成果\n"
        "- 将机器可读的结构化信息放在完成（complete）的 metadata 中\n"
        "- 将跨 Worker 的备注用结构化评论（[swarm:blackboard]JSON）放在根任务上\n"
        f"- 目标：{goal}"
    )


def _make_blackboard_body(key: str, value: Any) -> str:
    """Create a comment body with the blackboard prefix.

    Args:
        key: The blackboard key (e.g. "topology", "phase1_result").
        value: A JSON-serialisable value.

    Returns:
        A string like ``[swarm:blackboard]{"key":"topology","value":...}``.
    """
    payload = json.dumps({"key": key, "value": value}, ensure_ascii=False)
    return f"{BLACKBOARD_PREFIX}{payload}"


def _apply_body_augmentation(
    body: str,
    swarm_context: str,
    skills: Optional[list[str]] = None,
) -> str:
    """Append swarm context and skills hint to a task body."""
    augmented = body + swarm_context
    if skills:
        augmented += f"\n- 所需技能：{', '.join(skills)}"
    return augmented


# ---------------------------------------------------------------------------
# Core API
# ---------------------------------------------------------------------------


def create_swarm(
    store: TaskStore,
    *,
    goal: str,
    workers: list[SwarmWorkerSpec],
    verifier_profile: str,
    synthesizer_profile: str,
    root_title: Optional[str] = None,
    verifier_title: str = "Verify swarm outputs",
    synthesizer_title: str = "Synthesize swarm outputs",
    tenant: Optional[str] = None,
    created_by: str = "swarm-orchestrator",
    priority: int = 0,
) -> SwarmCreated:
    """Create a full Swarm topology.

    1. Create Root task and immediately complete it (metadata=swarm_v1).
    2. Create N Worker tasks with ``parents=[root]``.
    3. Create Verifier task with ``parents=worker_ids``.
    4. Create Synthesizer task with ``parents=[verifier]``.
    5. Write topology info to the blackboard.
    """
    if not workers:
        raise ValueError("At least one worker is required")
    for w in workers:
        if not w.profile:
            raise ValueError("Each worker must have a profile")
        if not w.title:
            raise ValueError("Each worker must have a title")
    if not verifier_profile:
        raise ValueError("Verifier profile is required")
    if not synthesizer_profile:
        raise ValueError("Synthesizer profile is required")

    root = store.create_task(
        title=root_title or f"Swarm: {goal[:60]}",
        body=f"# Swarm Root: {goal}\n\nSwarm orchestration root task.\n\n"
             f"Workers: {len(workers)}\n"
             f"Verifier: {verifier_profile}\n"
             f"Synthesizer: {synthesizer_profile}",
        assignee="swarm-orchestrator",
        priority=priority,
        tenant=tenant,
        created_by=created_by,
    )
    root_id = root.id

    # Build shared swarm context suffix.
    swarm_context = _build_swarm_context(root_id, goal)

    # Create Worker tasks (parents=[root]).
    worker_ids: list[str] = []
    for w in workers:
        body = _apply_body_augmentation(w.body, swarm_context, w.skills)
        task = store.create_task(
            title=w.title,
            body=body,
            assignee=w.profile,
            priority=w.priority if w.priority else priority,
            parents=[root_id],
            max_runtime_seconds=w.max_runtime_seconds,
            tenant=tenant,
            created_by=created_by,
        )
        worker_ids.append(task.id)

    # Create Verifier task (parents=worker_ids).
    verifier_body = (
        f"Verify the outputs from {len(workers)} swarm workers.\n\n"
        "Read all worker results, then decide:\n"
        "- If all pass → complete with metadata.gate='pass'\n"
        "- If any fail → block this task with reasons\n"
    ) + swarm_context
    verifier = store.create_task(
        title=verifier_title,
        body=verifier_body,
        assignee=verifier_profile,
        priority=priority,
        parents=worker_ids,
        tenant=tenant,
        created_by=created_by,
    )

    # Create Synthesizer task (parents=[verifier]).
    synth_body = (
        f"Synthesize the results from {len(workers)} swarm workers.\n\n"
        "Read all worker results and blackboard entries, then produce "
        "a consolidated output.\n"
    ) + swarm_context
    synthesizer = store.create_task(
        title=synthesizer_title,
        body=synth_body,
        assignee=synthesizer_profile,
        priority=priority,
        parents=[verifier.id],
        tenant=tenant,
        created_by=created_by,
    )

    # Now complete the root task.  Since all children already exist as
    # ``todo`` with parent links, completing the root triggers dependency
    # resolution which promotes workers to ``ready``.
    # State machine: ready → running → completed
    store.update_task_status(root_id, TaskStatus.RUNNING.value)
    store.update_task_status(
        root_id,
        TaskStatus.COMPLETED.value,
        metadata={
            "kind": "kanban_swarm_v1",
            "goal": goal,
            "worker_count": len(workers),
        },
    )

    # Write topology to blackboard.
    topology = {
        "goal": goal,
        "root_id": root_id,
        "worker_ids": worker_ids,
        "verifier_id": verifier.id,
        "synthesizer_id": synthesizer.id,
        "worker_specs": [
            {"profile": w.profile, "title": w.title, "skills": w.skills}
            for w in workers
        ],
        "verifier_profile": verifier_profile,
        "synthesizer_profile": synthesizer_profile,
    }
    post_blackboard(store, root_id, author=created_by, key="topology", value=topology)

    logger.info(
        "Swarm created: root=%s workers=%d verifier=%s synthesizer=%s",
        root_id, len(workers), verifier.id, synthesizer.id,
    )

    return SwarmCreated(
        root_id=root_id,
        worker_ids=worker_ids,
        verifier_id=verifier.id,
        synthesizer_id=synthesizer.id,
    )


def post_blackboard(
    store: TaskStore,
    root_id: str,
    *,
    author: str,
    key: str,
    value: Any,
) -> TaskComment:
    """Write a key→value update to the Swarm blackboard.

    The update is persisted as a structured comment on the root task with the
    ``[swarm:blackboard]`` prefix so it can be aggregated later.

    Args:
        store: TaskStore instance.
        root_id: The Swarm root task id.
        author: Who is writing the update.
        key: Blackboard key (e.g. ``"phase1_result"``).
        value: Any JSON-serialisable value.

    Returns:
        The created TaskComment.
    """
    body = _make_blackboard_body(key, value)
    return store.add_comment(root_id, author, body)


def read_blackboard(
    store: TaskStore,
    root_id: str,
) -> dict:
    """Read the Swarm blackboard, aggregating all ``[swarm:blackboard]`` comments.

    Later writes for the same key overwrite earlier ones.  The result includes
    an ``_authors`` dict mapping each key to its most recent author.

    Args:
        store: TaskStore instance.
        root_id: The Swarm root task id.

    Returns:
        A dict where each ``[swarm:blackboard]`` key maps to its latest value,
        plus ``_authors`` tracking authorship.
    """
    comments = store.get_comments(root_id)
    blackboard: Dict[str, Any] = {}
    authors: Dict[str, str] = {}

    for c in comments:
        body: str = c.body or ""
        # Compatible with both [swarm:blackboard] and [swarm:blackboard]  (trailing space).
        stripped = body
        if stripped.startswith(BLACKBOARD_PREFIX):
            stripped = stripped[len(BLACKBOARD_PREFIX):].lstrip(" ")
        elif stripped.startswith(BLACKBOARD_PREFIX + " "):
            stripped = stripped[len(BLACKBOARD_PREFIX) + 1:]
        else:
            continue

        try:
            data = json.loads(stripped)
        except json.JSONDecodeError:
            logger.debug("Skipping non-JSON blackboard comment on task %s", root_id)
            continue

        key = data.get("key")
        value = data.get("value")
        if key is not None:
            blackboard[key] = value
            authors[key] = c.author

    blackboard["_authors"] = authors
    return blackboard


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------


def get_swarm_status(
    store: TaskStore,
    root_id: str,
) -> Optional[dict]:
    """Fetch the full Swarm topology status.

    Args:
        store: TaskStore instance.
        root_id: The Swarm root task id.

    Returns:
        A dict with ``swarm``, ``workers``, ``verifier``, ``synthesizer``,
        and ``blackboard`` keys, or ``None`` if the root task doesn't exist.
    """
    root = store.get_task(root_id)
    if root is None:
        return None

    # Find children: workers, verifier, synthesizer
    children = store.get_children(root_id)
    blackboard = read_blackboard(store, root_id)
    topology = blackboard.get("topology", {})

    worker_ids: list[str] = topology.get("worker_ids", [])
    verifier_id: str = topology.get("verifier_id", "")
    synthesizer_id: str = topology.get("synthesizer_id", "")

    # Fallback: derive from children if topology not yet in blackboard.
    if not worker_ids and children:
        child_ids = [c["id"] for c in children]
        # Heuristic: verifier and synthesizer are the last two children.
        if len(child_ids) >= 2:
            synthesizer_id = child_ids[-1]
            verifier_id = child_ids[-2]
            worker_ids = child_ids[:-2]
        elif len(child_ids) >= 1:
            verifier_id = child_ids[0]
            worker_ids = []

    # Fetch each task's status.
    def _task_brief(tid: str) -> Optional[dict]:
        t = store.get_task(tid)
        if t is None:
            return None
        return {
            "id": t.id,
            "status": t.status,
            "assignee": t.assignee,
            "created_at": t.created_at,
            "started_at": t.started_at,
            "completed_at": t.completed_at,
        }

    workers_status = []
    for wid in worker_ids:
        w = _task_brief(wid)
        if w:
            workers_status.append(w)

    v = _task_brief(verifier_id) if verifier_id else None
    s = _task_brief(synthesizer_id) if synthesizer_id else None

    return {
        "swarm": {
            "root_id": root_id,
            "status": root.status,
            "worker_ids": worker_ids,
            "verifier_id": verifier_id,
            "synthesizer_id": synthesizer_id,
        },
        "workers": workers_status,
        "verifier": v,
        "synthesizer": s,
        "blackboard": blackboard,
    }