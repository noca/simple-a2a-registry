"""Unit tests for A2A v1.0 data models."""
from __future__ import annotations

from simple_a2a_registry.models import (
    AgentCard,
    AgentCapabilities,
    AgentSkill,
    AgentInterface,
    AgentProvider,
    AgentCardSignature,
    SecurityScheme,
    APIKeySecurityScheme,
    HTTPAuthSecurityScheme,
    OAuth2SecurityScheme,
    OpenIdConnectSecurityScheme,
    MutualTlsSecurityScheme,
    OAuthFlows,
    AuthorizationCodeOAuthFlow,
    ClientCredentialsOAuthFlow,
    SecurityRequirement,
    make_agent_card,
    make_agent_skill,
)


def test_make_agent_skill_v1() -> None:
    """make_agent_skill produces v1.0 skill dict (no version, tags required)."""
    skill = make_agent_skill(
        skill_id="s1",
        name="Test Skill",
        description="A skill for testing",
        tags=["test"],
    )
    assert skill["id"] == "s1"
    assert skill["name"] == "Test Skill"
    assert skill["tags"] == ["test"]
    # v1.0: version field is NOT present in AgentSkill
    assert "version" not in skill


def test_make_agent_card_v1() -> None:
    """make_agent_card produces v1.0 AgentCard dict (no id/tags, top-level skills)."""
    card = make_agent_card(
        name="Test Agent",
        description="A test agent card",
        url="https://test.agent",
        skills=[
            make_agent_skill("s1", "Skill One"),
            make_agent_skill("s2", "Skill Two"),
        ],
        provider={"organization": "Test Org", "url": "https://test.org"},
    )
    assert card["name"] == "Test Agent"
    assert len(card["skills"]) == 2
    assert card["provider"]["organization"] == "Test Org"
    # v1.0: id and tags are NOT in the model
    assert "id" not in card
    assert "tags" not in card
    # v1.0: skills are top-level, not inside capabilities
    assert "skills" in card
    # v1.0: URL becomes supported_interfaces
    assert "supported_interfaces" in card
    assert card["supported_interfaces"][0]["url"] == "https://test.agent"


def test_agent_card_to_dict_roundtrip() -> None:
    """Full roundtrip: AgentCard → dict → AgentCard preserves all fields."""
    card = AgentCard(
        name="Roundtrip",
        description="Test roundtrip",
        supported_interfaces=[
            AgentInterface(url="https://r.test", protocol_binding="JSONRPC", protocol_version="1.0"),
        ],
        provider=AgentProvider(organization="Org", url="https://org"),
        skills=[AgentSkill(id="s1", name="Skill 1", description="A skill", tags=["t1"])],
        capabilities=AgentCapabilities(streaming=True, push_notifications=False),
        default_input_modes=["text/plain"],
        default_output_modes=["text/markdown"],
        icon_url="https://icon.test",
    )
    d = card.to_dict()
    restored = AgentCard.from_dict(d)
    assert restored.name == "Roundtrip"
    assert restored.description == "Test roundtrip"
    assert len(restored.supported_interfaces) == 1
    assert restored.supported_interfaces[0].protocol_binding == "JSONRPC"
    assert restored.provider.organization == "Org"
    assert len(restored.skills) == 1
    assert restored.skills[0].name == "Skill 1"
    assert restored.capabilities.streaming is True
    assert restored.capabilities.push_notifications is False
    assert restored.default_input_modes == ["text/plain"]
    assert restored.default_output_modes == ["text/markdown"]
    assert restored.icon_url == "https://icon.test"


def test_agent_card_none_fields_dropped() -> None:
    """to_dict drops None fields — optional fields absent from serialized output.

    Empty lists/dicts (field factory defaults) ARE included since they're
    not None, but unset Optional fields are dropped.
    """
    card = AgentCard(name="Minimal", description="Minimal test")
    d = card.to_dict()
    assert "name" in d
    assert "description" in d
    # Optional None fields should be absent
    assert "documentation_url" not in d
    assert "icon_url" not in d
    # Empty containers (default factory) are included
    assert "signatures" in d
    assert isinstance(d["signatures"], list)
    assert len(d["signatures"]) == 0
    assert "supported_interfaces" in d
    assert "skills" in d


def test_agent_card_version_default() -> None:
    """Default version is 1.0.0."""
    card = AgentCard(name="Version Test", description="Testing version")
    assert card.version == "1.0.0"
    d = card.to_dict()
    assert d["version"] == "1.0.0"


def test_v0_backward_compat() -> None:
    """AgentCard.from_dict still parses v0.x-style dicts (capabilities.skills)."""
    old_style = {
        "name": "Old Agent",
        "description": "Old style agent",
        "capabilities": {
            "skills": [{"id": "s1", "name": "Old Skill", "description": "desc", "tags": []}],
        },
    }
    card = AgentCard.from_dict(old_style)
    assert len(card.skills) == 1
    assert card.skills[0].name == "Old Skill"
    assert card.skills[0].id == "s1"


def test_agent_interface_b1() -> None:
    """AgentInterface conforms to PM Review B1 — protocol_binding, protocol_version, tenant."""
    iface = AgentInterface(
        url="https://agent.test",
        protocol_binding="JSONRPC",
        protocol_version="1.0",
        tenant="acme-corp",
    )
    d = iface.to_dict()
    assert d["protocol_binding"] == "JSONRPC"
    assert d["protocol_version"] == "1.0"
    assert d["tenant"] == "acme-corp"

    # Without tenant
    iface2 = AgentInterface(url="https://agent.test", protocol_binding="GRPC", protocol_version="2.0")
    d2 = iface2.to_dict()
    assert "tenant" not in d2


def test_security_scheme_api_key() -> None:
    """API Key security scheme serialization."""
    ss = SecurityScheme(
        scheme_type="apiKey",
        description="API Key auth",
        api_key=APIKeySecurityScheme(name="X-API-Key", key_format="bearer"),
    )
    d = ss.to_dict()
    assert d["scheme_type"] == "apiKey"
    assert d["api_key"]["name"] == "X-API-Key"


def test_security_scheme_http() -> None:
    """HTTP Auth security scheme."""
    ss = SecurityScheme(
        scheme_type="http",
        http_auth=HTTPAuthSecurityScheme(scheme="Bearer"),
    )
    d = ss.to_dict()
    assert d["scheme_type"] == "http"
    assert d["http_auth"]["scheme"] == "Bearer"


def test_security_scheme_oauth2() -> None:
    """OAuth 2.1 security scheme with client_credentials flow."""
    ss = SecurityScheme(
        scheme_type="oauth2",
        description="OAuth 2.1",
        oauth2=OAuth2SecurityScheme(
            flows=OAuthFlows(
                client_credentials=ClientCredentialsOAuthFlow(
                    token_url="https://auth.example.com/token",
                    scopes={"read": "Read access"},
                ),
            ),
        ),
    )
    d = ss.to_dict()
    assert d["scheme_type"] == "oauth2"
    assert d["oauth2"]["flows"]["client_credentials"]["token_url"] == "https://auth.example.com/token"


def test_security_scheme_oauth2_roundtrip() -> None:
    """OAuth 2.1 scheme roundtrip via from_dict."""
    # Build via _dict_to_agent_card
    agent_dict = {
        "name": "Secure Agent",
        "description": "Has OAuth security",
        "security_schemes": {
            "my_oauth": {
                "scheme_type": "oauth2",
                "oauth2": {
                    "flows": {
                        "client_credentials": {
                            "token_url": "https://token.url",
                            "scopes": {"read": "Read"},
                        },
                    },
                },
            },
        },
        "security_requirements": [
            {"schemes": {"my_oauth": ["read"]}},
        ],
    }
    card = AgentCard.from_dict(agent_dict)
    assert "my_oauth" in card.security_schemes
    assert card.security_schemes["my_oauth"].scheme_type == "oauth2"
    assert card.security_schemes["my_oauth"].oauth2.flows.client_credentials.token_url == "https://token.url"
    assert len(card.security_requirements) == 1
    assert card.security_requirements[0].schemes == {"my_oauth": ["read"]}

    # Roundtrip back to dict
    d = card.to_dict()
    assert "security_schemes" in d
    assert d["security_schemes"]["my_oauth"]["scheme_type"] == "oauth2"
    assert "security_requirements" in d


def test_security_requirement_b5() -> None:
    """SecurityRequirement — PM Review B5."""
    sr = SecurityRequirement(schemes={"oauth2": ["read", "write"]})
    d = sr.to_dict()
    assert d["schemes"] == {"oauth2": ["read", "write"]}


def test_agent_card_signature() -> None:
    """AgentCardSignature serialization."""
    sig = AgentCardSignature(alg="RS256", signature="abc123", key_url="https://keys.test/pub", kid="key-1")
    d = sig.to_dict()
    assert d["alg"] == "RS256"
    assert d["key_url"] == "https://keys.test/pub"
    assert d["kid"] == "key-1"


def test_agent_card_with_signatures() -> None:
    """AgentCard with signatures roundtrip."""
    card = AgentCard(
        name="Signed Agent",
        description="Has JWS signature",
        signatures=[
            AgentCardSignature(alg="RS256", signature="sig1", key_url="https://keys.test/1"),
        ],
    )
    d = card.to_dict()
    assert len(d["signatures"]) == 1
    assert d["signatures"][0]["alg"] == "RS256"
    restored = AgentCard.from_dict(d)
    assert len(restored.signatures) == 1
    assert restored.signatures[0].alg == "RS256"


def test_make_agent_card_without_skills() -> None:
    """make_agent_card without skills produces empty list."""
    card = make_agent_card(name="Bare Agent", description="No skills")
    assert card["skills"] == []


def test_agent_card_default_input_output_modes() -> None:
    """Default input/output modes are text/plain."""
    card = AgentCard(name="Default Modes", description="Test modes")
    assert card.default_input_modes == ["text/plain"]
    assert card.default_output_modes == ["text/plain"]
    d = card.to_dict()
    assert d["default_input_modes"] == ["text/plain"]
    assert d["default_output_modes"] == ["text/plain"]


def test_agent_card_custom_modes() -> None:
    """Custom input/output modes are preserved."""
    card = AgentCard(
        name="Custom Modes",
        description="Custom IO modes",
        default_input_modes=["text/plain", "application/json"],
        default_output_modes=["text/markdown"],
    )
    d = card.to_dict()
    assert d["default_input_modes"] == ["text/plain", "application/json"]
    assert d["default_output_modes"] == ["text/markdown"]


def test_all_security_scheme_types() -> None:
    """All 5 security scheme types serialize and deserialize correctly."""
    schemes = {
        "apiKey": SecurityScheme(
            scheme_type="apiKey",
            api_key=APIKeySecurityScheme(name="X-Key", key_format="bearer"),
        ),
        "http": SecurityScheme(
            scheme_type="http",
            http_auth=HTTPAuthSecurityScheme(scheme="Basic"),
        ),
        "oauth2": SecurityScheme(
            scheme_type="oauth2",
            oauth2=OAuth2SecurityScheme(
                flows=OAuthFlows(
                    authorization_code=AuthorizationCodeOAuthFlow(
                        authorization_url="https://auth.com/auth",
                        token_url="https://auth.com/token",
                    ),
                ),
            ),
        ),
        "openIdConnect": SecurityScheme(
            scheme_type="openIdConnect",
            open_id_connect=OpenIdConnectSecurityScheme(
                issuer_url="https://issuer.example.com",
            ),
        ),
        "mutualTls": SecurityScheme(
            scheme_type="mutualTls",
            mtls=MutualTlsSecurityScheme(
                certificate_authority="https://ca.example.com/cert",
            ),
        ),
    }

    for scheme_type, ss in schemes.items():
        d = ss.to_dict()
        assert d["scheme_type"] == scheme_type
        # Deserialize through the full agent card path
        card = AgentCard.from_dict({
            "name": f"{scheme_type} Agent",
            "description": f"Has {scheme_type}",
            "security_schemes": {scheme_type: d},
        })
        assert scheme_type in card.security_schemes
        assert card.security_schemes[scheme_type].scheme_type == scheme_type


def test_a2a_v10_spec_json_example() -> None:
    """Deserialize a realistic A2A v1.0 spec JSON example."""
    spec_json = {
        "name": "Weather Agent",
        "description": "Provides weather forecasts and alerts",
        "version": "1.0.0",
        "supported_interfaces": [
            {
                "url": "https://weather.example.com/a2a",
                "protocol_binding": "JSONRPC",
                "protocol_version": "1.0",
            },
        ],
        "capabilities": {
            "streaming": True,
            "push_notifications": False,
            "extensions": [
                {
                    "name": "weather-alerts",
                    "url": "https://weather.example.com/alerts",
                    "required": False,
                },
            ],
            "extended_agent_card": False,
        },
        "provider": {
            "organization": "Weather Inc.",
            "url": "https://weather.example.com",
        },
        "default_input_modes": ["text/plain"],
        "default_output_modes": ["text/plain", "application/json"],
        "skills": [
            {
                "id": "forecast",
                "name": "Forecast",
                "description": "Get weather forecast for a location",
                "tags": ["weather", "forecast"],
                "examples": ["What's the weather in Tokyo?"],
                "input_modes": ["text/plain"],
                "output_modes": ["text/plain"],
                "security_requirements": [
                    {"schemes": {"forecast-auth": ["forecast:read"]}},
                ],
            },
            {
                "id": "alerts",
                "name": "Weather Alerts",
                "description": "Subscribe to severe weather alerts",
                "tags": ["weather", "alerts"],
            },
        ],
        "security_schemes": {
            "forecast-auth": {
                "scheme_type": "oauth2",
                "description": "OAuth 2.1 for forecast access",
                "oauth2": {
                    "flows": {
                        "client_credentials": {
                            "token_url": "https://auth.weather.example.com/token",
                            "scopes": {"forecast:read": "Read forecast data"},
                        },
                    },
                },
            },
            "api-key-alt": {
                "scheme_type": "apiKey",
                "description": "Alternative API key auth",
                "api_key": {
                    "name": "X-Weather-Key",
                    "key_format": "api-key",
                },
            },
        },
        "security_requirements": [
            {"schemes": {"forecast-auth": ["forecast:read"]}},
        ],
        "signatures": [
            {
                "alg": "RS256",
                "signature": "base64sig...",
                "key_url": "https://weather.example.com/jwks.json",
                "kid": "key-2026",
            },
        ],
        "icon_url": "https://weather.example.com/icon.png",
        "documentation_url": "https://weather.example.com/docs",
    }

    # Deserialize
    card = AgentCard.from_dict(spec_json)

    # Top-level fields
    assert card.name == "Weather Agent"
    assert card.description == "Provides weather forecasts and alerts"
    assert card.version == "1.0.0"
    assert card.documentation_url == "https://weather.example.com/docs"
    assert card.icon_url == "https://weather.example.com/icon.png"
    assert card.default_input_modes == ["text/plain"]
    assert card.default_output_modes == ["text/plain", "application/json"]

    # Interfaces
    assert len(card.supported_interfaces) == 1
    iface = card.supported_interfaces[0]
    assert iface.url == "https://weather.example.com/a2a"
    assert iface.protocol_binding == "JSONRPC"
    assert iface.protocol_version == "1.0"

    # Provider
    assert card.provider is not None
    assert card.provider.organization == "Weather Inc."
    assert card.provider.url == "https://weather.example.com"

    # Capabilities
    assert card.capabilities.streaming is True
    assert card.capabilities.push_notifications is False
    assert card.capabilities.extended_agent_card is False
    assert card.capabilities.extensions is not None
    assert len(card.capabilities.extensions) == 1
    assert card.capabilities.extensions[0].name == "weather-alerts"

    # Skills
    assert len(card.skills) == 2
    assert card.skills[0].id == "forecast"
    assert card.skills[0].name == "Forecast"
    assert card.skills[0].description == "Get weather forecast for a location"
    assert card.skills[0].tags == ["weather", "forecast"]
    assert card.skills[0].examples == ["What's the weather in Tokyo?"]
    assert card.skills[0].security_requirements is not None
    assert len(card.skills[0].security_requirements) == 1
    assert card.skills[1].id == "alerts"

    # Security schemes
    assert "forecast-auth" in card.security_schemes
    assert card.security_schemes["forecast-auth"].scheme_type == "oauth2"
    assert card.security_schemes["forecast-auth"].description == "OAuth 2.1 for forecast access"
    oauth2 = card.security_schemes["forecast-auth"].oauth2
    assert oauth2 is not None
    assert oauth2.flows.client_credentials is not None
    assert oauth2.flows.client_credentials.token_url == "https://auth.weather.example.com/token"
    assert oauth2.flows.client_credentials.scopes == {"forecast:read": "Read forecast data"}

    assert "api-key-alt" in card.security_schemes
    assert card.security_schemes["api-key-alt"].scheme_type == "apiKey"
    assert card.security_schemes["api-key-alt"].api_key is not None
    assert card.security_schemes["api-key-alt"].api_key.name == "X-Weather-Key"

    # Security requirements
    assert len(card.security_requirements) == 1
    assert card.security_requirements[0].schemes == {"forecast-auth": ["forecast:read"]}

    # Signatures
    assert len(card.signatures) == 1
    assert card.signatures[0].alg == "RS256"
    assert card.signatures[0].kid == "key-2026"

    # Roundtrip back to dict preserves structure
    d = card.to_dict()
    assert d["name"] == "Weather Agent"
    assert d["version"] == "1.0.0"
    assert "forecast-auth" in d["security_schemes"]
    assert d["security_schemes"]["forecast-auth"]["scheme_type"] == "oauth2"
    assert d["security_schemes"]["forecast-auth"]["oauth2"]["flows"]["client_credentials"]["token_url"] \
        == "https://auth.weather.example.com/token"