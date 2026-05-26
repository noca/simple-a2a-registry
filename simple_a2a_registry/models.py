"""A2A Agent Card data models — aligned with A2A v1.0 specification.

Pydantic-free data classes with dict serialization for the A2A registry.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Core Agent Card types  (A2A v1.0 protobuf alignment)
# ---------------------------------------------------------------------------


@dataclass
class AgentInterface:
    """An interface endpoint for an A2A agent.

    PM Review B1: ``type`` → ``protocol_binding``, added ``protocol_version``
    and ``tenant`` to match a2a.proto.
    """
    url: str                              # REQUIRED
    protocol_binding: str                 # REQUIRED, e.g. "JSONRPC", "GRPC"
    protocol_version: str                 # REQUIRED, e.g. "1.0"
    tenant: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return _dataclass_to_dict(self)


@dataclass
class AgentCapabilities:
    """Capability flags for an A2A agent.

    PM Review B2: removed ``state_transition_history`` (not in proto),
    added ``extensions`` and ``extended_agent_card``.
    """
    streaming: Optional[bool] = None
    push_notifications: Optional[bool] = None
    extensions: Optional[List[AgentExtension]] = None
    extended_agent_card: Optional[bool] = None

    def to_dict(self) -> Dict[str, Any]:
        return _dataclass_to_dict(self)


@dataclass
class AgentExtension:
    """Extension metadata — used inside AgentCapabilities.extensions."""
    name: str = ""
    url: str = ""
    required: Optional[bool] = None
    properties: Optional[Dict[str, Any]] = None


@dataclass
class AgentSkill:
    """An individual A2A v1.0 agent skill.

    PM Review B3: removed ``version`` (not in proto), ``description`` and
    ``tags`` are now REQUIRED, added ``security_requirements``.
    """
    id: str                                             # REQUIRED
    name: str                                           # REQUIRED
    description: str                                    # REQUIRED (proto)
    tags: List[str] = field(default_factory=list)       # REQUIRED (proto)
    examples: Optional[List[str]] = None
    input_modes: Optional[List[str]] = None
    output_modes: Optional[List[str]] = None
    security_requirements: Optional[List[SecurityRequirement]] = None
    # Project extension: URI scheme routing (not in a2a.proto)
    uri_schemes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return _dataclass_to_dict(self)


@dataclass
class AgentProvider:
    """Provider metadata for an A2A agent.

    PM Review B4: kept ``organization`` (proto field name), NOT renamed to
    ``name``.
    """
    url: str = ""                # REQUIRED
    organization: str = ""       # proto field name, kept as-is

    def to_dict(self) -> Dict[str, Any]:
        return _dataclass_to_dict(self)


# ---------------------------------------------------------------------------
# SecurityScheme hierarchy  (A2A v1.0 protobuf: oneof scheme)
# ---------------------------------------------------------------------------


@dataclass
class SecurityScheme:
    """Security scheme definition — oneof by ``scheme_type``.

    scheme_type values: "apiKey" | "http" | "oauth2" | "openIdConnect" | "mutualTls"

    Only the field matching ``scheme_type`` is populated at any time.
    """
    scheme_type: str                      # REQUIRED
    description: Optional[str] = None
    # Per-type (oneof semantics)
    api_key: Optional[APIKeySecurityScheme] = None
    http_auth: Optional[HTTPAuthSecurityScheme] = None
    oauth2: Optional[OAuth2SecurityScheme] = None
    open_id_connect: Optional[OpenIdConnectSecurityScheme] = None
    mtls: Optional[MutualTlsSecurityScheme] = None

    def to_dict(self) -> Dict[str, Any]:
        return _dataclass_to_dict(self)


@dataclass
class APIKeySecurityScheme:
    """API Key authentication — ``scheme_type: \"apiKey\"``."""
    name: str = ""
    key_format: Optional[str] = None      # e.g. "api-key", "bearer"


@dataclass
class HTTPAuthSecurityScheme:
    """HTTP Authentication — ``scheme_type: \"http\"``.

    ``scheme`` values: "Basic", "Bearer", "Digest", etc.
    """
    scheme: str = ""


@dataclass
class OAuth2SecurityScheme:
    """OAuth 2.1 — ``scheme_type: \"oauth2\"``."""
    flows: Optional[OAuthFlows] = None
    oauth2_metadata_url: Optional[str] = None  # RFC 8414


@dataclass
class OpenIdConnectSecurityScheme:
    """OpenID Connect — ``scheme_type: \"openIdConnect\"``."""
    issuer_url: str = ""
    discovery_url: Optional[str] = None


@dataclass
class MutualTlsSecurityScheme:
    """Mutual TLS — ``scheme_type: \"mutualTls\"``."""
    certificate_authority: Optional[str] = None
    certificate_requirements: Optional[List[str]] = None


# ---------------------------------------------------------------------------
# OAuth Flows
# ---------------------------------------------------------------------------


@dataclass
class AuthorizationCodeOAuthFlow:
    """Authorization Code grant (OAuth 2.0/2.1)."""
    authorization_url: str = ""
    token_url: str = ""
    refresh_url: Optional[str] = None
    scopes: Dict[str, str] = field(default_factory=dict)


@dataclass
class ClientCredentialsOAuthFlow:
    """Client Credentials grant."""
    token_url: str = ""
    refresh_url: Optional[str] = None
    scopes: Dict[str, str] = field(default_factory=dict)


@dataclass
class DeviceCodeOAuthFlow:
    """Device Code grant."""
    device_authorization_url: str = ""
    token_url: str = ""
    scopes: Dict[str, str] = field(default_factory=dict)


@dataclass
class ImplicitOAuthFlow:
    """Implicit grant (deprecated in OAuth 2.1, kept for proto completeness).

    PM Review W2: kept for protobuf ``oneof flow`` completeness.
    """
    authorization_url: str = ""
    refresh_url: Optional[str] = None
    scopes: Dict[str, str] = field(default_factory=dict)


@dataclass
class PasswordOAuthFlow:
    """Password grant (deprecated in OAuth 2.1, kept for proto completeness).

    PM Review W2.
    """
    token_url: str = ""
    refresh_url: Optional[str] = None
    scopes: Dict[str, str] = field(default_factory=dict)


@dataclass
class OAuthFlows:
    """Container for OAuth flow definitions.

    PM Review W2: includes deprecated ``implicit`` and ``password`` for
    protobuf ``oneof flow`` backward compatibility.
    """
    authorization_code: Optional[AuthorizationCodeOAuthFlow] = None
    client_credentials: Optional[ClientCredentialsOAuthFlow] = None
    device_code: Optional[DeviceCodeOAuthFlow] = None
    # Deprecated (kept for proto oneof completeness)
    implicit: Optional[ImplicitOAuthFlow] = None
    password: Optional[PasswordOAuthFlow] = None


# ---------------------------------------------------------------------------
# SecurityRequirement
# ---------------------------------------------------------------------------

@dataclass
class SecurityRequirement:
    """Map of scheme_name → required scopes.

    PM Review B5: added dataclass definition (was only referenced).
    """
    schemes: Dict[str, List[str]] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return _dataclass_to_dict(self)


# ---------------------------------------------------------------------------
# AgentCardSignature & AuthenticationInfo
# ---------------------------------------------------------------------------


@dataclass
class AuthenticationInfo:
    """Authentication metadata associated with an AgentCard."""
    authentication_required: bool = False
    authentication_schemes: List[str] = field(default_factory=list)


@dataclass
class AgentCardSignature:
    """JWS signature for an AgentCard — enables card integrity verification."""
    alg: str = ""          # REQUIRED, e.g. "RS256"
    signature: str = ""    # REQUIRED, base64-encoded JWS
    key_url: str = ""      # REQUIRED, URL to public key
    kid: Optional[str] = None  # Key identifier

    def to_dict(self) -> Dict[str, Any]:
        return _dataclass_to_dict(self)


# ---------------------------------------------------------------------------
# Main AgentCard
# ---------------------------------------------------------------------------


@dataclass
class AgentCard:
    """A2A v1.0 Agent Card — the standard agent identity document.

    Fully aligned with A2A v1.0 protobuf specification (a2a.proto).

    Changes from v0.x:
      - Removed: ``id``, ``url``, ``authentication``, ``notification``,
        ``tags``, ``metadata``
      - New REQUIRED: ``supported_interfaces``, ``default_input_modes``,
        ``default_output_modes``, ``skills``
      - New optional: ``documentation_url``, ``security_schemes``,
        ``security_requirements``, ``signatures``, ``icon_url``
      - ``capabilities`` restructured (flags only, no nested skills)
      - ``skills`` is now a top-level field
    """
    name: str                                            # REQUIRED
    description: str                                     # REQUIRED
    supported_interfaces: List[AgentInterface] = field(default_factory=list)  # REQUIRED
    provider: Optional[AgentProvider] = None
    version: str = "1.0.0"                               # REQUIRED
    documentation_url: Optional[str] = None
    capabilities: AgentCapabilities = field(default_factory=AgentCapabilities)  # REQUIRED
    security_schemes: Dict[str, SecurityScheme] = field(default_factory=dict)
    security_requirements: List[SecurityRequirement] = field(default_factory=list)
    default_input_modes: List[str] = field(default_factory=lambda: ["text/plain"])  # REQUIRED
    default_output_modes: List[str] = field(default_factory=lambda: ["text/plain"])  # REQUIRED
    skills: List[AgentSkill] = field(default_factory=list)        # REQUIRED
    signatures: List[AgentCardSignature] = field(default_factory=list)
    icon_url: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to a plain dict (drops None fields)."""
        return _dataclass_to_dict(self)

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> AgentCard:
        """Deserialize from a plain dict (backward-compatible with v0.x)."""
        return _dict_to_agent_card(data)


# ---------------------------------------------------------------------------
# Factory functions  (convenience for discovery)
# ---------------------------------------------------------------------------


def make_agent_skill(
    skill_id: str = "",
    name: str = "",
    description: str = "",
    tags: Optional[List[str]] = None,
    uri_schemes: Optional[List[str]] = None,
) -> Dict:
    """Create an AgentSkill as a plain dict (convenience for discovery).

    Returns a v1.0-style skill dict (no ``version`` field).
    """
    skill = AgentSkill(
        id=skill_id,
        name=name,
        description=description,
        tags=tags or [],
        uri_schemes=uri_schemes or [],
    )
    return skill.to_dict()


def make_agent_card(
    agent_id: str = "",
    name: str = "",
    description: str = "",
    url: str = "",
    skills: Optional[List[Dict]] = None,
    provider: Optional[Dict] = None,
    tags: Optional[List[str]] = None,
) -> Dict:
    """Create an AgentCard as a plain dict (convenience for discovery).

    Note about backward-compatible kwargs:
      - ``agent_id`` and ``tags`` are accepted but NOT stored in v1.0 AgentCard.
      - ``url`` is converted to a single-element ``supported_interfaces`` list.
    """
    interfaces = (
        [AgentInterface(url=url, protocol_binding="JSONRPC", protocol_version="1.0")]
        if url else []
    )
    prov = AgentProvider(**provider) if provider else None
    card = AgentCard(
        name=name,
        description=description,
        supported_interfaces=interfaces,
        provider=prov,
        skills=[_dict_to_skill(s) for s in (skills or [])],
    )
    return card.to_dict()


# ---------------------------------------------------------------------------
# Internal serialization helpers
# ---------------------------------------------------------------------------


def _dataclass_to_dict(obj: Any) -> Dict[str, Any]:
    """Recursively convert a dataclass tree to a plain dict, dropping None."""
    result = {}
    for field_name, field_type in obj.__dataclass_fields__.items():
        value = getattr(obj, field_name)
        if value is None:
            continue
        if isinstance(value, list):
            result[field_name] = [
                _dataclass_to_dict(v) if hasattr(v, "__dataclass_fields__") else v
                for v in value
            ]
        elif isinstance(value, dict):
            result[field_name] = {
                k: _dataclass_to_dict(v) if hasattr(v, "__dataclass_fields__") else v
                for k, v in value.items()
            }
        elif hasattr(value, "__dataclass_fields__"):
            result[field_name] = _dataclass_to_dict(value)
        else:
            result[field_name] = value
    return result


def _dict_to_skill(d: Dict) -> AgentSkill:
    """Convert a plain dict to an AgentSkill (v1.0)."""
    return AgentSkill(
        id=d.get("id", ""),
        name=d.get("name", ""),
        description=d.get("description", ""),
        tags=d.get("tags", []),
        uri_schemes=d.get("uri_schemes", []),
        examples=d.get("examples"),
        input_modes=d.get("input_modes"),
        output_modes=d.get("output_modes"),
        security_requirements=_dict_list_to_security_requirements(
            d.get("security_requirements")
        ),
    )


def _dict_to_security_requirement(d: Dict) -> SecurityRequirement:
    """Convert a plain dict to a SecurityRequirement."""
    return SecurityRequirement(schemes=d.get("schemes", {}))


def _dict_list_to_security_requirements(
    items: Optional[List[Dict]],
) -> Optional[List[SecurityRequirement]]:
    """Convert a list of dicts to a list of SecurityRequirements."""
    if items is None:
        return None
    return [_dict_to_security_requirement(sr) for sr in items]


def _dict_to_interface(d: Dict) -> AgentInterface:
    """Convert a plain dict to an AgentInterface."""
    return AgentInterface(
        url=d.get("url", ""),
        protocol_binding=d.get("protocol_binding", "JSONRPC"),
        protocol_version=d.get("protocol_version", "1.0"),
        tenant=d.get("tenant"),
    )


def _dict_to_capabilities(d: Dict) -> AgentCapabilities:
    """Convert a plain dict to AgentCapabilities (v1.0 flags)."""
    extensions_raw = d.get("extensions")
    extensions = None
    if extensions_raw is not None:
        extensions = [
            AgentExtension(
                name=ex.get("name", ""),
                url=ex.get("url", ""),
                required=ex.get("required"),
                properties=ex.get("properties"),
            )
            for ex in extensions_raw
        ]
    return AgentCapabilities(
        streaming=d.get("streaming"),
        push_notifications=d.get("push_notifications"),
        extensions=extensions,
        extended_agent_card=d.get("extended_agent_card"),
    )


def _dict_to_security_schemes(
    d: Optional[Dict[str, Dict]],
) -> Dict[str, SecurityScheme]:
    """Convert a dict of scheme dicts to Dict[str, SecurityScheme]."""
    if not d:
        return {}
    return {name: _dict_to_single_security_scheme(sc) for name, sc in d.items()}


def _dict_to_single_security_scheme(d: Dict) -> SecurityScheme:
    """Convert a single scheme dict to a SecurityScheme (oneof dispatch)."""
    scheme_type = d.get("scheme_type", "")
    api_key = http_auth = oauth2 = open_id_connect = mtls = None

    if scheme_type == "apiKey" and isinstance(d.get("api_key"), dict):
        api_key = APIKeySecurityScheme(**d["api_key"])
    elif scheme_type == "http" and isinstance(d.get("http_auth"), dict):
        http_auth = HTTPAuthSecurityScheme(**d["http_auth"])
    elif scheme_type == "oauth2" and isinstance(d.get("oauth2"), dict):
        o2 = d["oauth2"]
        flows = _dict_to_oauth_flows(o2.get("flows", {})) if o2.get("flows") else None
        oauth2 = OAuth2SecurityScheme(
            flows=flows,
            oauth2_metadata_url=o2.get("oauth2_metadata_url"),
        )
    elif scheme_type == "openIdConnect" and isinstance(d.get("open_id_connect"), dict):
        open_id_connect = OpenIdConnectSecurityScheme(**d["open_id_connect"])
    elif scheme_type == "mutualTls" and isinstance(d.get("mtls"), dict):
        mtls = MutualTlsSecurityScheme(**d["mtls"])

    return SecurityScheme(
        scheme_type=scheme_type,
        description=d.get("description"),
        api_key=api_key,
        http_auth=http_auth,
        oauth2=oauth2,
        open_id_connect=open_id_connect,
        mtls=mtls,
    )


def _dict_to_oauth_flows(d: Dict) -> OAuthFlows:
    """Convert a dict to an OAuthFlows instance."""

    def _parse(name: str, cls: type) -> Any:
        data = d.get(name)
        return cls(**data) if isinstance(data, dict) else None

    return OAuthFlows(
        authorization_code=_parse("authorization_code", AuthorizationCodeOAuthFlow),
        client_credentials=_parse("client_credentials", ClientCredentialsOAuthFlow),
        device_code=_parse("device_code", DeviceCodeOAuthFlow),
        implicit=_parse("implicit", ImplicitOAuthFlow),
        password=_parse("password", PasswordOAuthFlow),
    )


def _dict_to_signature(d: Dict) -> AgentCardSignature:
    """Convert a dict to an AgentCardSignature."""
    return AgentCardSignature(
        alg=d.get("alg", ""),
        signature=d.get("signature", ""),
        key_url=d.get("key_url", ""),
        kid=d.get("kid"),
    )


def _dict_to_agent_card(d: Dict) -> AgentCard:
    """Convert a plain dict to an AgentCard (v1.0).

    Backward-compatible: accepts both v1.0 (skills at top-level) and v0.x
    (skills inside ``capabilities.skills``) format.
    """
    # Interfaces
    interfaces = [
        _dict_to_interface(i)
        for i in (d.get("supported_interfaces") or [])
    ]

    # Skills: prefer top-level ``skills``, fall back to old ``capabilities.skills``
    skills_raw = d.get("skills")
    if skills_raw is None:
        caps = d.get("capabilities", {})
        skills_raw = caps.get("skills") if isinstance(caps, dict) else None
    skills = [_dict_to_skill(s) for s in (skills_raw or [])]

    # Capabilities (v1.0 flags, no nested skills)
    caps_data = d.get("capabilities", {})
    # If capabilities has a 'skills' key we already consumed, parse cleanly
    capabilities = _dict_to_capabilities(caps_data) if isinstance(caps_data, dict) else AgentCapabilities()

    # Provider
    prov = None
    if isinstance(d.get("provider"), dict):
        prov = AgentProvider(**d["provider"])

    # Signatures
    signatures = [
        _dict_to_signature(s) for s in (d.get("signatures") or [])
    ]

    # Security
    security_schemes = _dict_to_security_schemes(d.get("security_schemes"))
    security_requirements = _dict_list_to_security_requirements(
        d.get("security_requirements")
    ) or []

    return AgentCard(
        name=d.get("name", ""),
        description=d.get("description", ""),
        supported_interfaces=interfaces,
        provider=prov,
        version=d.get("version", "1.0.0"),
        documentation_url=d.get("documentation_url"),
        capabilities=capabilities,
        security_schemes=security_schemes,
        security_requirements=security_requirements,
        default_input_modes=d.get("default_input_modes", ["text/plain"]),
        default_output_modes=d.get("default_output_modes", ["text/plain"]),
        skills=skills,
        signatures=signatures,
        icon_url=d.get("icon_url"),
    )