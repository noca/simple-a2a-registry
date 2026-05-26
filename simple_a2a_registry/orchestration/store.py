"""Database-backed task store — the persistence layer for the orchestration engine.

All operations use ``BEGIN IMMEDIATE`` transactions to safely handle
concurrent writes.

Backed by a :class:`DatabaseEngine` so it transparently supports
SQLite (dev) and MySQL (production).  Legacy callers can still pass a
``db_path`` string — a :class:`SQLiteEngine` is created automatically.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional, Tuple

from simple_a2a_registry.database import DatabaseEngine, CursorResult, SQLiteEngine
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
# SQL schema — MySQL version
# ---------------------------------------------------------------------------

_SCHEMA_SQL_MYSQL = """
CREATE TABLE IF NOT EXISTS tasks (
    id                    VARCHAR(255) PRIMARY KEY,
    title                 VARCHAR(255) NOT NULL,
    body                  TEXT,
    assignee              VARCHAR(255),
    status                VARCHAR(50) NOT NULL DEFAULT 'todo',
    priority              INT NOT NULL DEFAULT 0,
    created_by            VARCHAR(255),
    created_at            BIGINT NOT NULL,
    started_at            BIGINT,
    completed_at          BIGINT,
    workspace_kind        VARCHAR(50),
    workspace_path        VARCHAR(1024),
    claim_lock            VARCHAR(255),
    claim_expires         BIGINT,
    tenant                VARCHAR(255),
    result                TEXT,
    consecutive_failures  INT NOT NULL DEFAULT 0,
    worker_pid            INT,
    last_failure_error    TEXT,
    max_runtime_seconds   INT,
    last_heartbeat_at     BIGINT,
    current_run_id        INT,
    max_retries           INT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS task_links (
    parent_id VARCHAR(255) NOT NULL,
    child_id  VARCHAR(255) NOT NULL,
    PRIMARY KEY (parent_id, child_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS task_runs (
    id                  INT AUTO_INCREMENT PRIMARY KEY,
    task_id             VARCHAR(255) NOT NULL,
    profile             VARCHAR(255),
    status              VARCHAR(50) NOT NULL DEFAULT 'running',
    claim_lock          VARCHAR(255),
    claim_expires       BIGINT,
    worker_pid          INT,
    max_runtime_seconds INT,
    last_heartbeat_at   BIGINT,
    started_at          BIGINT NOT NULL,
    ended_at            BIGINT,
    outcome             VARCHAR(50),
    summary             TEXT,
    metadata            TEXT,
    error               TEXT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS task_comments (
    id         INT AUTO_INCREMENT PRIMARY KEY,
    task_id    VARCHAR(255) NOT NULL,
    author     VARCHAR(255) NOT NULL,
    body       TEXT NOT NULL,
    created_at BIGINT NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS task_events (
    id         INT AUTO_INCREMENT PRIMARY KEY,
    task_id    VARCHAR(255) NOT NULL,
    run_id     INT,
    kind       VARCHAR(100) NOT NULL,
    payload    TEXT,
    created_at BIGINT NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE INDEX idx_tasks_assignee_status ON tasks(assignee, status);
CREATE INDEX idx_tasks_status ON tasks(status);
CREATE INDEX idx_tasks_tenant ON tasks(tenant);
CREATE INDEX idx_task_events_task_id ON task_events(task_id, created_at);
CREATE INDEX idx_task_runs_task_id ON task_runs(task_id, started_at);
CREATE INDEX idx_task_comments_task_id ON task_comments(task_id, created_at);
"""


def _maybe_create_schema(engine: DatabaseEngine) -> None:
    """Create schema on first connect (SQLite or MySQL)."""
    if engine.driver == "sqlite":
        engine.executescript(_SCHEMA_SQL)
        engine.commit()
    elif engine.driver == "mysql":
        for statement in _SCHEMA_SQL_MYSQL.split(";"):
            stripped = statement.strip()
            if not stripped:
                continue
            try:
                engine.execute(stripped)
            except Exception:
                pass  # ignore "already exists" errors
        engine.commit()


# ---------------------------------------------------------------------------
# Helper — convert sqlite3.Row-compatible result rows to Task / etc.
# ---------------------------------------------------------------------------


def _result_to_task(result: CursorResult) -> Optional[Task]:
    row = result.fetchone()
    if row is None:
        return None
    return Task(**row)


def _result_to_task_list(result: CursorResult) -> List[Task]:
    return [Task(**r) for r in result.fetchall()]


def _result_to_run(result: CursorResult) -> Optional[TaskRun]:
    row = result.fetchone()
    if row is None:
        return None
    return TaskRun(**row)


def _result_to_run_list(result: CursorResult) -> List[TaskRun]:
    return [TaskRun(**r) for r in result.fetchall()]


def _result_to_comment_list(result: CursorResult) -> List[TaskComment]:
    return [TaskComment(**r) for r in result.fetchall()]


def _result_to_event_list(result: CursorResult) -> List[TaskEvent]:
    return [TaskEvent(**r) for r in result.fetchall()]


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class TaskStore:
    """Database-backed task store with atomic operations.

    Thread-safe via ``threading.RLock``.  All public methods acquire the lock
    before touching the database.
    """

    def __init__(
        self,
        db_path_or_engine: str | DatabaseEngine,
    ) -> None:
        """Initialise the task store.

        Two calling conventions:
        1. Legacy: ``TaskStore(\"~/.simple-a2a-registry/board.db\")``
           — creates a :class:`SQLiteEngine` internally.
        2. New:    ``TaskStore(my_engine)``
           — uses the pre-configured engine (SQLite or MySQL).
        """
        self._lock = threading.RLock()

        if isinstance(db_path_or_engine, str):
            engine = SQLiteEngine(str(Path(db_path_or_engine).expanduser().resolve()))
            engine.connect()
            _maybe_create_schema(engine)
            self._engine = engine
        else:
            self._engine = db_path_or_engine

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Close the database connection explicitly."""
        with self._lock:
            self._engine.close()

    @contextmanager
    def _tx(self, mode: str = "IMMEDIATE") -> Generator[DatabaseEngine, None, None]:
        """Context manager: acquire lock, begin transaction, yield engine.

        Rolls back on exception, commits on success.
        """
        with self._lock:
            self._engine.begin(mode)
            try:
                yield self._engine
                self._engine.commit()
            except Exception:
                self._engine.rollback()
                raise

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _emit_event(
        self,
        engine: DatabaseEngine,
        task_id: str,
        kind: str,
        run_id: Optional[int] = None,
        payload: Optional[dict] = None,
    ) -> int:
        """Insert an audit event and return its id."""
        now = int(time.time())
        payload_str = json.dumps(payload) if payload else None
        result = engine.execute(
            "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (task_id, run_id, kind, payload_str, now),
        )
        return result.lastrowid

    def _link_exists(self, engine: DatabaseEngine, parent_id: str, child_id: str) -> bool:
        result = engine.execute(
            "SELECT 1 FROM task_links WHERE parent_id=? AND child_id=?",
            (parent_id, child_id),
        )
        return result.fetchone() is not None

    def _detect_cycle(
        self, engine: DatabaseEngine, child_id: str, proposed_parent_id: str,
    ) -> bool:
        """DFS: does *child_id* already reach *proposed_parent_id* via the
        child chain in the current graph?

        If so, adding ``proposed_parent_id → child_id`` would create a cycle.
        """
        if child_id == proposed_parent_id:
            return True
        visited: set = set()
        stack = [child_id]
        while stack:
            node = stack.pop()
            if node == proposed_parent_id:
                return True
            if node in visited:
                continue
            visited.add(node)
            result = engine.execute(
                "SELECT child_id FROM task_links WHERE parent_id=?",
                (node,),
            )
            for r in result.fetchall():
                stack.append(r["child_id"])
        return False

    def _resolve_dependencies(self, engine: DatabaseEngine, task_id: str) -> None:
        """If all parents of *task_id* are done, promote it from ``todo`` to ``ready``.
        If a parent is re-activated, demote from ``ready`` back to ``todo``."""
        result = engine.execute(
            "SELECT p.status FROM task_links l "
            "JOIN tasks p ON l.parent_id = p.id "
            "WHERE l.child_id = ?",
            (task_id,),
        )
        parent_statuses = [r["status"] for r in result.fetchall()]
        if not parent_statuses:
            # No parents at all — if currently todo, promote to ready.
            t = _result_to_task(
                engine.execute("SELECT status FROM tasks WHERE id=?", (task_id,))
            )
            if t and t.status == TaskStatus.TODO.value:
                engine.execute(
                    "UPDATE tasks SET status=? WHERE id=?",
                    (TaskStatus.READY.value, task_id),
                )
                self._emit_event(
                    engine, task_id, TaskEventKind.DEPENDENCY_PROMOTED.value,
                    payload={"from": "todo", "to": "ready", "reason": "no_parents"},
                )
            return

        t = _result_to_task(
            engine.execute("SELECT status FROM tasks WHERE id=?", (task_id,))
        )
        if t is None:
            return
        current_status = t.status

        all_done = all(
            s in (TaskStatus.COMPLETED.value, TaskStatus.ARCHIVED.value)
            for s in parent_statuses
        )

        if all_done and current_status == TaskStatus.TODO.value:
            engine.execute(
                "UPDATE tasks SET status=? WHERE id=?",
                (TaskStatus.READY.value, task_id),
            )
            self._emit_event(
                engine, task_id, TaskEventKind.DEPENDENCY_PROMOTED.value,
                payload={"from": "todo", "to": "ready"},
            )
        elif not all_done and current_status == TaskStatus.READY.value:
            engine.execute(
                "UPDATE tasks SET status=? WHERE id=?",
                (TaskStatus.TODO.value, task_id),
            )
            self._emit_event(
                engine, task_id, TaskEventKind.DEPENDENCY_PROMOTED.value,
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

        parents_list = parents or []
        task.status = TaskStatus.TODO.value if parents_list else TaskStatus.READY.value

        with self._tx() as engine:
            for pid in parents_list:
                result = engine.execute("SELECT id FROM tasks WHERE id=?", (pid,))
                if result.fetchone() is None:
                    raise ValueError(f"Parent task '{pid}' not found")

            for pid in parents_list:
                if self._detect_cycle(engine, task.id, pid):
                    raise ValueError(
                        f"Cannot add parent '{pid}' — would create a cycle"
                    )

            engine.execute(
                "INSERT INTO tasks "
                "(id, title, body, assignee, status, priority, created_by, "
                " created_at, workspace_kind, workspace_path, tenant, "
                " max_runtime_seconds, max_retries) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    task.id, task.title, task.body, task.assignee,
                    task.status, task.priority, task.created_by,
                    task.created_at, task.workspace_kind, task.workspace_path,
                    task.tenant, task.max_runtime_seconds, task.max_retries,
                ),
            )

            for pid in parents_list:
                engine.execute(
                    "INSERT INTO task_links (parent_id, child_id) VALUES (?, ?)",
                    (pid, task.id),
                )

            self._emit_event(
                engine, task.id, TaskEventKind.CREATED.value,
                payload={
                    "assignee": assignee,
                    "parents": parents_list,
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
        with self._tx() as engine:
            task = _result_to_task(
                engine.execute("SELECT * FROM tasks WHERE id=?", (task_id,))
            )
            if task is None:
                raise ValueError(f"Task '{task_id}' not found")

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

            engine.execute(
                "UPDATE tasks SET title=?, body=?, assignee=?, priority=? WHERE id=?",
                (task.title, task.body, task.assignee, task.priority, task_id),
            )

            self._emit_event(
                engine, task_id, TaskEventKind.UPDATED.value,
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
        with self._tx("DEFERRED") as engine:
            task = _result_to_task(
                engine.execute("SELECT * FROM tasks WHERE id=?", (task_id,))
            )
            if task is None:
                return None

            # Load parents
            result = engine.execute(
                "SELECT t.id, t.title, t.status FROM task_links l "
                "JOIN tasks t ON l.parent_id = t.id "
                "WHERE l.child_id=?",
                (task_id,),
            )
            task.parents = result.fetchall()

            # Load children
            result = engine.execute(
                "SELECT t.id, t.title, t.status FROM task_links l "
                "JOIN tasks t ON l.child_id = t.id "
                "WHERE l.parent_id=?",
                (task_id,),
            )
            task.children = result.fetchall()

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

        with self._tx("DEFERRED") as engine:
            result = engine.execute(
                f"SELECT COUNT(*) AS total FROM tasks t {where}",
                tuple(params),
            )
            total = result.fetchone()["total"]

            result = engine.execute(
                f"SELECT t.* FROM tasks t {where} {order} LIMIT ? OFFSET ?",
                (*params, limit, offset),
            )
            tasks = [Task(**r) for r in result.fetchall()]

        return tasks, total

    def update_task_status(
        self,
        task_id: str,
        new_status: str,
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

        with self._tx() as engine:
            task = _result_to_task(
                engine.execute("SELECT * FROM tasks WHERE id=?", (task_id,))
            )
            if task is None:
                raise ValueError(f"Task '{task_id}' not found")

            old_status = task.status

            validate_transition(old_status, new_status)

            if old_status == TaskStatus.RUNNING.value and claim_lock:
                if task.claim_lock != claim_lock:
                    raise PermissionError(
                        f"Claim lock mismatch: expected '{task.claim_lock}', "
                        f"got '{claim_lock}'"
                    )

            updates: List[str] = ["status = ?"]
            update_params: List[Any] = [new_status]

            if new_status in (
                TaskStatus.COMPLETED.value, TaskStatus.FAILED.value,
            ):
                updates.append("completed_at = ?")
                update_params.append(now)

                if task.current_run_id is not None:
                    outcome = (
                        TaskRunStatus.DONE.value
                        if new_status == TaskStatus.COMPLETED.value
                        else TaskRunStatus.FAILED.value
                    )
                    engine.execute(
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
                updates.append("consecutive_failures = ?")
                update_params.append(task.consecutive_failures)

            updates_str = ", ".join(updates)
            engine.execute(
                f"UPDATE tasks SET {updates_str} WHERE id=?",
                (*update_params, task_id),
            )

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
                engine, task_id, kind,
                run_id=task.current_run_id,
                payload={
                    "from": old_status,
                    "to": new_status,
                    "result": result,
                },
            )

            if new_status == TaskStatus.COMPLETED.value:
                result_links = engine.execute(
                    "SELECT child_id FROM task_links WHERE parent_id=?",
                    (task_id,),
                )
                for r in result_links.fetchall():
                    self._resolve_dependencies(engine, r["child_id"])

        return self.get_task(task_id)  # type: ignore[return-value]

    def delete_task(self, task_id: str) -> bool:
        """Archive a completed or failed task."""
        return self.update_task_status(
            task_id, TaskStatus.ARCHIVED.value
        ) is not None

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

        with self._tx() as engine:
            update_result = engine.execute(
                "UPDATE tasks "
                "SET status = ?, "
                "    claim_lock = ?, "
                "    claim_expires = ?, "
                "    started_at = COALESCE(started_at, ?), "
                "    worker_pid = ?, "
                "    last_heartbeat_at = ? "
                "WHERE id = ? "
                "  AND status = ? "
                "  AND (claim_lock IS NULL OR claim_expires < ?)",
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

            if update_result.rowcount == 0:
                return None

            # Create a run record
            result = engine.execute(
                "INSERT INTO task_runs "
                "(task_id, profile, status, claim_lock, claim_expires, "
                " worker_pid, max_runtime_seconds, last_heartbeat_at, started_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    task_id, worker_id, TaskRunStatus.RUNNING.value,
                    lock, expires, pid, None, now, now,
                ),
            )
            run_id = result.lastrowid
            assert run_id is not None, "Insert created a run but got no id"

            engine.execute(
                "UPDATE tasks SET current_run_id=? WHERE id=?",
                (run_id, task_id),
            )

            self._emit_event(
                engine, task_id, TaskEventKind.CLAIMED.value,
                run_id=run_id,
                payload={"worker_id": worker_id, "pid": pid, "lock": lock},
            )

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
        with self._tx() as engine:
            result = engine.execute(
                "SELECT claim_lock, claim_expires, current_run_id "
                "FROM tasks WHERE id=?",
                (task_id,),
            )
            row = result.fetchone()
            if row is None:
                return False

            if row["claim_lock"] != claim_lock:
                return False

            new_expires = now + DEFAULT_CLAIM_TTL
            engine.execute(
                "UPDATE tasks SET last_heartbeat_at=?, claim_expires=? "
                "WHERE id=?",
                (now, new_expires, task_id),
            )

            run_id = row["current_run_id"]
            if run_id is not None:
                engine.execute(
                    "UPDATE task_runs SET last_heartbeat_at=?, claim_expires=? "
                    "WHERE id=?",
                    (now, new_expires, run_id),
                )

            self._emit_event(
                engine, task_id, TaskEventKind.HEARTBEAT.value,
                run_id=run_id,
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

        with self._tx() as engine:
            result = engine.execute(
                "SELECT id, current_run_id, consecutive_failures, "
                "       COALESCE(max_retries, ?) AS max_ret "
                "FROM tasks "
                "WHERE status IN (?, ?) "
                "  AND claim_expires IS NOT NULL "
                "  AND claim_expires <= ?",
                (
                    DEFAULT_MAX_RETRIES,
                    TaskStatus.RUNNING.value,
                    TaskStatus.BLOCKED.value,
                    now,
                ),
            )
            expired = result.fetchall()

            for row in expired:
                task_id = row["id"]
                run_id = row["current_run_id"]
                failures = row["consecutive_failures"] + 1
                max_ret = row["max_ret"]

                if run_id is not None:
                    engine.execute(
                        "UPDATE task_runs SET status=?, ended_at=?, "
                        "outcome=? WHERE id=?",
                        (
                            TaskRunStatus.TIMED_OUT.value,
                            now,
                            "timed_out",
                            run_id,
                        ),
                    )

                engine.execute(
                    "UPDATE tasks "
                    "SET status=?, completed_at=?, consecutive_failures=?, "
                    "    last_failure_error=?, "
                    "    claim_lock=NULL, claim_expires=NULL, "
                    "    worker_pid=NULL, current_run_id=NULL "
                    "WHERE id=?",
                    (
                        TaskStatus.FAILED.value,
                        now,
                        failures,
                        "Claim TTL expired",
                        task_id,
                    ),
                )

                self._emit_event(
                    engine, task_id, "released",
                    run_id=run_id,
                    payload={"reason": "claim_ttl_expired", "failures": failures},
                )

                self._emit_event(
                    engine, task_id, TaskEventKind.FAILED.value,
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

        with self._tx() as engine:
            result = engine.execute(
                "SELECT id, consecutive_failures "
                "FROM tasks "
                "WHERE status = ? "
                "  AND consecutive_failures <= COALESCE(max_retries, ?) "
                "  AND consecutive_failures > 0",
                (TaskStatus.FAILED.value, DEFAULT_MAX_RETRIES),
            )

            for row in result.fetchall():
                task_id = row["id"]

                run_result = engine.execute(
                    "INSERT INTO task_runs "
                    "(task_id, status, started_at) "
                    "VALUES (?, ?, ?)",
                    (task_id, TaskRunStatus.RUNNING.value, now),
                )
                run_id = run_result.lastrowid

                engine.execute(
                    "UPDATE tasks "
                    "SET status=?, claim_lock=NULL, claim_expires=NULL, "
                    "    current_run_id=?, worker_pid=NULL "
                    "WHERE id=?",
                    (TaskStatus.READY.value, run_id, task_id),
                )

                self._emit_event(
                    engine, task_id, TaskEventKind.DEPENDENCY_PROMOTED.value,
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
        with self._tx() as engine:
            result = engine.execute("SELECT id FROM tasks WHERE id=?", (task_id,))
            if result.fetchone() is None:
                raise ValueError(f"Task '{task_id}' not found")

            result = engine.execute(
                "INSERT INTO task_comments (task_id, author, body, created_at) "
                "VALUES (?, ?, ?, ?)",
                (task_id, author, body, now),
            )

            comment = TaskComment(
                id=result.lastrowid,
                task_id=task_id,
                author=author,
                body=body,
                created_at=now,
            )

            self._emit_event(
                engine, task_id, TaskEventKind.COMMENTED.value,
                payload={"author": author},
            )

        return comment

    def get_comments(self, task_id: str) -> List[TaskComment]:
        """Return all comments for a task, ordered by creation time."""
        with self._tx("DEFERRED") as engine:
            result = engine.execute(
                "SELECT * FROM task_comments WHERE task_id=? ORDER BY created_at",
                (task_id,),
            )
            return _result_to_comment_list(result)

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------

    def get_events(self, task_id: str, limit: int = 50) -> List[TaskEvent]:
        """Return audit events for a task, latest first."""
        with self._tx("DEFERRED") as engine:
            result = engine.execute(
                "SELECT * FROM task_events WHERE task_id=? "
                "ORDER BY id DESC LIMIT ?",
                (task_id, limit),
            )
            return _result_to_event_list(result)

    # ------------------------------------------------------------------
    # Runs
    # ------------------------------------------------------------------

    def get_runs(self, task_id: str) -> List[TaskRun]:
        """Return all runs for a task, latest first."""
        with self._tx("DEFERRED") as engine:
            result = engine.execute(
                "SELECT * FROM task_runs WHERE task_id=? ORDER BY id DESC",
                (task_id,),
            )
            return _result_to_run_list(result)

    # ------------------------------------------------------------------
    # Dependencies
    # ------------------------------------------------------------------

    def add_dependency(self, task_id: str, parent_id: str) -> None:
        """Add a parent → child dependency edge.

        Raises ``ValueError`` if the parent doesn't exist, self-link, or cycle.
        """
        with self._tx() as engine:
            result = engine.execute("SELECT id FROM tasks WHERE id=?", (task_id,))
            if result.fetchone() is None:
                raise ValueError(f"Task '{task_id}' not found")
            result = engine.execute("SELECT id FROM tasks WHERE id=?", (parent_id,))
            if result.fetchone() is None:
                raise ValueError(f"Parent task '{parent_id}' not found")

            if task_id == parent_id:
                raise ValueError("Self-links are not allowed")

            if self._detect_cycle(engine, task_id, parent_id):
                raise ValueError("Would create a cycle")

            if self._link_exists(engine, parent_id, task_id):
                return

            engine.execute(
                "INSERT INTO task_links (parent_id, child_id) VALUES (?, ?)",
                (parent_id, task_id),
            )

            self._resolve_dependencies(engine, task_id)

    def remove_dependency(self, task_id: str, parent_id: str) -> bool:
        """Remove a parent → child dependency edge.

        Returns:
            ``True`` if removed, ``False`` if the link didn't exist.
        """
        with self._tx() as engine:
            result = engine.execute(
                "DELETE FROM task_links WHERE parent_id=? AND child_id=?",
                (parent_id, task_id),
            )
            removed = result.rowcount > 0
            if removed:
                self._resolve_dependencies(engine, task_id)
            return removed

    def get_parents(self, task_id: str) -> List[dict]:
        """Return parent task summaries (id, title, status)."""
        with self._tx("DEFERRED") as engine:
            result = engine.execute(
                "SELECT t.id, t.title, t.status FROM task_links l "
                "JOIN tasks t ON l.parent_id = t.id "
                "WHERE l.child_id=?",
                (task_id,),
            )
            return result.fetchall()

    def get_children(self, task_id: str) -> List[dict]:
        """Return child task summaries (id, title, status)."""
        with self._tx("DEFERRED") as engine:
            result = engine.execute(
                "SELECT t.id, t.title, t.status FROM task_links l "
                "JOIN tasks t ON l.child_id = t.id "
                "WHERE l.parent_id=?",
                (task_id,),
            )
            return result.fetchall()

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    def stats(self) -> Dict[str, Any]:
        """Return summary statistics across all tasks."""
        with self._tx("DEFERRED") as engine:
            result = engine.execute(
                "SELECT status, COUNT(*) AS cnt FROM tasks GROUP BY status"
            )
            by_status: Dict[str, int] = {}
            for r in result.fetchall():
                by_status[r["status"]] = r["cnt"]

            result = engine.execute("SELECT COUNT(*) AS total FROM tasks")
            total = result.fetchone()["total"]

            return {
                "total": total,
                "by_status": by_status,
            }

    # ------------------------------------------------------------------
    # Dispatcher helpers
    # ------------------------------------------------------------------

    def _update_workspace_path(self, task_id: str, workspace_path: str) -> None:
        """Update the ``workspace_path`` for a task (called after allocation)."""
        with self._tx() as engine:
            engine.execute(
                "UPDATE tasks SET workspace_path=? WHERE id=?",
                (workspace_path, task_id),
            )

    def _set_worker_pid(self, task_id: str, pid: int) -> None:
        """Set the ``worker_pid`` for a task (called after spawning)."""
        with self._tx() as engine:
            engine.execute(
                "UPDATE tasks SET worker_pid=? WHERE id=?",
                (pid, task_id),
            )