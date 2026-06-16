"""TaskEnvelope builder — construct envelopes for WS dispatch (§6 SCN-02, SCN-03).

Provides ``build_envelope()`` which takes a kanban ``Task`` object and an
optional ``DelegatedTaskToken``, produces a fully populated ``TaskEnvelope``
ready for JSON serialization and WebSocket delivery.

Three dispatch workflows rely on this builder:

1. **V1 HTTP dispatch** (``RegistryHandler.handle_dispatch``) — clients call
   ``POST /v1/agents/{agent_id}/dispatch`` and the result is pushed via WS.
2. **V2 Dispatcher WS dispatch** (``Dispatcher._dispatch_via_ws``) — the
   background dispatcher polls the kanban board and pushes ready tasks.
3. **Reconnection pending dispatch** (``_maybe_dispatch_pending``) — when an
   agent reconnects, any BLOCKED→RUNNING tasks are re-enveloped and sent.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

from simple_a2a_registry.orchestration.contract import (
    InteractionMode,
    OutputContract,
    SecurityContext,
    TaskEnvelope,
)
from simple_a2a_registry.orchestration.models import Task
from simple_a2a_registry.security.dtm import (
    DelegatedTaskToken,
)

logger = logging.getLogger("a2a_registry.orchestration.envelope")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Default deadline for tasks that don't carry a DTM (milliseconds from epoch).
_DEFAULT_DEADLINE_MS: int = 0

_DEFAULT_INTERACTION_MODE: InteractionMode = InteractionMode.TASK


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_envelope(
    task: Task,
    security_context: Optional[SecurityContext] = None,
    task_dict: Optional[Dict[str, Any]] = None,
    *,
    interaction_mode: Optional[InteractionMode] = None,
) -> TaskEnvelope:
    """Build a ``TaskEnvelope`` from a kanban ``Task`` object.

    Args:
        task:            The kanban ``Task`` being dispatched.
        security_context:  Pre-built ``SecurityContext`` (e.g. from DTM
                          attenuation).  If ``None``, one is synthesised
                          from the task's own provenance fields.
        task_dict:       Optional extra fields that may be present in a V1
                         dispatch (e.g. ``query``, ``sessionId``) — merged
                         into ``input``.
        interaction_mode:  Override the interaction mode.  Defaults to
                          ``InteractionMode.TASK``.

    Returns:
        A fully populated ``TaskEnvelope`` ready for ``to_dict()`` +
        JSON serialisation.
    """
    # ---------- security context ----------
    if security_context is None:
        security_context = _synthesise_security_context(task)
    elif security_context is not None and isinstance(security_context, dict):
        security_context = SecurityContext.from_dict(security_context)

    # ---------- interaction mode ----------
    mode = interaction_mode or _DEFAULT_INTERACTION_MODE

    # ---------- input payload ----------
    input_payload: Dict[str, Any] = {}
    if task.body:
        try:
            parsed = json.loads(task.body)
            if isinstance(parsed, dict):
                input_payload = parsed
            else:
                input_payload["body"] = parsed
        except (json.JSONDecodeError, TypeError):
            input_payload["body"] = task.body

    # Merge extra V1 fields — normalize sessionId → session_id
    if task_dict:
        for extra_key in ("query", "sessionId", "session_id", "title"):
            if extra_key in task_dict:
                target_key = "session_id" if extra_key == "sessionId" else extra_key
                input_payload[target_key] = task_dict[extra_key]

    # ---------- output contract ----------
    output_contract = OutputContract(required_fields=[])

    # ---------- skill ----------
    skill = task.assignee or ""
    # Try to extract skill from task body or metadata
    if task.metadata:
        try:
            meta = json.loads(task.metadata) if isinstance(task.metadata, str) else task.metadata
            if isinstance(meta, dict) and "skill" in meta:
                skill = meta["skill"]
        except (json.JSONDecodeError, TypeError):
            pass

    # ---------- tenant ----------
    tenant_id = task.tenant or ""

    # ---------- workspace URI ----------
    workspace_uri: Optional[str] = None
    if task.workspace_path:
        ws_path = task.workspace_path.lstrip("/")
        workspace_uri = f"workspace://{tenant_id}/{ws_path}" if tenant_id else task.workspace_path

    # ---------- input schema (from task metadata) ----------
    input_schema: Optional[Dict[str, Any]] = None
    if task.metadata:
        try:
            meta = json.loads(task.metadata) if isinstance(task.metadata, str) else task.metadata
            if isinstance(meta, dict) and "input_schema" in meta:
                input_schema = meta["input_schema"]
        except (json.JSONDecodeError, TypeError):
            pass

    return TaskEnvelope(
        task_id=task.id,
        interaction_mode=mode,
        skill=skill,
        input_schema=input_schema,
        input=input_payload,
        output_contract=output_contract,
        security_context=security_context,
        tenant_id=tenant_id,
        workspace_uri=workspace_uri,
    )


def build_envelope_from_dtm(
    task: Task,
    dtm_token: DelegatedTaskToken,
    task_dict: Optional[Dict[str, Any]] = None,
    *,
    interaction_mode: Optional[InteractionMode] = None,
) -> TaskEnvelope:
    """Build a ``TaskEnvelope`` from a kanban ``Task`` + DTM-delegated token.

    The DTM ``DelegatedTaskToken`` provides the security context fields
    (``effective_scope``, ``delegation_depth``, etc.) after attenuation.

    Args:
        task:      The kanban ``Task`` being dispatched.
        dtm_token: The DTM ``DelegatedTaskToken`` (post-attenuation).
        task_dict:  Optional extra V1 dispatch fields merged into ``input``.
        interaction_mode:  Override the interaction mode.

    Returns:
        A fully populated ``TaskEnvelope``.
    """
    security_context = SecurityContext(
        effective_scope=dtm_token.effective_scope,
        delegation_depth=dtm_token.depth,
        deadline_ms=int(dtm_token.exp * 1000) if dtm_token.exp else _DEFAULT_DEADLINE_MS,
        provenance_chain_id=_derive_provenance_chain(dtm_token),
    )
    return build_envelope(
        task=task,
        security_context=security_context,
        task_dict=task_dict,
        interaction_mode=interaction_mode,
    )


# ---------------------------------------------------------------------------
# Ingress security fence hook (placeholder — implemented in T6)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Module-level guardrail engine reference (wired by create_app in server.py)
# ---------------------------------------------------------------------------

_guardrail_engine: Optional[Any] = None


def set_guardrail_engine(engine: Any) -> None:
    """Set the module-level GuardrailEngine reference.

    Called by ``server.py create_app`` when the security harness is enabled.
    All subsequent ``check_ingress_security_fence`` calls use this engine
    instead of the placeholder.
    """
    global _guardrail_engine  # noqa: PLW0603
    _guardrail_engine = engine
    logger.info("GuardrailEngine wired into envelope module (T7)")


def get_guardrail_engine() -> Optional[Any]:
    """Return the current module-level GuardrailEngine reference."""
    return _guardrail_engine


# ---------------------------------------------------------------------------
# Ingress security fence — powered by GuardrailEngine when wired
# ---------------------------------------------------------------------------


async def check_ingress_security_fence(
    envelope: TaskEnvelope,
) -> bool:
    """Ingress security fence — invoked before sending the envelope via WS.

    When a ``GuardrailEngine`` has been wired (via ``set_guardrail_engine``),
    this runs ``check_inbound`` against the envelope input.  In enforce mode,
    a detected injection pattern returns ``False`` (block dispatch).  In warn
    mode, the dispatch proceeds but the warning header is not attached here
    (the caller is responsible for attaching response headers).

    When **no** GuardrailEngine is wired (legacy / dev mode), always returns
    ``True`` (backward-compatible placeholder behaviour).

    Returns:
        ``True`` if the fence passes (allow dispatch), ``False`` to block.
    """
    engine = _guardrail_engine
    if engine is None:
        logger.debug("Ingress security fence: ALLOW (no guardrail engine wired)")
        return True

    result = engine.check_inbound(
        input_data=envelope.input,
        actor=envelope.security_context.effective_scope if envelope.security_context else "anonymous",
        tenant=envelope.tenant_id,
        task_id=envelope.task_id,
    )
    if not result.allowed:
        logger.warning(
            "Ingress security fence: DENY (task=%s, reason=%s)",
            envelope.task_id, result.reason,
        )
        return False

    logger.debug("Ingress security fence: ALLOW (task=%s)", envelope.task_id)
    return True


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _synthesise_security_context(task: Task) -> SecurityContext:
    """Build a ``SecurityContext`` from task provenance fields.

    When no DTM is available (e.g. V1 HTTP dispatch without delegation),
    the task's own fields are used instead.
    """
    return SecurityContext(
        effective_scope=task.effective_scope or "",
        delegation_depth=task.delegation_depth or 0,
        deadline_ms=_DEFAULT_DEADLINE_MS,
        provenance_chain_id=task.provenance_chain_id or "",
    )


def _derive_provenance_chain(dtm_token: DelegatedTaskToken) -> str:
    """Derive a provenance chain id from the DTM delegation chain.

    Uses the first hop's metadata if available, otherwise falls back to
    ``dtm_token.jti``.
    """
    if dtm_token.delegation_chain and len(dtm_token.delegation_chain) > 0:
        first = dtm_token.delegation_chain[0]
        return first.get("jti", dtm_token.jti)
    return dtm_token.jti