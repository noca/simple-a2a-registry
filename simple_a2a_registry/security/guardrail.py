"""Execution-time Security Guardrails — Inbound injection detection + Outbound data sanitization.

Implements §6 SCN-02.3, SCN-04.2, AC-07, AC-08, D3 resolution.

Architecture
-----------
Inbound Guardrail (check_inbound):
    - Reuses APE's audit / warn / enforce three-mode configuration (D3).
    - Detects prompt-injection / jailbreak patterns in task input.
    - enforce mode → deny submission + record security event (AC-07).
    - warn   mode → allow submission + tag warning header.
    - audit  mode → record event only.

Outbound Guardrail (sanitize_output):
    - Scans agent output for sensitive patterns (keys, tokens, passwords).
    - On match → redact to "***" before persistence and delivery (AC-08).
    - Preserves output structure integrity.

Constraints
-----------
- Transparent to the agent (INV-7) — agent does not know guardrails exist.
- Must be measurable for performance overhead (NFR-2).
- Guardrails MUST NOT impose additional cognitive burden on the agent (R2 risk).
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from simple_a2a_registry.security.events import SecurityEventStore, SecurityEventType

logger = logging.getLogger("a2a_registry.security.guardrail")

# ---------------------------------------------------------------------------
# Sensitive pattern definitions for outbound sanitisation
# ---------------------------------------------------------------------------

SENSITIVE_PATTERNS: List[re.Pattern] = [
    # AWS / generic access keys — 20 alphanumeric chars
    re.compile(r"(?<![A-Za-z0-9+/=])(AKIA[0-9A-Z]{16})(?![A-Za-z0-9])"),
    # Generic bearer tokens (JWT-like, base64-url strings of 20+ chars)
    re.compile(r"(?i)(?:bearer|token|api[_-]?key|secret)\s+([A-Za-z0-9_\-\.]{20,})(?:\s|$|\"|')"),
    # Private keys — PEM armoured blocks
    re.compile(r"-----BEGIN\s+(?:RSA\s+)?PRIVATE\s+KEY-----"),
    # Password / credential patterns in JSON values
    re.compile(r"(?i)(?:\"password\"|\"passwd\"|\"secret\")\s*:\s*\"[^\"]{4,}\""),
    # Connection strings — e.g. mysql://user:***@host
    re.compile(r"(?i)(?:mysql|postgres|redis|mongodb|amqp|rabbitmq)://[^:]+:[^@]+@"),
    # Generic SSH private key header
    re.compile(r"-----BEGIN\s+OPENSSH\s+PRIVATE\s+KEY-----"),
]

# ---------------------------------------------------------------------------
# Sensitive key names — values under these keys are unconditionally redacted
# ---------------------------------------------------------------------------

SENSITIVE_KEY_NAMES: List[re.Pattern] = [
    re.compile(r"(?i)^(?:password|passwd|secret|api[_-]?(?:key|secret)|token|access[_-]?key)$"),
]

# ---------------------------------------------------------------------------
# Suspicious input patterns for inbound injection detection
# ---------------------------------------------------------------------------

INJECTION_PATTERNS: List[re.Pattern] = [
    # Direct instruction override attempts
    re.compile(r"(?i)\bignore\s+(?:all\s+)?(?:previous|above|prior)\s+instructions\b"),
    re.compile(r"(?i)\bdisregard\s+(?:all\s+)?(?:previous|above)\b"),
    # System prompt extraction attempts
    re.compile(r"(?i)(?:what'?s|show|print|output|reveal)\s+(?:your\s+)?(?:system\s+)?prompt"),
    re.compile(r"(?i)(?:pretend|act)\s+(?:as\s+)?(?:if\s+)?you\s+are\s+(?:the\s+)?admin"),
    # Role-play / DAN-type jailbreak
    re.compile(r"(?i)(?:do\s+anything\s+now|DAN|jail\s*(?:break|broke))\b"),
    # Instruction injection via delimiter
    re.compile(r"(?i)(?:new\s+)?instructions?\s*[:：][^。.]+?(?:ignore|override|forget)"),
    # Prompt leaking / delimiters
    re.compile(r"(?i)\[system\]|\[instruction\]|<\|im_start\|>|<\|im_end\|>"),
    # Attempt to chain command execution
    re.compile(r"(?i)\bexecute\s+(?:shell|command|system|cmd)\b"),
    # Template injection
    re.compile(r"\{\{.*?\}\}|\{\%|\%\}"),
]


# ---------------------------------------------------------------------------
# GuardrailResult
# ---------------------------------------------------------------------------


@dataclass
class GuardrailResult:
    """Result of a guardrail evaluation.

    Attributes:
        allowed:    True if the operation is permitted.
        reason:     Human-readable explanation when blocked/warned.
        severity:   One of ``"info"``, ``"warn"``, ``"block"`` — or None
                    when no violation was detected.
        matched_pattern: The specific pattern that triggered (for diagnostics).
        response_headers: Extra headers to attach to the HTTP response
                          (used in warn mode for X-Security-Warning).
    """
    allowed: bool = True
    reason: Optional[str] = None
    severity: Optional[str] = None  # "info" / "warn" / "block"
    matched_pattern: Optional[str] = None
    response_headers: Dict[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Sanitisation report
# ---------------------------------------------------------------------------


@dataclass
class SanitizeReport:
    """Report of what was sanitised in an outbound payload.

    Attributes:
        sanitised:     True if at least one redaction was applied.
        field_paths:   JSON-path-like locations where redactions occurred.
        count:         Total number of redactions applied.
    """
    sanitised: bool = False
    field_paths: List[str] = field(default_factory=list)
    count: int = 0


# ---------------------------------------------------------------------------
# GuardrailEngine
# ---------------------------------------------------------------------------


class GuardrailEngine:
    """Execution-time security guardrail engine.

    Provides two independent guardrails:

    1. **Inbound (check_inbound)** — scans task input for prompt-injection
       and jailbreak patterns before the WS dispatch submits it to an agent.

    2. **Outbound (sanitize_output)** — recursively walks agent output,
       redacts sensitive data (keys, tokens, passwords) by replacing
       matched values with ``"***"`` before persistence and delivery.

    Both guardrails record security events via the shared
    :class:`SecurityEventStore`.
    """

    def __init__(
        self,
        event_store: SecurityEventStore,
        mode: str = "warn",
    ) -> None:
        """
        Args:
            event_store:   Shared SecurityEventStore for audit logging.
            mode:          Default enforcement mode — ``"audit"``,
                           ``"warn"``, or ``"enforce"``.
        """
        self.event_store = event_store
        self.mode = mode

    # ------------------------------------------------------------------
    # Inbound Guardrail — Injection Detection
    # ------------------------------------------------------------------

    def check_inbound(
        self,
        input_data: Dict[str, Any],
        mode: Optional[str] = None,
        *,
        actor: str = "anonymous",
        tenant: str = "",
        task_id: Optional[str] = None,
    ) -> GuardrailResult:
        """Scan *input_data* for prompt-injection / jailbreak patterns.

        Args:
            input_data:  The raw task input payload (a JSON-like dict).
            mode:        Enforcement mode override.  Falls back to the
                         engine-level ``self.mode`` if not provided.
            actor:       Who is submitting the input (for event logging).
            tenant:      Tenant namespace (for event logging).
            task_id:     Optional task ID for event correlation.

        Returns:
            A :class:`GuardrailResult`.  The caller must honour
            ``allowed`` (enforce mode), attach ``response_headers``
            to the response (warn mode), or simply log (audit mode).
        """
        effective_mode = mode or self.mode

        # Flatten input_data into a single text blob for pattern matching
        text_blob = self._flatten_to_text(input_data)

        if not text_blob:
            return GuardrailResult(allowed=True)

        # Scan for injection patterns
        for pattern in INJECTION_PATTERNS:
            match = pattern.search(text_blob)
            if match:
                matched_text = match.group().strip()
                reason = f"Injection pattern detected: '{matched_text[:60]}'"
                return self._apply_mode(
                    effective_mode,
                    reason,
                    matched_pattern=matched_text[:80],
                    actor=actor,
                    tenant=tenant,
                    task_id=task_id,
                )

        return GuardrailResult(allowed=True)

    # ------------------------------------------------------------------
    # Outbound Guardrail — Sensitive Data Sanitisation
    # ------------------------------------------------------------------

    def sanitize_output(
        self,
        output: Dict[str, Any],
        *,
        actor: str = "anonymous",
        tenant: str = "",
        task_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Recursively redact sensitive data from *output*.

        Scans every string value in the output dict tree for sensitive
        patterns.  Matched substrings are replaced with ``"***"``.

        Returns:
            A **new** dict with redacted values.  The original *output*
            is not mutated.

        Records a ``DATA_EXFILTRATION_PREVENTED`` security event when
        redactions are applied (AC-08).
        """
        sanitised, new_output = self._redact_tree(output)

        if sanitised.sanitised:
            self.event_store.record(
                event_type=SecurityEventType.SECURITY_VIOLATION.value,
                actor=actor,
                target="output_sanitisation",
                decision="allow",
                tenant=tenant,
                reason=(
                    f"Sanitised {sanitised.count} sensitive pattern(s) "
                    f"in output fields: {', '.join(sanitised.field_paths)}"
                ),
                metadata={"field_paths": sanitised.field_paths, "count": sanitised.count},
                task_id=task_id,
            )
            logger.info(
                "Sanitised %d sensitive patterns in output for task '%s': %s",
                sanitised.count, task_id or "N/A", sanitised.field_paths,
            )

        return new_output

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _apply_mode(
        self,
        mode: str,
        reason: str,
        *,
        matched_pattern: Optional[str] = None,
        actor: str = "anonymous",
        tenant: str = "",
        task_id: Optional[str] = None,
    ) -> GuardrailResult:
        """Apply the three-mode logic and return the appropriate result.

        *Audit* — log only, always allow.
        *Warn*  — log, attach X-Security-Warning header, allow.
        *Enforce* — log, deny.
        """
        if mode == "audit":
            self.event_store.record(
                event_type=SecurityEventType.MODE_AUDIT.value,
                actor=actor,
                target="inbound_guardrail",
                decision="allow",
                tenant=tenant,
                reason=f"{reason} (audit mode)",
                metadata={"matched_pattern": matched_pattern} if matched_pattern else None,
                task_id=task_id,
            )
            return GuardrailResult(
                allowed=True,
                reason=reason,
                severity="info",
                matched_pattern=matched_pattern,
            )

        if mode == "warn":
            self.event_store.record(
                event_type=SecurityEventType.MODE_WARN.value,
                actor=actor,
                target="inbound_guardrail",
                decision="allow",
                tenant=tenant,
                reason=f"{reason} (warn mode)",
                metadata={"matched_pattern": matched_pattern} if matched_pattern else None,
                task_id=task_id,
            )
            warn_value = (
                f"type=INJECTION_DETECTED; "
                f"actor={actor}; "
                f"pattern={matched_pattern or 'unknown'}; "
                f"reason={reason}"
            )
            return GuardrailResult(
                allowed=True,
                reason=reason,
                severity="warn",
                matched_pattern=matched_pattern,
                response_headers={"X-Security-Warning": warn_value},
            )

        # enforce mode — deny
        self.event_store.record(
            event_type=SecurityEventType.SECURITY_VIOLATION.value,
            actor=actor,
            target="inbound_guardrail",
            decision="deny",
            tenant=tenant,
            reason=reason,
            metadata={"matched_pattern": matched_pattern} if matched_pattern else None,
            task_id=task_id,
        )
        return GuardrailResult(
            allowed=False,
            reason=reason,
            severity="block",
            matched_pattern=matched_pattern,
        )

    @staticmethod
    def _flatten_to_text(data: Any, depth: int = 0, max_depth: int = 8) -> str:
        """Recursively flatten a JSON-like structure into a single text blob.

        Stops recursion at *max_depth* to avoid infinite loops on
        cyclic references.
        """
        if depth > max_depth:
            return ""
        parts: List[str] = []
        if isinstance(data, dict):
            for key, value in data.items():
                parts.append(str(key))
                parts.append(GuardrailEngine._flatten_to_text(value, depth + 1, max_depth))
        elif isinstance(data, list):
            for item in data:
                parts.append(GuardrailEngine._flatten_to_text(item, depth + 1, max_depth))
        elif isinstance(data, str):
            parts.append(data)
        elif data is not None:
            parts.append(str(data))
        return " ".join(parts)

    def _redact_tree(
        self,
        data: Any,
        path: str = "$",
        depth: int = 0,
        max_depth: int = 16,
    ) -> tuple[SanitizeReport, Any]:
        """Recursively walk *data*, redacting sensitive patterns in strings.

        Returns:
            ``(SanitizeReport, redacted_copy)`` — the report describes
            what was redacted; the copy is the sanitised output.
        """
        report = SanitizeReport()
        if depth > max_depth:
            return report, data

        if isinstance(data, str):
            original = data
            for pattern in SENSITIVE_PATTERNS:
                match = pattern.search(data)
                if match:
                    data = pattern.sub("***", data)
                    report.sanitised = True
                    if path not in report.field_paths:
                        report.field_paths.append(path)
                    report.count += 1
            # Count redacted positions (each "***" counts as one redaction)
            if report.sanitised:
                report.count = data.count("***")
            return report, data

        if isinstance(data, dict):
            new_dict: Dict[str, Any] = {}
            for key, value in data.items():
                child_path = f"{path}.{key}"
                # Check if the key name itself is sensitive — if so, redact the
                # entire value unconditionally
                if isinstance(value, str) and any(
                    pk.search(key) for pk in SENSITIVE_KEY_NAMES
                ):
                    new_dict[key] = "***"
                    report.sanitised = True
                    if child_path not in report.field_paths:
                        report.field_paths.append(child_path)
                    report.count += 1
                else:
                    child_report, child_value = self._redact_tree(value, child_path, depth + 1, max_depth)
                    if child_report.sanitised:
                        report.sanitised = True
                        report.field_paths.extend(child_report.field_paths)
                        report.count += child_report.count
                    new_dict[key] = child_value
            return report, new_dict

        if isinstance(data, list):
            new_list: List[Any] = []
            for idx, item in enumerate(data):
                child_path = f"{path}[{idx}]"
                child_report, child_item = self._redact_tree(item, child_path, depth + 1, max_depth)
                if child_report.sanitised:
                    report.sanitised = True
                    report.field_paths.extend(child_report.field_paths)
                    report.count += child_report.count
                new_list.append(child_item)
            return report, new_list

        # Primitive / none — no redaction possible
        return report, data


# ---------------------------------------------------------------------------
# Convenience functions (for callers that don't need a full engine)
# ---------------------------------------------------------------------------

def check_inbound(
    input_data: Dict[str, Any],
    mode: str = "enforce",
    *,
    event_store: Optional[SecurityEventStore] = None,
    actor: str = "anonymous",
    tenant: str = "",
    task_id: Optional[str] = None,
) -> GuardrailResult:
    """Convenience wrapper — one-shot inbound guardrail check.

    Creates a temporary :class:`GuardrailEngine` and runs
    :meth:`GuardrailEngine.check_inbound`.  Prefer instantiating the
    engine when making repeated calls.
    """
    if event_store is None:
        # Null-safe fallback — use a no-op memory store if none provided
        from unittest.mock import MagicMock
        event_store = MagicMock(spec=SecurityEventStore)  # type: ignore[assignment]

    engine = GuardrailEngine(event_store=event_store, mode=mode)
    return engine.check_inbound(
        input_data=input_data,
        actor=actor,
        tenant=tenant,
        task_id=task_id,
    )


def sanitize_output(
    output: Dict[str, Any],
    *,
    event_store: Optional[SecurityEventStore] = None,
    actor: str = "anonymous",
    tenant: str = "",
    task_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Convenience wrapper — one-shot outbound sanitisation.

    Creates a temporary :class:`GuardrailEngine` and runs
    :meth:`GuardrailEngine.sanitize_output`.
    """
    if event_store is None:
        from unittest.mock import MagicMock
        event_store = MagicMock(spec=SecurityEventStore)  # type: ignore[assignment]

    engine = GuardrailEngine(event_store=event_store)
    return engine.sanitize_output(
        output=output,
        actor=actor,
        tenant=tenant,
        task_id=task_id,
    )