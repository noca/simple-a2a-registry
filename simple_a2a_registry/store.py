"""Unified SQLite-backed persistence — registry store + OAuth auth store.

Merges ``A2ARegistryStore`` (agent registration with heartbeat) and
``AuthStore`` (OAuth client/token management) into one SQLite database.
Thread-safe via ``threading.RLock``.
"""
from __future__ import annotations

import hashlib
import json
import logging
import secrets
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional

from simple_a2a_registry.models import AgentCard

logger = logging.getLogger("a2a_registry.store")

# ---------------------------------------------------------------------------
# Registry constants
# ---------------------------------------------------------------------------

HEARTBEAT_TIMEOUT = 120   # seconds before an agent is considered stale
HEARTBEAT_PURGE = 300     # seconds before a stale agent is fully removed

# ---------------------------------------------------------------------------
# Auth constants (also used by auth.py — keeps them here for import convenience)
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
# Dataclass records  (same shape as before, used as Python-level value objects)
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


@dataclass
class TokenRecord:
    """An issued access token record for auditing / revocation."""
    jti: str
    client_id: str
    scope: str
    expires_at: float


# ---------------------------------------------------------------------------
# SQL schema
# ---------------------------------------------------------------------------

_SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA busy_timeout=5000;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS agents (
    id              TEXT PRIMARY KEY,
    card_json       TEXT NOT NULL,
    heartbeat_at    REAL NOT NULL DEFAULT 0,
    registered_at   TEXT NOT NULL,
    created_at      REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS oauth_clients (
    client_id           TEXT PRIMARY KEY,
    client_secret_hash  TEXT NOT NULL,
    allowed_scopes      TEXT NOT NULL,
    agent_card_id       TEXT NOT NULL DEFAULT '',
    created_at          REAL NOT NULL,
    description         TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS oauth_tokens (
    jti         TEXT PRIMARY KEY,
    client_id   TEXT NOT NULL,
    scope       TEXT NOT NULL,
    expires_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS auth_codes (
    code                    TEXT PRIMARY KEY,
    client_id               TEXT NOT NULL,
    code_challenge          TEXT NOT NULL,
    code_challenge_method   TEXT NOT NULL,
    redirect_uri            TEXT NOT NULL,
    scope                   TEXT NOT NULL,
    created_at              REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_oauth_tokens_client_id ON oauth_tokens(client_id);
CREATE INDEX IF NOT EXISTS idx_agents_heartbeat ON agents(heartbeat_at);
"""


# ======================================================================
# Unified Store
# ======================================================================


class Store:
    """Unified persistence layer combining registry and auth stores.

    Thread-safe via ``threading.RLock``.  Uses WAL mode with
    ``busy_timeout=5000`` and ``BEGIN IMMEDIATE`` transactions.
    """

    def __init__(self, data_dir: str,
             bootstrap_secret: Optional[str] = None) -> None:
        resolved = Path(data_dir).expanduser().resolve()
        resolved.mkdir(parents=True, exist_ok=True)
        self._db_path = str(resolved / "registry.db")
        self._lock = threading.RLock()
        self._conn: Optional[sqlite3.Connection] = None
        self._bootstrap_secret = bootstrap_secret

        self._connect()
        self._maybe_migrate_from_json(resolved)
        self._bootstrap_registry()

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
                raise RuntimeError("Store is closed")
            conn.execute(f"BEGIN {mode}")
            try:
                yield conn.cursor()
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    # ------------------------------------------------------------------
    # JSON migration  (legacy auth.json / registry.json → SQLite)
    # ------------------------------------------------------------------

    def _maybe_migrate_from_json(self, data_dir: Path) -> None:
        """Import from legacy ``auth.json`` / ``registry.json`` if the DB is
        empty (no clients yet).  Safe to call repeatedly — runs only once."""
        auth_file = data_dir / "auth.json"
        reg_file = data_dir / "registry.json"
        if not auth_file.exists() and not reg_file.exists():
            return

        with self._lock:
            cur = self._conn.cursor()
            cur.execute("SELECT COUNT(*) FROM oauth_clients")
            if cur.fetchone()[0] > 0:
                return  # already migrated (or bootstrapped)

            migrated = False

            if auth_file.exists():
                try:
                    data = json.loads(auth_file.read_text())
                    clients = data.get("clients", {})
                    tokens = data.get("tokens", {})
                    for cid, rec in clients.items():
                        cur.execute(
                            "INSERT OR IGNORE INTO oauth_clients "
                            "(client_id, client_secret_hash, allowed_scopes, "
                            " agent_card_id, created_at, description) "
                            "VALUES (?,?,?,?,?,?)",
                            (cid,
                             rec["client_secret_hash"],
                             ",".join(rec.get("allowed_scopes", [])),
                             rec.get("agent_card_id", ""),
                             rec.get("created_at", 0),
                             rec.get("description", "")),
                        )
                    for jti, rec in tokens.items():
                        cur.execute(
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
                        cur.execute(
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
                self._conn.commit()

    # ------------------------------------------------------------------
    # Bootstrap
    # ------------------------------------------------------------------

    def _bootstrap_registry(self) -> None:
        """Create the registry's own OAuth service account if not present."""
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                "SELECT 1 FROM oauth_clients WHERE client_id=?",
                ("simple-a2a-registry",),
            )
            if cur.fetchone():
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
            cur.execute(
                "INSERT INTO oauth_clients "
                "(client_id, client_secret_hash, allowed_scopes, "
                " agent_card_id, created_at, description) "
                "VALUES (?,?,?,?,?,?)",
                ("simple-a2a-registry", secret_hash,
                 ",".join(SCOPES.keys()),
                 "simple-a2a-registry", time.time(),
                 "Registry service account (auto-bootstrapped)"),
            )
            self._conn.commit()
            logger.info(
                "Bootstrapped registry service account "
                "(client_id=simple-a2a-registry, secret=%s)", secret
            )

    # ======================================================================
    # Registry — agent registration & heartbeat
    # ======================================================================

    # -- Queries ---------------------------------------------------------------

    def list_agents(
        self,
        skill: Optional[str] = None,
        tag: Optional[str] = None,
        q: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """List all agents, optionally filtered.

        Args:
            skill: Substring match against skill name or id.
            tag: Exact match against agent tags.
            q: Case-insensitive full-text search across the entire card.

        Returns:
            List of Agent Card dicts with ``status`` and ``lastHeartbeat``.
        """
        now = time.time()
        results: List[Dict[str, Any]] = []

        with self._tx("DEFERRED") as cur:
            cur.execute("SELECT id, card_json, heartbeat_at FROM agents")
            for row in cur.fetchall():
                agent_id = row["id"]
                card = json.loads(row["card_json"])
                last_hb = row["heartbeat_at"]
                elapsed = now - last_hb if last_hb else HEARTBEAT_TIMEOUT + 1
                is_alive = elapsed <= HEARTBEAT_TIMEOUT
                card["id"] = agent_id
                card["status"] = "alive" if is_alive else "stale"
                card["lastHeartbeat"] = last_hb

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

    def get_agent(self, agent_id: str) -> Optional[Dict[str, Any]]:
        """Get a single agent's card with live status.

        Returns:
            Agent Card dict with ``status`` and ``lastHeartbeat``,
            or ``None`` if the agent doesn't exist.
        """
        with self._tx("DEFERRED") as cur:
            cur.execute("SELECT id, card_json, heartbeat_at FROM agents WHERE id=?", (agent_id,))
            row = cur.fetchone()
            if row is None:
                return None
            card = json.loads(row["card_json"])
            last_hb = row["heartbeat_at"]
            elapsed = time.time() - last_hb if last_hb else HEARTBEAT_TIMEOUT + 1
            card["id"] = agent_id
            card["status"] = "alive" if elapsed <= HEARTBEAT_TIMEOUT else "stale"
            card["lastHeartbeat"] = last_hb
            return card

    # -- Mutations ------------------------------------------------------------

    def register_agent(self, agent_card: Dict) -> str:
        """Register an external agent and set its first heartbeat.

        Args:
            agent_card: Agent Card dict (as per A2A spec).

        Returns:
            The assigned agent id.
        """
        card = AgentCard.from_dict(agent_card)
        agent_id = str(uuid.uuid4())
        now_ts = time.time()
        registered_at = datetime.now(timezone.utc).isoformat()
        card_json = json.dumps(card.to_dict(), ensure_ascii=False)

        with self._tx() as cur:
            cur.execute(
                "INSERT INTO agents (id, card_json, heartbeat_at, registered_at, created_at) "
                "VALUES (?,?,?,?,?)",
                (agent_id, card_json, now_ts, registered_at, now_ts),
            )

        return agent_id

    def heartbeat(self, agent_id: str) -> bool:
        """Record a heartbeat for an agent.

        Returns:
            ``True`` if the agent is known, ``False`` otherwise.
        """
        with self._tx() as cur:
            cur.execute(
                "UPDATE agents SET heartbeat_at=? WHERE id=?",
                (time.time(), agent_id),
            )
            return cur.rowcount > 0

    def unregister(self, agent_id: str) -> bool:
        """Remove an agent registration.

        Returns:
            ``True`` if removed, ``False`` if not found.
        """
        with self._tx() as cur:
            cur.execute("DELETE FROM agents WHERE id=?", (agent_id,))
            return cur.rowcount > 0

    def purge_stale(self) -> int:
        """Remove agents that haven't sent a heartbeat in
        ``HEARTBEAT_PURGE`` seconds.

        Returns:
            Number of agents removed.
        """
        cutoff = time.time() - HEARTBEAT_PURGE
        with self._tx() as cur:
            cur.execute(
                "DELETE FROM agents WHERE heartbeat_at > 0 AND heartbeat_at < ?",
                (cutoff,),
            )
            removed = cur.rowcount
            if removed:
                logger.info("Purged %d stale agents", removed)
            return removed

    # -- Stats -----------------------------------------------------------------

    def stats(self) -> Dict[str, Any]:
        """Return registry statistics.

        Returns:
            Dict with keys: ``totalAgents``, ``aliveAgents``, ``staleAgents``.
        """
        now = time.time()
        with self._tx("DEFERRED") as cur:
            cur.execute("SELECT COUNT(*) FROM agents")
            total = cur.fetchone()[0]
            cur.execute(
                "SELECT COUNT(*) FROM agents WHERE ? - heartbeat_at <= ?",
                (now, HEARTBEAT_TIMEOUT),
            )
            alive = cur.fetchone()[0]
        return {
            "totalAgents": total,
            "aliveAgents": alive,
            "staleAgents": total - alive,
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
    ) -> Dict[str, str]:
        """Register a new OAuth 2.1 client.

        Returns:
            Dict with ``client_id`` and ``client_secret`` (raw — show once).
        """
        client_id = f"client-{uuid.uuid4().hex[:12]}"
        client_secret = secrets.token_urlsafe(32)
        secret_hash = hashlib.sha256(client_secret.encode()).hexdigest()

        with self._tx() as cur:
            cur.execute(
                "INSERT INTO oauth_clients "
                "(client_id, client_secret_hash, allowed_scopes, "
                " agent_card_id, created_at, description) "
                "VALUES (?,?,?,?,?,?)",
                (client_id, secret_hash,
                 ",".join(allowed_scopes or list(SCOPES.keys())),
                 agent_card_id, time.time(), description),
            )

        return {"client_id": client_id, "client_secret": client_secret}

    def list_clients(self) -> List[Dict[str, Any]]:
        """List all registered OAuth clients with token counts.

        Returns:
            List of dicts with client metadata + active token count.
        """
        now = time.time()
        with self._tx("DEFERRED") as cur:
            cur.execute("SELECT * FROM oauth_clients ORDER BY client_id")
            rows = cur.fetchall()
            result = []
            for row in rows:
                cid = row["client_id"]
                cur.execute(
                    "SELECT COUNT(*) FROM oauth_tokens "
                    "WHERE client_id=? AND expires_at>?",
                    (cid, now),
                )
                token_count = cur.fetchone()[0]
                result.append({
                    "client_id": cid,
                    "agent_card_id": row["agent_card_id"],
                    "description": row["description"],
                    "scopes": row["allowed_scopes"].split(",") if row["allowed_scopes"] else [],
                    "token_count": token_count,
                    "created_at": row["created_at"],
                })
            return result

    def delete_client(self, client_id: str) -> bool:
        """Delete a client and revoke all its tokens.

        Returns:
            True if the client was found and deleted, False otherwise.
        """
        with self._tx() as cur:
            cur.execute("DELETE FROM oauth_tokens WHERE client_id=?", (client_id,))
            cur.execute("DELETE FROM oauth_clients WHERE client_id=?", (client_id,))
            return cur.rowcount > 0

    def get_client(self, client_id: str) -> Optional[ClientRecord]:
        """Get a client record."""
        with self._tx("DEFERRED") as cur:
            cur.execute("SELECT * FROM oauth_clients WHERE client_id=?", (client_id,))
            row = cur.fetchone()
            if row is None:
                return None
            return ClientRecord(
                client_id=row["client_id"],
                client_secret_hash=row["client_secret_hash"],
                allowed_scopes=row["allowed_scopes"].split(",") if row["allowed_scopes"] else [],
                agent_card_id=row["agent_card_id"],
                created_at=row["created_at"],
                description=row["description"],
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
        with self._tx() as cur:
            cur.execute(
                "INSERT OR REPLACE INTO oauth_tokens "
                "(jti, client_id, scope, expires_at) VALUES (?,?,?,?)",
                (jti,
                 token_payload.get("sub", ""),
                 token_payload.get("scope", ""),
                 token_payload.get("exp", 0)),
            )

    def get_token(self, jti: str) -> Optional[TokenRecord]:
        """Get a token record, auto-expiring stale entries."""
        with self._tx("DEFERRED") as cur:
            cur.execute("SELECT * FROM oauth_tokens WHERE jti=?", (jti,))
            row = cur.fetchone()
            if row is None:
                return None
            rec = TokenRecord(
                jti=row["jti"],
                client_id=row["client_id"],
                scope=row["scope"],
                expires_at=row["expires_at"],
            )
            if rec.expires_at < time.time():
                cur.execute("DELETE FROM oauth_tokens WHERE jti=?", (jti,))
                return None
            return rec

    def revoke_token(self, jti: str) -> bool:
        """Revoke a single token."""
        with self._tx() as cur:
            cur.execute("DELETE FROM oauth_tokens WHERE jti=?", (jti,))
            return cur.rowcount > 0

    def revoke_client_tokens(self, client_id: str) -> int:
        """Revoke all tokens belonging to a client.

        Returns:
            Number of tokens revoked.
        """
        with self._tx() as cur:
            cur.execute("DELETE FROM oauth_tokens WHERE client_id=?", (client_id,))
            return cur.rowcount

    # -- Authorization codes  (authorization_code grant) ------------------------

    def create_auth_code(
        self, client_id: str, code_challenge: str, code_challenge_method: str,
        redirect_uri: str, scope: str,
    ) -> str:
        """Create and store an authorization code."""
        code = secrets.token_urlsafe(32)
        with self._tx() as cur:
            cur.execute(
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
        with self._tx() as cur:
            cur.execute("SELECT * FROM auth_codes WHERE code=?", (code,))
            row = cur.fetchone()
            if row is None:
                return None
            # Expiry check
            if time.time() - row["created_at"] > AUTH_CODE_EXPIRY_SECONDS:
                cur.execute("DELETE FROM auth_codes WHERE code=?", (code,))
                return None
            # PKCE verification
            if row["code_challenge_method"] == "S256":
                expected = hashlib.sha256(code_verifier.encode()).hexdigest()
                if not secrets.compare_digest(expected, row["code_challenge"]):
                    logger.warning("PKCE code_verifier mismatch")
                    return None
            result = {
                "client_id": row["client_id"],
                "code_challenge": row["code_challenge"],
                "code_challenge_method": row["code_challenge_method"],
                "redirect_uri": row["redirect_uri"],
                "scope": row["scope"],
                "created_at": row["created_at"],
            }
            cur.execute("DELETE FROM auth_codes WHERE code=?", (code,))
            return result

    # -- Auth stats -------------------------------------------------------------

    def auth_stats(self) -> Dict[str, Any]:
        """Return OAuth store statistics."""
        now = time.time()
        with self._tx("DEFERRED") as cur:
            cur.execute("SELECT COUNT(*) FROM oauth_clients")
            total_clients = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM oauth_tokens")
            total_tokens = cur.fetchone()[0]
            cur.execute(
                "SELECT COUNT(*) FROM oauth_tokens WHERE expires_at>?", (now,),
            )
            active_tokens = cur.fetchone()[0]
        return {
            "totalClients": total_clients,
            "totalTokens": total_tokens,
            "activeTokens": active_tokens,
        }
