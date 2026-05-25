"""SQLite-backed task store — the persistence layer for the orchestration engine.

All operations use ``BEGIN IMMEDIATE`` transactions to safely handle
concurrent writes.  The database is opened in WAL mode with ``busy_timeout=5000``.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional, Tuple

from simple_a2a_registry.orchestration.models import (
    Task,
    TaskComment,
    TaskEvent,
    TaskEventKind,
    TaskRun,
    TaskRunStatus,
    TaskStatus,
)
from simple_a2a_registry.orchestration.state_machine import (
    InvalidTransitionError,
    validate_transition,
)

logger = logging.getLogger("a2a_registry.orchestration.store")

# Default TTL for claim locks (15 minutes)
DEFAULT_CLAIM_TTL = 900
# Default number of retries before a task is permanently failed
DEFAULT_MAX_RETRIES = 3


# ---------------------------------------------------------------------------
# SQL creation
# ---------------------------------------------------------------------------

_SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA busy_timeout=5000;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS tasks (
    id                    TEXT PRIMARY KEY,
    title                 TEXT NOT NULL,
    body                  TEXT,
    assignee              TEXT,
    status                TEXT NOT NULL DEFAULT 'todo',
    priority              INTEGER NOT NULL DEFAULT 0,
    created_by            TEXT,
    created_at            INTEGER NOT NULL,
    started_at            INTEGER,
    completed_at          INTEGER,
    workspace_kind        TEXT,
    workspace_path        TEXT,
    claim_lock            TEXT,
    claim_expires         INTEGER,
    tenant                TEXT,
    result                TEXT,
    consecutive_failures  INTEGER NOT NULL DEFAULT 0,
    worker_pid            INTEGER,
    last_failure_error    TEXT,
    max_runtime_seconds   INTEGER,
    last_heartbeat_at     INTEGER,
    current_run_id        INTEGER,
    max_retries           INTEGER
);

CREATE TABLE IF NOT EXISTS task_links (
    parent_id TEXT NOT NULL,
    child_id  TEXT NOT NULL,
    PRIMARY KEY (parent_id, child_id),
    FOREIGN KEY (parent_id) REFERENCES tasks(id),
    FOREIGN KEY (child_id)  REFERENCES tasks(id)
);

CREATE TABLE IF NOT EXISTS task_runs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id             TEXT NOT NULL,
    profile             TEXT,
    status              TEXT NOT NULL DEFAULT 'running',
    claim_lock          TEXT,
    claim_expires       INTEGER,
    worker_pid          INTEGER,
    max_runtime_seconds INTEGER,
    last_heartbeat_at   INTEGER,
    started_at          INTEGER NOT NULL,
    ended_at            INTEGER,
    outcome             TEXT,
    summary             TEXT,
    metadata            TEXT,
    error               TEXT,
    FOREIGN KEY (task_id) REFERENCES tasks(id)
);

CREATE TABLE IF NOT EXISTS task_comments (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    TEXT NOT NULL,
    author     TEXT NOT NULL,
    body       TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    FOREIGN KEY (task_id) REFERENCES tasks(id)
);

CREATE TABLE IF NOT EXISTS task_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    TEXT NOT NULL,
    run_id     INTEGER,
    kind       TEXT NOT NULL,
    payload    TEXT,
    created_at INTEGER NOT NULL,
    FOREIGN KEY (task_id) REFERENCES tasks(id)
);

CREATE INDEX IF NOT EXISTS idx_tasks_assignee_status ON tasks(assignee, status);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_tenant ON tasks(tenant);
CREATE INDEX IF NOT EXISTS idx_task_events_task_id ON task_events(task_id, created_at);
CREATE INDEX IF NOT EXISTS idx_task_runs_task_id ON task_runs(task_id, started_at);
CREATE INDEX IF NOT EXISTS idx_task_comments_task_id ON task_comments(task_id, created_at);
"""


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class TaskStore:
    """SQLite-backed task store with atomic operations.

    Thread-safe via ``threading.RLock``.  All public methods acquire the lock
    before touching the database.
    """

    def __init__(self, db_path: str) -> None:
        self._db_path = str(Path(db_path).expanduser().resolve())
        self._lock = threading.RLock()
        self._conn: Optional[sqlite3.Connection] = None
        self._connect()

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def _connect(self) -> None:
        """Open (or reopen) the SQLite connection and ensure the schema."""
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(_SCHEMA_SQL)
        conn.commit()
        self._conn = conn

    def close(self) -> None:
        """Close the database connection explicitly."""
        with self._lock:
            if self._conn:
                self._conn.close()
                self._conn = None

    @contextmanager
    def _tx(self, mode: str = "IMMEDIATE") -> Generator[sqlite3.Cursor, None, None]:
        """Context manager: acquire lock, begin transaction, yield cursor.

        Rolls back on exception, commits on success.
        """
        with self._lock:
            conn = self._conn
            if conn is None:
                raise RuntimeError("TaskStore is closed")
            conn.execute(f"BEGIN {mode}")
            try:
                yield conn.cursor()
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _emit_event(
        self, cur: sqlite3.Cursor, task_id: str, kind: str,
        run_id: Optional[int] = None, payload: Optional[dict] = None,
    ) -> int:
        """Insert an audit event and return its id."""
        now = int(time.time())
        payload_str = json.dumps(payload) if payload else None
        cur.execute(
            "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (task_id, run_id, kind, payload_str, now),
        )
        return cur.lastrowid  # type: ignore[return-value]

    def _row_to_task(self, row: sqlite3.Row) -> Task:
        """Convert a raw DB row to a ``Task`` instance."""
        d = dict(row)
        return Task(**d)

    def _row_to_run(self, row: sqlite3.Row) -> TaskRun:
        d = dict(row)
        return TaskRun(**d)

    def _row_to_comment(self, row: sqlite3.Row) -> TaskComment:
        d = dict(row)
        return TaskComment(**d)

    def _row_to_event(self, row: sqlite3.Row) -> TaskEvent:
        d = dict(row)
        return TaskEvent(**d)

    def _link_exists(self, cur: sqlite3.Cursor, parent_id: str, child_id: str) -> bool:
        cur.execute(
            "SELECT 1 FROM task_links WHERE parent_id=? AND child_id=?",
            (parent_id, child_id),
        )
        return cur.fetchone() is not None

    def _detect_cycle(
        self, cur: sqlite3.Cursor, child_id: str, proposed_parent_id: str,
    ) -> bool:
        """DFS: does *child_id* already reach *proposed_parent_id* via the
        child chain in the current graph?

        If so, adding ``proposed_parent_id → child_id`` would create a cycle.
        """
        if child_id == proposed_parent_id:
            return True  # self-link
        visited: set = set()
        stack = [child_id]
        while stack:
            node = stack.pop()
            if node == proposed_parent_id:
                return True
            if node in visited:
                continue
            visited.add(node)
            # Follow child chain: tasks that depend on *node*
            cur.execute(
                "SELECT child_id FROM task_links WHERE parent_id=?",
                (node,),
            )
            for (cid,) in cur.fetchall():
                stack.append(cid)
        return False

    def _resolve_dependencies(self, cur: sqlite3.Cursor, task_id: str) -> None:
        """If all parents of *task_id* are done, promote it from ``todo`` to ``ready``.
        If a parent is re-activated, demote from ``ready`` back to ``todo``."""
        cur.execute(
            "SELECT p.status FROM task_links l "
            "JOIN tasks p ON l.parent_id = p.id "
            "WHERE l.child_id = ?",
            (task_id,),
        )
        parent_statuses = [r[0] for r in cur.fetchall()]
        if not parent_statuses:
            # No parents at all — if currently todo, promote to ready.
            cur.execute("SELECT status FROM tasks WHERE id=?", (task_id,))
            row = cur.fetchone()
            if row and row[0] == TaskStatus.TODO.value:
                cur.execute(
                    "UPDATE tasks SET status=? WHERE id=?",
                    (TaskStatus.READY.value, task_id),
                )
                self._emit_event(
                    cur, task_id, TaskEventKind.DEPENDENCY_PROMOTED.value,
                    payload={"from": "todo", "to": "ready", "reason": "no_parents"},
                )
            return

        cur.execute("SELECT status FROM tasks WHERE id=?", (task_id,))
        row = cur.fetchone()
        if row is None:
            return
        current_status = row[0]

        all_done = all(
            s in (TaskStatus.COMPLETED.value, TaskStatus.ARCHIVED.value)
            for s in parent_statuses
        )

        if all_done and current_status == TaskStatus.TODO.value:
            cur.execute(
                "UPDATE tasks SET status=? WHERE id=?",
                (TaskStatus.READY.value, task_id),
            )
            self._emit_event(
                cur, task_id, TaskEventKind.DEPENDENCY_PROMOTED.value,
                payload={"from": "todo", "to": "ready"},
            )
        elif not all_done and current_status == TaskStatus.READY.value:
            cur.execute(
                "UPDATE tasks SET status=? WHERE id=?",
                (TaskStatus.TODO.value, task_id),
            )
            self._emit_event(
                cur, task_id, TaskEventKind.DEPENDENCY_PROMOTED.value,
                payload={"from": "ready", "to": "todo"},
            )

    # ------------------------------------------------------------------
    # Task CRUD
    # ------------------------------------------------------------------

    def create_task(
        self,
        title: str,
        body: Optional[str] = None,
        assignee: Optional[str] = None,
        priority: int = 0,
        parents: Optional[List[str]] = None,
        workspace_kind: Optional[str] = None,
        workspace_path: Optional[str] = None,
        max_runtime_seconds: Optional[int] = None,
        max_retries: Optional[int] = None,
        tenant: Optional[str] = None,
        created_by: Optional[str] = None,
    ) -> Task:
        """Create a new task and return it.

        If ``parents`` is provided, validates each exists and checks for cycles.
        The task starts in ``todo`` (or ``ready`` if no parents).
        """
        task = Task(
            title=title,
            body=body,
            assignee=assignee,
            priority=priority,
            workspace_kind=workspace_kind,
            workspace_path=workspace_path,
            max_runtime_seconds=max_runtime_seconds,
            max_retries=max_retries,
            tenant=tenant,
            created_by=created_by,
        )
        task.ensure_id()
        now = int(time.time())
        task.created_at = now

        # Determine initial status
        parents = parents or []
        task.status = TaskStatus.TODO.value if parents else TaskStatus.READY.value

        with self._tx() as cur:
            # Validate parent tasks exist
            for pid in parents:
                cur.execute("SELECT id FROM tasks WHERE id=?", (pid,))
                if cur.fetchone() is None:
                    raise ValueError(f"Parent task '{pid}' not found")

            # Cycle detection: none of the parents can be reachable from this task
            for pid in parents:
                if self._detect_cycle(cur, task.id, pid):
                    raise ValueError(
                        f"Cannot add parent '{pid}' — would create a cycle"
                    )

            # Insert task
            cur.execute(
                """INSERT INTO tasks
                   (id, title, body, assignee, status, priority, created_by,
                    created_at, workspace_kind, workspace_path, tenant,
                    max_runtime_seconds, max_retries)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    task.id, task.title, task.body, task.assignee,
                    task.status, task.priority, task.created_by,
                    task.created_at, task.workspace_kind, task.workspace_path,
                    task.tenant, task.max_runtime_seconds, task.max_retries,
                ),
            )

            # Insert parent links
            for pid in parents:
                cur.execute(
                    "INSERT INTO task_links (parent_id, child_id) VALUES (?, ?)",
                    (pid, task.id),
                )

            self._emit_event(
                cur, task.id, TaskEventKind.CREATED.value,
                payload={
                    "assignee": assignee,
                    "parents": parents,
                    "status": task.status,
                },
            )

        return task

    def update_task(
        self,
        task_id: str,
        title: Optional[str] = None,
        body: Optional[str] = None,
        assignee: Optional[str] = None,
        priority: Optional[int] = None,
    ) -> Task:
        """Update editable fields of an existing task."""
        with self._tx() as cur:
            cur.execute("SELECT * FROM tasks WHERE id=?", (task_id,))
            row = cur.fetchone()
            if row is None:
                raise ValueError(f"Task '{task_id}' not found")

            task = self._row_to_task(row)
            changed = False

            if title is not None and title.strip() != task.title:
                task.title = title.strip()
                changed = True
            if body is not None:
                task.body = body.strip() if body.strip() else None
                changed = True
            if assignee is not None:
                task.assignee = assignee.strip() if assignee.strip() else None
                changed = True
            if priority is not None:
                task.priority = priority
                changed = True

            if not changed:
                return task

            cur.execute(
                """UPDATE tasks
                   SET title=?, body=?, assignee=?, priority=?
                   WHERE id=?""",
                (task.title, task.body, task.assignee, task.priority, task_id),
            )

            self._emit_event(
                cur, task_id, TaskEventKind.UPDATED.value,
                payload={
                    "title": task.title,
                    "body": task.body,
                    "assignee": task.assignee,
                    "priority": task.priority,
                },
            )

        return task

    def get_task(self, task_id: str) -> Optional[Task]:
        """Fetch a single task, including transient parent/child data."""
        with self._tx("DEFERRED") as cur:
            cur.execute("SELECT * FROM tasks WHERE id=?", (task_id,))
            row = cur.fetchone()
            if row is None:
                return None
            task = self._row_to_task(row)

            # Load parents
            cur.execute(
                "SELECT t.id, t.title, t.status FROM task_links l "
                "JOIN tasks t ON l.parent_id = t.id "
                "WHERE l.child_id=?",
                (task_id,),
            )
            task.parents = [dict(r) for r in cur.fetchall()]

            # Load children
            cur.execute(
                "SELECT t.id, t.title, t.status FROM task_links l "
                "JOIN tasks t ON l.child_id = t.id "
                "WHERE l.parent_id=?",
                (task_id,),
            )
            task.children = [dict(r) for r in cur.fetchall()]

            return task

    def list_tasks(
        self,
        status: Optional[str] = None,
        assignee: Optional[str] = None,
        tenant: Optional[str] = None,
        parent_id: Optional[str] = None,
        q: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
        sort: str = "-created_at",
    ) -> Tuple[List[Task], int]:
        """List tasks with filtering, pagination, and sorting.

        Returns:
            Tuple of (tasks list, total count).
        """
        where_clauses: List[str] = []
        params: List[Any] = []

        if status:
            statuses = [s.strip() for s in status.split(",")]
            placeholders = ",".join("?" for _ in statuses)
            where_clauses.append(f"t.status IN ({placeholders})")
            params.extend(statuses)

        if assignee:
            where_clauses.append("t.assignee = ?")
            params.append(assignee)

        if tenant:
            where_clauses.append("t.tenant = ?")
            params.append(tenant)

        if parent_id:
            where_clauses.append(
                "t.id IN (SELECT child_id FROM task_links WHERE parent_id=?)"
            )
            params.append(parent_id)

        if q:
            where_clauses.append("(t.title LIKE ? OR t.body LIKE ?)")
            like_q = f"%{q}%"
            params.append(like_q)
            params.append(like_q)

        where = ""
        if where_clauses:
            where = "WHERE " + " AND ".join(where_clauses)

        # Sort: allow column names with optional leading '-' for DESC
        sort_col = "created_at"
        sort_dir = "DESC"
        if sort.startswith("-"):
            sort_col = sort[1:]
        elif sort:
            sort_col = sort

        allowed_sorts = {"created_at", "priority", "started_at", "title"}
        if sort_col not in allowed_sorts:
            sort_col = "created_at"

        order = f"ORDER BY t.{sort_col} {sort_dir}, t.id"

        with self._tx("DEFERRED") as cur:
            # Count
            cur.execute(
                f"SELECT COUNT(*) AS total FROM tasks t {where}",
                params,
            )
            total = cur.fetchone()[0]

            # Data
            cur.execute(
                f"SELECT t.* FROM tasks t {where} {order} LIMIT ? OFFSET ?",
                [*params, limit, offset],
            )
            tasks = [self._row_to_task(r) for r in cur.fetchall()]

        return tasks, total

    def update_task_status(
        self, task_id: str, new_status: str,
        claim_lock: Optional[str] = None,
        result: Optional[str] = None,
        summary: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> Task:
        """Transition a task to *new_status*, validating via the state machine.

        When a task is completed or failed, also updates the current run.
        When a task is completed, runs dependency resolution on children.
        """
        now = int(time.time())

        with self._tx() as cur:
            cur.execute("SELECT * FROM tasks WHERE id=?", (task_id,))
            row = cur.fetchone()
            if row is None:
                raise ValueError(f"Task '{task_id}' not found")

            task = self._row_to_task(row)
            old_status = task.status

            # State machine validation
            validate_transition(old_status, new_status)

            # Claim-lock check for running → anything
            if old_status == TaskStatus.RUNNING.value and claim_lock:
                if task.claim_lock != claim_lock:
                    raise PermissionError(
                        f"Claim lock mismatch: expected '{task.claim_lock}', "
                        f"got '{claim_lock}'"
                    )

            # Build update fields
            updates: List[str] = ["status = ?"]
            update_params: List[Any] = [new_status]

            if new_status in (
                TaskStatus.COMPLETED.value, TaskStatus.FAILED.value,
            ):
                updates.append("completed_at = ?")
                update_params.append(now)

                # End the current run
                if task.current_run_id is not None:
                    outcome = (
                        TaskRunStatus.DONE.value
                        if new_status == TaskStatus.COMPLETED.value
                        else TaskRunStatus.FAILED.value
                    )
                    cur.execute(
                        "UPDATE task_runs SET status=?, ended_at=?, "
                        "outcome=?, summary=?, metadata=?, error=? "
                        "WHERE id=?",
                        (
                            outcome,
                            now,
                            new_status,
                            summary,
                            json.dumps(metadata) if metadata else None,
                            result,
                            task.current_run_id,
                        ),
                    )

                if new_status == TaskStatus.FAILED.value:
                    updates.append("consecutive_failures = consecutive_failures + 1")
                    updates.append("last_failure_error = ?")
                    update_params.append(result)

            if new_status == TaskStatus.COMPLETED.value:
                updates.append("consecutive_failures = 0")
                if result:
                    updates.append("result = ?")
                    update_params.append(result)

            if new_status in (
                TaskStatus.READY.value, TaskStatus.RUNNING.value,
            ) and old_status == TaskStatus.FAILED.value:
                # Retry — create a new run record
                updates.append("consecutive_failures = ?")
                update_params.append(task.consecutive_failures)

            updates_str = ", ".join(updates)
            cur.execute(
                f"UPDATE tasks SET {updates_str} WHERE id=?",
                [*update_params, task_id],
            )

            # Emit event
            # Handle special transitions that can't be inferred from new_status alone
            if old_status == TaskStatus.BLOCKED.value and new_status == TaskStatus.RUNNING.value:
                kind = TaskEventKind.UNBLOCKED.value
            elif old_status == TaskStatus.FAILED.value and new_status == TaskStatus.READY.value:
                kind = TaskEventKind.RETRIED.value
            else:
                event_kind_map = {
                    TaskStatus.COMPLETED.value: TaskEventKind.COMPLETED.value,
                    TaskStatus.FAILED.value: TaskEventKind.FAILED.value,
                    TaskStatus.BLOCKED.value: TaskEventKind.BLOCKED.value,
                    TaskStatus.RUNNING.value: TaskEventKind.STARTED.value,
                    TaskStatus.ARCHIVED.value: TaskEventKind.ARCHIVED.value,
                }
                kind = event_kind_map.get(new_status, "status_changed")
            self._emit_event(
                cur, task_id, kind,
                run_id=task.current_run_id,
                payload={
                    "from": old_status,
                    "to": new_status,
                    "result": result,
                },
            )

            # If completed, resolve child dependencies
            if new_status == TaskStatus.COMPLETED.value:
                cur.execute(
                    "SELECT child_id FROM task_links WHERE parent_id=?",
                    (task_id,),
                )
                for (child_id,) in cur.fetchall():
                    self._resolve_dependencies(cur, child_id)

        return self.get_task(task_id)  # type: ignore[return-value]

    def delete_task(self, task_id: str) -> bool:
        """Archive a completed or failed task — moves it to ``archived``.

        Only completed/failed/cancelled tasks may be archived.
        """
        return self.update_task_status(task_id, TaskStatus.ARCHIVED.value) is not None

    # ------------------------------------------------------------------
    # Atomic Claim
    # ------------------------------------------------------------------

    def claim_task(
        self,
        task_id: str,
        worker_id: str,
        pid: int,
        ttl: int = DEFAULT_CLAIM_TTL,
    ) -> Optional[dict]:
        """Atomically claim a ``ready`` task for a worker.

        The claim only succeeds when the task is ``ready`` **and** either has
        no existing claim lock, or the previous lock has expired.

        Returns:
            Dict with ``task_id``, ``claim_lock``, ``claim_expires``,
            ``workspace_path`` on success, or ``None`` if the claim failed.
        """
        now = int(time.time())
        expires = now + ttl
        lock = f"{worker_id}:{pid}"

        with self._tx() as cur:
            cur.execute(
                """UPDATE tasks
                   SET status = ?,
                       claim_lock = ?,
                       claim_expires = ?,
                       started_at = COALESCE(started_at, ?),
                       worker_pid = ?,
                       last_heartbeat_at = ?
                   WHERE id = ?
                     AND status = ?
                     AND (claim_lock IS NULL OR claim_expires < ?)""",
                (
                    TaskStatus.RUNNING.value,
                    lock,
                    expires,
                    now,
                    pid,
                    now,
                    task_id,
                    TaskStatus.READY.value,
                    now,
                ),
            )

            if cur.rowcount == 0:
                return None

            # Create a run record
            cur.execute(
                """INSERT INTO task_runs
                   (task_id, profile, status, claim_lock, claim_expires,
                    worker_pid, max_runtime_seconds, last_heartbeat_at, started_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    task_id, worker_id, TaskRunStatus.RUNNING.value,
                    lock, expires, pid, None, now, now,
                ),
            )
            run_id = cur.lastrowid
            assert run_id is not None, "Insert created a run but got no id"

            # Link run to task
            cur.execute(
                "UPDATE tasks SET current_run_id=? WHERE id=?",
                (run_id, task_id),
            )

            self._emit_event(
                cur, task_id, TaskEventKind.CLAIMED.value,
                run_id=run_id,
                payload={"worker_id": worker_id, "pid": pid, "lock": lock},
            )

        # Fetch workspace info
        task = self.get_task(task_id)
        return {
            "task_id": task_id,
            "claim_lock": lock,
            "claim_expires": expires,
            "workspace_path": task.workspace_path if task else None,
        }

    def heartbeat(self, task_id: str, claim_lock: str) -> bool:
        """Extend the claim TTL for a running task.

        Args:
            task_id: Task to heartbeat.
            claim_lock: Must match the current ``claim_lock`` on the task.

        Returns:
            ``True`` if the heartbeat was accepted, ``False`` otherwise.
        """
        now = int(time.time())
        with self._tx() as cur:
            cur.execute(
                "SELECT claim_lock, claim_expires, current_run_id "
                "FROM tasks WHERE id=?",
                (task_id,),
            )
            row = cur.fetchone()
            if row is None:
                return False

            if row[0] != claim_lock:
                return False

            # Extend TTL
            new_expires = now + DEFAULT_CLAIM_TTL
            cur.execute(
                "UPDATE tasks SET last_heartbeat_at=?, claim_expires=? "
                "WHERE id=?",
                (now, new_expires, task_id),
            )

            # Update run too
            if row[2] is not None:
                cur.execute(
                    "UPDATE task_runs SET last_heartbeat_at=?, claim_expires=? "
                    "WHERE id=?",
                    (now, new_expires, row[2]),
                )

            self._emit_event(
                cur, task_id, TaskEventKind.HEARTBEAT.value,
                run_id=row[2],
            )

        return True

    # ------------------------------------------------------------------
    # TTL Release
    # ------------------------------------------------------------------

    def release_expired_claims(self) -> int:
        """Release all tasks whose claim lock has expired.

        Expired running tasks are marked ``failed`` and their runs are ended.
        Expired blocked tasks are also marked ``failed``.

        Returns:
            Number of tasks released.
        """
        now = int(time.time())
        released = 0

        with self._tx() as cur:
            # Running → failed
            cur.execute(
                """SELECT id, current_run_id, consecutive_failures,
                          COALESCE(max_retries, ?) AS max_ret
                   FROM tasks
                   WHERE status IN (?, ?)
                     AND claim_expires IS NOT NULL
                     AND claim_expires <= ?""",
                (
                    DEFAULT_MAX_RETRIES,
                    TaskStatus.RUNNING.value,
                    TaskStatus.BLOCKED.value,
                    now,
                ),
            )
            expired = cur.fetchall()

            for row in expired:
                task_id = row[0]
                run_id = row[1]
                failures = row[2] + 1
                max_ret = row[3]

                # End the run
                if run_id is not None:
                    cur.execute(
                        "UPDATE task_runs SET status=?, ended_at=?, "
                        "outcome=? WHERE id=?",
                        (
                            TaskRunStatus.TIMED_OUT.value,
                            now,
                            "timed_out",
                            run_id,
                        ),
                    )

                # Update task
                cur.execute(
                    """UPDATE tasks
                       SET status=?, completed_at=?, consecutive_failures=?,
                           last_failure_error=?,
                           claim_lock=NULL, claim_expires=NULL,
                           worker_pid=NULL, current_run_id=NULL
                       WHERE id=?""",
                    (
                        TaskStatus.FAILED.value,
                        now,
                        failures,
                        "Claim TTL expired",
                        task_id,
                    ),
                )

                # Emit released audit event for the claim lock release
                self._emit_event(
                    cur, task_id, "released",
                    run_id=run_id,
                    payload={"reason": "claim_ttl_expired", "failures": failures},
                )

                self._emit_event(
                    cur, task_id, TaskEventKind.FAILED.value,
                    run_id=run_id,
                    payload={
                        "reason": "claim_ttl_expired",
                        "failures": failures,
                    },
                )
                released += 1

        return released

    # ------------------------------------------------------------------
    # Retry promotion
    # ------------------------------------------------------------------

    def promote_retryable_tasks(self) -> int:
        """Promote failed tasks that are below their retry limit back to ``ready``.

        Returns:
            Number of tasks promoted.
        """
        now = int(time.time())
        promoted = 0

        with self._tx() as cur:
            cur.execute(
                """SELECT id, consecutive_failures
                   FROM tasks
                   WHERE status = ?
                     AND consecutive_failures <= COALESCE(max_retries, ?)
                     AND consecutive_failures > 0""",
                (TaskStatus.FAILED.value, DEFAULT_MAX_RETRIES),
            )

            for row in cur.fetchall():
                task_id = row[0]

                # Create a new run record
                cur.execute(
                    """INSERT INTO task_runs
                       (task_id, status, started_at)
                       VALUES (?, ?, ?)""",
                    (task_id, TaskRunStatus.RUNNING.value, now),
                )
                run_id = cur.lastrowid

                cur.execute(
                    """UPDATE tasks
                       SET status=?, claim_lock=NULL, claim_expires=NULL,
                           current_run_id=?, worker_pid=NULL
                       WHERE id=?""",
                    (TaskStatus.READY.value, run_id, task_id),
                )

                self._emit_event(
                    cur, task_id, TaskEventKind.DEPENDENCY_PROMOTED.value,
                    payload={"from": "failed", "to": "ready", "reason": "retry"},
                )
                promoted += 1

        return promoted

    # ------------------------------------------------------------------
    # Comments
    # ------------------------------------------------------------------

    def add_comment(
        self, task_id: str, author: str, body: str,
    ) -> TaskComment:
        """Add a comment to a task.

        Returns:
            The created comment with its ``id`` populated.
        """
        now = int(time.time())
        with self._tx() as cur:
            # Verify task exists
            cur.execute("SELECT id FROM tasks WHERE id=?", (task_id,))
            if cur.fetchone() is None:
                raise ValueError(f"Task '{task_id}' not found")

            cur.execute(
                "INSERT INTO task_comments (task_id, author, body, created_at) "
                "VALUES (?, ?, ?, ?)",
                (task_id, author, body, now),
            )

            comment = TaskComment(
                id=cur.lastrowid,
                task_id=task_id,
                author=author,
                body=body,
                created_at=now,
            )

            self._emit_event(
                cur, task_id, TaskEventKind.COMMENTED.value,
                payload={"author": author},
            )

        return comment

    def get_comments(self, task_id: str) -> List[TaskComment]:
        """Return all comments for a task, ordered by creation time."""
        with self._tx("DEFERRED") as cur:
            cur.execute(
                "SELECT * FROM task_comments WHERE task_id=? ORDER BY created_at",
                (task_id,),
            )
            return [self._row_to_comment(r) for r in cur.fetchall()]

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------

    def get_events(self, task_id: str, limit: int = 50) -> List[TaskEvent]:
        """Return audit events for a task, latest first."""
        with self._tx("DEFERRED") as cur:
            cur.execute(
                "SELECT * FROM task_events WHERE task_id=? "
                "ORDER BY id DESC LIMIT ?",
                (task_id, limit),
            )
            return [self._row_to_event(r) for r in cur.fetchall()]

    # ------------------------------------------------------------------
    # Runs
    # ------------------------------------------------------------------

    def get_runs(self, task_id: str) -> List[TaskRun]:
        """Return all runs for a task, latest first."""
        with self._tx("DEFERRED") as cur:
            cur.execute(
                "SELECT * FROM task_runs WHERE task_id=? ORDER BY id DESC",
                (task_id,),
            )
            return [self._row_to_run(r) for r in cur.fetchall()]

    # ------------------------------------------------------------------
    # Dependencies
    # ------------------------------------------------------------------

    def add_dependency(self, task_id: str, parent_id: str) -> None:
        """Add a parent → child dependency edge.

        Raises ``ValueError`` if the parent doesn't exist, self-link, or cycle.
        """
        with self._tx() as cur:
            # Both tasks must exist
            cur.execute("SELECT id FROM tasks WHERE id=?", (task_id,))
            if cur.fetchone() is None:
                raise ValueError(f"Task '{task_id}' not found")
            cur.execute("SELECT id FROM tasks WHERE id=?", (parent_id,))
            if cur.fetchone() is None:
                raise ValueError(f"Parent task '{parent_id}' not found")

            if task_id == parent_id:
                raise ValueError("Self-links are not allowed")

            if self._detect_cycle(cur, task_id, parent_id):
                raise ValueError("Would create a cycle")

            if self._link_exists(cur, parent_id, task_id):
                return  # idempotent

            cur.execute(
                "INSERT INTO task_links (parent_id, child_id) VALUES (?, ?)",
                (parent_id, task_id),
            )

            # Re-resolve dependencies
            self._resolve_dependencies(cur, task_id)

    def remove_dependency(self, task_id: str, parent_id: str) -> bool:
        """Remove a parent → child dependency edge.

        Returns:
            ``True`` if removed, ``False`` if the link didn't exist.
        """
        with self._tx() as cur:
            cur.execute(
                "DELETE FROM task_links WHERE parent_id=? AND child_id=?",
                (parent_id, task_id),
            )
            removed = cur.rowcount > 0
            if removed:
                self._resolve_dependencies(cur, task_id)
            return removed

    def get_parents(self, task_id: str) -> List[dict]:
        """Return parent task summaries (id, title, status)."""
        with self._tx("DEFERRED") as cur:
            cur.execute(
                "SELECT t.id, t.title, t.status FROM task_links l "
                "JOIN tasks t ON l.parent_id = t.id "
                "WHERE l.child_id=?",
                (task_id,),
            )
            return [dict(r) for r in cur.fetchall()]

    def get_children(self, task_id: str) -> List[dict]:
        """Return child task summaries (id, title, status)."""
        with self._tx("DEFERRED") as cur:
            cur.execute(
                "SELECT t.id, t.title, t.status FROM task_links l "
                "JOIN tasks t ON l.child_id = t.id "
                "WHERE l.parent_id=?",
                (task_id,),
            )
            return [dict(r) for r in cur.fetchall()]

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    def stats(self) -> Dict[str, Any]:
        """Return summary statistics across all tasks."""
        with self._tx("DEFERRED") as cur:
            cur.execute(
                "SELECT status, COUNT(*) AS cnt FROM tasks GROUP BY status"
            )
            by_status: Dict[str, int] = {}
            for r in cur.fetchall():
                by_status[r[0]] = r[1]

            cur.execute("SELECT COUNT(*) FROM tasks")
            total = cur.fetchone()[0]

            return {
                "total": total,
                "by_status": by_status,
            }

    # ------------------------------------------------------------------
    # Dispatcher helpers
    # ------------------------------------------------------------------

    def _update_workspace_path(self, task_id: str, workspace_path: str) -> None:
        """Update the ``workspace_path`` for a task (called after allocation)."""
        with self._tx() as cur:
            cur.execute(
                "UPDATE tasks SET workspace_path=? WHERE id=?",
                (workspace_path, task_id),
            )

    def _set_worker_pid(self, task_id: str, pid: int) -> None:
        """Set the ``worker_pid`` for a task (called after spawning)."""
        with self._tx() as cur:
            cur.execute(
                "UPDATE tasks SET worker_pid=? WHERE id=?",
                (pid, task_id),
            )
