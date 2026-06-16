"""Cron Scheduled Task Scheduler — periodic task dispatch.

Provides:
- ``CronTaskStore`` — persistence layer (cron_tasks + cron_executions tables)
- ``CronScheduler`` — background asyncio loop, checks every minute,
  creates kanban tasks via the TaskStore when a cron expression matches.

Uses ``croniter`` for cron-expression parsing.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from croniter import croniter

from simple_a2a_registry.database import DatabaseEngine
from simple_a2a_registry.orchestration.store import TaskStore

logger = logging.getLogger("a2a_registry.orchestration.cron")

# Default poll interval (seconds)
SCHEDULER_INTERVAL = 60


# ---------------------------------------------------------------------------
# SQL schema
# ---------------------------------------------------------------------------

_SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA busy_timeout=5000;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS cron_tasks (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    assignee        TEXT NOT NULL,
    cron_expression TEXT NOT NULL,
    task_template   TEXT NOT NULL,  -- JSON: {title, body, priority, ...}
    enabled         INTEGER NOT NULL DEFAULT 1,
    last_run        INTEGER,
    next_run        INTEGER,
    created_by      TEXT NOT NULL,
    created_at      INTEGER NOT NULL,
    tenant          TEXT
);

CREATE TABLE IF NOT EXISTS cron_executions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    cron_task_id   TEXT NOT NULL,
    task_id        TEXT NOT NULL,
    scheduled_at   INTEGER NOT NULL,
    started_at     INTEGER,
    completed_at   INTEGER,
    status         TEXT NOT NULL DEFAULT 'running',
    FOREIGN KEY (cron_task_id) REFERENCES cron_tasks(id)
);

CREATE INDEX IF NOT EXISTS idx_cron_tasks_enabled      ON cron_tasks(enabled);
CREATE INDEX IF NOT EXISTS idx_cron_tasks_next_run     ON cron_tasks(next_run);
CREATE INDEX IF NOT EXISTS idx_cron_executions_task    ON cron_executions(cron_task_id, scheduled_at);
"""

_MYSQL_SCHEMA = """
CREATE TABLE IF NOT EXISTS cron_tasks (
    id              VARCHAR(64) PRIMARY KEY,
    name            VARCHAR(255) NOT NULL,
    assignee        VARCHAR(255) NOT NULL,
    cron_expression VARCHAR(255) NOT NULL,
    task_template   TEXT NOT NULL,
    enabled         INT NOT NULL DEFAULT 1,
    last_run        BIGINT,
    next_run        BIGINT,
    created_by      VARCHAR(255) NOT NULL,
    created_at      BIGINT NOT NULL,
    tenant          VARCHAR(255)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS cron_executions (
    id             INT AUTO_INCREMENT PRIMARY KEY,
    cron_task_id   VARCHAR(64) NOT NULL,
    task_id        VARCHAR(64) NOT NULL,
    scheduled_at   BIGINT NOT NULL,
    started_at     BIGINT,
    completed_at   BIGINT,
    status         VARCHAR(50) NOT NULL DEFAULT 'running',
    FOREIGN KEY (cron_task_id) REFERENCES cron_tasks(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE INDEX IF NOT EXISTS idx_cron_tasks_enabled      ON cron_tasks(enabled);
CREATE INDEX IF NOT EXISTS idx_cron_tasks_next_run     ON cron_tasks(next_run);
CREATE INDEX IF NOT EXISTS idx_cron_executions_task    ON cron_executions(cron_task_id, scheduled_at);
"""


def _maybe_create_schema(engine: DatabaseEngine) -> None:
    """Create cron tables on first connect (SQLite or MySQL)."""
    if engine.driver == "sqlite":
        engine.executescript(_SCHEMA_SQL)
        engine.commit()
    else:
        engine.executescript(_MYSQL_SCHEMA)
        engine.commit()


# ---------------------------------------------------------------------------
# CronTaskStore
# ---------------------------------------------------------------------------


@dataclass
class CronTask:
    """A single cron definition — maps to ``cron_tasks`` table."""

    id: str = ""
    name: str = ""
    assignee: str = ""
    cron_expression: str = ""
    task_template: str = ""  # JSON
    enabled: bool = True
    last_run: Optional[int] = None
    next_run: Optional[int] = None
    created_by: str = ""
    created_at: int = 0
    tenant: Optional[str] = None

    def ensure_id(self) -> str:
        if not self.id:
            self.id = "cron_" + uuid.uuid4().hex[:12]
        return self.id

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {}
        for f_name in self.__dataclass_fields__:
            val = getattr(self, f_name)
            if val is not None:
                d[f_name] = val
        d["enabled"] = int(self.enabled)
        try:
            d["task_template"] = json.loads(self.task_template) if isinstance(self.task_template, str) else self.task_template
        except (json.JSONDecodeError, TypeError):
            pass
        return d

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> CronTask:
        valid = {k: v for k, v in data.items() if k in CronTask.__dataclass_fields__}
        if "enabled" in valid:
            valid["enabled"] = bool(valid["enabled"])
        if isinstance(valid.get("task_template"), dict):
            valid["task_template"] = json.dumps(valid["task_template"])
        return CronTask(**valid)


@dataclass
class CronExecution:
    """A single execution record — maps to ``cron_executions`` table."""

    id: int = 0
    cron_task_id: str = ""
    task_id: str = ""
    scheduled_at: int = 0
    started_at: Optional[int] = None
    completed_at: Optional[int] = None
    status: str = "running"

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {}
        for f_name in self.__dataclass_fields__:
            val = getattr(self, f_name)
            if val is not None:
                d[f_name] = val
        return d

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> CronExecution:
        valid = {k: v for k, v in data.items() if k in CronExecution.__dataclass_fields__}
        return CronExecution(**valid)


def _row_to_cron(row: dict) -> CronTask:
    """Convert a raw DB row dict to a CronTask."""
    return CronTask(
        id=row["id"],
        name=row["name"],
        assignee=row["assignee"],
        cron_expression=row["cron_expression"],
        task_template=row["task_template"],
        enabled=bool(row["enabled"]),
        last_run=row.get("last_run"),
        next_run=row.get("next_run"),
        created_by=row["created_by"],
        created_at=row["created_at"],
        tenant=row.get("tenant"),
    )


def _row_to_execution(row: dict) -> CronExecution:
    """Convert a raw DB row dict to a CronExecution."""
    return CronExecution(
        id=row["id"],
        cron_task_id=row["cron_task_id"],
        task_id=row["task_id"],
        scheduled_at=row["scheduled_at"],
        started_at=row.get("started_at"),
        completed_at=row.get("completed_at"),
        status=row["status"],
    )


class CronTaskStore:
    """Database persistence for cron tasks and their execution history.

    Thread-safe via the same ``DatabaseEngine`` locking as ``TaskStore``.
    """

    def __init__(self, engine: DatabaseEngine) -> None:
        self._engine = engine

    # -- cron_tasks CRUD ---------------------------------------------------

    def create_cron_task(self, cron: CronTask) -> str:
        """Insert a new cron task. Returns its id."""
        cron.ensure_id()
        now = int(time.time())
        cron.created_at = now

        # Compute first next_run
        try:
            itr = croniter(cron.cron_expression, now - 1)
            cron.next_run = int(itr.get_next())
        except (ValueError, KeyError):
            cron.next_run = now + 3600  # fallback: 1 hour later

        self._engine.execute(
            """INSERT INTO cron_tasks
               (id, name, assignee, cron_expression, task_template,
                enabled, last_run, next_run, created_by, created_at, tenant)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                cron.id, cron.name, cron.assignee, cron.cron_expression,
                cron.task_template, int(cron.enabled),
                cron.last_run, cron.next_run, cron.created_by,
                cron.created_at, cron.tenant,
            ),
        )
        self._engine.commit()
        return cron.id

    def get_cron_task(self, cron_id: str) -> Optional[CronTask]:
        """Fetch a cron task by id."""
        cur = self._engine.execute(
            "SELECT * FROM cron_tasks WHERE id = ?", (cron_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        return _row_to_cron(row)

    def list_cron_tasks(self, enabled_only: bool = False) -> List[CronTask]:
        """List all cron tasks, optionally only enabled ones."""
        if enabled_only:
            cur = self._engine.execute(
                "SELECT * FROM cron_tasks WHERE enabled = 1 ORDER BY created_at DESC",
            )
        else:
            cur = self._engine.execute(
                "SELECT * FROM cron_tasks ORDER BY created_at DESC",
            )
        return [_row_to_cron(row) for row in cur.fetchall()]

    def delete_cron_task(self, cron_id: str) -> bool:
        """Delete a cron task and its executions. Returns True if deleted."""
        self._engine.execute(
            "DELETE FROM cron_executions WHERE cron_task_id = ?", (cron_id,),
        )
        cur = self._engine.execute(
            "DELETE FROM cron_tasks WHERE id = ?", (cron_id,),
        )
        self._engine.commit()
        return cur.rowcount > 0

    def update_next_run(self, cron_id: str, last_run: int, next_run: int) -> None:
        """Update last_run and next_run after a successful tick."""
        self._engine.execute(
            "UPDATE cron_tasks SET last_run = ?, next_run = ? WHERE id = ?",
            (last_run, next_run, cron_id),
        )
        self._engine.commit()

    def set_enabled(self, cron_id: str, enabled: bool) -> bool:
        """Enable or disable a cron task. Returns True if updated."""
        cur = self._engine.execute(
            "UPDATE cron_tasks SET enabled = ? WHERE id = ?",
            (int(enabled), cron_id),
        )
        self._engine.commit()
        return cur.rowcount > 0

    # -- cron_executions CRUD ----------------------------------------------

    def create_execution(self, execution: CronExecution) -> int:
        """Insert an execution record. Returns its id."""
        self._engine.execute(
            """INSERT INTO cron_executions
               (cron_task_id, task_id, scheduled_at, started_at, completed_at, status)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                execution.cron_task_id, execution.task_id,
                execution.scheduled_at, execution.started_at,
                execution.completed_at, execution.status,
            ),
        )
        self._engine.commit()
        cur = self._engine.execute("SELECT last_insert_rowid()")
        row = cur.fetchone()
        execution.id = row["last_insert_rowid()"] if row else 0
        return execution.id

    def update_execution_status(
        self, exec_id: int, status: str,
        completed_at: Optional[int] = None,
    ) -> None:
        """Update the status (and optionally completed_at) of an execution."""
        if completed_at is not None:
            self._engine.execute(
                "UPDATE cron_executions SET status = ?, completed_at = ? WHERE id = ?",
                (status, completed_at, exec_id),
            )
        else:
            self._engine.execute(
                "UPDATE cron_executions SET status = ? WHERE id = ?",
                (status, exec_id),
            )
        self._engine.commit()

    def list_executions(
        self, cron_task_id: str, limit: int = 50, offset: int = 0,
    ) -> List[CronExecution]:
        """List executions for a cron task, newest first."""
        cur = self._engine.execute(
            "SELECT * FROM cron_executions WHERE cron_task_id = ? "
            "ORDER BY scheduled_at DESC LIMIT ? OFFSET ?",
            (cron_task_id, limit, offset),
        )
        return [_row_to_execution(row) for row in cur.fetchall()]

    def close(self) -> None:
        """Close the underlying engine."""
        self._engine.close()


# ---------------------------------------------------------------------------
# CronScheduler — background loop
# ---------------------------------------------------------------------------


def compute_next_run(cron_expression: str, after: float) -> Optional[int]:
    """Compute the next cron match timestamp.

    Args:
        cron_expression: Standard cron expression (5 fields).
        after: Unix timestamp to compute from.

    Returns:
        Next matching Unix timestamp (int), or None if invalid.
    """
    try:
        itr = croniter(cron_expression, after)
        return int(itr.get_next())
    except (ValueError, KeyError):
        return None


async def asyncio_sleep(seconds: float) -> None:
    """Thin wrapper around asyncio.sleep so tests can monkey-patch it."""
    await asyncio.sleep(seconds)


class CronScheduler:
    """Background coroutine that periodically checks cron tasks and dispatches work.

    Every ``interval`` seconds, it:
      1. Loads enabled cron tasks whose ``next_run`` is due.
      2. Creates a kanban task via ``TaskStore.create_task``.
      3. Records an execution entry.
      4. Advances the cron task's ``next_run``.

    Wired into ``create_app`` via ``on_startup`` / ``on_cleanup``.
    """

    def __init__(
        self,
        cron_store: CronTaskStore,
        task_store: TaskStore,
        interval: int = SCHEDULER_INTERVAL,
        created_by: str = "cron-scheduler",
    ) -> None:
        self._cron_store = cron_store
        self._task_store = task_store
        self._interval = interval
        self._created_by = created_by
        self._running = False
        self._task: Optional[asyncio.Task] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Enter the scheduler loop until ``stop()`` is called."""
        self._running = True
        logger.info(
            "CronScheduler started (interval=%ds)", self._interval,
        )
        while self._running:
            try:
                await self._tick()
            except Exception:
                logger.exception("CronScheduler tick failed")
            await asyncio_sleep(self._interval)
        logger.info("CronScheduler stopped")

    def stop(self) -> None:
        """Signal the loop to exit."""
        self._running = False
        logger.info("CronScheduler stopping...")

    # ------------------------------------------------------------------
    # Tick logic
    # ------------------------------------------------------------------

    async def _tick(self) -> int:
        """Run one scheduler tick.

        Returns:
            Number of tasks created in this tick.
        """
        now = int(time.time())
        due = self._load_due_cron_tasks(now)
        if not due:
            return 0

        created = 0
        for cron in due:
            try:
                await self._fire_cron_task(cron, now)
                created += 1
            except Exception:
                logger.exception(
                    "Failed to fire cron task '%s' (%s)", cron.id, cron.name,
                )
        return created

    def _load_due_cron_tasks(self, now: int) -> List[CronTask]:
        """Load enabled cron tasks whose next_run is <= now."""
        all_enabled = self._cron_store.list_cron_tasks(enabled_only=True)
        due: List[CronTask] = []
        for cron in all_enabled:
            if cron.next_run is not None and cron.next_run <= now:
                due.append(cron)
        return due

    async def _fire_cron_task(self, cron: CronTask, now: int) -> None:
        """Create a kanban task from a cron definition and record execution."""
        template = self._parse_template(cron.task_template)
        title = template.get("title", cron.name)
        body = template.get("body", "")
        priority = template.get("priority", 0)

        # Create the kanban task
        task = self._task_store.create_task(
            title=title,
            body=body,
            assignee=cron.assignee,
            priority=priority,
            tenant=cron.tenant,
            created_by=self._created_by,
        )

        # Record execution
        exec_rec = CronExecution(
            cron_task_id=cron.id,
            task_id=task.id,
            scheduled_at=now,
            started_at=now,
            status="running",
        )
        self._cron_store.create_execution(exec_rec)

        # Mark execution completed (kanban task was created successfully)
        self._cron_store.update_execution_status(
            exec_rec.id, "completed", completed_at=int(time.time()),
        )

        # Advance next_run
        next_run = compute_next_run(cron.cron_expression, now - 1)
        if next_run is not None:
            self._cron_store.update_next_run(cron.id, now, next_run)
        else:
            logger.warning(
                "Invalid cron expression '%s' for task '%s' — disabling",
                cron.cron_expression, cron.name,
            )
            self._cron_store.set_enabled(cron.id, False)

        logger.info(
            "Cron task '%s' (%s) fired → task '%s' (next_run=%s)",
            cron.id, cron.name, task.id,
            next_run,
        )

    @staticmethod
    def _parse_template(raw: str) -> Dict[str, Any]:
        """Parse the task_template JSON string, returning a dict."""
        if not raw:
            return {}
        try:
            if isinstance(raw, dict):
                return raw
            return json.loads(raw) if isinstance(raw, str) else {}
        except (json.JSONDecodeError, TypeError):
            logger.warning("Failed to parse task_template JSON: %s", raw[:200])
            return {}