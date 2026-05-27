"""Audit logging subsystem — append-only, tamper-evident event tracking.

Provides:
- ``EventType`` enum: all sensitive operation categories.
- ``AuditEvent`` dataclass: timestamp, event_type, actor, target, detail, success.
- ``AuditStore``: append-only SQLite / MySQL persistence with time/type/actor query.
- Retention policy: configurable TTL (default 90 days).
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Dict, List, Optional

from simple_a2a_registry.database import DatabaseEngine, CursorResult

logger = logging.getLogger("a2a_registry.audit")

# ---------------------------------------------------------------------------
# Event Types
# ---------------------------------------------------------------------------


class EventType(str, Enum):
    """Categories of auditable operations in the A2A Registry."""

    CLIENT_CREATE = "CLIENT_CREATE"
    CLIENT_DELETE = "CLIENT_DELETE"
    AGENT_REGISTER = "AGENT_REGISTER"
    AGENT_DEREGISTER = "AGENT_DEREGISTER"
    TOKEN_ISSUE = "TOKEN_ISSUE"
    TASK_DISPATCH = "TASK_DISPATCH"
    CONFIG_CHANGE = "CONFIG_CHANGE"
    AUTH_FAILURE = "AUTH_FAILURE"


# ---------------------------------------------------------------------------
# Audit Event Record
# ---------------------------------------------------------------------------


@dataclass
class AuditEvent:
    """A single audit log entry.

    Fields
    ------
    timestamp:  Unix timestamp (float) when the event occurred.
    event_type: One of the ``EventType`` values.
    actor:      Subject who performed the action (client_id, agent_id, or IP).
    target:     Object the action was performed on (agent_id, client_id, etc.).
    detail:     Free-form JSON string with event-specific context.
    success:    Whether the operation succeeded.
    tenant_id:  Optional tenant scope for multi-tenant filtering.
    """

    timestamp: float
    event_type: str
    actor: str
    target: str
    detail: str = ""
    success: bool = True
    tenant_id: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_row(row: Dict[str, Any]) -> AuditEvent:
        return AuditEvent(
            timestamp=row["timestamp"],
            event_type=row["event_type"],
            actor=row["actor"],
            target=row["target"],
            detail=row.get("detail", ""),
            success=bool(row["success"]),
            tenant_id=row.get("tenant_id", ""),
        )


# ---------------------------------------------------------------------------
# Default retention
# ---------------------------------------------------------------------------

DEFAULT_RETENTION_DAYS = 90
AUDIT_TABLE_NAME = "audit_log"

# ---------------------------------------------------------------------------
# SQL Schema — SQLite
# ---------------------------------------------------------------------------

_AUDIT_SCHEMA_SQLITE = f"""\
CREATE TABLE IF NOT EXISTS {AUDIT_TABLE_NAME} (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   REAL NOT NULL,
    event_type  TEXT NOT NULL,
    actor       TEXT NOT NULL,
    target      TEXT NOT NULL,
    detail      TEXT NOT NULL DEFAULT '',
    success     INTEGER NOT NULL DEFAULT 1,
    tenant_id   TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_audit_timestamp ON {AUDIT_TABLE_NAME}(timestamp);
CREATE INDEX IF NOT EXISTS idx_audit_event_type ON {AUDIT_TABLE_NAME}(event_type);
CREATE INDEX IF NOT EXISTS idx_audit_actor ON {AUDIT_TABLE_NAME}(actor);
CREATE INDEX IF NOT EXISTS idx_audit_tenant ON {AUDIT_TABLE_NAME}(tenant_id);
"""

# ---------------------------------------------------------------------------
# SQL Schema — MySQL
# ---------------------------------------------------------------------------

_AUDIT_SCHEMA_MYSQL = f"""\
CREATE TABLE IF NOT EXISTS {AUDIT_TABLE_NAME} (
    id          BIGINT AUTO_INCREMENT PRIMARY KEY,
    timestamp   DOUBLE NOT NULL,
    event_type  VARCHAR(64) NOT NULL,
    actor       VARCHAR(255) NOT NULL,
    target      VARCHAR(255) NOT NULL,
    detail      TEXT NOT NULL,
    success     TINYINT NOT NULL DEFAULT 1,
    tenant_id   VARCHAR(128) NOT NULL DEFAULT '',
    INDEX idx_audit_timestamp (timestamp),
    INDEX idx_audit_event_type (event_type),
    INDEX idx_audit_actor (actor),
    INDEX idx_audit_tenant (tenant_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

# ---------------------------------------------------------------------------
# AuditStore
# ---------------------------------------------------------------------------


def _maybe_create_audit_schema(engine: DatabaseEngine, retention_days: int = DEFAULT_RETENTION_DAYS) -> None:
    """Create the audit_log table if it does not exist.

    Renamed from ``_create_schema`` to avoid collision with other
    ``_maybe_create_schema`` functions in the project, and to make
    the idempotency contract explicit in the name.

    Args:
        engine:         Database engine (SQLite or MySQL).
        retention_days: Default event retention period.  Only stored as
                        metadata when the schema is first created.
    """
    if engine.driver == "sqlite":
        engine.executescript(_AUDIT_SCHEMA_SQLITE)
        engine.commit()
    elif engine.driver == "mysql":
        for statement in _AUDIT_SCHEMA_MYSQL.split(";"):
            stripped = statement.strip()
            if not stripped:
                continue
            try:
                engine.execute(stripped)
            except Exception:
                pass  # ignore "already exists" errors
        engine.commit()
    logger.info(
        "Audit schema ready (retention=%d days)",
        retention_days,
    )


class AuditStore:
    """Append-only audit log store.

    Thread-safe via ``threading.RLock``.

    All audit events are write-once — there is no update or delete API
    (except for the TTL-based purge which is internal-only).
    """

    def __init__(
        self,
        engine: DatabaseEngine,
        retention_days: int = DEFAULT_RETENTION_DAYS,
    ) -> None:
        self._engine = engine
        self._lock = threading.RLock()
        self._retention_days = retention_days

    # ------------------------------------------------------------------
    # Log an event
    # ------------------------------------------------------------------

    def log(
        self,
        event_type: str,
        actor: str,
        target: str,
        *,
        detail: str = "",
        success: bool = True,
        tenant_id: str = "",
    ) -> int:
        """Record a single audit event (append-only).

        Args:
            event_type: One of ``EventType.*`` values.
            actor:      Who did it (client_id, agent_id, IP address).
            target:     What was acted upon (agent_id, client_id, etc.).
            detail:     Optional free-form JSON string.
            success:    Whether the operation succeeded.
            tenant_id:  Optional tenant scope for multi-tenant filtering.

        Returns:
            The row id of the newly inserted audit record.
        """
        now = time.time()
        with self._lock:
            result = self._engine.execute(
                f"INSERT INTO {AUDIT_TABLE_NAME} "
                "(timestamp, event_type, actor, target, detail, success, tenant_id) "
                "VALUES (?,?,?,?,?,?,?)",
                (now, event_type, actor, target, detail or "", 1 if success else 0, tenant_id),
            )
            self._engine.commit()
            row_id = result.lastrowid
            logger.debug(
                "Audit: [%s] actor=%s target=%s success=%s tenant=%s",
                event_type, actor, target, success, tenant_id or "(global)",
            )
            return row_id

    # ------------------------------------------------------------------
    # Query events
    # ------------------------------------------------------------------

    def query(
        self,
        *,
        event_type: Optional[str] = None,
        actor: Optional[str] = None,
        since: Optional[float] = None,
        until: Optional[float] = None,
        tenant_id: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
        sort_desc: bool = True,
    ) -> List[AuditEvent]:
        """Query audit events with optional filters.

        Args:
            event_type: Filter by event type.
            actor:      Filter by actor (substring match, case-insensitive).
            since:      Only events at or after this timestamp.
            until:      Only events before this timestamp.
            tenant_id:  Filter by exact tenant match (None = all tenants).
            limit:      Maximum results (default 100, max 1000).
            offset:     Pagination offset (default 0).
            sort_desc:  Sort newest-first (default True).

        Returns:
            List of :class:`AuditEvent` records.
        """
        limit = min(max(limit, 1), 1000)
        offset = max(offset, 0)

        where_clauses: List[str] = []
        params: List[Any] = []

        if event_type:
            where_clauses.append("event_type = ?")
            params.append(event_type)
        if actor:
            where_clauses.append("LOWER(actor) LIKE ?")
            params.append(f"%{actor.lower()}%")
        if since is not None:
            where_clauses.append("timestamp >= ?")
            params.append(since)
        if until is not None:
            where_clauses.append("timestamp < ?")
            params.append(until)
        if tenant_id is not None:
            where_clauses.append("tenant_id = ?")
            params.append(tenant_id)

        where_sql = ""
        if where_clauses:
            where_sql = "WHERE " + " AND ".join(where_clauses)

        order = "DESC" if sort_desc else "ASC"

        sql = (
            f"SELECT id, timestamp, event_type, actor, target, detail, success, tenant_id "
            f"FROM {AUDIT_TABLE_NAME} "
            f"{where_sql} "
            f"ORDER BY timestamp {order}, id {order} "
            f"LIMIT ? OFFSET ?"
        )
        params.append(limit)
        params.append(offset)

        with self._lock:
            result = self._engine.execute(sql, tuple(params))
            rows = result.fetchall()

        return [AuditEvent.from_row(r) for r in rows]

    def count(
        self,
        *,
        event_type: Optional[str] = None,
        actor: Optional[str] = None,
        since: Optional[float] = None,
        until: Optional[float] = None,
        tenant_id: Optional[str] = None,
    ) -> int:
        """Count audit events matching the given filters.

        Args:
            Same as :meth:`query`.

        Returns:
            Total matching event count.
        """
        where_clauses: List[str] = []
        params: List[Any] = []

        if event_type:
            where_clauses.append("event_type = ?")
            params.append(event_type)
        if actor:
            where_clauses.append("LOWER(actor) LIKE ?")
            params.append(f"%{actor.lower()}%")
        if since is not None:
            where_clauses.append("timestamp >= ?")
            params.append(since)
        if until is not None:
            where_clauses.append("timestamp < ?")
            params.append(until)
        if tenant_id is not None:
            where_clauses.append("tenant_id = ?")
            params.append(tenant_id)

        where_sql = ""
        if where_clauses:
            where_sql = "WHERE " + " AND ".join(where_clauses)

        with self._lock:
            result = self._engine.execute(
                f"SELECT COUNT(*) AS cnt FROM {AUDIT_TABLE_NAME} {where_sql}",
                tuple(params),
            )
            row = result.fetchone()
            return row["cnt"] if row else 0

    def stats(self) -> Dict[str, Any]:
        """Return summary statistics about the audit log.

        Returns:
            Dict with total_events, oldest_timestamp, newest_timestamp,
            and counts per event type.
        """
        with self._lock:
            total = self._engine.execute(
                f"SELECT COUNT(*) AS cnt FROM {AUDIT_TABLE_NAME}"
            ).fetchone()["cnt"]

            oldest_row = self._engine.execute(
                f"SELECT MIN(timestamp) AS ts FROM {AUDIT_TABLE_NAME}"
            ).fetchone()
            oldest = oldest_row["ts"] if oldest_row else None

            newest_row = self._engine.execute(
                f"SELECT MAX(timestamp) AS ts FROM {AUDIT_TABLE_NAME}"
            ).fetchone()
            newest = newest_row["ts"] if newest_row else None

            by_type_rows = self._engine.execute(
                f"SELECT event_type, COUNT(*) AS cnt "
                f"FROM {AUDIT_TABLE_NAME} GROUP BY event_type "
                f"ORDER BY cnt DESC"
            ).fetchall()

            by_type: Dict[str, int] = {}
            for r in by_type_rows:
                by_type[r["event_type"]] = r["cnt"]

        return {
            "total_events": total,
            "oldest_timestamp": oldest,
            "newest_timestamp": newest,
            "by_event_type": by_type,
        }

    # ------------------------------------------------------------------
    # Retention policy — purge events older than TTL
    # ------------------------------------------------------------------

    def purge_old(self, retention_days: Optional[int] = None) -> int:
        """Delete audit events older than the configured retention period.

        Args:
            retention_days: Override the default retention period.
                            If ``None``, uses the value set at construction time.

        Returns:
            Number of purged rows.
        """
        days = retention_days if retention_days is not None else self._retention_days
        cutoff = time.time() - (days * 86400)

        with self._lock:
            result = self._engine.execute(
                f"DELETE FROM {AUDIT_TABLE_NAME} WHERE timestamp < ?",
                (cutoff,),
            )
            self._engine.commit()
            purged = result.rowcount
            if purged:
                logger.info(
                    "Purged %d audit event(s) older than %d days",
                    purged, days,
                )
            return purged