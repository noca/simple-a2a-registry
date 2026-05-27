"""Centralized input validation, sanitization, and security hardening.

Provides:
- ``validate_agent_card()`` — schema validation for Agent Card registration
- ``validate_path_param()`` — format constraints for URL path parameters
- ``validate_scope_name()`` — whitelist check against registered scopes
- ``validate_jwt_claims()`` — JWT token field format checks
- ``sanitize_html()`` — HTML-encode strings for XSS prevention
- ``sanitize_card_output()`` — recursively HTML-encode string values in a card dict
- ``BodySizeLimitMiddleware`` — aiohttp middleware enforcing POST/PUT body size
"""

from __future__ import annotations

import html
import logging
import re
from typing import Any, Dict, List, Optional

from aiohttp import web

from simple_a2a_registry.store import SCOPES

logger = logging.getLogger("a2a_registry.validation")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Maximum string field lengths for Agent Card fields
_MAX_NAME_LENGTH = 255
_MAX_DESCRIPTION_LENGTH = 2000
_MAX_URL_LENGTH = 2048
_MAX_SKILL_ID_LENGTH = 128
_MAX_SKILL_NAME_LENGTH = 128
_MAX_PROTOCOL_VERSION_LENGTH = 32
_MAX_SCHEME_TYPE_LENGTH = 32
_MAX_TENANT_LENGTH = 128
_MAX_ORGANIZATION_LENGTH = 255
_MAX_TAG_LENGTH = 64
_MAX_INPUT_MODE_LENGTH = 64
_MAX_OUTPUT_MODE_LENGTH = 64
_MAX_URI_SCHEME_LENGTH = 32
_MAX_SKILL_TAGS = 50
_MAX_SKILL_EXAMPLES = 50
_MAX_SKILL_INPUT_MODES = 20
_MAX_SKILL_OUTPUT_MODES = 20
_MAX_SECURITY_SCHEMES = 20
_MAX_SKILLS_COUNT = 200
_MAX_INTERFACES_COUNT = 10

# Path parameter format constraints
# agent_id: alphanumeric, hyphens, underscores, dots, 1-128 chars
AGENT_ID_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$")
# task_id: UUID v4 or custom t_<hex> format
TASK_ID_PATTERN = re.compile(
    r"^(?:"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    r"|t_[0-9a-f]{8,16}"
    r")$",
    re.IGNORECASE,
)
# client_id: same as agent_id (alphanumeric + hyphens/underscores/dots)
CLIENT_ID_PATTERN = AGENT_ID_PATTERN

# JWT claim patterns
_JWT_ISSUER_PATTERN = re.compile(r"^[a-zA-Z0-9._-]{1,64}$")
_JWT_SCOPE_PATTERN = re.compile(r"^[a-z]+:[a-z]+(?: [a-z]+:[a-z]+)*$")

# ---------------------------------------------------------------------------
# Agent Card validation
# ---------------------------------------------------------------------------


class ValidationError(ValueError):
    """Raised when validation fails — maps to HTTP 400 responses."""

    def __init__(self, field: str, detail: str) -> None:
        self.field = field
        self.detail = detail
        super().__init__(f"Field '{field}': {detail}")


def _check_required_field(
    data: Dict[str, Any], field: str, label: str,
) -> Optional[ValidationError]:
    """Check a required field is present and non-empty."""
    value = data.get(field)
    if value is None or (isinstance(value, str) and not value.strip()):
        return ValidationError(field, f"{label} is required and cannot be empty")
    return None


def _check_string_length(
    data: Dict[str, Any], field: str, max_len: int,
) -> Optional[ValidationError]:
    """Check a string field doesn't exceed *max_len* characters."""
    value = data.get(field)
    if isinstance(value, str) and len(value) > max_len:
        return ValidationError(
            field,
            f"Must not exceed {max_len} characters (got {len(value)})",
        )
    return None


def _check_type(
    data: Dict[str, Any], field: str, expected_type: type,
    label: str = "",
) -> Optional[ValidationError]:
    """Check a field is of the expected type."""
    value = data.get(field)
    if value is not None and not isinstance(value, expected_type):
        return ValidationError(
            field,
            f"{label or field} must be {expected_type.__name__}, got {type(value).__name__}",
        )
    return None


def _check_list_type(
    data: Dict[str, Any], field: str,
    item_type: type, max_items: int,
    label: str = "",
) -> Optional[ValidationError]:
    """Check a field is a list of *item_type* with max *max_items* items."""
    value = data.get(field)
    if value is None:
        return None
    if not isinstance(value, list):
        return ValidationError(
            field,
            f"{label or field} must be a list, got {type(value).__name__}",
        )
    if len(value) > max_items:
        return ValidationError(
            field,
            f"{label or field} must not exceed {max_items} items (got {len(value)})",
        )
    for i, item in enumerate(value):
        if not isinstance(item, item_type):
            return ValidationError(
                field,
                f"{label or field}[{i}] must be {item_type.__name__}, "
                f"got {type(item).__name__}",
            )
    return None


def validate_agent_card(data: Dict[str, Any]) -> List[ValidationError]:
    """Validate an incoming Agent Card registration payload.

    Checks:
    - Required fields are present
    - String field length limits
    - Field type correctness
    - List size limits

    Args:
        data: The parsed JSON body of a registration request.

    Returns:
        A list of :class:`ValidationError` instances (empty if valid).
    """
    errors: List[ValidationError] = []

    # --- Top-level required fields ---
    for field, label in [("name", "Agent name")]:
        err = _check_required_field(data, field, label)
        if err:
            errors.append(err)

    # --- Top-level string length checks ---
    str_checks = [
        ("name", _MAX_NAME_LENGTH, "Agent name"),
        ("description", _MAX_DESCRIPTION_LENGTH, "Description"),
        ("version", _MAX_NAME_LENGTH, "Version"),
        ("documentation_url", _MAX_URL_LENGTH, "Documentation URL"),
        ("icon_url", _MAX_URL_LENGTH, "Icon URL"),
    ]
    for field, max_len, label in str_checks:
        err = _check_string_length(data, field, max_len)
        if err:
            errors.append(err)

    # --- Type checks ---
    type_checks = [
        ("description", str, "Description"),
        ("version", str, "Version"),
        ("documentation_url", str, "Documentation URL"),
        ("icon_url", str, "Icon URL"),
        ("security_schemes", dict, "security_schemes"),
        ("security_requirements", list, "security_requirements"),
        ("signatures", list, "Signatures"),
        ("default_input_modes", list, "default_input_modes"),
        ("default_output_modes", list, "default_output_modes"),
    ]
    for field, t, label in type_checks:
        err = _check_type(data, field, t, label)
        if err:
            errors.append(err)

    # --- List type checks with size limits ---
    list_checks = [
        ("skills", dict, _MAX_SKILLS_COUNT, "skills"),
        ("supported_interfaces", dict, _MAX_INTERFACES_COUNT, "supported_interfaces"),
    ]
    for field, item_type, max_items, label in list_checks:
        err = _check_list_type(data, field, item_type, max_items, label)
        if err:
            errors.append(err)

    # --- Validate nested provider if present ---
    provider = data.get("provider")
    if provider is not None:
        if not isinstance(provider, dict):
            errors.append(ValidationError("provider", "Provider must be a dict"))
        else:
            for pf, pl, pmax in [
                ("url", "Provider URL", _MAX_URL_LENGTH),
                ("organization", "Provider organization", _MAX_ORGANIZATION_LENGTH),
            ]:
                err = _check_string_length(provider, pf, pmax)
                if err:
                    errors.append(err)

    # --- Validate each skill ---
    skills = data.get("skills", [])
    if isinstance(skills, list):
        for i, skill in enumerate(skills):
            if not isinstance(skill, dict):
                continue
            # Required skill fields
            for sf in ["id", "name", "description"]:
                err = _check_required_field(skill, sf, f"Skill[{i}].{sf}")
                if err:
                    errors.append(err)
            # Skill string length
            skill_str_checks = [
                ("id", _MAX_SKILL_ID_LENGTH),
                ("name", _MAX_SKILL_NAME_LENGTH),
                ("description", _MAX_DESCRIPTION_LENGTH),
            ]
            for sf, smax in skill_str_checks:
                err = _check_string_length(skill, sf, smax)
                if err:
                    errors.append(err)
            # Skill list fields
            err = _check_list_type(
                skill, "tags", str, _MAX_SKILL_TAGS, f"Skill[{i}].tags",
            )
            if err:
                errors.append(err)
            err = _check_list_type(
                skill, "examples", str, _MAX_SKILL_EXAMPLES, f"Skill[{i}].examples",
            )
            if err:
                errors.append(err)
            err = _check_list_type(
                skill, "input_modes", str, _MAX_SKILL_INPUT_MODES,
                f"Skill[{i}].input_modes",
            )
            if err:
                errors.append(err)
            err = _check_list_type(
                skill, "output_modes", str, _MAX_SKILL_OUTPUT_MODES,
                f"Skill[{i}].output_modes",
            )
            if err:
                errors.append(err)
            err = _check_list_type(
                skill, "uri_schemes", str, _MAX_SKILL_TAGS,
                f"Skill[{i}].uri_schemes",
            )
            if err:
                errors.append(err)

            # Tag length
            tags = skill.get("tags", [])
            if isinstance(tags, list):
                for j, tag in enumerate(tags):
                    if isinstance(tag, str) and len(tag) > _MAX_TAG_LENGTH:
                        errors.append(ValidationError(
                            f"skills[{i}].tags[{j}]",
                            f"Tag must not exceed {_MAX_TAG_LENGTH} characters "
                            f"(got {len(tag)})",
                        ))

    # --- Validate each interface ---
    interfaces = data.get("supported_interfaces", [])
    if isinstance(interfaces, list):
        for i, iface in enumerate(interfaces):
            if not isinstance(iface, dict):
                continue
            # Required interface fields
            for ifn in ["url", "protocol_binding", "protocol_version"]:
                err = _check_required_field(
                    iface, ifn, f"Interface[{i}].{ifn}",
                )
                if err:
                    errors.append(err)
            iface_str_checks = [
                ("url", _MAX_URL_LENGTH),
                ("protocol_binding", _MAX_SCHEME_TYPE_LENGTH),
                ("protocol_version", _MAX_PROTOCOL_VERSION_LENGTH),
                ("tenant", _MAX_TENANT_LENGTH),
            ]
            for ifn, imax in iface_str_checks:
                err = _check_string_length(iface, ifn, imax)
                if err:
                    errors.append(err)

    # --- Validate security_schemes if present ---
    security_schemes = data.get("security_schemes", {})
    if isinstance(security_schemes, dict):
        if len(security_schemes) > _MAX_SECURITY_SCHEMES:
            errors.append(ValidationError(
                "security_schemes",
                f"Must not exceed {_MAX_SECURITY_SCHEMES} schemes "
                f"(got {len(security_schemes)})",
            ))
        valid_scheme_types = {
            "apiKey", "http", "oauth2", "openIdConnect", "mutualTls",
        }
        for scheme_name, scheme in security_schemes.items():
            if not isinstance(scheme, dict):
                continue
            scheme_type = scheme.get("scheme_type", "")
            if scheme_type and scheme_type not in valid_scheme_types:
                errors.append(ValidationError(
                    f"security_schemes.{scheme_name}.scheme_type",
                    f"Unknown scheme type '{scheme_type}'. Valid: "
                    + ", ".join(sorted(valid_scheme_types)),
                ))

    return errors


# ---------------------------------------------------------------------------
# Path parameter validation
# ---------------------------------------------------------------------------


def validate_agent_id(param_name: str, value: str) -> Optional[ValidationError]:
    """Validate an agent_id URL path parameter."""
    if not isinstance(value, str) or not value:
        return ValidationError(param_name, "Agent ID must be a non-empty string")
    if not AGENT_ID_PATTERN.match(value):
        return ValidationError(
            param_name,
            f"Invalid agent_id format: '{value}'. Must be 1-128 alphanumeric "
            "characters, hyphens, underscores, or dots.",
        )
    return None


def validate_task_id(param_name: str, value: str) -> Optional[ValidationError]:
    """Validate a task_id URL path parameter."""
    if not isinstance(value, str) or not value:
        return ValidationError(param_name, "Task ID must be a non-empty string")
    if not TASK_ID_PATTERN.match(value):
        return ValidationError(
            param_name,
            f"Invalid task_id format: '{value}'. Must be a UUID or t_<hex>.",
        )
    return None


def validate_client_id(param_name: str, value: str) -> Optional[ValidationError]:
    """Validate a client_id URL path parameter."""
    if not isinstance(value, str) or not value:
        return ValidationError(param_name, "Client ID must be a non-empty string")
    if not CLIENT_ID_PATTERN.match(value):
        return ValidationError(
            param_name,
            f"Invalid client_id format: '{value}'. Must be 1-128 alphanumeric "
            "characters, hyphens, underscores, or dots.",
        )
    return None


# ---------------------------------------------------------------------------
# Scope validation
# ---------------------------------------------------------------------------

VALID_SCOPES: set = set(SCOPES.keys())


def validate_scope_name(scope: str) -> Optional[ValidationError]:
    """Validate a scope name against the whitelist of registered scopes.

    Returns:
        ``None`` if valid, or a :class:`ValidationError` for unknown scopes.
    """
    if scope not in VALID_SCOPES:
        return ValidationError(
            "scope",
            f"Invalid scope '{scope}'. Valid scopes: {', '.join(sorted(VALID_SCOPES))}",
        )
    return None


def validate_scope_list(scopes: List[str]) -> List[ValidationError]:
    """Validate a list of scope names."""
    errors: List[ValidationError] = []
    for s in scopes:
        err = validate_scope_name(s)
        if err:
            errors.append(err)
    return errors


def validate_scope_string(scope_str: str) -> List[ValidationError]:
    """Validate a space-separated scope string (as in JWT 'scope' claim)."""
    errors: List[ValidationError] = []
    if not scope_str:
        return errors
    for s in scope_str.split():
        err = validate_scope_name(s)
        if err:
            errors.append(err)
    return errors


# ---------------------------------------------------------------------------
# JWT claim validation
# ---------------------------------------------------------------------------


def validate_jwt_claims(payload: Dict[str, Any]) -> List[ValidationError]:
    """Validate JWT token payload claims.

    Checks:
    - ``iss``: non-empty, valid format
    - ``exp``: must be a number
    - ``scope``: if present, must be valid space-separated scope names
    - ``sub``: non-empty string
    - ``jti``: non-empty string

    Args:
        payload: The decoded JWT payload dict.

    Returns:
        A list of :class:`ValidationError` instances (empty if valid).
    """
    errors: List[ValidationError] = []

    # iss
    iss = payload.get("iss", "")
    if not isinstance(iss, str) or not iss:
        errors.append(ValidationError("iss", "JWT 'iss' claim is required"))
    elif len(iss) > 64:
        errors.append(ValidationError(
            "iss", f"JWT 'iss' must not exceed 64 characters (got {len(iss)})",
        ))

    # sub
    sub = payload.get("sub", "")
    if not isinstance(sub, str) or not sub:
        errors.append(ValidationError("sub", "JWT 'sub' claim is required"))

    # exp
    exp = payload.get("exp")
    if exp is not None and not isinstance(exp, (int, float)):
        errors.append(ValidationError(
            "exp", "JWT 'exp' claim must be a number (Unix timestamp)",
        ))

    # jti
    jti = payload.get("jti", "")
    if jti and not isinstance(jti, str):
        errors.append(ValidationError(
            "jti", "JWT 'jti' claim must be a string",
        ))

    # scope
    scope = payload.get("scope")
    if scope is not None:
        if not isinstance(scope, str):
            errors.append(ValidationError(
                "scope", "JWT 'scope' claim must be a space-separated string",
            ))
        else:
            if not _JWT_SCOPE_PATTERN.match(scope):
                errors.append(ValidationError(
                    "scope",
                    "JWT 'scope' format invalid. Must be space-separated "
                    "'category:action' pairs (e.g. 'task:read task:write').",
                ))
            errors.extend(validate_scope_string(scope))

    return errors


# ---------------------------------------------------------------------------
# XSS prevention — HTML output encoding
# ---------------------------------------------------------------------------


def sanitize_html(value: Any) -> Any:
    """HTML-encode a string value to prevent XSS.

    Non-string values are returned unchanged.
    """
    if isinstance(value, str):
        return html.escape(value, quote=True)
    return value


def sanitize_card_output(data: Any, depth: int = 0) -> Any:
    """Recursively HTML-encode all string values in an Agent Card dict.

    Scans dict values and list items, encoding any string values with
    :func:`html.escape`. This prevents XSS when card data (name,
    description, skill names, etc.) is rendered in web dashboards.

    Args:
        data: The Agent Card dict (or any nested JSON-compatible structure).
        depth: Current recursion depth (internal, max 20).

    Returns:
        A new dict/list with string values HTML-encoded.
    """
    if depth > 20:
        return data

    if isinstance(data, dict):
        return {
            k: sanitize_card_output(v, depth + 1) for k, v in data.items()
        }
    elif isinstance(data, list):
        return [sanitize_card_output(v, depth + 1) for v in data]
    elif isinstance(data, str):
        # Double-encoding guard: avoid encoding already-encoded strings
        # (simple heuristic: if it contains &amp; or &#x, skip encoding)
        if "&amp;" in data or "&#x" in data or "&lt;" in data or "&gt;" in data:
            return data
        return html.escape(data, quote=True)
    return data


# ---------------------------------------------------------------------------
# Body size limit middleware
# ---------------------------------------------------------------------------


def body_size_limit_middleware_factory(
    max_body_size: int = 1 * 1024 * 1024,  # 1 MB default
) -> callable:
    """Create an aiohttp middleware that enforces POST/PUT body size limits.

    Middleware checks ``Content-Length`` header for large bodies BEFORE
    reading the body, returning 413 Payload Too Large immediately.

    This is a lightweight safety net — aiohttp's built-in
    ``client_max_size`` on the ``Application`` is the primary shield,
    and this middleware provides an early-out with a proper 413 response.

    Args:
        max_body_size: Maximum allowed body size in bytes (default 1 MB).

    Returns:
        An aiohttp middleware function.
    """

    @web.middleware
    async def _body_size_middleware(
        request: web.Request, handler: callable,
    ) -> web.StreamResponse:
        if request.method in ("POST", "PUT", "PATCH"):
            content_length = request.headers.get("Content-Length")
            if content_length:
                try:
                    cl = int(content_length)
                    if cl > max_body_size:
                        return web.json_response(
                            {
                                "error": "payload_too_large",
                                "detail": f"Request body exceeds maximum size of "
                                f"{max_body_size} bytes ({cl} bytes received)",
                            },
                            status=413,
                        )
                except (ValueError, TypeError):
                    pass
        return await handler(request)

    return _body_size_middleware


# ---------------------------------------------------------------------------
# Path parameter validation middleware
# ---------------------------------------------------------------------------


def path_param_middleware_factory(
    path_rules: Optional[Dict[str, callable]] = None,
) -> callable:
    """Create an aiohttp middleware that validates URL path parameters.

    If a path matches a known rule pattern, the matched parameters are
    validated against the registered validator function.

    Default rules validate ``agent_id``, ``task_id``, and ``client_id``
    parameters found in route match_info.

    Args:
        path_rules: Optional dict of ``param_name → validator_function``.
            If omitted, defaults to agent_id, task_id, client_id validation.

    Returns:
        An aiohttp middleware function.
    """
    rules = path_rules or {
        "agent_id": validate_agent_id,
        "task_id": validate_task_id,
        "client_id": validate_client_id,
    }

    @web.middleware
    async def _path_param_middleware(
        request: web.Request, handler: callable,
    ) -> web.StreamResponse:
        # Only validate on HTTP methods that deal with resources
        if request.method in ("GET", "POST", "PUT", "PATCH", "DELETE"):
            for param_name, validator in rules.items():
                value = request.match_info.get(param_name)
                if value is not None:
                    err = validator(param_name, value)
                    if err is not None:
                        return web.json_response(
                            {
                                "error": "validation_error",
                                "detail": err.detail,
                            },
                            status=400,
                        )
        return await handler(request)

    return _path_param_middleware
