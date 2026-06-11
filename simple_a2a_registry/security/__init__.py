"""Security Harness — Authorization Policy Engine + Delegated Token Manager + Provenance + Events."""
from __future__ import annotations

from simple_a2a_registry.security.errors import (
    APEError,
    AuthenticationError,
    AuthorizationError,
    AuthzDecision,
    AuthzOutcome,
    AgentNotFoundError,
    AgentDisabledError,
    DelegationDepthExceeded,
    DTMError,
    ScopeError,
    SecurityHarnessError,
    TenantMismatchError,
    TokenExpiredError,
    TokenInvalidError,
    TokenReplayError,
    TokenSubjectMismatchError,
    TokenTaskMismatchError,
)

from simple_a2a_registry.security.ape import (
    APEConfig,
    AuthorizationPolicyEngine,
    CallerIdentity,
    CheckpointResult,
)

from simple_a2a_registry.security.dtm import (
    DelegatedTaskToken,
    DelegatedTokenManager,
    attenuate_scope,
)

from simple_a2a_registry.security.events import (
    SecurityEvent,
    SecurityEventStore,
    SecurityEventType,
)

from simple_a2a_registry.security.pt import (
    ProvenanceChain,
    ProvenanceHop,
    ProvenanceTracker,
)

__all__ = [
    "APEError", "AuthenticationError", "AuthorizationError",
    "AuthzDecision", "AuthzOutcome",
    "AgentNotFoundError", "AgentDisabledError",
    "DelegationDepthExceeded", "DTMError", "ScopeError",
    "SecurityHarnessError", "TenantMismatchError",
    "TokenExpiredError", "TokenInvalidError", "TokenReplayError",
    "TokenSubjectMismatchError", "TokenTaskMismatchError",
    "APEConfig", "AuthorizationPolicyEngine", "CallerIdentity",
    "CheckpointResult",
    "DelegatedTaskToken", "DelegatedTokenManager", "attenuate_scope",
    "SecurityEvent", "SecurityEventStore", "SecurityEventType",
    "ProvenanceChain", "ProvenanceHop", "ProvenanceTracker",
]