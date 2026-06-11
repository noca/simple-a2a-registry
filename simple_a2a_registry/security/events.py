"""Security Event models (P1) — unified security audit event system.

Every security decision (allow / deny / block) produces a SecurityEvent
that is persisted in the ``security_events`` table and queryable via API.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

from simple_a2a_registry.database import DatabaseEngine

logger = logging.getLogger("a2a_registry.security.events")


# ---------------------------------------------------------------------------
# Event types
# ---------------------------------------------------------------------------


class SecurityEventType(str, Enum):
    AUTH_FAILURE = "AUTH_FAILURE"
    SCOPE_DENIED = "SCOPE_DENIED"
    TENANT_MISMATCH = "TENANT_MISMATCH"
    AGENT_NOT_FOUND = "AGENT_NOT_FOUND"
    AGENT_DISABLED = "AGENT_DISABLED"
    DELEGATION_DEPTH_EXCEEDED = "DELEGATION_DEPTH_EXCEEDED"
    TOKEN_EXPIRED = "TOKEN_EXPIRED"
    TOKEN_INVALID = "TOKEN_INVALID"
    TOKEN_MISMATCH = "TOKEN_MISMATCH"
    AUTHORIZATION_DENIED = "AUTHORIZATION_DENIED"
    AUTHORIZATION_ALLOWED = "AUTHORIZATION_ALLOWED"
    DISPATCH_DENY = "DISPATCH_DENY"
    PLUGIN_REJECT = "PLUGIN_REJECT"
    SECURITY_VIOLATION = "SECURITY_VIOLATION"
    MODE_AUDIT = "MODE_AUDIT"      # recorded in audit mode (would-be deny)
    MODE_WARN = "MODE_WARN"        # recorded in warn mode (would-be deny)


# ---------------------------------------------------------------------------
# SecurityEvent dataclass
# ---------------------------------------------------------------------------


@dataclass
class SecurityEvent:
    event_id: str = ""
    event_type: str = ""
    timestamp: float = 0.0
    actor: str = ""
    target: str = ""
    tenant: str = ""
    decision: str = "deny"  # "allow" | "deny" | "block"
    reason: str = ""
    scope_used: str = ""
    task_id: str = ""
    created_at: float = 0.0
    delegation_chain: Optional[List[Dict[str, Any]]] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "timestamp": self.timestamp,
            "actor": self.actor,
            "target": self.target,
            "tenant": self.tenant,
            "decision": self.decision,
            "reason": self.reason,
            "scope_used": self.scope_used,
            "task_id": self.task_id,
            "created_at": self.created_at,
        }
        if self.delegation_chain is not None:
            d["delegation_chain"] = self.delegation_chain
        if self.metadata:
            d["metadata"] = self.metadata
        return d


# ---------------------------------------------------------------------------
# Event persistence schema
# ---------------------------------------------------------------------------

SECURITY_EVENTS_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS security_events (
    event_id        TEXT PRIMARY KEY,
    event_type      TEXT NOT NULL,
    timestamp       REAL NOT NULL,
    actor           TEXT NOT NULL,
    target          TEXT NOT NULL,
    tenant          TEXT NOT NULL DEFAULT '',
    decision        TEXT NOT NULL,
    reason          TEXT,
    scope_used      TEXT NOT NULL DEFAULT '',
    delegation_chain TEXT,       -- JSON
    metadata        TEXT,        -- JSON blob
    task_id         TEXT,
    created_at      REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sec_events_task ON security_events(task_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_sec_events_type ON security_events(event_type, timestamp);
CREATE INDEX IF NOT EXISTS idx_sec_events_tenant ON security_events(tenant, timestamp);
CREATE INDEX IF NOT EXISTS idx_sec_events_actor ON security_events(actor, timestamp);
"""

SECURITY_EVENTS_SCHEMA_MYSQL = """
CREATE TABLE IF NOT EXISTS security_events (
    event_id        VARCHAR(64) PRIMARY KEY,
    event_type      VARCHAR(50) NOT NULL,
    timestamp       DOUBLE NOT NULL,
    actor           VARCHAR(255) NOT NULL,
    target          VARCHAR(255) NOT NULL,
    tenant          VARCHAR(255) NOT NULL DEFAULT '',
    decision        VARCHAR(20) NOT NULL,
    reason          TEXT,
    scope_used      VARCHAR(255) NOT NULL DEFAULT '',
    delegation_chain TEXT,       -- JSON
    metadata        TEXT,        -- JSON blob
    task_id         VARCHAR(64),
    created_at      DOUBLE NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE INDEX idx_sec_events_task ON security_events(task_id, created_at);
CREATE INDEX idx_sec_events_type ON security_events(event_type, created_at);
CREATE INDEX idx_sec_events_tenant ON security_events(tenant, created_at);
CREATE INDEX idx_sec_events_actor ON security_events(actor, created_at);
"""


# ---------------------------------------------------------------------------
# SecurityEventStore
# ---------------------------------------------------------------------------


class SecurityEventStore:
    """Persistent store for SecurityEvents, sharing the TaskStore's engine."""

    def __init__(self, engine: DatabaseEngine) -> None:
        self._engine = engine
        self._lock = threading.RLock()

    def ensure_schema(self) -> None:
        """Create the security_events table if it doesn't exist."""
        if self._engine.driver == "sqlite":
            self._engine.executescript(SECURITY_EVENTS_SCHEMA_SQL)
        else:
            for stmt in SECURITY_EVENTS_SCHEMA_MYSQL.split(";"):
                stripped = stmt.strip()
                if stripped:
                    try:
                        self._engine.execute(stripped)
                    except Exception:
                        pass
        self._engine.commit()

    def record(
        self,
        event_type: str,
        actor: str,
        target: str,
        decision: str,
        *,
        tenant: str = "",
        reason: str = "",
        scope_used: str = "",
        delegation_chain: Optional[List[Dict[str, Any]]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        task_id: Optional[str] = None,
    ) -> SecurityEvent:
        """Create and persist a SecurityEvent. Returns the event."""
        event_id = "sev_" + uuid.uuid4().hex[:12]
        now = time.time()
        event = SecurityEvent(
            event_id=event_id,
            event_type=event_type,
            timestamp=now,
            actor=actor,
            target=target,
            tenant=tenant,
            decision=decision,
            reason=reason,
            scope_used=scope_used,
            delegation_chain=delegation_chain,
            metadata=metadata or {},
        )
        with self._lock:
            self._engine.execute(
                """INSERT OR IGNORE INTO security_events
                   (event_id, event_type, timestamp, actor, target, tenant,
                    decision, reason, scope_used, delegation_chain, metadata, task_id, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    event_id, event_type, now, actor, target, tenant,
                    decision, reason, scope_used,
                    json.dumps(delegation_chain) if delegation_chain else None,
                    json.dumps(metadata) if metadata else None,
                    task_id, now,
                ),
            )
            self._engine.commit()
        return event

    def list_by_task(
        self,
        task_id: str,
        limit: int = 50,
        offset: int = 0,
    ) -> List[SecurityEvent]:
        """Return security events for a specific task, newest first."""
        with self._lock:
            result = self._engine.execute(
                "SELECT * FROM security_events WHERE task_id=? ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (task_id, limit, offset),
            )
            return self._rows_to_events(result)

    def list_all(
        self,
        limit: int = 50,
        offset: int = 0,
        event_type: Optional[str] = None,
        actor: Optional[str] = None,
        tenant: Optional[str] = None,
        task_id: Optional[str] = None,
        since: Optional[float] = None,
        until: Optional[float] = None,
    ) -> List[SecurityEvent]:
        """List security events globally with optional filters, newest first."""
        clauses = []
        params: list = []
        if event_type:
            clauses.append("event_type=?")
            params.append(event_type)
        if actor:
            clauses.append("actor=?")
            params.append(actor)
        if tenant:
            clauses.append("tenant=?")
            params.append(tenant)
        if task_id:
            clauses.append("task_id=?")
            params.append(task_id)
        if since is not None:
            clauses.append("created_at>=?")
            params.append(since)
        if until is not None:
            clauses.append("created_at<?")
            params.append(until)
        where = ""
        if clauses:
            where = "WHERE " + " AND ".join(clauses)
        with self._lock:
            result = self._engine.execute(
                f"SELECT * FROM security_events {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (*params, limit, offset),
            )
            return self._rows_to_events(result)

    def count_all(self, event_type: Optional[str] = None,
                  actor: Optional[str] = None,
                  tenant: Optional[str] = None,
                  task_id: Optional[str] = None,
                  since: Optional[float] = None,
                  until: Optional[float] = None) -> int:
        clauses = []
        params: list = []
        if event_type:
            clauses.append("event_type=?")
            params.append(event_type)
        if actor:
            clauses.append("actor=?")
            params.append(actor)
        if tenant:
            clauses.append("tenant=?")
            params.append(tenant)
        if task_id:
            clauses.append("task_id=?")
            params.append(task_id)
        if since is not None:
            clauses.append("created_at>=?")
            params.append(since)
        if until is not None:
            clauses.append("created_at<?")
            params.append(until)
        where = ""
        if clauses:
            where = "WHERE " + " AND ".join(clauses)
        with self._lock:
            result = self._engine.execute(
                f"SELECT COUNT(*) AS cnt FROM security_events {where}", tuple(params),
            )
            row = result.fetchone()
            return row["cnt"] if row else 0

    @staticmethod
    def _rows_to_events(result) -> List[SecurityEvent]:
        events: List[SecurityEvent] = []
        for row in result.fetchall():
            dc = row.get("delegation_chain")
            md = row.get("metadata")
            events.append(SecurityEvent(
                event_id=row["event_id"],
                event_type=row["event_type"],
                timestamp=row["timestamp"],
                actor=row["actor"],
                target=row["target"],
                tenant=row.get("tenant", ""),
                decision=row["decision"],
                reason=row.get("reason", ""),
                scope_used=row.get("scope_used", ""),
                task_id=row.get("task_id", ""),
                created_at=row.get("created_at", 0.0),
                delegation_chain=json.loads(dc) if dc else None,
                metadata=json.loads(md) if md else {},
            ))
        return events