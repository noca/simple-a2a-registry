"""ProvenanceTracker (PT) — P1 module for delegation chain tracking.

Tracks every delegation hop as a ProvenanceHop, building a traceable DAG
from the original requestor to the current worker.  Queryable by task id.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from simple_a2a_registry.database import DatabaseEngine

logger = logging.getLogger("a2a_registry.security.pt")


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


@dataclass
class ProvenanceHop:
    from_agent: str = ""
    to_agent: str = ""
    action: str = ""
    scope_at: str = ""
    timestamp: float = 0.0
    token_jti: str = ""


@dataclass
class ProvenanceChain:
    chain_id: str = ""
    origin_agent: str = ""
    origin_tenant: str = ""
    root_task_id: str = ""
    parent_task_id: Optional[str] = None
    depth: int = 0
    hops: List[ProvenanceHop] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "chain_id": self.chain_id,
            "origin_agent": self.origin_agent,
            "origin_tenant": self.origin_tenant,
            "root_task_id": self.root_task_id,
            "parent_task_id": self.parent_task_id,
            "depth": self.depth,
            "hops": [
                {
                    "from_agent": h.from_agent,
                    "to_agent": h.to_agent,
                    "action": h.action,
                    "scope_at": h.scope_at,
                    "timestamp": h.timestamp,
                    "token_jti": h.token_jti,
                }
                for h in self.hops
            ],
        }


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

PROVENANCE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS provenance_chains (
    chain_id        TEXT PRIMARY KEY,
    origin_agent    TEXT NOT NULL,
    origin_tenant   TEXT NOT NULL DEFAULT '',
    root_task_id    TEXT NOT NULL,
    parent_task_id  TEXT,
    depth           INTEGER NOT NULL DEFAULT 0,
    task_id         TEXT,
    created_at      REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS provenance_hops (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chain_id    TEXT NOT NULL,
    from_agent  TEXT NOT NULL,
    to_agent    TEXT NOT NULL,
    action      TEXT NOT NULL,
    scope_at    TEXT NOT NULL DEFAULT '',
    timestamp   REAL NOT NULL,
    token_jti   TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (chain_id) REFERENCES provenance_chains(chain_id)
);

CREATE INDEX IF NOT EXISTS idx_prov_chain_task ON provenance_chains(task_id);
CREATE INDEX IF NOT EXISTS idx_prov_hops_chain ON provenance_hops(chain_id);
"""

PROVENANCE_SCHEMA_MYSQL = """
CREATE TABLE IF NOT EXISTS provenance_chains (
    chain_id        VARCHAR(64) PRIMARY KEY,
    origin_agent    VARCHAR(255) NOT NULL,
    origin_tenant   VARCHAR(255) NOT NULL DEFAULT '',
    root_task_id    VARCHAR(64) NOT NULL,
    parent_task_id  VARCHAR(64),
    depth           INT NOT NULL DEFAULT 0,
    task_id         VARCHAR(64),
    created_at      DOUBLE NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS provenance_hops (
    id          INT AUTO_INCREMENT PRIMARY KEY,
    chain_id    VARCHAR(64) NOT NULL,
    from_agent  VARCHAR(255) NOT NULL,
    to_agent    VARCHAR(255) NOT NULL,
    action      VARCHAR(100) NOT NULL,
    scope_at    VARCHAR(255) NOT NULL DEFAULT '',
    timestamp   DOUBLE NOT NULL,
    token_jti   VARCHAR(64) NOT NULL DEFAULT ''
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE INDEX idx_prov_chain_task ON provenance_chains(task_id);
CREATE INDEX idx_prov_hops_chain ON provenance_hops(chain_id);
"""


# ---------------------------------------------------------------------------
# ProvenanceTracker
# ---------------------------------------------------------------------------


class ProvenanceTracker:
    """Tracks delegation chains across task hierarchies."""

    def __init__(self, engine: DatabaseEngine) -> None:
        self._engine = engine
        self._lock = threading.RLock()

    def ensure_schema(self) -> None:
        if self._engine.driver == "sqlite":
            self._engine.executescript(PROVENANCE_SCHEMA_SQL)
        else:
            for stmt in PROVENANCE_SCHEMA_MYSQL.split(";"):
                stripped = stmt.strip()
                if stripped:
                    try:
                        self._engine.execute(stripped)
                    except Exception:
                        pass
        self._engine.commit()

    # ------------------------------------------------------------------
    # Record
    # ------------------------------------------------------------------

    def record_hop(
        self,
        *,
        chain_id: str,
        from_agent: str,
        to_agent: str,
        action: str = "delegate",
        scope_at: str = "",
        token_jti: str = "",
    ) -> ProvenanceHop:
        """Record a single delegation hop."""
        hop = ProvenanceHop(
            from_agent=from_agent,
            to_agent=to_agent,
            action=action,
            scope_at=scope_at,
            timestamp=time.time(),
            token_jti=token_jti,
        )
        with self._lock:
            self._engine.execute(
                "INSERT INTO provenance_hops (chain_id, from_agent, to_agent, action, scope_at, timestamp, token_jti) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (chain_id, from_agent, to_agent, action, scope_at, hop.timestamp, token_jti),
            )
            # Update chain depth
            self._engine.execute(
                "UPDATE provenance_chains SET depth = (SELECT COUNT(*) FROM provenance_hops WHERE chain_id=?) "
                "WHERE chain_id=?",
                (chain_id, chain_id),
            )
            self._engine.commit()
        return hop

    def ensure_chain(
        self,
        *,
        chain_id: str,
        origin_agent: str,
        origin_tenant: str = "",
        root_task_id: str,
        parent_task_id: Optional[str] = None,
        task_id: str,
    ) -> None:
        """Ensure a provenance chain record exists (idempotent)."""
        with self._lock:
            self._engine.execute(
                """INSERT OR IGNORE INTO provenance_chains
                   (chain_id, origin_agent, origin_tenant, root_task_id,
                    parent_task_id, depth, task_id, created_at)
                   VALUES (?, ?, ?, ?, ?, 0, ?, ?)""",
                (chain_id, origin_agent, origin_tenant,
                 root_task_id, parent_task_id, task_id, time.time()),
            )
            self._engine.commit()

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def get_chain_by_task(self, task_id: str) -> Optional[ProvenanceChain]:
        """Retrieve the provenance chain for a given task."""
        with self._lock:
            result = self._engine.execute(
                "SELECT * FROM provenance_chains WHERE task_id=?",
                (task_id,),
            )
            row = result.fetchone()
            if row is None:
                return None

            chain = ProvenanceChain(
                chain_id=row["chain_id"],
                origin_agent=row["origin_agent"],
                origin_tenant=row.get("origin_tenant", ""),
                root_task_id=row["root_task_id"],
                parent_task_id=row.get("parent_task_id"),
                depth=row["depth"],
            )

            hop_result = self._engine.execute(
                "SELECT * FROM provenance_hops WHERE chain_id=? ORDER BY timestamp ASC",
                (chain.chain_id,),
            )
            for h in hop_result.fetchall():
                chain.hops.append(ProvenanceHop(
                    from_agent=h["from_agent"],
                    to_agent=h["to_agent"],
                    action=h["action"],
                    scope_at=h.get("scope_at", ""),
                    timestamp=h["timestamp"],
                    token_jti=h.get("token_jti", ""),
                ))
            return chain

    def list_chains_by_root(self, root_task_id: str) -> List[ProvenanceChain]:
        """List all chains originating from a root task."""
        chains: List[ProvenanceChain] = []
        with self._lock:
            result = self._engine.execute(
                "SELECT * FROM provenance_chains WHERE root_task_id=? ORDER BY created_at ASC",
                (root_task_id,),
            )
            for row in result.fetchall():
                chain = ProvenanceChain(
                    chain_id=row["chain_id"],
                    origin_agent=row["origin_agent"],
                    origin_tenant=row.get("origin_tenant", ""),
                    root_task_id=row["root_task_id"],
                    parent_task_id=row.get("parent_task_id"),
                    depth=row["depth"],
                )
                hop_result = self._engine.execute(
                    "SELECT * FROM provenance_hops WHERE chain_id=? ORDER BY timestamp ASC",
                    (chain.chain_id,),
                )
                for h in hop_result.fetchall():
                    chain.hops.append(ProvenanceHop(
                        from_agent=h["from_agent"],
                        to_agent=h["to_agent"],
                        action=h["action"],
                        scope_at=h.get("scope_at", ""),
                        timestamp=h["timestamp"],
                        token_jti=h.get("token_jti", ""),
                    ))
                chains.append(chain)
        return chains