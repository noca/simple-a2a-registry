"""A2A Agent Card data models.

Pydantic-free data classes with dict serialization for the A2A registry.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional


@dataclass
class Authentication:
    """Authentication info for an A2A agent."""
    schemes: List[str] = field(default_factory=list)
    credentials: str = ""


@dataclass
class AgentNotification:
    """Notification config for async A2A task results."""
    url: str = ""
    events: List[str] = field(default_factory=lambda: ["done"])


@dataclass
class AgentSkill:
    """An individual A2A agent skill."""
    id: str = ""
    name: str = ""
    description: str = ""
    version: str = "1.0.0"
    tags: Optional[List[str]] = None
    # A2A-compliant URI scheme — keys tell the router how to reach this skill
    uri_schemes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return _dataclass_to_dict(self)


@dataclass
class AgentCapabilities:
    """Capability description for an A2A agent."""
    skills: List[AgentSkill] = field(default_factory=list)


@dataclass
class Provider:
    """Provider metadata for an A2A agent."""
    organization: str = ""
    url: str = ""


@dataclass
class AgentCard:
    """An A2A Agent Card — the standard identity document."""
    id: str = ""
    name: str = ""
    description: str = ""
    url: str = ""
    version: str = "1.0.0"
    capabilities: AgentCapabilities = field(default_factory=AgentCapabilities)
    provider: Provider = field(default_factory=Provider)
    authentication: Optional[Authentication] = None
    notification: Optional[AgentNotification] = None
    tags: Optional[List[str]] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def ensure_id(self) -> str:
        """Generate a UUID if no id is set."""
        if not self.id:
            self.id = str(uuid.uuid4())
        return self.id

    def to_dict(self) -> Dict[str, Any]:
        return _dataclass_to_dict(self)

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> AgentCard:
        return _dict_to_agent_card(data)


def make_agent_skill(
    skill_id: str = "",
    name: str = "",
    description: str = "",
    tags: Optional[List[str]] = None,
) -> Dict:
    """Create an AgentSkill as a plain dict (convenience for discovery)."""
    return AgentSkill(
        id=skill_id, name=name, description=description, tags=tags,
    ).to_dict()


def make_agent_card(
    agent_id: str = "",
    name: str = "",
    description: str = "",
    url: str = "",
    skills: Optional[List[Dict]] = None,
    provider: Optional[Dict] = None,
    tags: Optional[List[str]] = None,
) -> Dict:
    """Create an AgentCard as a plain dict (convenience for discovery)."""
    capabilities = AgentCapabilities(
        skills=[_dict_to_skill(s) for s in (skills or [])],
    )
    prov = Provider(**provider) if provider else Provider()
    return AgentCard(
        id=agent_id,
        name=name,
        description=description,
        url=url,
        capabilities=capabilities,
        provider=prov,
        tags=tags,
    ).to_dict()


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
        elif hasattr(value, "__dataclass_fields__"):
            result[field_name] = _dataclass_to_dict(value)
        elif isinstance(value, dict):
            result[field_name] = {
                k: _dataclass_to_dict(v) if hasattr(v, "__dataclass_fields__") else v
                for k, v in value.items()
            }
        else:
            result[field_name] = value
    return result


def _dict_to_skill(d: Dict) -> AgentSkill:
    """Convert a plain dict to an AgentSkill."""
    return AgentSkill(
        id=d.get("id", ""),
        name=d.get("name", ""),
        description=d.get("description", ""),
        version=d.get("version", "1.0.0"),
        tags=d.get("tags"),
        uri_schemes=d.get("uri_schemes", []),
    )


def _dict_to_agent_card(d: Dict) -> AgentCard:
    """Convert a plain dict to an AgentCard."""
    caps_data = d.get("capabilities", {})
    skills = [_dict_to_skill(s) for s in caps_data.get("skills", [])] if isinstance(caps_data, dict) else []

    return AgentCard(
        id=d.get("id", ""),
        name=d.get("name", ""),
        description=d.get("description", ""),
        url=d.get("url", ""),
        version=d.get("version", "1.0.0"),
        capabilities=AgentCapabilities(skills=skills),
        provider=Provider(**d["provider"]) if isinstance(d.get("provider"), dict) else Provider(),
        authentication=Authentication(**d["authentication"]) if isinstance(d.get("authentication"), dict) else None,
        notification=AgentNotification(**d["notification"]) if isinstance(d.get("notification"), dict) else None,
        tags=d.get("tags"),
        metadata=d.get("metadata", {}),
    )