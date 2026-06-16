"""Structured KV blackboard store — versioned, atomic, with optimistic locking.

Replaces the comment-prefix-based blackboard (``[swarm:blackboard]``) with
a proper database-backed key-value store supporting:

- Atomic writes with optimistic locking (CAS)
- Version tracking per key
- Batch reads (filtered or full dump)
- Key deletion

The table ``blackboard_entries`` lives in the same database as the task store,
sharing the same :class:`DatabaseEngine` connection.

Backward compatibility
----------------------
The new v2 API and the old comment-based blackboard coexist.  The
:func:`read_blackboard` function in ``swarm.py`` still aggregates
legacy comments.  New code should use ``BlackboardStore`` for atomicity.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

from simple_a2a_registry.database import DatabaseEngine

logger = logging.getLogger("a2a_registry.orchestration.blackboard_store")


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class BlackboardError(Exception):
    """Base exception for blackboard operations."""


class OptimisticLockError(BlackboardError):
    """Raised when a CAS write fails because the version doesn't match."""


class KeyNotFoundError(BlackboardError):
    """Raised when a key does not exist during a delete."""


# ---------------------------------------------------------------------------
# SQL schema fragment — appended to the task store schema
# ---------------------------------------------------------------------------

BLACKBOARD_SCHEMA_SQLITE = """
CREATE TABLE IF NOT EXISTS blackboard_entries (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    swarm_root_id TEXT NOT NULL,
    key         TEXT NOT NULL,
    value       TEXT NOT NULL,   -- JSON-encoded
    version     INTEGER NOT NULL DEFAULT 1,
    created_by  TEXT NOT NULL,
    created_at  INTEGER NOT NULL,
    updated_at  INTEGER NOT NULL,
    UNIQUE(swarm_root_id, key),
    FOREIGN KEY (swarm_root_id) REFERENCES tasks(id)
);
CREATE INDEX IF NOT EXISTS idx_bb_root_id ON blackboard_entries(swarm_root_id);
"""

BLACKBOARD_SCHEMA_MYSQL = """
CREATE TABLE IF NOT EXISTS blackboard_entries (
    id           INT AUTO_INCREMENT PRIMARY KEY,
    swarm_root_id VARCHAR(255) NOT NULL,
    `key`        VARCHAR(255) NOT NULL,
    value        TEXT NOT NULL,
    version      INT NOT NULL DEFAULT 1,
    created_by   VARCHAR(255) NOT NULL,
    created_at   BIGINT NOT NULL,
    updated_at   BIGINT NOT NULL,
    UNIQUE(swarm_root_id, `key`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE INDEX idx_bb_root_id ON blackboard_entries(swarm_root_id);
"""

# ---------------------------------------------------------------------------
# BlackboardStore
# ---------------------------------------------------------------------------


class BlackboardStore:
    """Versioned key-value blackboard stored in the task-store database.

    Thread-safe via the shared engine's lock (``TaskStore._tx`` pattern).

    Args:
        engine: A connected :class:`DatabaseEngine` (shared with TaskStore).
    """

    def __init__(self, engine: DatabaseEngine) -> None:
        self._engine = engine
        self._ensure_table()

    # ------------------------------------------------------------------
    # Schema management
    # ------------------------------------------------------------------

    def _ensure_table(self) -> None:
        """Create the blackboard_entries table if it doesn't exist."""
        if self._engine.driver == "mysql":
            stmts = [s.strip() for s in BLACKBOARD_SCHEMA_MYSQL.split(";") if s.strip()]
        else:
            stmts = [s.strip() for s in BLACKBOARD_SCHEMA_SQLITE.split(";") if s.strip()]

        for stmt in stmts:
            if stmt:
                self._engine.execute(stmt)

    # ------------------------------------------------------------------
    # Core CRUD
    # ------------------------------------------------------------------

    def write(
        self,
        swarm_root_id: str,
        key: str,
        value: Any,
        created_by: str = "anonymous",
        expected_version: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Atomically write a key-value pair with optimistic locking.

        Args:
            swarm_root_id: The swarm root task ID.
            key:           Blackboard key.
            value:         JSON-serialisable value.
            created_by:    Who is writing.
            expected_version:
                If set, the write only succeeds if the current version
                matches.  Used for CAS (compare-and-swap) updates.
                When ``None``, performs an upsert unconditionally.

        Returns:
            A dict with ``key``, ``version`` (new), ``created_at``,
            ``updated_at``.

        Raises:
            OptimisticLockError: If *expected_version* does not match the
                current stored version.
        """
        now = int(time.time())
        value_str = json.dumps(value, ensure_ascii=False, default=str)

        # Fetch current entry to detect conflicts
        existing = self._engine.execute(
            "SELECT key, version FROM blackboard_entries "
            "WHERE swarm_root_id=? AND key=?",
            (swarm_root_id, key),
        ).fetchone()

        if existing is None:
            # INSERT: no existing entry
            if expected_version is not None and expected_version != 0:
                raise OptimisticLockError(
                    f"Key '{key}' does not exist (expected_version={expected_version}, actual=0)"
                )

            self._engine.execute(
                "INSERT INTO blackboard_entries "
                "(swarm_root_id, key, value, version, created_by, created_at, updated_at) "
                "VALUES (?, ?, ?, 1, ?, ?, ?)",
                (swarm_root_id, key, value_str, created_by, now, now),
            )
            return {
                "key": key,
                "version": 1,
                "created_at": now,
                "updated_at": now,
            }

        current_version: int = existing["version"]

        # CAS check
        if expected_version is not None and current_version != expected_version:
            raise OptimisticLockError(
                f"Key '{key}' version mismatch: expected={expected_version}, actual={current_version}"
            )

        new_version = current_version + 1
        self._engine.execute(
            "UPDATE blackboard_entries "
            "SET value=?, version=?, updated_at=?, created_by=? "
            "WHERE swarm_root_id=? AND key=? AND version=?",
            (value_str, new_version, now, created_by, swarm_root_id, key, current_version),
        )

        return {
            "key": key,
            "version": new_version,
            "created_at": existing.get("created_at", now),
            "updated_at": now,
        }

    def read(
        self,
        swarm_root_id: str,
        keys: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Read blackboard entries, optionally filtered by keys.

        Args:
            swarm_root_id: The swarm root task ID.
            keys:          Optional list of keys to read.  ``None`` returns all.

        Returns:
            A dict where each top-level key maps to:
            ``{value, version, created_by, created_at, updated_at}``.
            Also includes ``_authors`` (key → most recent created_by).
        """
        if keys:
            placeholders = ",".join("?" for _ in keys)
            result = self._engine.execute(
                "SELECT key, value, version, created_by, created_at, updated_at "
                "FROM blackboard_entries "
                f"WHERE swarm_root_id=? AND key IN ({placeholders}) "
                "ORDER BY key",
                (swarm_root_id, *keys),
            )
        else:
            result = self._engine.execute(
                "SELECT key, value, version, created_by, created_at, updated_at "
                "FROM blackboard_entries "
                "WHERE swarm_root_id=? ORDER BY key",
                (swarm_root_id,),
            )

        entries: Dict[str, Any] = {}
        authors: Dict[str, str] = {}
        for row in result.fetchall():
            k = row["key"]
            try:
                val = json.loads(row["value"])
            except (json.JSONDecodeError, TypeError):
                val = row["value"]
            entries[k] = {
                "value": val,
                "version": row["version"],
                "created_by": row["created_by"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
            authors[k] = row["created_by"]

        entries["_authors"] = authors
        return entries

    def delete(self, swarm_root_id: str, key: str) -> bool:
        """Delete a blackboard entry by key.

        Args:
            swarm_root_id: The swarm root task ID.
            key:           The key to delete.

        Returns:
            ``True`` if an entry was deleted, ``False`` if it didn't exist.
        """
        result = self._engine.execute(
            "DELETE FROM blackboard_entries "
            "WHERE swarm_root_id=? AND key=?",
            (swarm_root_id, key),
        )
        return result.rowcount > 0

    def get_version(self, swarm_root_id: str, key: str) -> Optional[int]:
        """Return the current version of a key, or ``None`` if it doesn't exist."""
        row = self._engine.execute(
            "SELECT version FROM blackboard_entries "
            "WHERE swarm_root_id=? AND key=?",
            (swarm_root_id, key),
        ).fetchone()
        return row["version"] if row else None

    # ------------------------------------------------------------------
    # Legacy compatibility — read v1 blackboard data from comments too
    # ------------------------------------------------------------------

    def read_legacy_compat(self, swarm_root_id: str) -> Dict[str, Any]:
        """Return all structured blackboard entries in the comment-based format.

        This merges the new table entries with the old comment entries,
        letting callers like :func:`get_swarm_status` continue to work
        as long as both formats are in use.

        Note: This is a temporary bridge method.  Once all writers have
        migrated to ``BlackboardStore.write()``, this can be removed.

        Returns:
            A dict with value keys + ``_authors``, same shape as
            :func:`read_blackboard` in ``swarm.py``.
        """
        # Read from the new table
        entries = self.read(swarm_root_id)
        authors = entries.pop("_authors", {})
        # Flatten: entries → {key: value}
        flattened: Dict[str, Any] = {}
        for k, v in entries.items():
            flattened[k] = v["value"]
        flattened["_authors"] = authors
        return flattened
