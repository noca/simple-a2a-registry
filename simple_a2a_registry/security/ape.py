"""Authorization Policy Engine (APE) — P0 mandatory module.

Core enforcement layer that checks every task operation at the route
entry point.  Designed to work with three-phase migration:
``audit`` → ``warn`` → ``enforce``.

Checkpoints (see PRD §4.1.2):
  1. POST /v2/tasks — create task
  2. POST /v2/tasks/{id}/claim — claim task
  3. POST /v2/tasks/{id}/complete — complete task
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from simple_a2a_registry.security.errors import (
    AgentDisabledError,
    AgentNotFoundError,
    AuthenticationError,
    AuthorizationError,
    AuthzDecision,
    AuthzOutcome,
    DelegationDepthExceeded,
    ScopeError,
    SecurityHarnessError,
    TenantMismatchError,
)
from simple_a2a_registry.security.events import SecurityEventStore, SecurityEventType
from simple_a2a_registry.security.dtm import DelegatedTokenManager

logger = logging.getLogger("a2a_registry.security.ape")


@dataclass
class APEConfig:
    """Configuration for the APE."""

    mode: str = "enforce"          # audit | warn | enforce
    default_delegation_policy: str = "open"  # open | restricted
    max_delegation_depth: int = 10


@dataclass
class CallerIdentity:
    """Resolved caller identity from the JWT token."""

    agent_id: str
    tenant: str = ""
    scope: str = ""
    token_payload: Dict[str, Any] = field(default_factory=dict)


@dataclass
class CheckpointResult:
    """Result of a single APE checkpoint evaluation."""

    allowed: bool
    reason: str = ""
    effective_scope: str = ""
    recorded_event: bool = False
    response_headers: Dict[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# APE
# ---------------------------------------------------------------------------


class AuthorizationPolicyEngine:
    """Central enforcement engine for all security checkpoints.

    Uses a plugin hook dispatcher (if provided) for extensible decisions.
    """

    def __init__(
        self,
        config: APEConfig,
        dtm: DelegatedTokenManager,
        event_store: SecurityEventStore,
        registry_store: Any,  # Store from store.py — for agent lookups
        task_store: Any = None,  # TaskStore — for task lookups
        plugin_registry: Any = None,  # PluginRegistry — for hook calls
    ) -> None:
        self.config = config
        self.dtm = dtm
        self.event_store = event_store
        self.registry_store = registry_store
        self.task_store = task_store
        self.plugin_registry = plugin_registry

    # ------------------------------------------------------------------
    # Checkpoint: Task Create
    # ------------------------------------------------------------------

    async def check_task_create(
        self,
        caller: CallerIdentity,
        task_data: Dict[str, Any],
        delegation_chain: Optional[List[Dict[str, Any]]] = None,
    ) -> CheckpointResult:
        """Evaluate all security checks for task creation.

        Returns a ``CheckpointResult`` that the caller either honours
        (enforce), warns about (warn), or logs (audit).
        """
        assignee = task_data.get("assignee", "")
        parent_id = task_data.get("parent_id") or None

        # ── 1. Caller identity ──────────────────────────────────────
        if not caller.agent_id:
            return self._deny("caller identity is empty", caller, assignee)
        if caller.agent_id == "anonymous":
            return self._deny("anonymous caller not allowed", caller, assignee)

        is_human = caller.agent_id.startswith("user:")

        # ── 2. Caller must be registered (agents only) ──────────────
        if is_human:
            pass  # Human callers (user:admin) are trusted by scope check below
        else:
            caller_card = self._get_agent(caller.agent_id, caller.tenant)
            if caller_card is None:
                err = f"caller '{caller.agent_id}' not registered"
                return self._deny(err, caller, assignee, err_type="AGENT_NOT_FOUND")
            if caller_card.get("disabled", 0) == 1:
                err = f"caller '{caller.agent_id}' is disabled"
                return self._deny(err, caller, assignee, err_type="AGENT_DISABLED")

        # ── 3. Assignee exists and active ───────────────────────────
        if assignee:
            assignee_card = self._get_agent(assignee, caller.tenant)
            if assignee_card is None:
                err = f"assignee '{assignee}' not found"
                return self._deny(err, caller, assignee, err_type="AGENT_NOT_FOUND")
            if assignee_card.get("disabled", 0) == 1:
                err = f"assignee '{assignee}' is disabled"
                return self._deny(err, caller, assignee, err_type="AGENT_DISABLED")

        # ── 4. Scope check ──────────────────────────────────────────
        token_scopes = set(caller.scope.split()) if caller.scope else set()
        if "task:write" not in token_scopes and "registry:admin" not in token_scopes:
            err = "insufficient scope: task:write required"
            return self._deny(err, caller, assignee, err_type="SCOPE_DENIED")

        # ── 5. Tenant consistency (agents only) ─────────────────────
        if not is_human and caller.tenant and assignee:
            assignee_card = self._get_agent(assignee, caller.tenant)
            if assignee_card:
                assignee_tenant = assignee_card.get("tenant_id", "")
                if caller.tenant and assignee_tenant and caller.tenant != assignee_tenant:
                    err = f"tenant mismatch: caller='{caller.tenant}', assignee='{assignee_tenant}'"
                    return self._deny(err, caller, assignee, err_type="TENANT_MISMATCH")

        # ── 6. Delegation depth ─────────────────────────────────────
        if parent_id:
            parent_task = self._get_task(parent_id)
            if parent_task:
                parent_depth = getattr(parent_task, "delegation_depth", 0) or 0
                if parent_depth >= self.config.max_delegation_depth:
                    err = f"delegation depth exceeded: {parent_depth} >= {self.config.max_delegation_depth}"
                    return self._deny(err, caller, assignee, err_type="DELEGATION_DEPTH_EXCEEDED")

        # ── 7. Agent-to-Agent authorization (agents only) ───────────
        if not is_human and assignee and caller.agent_id != assignee:
            authz_violation = self._check_agent_authz(caller.agent_id, assignee, token_scopes)
            if authz_violation:
                return self._deny(authz_violation, caller, assignee, err_type="AUTHORIZATION_DENIED")

        # ── 8. Plugin hook ──────────────────────────────────────────
        if self.plugin_registry is not None:
            try:
                decisions = await self._fire_authz_hooks("authorize_task_create", caller, task_data, delegation_chain)
                for d in decisions:
                    if d.outcome == AuthzOutcome.REJECT:
                        return self._deny(
                            d.reason or "plugin rejected task creation",
                            caller, assignee, err_type="PLUGIN_REJECT",
                        )
                    elif d.outcome == AuthzOutcome.ACCEPT:
                        break
            except Exception as exc:
                logger.exception("Plugin authorize_task_create hook failed: %s", exc)

        # ── ALL CHECKS PASSED ───────────────────────────────────────
        return self._allow(
            caller, assignee,
            scope=caller.scope,
            reason="all checks passed",
        )

    # ------------------------------------------------------------------
    # Checkpoint: Task Claim
    # ------------------------------------------------------------------

    async def check_task_claim(
        self,
        caller: CallerIdentity,
        task_id: str,
        delegation_token_str: Optional[str] = None,
    ) -> CheckpointResult:
        """Evaluate security checks for task claiming."""
        task = self._get_task(task_id)
        if task is None:
            return self._deny("task not found", caller, task_id, task_id=task_id)

        assignee = getattr(task, "assignee", "")

        # ── 1. Caller is the assignee ───────────────────────────────
        if caller.agent_id != assignee:
            return self._deny(
                f"caller '{caller.agent_id}' is not the assignee '{assignee}'",
                caller, task_id, task_id=task_id,
            )

        # ── 2. Validate delegation token ────────────────────────────
        if delegation_token_str:
            try:
                token = self.dtm.verify_delegation_token(
                    delegation_token_str, task_id, caller.agent_id,
                )
                # Store effective scope from token
            except SecurityHarnessError as e:
                return self._deny(str(e), caller, task_id, task_id=task_id)
        elif self.config.default_delegation_policy == "restricted":
            err = "delegation token required but not provided"
            return self._deny(err, caller, task_id,
                              err_type="SECURITY_VIOLATION", task_id=task_id)
        else:
            # open mode — allow without token, record event
            self.event_store.record(
                event_type=SecurityEventType.SECURITY_VIOLATION.value,
                actor=caller.agent_id,
                target=task_id,
                decision="allow",
                tenant=caller.tenant,
                reason="delegation token not provided (open mode)",
                scope_used=caller.scope,
                task_id=task_id,
            )

        # ── 3. Plugin hook ──────────────────────────────────────────
        if self.plugin_registry is not None:
            try:
                decisions = await self._fire_authz_hooks(
                    "authorize_task_claim", caller, task, delegation_token_str,
                )
                for d in decisions:
                    if d.outcome == AuthzOutcome.REJECT:
                        return self._deny(
                            d.reason or "plugin rejected task claim",
                            caller, task_id, err_type="PLUGIN_REJECT",
                            task_id=task_id,
                        )
            except Exception as exc:
                logger.exception("Plugin authorize_task_claim hook failed: %s", exc)

        return self._allow(caller, task_id, reason="claim allowed", task_id=task_id)

    # ------------------------------------------------------------------
    # Checkpoint: Task Complete
    # ------------------------------------------------------------------

    async def check_task_complete(
        self,
        caller: CallerIdentity,
        task_id: str,
        claim_lock: Optional[str] = None,
    ) -> CheckpointResult:
        """Evaluate security checks for task completion.

        Note: claim_lock matching is handled by the store layer.
        This checkpoint verifies caller identity is consistent.
        """
        task = self._get_task(task_id)
        if task is None:
            return self._deny("task not found", caller, task_id, task_id=task_id)

        # Verify caller is the claimer (claim_lock prefix check)
        current_lock = getattr(task, "claim_lock", None)
        if current_lock and claim_lock and current_lock != claim_lock:
            return self._deny(
                f"claim_lock mismatch: caller's lock does not match",
                caller, task_id, task_id=task_id,
            )

        return self._allow(caller, task_id, reason="complete allowed", task_id=task_id)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _deny(
        self,
        reason: str,
        caller: CallerIdentity,
        target: str,
        *,
        err_type: str = "AUTHORIZATION_DENIED",
        task_id: Optional[str] = None,
    ) -> CheckpointResult:
        """Apply mode and return appropriate CheckpointResult."""
        mode = self.config.mode

        if mode == "audit":
            # Log but don't block
            self._record_event(err_type, caller, target, decision="allow",
                               reason=f"{reason} (audit mode)", task_id=task_id)
            return CheckpointResult(allowed=True, reason=reason,
                                    recorded_event=True)

        if mode == "warn":
            # Log and warn, but don't block
            self._record_event(err_type, caller, target, decision="allow",
                               reason=f"{reason} (warn mode)", task_id=task_id)
            return CheckpointResult(
                allowed=True, reason=reason,
                recorded_event=True,
                response_headers={"X-Security-Warning": reason},
            )

        # enforce mode — block
        self._record_event(err_type, caller, target, decision="deny",
                           reason=reason, task_id=task_id)
        return CheckpointResult(allowed=False, reason=reason,
                                recorded_event=True)

    def _allow(
        self,
        caller: CallerIdentity,
        target: str,
        *,
        scope: str = "",
        reason: str = "",
        task_id: Optional[str] = None,
    ) -> CheckpointResult:
        """Record allow and return positive result."""
        self._record_event("AUTHORIZATION_ALLOWED", caller, target,
                           decision="allow", reason=reason, task_id=task_id)
        return CheckpointResult(allowed=True, reason=reason,
                                effective_scope=scope or caller.scope,
                                recorded_event=True)

    def _record_event(
        self,
        event_type: str,
        caller: CallerIdentity,
        target: str,
        decision: str,
        reason: str,
        *,
        task_id: Optional[str] = None,
    ) -> None:
        try:
            self.event_store.record(
                event_type=event_type,
                actor=caller.agent_id,
                target=target,
                decision=decision,
                tenant=caller.tenant,
                reason=reason,
                scope_used=caller.scope,
                task_id=task_id,
            )
        except Exception:
            logger.exception("Failed to record security event")

    def _get_agent(self, agent_id: str, tenant: str = "") -> Optional[Dict[str, Any]]:
        """Look up an agent in the registry store."""
        try:
            return self.registry_store.get_agent(agent_id, tenant=tenant or None)
        except Exception:
            logger.exception("Failed to look up agent '%s'", agent_id)
            return None

    def _get_task(self, task_id: str) -> Any:
        """Look up a task via the task store."""
        if self.task_store is None:
            return None
        try:
            return self.task_store.get_task(task_id)
        except Exception:
            logger.exception("Failed to look up task '%s'", task_id)
            return None

    def _check_agent_authz(
        self,
        source: str,
        target: str,
        token_scopes: set,
    ) -> Optional[str]:
        """Check agent-to-agent authorisation matrix.

        Returns a violation message if denied, or None if allowed.
        """
        if self.config.default_delegation_policy == "open":
            return None  # open mode — allow all

        try:
            result = self.registry_store.execute(
                "SELECT allowed_actions, scope_restriction, expires_at "
                "FROM agent_authorizations "
                "WHERE source_agent_id=? AND target_agent_id=?",
                (source, target),
            )
            row = result.fetchone()
            if row is None:
                return "no authorization record found (restricted policy)"

            # Check expiry
            if row.get("expires_at"):
                import time
                expires = row["expires_at"]
                if isinstance(expires, (int, float)) and time.time() > expires:
                    return "authorization record has expired"

            # Check allowed_actions
            actions_json = row.get("allowed_actions", "[]")
            try:
                allowed = json.loads(actions_json)
            except (json.JSONDecodeError, TypeError):
                allowed = []
            if "*" not in allowed:
                # Check if any token scope is covered
                for s in token_scopes:
                    if s in allowed:
                        break
                else:
                    return f"no allowed action covers token scopes {token_scopes}"

        except Exception as exc:
            logger.exception("Agent authz matrix check failed: %s", exc)
            # Fail closed on DB error
            return f"authorization check failed: {exc}"

        return None  # allowed

    async def _fire_authz_hooks(
        self, hook_name: str, *args, **kwargs,
    ) -> List[AuthzDecision]:
        """Fire an authorisation hook across all loaded plugins.

        Returns a list of AuthzDecision from plugins that implement
        the hook.  Empty list if no plugin implements it.
        """
        decisions: List[AuthzDecision] = []
        if self.plugin_registry is None:
            return decisions

        for name, plugin in self.plugin_registry.plugins.items():
            hook_fn = getattr(plugin, hook_name, None)
            if hook_fn is None:
                continue
            try:
                result = await hook_fn(*args, **kwargs)
                if isinstance(result, AuthzDecision):
                    decisions.append(result)
            except Exception as exc:
                logger.exception("Plugin '%s' hook '%s' failed: %s",
                                 name, hook_name, exc)
        return decisions