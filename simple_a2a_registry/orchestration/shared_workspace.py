"""Shared Workspace Manager — multi-agent collaboration with file-level locking.

Provides a workspace kind ("shared") that multiple agents can join to
cooperate on files in the same directory, with:

- **File-level locks**: agents acquire file-specific write locks with TTL
- **Change detection**: checksum-based file version tracking
- **Member management**: join/leave workspace
- **Admin WS notification**: workspace changes broadcast to Admin UI

Schema
------
``shared_workspaces``::
    id              TEXT PRIMARY KEY
    name            TEXT NOT NULL
    path            TEXT NOT NULL        — absolute path on filesystem
    tenant          TEXT NOT NULL DEFAULT ''
    member_agent_ids TEXT NOT NULL        — JSON array of agent IDs
    created_by      TEXT NOT NULL
    created_at      INTEGER NOT NULL

``workspace_files``::
    id              INTEGER PRIMARY KEY AUTOINCREMENT
    workspace_id    TEXT NOT NULL
    path            TEXT NOT NULL        — relative path within workspace
    locked_by       TEXT                 — agent ID holding the lock
    lock_expires    INTEGER              — unix ts when lock auto-releases
    checksum        TEXT                 — SHA-256 of file content
    version         INTEGER NOT NULL DEFAULT 1
    updated_at      INTEGER NOT NULL
    FOREIGN KEY (workspace_id) REFERENCES shared_workspaces(id)

.. note::
    No nested / recursive locking. Locks are file-level and never nest.
    A lock is released explicitly or via TTL expiry.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from simple_a2a_registry.database import DatabaseEngine, SQLiteEngine

logger = logging.getLogger("a2a_registry.orchestration.shared_workspace")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_LOCK_TTL = 300  # 5 minutes
MAX_FILE_SIZE_BYTES = 100 * 1024 * 1024  # 100 MB

_SHARED_WS_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS shared_workspaces (
    id               TEXT PRIMARY KEY,
    name             TEXT NOT NULL,
    path             TEXT NOT NULL,
    tenant           TEXT NOT NULL DEFAULT '',
    member_agent_ids TEXT NOT NULL DEFAULT '[]',
    created_by       TEXT NOT NULL,
    created_at       INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS workspace_files (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id    TEXT NOT NULL,
    path            TEXT NOT NULL,
    locked_by       TEXT,
    lock_expires    INTEGER,
    checksum        TEXT,
    version         INTEGER NOT NULL DEFAULT 1,
    updated_at      INTEGER NOT NULL,
    FOREIGN KEY (workspace_id) REFERENCES shared_workspaces(id)
);

CREATE INDEX IF NOT EXISTS idx_ws_files_workspace ON workspace_files(workspace_id, path);
CREATE INDEX IF NOT EXISTS idx_ws_files_locked ON workspace_files(workspace_id, locked_by);
"""


# ---------------------------------------------------------------------------
# Facade helpers (synchronous DB, wrapped in threads)
# ---------------------------------------------------------------------------

_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="shared-ws")

def _run_sync(fn, *args, **kwargs):
    """Run a synchronous function in a thread and return the result."""
    import asyncio
    loop = asyncio.get_event_loop()
    return loop.run_in_executor(_EXECUTOR, lambda: fn(*args, **kwargs))


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class SharedWorkspaceError(RuntimeError):
    """Base exception for shared workspace operations."""


class WorkspaceNotFoundError(SharedWorkspaceError):
    """Raised when the workspace does not exist."""


class LockError(SharedWorkspaceError):
    """Raised when a file lock cannot be acquired."""


class MemberError(SharedWorkspaceError):
    """Raised on join/leave violations."""


# ---------------------------------------------------------------------------
# Shared Workspace Manager
# ---------------------------------------------------------------------------


class SharedWorkspaceManager:
    """Manages multi-agent shared workspaces with file-level locking.

    This manager is independent of the :class:`TaskStore` — it uses its own
    database schema (``shared_workspaces`` / ``workspace_files`` tables).

    Args:
        engine:
            A :class:`DatabaseEngine` instance.  When *None*, the manager
            creates a temporary SQLite engine (testing / lightweight use).
        workspaces_root:
            Root directory under which shared workspace directories are
            created.  When *None*, defaults to ``<current-dir>/shared-workspaces``.
        lock_ttl:
            Default TTL for file locks in seconds (default 300 / 5 min).
        broadcast_fn:
            Optional async callable ``(event_type: str, data: dict)`` for
            broadcasting workspace updates to Admin WebSocket clients.
    """

    def __init__(
        self,
        engine: Optional[DatabaseEngine] = None,
        workspaces_root: Optional[str] = None,
        lock_ttl: int = DEFAULT_LOCK_TTL,
        broadcast_fn: Optional[Callable] = None,
    ) -> None:
        # Database
        if engine is not None:
            self._engine = engine
        else:
            self._engine = SQLiteEngine(":memory:")
            self._engine.connect()
        self._ensure_schema()

        # Filesystem root
        if workspaces_root is None:
            workspaces_root = str(Path.cwd() / "shared-workspaces")
        self._workspaces_root = Path(workspaces_root).expanduser().resolve()
        self._workspaces_root.mkdir(parents=True, exist_ok=True)

        self._lock_ttl = lock_ttl
        self._broadcast_fn = broadcast_fn

        logger.info(
            "SharedWorkspaceManager initialised (root=%s, lock_ttl=%ds)",
            self._workspaces_root, lock_ttl,
        )

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def _ensure_schema(self) -> None:
        """Create shared workspace tables if they don't exist."""
        for stmt in _SHARED_WS_SCHEMA_SQL.split(";"):
            stripped = stmt.strip()
            if not stripped:
                continue
            try:
                self._engine.execute(stripped)
            except Exception:
                logger.warning("Schema statement failed (may already exist): %s", stripped[:80])
        self._engine.commit()

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _now(self) -> int:
        return int(time.time())

    def _generate_id(self) -> str:
        return "ws_" + uuid.uuid4().hex[:8]

    def _path_alias(self, path: str) -> str:
        """Resolve and canonicalise a path relative to the workspace root."""
        p = Path(path)
        if not p.is_absolute():
            p = self._workspaces_root / p
        return str(p.resolve())

    def _compute_checksum(self, filepath: str) -> Optional[str]:
        """Compute SHA-256 checksum of a file.  Returns None if the file
        does not exist or is too large."""
        try:
            size = os.path.getsize(filepath)
        except OSError:
            return None
        if size > MAX_FILE_SIZE_BYTES:
            logger.warning("File too large for checksum (%d bytes): %s", size, filepath)
            return None
        try:
            h = hashlib.sha256()
            with open(filepath, "rb") as f:
                while True:
                    chunk = f.read(65536)
                    if not chunk:
                        break
                    h.update(chunk)
            return h.hexdigest()
        except OSError:
            return None

    def _row_to_dict(self, row) -> dict:
        """Convert a sqlite3.Row to a plain dict."""
        if row is None:
            return {}
        return dict(row)

    # ------------------------------------------------------------------
    # Workspace CRUD
    # ------------------------------------------------------------------

    def create(
        self,
        name: str,
        *,
        created_by: str = "anonymous",
        tenant: str = "",
        members: Optional[List[str]] = None,
        path: Optional[str] = None,
    ) -> dict:
        """Create a new shared workspace.

        Args:
            name: Human-friendly workspace name.
            created_by: Agent ID of the creator.
            tenant: Tenant namespace (for isolation).
            members: Initial member agent IDs.  The creator is always
                included automatically.
            path: Explicit filesystem path.  When omitted, a directory
                under ``workspaces_root/<workspace_id>`` is created.

        Returns:
            The workspace record as a dict.
        """
        ws_id = self._generate_id()

        # Resolve path
        if path:
            ws_path = self._path_alias(path)
        else:
            ws_path = str(self._workspaces_root / ws_id)

        # Ensure directory exists
        Path(ws_path).mkdir(parents=True, exist_ok=True)

        # Build member list (creator is always included)
        agent_ids = set(members or [])
        agent_ids.add(created_by)
        member_json = json.dumps(sorted(agent_ids))

        now = self._now()
        self._engine.execute(
            "INSERT INTO shared_workspaces "
            "(id, name, path, tenant, member_agent_ids, created_by, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (ws_id, name, ws_path, tenant, member_json, created_by, now),
        )
        self._engine.commit()

        record = self.get(ws_id) or {}
        logger.info("Shared workspace '%s' (%s) created by %s", name, ws_id, created_by)
        self._broadcast("workspace_created", record)
        return record

    def get(self, workspace_id: str) -> Optional[dict]:
        """Get a workspace by id.  Returns None if not found."""
        result = self._engine.execute(
            "SELECT * FROM shared_workspaces WHERE id=?",
            (workspace_id,),
        )
        row = result.fetchone()
        if row is None:
            return None
        record = self._row_to_dict(row)
        # Parse member_agent_ids
        try:
            record["member_agent_ids"] = json.loads(record.get("member_agent_ids", "[]"))
        except (json.JSONDecodeError, TypeError):
            record["member_agent_ids"] = []
        return record

    def list_accessible(
        self,
        agent_id: Optional[str] = None,
        tenant: Optional[str] = None,
    ) -> List[dict]:
        """List workspaces the agent has access to.

        Args:
            agent_id: When set, only returns workspaces the agent is a member of.
            tenant: Optional tenant filter.

        Returns:
            List of workspace record dicts.
        """
        if agent_id and tenant:
            result = self._engine.execute(
                "SELECT * FROM shared_workspaces WHERE tenant=? AND member_agent_ids LIKE ?",
                (tenant, f"%{agent_id}%"),
            )
        elif agent_id:
            result = self._engine.execute(
                "SELECT * FROM shared_workspaces WHERE member_agent_ids LIKE ?",
                (f"%{agent_id}%",),
            )
        elif tenant:
            result = self._engine.execute(
                "SELECT * FROM shared_workspaces WHERE tenant=?",
                (tenant,),
            )
        else:
            result = self._engine.execute("SELECT * FROM shared_workspaces")

        records = []
        for row in result.fetchall():
            record = self._row_to_dict(row)
            try:
                record["member_agent_ids"] = json.loads(record.get("member_agent_ids", "[]"))
            except (json.JSONDecodeError, TypeError):
                record["member_agent_ids"] = []
            records.append(record)
        return records

    def delete(self, workspace_id: str, caller: str = "") -> bool:
        """Delete a shared workspace (owner only).

        Args:
            workspace_id: The workspace to delete.
            caller: Agent requesting deletion.  Must be the creator.

        Returns:
            True if deleted, False if not found.
        """
        ws = self.get(workspace_id)
        if ws is None:
            return False
        if caller and ws.get("created_by") and ws["created_by"] != caller:
            raise SharedWorkspaceError(
                f"Agent '{caller}' is not the creator of workspace '{workspace_id}'"
            )

        # Remove all file records
        self._engine.execute(
            "DELETE FROM workspace_files WHERE workspace_id=?",
            (workspace_id,),
        )
        del_result = self._engine.execute(
            "DELETE FROM shared_workspaces WHERE id=?",
            (workspace_id,),
        )
        self._engine.commit()
        logger.info("Shared workspace '%s' deleted by %s", workspace_id, caller or "unknown")
        self._broadcast("workspace_deleted", {"id": workspace_id})
        return del_result.rowcount > 0

    # ------------------------------------------------------------------
    # Membership
    # ------------------------------------------------------------------

    def join(self, workspace_id: str, agent_id: str) -> dict:
        """Add an agent to a shared workspace.

        Args:
            workspace_id: The workspace to join.
            agent_id: Agent ID to add.

        Returns:
            Updated workspace record.
        """
        ws = self.get(workspace_id)
        if ws is None:
            raise WorkspaceNotFoundError(f"Workspace '{workspace_id}' not found")

        members: set = set(ws.get("member_agent_ids", []))
        if agent_id in members:
            return ws  # already a member, idempotent

        members.add(agent_id)
        member_json = json.dumps(sorted(members))
        self._engine.execute(
            "UPDATE shared_workspaces SET member_agent_ids=? WHERE id=?",
            (member_json, workspace_id),
        )
        self._engine.commit()

        updated = self.get(workspace_id) or {}
        logger.info("Agent '%s' joined shared workspace '%s'", agent_id, workspace_id)
        self._broadcast("workspace_updated", updated)
        return updated

    def leave(self, workspace_id: str, agent_id: str) -> dict:
        """Remove an agent from a shared workspace.

        Args:
            workspace_id: The workspace to leave.
            agent_id: Agent ID to remove.

        Returns:
            Updated workspace record.
        """
        ws = self.get(workspace_id)
        if ws is None:
            raise WorkspaceNotFoundError(f"Workspace '{workspace_id}' not found")

        members: set = set(ws.get("member_agent_ids", []))
        if agent_id not in members:
            return ws  # not a member, idempotent

        members.discard(agent_id)
        member_json = json.dumps(sorted(members))
        self._engine.execute(
            "UPDATE shared_workspaces SET member_agent_ids=? WHERE id=?",
            (member_json, workspace_id),
        )
        # Release any locks held by this agent
        now = self._now()
        self._engine.execute(
            "UPDATE workspace_files SET locked_by=NULL, lock_expires=NULL "
            "WHERE workspace_id=? AND locked_by=? AND (lock_expires IS NULL OR lock_expires > ?)",
            (workspace_id, agent_id, now),
        )
        self._engine.commit()

        updated = self.get(workspace_id) or {}
        logger.info("Agent '%s' left shared workspace '%s'", agent_id, workspace_id)
        self._broadcast("workspace_updated", updated)
        return updated

    # ------------------------------------------------------------------
    # File lock / unlock
    # ------------------------------------------------------------------

    def lock(
        self,
        workspace_id: str,
        file_path: str,
        agent_id: str,
        ttl: Optional[int] = None,
    ) -> dict:
        """Acquire an exclusive file-level lock.

        Thread-safe via DB transactions.  Locks are file-level — no nesting
        or recursion.  A TTL ensures the lock auto-releases if the agent
        crashes.

        Args:
            workspace_id: The shared workspace.
            file_path: Relative path of the file within the workspace.
            agent_id: Agent requesting the lock.
            ttl: Lock TTL in seconds (defaults to ``self._lock_ttl``).

        Returns:
            The ``workspace_files`` record dict.

        Raises:
            WorkspaceNotFoundError: If the workspace does not exist.
            LockError: If the file is already locked by another agent.
        """
        ws = self.get(workspace_id)
        if ws is None:
            raise WorkspaceNotFoundError(f"Workspace '{workspace_id}' not found")

        if agent_id not in ws.get("member_agent_ids", []):
            raise MemberError(f"Agent '{agent_id}' is not a member of workspace '{workspace_id}'")

        # Normalise path relative to workspace root
        ws_path = Path(ws["path"])
        abs_file_path = ws_path / file_path
        rel_path = str(abs_file_path.resolve().relative_to(ws_path.resolve()))
        ttl = ttl if ttl is not None else self._lock_ttl
        now = self._now()
        expires = now + ttl

        # Check existing lock
        result = self._engine.execute(
            "SELECT * FROM workspace_files WHERE workspace_id=? AND path=?",
            (workspace_id, rel_path),
        )
        existing = result.fetchone()

        if existing:
            existing_dict = self._row_to_dict(existing)
            # Check if lock is still valid and held by another agent
            if (existing_dict.get("locked_by")
                    and existing_dict["locked_by"] != agent_id
                    and (existing_dict.get("lock_expires") or 0) > now):
                raise LockError(
                    f"File '{rel_path}' is already locked by "
                    f"agent '{existing_dict['locked_by']}' until "
                    f"{existing_dict['lock_expires']}"
                )
            # Lock is expired or we're re-locking
            self._engine.execute(
                "UPDATE workspace_files SET locked_by=?, lock_expires=?, version=version+1 WHERE id=?",
                (agent_id, expires, existing_dict["id"]),
            )
        else:
            # Insert new file tracking record
            checksum = self._compute_checksum(str(abs_file_path))
            self._engine.execute(
                "INSERT INTO workspace_files "
                "(workspace_id, path, locked_by, lock_expires, checksum, version, updated_at) "
                "VALUES (?, ?, ?, ?, ?, 1, ?)",
                (workspace_id, rel_path, agent_id, expires, checksum, now),
            )

        self._engine.commit()

        # Fetch final record
        result = self._engine.execute(
            "SELECT * FROM workspace_files WHERE workspace_id=? AND path=?",
            (workspace_id, rel_path),
        )
        record = self._row_to_dict(result.fetchone())
        logger.info(
            "Agent '%s' locked '%s' in workspace '%s' (ttl=%ds)",
            agent_id, rel_path, workspace_id, ttl,
        )
        self._broadcast("file_locked", {
            "workspace_id": workspace_id, "path": rel_path, "agent_id": agent_id,
        })
        return record

    def unlock(
        self,
        workspace_id: str,
        file_path: str,
        agent_id: str,
    ) -> Optional[dict]:
        """Release a file lock.

        Args:
            workspace_id: The shared workspace.
            file_path: Relative path of the file.
            agent_id: Agent releasing the lock.

        Returns:
            The updated ``workspace_files`` record, or None if not found.

        Raises:
            LockError: If the file is locked by a different agent.
        """
        ws = self.get(workspace_id)
        if ws is None:
            raise WorkspaceNotFoundError(f"Workspace '{workspace_id}' not found")

        ws_path = Path(ws["path"])
        abs_file_path = ws_path / file_path
        rel_path = str(abs_file_path.resolve().relative_to(ws_path.resolve()))

        result = self._engine.execute(
            "SELECT * FROM workspace_files WHERE workspace_id=? AND path=?",
            (workspace_id, rel_path),
        )
        existing = result.fetchone()
        if existing is None:
            return None

        existing_dict = self._row_to_dict(existing)
        if existing_dict.get("locked_by") and existing_dict["locked_by"] != agent_id:
            raise LockError(
                f"File '{rel_path}' is locked by agent "
                f"'{existing_dict['locked_by']}', not '{agent_id}'"
            )

        # Compute new checksum before releasing lock (capture any writes)
        new_checksum = self._compute_checksum(str(abs_file_path))

        now = self._now()
        self._engine.execute(
            "UPDATE workspace_files SET locked_by=NULL, lock_expires=NULL, "
            "checksum=?, version=version+1, updated_at=? WHERE id=?",
            (new_checksum, now, existing_dict["id"]),
        )
        self._engine.commit()

        result = self._engine.execute(
            "SELECT * FROM workspace_files WHERE id=?",
            (existing_dict["id"],),
        )
        record = self._row_to_dict(result.fetchone())
        logger.info("Agent '%s' unlocked '%s' in workspace '%s'", agent_id, rel_path, workspace_id)
        self._broadcast("file_unlocked", {
            "workspace_id": workspace_id, "path": rel_path, "agent_id": agent_id,
        })
        return record

    # ------------------------------------------------------------------
    # File tracking
    # ------------------------------------------------------------------

    def list_files(
        self,
        workspace_id: str,
    ) -> List[dict]:
        """List tracked files in a workspace with lock status.

        Args:
            workspace_id: The shared workspace.

        Returns:
            List of file record dicts.
        """
        result = self._engine.execute(
            "SELECT * FROM workspace_files WHERE workspace_id=? ORDER BY path",
            (workspace_id,),
        )
        return [self._row_to_dict(r) for r in result.fetchall()]

    def get_file_status(
        self,
        workspace_id: str,
        file_path: str,
    ) -> Optional[dict]:
        """Get the lock and checksum status of a single file."""
        ws = self.get(workspace_id)
        if ws is None:
            return None

        ws_path = Path(ws["path"])
        abs_file_path = ws_path / file_path
        rel_path = str(abs_file_path.resolve().relative_to(ws_path.resolve()))

        result = self._engine.execute(
            "SELECT * FROM workspace_files WHERE workspace_id=? AND path=?",
            (workspace_id, rel_path),
        )
        row = result.fetchone()
        if row is None:
            return None
        return self._row_to_dict(row)

    # ------------------------------------------------------------------
    # Stale lock reaper
    # ------------------------------------------------------------------

    def reap_stale_locks(self, max_age: Optional[int] = None) -> int:
        """Release all locks that have expired.

        Args:
            max_age: Override TTL (defaults to ``self._lock_ttl``).

        Returns:
            Number of locks released.
        """
        if max_age is None:
            max_age = self._lock_ttl
        cutoff = self._now() - max_age
        result = self._engine.execute(
            "UPDATE workspace_files SET locked_by=NULL, lock_expires=NULL "
            "WHERE lock_expires IS NOT NULL AND lock_expires < ?",
            (cutoff,),
        )
        self._engine.commit()
        count = result.rowcount if hasattr(result, "rowcount") else 0
        if count:
            logger.info("Reaped %d stale lock(s) (cutoff=%d)", count, cutoff)
        return count

    # ------------------------------------------------------------------
    # Broadcasting
    # ------------------------------------------------------------------

    def _broadcast(self, event_type: str, data: dict) -> None:
        """Fire-and-forget broadcast via the registered callback."""
        if self._broadcast_fn is None:
            return
        try:
            import asyncio
            asyncio.ensure_future(self._broadcast_fn(event_type, data))
        except Exception:
            logger.debug("Failed to schedule broadcast: %s/%s", event_type, data.get("id", ""))
