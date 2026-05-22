"""Simple A2A Registry — Agent-to-Agent Discovery Server.

A spec-compliant A2A Registry that discovers agent profiles and skills
as Agent Cards, and accepts external agent registrations with heartbeat-based
liveness monitoring.
"""

from simple_a2a_registry.models import (
    AgentCard,
    AgentCapabilities,
    AgentSkill,
    Authentication,
    AgentNotification,
    Provider,
    make_agent_card,
    make_agent_skill,
)

from simple_a2a_registry.discovery import discover_profiles

__all__ = [
    "AgentCard",
    "AgentCapabilities",
    "AgentSkill",
    "Authentication",
    "AgentNotification",
    "Provider",
    "make_agent_card",
    "make_agent_skill",
    "discover_profiles",
]