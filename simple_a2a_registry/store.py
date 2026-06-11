"""Unified persistence — registry store + OAuth auth store.

Merges ``A2ARegistryStore`` (agent registration with heartbeat) and
``AuthStore`` (OAuth client/token management) into one database.
Thread-safe via ``threading.RLock``.

Backed by a :class:`DatabaseEngine` so it transparently supports
SQLite (dev) and MySQL (production).
"""
from __future__ import annotations

import hashlib
import json
import logging
import secrets
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional

from simple_a2a_registry.database import DatabaseEngine, CursorResult, SQLiteEngine, RetryEngine
from simple_a2a_registry.models import AgentCard

logger = logging.getLogger("a2a_registry.store")

# ---------------------------------------------------------------------------
# Registry constants
# ---------------------------------------------------------------------------

HEARTBEAT_TIMEOUT = 120   # seconds before an agent is considered stale
HEARTBEAT_PURGE = 300     # seconds before a stale agent is fully removed

# ---------------------------------------------------------------------------
# Auth constants (also used by auth.py — kept here for import convenience)
# ---------------------------------------------------------------------------

SCOPES: Dict[str, str] = {
    "task:read": "Read task list and details",
    "task:write": "Create and modify tasks",
    "agent:read": "Read agent list and details",
    "agent:register": "Register new agents",
    "agent:admin": "Manage agents (delete/disable)",
    "registry:admin": "Registry administration operations",
}

AUTH_CODE_EXPIRY_SECONDS = 600  # 10 minutes for authorization codes

# ---------------------------------------------------------------------------
# Dataclass records
# ---------------------------------------------------------------------------


@dataclass
class ClientRecord:
    """A registered OAuth 2.1 client (agent)."""
    client_id: str
    client_secret_hash: str
    allowed_scopes: List[str] = field(default_factory=lambda: list(SCOPES.keys()))
    agent_card_id: str = ""
    created_at: float = 0.0
    description: str = ""
    tenant: str = ""


@dataclass
class TokenRecord:
    """An issued access token record for auditing / revocation."""
    jti: str
    client_id: str
    scope: str
    expires_at: float


@dataclass
class AuthorizationRecord:
    """An agent-to-agent authorization record."""
    id: int
    source_agent_id: str
    target_agent_id: str
    allowed_actions: List[str]
    scope_restriction: Optional[str] = None
    max_depth: int = 5
    tenant_id: str = ""
    created_at: float = 0.0
    expires_at: Optional[float] = None


# ---------------------------------------------------------------------------
# SQL schema — SQLite version
# ---------------------------------------------------------------------------

_SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA busy_timeout=5000;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS agents (
    id              TEXT PRIMARY KEY,
    card_json       TEXT NOT NULL,
    heartbeat_at    REAL NOT NULL DEFAULT 0,
    disabled        INTEGER NOT NULL DEFAULT 0,
    registered_at   TEXT NOT NULL,
    created_at      REAL NOT NULL,
    tenant_id       TEXT NOT NULL DEFAULT '',
    preferred_channel TEXT NOT NULL DEFAULT 'ws',
    callback_url    TEXT NOT NULL DEFAULT '',
    callback_token  TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS oauth_clients (
    client_id           TEXT PRIMARY KEY,
    client_secret_hash  TEXT NOT NULL,
    allowed_scopes      TEXT NOT NULL,
    agent_card_id       TEXT NOT NULL DEFAULT '',
    created_at          REAL NOT NULL,
    description         TEXT NOT NULL DEFAULT '',
    tenant_id           TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS oauth_tokens (
    jti         TEXT PRIMARY KEY,
    client_id   TEXT NOT NULL,
    scope       TEXT NOT NULL,
    expires_at  REAL NOT NULL,
    tenant_id   TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS auth_codes (
    code                    TEXT PRIMARY KEY,
    client_id               TEXT NOT NULL,
    code_challenge          TEXT NOT NULL,
    code_challenge_method   TEXT NOT NULL,
    redirect_uri            TEXT NOT NULL,
    scope                   TEXT NOT NULL,
    created_at              REAL NOT NULL,
    tenant_id               TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_oauth_tokens_client_id ON oauth_tokens(client_id);
CREATE INDEX IF NOT EXISTS idx_agents_heartbeat ON agents(heartbeat_at);
CREATE INDEX IF NOT EXISTS idx_agents_channel ON agents(preferred_channel);

CREATE TABLE IF NOT EXISTS agent_authorizations (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    source_agent_id     TEXT NOT NULL,
    target_agent_id     TEXT NOT NULL,
    allowed_actions     TEXT NOT NULL DEFAULT '["*"]',
    scope_restriction   TEXT,
    max_depth           INTEGER NOT NULL DEFAULT 5,
    tenant_id           TEXT NOT NULL DEFAULT '',
    created_at          REAL NOT NULL,
    expires_at          REAL,
    UNIQUE(source_agent_id, target_agent_id, tenant_id)
);

CREATE INDEX IF NOT EXISTS idx_authz_source ON agent_authorizations(source_agent_id);
CREATE INDEX IF NOT EXISTS idx_authz_target ON agent_authorizations(target_agent_id);
"""

# ---------------------------------------------------------------------------
# SQL schema — MySQL version
# ---------------------------------------------------------------------------

_SCHEMA_SQL_MYSQL = """
CREATE TABLE IF NOT EXISTS agents (
    id              VARCHAR(255) PRIMARY KEY,
    card_json       LONGTEXT NOT NULL,
    heartbeat_at    DOUBLE NOT NULL DEFAULT 0,
    disabled        TINYINT(1) NOT NULL DEFAULT 0,
    registered_at   VARCHAR(255) NOT NULL,
    created_at      DOUBLE NOT NULL,
    tenant_id       VARCHAR(255) NOT NULL DEFAULT '',
    preferred_channel VARCHAR(20) NOT NULL DEFAULT 'ws',
    callback_url    TEXT NOT NULL DEFAULT '',
    callback_token  TEXT NOT NULL DEFAULT ''
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS oauth_clients (
    client_id           VARCHAR(255) PRIMARY KEY,
    client_secret_hash  VARCHAR(255) NOT NULL,
    allowed_scopes      TEXT NOT NULL,
    agent_card_id       VARCHAR(255) NOT NULL DEFAULT '',
    created_at          DOUBLE NOT NULL,
    description         TEXT NOT NULL,
    tenant_id           VARCHAR(255) NOT NULL DEFAULT ''
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS oauth_tokens (
    jti         VARCHAR(255) PRIMARY KEY,
    client_id   VARCHAR(255) NOT NULL,
    scope       TEXT NOT NULL,
    expires_at  DOUBLE NOT NULL,
    tenant_id   VARCHAR(255) NOT NULL DEFAULT ''
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS auth_codes (
    code                    VARCHAR(255) PRIMARY KEY,
    client_id               VARCHAR(255) NOT NULL,
    code_challenge          VARCHAR(255) NOT NULL,
    code_challenge_method   VARCHAR(255) NOT NULL,
    redirect_uri            TEXT NOT NULL,
    scope                   TEXT NOT NULL,
    created_at              DOUBLE NOT NULL,
    tenant_id               VARCHAR(255) NOT NULL DEFAULT ''
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE INDEX idx_oauth_tokens_client_id ON oauth_tokens(client_id);
CREATE INDEX idx_agents_heartbeat ON agents(heartbeat_at);
CREATE INDEX idx_agents_tenant ON agents(tenant_id);

CREATE TABLE IF NOT EXISTS agent_authorizations (
    id                  INT AUTO_INCREMENT PRIMARY KEY,
    source_agent_id     VARCHAR(255) NOT NULL,
    target_agent_id     VARCHAR(255) NOT NULL,
    allowed_actions     TEXT NOT NULL DEFAULT '["*"]',
    scope_restriction   TEXT,
    max_depth           INT NOT NULL DEFAULT 5,
    tenant_id           VARCHAR(255) NOT NULL DEFAULT '',
    created_at          DOUBLE NOT NULL,
    expires_at          DOUBLE,
    UNIQUE KEY uk_authz_pair (source_agent_id, target_agent_id, tenant_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE INDEX idx_authz_source ON agent_authorizations(source_agent_id);
CREATE INDEX idx_authz_target ON agent_authorizations(target_agent_id);
"""

# ---------------------------------------------------------------------------
# Helper — execute SQLite schema (ignored for MySQL)
# ---------------------------------------------------------------------------


def _maybe_create_schema(engine: DatabaseEngine) -> None:
    """Create schema on first connect (SQLite or MySQL)."""
    if engine.driver == "sqlite":
        engine.executescript(_SCHEMA_SQL)
        engine.commit()
        # Migrate existing databases — add disabled column if missing
        try:
            engine.execute(
                "ALTER TABLE agents ADD COLUMN disabled INTEGER NOT NULL DEFAULT 0"
            )
            engine.commit()
        except Exception:
            pass  # column already exists
        # Migrate existing databases — add tenant_id to all tables
        for tbl in ("agents", "oauth_clients", "oauth_tokens", "auth_codes"):
            try:
                engine.execute(
                    f"ALTER TABLE {tbl} ADD COLUMN tenant_id TEXT NOT NULL DEFAULT ''"
                )
                engine.commit()
            except Exception:
                pass  # column already exists
        # P3.1: migrate existing databases — add callback columns
        for col in ("preferred_channel", "callback_url", "callback_token"):
            try:
                engine.execute(
                    f"ALTER TABLE agents ADD COLUMN {col} TEXT NOT NULL DEFAULT ''"
                )
                engine.commit()
            except Exception:
                pass  # column already exists
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
        # Migrate existing databases — add disabled column if missing
        try:
            engine.execute("ALTER TABLE agents ADD COLUMN disabled TINYINT(1) NOT NULL DEFAULT 0")
            engine.commit()
        except Exception:
            pass  # column already exists
        # Migrate existing databases — add tenant_id to all tables
        for tbl in ("agents", "oauth_clients", "oauth_tokens", "auth_codes"):
            try:
                engine.execute(
                    f"ALTER TABLE {tbl} ADD COLUMN tenant_id VARCHAR(255) NOT NULL DEFAULT ''"
                )
                engine.commit()
            except Exception:
                pass  # column already exists
        # P3.1: migrate — add callback columns
        for col in ("preferred_channel", "callback_url", "callback_token"):
            try:
                engine.execute(f"ALTER TABLE agents ADD COLUMN {col} TEXT NOT NULL DEFAULT ''")
                engine.commit()
            except Exception:
                pass  # column already exists
        try:
            engine.execute("ALTER TABLE agents MODIFY COLUMN preferred_channel VARCHAR(20) NOT NULL DEFAULT 'ws'")
            engine.commit()
        except Exception:
            pass
        try:
            engine.execute("CREATE INDEX idx_agents_channel ON agents(preferred_channel)")
            engine.commit()
        except Exception:
            pass


# ======================================================================
# Unified Store
# ======================================================================


class Store:
    """Unified persistence layer combining registry and auth stores.

    Thread-safe via ``threading.RLock``.
    """

    def __init__(
        self,
        data_dir_or_engine: str | DatabaseEngine,
        bootstrap_secret: Optional[str] = None,
    ) -> None:
        """Initialise the store.

        Two calling conventions:
        1. Legacy: ``Store(\"~/.simple-a2a-registry\", secret=...)``
           — creates a :class:`SQLiteEngine` internally.
        2. New:    ``Store(my_engine, secret=...)``
           — uses the pre-configured engine (SQLite or MySQL).

        This maintains backward compatibility with existing tests and the
        ``create_app()`` call path that passes ``data_dir``.
        """
        self._lock = threading.RLock()
        self._bootstrap_secret = bootstrap_secret

        if isinstance(data_dir_or_engine, str):
            # :memory: special case — SQLite in-memory database
            if data_dir_or_engine == ":memory:":
                engine = SQLiteEngine(":memory:")
                engine.connect()
                _maybe_create_schema(engine)
                self._engine = RetryEngine(engine)
                self._data_dir = ""
            else:
                # Legacy path: create SQLiteEngine automatically
                resolved = Path(data_dir_or_engine).expanduser().resolve()
                resolved.mkdir(parents=True, exist_ok=True)
                db_path = str(resolved / "registry.db")
                engine = SQLiteEngine(db_path)
                engine.connect()
                _maybe_create_schema(engine)
                self._engine = RetryEngine(engine)
                self._data_dir = str(resolved)
        else:
            self._engine = RetryEngine(data_dir_or_engine)
            self._data_dir = ""
            # Ensure schema when using a pre-configured engine
            _maybe_create_schema(data_dir_or_engine)

        self._maybe_migrate_from_json()
        self._bootstrap_registry()

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

    @staticmethod
    def _is_transient_error(e: Exception) -> bool:
        """Check if an exception is a transient DB error worth retrying."""
        exc_name = type(e).__module__ + "." + type(e).__name__
        return any(
            kw in exc_name.lower() or kw in str(e).lower()
            for kw in ["operationalerror", "databaselocked", "timeout", "lock"]
        )

    def _retry_operation(
        self,
        operation_name: str,
        fn,
        max_retries: int = 3,
        base_delay: float = 0.1,
    ):
        """Execute *fn* with exponential-backoff retry for transient DB failures.

        Retries on ``sqlite3.OperationalError`` or ``pymysql.err.OperationalError``
        (e.g. database locked, connection lost).  Non-transient exceptions are
        re-raised immediately.

        Args:
            operation_name: Human-readable label for logging.
            fn:             Zero-argument callable to retry.
            max_retries:    Maximum retry attempts (default 3).
            base_delay:     Initial delay in seconds (default 0.1).

        Returns:
            The return value of *fn*.

        Raises:
            sqlite3.OperationalError / pymysql.err.OperationalError:
                If all retries are exhausted.
        """
        last_exc = None
        for attempt in range(max_retries + 1):
            try:
                return fn()
            except Exception as e:
                # Only retry transient DB errors
                exc_name = type(e).__module__ + "." + type(e).__name__
                is_transient = any(
                    kw in exc_name.lower() or kw in str(e).lower()
                    for kw in ["operationalerror", "databaselocked", "timeout", "lock"]
                )
                if not is_transient:
                    raise
                last_exc = e
                if attempt < max_retries:
                    delay = base_delay * (2 ** attempt)
                    logger.warning(
                        "DB %s failed (attempt %d/%d): %s — retrying in %.1fs",
                        operation_name, attempt + 1, max_retries, e, delay,
                    )
                    time.sleep(delay)
                else:
                    logger.error(
                        "DB %s failed after %d retries: %s",
                        operation_name, max_retries + 1, e,
                    )

        raise last_exc  # type: ignore[misc]

    # ------------------------------------------------------------------
    # JSON migration  (legacy auth.json / registry.json → SQLite)
    # ------------------------------------------------------------------

    def _maybe_migrate_from_json(self) -> None:
        """Import from legacy ``auth.json`` / ``registry.json``."""

        if not self._data_dir:
            return
        data_dir = Path(self._data_dir)
        auth_file = data_dir / "auth.json"
        reg_file = data_dir / "registry.json"
        if not auth_file.exists() and not reg_file.exists():
            return

        with self._lock:
            result = self._engine.execute(
                "SELECT COUNT(*) FROM oauth_clients"
            )
            if result.fetchone()["COUNT(*)"] > 0:
                return  # already migrated

            migrated = False

            if auth_file.exists():
                try:
                    data = json.loads(auth_file.read_text())
                    clients = data.get("clients", {})
                    tokens = data.get("tokens", {})
                    for cid, rec in clients.items():
                        self._engine.execute(
                            "INSERT OR IGNORE INTO oauth_clients "
                            "(client_id, client_secret_hash, allowed_scopes, "
                            " agent_card_id, created_at, description, tenant_id) "
                            "VALUES (?,?,?,?,?,?,?)",
                            (cid,
                             rec["client_secret_hash"],
                             ",".join(rec.get("allowed_scopes", [])),
                             rec.get("agent_card_id", ""),
                             rec.get("created_at", 0),
                             rec.get("description", ""),
                             rec.get("tenant", "")),
                        )
                    for jti, rec in tokens.items():
                        self._engine.execute(
                            "INSERT OR IGNORE INTO oauth_tokens "
                            "(jti, client_id, scope, expires_at) VALUES (?,?,?,?)",
                            (jti, rec["client_id"], rec["scope"], rec["expires_at"]),
                        )
                    logger.info(
                        "Migrated %d clients, %d tokens from auth.json",
                        len(clients), len(tokens),
                    )
                    migrated = True
                except Exception as e:
                    logger.warning("Failed to migrate auth.json: %s", e)

            if reg_file.exists():
                try:
                    data = json.loads(reg_file.read_text())
                    agents = data.get("agents", {})
                    heartbeats = data.get("heartbeats", {})
                    registered_at = data.get("registered_at", {})
                    for aid, card in agents.items():
                        card_json = json.dumps(card, ensure_ascii=False)
                        hb = heartbeats.get(aid, 0.0)
                        rt = registered_at.get(
                            aid, datetime.now(timezone.utc).isoformat()
                        )
                        self._engine.execute(
                            "INSERT OR IGNORE INTO agents "
                            "(id, card_json, heartbeat_at, registered_at, created_at) "
                            "VALUES (?,?,?,?,?)",
                            (aid, card_json, hb, rt, time.time()),
                        )
                    logger.info("Migrated %d agents from registry.json", len(agents))
                    migrated = True
                except Exception as e:
                    logger.warning("Failed to migrate registry.json: %s", e)

            if migrated:
                self._engine.commit()

    # ------------------------------------------------------------------
    # Bootstrap
    # ------------------------------------------------------------------

    def _bootstrap_registry(self) -> None:
        """Create the registry's own OAuth service account if not present."""
        with self._lock:
            result = self._engine.execute(
                "SELECT 1 FROM oauth_clients WHERE client_id=?",
                ("simple-a2a-registry",),
            )
            if result.fetchone():
                return
            if self._bootstrap_secret:
                secret = self._bootstrap_secret
                logger.info(
                    "Using CLI-provided bootstrap secret for "
                    "registry service account"
                )
            else:
                secret = secrets.token_urlsafe(32)
                logger.info(
                    "Bootstrapped registry service account "
                    "(client_id=simple-a2a-registry, secret=%s)", secret
                )
            secret_hash = hashlib.sha256(secret.encode()).hexdigest()
            self._engine.execute(
                "INSERT INTO oauth_clients "
                "(client_id, client_secret_hash, allowed_scopes, "
                " agent_card_id, created_at, description, tenant_id) "
                "VALUES (?,?,?,?,?,?,?)",
                ("simple-a2a-registry", secret_hash,
                 ",".join(SCOPES.keys()),
                 "simple-a2a-registry", time.time(),
                 "Registry service account (auto-bootstrapped)", ""),
            )
            self._engine.commit()
            logger.info(
                "Bootstrapped registry service account "
                "(client_id=simple-a2a-registry, secret=%s)", secret
            )

    # ------------------------------------------------------------------
    # Registry — agent registration & heartbeat
    # ------------------------------------------------------------------

    # -- Queries ---------------------------------------------------------------

    def list_agents(
        self,
        skill: Optional[str] = None,
        tag: Optional[str] = None,
        q: Optional[str] = None,
        tenant: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """List all agents, optionally filtered.

        Args:
            skill: Substring match against skill name or id.
            tag: Exact match against agent tags.
            q: Case-insensitive full-text search across the entire card.
            tenant: Filter by tenant.  Pass ``None`` or ``''`` to see all (admin).
                Pass a non-empty tenant string to filter; empty-tenant agents
                (pre-tenant-isolation legacy data) are also returned for
                backward compatibility.

        Returns:
            List of Agent Card dicts with ``status`` and ``lastHeartbeat``.
        """
        now = time.time()
        results: List[Dict[str, Any]] = []

        with self._tx("DEFERRED") as engine:
            if tenant is not None and tenant != "":
                # Backward compatibility: non-empty tenant filter also returns
                # agents with empty tenant_id (pre-tenant-isolation legacy data).
                result = engine.execute(
                    "SELECT id, card_json, heartbeat_at, disabled, tenant_id, preferred_channel, callback_url, callback_token FROM agents WHERE (tenant_id=? OR tenant_id='')", (tenant,),
                )
            elif tenant == "":
                # Empty string = backward compat: only empty-tenant agents
                result = engine.execute(
                    "SELECT id, card_json, heartbeat_at, disabled, tenant_id, preferred_channel, callback_url, callback_token FROM agents WHERE (tenant_id='' OR tenant_id IS NULL)"
                )
            else:
                # None = no tenant filter = all agents (admin scope)
                result = engine.execute("SELECT id, card_json, heartbeat_at, disabled, tenant_id, preferred_channel, callback_url, callback_token FROM agents")
            for row in result.fetchall():
                agent_id = row["id"]
                card = json.loads(row["card_json"])
                last_hb = row["heartbeat_at"]
                elapsed = now - last_hb if last_hb else HEARTBEAT_TIMEOUT + 1
                card["id"] = agent_id
                if row.get("disabled"):
                    card["status"] = "disabled"
                    card["disabled"] = True
                else:
                    card["status"] = "alive" if elapsed <= HEARTBEAT_TIMEOUT else "stale"
                    card["disabled"] = False
                card["lastHeartbeat"] = last_hb
                card["tenant"] = row.get("tenant_id", "") or ""
                card["preferred_channel"] = row.get("preferred_channel", "ws") or "ws"
                card["callback_url"] = row.get("callback_url", "") or ""
                card["callback_token"] = row.get("callback_token", "") or ""

                if skill:
                    skills = card.get("skills", [])
                    if not any(
                        skill in (s.get("name", "") or s.get("id", ""))
                        for s in skills
                    ):
                        continue
                if tag:
                    if tag not in card.get("tags", []):
                        continue
                if q:
                    ql = q.lower()
                    haystack = json.dumps(card, ensure_ascii=False).lower()
                    if ql not in haystack:
                        continue
                results.append(card)

        return results

    def get_agent(self, agent_id: str, tenant: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Get a single agent's card with live status.

        Args:
            agent_id: The agent's unique identifier.
            tenant: If set, only return the agent if it belongs to this tenant.
                Pass ``None`` to skip tenant check (admin mode).
                When filtering by a non-empty tenant, agents with empty
                tenant_id (pre-tenant-isolation legacy data) are also
                returned for backward compatibility.

        Returns:
            Agent Card dict with ``status`` and ``lastHeartbeat``,
            or ``None`` if the agent doesn't exist or tenant doesn't match.
        """
        with self._tx("DEFERRED") as engine:
            if tenant is not None and tenant != "":
                result = engine.execute(
                    "SELECT id, card_json, heartbeat_at, disabled, tenant_id, preferred_channel, callback_url, callback_token FROM agents WHERE id=? AND (tenant_id=? OR tenant_id='')", (agent_id, tenant),
                )
            elif tenant == "":
                result = engine.execute(
                    "SELECT id, card_json, heartbeat_at, disabled, tenant_id, preferred_channel, callback_url, callback_token FROM agents WHERE id=? AND (tenant_id='' OR tenant_id IS NULL)", (agent_id,),
                )
            else:
                result = engine.execute(
                    "SELECT id, card_json, heartbeat_at, disabled, tenant_id, preferred_channel, callback_url, callback_token FROM agents WHERE id=?", (agent_id,),
                )
            row = result.fetchone()
            if row is None:
                return None
            card = json.loads(row["card_json"])
            card["id"] = row["id"]
            # Include tenant_id from DB in the returned card
            db_tenant = row.get("tenant_id", "")
            if db_tenant:
                card["tenant"] = db_tenant
            else:
                card["tenant"] = ""
            card["preferred_channel"] = row.get("preferred_channel", "ws") or "ws"
            card["callback_url"] = row.get("callback_url", "") or ""
            card["callback_token"] = row.get("callback_token", "") or ""
            last_hb = row["heartbeat_at"]
            elapsed = time.time() - last_hb if last_hb else HEARTBEAT_TIMEOUT + 1
            if row.get("disabled"):
                card["status"] = "disabled"
                card["disabled"] = True
            else:
                is_alive = elapsed <= HEARTBEAT_TIMEOUT
                card["status"] = "alive" if is_alive else "stale"
                card["disabled"] = False
            return card

    # -- Mutations ------------------------------------------------------------

    def register_agent(self, agent_card: Dict, tenant: str = "") -> str:
        """Register an external agent and set its first heartbeat.

        Args:
            agent_card: Agent Card dict (as per A2A spec).
            tenant: Tenant namespace.  Pass ``''`` for no tenant.

        Returns:
            The assigned agent id.
        """
        card = AgentCard.from_dict(agent_card)
        agent_id = str(uuid.uuid4())
        now_ts = time.time()
        registered_at = datetime.now(timezone.utc).isoformat()
        card_json = json.dumps(card.to_dict(), ensure_ascii=False)

        # Extract tenant from AgentInterface if not explicitly provided
        if not tenant:
            # First try the card dict's top-level "tenant" (server handler extracts
            # from the same place, so store must be consistent).
            tenant = agent_card.get("tenant", "")
            if not tenant:
                for iface in card.supported_interfaces:
                    if iface.tenant:
                        tenant = iface.tenant
                        break

        # Extract preferred_channel and callback_url from the agent_card
        preferred_channel = agent_card.get("preferred_channel", "ws")
        callback_url = agent_card.get("callback_url", "")
        # Generate a callback token for callback-mode agents
        callback_token = ""
        if preferred_channel == "callback":
            callback_token = secrets.token_urlsafe(32)

        with self._tx() as engine:
            engine.execute(
                "INSERT INTO agents (id, card_json, heartbeat_at, registered_at, created_at, tenant_id, preferred_channel, callback_url, callback_token) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (agent_id, card_json, now_ts, registered_at, now_ts, tenant, preferred_channel, callback_url, callback_token),
            )

        return agent_id

    def heartbeat(self, agent_id: str, tenant: str = "") -> bool:
        """Record a heartbeat for an agent.

        Args:
            agent_id: The agent's unique identifier.
            tenant: If non-empty, only heartbeat if agent belongs to this tenant.
                Pass '' to skip tenant check (legacy / admin scope).

        Returns:
            True if updated, False if agent not found or tenant mismatch.
        """
        with self._tx() as engine:
            if tenant:
                result = engine.execute(
                    "UPDATE agents SET heartbeat_at=? WHERE id=? AND tenant_id=?",
                    (time.time(), agent_id, tenant),
                )
            else:
                result = engine.execute(
                    "UPDATE agents SET heartbeat_at=? WHERE id=?",
                    (time.time(), agent_id),
                )
            return result.rowcount > 0

    def unregister(self, agent_id: str, tenant: str = "") -> bool:
        """Remove an agent registration.

        Args:
            agent_id: The agent's unique identifier.
            tenant: If non-empty, only unregister if agent belongs to this tenant.
                Pass '' to skip tenant check (legacy / admin scope).

        Returns:
            True if removed, False if not found or tenant mismatch.
        """
        with self._tx() as engine:
            if tenant:
                result = engine.execute(
                    "DELETE FROM agents WHERE id=? AND tenant_id=?",
                    (agent_id, tenant),
                )
            else:
                result = engine.execute("DELETE FROM agents WHERE id=?", (agent_id,))
            return result.rowcount > 0

    def purge_stale(self) -> int:
        """Remove agents that haven't sent a heartbeat in
        ``HEARTBEAT_PURGE`` seconds.

        Returns:
            Number of agents removed.
        """
        cutoff = time.time() - HEARTBEAT_PURGE
        with self._tx() as engine:
            result = engine.execute(
                "DELETE FROM agents WHERE heartbeat_at > 0 AND heartbeat_at < ?",
                (cutoff,),
            )
            removed = result.rowcount
            if removed:
                logger.info("Purged %d stale agents", removed)
            return removed

    def toggle_agent(self, agent_id: str, tenant: str = "") -> Optional[bool]:
        """Toggle the disabled status of an agent.

        Args:
            agent_id: The agent's unique identifier.
            tenant: If non-empty, only toggle if agent belongs to this tenant.
                Pass '' to skip tenant check (legacy / admin scope).

        Returns:
            True if now disabled, False if now enabled,
            None if agent not found or tenant mismatch.
        """
        with self._tx() as engine:
            if tenant:
                row = engine.execute(
                    "SELECT disabled FROM agents WHERE id=? AND tenant_id=?",
                    (agent_id, tenant),
                ).fetchone()
            else:
                row = engine.execute(
                    "SELECT disabled FROM agents WHERE id=?", (agent_id,)
                ).fetchone()
            if row is None:
                return None
            new_val = 0 if row["disabled"] else 1
            engine.execute(
                "UPDATE agents SET disabled=? WHERE id=?", (new_val, agent_id)
            )
            return bool(new_val)

    # -- Stats -----------------------------------------------------------------

    def stats(self, tenant: Optional[str] = None) -> Dict[str, Any]:
        """Return registry statistics, optionally filtered by tenant.

        Args:
            tenant: Filter by tenant.  Pass ``None`` or ``''`` to see all (admin scope).

        Returns:
            Dict with keys: ``totalAgents``, ``aliveAgents``, ``staleAgents``.
        """
        now = time.time()
        with self._tx("DEFERRED") as engine:
            if tenant is not None and tenant != "":
                result = engine.execute(
                    "SELECT COUNT(*) FROM agents WHERE tenant_id=?", (tenant,)
                )
                total = result.fetchone()["COUNT(*)"]
                result = engine.execute(
                    "SELECT COUNT(*) FROM agents WHERE tenant_id=? AND ? - heartbeat_at <= ?",
                    (tenant, now, HEARTBEAT_TIMEOUT),
                )
            else:
                result = engine.execute("SELECT COUNT(*) FROM agents")
                total = result.fetchone()["COUNT(*)"]
                result = engine.execute(
                    "SELECT COUNT(*) FROM agents WHERE ? - heartbeat_at <= ?",
                    (now, HEARTBEAT_TIMEOUT),
                )
            alive = result.fetchone()["COUNT(*)"]
        return {
            "totalAgents": total,
            "aliveAgents": alive,
            "staleAgents": total - alive,
        }

    def stats_by_tenant(self) -> Dict[str, Dict[str, int]]:
        """Return registry statistics grouped by tenant.

        Returns:
            Dict mapping tenant name (or ``""`` for unassigned) to stats::

                {
                    "tenant1": {"totalAgents": 10, "aliveAgents": 8, "staleAgents": 2},
                    "": {"totalAgents": 5, "aliveAgents": 3, "staleAgents": 2},
                }
        """
        now = time.time()
        with self._tx("DEFERRED") as engine:
            result = engine.execute(
                "SELECT tenant_id, COUNT(*) AS cnt FROM agents GROUP BY tenant_id",
            )
            total_by_tenant: Dict[str, int] = {}
            for row in result.fetchall():
                tid = row["tenant_id"] or ""
                total_by_tenant[tid] = row["cnt"]

            result = engine.execute(
                "SELECT tenant_id, COUNT(*) AS cnt FROM agents WHERE ? - heartbeat_at <= ? GROUP BY tenant_id",
                (now, HEARTBEAT_TIMEOUT),
            )
            alive_by_tenant: Dict[str, int] = {}
            for row in result.fetchall():
                tid = row["tenant_id"] or ""
                alive_by_tenant[tid] = row["cnt"]

        all_tenants = set(total_by_tenant.keys()) | set(alive_by_tenant.keys())
        stats: Dict[str, Dict[str, int]] = {}
        for tid in all_tenants:
            total = total_by_tenant.get(tid, 0)
            alive = alive_by_tenant.get(tid, 0)
            stats[tid] = {
                "totalAgents": total,
                "aliveAgents": alive,
                "staleAgents": total - alive,
            }
        return stats

    def list_tenants(self) -> List[str]:
        """Return a list of all distinct tenant IDs across the agents table.

        Returns:
            List of tenant ID strings.  Empty string represents agents
            that were registered without a tenant.
        """
        with self._tx("DEFERRED") as engine:
            result = engine.execute(
                "SELECT DISTINCT tenant_id FROM agents ORDER BY tenant_id"
            )
            return [row["tenant_id"] or "" for row in result.fetchall()]

    def tenant_stats(self) -> Dict[str, Any]:
        """Return registry statistics grouped by domain (agents, oauth).

        Returns:
            Dict with keys ``agents`` and ``oauth``::

                {
                    "agents": {
                        "by_tenant": {
                            "tenant1": {"totalAgents": 5, "aliveAgents": 3, ...},
                            ...
                        },
                        "summary": {"total": N, "alive": M, "stale": K},
                    },
                    "oauth": {
                        "by_tenant": {...},
                        "summary": {"totalClients": N, "totalTokens": M, "activeTokens": K},
                    },
                }
        """
        now = time.time()
        with self._tx("DEFERRED") as engine:
            # --- Agent stats by tenant ---
            result = engine.execute(
                "SELECT tenant_id, COUNT(*) AS cnt FROM agents GROUP BY tenant_id"
            )
            total_by_tenant: Dict[str, int] = {}
            for row in result.fetchall():
                tid = row["tenant_id"] or ""
                total_by_tenant[tid] = row["cnt"]

            result = engine.execute(
                "SELECT tenant_id, COUNT(*) AS cnt FROM agents "
                "WHERE ? - heartbeat_at <= ? GROUP BY tenant_id",
                (now, HEARTBEAT_TIMEOUT),
            )
            alive_by_tenant: Dict[str, int] = {}
            for row in result.fetchall():
                tid = row["tenant_id"] or ""
                alive_by_tenant[tid] = row["cnt"]

            all_tenants = set(total_by_tenant.keys()) | set(alive_by_tenant.keys())
            agents_by_tenant: Dict[str, Dict[str, int]] = {}
            total_agents = 0
            total_alive = 0
            for tid in all_tenants:
                total = total_by_tenant.get(tid, 0)
                alive = alive_by_tenant.get(tid, 0)
                agents_by_tenant[tid] = {
                    "totalAgents": total,
                    "aliveAgents": alive,
                    "staleAgents": total - alive,
                }
                total_agents += total
                total_alive += alive

            # --- OAuth stats by tenant ---
            result = engine.execute(
                "SELECT tenant_id, COUNT(*) AS cnt FROM oauth_clients GROUP BY tenant_id"
            )
            clients_by_tenant: Dict[str, int] = {}
            for row in result.fetchall():
                tid = row["tenant_id"] or ""
                clients_by_tenant[tid] = row["cnt"]

            result = engine.execute(
                "SELECT tenant_id, COUNT(*) AS cnt FROM oauth_tokens GROUP BY tenant_id"
            )
            tokens_by_tenant: Dict[str, int] = {}
            for row in result.fetchall():
                tid = row["tenant_id"] or ""
                tokens_by_tenant[tid] = row["cnt"]

            result = engine.execute(
                "SELECT tenant_id, COUNT(*) AS cnt FROM oauth_tokens "
                "WHERE expires_at > ? GROUP BY tenant_id",
                (now,),
            )
            active_tokens_by_tenant: Dict[str, int] = {}
            for row in result.fetchall():
                tid = row["tenant_id"] or ""
                active_tokens_by_tenant[tid] = row["cnt"]

            all_oauth_tenants = (
                set(clients_by_tenant.keys())
                | set(tokens_by_tenant.keys())
                | set(active_tokens_by_tenant.keys())
            )
            oauth_by_tenant: Dict[str, Dict[str, int]] = {}
            total_clients = 0
            total_tokens = 0
            total_active = 0
            for tid in all_oauth_tenants:
                tc = clients_by_tenant.get(tid, 0)
                tt = tokens_by_tenant.get(tid, 0)
                ta = active_tokens_by_tenant.get(tid, 0)
                oauth_by_tenant[tid] = {
                    "totalClients": tc,
                    "totalTokens": tt,
                    "activeTokens": ta,
                }
                total_clients += tc
                total_tokens += tt
                total_active += ta

        return {
            "agents": {
                "by_tenant": agents_by_tenant,
                "summary": {
                    "total": total_agents,
                    "alive": total_alive,
                    "stale": total_agents - total_alive,
                },
            },
            "oauth": {
                "by_tenant": oauth_by_tenant,
                "summary": {
                    "totalClients": total_clients,
                    "totalTokens": total_tokens,
                    "activeTokens": total_active,
                },
            },
        }

    # ======================================================================
    # OAuth — client & token management
    # ======================================================================

    # -- Client CRUD ----------------------------------------------------------

    def register_client(
        self,
        *,
        agent_card_id: str = "",
        allowed_scopes: Optional[List[str]] = None,
        description: str = "",
        tenant: str = "",
    ) -> Dict[str, str]:
        """Register a new OAuth 2.1 client.

        Returns:
            Dict with ``client_id`` and ``client_secret`` (raw — show once).
        """
        client_id = f"client-{uuid.uuid4().hex[:12]}"
        client_secret = secrets.token_urlsafe(32)
        secret_hash = hashlib.sha256(client_secret.encode()).hexdigest()

        with self._tx() as engine:
            engine.execute(
                "INSERT INTO oauth_clients "
                "(client_id, client_secret_hash, allowed_scopes, "
                " agent_card_id, created_at, description, tenant_id) "
                "VALUES (?,?,?,?,?,?,?)",
                (client_id, secret_hash,
                 ",".join(allowed_scopes or list(SCOPES.keys())),
                 agent_card_id, time.time(), description, tenant),
            )

        return {"client_id": client_id, "client_secret": client_secret}

    def list_clients(self, tenant: Optional[str] = None) -> List[Dict[str, Any]]:
        """List all registered OAuth clients with token counts.

        Args:
            tenant: Filter by tenant.  Pass ``None`` to see all (admin).

        Returns:
            List of dicts with client metadata + active token count.
        """
        now = time.time()
        with self._tx("DEFERRED") as engine:
            if tenant is not None:
                result = engine.execute(
                    "SELECT * FROM oauth_clients WHERE tenant_id=? ORDER BY client_id",
                    (tenant,),
                )
            else:
                result = engine.execute(
                    "SELECT * FROM oauth_clients ORDER BY client_id"
                )
            rows = result.fetchall()
            result_list = []
            for row in rows:
                cid = row["client_id"]
                token_result = engine.execute(
                    "SELECT COUNT(*) FROM oauth_tokens "
                    "WHERE client_id=? AND expires_at>?",
                    (cid, now),
                )
                token_count = token_result.fetchone()["COUNT(*)"]
                result_list.append({
                    "client_id": cid,
                    "agent_card_id": row["agent_card_id"],
                    "description": row["description"],
                    "tenant": row.get("tenant_id", ""),
                    "scopes": row["allowed_scopes"].split(",") if row["allowed_scopes"] else [],
                    "token_count": token_count,
                    "created_at": row["created_at"],
                })
            return result_list

    def delete_client(self, client_id: str) -> bool:
        """Delete a client and revoke all its tokens.

        Returns:
            True if the client was found and deleted, False otherwise.
        """
        with self._tx() as engine:
            engine.execute("DELETE FROM oauth_tokens WHERE client_id=?", (client_id,))
            result = engine.execute(
                "DELETE FROM oauth_clients WHERE client_id=?", (client_id,)
            )
            return result.rowcount > 0

    def get_client(self, client_id: str) -> Optional[ClientRecord]:
        """Get a client record."""
        with self._tx("DEFERRED") as engine:
            result = engine.execute(
                "SELECT * FROM oauth_clients WHERE client_id=?", (client_id,)
            )
            row = result.fetchone()
            if row is None:
                return None
            return ClientRecord(
                client_id=row["client_id"],
                client_secret_hash=row["client_secret_hash"],
                allowed_scopes=row["allowed_scopes"].split(",") if row["allowed_scopes"] else [],
                agent_card_id=row["agent_card_id"],
                created_at=row["created_at"],
                description=row["description"],
                tenant=row.get("tenant_id", ""),
            )

    def verify_client_secret(self, client_id: str, secret: str) -> bool:
        """Verify a client's secret against its stored hash."""
        rec = self.get_client(client_id)
        if rec is None:
            return False
        return secrets.compare_digest(
            rec.client_secret_hash,
            hashlib.sha256(secret.encode()).hexdigest(),
        )

    def client_allowed_scopes(self, client_id: str, requested_scopes: str) -> bool:
        """Check that all requested scopes are allowed for this client."""
        rec = self.get_client(client_id)
        if rec is None:
            return False
        requested = set(requested_scopes.split())
        allowed = set(rec.allowed_scopes)
        return requested.issubset(allowed)

    # -- Token tracking --------------------------------------------------------

    def record_token(self, token_payload: Dict[str, Any]) -> None:
        """Record an issued access token for auditing / revocation."""
        jti = token_payload.get("jti", "")
        with self._tx() as engine:
            engine.execute(
                "INSERT OR REPLACE INTO oauth_tokens "
                "(jti, client_id, scope, expires_at) VALUES (?,?,?,?)",
                (jti,
                 token_payload.get("sub", ""),
                 token_payload.get("scope", ""),
                 token_payload.get("exp", 0)),
            )

    def get_token(self, jti: str) -> Optional[TokenRecord]:
        """Get a token record, auto-expiring stale entries."""
        with self._tx("DEFERRED") as engine:
            result = engine.execute(
                "SELECT * FROM oauth_tokens WHERE jti=?", (jti,)
            )
            row = result.fetchone()
            if row is None:
                return None
            rec = TokenRecord(
                jti=row["jti"],
                client_id=row["client_id"],
                scope=row["scope"],
                expires_at=row["expires_at"],
            )
            if rec.expires_at < time.time():
                engine.execute("DELETE FROM oauth_tokens WHERE jti=?", (jti,))
                return None
            return rec

    def revoke_token(self, jti: str) -> bool:
        """Revoke a single token."""
        with self._tx() as engine:
            result = engine.execute(
                "DELETE FROM oauth_tokens WHERE jti=?", (jti,)
            )
            return result.rowcount > 0

    def revoke_client_tokens(self, client_id: str) -> int:
        """Revoke all tokens belonging to a client.

        Returns:
            Number of tokens revoked.
        """
        with self._tx() as engine:
            result = engine.execute(
                "DELETE FROM oauth_tokens WHERE client_id=?", (client_id,)
            )
            return result.rowcount

    # -- Authorization codes  (authorization_code grant) ------------------------

    def create_auth_code(
        self, client_id: str, code_challenge: str, code_challenge_method: str,
        redirect_uri: str, scope: str,
    ) -> str:
        """Create and store an authorization code."""
        code = secrets.token_urlsafe(32)
        with self._tx() as engine:
            engine.execute(
                "INSERT INTO auth_codes "
                "(code, client_id, code_challenge, code_challenge_method, "
                " redirect_uri, scope, created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (code, client_id, code_challenge, code_challenge_method,
                 redirect_uri, scope, time.time()),
            )
        return code

    def consume_auth_code(self, code: str, code_verifier: str) -> Optional[Dict[str, Any]]:
        """Validate and consume an authorization code (PKCE).

        Returns:
            The auth code data dict, or ``None`` if invalid/expired.
        """
        with self._tx() as engine:
            result = engine.execute(
                "SELECT * FROM auth_codes WHERE code=?", (code,)
            )
            row = result.fetchone()
            if row is None:
                return None
            # Expiry check
            if time.time() - row["created_at"] > AUTH_CODE_EXPIRY_SECONDS:
                engine.execute("DELETE FROM auth_codes WHERE code=?", (code,))
                return None
            # PKCE verification
            if row["code_challenge_method"] == "S256":
                expected = hashlib.sha256(code_verifier.encode()).hexdigest()
                if not secrets.compare_digest(expected, row["code_challenge"]):
                    logger.warning("PKCE code_verifier mismatch")
                    return None
            result_data = {
                "client_id": row["client_id"],
                "code_challenge": row["code_challenge"],
                "code_challenge_method": row["code_challenge_method"],
                "redirect_uri": row["redirect_uri"],
                "scope": row["scope"],
                "created_at": row["created_at"],
            }
            engine.execute("DELETE FROM auth_codes WHERE code=?", (code,))
            return result_data

    # -- Auth stats -------------------------------------------------------------

    # ------------------------------------------------------------------
    # Agent Authorization Matrix CRUD
    # ------------------------------------------------------------------

    def create_authorization(
        self,
        *,
        source_agent_id: str,
        target_agent_id: str,
        allowed_actions: Optional[List[str]] = None,
        scope_restriction: Optional[str] = None,
        max_depth: int = 5,
        expires_at: Optional[float] = None,
        tenant: str = "",
    ) -> Dict[str, Any]:
        """Create an agent-to-agent authorization record.

        Returns the created record as a dict (including the new ``id``).
        Raises on duplicate (source, target, tenant) violation.
        """
        actions_json = json.dumps(allowed_actions or ["*"])
        now = time.time()
        with self._tx() as engine:
            result = engine.execute(
                "INSERT INTO agent_authorizations "
                "(source_agent_id, target_agent_id, allowed_actions, "
                " scope_restriction, max_depth, tenant_id, created_at, expires_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (source_agent_id, target_agent_id, actions_json,
                 scope_restriction, max_depth, tenant, now, expires_at),
            )
            rowid = result.lastrowid
        created = self.get_authorization(rowid)
        assert created is not None, "Failed to retrieve created authorization"
        return created

    def get_authorization(self, authz_id: int) -> Optional[Dict[str, Any]]:
        """Get a single authorization record by id."""
        with self._tx("DEFERRED") as engine:
            result = engine.execute(
                "SELECT * FROM agent_authorizations WHERE id=?", (authz_id,),
            )
            row = result.fetchone()
            if row is None:
                return None
            return self._row_to_authz(row)

    def list_authorizations(
        self,
        *,
        source: Optional[str] = None,
        target: Optional[str] = None,
        tenant: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """List authorization records, optionally filtered."""
        conditions: List[str] = []
        params: List[Any] = []
        if source:
            conditions.append("source_agent_id=?")
            params.append(source)
        if target:
            conditions.append("target_agent_id=?")
            params.append(target)
        if tenant is not None:
            conditions.append("tenant_id=?")
            params.append(tenant)

        where = "WHERE " + " AND ".join(conditions) if conditions else ""
        sql = f"SELECT * FROM agent_authorizations {where} ORDER BY created_at DESC"

        with self._tx("DEFERRED") as engine:
            result = engine.execute(sql, tuple(params))
            rows = result.fetchall()
            return [self._row_to_authz(r) for r in rows]

    def delete_authorization(self, authz_id: int) -> bool:
        """Delete an authorization record by id.

        Returns True if the record existed and was deleted.
        """
        with self._tx() as engine:
            result = engine.execute(
                "DELETE FROM agent_authorizations WHERE id=?", (authz_id,),
            )
            return result.rowcount > 0

    def _row_to_authz(self, row: Any) -> Dict[str, Any]:
        """Convert a DB row to an authorization dict."""
        try:
            actions = json.loads(row["allowed_actions"]) if row.get("allowed_actions") else ["*"]
        except (json.JSONDecodeError, TypeError):
            actions = ["*"]
        return {
            "id": row["id"],
            "source_agent_id": row["source_agent_id"],
            "target_agent_id": row["target_agent_id"],
            "allowed_actions": actions,
            "scope_restriction": row.get("scope_restriction"),
            "max_depth": row.get("max_depth", 5),
            "tenant_id": row.get("tenant_id", ""),
            "created_at": row["created_at"],
            "expires_at": row.get("expires_at"),
        }

    # ------------------------------------------------------------------
    # Auth store helpers

    def auth_stats(self) -> Dict[str, Any]:
        """Return OAuth store statistics."""
        now = time.time()
        with self._tx("DEFERRED") as engine:
            result = engine.execute("SELECT COUNT(*) FROM oauth_clients")
            total_clients = result.fetchone()["COUNT(*)"]
            result = engine.execute("SELECT COUNT(*) FROM oauth_tokens")
            total_tokens = result.fetchone()["COUNT(*)"]
            result = engine.execute(
                "SELECT COUNT(*) FROM oauth_tokens WHERE expires_at>?", (now,),
            )
            active_tokens = result.fetchone()["COUNT(*)"]
        return {
            "totalClients": total_clients,
            "totalTokens": total_tokens,
            "activeTokens": active_tokens,
        }