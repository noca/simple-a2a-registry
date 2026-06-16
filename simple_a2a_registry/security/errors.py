"""Security Harness — exception hierarchy.

All Security Harness exceptions inherit from ``SecurityHarnessError``.
"""
from __future__ import annotations


class SecurityHarnessError(Exception):
    """Base exception for all Security Harness errors."""


# ── APE (Authorization Policy Engine) ──────────────────────────────────


class APEError(SecurityHarnessError):
    """Base APE exception."""


class AuthenticationError(APEError):
    """Caller identity could not be verified."""


class AuthorizationError(APEError):
    """Caller is not authorised for the requested operation."""


class ScopeError(APEError):
    """Caller does not hold the required scope."""


class TenantMismatchError(APEError):
    """Caller and assignee belong to different tenants."""


class AgentNotFoundError(APEError):
    """Agent or assignee not found in the registry."""


class AgentDisabledError(APEError):
    """Target agent is disabled."""


class DelegationDepthExceeded(APEError):
    """Delegation chain exceeds max_depth."""


# ── DTM (Delegation Token Manager) ────────────────────────────────────


class DTMError(SecurityHarnessError):
    """Base DTM exception."""


class TokenExpiredError(DTMError):
    """The delegation token has expired."""


class TokenInvalidError(DTMError):
    """The delegation token is malformed or signature invalid."""


class TokenTaskMismatchError(DTMError):
    """The delegation token is bound to a different task."""


class TokenSubjectMismatchError(DTMError):
    """The delegation token's sub does not match the claiming agent."""


class TokenReplayError(DTMError):
    """The delegation token jti has already been used."""


# ── AuthzDecision (used across APE + Plugin hooks) ────────────────────


from enum import Enum
from dataclasses import dataclass
from typing import Optional


class AuthzOutcome(Enum):
    ACCEPT = "accept"
    REJECT = "reject"
    DEFER = "defer"  # hand back to default policy


@dataclass
class AuthzDecision:
    outcome: AuthzOutcome
    reason: str = ""
    override_scope: Optional[str] = None