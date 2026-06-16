"""Agent Memory Store — persistent key-value memory for agents.

Each agent has its own personal namespace, with shared and global scopes
for cross-agent collaboration.  TTL-based expiry, JSON values, and full
CRUD + prefix/namespace query support.

Backed by a :class:`DatabaseEngine` so it transparently supports
SQLite (dev) and MySQL (production).
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional

from simple_a2a_registry.database import DatabaseEngine

logger = logging.getLogger("a2a_registry.orchestration.memory")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MEMORY_TABLE_NAME = "agent_memory"

NAMESPACE_PERSONAL = "personal"  # only the owning agent can read
NAMESPACE_SHARED = "shared"      # visible within the same task
NAMESPACE_GLOBAL = "global"      # visible to all agents

VALID_NAMESPACES = {NAMESPACE_PERSONAL, NAMESPACE_SHARED, NAMESPACE_GLOBAL}

# ---------------------------------------------------------------------------
# Data record
# ---------------------------------------------------------------------------


@dataclass
class MemoryRecord:
    """A single key-value memory entry.

    Fields
    ------
    id:         Auto-increment primary key.
    agent_id:   The agent that owns this memory entry.
    key:        Unique key within (agent_id, namespace).
    value:      JSON-encoded value string.
    namespace:  Isolation scope — personal | shared | global.
    ttl:        Time-to-live in seconds (0 = no expiry).
    expires_at: Unix timestamp when this entry expires (None = never).
    created_at: Unix timestamp of creation.
    updated_at: Unix timestamp of last update.
    """

    id: int = 0
    agent_id: str = ""
    key: str = ""
    value: str = ""
    namespace: str = NAMESPACE_PERSONAL
    ttl: int = 0
    expires_at: Optional[float] = None
    created_at: float = 0.0
    updated_at: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        """Return a dict suitable for JSON serialisation (no internal id)."""
        d = asdict(self)
        d.pop("id", None)
        try:
            d["value"] = json.loads(d["value"])
        except (json.JSONDecodeError, TypeError):
            pass
        return d


# ---------------------------------------------------------------------------
# SQL Schema — SQLite
# ---------------------------------------------------------------------------

_MEMORY_SCHEMA_SQLITE = f"""\
CREATE TABLE IF NOT EXISTS {MEMORY_TABLE_NAME} (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id    TEXT NOT NULL,
    key         TEXT NOT NULL,
    value       TEXT NOT NULL DEFAULT '',
    namespace   TEXT NOT NULL DEFAULT '{NAMESPACE_PERSONAL}',
    ttl         INTEGER NOT NULL DEFAULT 0,
    expires_at  REAL,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_agent_key
    ON {MEMORY_TABLE_NAME}(agent_id, key, namespace);
CREATE INDEX IF NOT EXISTS idx_memory_agent
    ON {MEMORY_TABLE_NAME}(agent_id, namespace);
CREATE INDEX IF NOT EXISTS idx_memory_expires
    ON {MEMORY_TABLE_NAME}(expires_at);
CREATE INDEX IF NOT EXISTS idx_memory_namespace
    ON {MEMORY_TABLE_NAME}(namespace);
"""

# ---------------------------------------------------------------------------
# SQL Schema — MySQL
# ---------------------------------------------------------------------------

_MEMORY_SCHEMA_MYSQL = f"""\
CREATE TABLE IF NOT EXISTS {MEMORY_TABLE_NAME} (
    id          BIGINT AUTO_INCREMENT PRIMARY KEY,
    agent_id    VARCHAR(255) NOT NULL,
    `key`       VARCHAR(255) NOT NULL,
    value       TEXT NOT NULL,
    namespace   VARCHAR(50) NOT NULL DEFAULT '{NAMESPACE_PERSONAL}',
    ttl         INT NOT NULL DEFAULT 0,
    expires_at  DOUBLE,
    created_at  DOUBLE NOT NULL,
    updated_at  DOUBLE NOT NULL,
    UNIQUE KEY uk_memory_agent_key (agent_id, `key`, namespace),
    INDEX idx_memory_agent (agent_id, namespace),
    INDEX idx_memory_expires (expires_at),
    INDEX idx_memory_namespace (namespace)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""


def _maybe_create_memory_schema(engine: DatabaseEngine) -> None:
    """Create the agent_memory table if it does not exist.

    Idempotent — safe to call multiple times.
    """
    if engine.driver == "sqlite":
        engine.executescript(_MEMORY_SCHEMA_SQLITE)
        # Migration: add ttl column if missing (pre-TTL schema)
        try:
            engine.execute(
                f"ALTER TABLE {MEMORY_TABLE_NAME} ADD COLUMN ttl INTEGER NOT NULL DEFAULT 0"
            )
        except Exception:
            pass
        try:
            engine.execute(
                f"ALTER TABLE {MEMORY_TABLE_NAME} ADD COLUMN expires_at REAL"
            )
        except Exception:
            pass
        engine.commit()
    elif engine.driver == "mysql":
        for statement in _MEMORY_SCHEMA_MYSQL.split(";"):
            stripped = statement.strip()
            if not stripped:
                continue
            try:
                engine.execute(stripped)
            except Exception:
                pass  # ignore "already exists" errors
        engine.commit()
    logger.info("Memory schema ready")


# ---------------------------------------------------------------------------
# AgentMemoryStore
# ---------------------------------------------------------------------------


class AgentMemoryStore:
    """Persistent key-value memory store for agents.

    Thread-safe via ``threading.RLock``.  Namespace isolation:

    - ``personal`` — only the owning agent can read/write.
    - ``shared`` — visible to any agent within the same task/context.
    - ``global`` — visible to all agents.

    Entries with a TTL are automatically purged when expired.
    """

    def __init__(self, engine: DatabaseEngine) -> None:
        self._engine = engine
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def get(
        self,
        key: str,
        agent_id: str,
        namespace: str = NAMESPACE_PERSONAL,
    ) -> Optional[MemoryRecord]:
        """Retrieve a single memory entry by key.

        Returns ``None`` if the key does not exist or has expired.
        """
        now = time.time()
        with self._lock:
            result = self._engine.execute(
                f"SELECT id, agent_id, key, value, namespace, ttl, expires_at, "
                f"created_at, updated_at "
                f"FROM {MEMORY_TABLE_NAME} "
                f"WHERE agent_id=? AND key=? AND namespace=? "
                f"AND (expires_at IS NULL OR expires_at > ?)",
                (agent_id, key, namespace, now),
            )
            row = result.fetchone()
            if row is None:
                return None
            return MemoryRecord(**row)

    def set(
        self,
        key: str,
        value: Any,
        agent_id: str,
        *,
        ttl: int = 0,
        namespace: str = NAMESPACE_PERSONAL,
    ) -> MemoryRecord:
        """Create or update a memory entry.

        Args:
            key:       Memory key (unique within agent_id + namespace).
            value:     Any JSON-serialisable value.
            agent_id:  The owning agent.
            ttl:       Time-to-live in seconds (0 = never expires).
            namespace: Isolation scope.

        Returns:
            The newly written :class:`MemoryRecord`.
        """
        now = time.time()
        value_str = json.dumps(value, ensure_ascii=False, default=str)
        expires_at = (now + ttl) if ttl > 0 else None

        with self._lock:
            # Check if key already exists
            existing = self._engine.execute(
                f"SELECT id FROM {MEMORY_TABLE_NAME} "
                f"WHERE agent_id=? AND key=? AND namespace=?",
                (agent_id, key, namespace),
            ).fetchone()

            if existing:
                self._engine.execute(
                    f"UPDATE {MEMORY_TABLE_NAME} SET "
                    f"value=?, ttl=?, expires_at=?, updated_at=? "
                    f"WHERE agent_id=? AND key=? AND namespace=?",
                    (value_str, ttl, expires_at, now, agent_id, key, namespace),
                )
            else:
                self._engine.execute(
                    f"INSERT INTO {MEMORY_TABLE_NAME} "
                    f"(agent_id, key, value, namespace, ttl, expires_at, created_at, updated_at) "
                    f"VALUES (?,?,?,?,?,?,?,?)",
                    (agent_id, key, value_str, namespace, ttl, expires_at, now, now),
                )
            self._engine.commit()

        return self.get(key, agent_id, namespace)  # type: ignore[return-value]

    def delete(
        self,
        key: str,
        agent_id: str,
        namespace: str = NAMESPACE_PERSONAL,
    ) -> bool:
        """Delete a memory entry.

        Returns:
            True if a row was actually deleted.
        """
        with self._lock:
            result = self._engine.execute(
                f"DELETE FROM {MEMORY_TABLE_NAME} "
                f"WHERE agent_id=? AND key=? AND namespace=?",
                (agent_id, key, namespace),
            )
            self._engine.commit()
            return result.rowcount > 0

    def list_keys(
        self,
        agent_id: str,
        *,
        prefix: str = "",
        namespace: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[MemoryRecord]:
        """List memory entries for an agent, optionally filtered by prefix and namespace.

        Args:
            agent_id:  The owning agent.
            prefix:    Only return keys starting with this prefix.
            namespace: Filter by namespace (None = all namespaces).
            limit:     Max results (default 100, max 1000).
            offset:     Pagination offset.

        Returns:
            List of :class:`MemoryRecord`.
        """
        now = time.time()
        limit = min(max(limit, 1), 1000)
        offset = max(offset, 0)

        where_clauses: List[str] = ["agent_id = ?"]
        params: List[Any] = [agent_id]

        if prefix:
            where_clauses.append("key LIKE ?")
            params.append(f"{prefix}%")
        if namespace:
            where_clauses.append("namespace = ?")
            params.append(namespace)

        # Filter out expired entries
        where_clauses.append("(expires_at IS NULL OR expires_at > ?)")
        params.append(now)

        where_sql = " AND ".join(where_clauses)

        with self._lock:
            result = self._engine.execute(
                f"SELECT id, agent_id, key, value, namespace, ttl, expires_at, "
                f"created_at, updated_at "
                f"FROM {MEMORY_TABLE_NAME} "
                f"WHERE {where_sql} "
                f"ORDER BY key ASC "
                f"LIMIT ? OFFSET ?",
                tuple(params) + (limit, offset),
            )
            rows = result.fetchall()

        return [MemoryRecord(**r) for r in rows]

    def search(
        self,
        query: str,
        agent_id: str,
        *,
        namespace: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[MemoryRecord]:
        """Search memory entries by value content (substring match, case-insensitive).

        Args:
            query:     Substring to search for in the value field.
            agent_id:  The owning agent.
            namespace: Filter by namespace.
            limit:     Max results (default 100, max 1000).
            offset:     Pagination offset.

        Returns:
            List of matching :class:`MemoryRecord`.
        """
        now = time.time()
        limit = min(max(limit, 1), 1000)
        offset = max(offset, 0)

        where_clauses: List[str] = ["agent_id = ?", "LOWER(value) LIKE ?"]
        params: List[Any] = [agent_id, f"%{query.lower()}%"]

        if namespace:
            where_clauses.append("namespace = ?")
            params.append(namespace)

        where_clauses.append("(expires_at IS NULL OR expires_at > ?)")
        params.append(now)

        where_sql = " AND ".join(where_clauses)

        with self._lock:
            result = self._engine.execute(
                f"SELECT id, agent_id, key, value, namespace, ttl, expires_at, "
                f"created_at, updated_at "
                f"FROM {MEMORY_TABLE_NAME} "
                f"WHERE {where_sql} "
                f"ORDER BY updated_at DESC "
                f"LIMIT ? OFFSET ?",
                tuple(params) + (limit, offset),
            )
            rows = result.fetchall()

        return [MemoryRecord(**r) for r in rows]

    # ------------------------------------------------------------------
    # TTL expiry — purge expired entries
    # ------------------------------------------------------------------

    def purge_expired(self) -> int:
        """Delete all expired memory entries.

        Returns:
            Number of purged rows.
        """
        now = time.time()
        with self._lock:
            result = self._engine.execute(
                f"DELETE FROM {MEMORY_TABLE_NAME} "
                f"WHERE expires_at IS NOT NULL AND expires_at <= ?",
                (now,),
            )
            self._engine.commit()
            purged = result.rowcount
            if purged:
                logger.info("Purged %d expired memory entr(y/ies)", purged)
            return purged

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    def stats(self) -> Dict[str, Any]:
        """Return summary statistics about the memory store.

        Returns:
            Dict with total_entries, entries_by_namespace, entries_by_agent_limit.
        """
        with self._lock:
            total = self._engine.execute(
                f"SELECT COUNT(*) AS cnt FROM {MEMORY_TABLE_NAME}"
            ).fetchone()["cnt"]

            by_ns = self._engine.execute(
                f"SELECT namespace, COUNT(*) AS cnt "
                f"FROM {MEMORY_TABLE_NAME} GROUP BY namespace ORDER BY cnt DESC"
            ).fetchall()

            by_agent = self._engine.execute(
                f"SELECT agent_id, COUNT(*) AS cnt "
                f"FROM {MEMORY_TABLE_NAME} GROUP BY agent_id "
                f"ORDER BY cnt DESC LIMIT 10"
            ).fetchall()

        return {
            "total_entries": total,
            "by_namespace": {r["namespace"]: r["cnt"] for r in by_ns},
            "top_agents": {r["agent_id"]: r["cnt"] for r in by_agent},
        }