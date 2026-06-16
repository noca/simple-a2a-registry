"""Delegated Token Manager (DTM) — P0 mandatory module.

Manages the lifecycle of ``DelegatedTaskToken``:
- Minting (JWT creation with scope attenuation)
- Verification (RS256 signature, expiry, task/agent binding)
- Replay prevention via ``delegation_tokens`` table
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from simple_a2a_registry.security.errors import (
    AuthzDecision,
    TokenExpiredError,
    TokenInvalidError,
    TokenReplayError,
    TokenSubjectMismatchError,
    TokenTaskMismatchError,
)
from simple_a2a_registry.database import DatabaseEngine
from simple_a2a_registry.auth import ISSUER, _rsa_sign_jwt, _rsa_verify_jwt

logger = logging.getLogger("a2a_registry.security.dtm")


# ---------------------------------------------------------------------------
# DelegatedTaskToken dataclass
# ---------------------------------------------------------------------------


@dataclass
class DelegatedTaskToken:
    jti: str = ""
    iss: str = ISSUER
    sub: str = ""                       # current operator (assignee)
    iat: float = 0.0
    exp: float = 0.0

    # Delegation chain
    origin_agent: str = ""              # original requester
    origin_tenant: str = ""             # original tenant
    delegation_chain: List[Dict[str, Any]] = field(default_factory=list)

    # Auth boundary
    effective_scope: str = ""           # current effective scope
    attenuated_from: str = ""           # parent scope (audit)
    max_depth: int = 10
    depth: int = 0

    # Constraints
    allowed_callees: Optional[List[str]] = None
    task_id: str = ""

    def to_jwt_payload(self) -> Dict[str, Any]:
        return {
            "jti": self.jti,
            "iss": self.iss,
            "sub": self.sub,
            "iat": self.iat,
            "exp": self.exp,
            "origin_agent": self.origin_agent,
            "origin_tenant": self.origin_tenant,
            "delegation_chain": self.delegation_chain,
            "effective_scope": self.effective_scope,
            "attenuated_from": self.attenuated_from,
            "max_depth": self.max_depth,
            "depth": self.depth,
            "allowed_callees": self.allowed_callees,
            "task_id": self.task_id,
        }

    def to_jwt(self, private_key: str) -> str:
        return _rsa_sign_jwt(self.to_jwt_payload(), private_key)

    @staticmethod
    def from_jwt(token: str, public_key: str) -> "DelegatedTaskToken":
        # Decode the payload early so we can surface a specific error for
        # expired tokens before falling through to the generic "invalid" path.
        import base64, json, time
        try:
            parts = token.split(".")
            if len(parts) != 3:
                raise TokenInvalidError("Invalid or malformed delegation token")
            _pad = lambda d: d + "=" * (4 - len(d) % 4) if len(d) % 4 else d
            payload = json.loads(base64.urlsafe_b64decode(_pad(parts[1])))
            exp = payload.get("exp", 0)
            if exp and time.time() > exp:
                raise TokenExpiredError(
                    f"Delegation token expired (exp={exp})"
                )
        except TokenExpiredError:
            raise
        except Exception:
            payload = None

        # Full cryptographic verification
        payload = _rsa_verify_jwt(token, public_key)
        if payload is None:
            raise TokenInvalidError("Invalid or malformed delegation token")
        return DelegatedTaskToken(
            jti=payload.get("jti", ""),
            iss=payload.get("iss", ISSUER),
            sub=payload.get("sub", ""),
            iat=payload.get("iat", 0.0),
            exp=payload.get("exp", 0.0),
            origin_agent=payload.get("origin_agent", ""),
            origin_tenant=payload.get("origin_tenant", ""),
            delegation_chain=payload.get("delegation_chain", []),
            effective_scope=payload.get("effective_scope", ""),
            attenuated_from=payload.get("attenuated_from", ""),
            max_depth=payload.get("max_depth", 10),
            depth=payload.get("depth", 0),
            allowed_callees=payload.get("allowed_callees"),
            task_id=payload.get("task_id", ""),
        )


# ---------------------------------------------------------------------------
# Schema for delegation_tokens table
# ---------------------------------------------------------------------------

DELEGATION_TOKENS_SCHEMA = """
CREATE TABLE IF NOT EXISTS delegation_tokens (
    jti          TEXT PRIMARY KEY,
    task_id      TEXT NOT NULL,
    sub          TEXT NOT NULL,
    origin_agent TEXT NOT NULL,
    scope        TEXT NOT NULL,
    depth        INTEGER NOT NULL DEFAULT 0,
    expires_at   INTEGER NOT NULL,
    used_at      TIMESTAMP,
    created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (task_id) REFERENCES tasks(id)
);
"""

DELEGATION_TOKENS_SCHEMA_MYSQL = """
CREATE TABLE IF NOT EXISTS delegation_tokens (
    jti          VARCHAR(64) PRIMARY KEY,
    task_id      VARCHAR(64) NOT NULL,
    sub          VARCHAR(255) NOT NULL,
    origin_agent VARCHAR(255) NOT NULL,
    scope        VARCHAR(255) NOT NULL,
    depth        INT NOT NULL DEFAULT 0,
    expires_at   BIGINT NOT NULL,
    used_at      DOUBLE,
    created_at   DOUBLE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""


# ---------------------------------------------------------------------------
# Scope attenuation logic
# ---------------------------------------------------------------------------


def attenuate_scope(parent_scope: str, restriction: Optional[Dict[str, Any]] = None) -> str:
    """Apply scope attenuation rules.

    Args:
        parent_scope: Space-separated scope string from parent token.
        restriction: Dict with one of:
            - ``{"exclude": ["agent:read"]}`` — remove scopes
            - ``{"reduce_to": ["task:read"]}`` — intersection
            - ``{"map": {"admin": "read"}}`` — remap scopes (downgrade)
            - ``None`` / ``{}`` — inherit unchanged

    Returns:
        Attenuated scope string (subset of parent_scope).

    Raises:
        ValueError: If result is empty.
    """
    if not restriction:
        return parent_scope

    parent_set = set(parent_scope.split()) if parent_scope else set()

    if "exclude" in restriction:
        excluded = set(restriction["exclude"])
        result = parent_set - excluded
    elif "reduce_to" in restriction:
        allowed = set(restriction["reduce_to"])
        result = parent_set & allowed  # intersection
    elif "map" in restriction:
        mapping: Dict[str, str] = restriction["map"]
        result: set = set()
        for s in parent_set:
            mapped = mapping.get(s, s)
            # only allow mapped scope if the resultant scope is in parent_set
            # (no expansion): if mapped is not the original and mapped is not
            # in parent_set, skip it
            if mapped in parent_set:
                result.add(mapped)
            else:
                result.add(s)
    else:
        result = set(parent_set)

    attenuated = " ".join(sorted(result))
    if not attenuated:
        raise ValueError("Scope attenuation produced empty scope — delegation rejected")
    return attenuated


# ---------------------------------------------------------------------------
# DTM
# ---------------------------------------------------------------------------


class DelegatedTokenManager:
    """Manages the lifecycle of DelegatedTaskTokens."""

    def __init__(
        self,
        engine: DatabaseEngine,
        private_key: str,
        public_key: str,
        default_ttl: int = 300,
        max_depth: int = 10,
    ) -> None:
        self._engine = engine
        self._private_key = private_key
        self._public_key = public_key
        self._default_ttl = default_ttl
        self._max_depth = max_depth
        self._lock = threading.RLock()

    def ensure_schema(self) -> None:
        """Create delegation_tokens table if missing."""
        if self._engine.driver == "sqlite":
            self._engine.executescript(DELEGATION_TOKENS_SCHEMA)
        else:
            for stmt in DELEGATION_TOKENS_SCHEMA_MYSQL.split(";"):
                stripped = stmt.strip()
                if stripped:
                    try:
                        self._engine.execute(stripped)
                    except Exception:
                        pass
        self._engine.commit()

    # ------------------------------------------------------------------
    # Mint
    # ------------------------------------------------------------------

    def mint_delegation_token(
        self,
        *,
        sub: str,
        task_id: str,
        origin_agent: str,
        origin_tenant: str = "",
        effective_scope: str,
        parent_chain: Optional[List[Dict[str, Any]]] = None,
        parent_scope: str = "",
        restriction: Optional[Dict[str, Any]] = None,
        depth: int = 0,
        max_depth: Optional[int] = None,
        ttl: Optional[int] = None,
        allowed_callees: Optional[List[str]] = None,
    ) -> DelegatedTaskToken:
        """Mint a new DelegatedTaskToken.

        Args:
            sub: Assignee (who will claim this task).
            task_id: Binding task id.
            origin_agent: Original requester.
            origin_tenant: Original tenant.
            effective_scope: The scope for this token (pre-attenuation).
            parent_chain: Existing delegation chain from parent token.
            parent_scope: Parent token's scope (for attenuation audit).
            restriction: Scope restriction to apply.
            depth: Current delegation depth.
            max_depth: Max allowed depth.
            ttl: Token TTL in seconds.
            allowed_callees: Agents this token may delegate to.

        Returns:
            A signed DelegatedTaskToken.
        """
        now = time.time()
        token_ttl = ttl if ttl is not None else self._default_ttl
        max_d = max_depth if max_depth is not None else self._max_depth

        # Attach delegation chain
        chain = list(parent_chain) if parent_chain else []
        hop = {
            "agent": sub,
            "action": "delegate",
            "scope": effective_scope,
            "timestamp": now,
        }
        chain.append(hop)

        # Attenuate scope
        if restriction:
            attenuated = attenuate_scope(effective_scope, restriction)
        else:
            attenuated = effective_scope

        token = DelegatedTaskToken(
            jti=str(uuid.uuid4()),
            iss=ISSUER,
            sub=sub,
            iat=now,
            exp=now + token_ttl,
            origin_agent=origin_agent,
            origin_tenant=origin_tenant,
            delegation_chain=chain,
            effective_scope=attenuated,
            attenuated_from=parent_scope or effective_scope,
            max_depth=max_d,
            depth=depth,
            allowed_callees=allowed_callees,
            task_id=task_id,
        )
        return token

    def persist_token_hash(self, token: DelegatedTaskToken) -> None:
        """Store token hash for replay prevention."""
        token_hash = self._hash_token_identifier(token.jti)
        with self._lock:
            self._engine.execute(
                """INSERT OR IGNORE INTO delegation_tokens
                   (jti, task_id, sub, origin_agent, scope, depth, expires_at, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    token.jti,
                    token.task_id,
                    token.sub,
                    token.origin_agent,
                    token.effective_scope,
                    token.depth,
                    int(token.exp),
                    time.time(),
                ),
            )
            self._engine.commit()

    # ------------------------------------------------------------------
    # Verify
    # ------------------------------------------------------------------

    def verify_delegation_token(
        self,
        token_str: str,
        task_id: str,
        agent_id: str,
        *,
        consume: bool = False,
    ) -> "DelegatedTaskToken":
        """Verify a delegation token (and optionally consume it).

        Args:
            token_str: The delegation token (full JWT or jti UUID).
            task_id: Expected task binding.
            agent_id: Expected agent subject.
            consume: If True, also mark the token as used (replay protection).
                     Set to True only when the agent actually claims the task,
                     NOT during periodic dispatcher pre-dispatch checks.

        Raises:
            TokenInvalidError: Token not found in DB.
            TokenExpiredError: Token has expired.
            TokenTaskMismatchError: Token bound to different task.
            TokenSubjectMismatchError: Token bound to different agent.
            TokenReplayError: Token jti already used (replay).
        """
        # Detect mode: full JWT (contains '.') vs jti lookup (UUID, no dots)
        if "." not in token_str:
            # Database lookup mode - dispatcher stores only the jti UUID
            with self._lock:
                row = self._engine.execute(
                    "SELECT jti, sub, task_id, used_at, expires_at "
                    "FROM delegation_tokens WHERE jti=?",
                    (token_str,),
                ).fetchone()
                if row is None:
                    raise TokenInvalidError(
                        f"Delegation token jti='{token_str}' not found"
                    )

                jti = row["jti"]
                stored_sub = row["sub"]
                stored_task_id = row["task_id"]
                used_at = row.get("used_at")
                expires_at = row.get("expires_at")

                # Check expiry
                expires_at_float = float(expires_at) if expires_at is not None else None
                if expires_at_float is not None and expires_at_float < time.time():
                    raise TokenExpiredError(
                        f"Delegation token expired (exp={expires_at})"
                    )

                # Check task binding
                if stored_task_id != task_id:
                    raise TokenTaskMismatchError(
                        f"Token bound to task '{stored_task_id}', expected '{task_id}'"
                    )

                # Check subject (empty sub = unassigned at mint time = allow any agent)
                if stored_sub and stored_sub != agent_id:
                    raise TokenSubjectMismatchError(
                        f"Token sub='{stored_sub}', claiming agent='{agent_id}'"
                    )

                # Replay check
                if used_at is not None:
                    raise TokenReplayError(
                        f"Delegation token jti='{jti}' already used"
                    )

                # Mark as used (only when consume=True - actual claim, not pre-dispatch check)
                if consume:
                    self._engine.execute(
                        "UPDATE delegation_tokens SET used_at=? WHERE jti=? AND used_at IS NULL",
                        (time.time(), jti),
                    )
                    self._engine.commit()

            # Return a minimal token (the caller doesn't use the return fields)
            return DelegatedTaskToken(
                jti=token_str,
                sub=agent_id,
                task_id=task_id,
            )

        # Full JWT verification path (original)
        token = DelegatedTaskToken.from_jwt(token_str, self._public_key)

        # Check expiry
        if token.exp < time.time():
            raise TokenExpiredError(
                f"Delegation token expired (exp={token.exp})"
            )

        # Check task binding
        if token.task_id != task_id:
            raise TokenTaskMismatchError(
                f"Token bound to task '{token.task_id}', expected '{task_id}'"
            )

        # Check subject (empty sub = unassigned at mint time = allow any agent)
        if token.sub and token.sub != agent_id:
            raise TokenSubjectMismatchError(
                f"Token sub='{token.sub}', claiming agent='{agent_id}'"
            )

        # Replay check - verify jti has not been used
        with self._lock:
            result = self._engine.execute(
                "SELECT used_at FROM delegation_tokens WHERE jti=? AND used_at IS NOT NULL",
                (token.jti,),
            )
            if result.fetchone():
                raise TokenReplayError(
                    f"Delegation token jti='{token.jti}' already used"
                )
            # Mark as used (only when consume=True)
            if consume:
                self._engine.execute(
                    "UPDATE delegation_tokens SET used_at=? WHERE jti=? AND used_at IS NULL",
                    (time.time(), token.jti),
                )
                self._engine.commit()

        return token

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _hash_token_identifier(token_str: str) -> str:
        return hashlib.sha256(token_str.encode("utf-8")).hexdigest()

    def get_public_key(self) -> str:
        return self._public_key
