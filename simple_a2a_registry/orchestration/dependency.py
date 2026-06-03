"""Dependency chain engine — cycle detection, DAG resolution, promotion logic.

Pure functions operating on a SQLite cursor, extracted from the store layer
so they can be tested and reasoned about independently.

Usage::

    from simple_a2a_registry.orchestration.dependency import (
        detect_cycle,
        resolve_dependencies,
        promote_children,
    )

    with store._tx() as cur:
        if detect_cycle(cur, child_id, parent_id):
            raise ValueError("cycle detected")
        resolve_dependencies(cur, task_id)
"""

from __future__ import annotations

import sqlite3
from typing import List, Optional

from simple_a2a_registry.orchestration.models import (
    TaskEventKind,
    TaskStatus,
)


# ---------------------------------------------------------------------------
# Cycle detection  (DFS via child chain)
# ---------------------------------------------------------------------------


def detect_cycle(
    cur: sqlite3.Cursor,
    child_id: str,
    proposed_parent_id: str,
) -> bool:
    """Return ``True`` if adding ``proposed_parent_id → child_id`` would create
    a cycle in the DAG.

    The check walks the *parent* direction from *proposed_parent_id*: if we
    ever reach *child_id* (meaning *child_id* is already an ancestor of
    *proposed_parent_id*, so a new edge would close the loop), we have a cycle.

    This is the standard reverse DFS algorithm described in the architecture
    doc (section 6.2).
    """
    if child_id == proposed_parent_id:
        return True  # self-link

    visited: set = set()
    stack: List[str] = [proposed_parent_id]

    while stack:
        node = stack.pop()
        if node == child_id:
            return True
        if node in visited:
            continue
        visited.add(node)
        # Follow the **parent** chain of *node*:
        # find all tasks that *node* depends on as a child.
        cur.execute(
            "SELECT parent_id FROM task_links WHERE child_id=?",
            (node,),
        )
        for (pid,) in cur.fetchall():
            stack.append(pid)

    return False


# ---------------------------------------------------------------------------
# Dependency resolution  (todo ↔ ready promotion/demotion)
# ---------------------------------------------------------------------------


def resolve_dependencies(
    cur: sqlite3.Cursor,
    task_id: str,
    _emit_event_func=None,
) -> None:
    """Check whether *task_id*'s parent dependencies are satisfied and update
    its status accordingly.

    - If all parents are ``completed``/``archived`` (and their condition, if
      any, is satisfied) and the task is currently ``todo``, promote it to
      ``ready``.
    - If a parent has been re-activated and the task is currently ``ready``,
      demote it back to ``todo``.

    For parent links with a ``condition``, the parent is only considered
    satisfied if its ``result`` field matches the condition string
    (exact match).
    """
    cur.execute(
        "SELECT p.status, p.result, l.condition AS link_condition "
        "FROM task_links l "
        "JOIN tasks p ON l.parent_id = p.id "
        "WHERE l.child_id=?",
        (task_id,),
    )
    parent_rows = cur.fetchall()

    cur.execute("SELECT status FROM tasks WHERE id=?", (task_id,))
    row = cur.fetchone()
    if row is None:
        return
    current_status = row[0]

    if not parent_rows:
        # No parents at all — if currently todo, promote to ready.
        if current_status == TaskStatus.TODO.value:
            cur.execute(
                "UPDATE tasks SET status=? WHERE id=?",
                (TaskStatus.READY.value, task_id),
            )
            if _emit_event_func:
                _emit_event_func(
                    cur, task_id, TaskEventKind.DEPENDENCY_PROMOTED.value,
                    payload={"from": "todo", "to": "ready", "reason": "no_parents"},
                )
        return

    all_satisfied = True
    for row in parent_rows:
        p_status = row[0] if isinstance(row, tuple) else row["status"]
        # Parent must be completed/archived
        if p_status not in (TaskStatus.COMPLETED.value, TaskStatus.ARCHIVED.value):
            all_satisfied = False
            break
        # If the link has a condition, parent's result must match
        link_condition = row[2] if isinstance(row, tuple) else row.get("link_condition") or row.get("condition")
        if link_condition:
            p_result = row[1] if isinstance(row, tuple) else row.get("result")
            if not p_result or p_result != link_condition:
                all_satisfied = False
                break

    if all_satisfied and current_status == TaskStatus.TODO.value:
        cur.execute(
            "UPDATE tasks SET status=? WHERE id=?",
            (TaskStatus.READY.value, task_id),
        )
        if _emit_event_func:
            _emit_event_func(
                cur, task_id, TaskEventKind.DEPENDENCY_PROMOTED.value,
                payload={"from": "todo", "to": "ready"},
            )

    elif not all_satisfied and current_status == TaskStatus.READY.value:
        cur.execute(
            "UPDATE tasks SET status=? WHERE id=?",
            (TaskStatus.TODO.value, task_id),
        )
        if _emit_event_func:
            _emit_event_func(
                cur, task_id, TaskEventKind.DEPENDENCY_PROMOTED.value,
                payload={"from": "ready", "to": "todo"},
            )


def promote_children(
    cur: sqlite3.Cursor,
    task_id: str,
    _emit_event_func=None,
) -> int:
    """After *task_id* is completed, re-resolve all tasks that depend on it
    (its immediate children).

    Returns the number of children resolved (promoted or demoted).
    """
    cur.execute(
        "SELECT child_id FROM task_links WHERE parent_id=?",
        (task_id,),
    )
    children = [r[0] for r in cur.fetchall()]
    for child_id in children:
        resolve_dependencies(cur, child_id, _emit_event_func)
    return len(children)


# ---------------------------------------------------------------------------
# DAG integrity helpers
# ---------------------------------------------------------------------------


def validate_dag_integrity(
    cur: sqlite3.Cursor,
    parent_ids: List[str],
    task_id: str,
) -> List[str]:
    """Validate that all items in *parent_ids* exist, none is a self-link,
    and adding them does not create a cycle.

    Returns the successfully validated *parent_ids* (all-pass when no error).

    Raises:
        ValueError: If any validation check fails.
    """
    for pid in parent_ids:
        # Existence check
        cur.execute("SELECT id FROM tasks WHERE id=?", (pid,))
        if cur.fetchone() is None:
            raise ValueError(f"Parent task '{pid}' not found")

        # Self-link check
        if pid == task_id:
            raise ValueError("Self-links are not allowed")

        # Cycle check
        if detect_cycle(cur, task_id, pid):
            raise ValueError(
                f"Cannot add parent '{pid}' — would create a cycle"
            )

    return parent_ids