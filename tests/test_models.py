"""Unit tests for A2A data models."""
from __future__ import annotations

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


def test_make_agent_skill() -> None:
    skill = make_agent_skill(
        skill_id="s1",
        name="Test Skill",
        description="A skill for testing",
        tags=["test"],
    )
    assert skill["id"] == "s1"
    assert skill["name"] == "Test Skill"
    assert skill["tags"] == ["test"]


def test_make_agent_card() -> None:
    card = make_agent_card(
        agent_id="test-agent",
        name="Test Agent",
        description="A test agent card",
        url="https://test.agent",
        skills=[
            make_agent_skill("s1", "Skill One"),
            make_agent_skill("s2", "Skill Two"),
        ],
        provider={"organization": "Test Org", "url": "https://test.org"},
        tags=["demo"],
    )
    assert card["id"] == "test-agent"
    assert card["name"] == "Test Agent"
    assert len(card["capabilities"]["skills"]) == 2
    assert card["provider"]["organization"] == "Test Org"
    assert card["tags"] == ["demo"]


def test_agent_card_to_dict_roundtrip() -> None:
    card = AgentCard(
        id="r1",
        name="Roundtrip",
        description="Test roundtrip",
        url="https://r.test",
        capabilities=AgentCapabilities(
            skills=[AgentSkill(id="s1", name="Skill 1")],
        ),
        provider=Provider(organization="Org", url="https://org"),
        tags=["test"],
        metadata={"key": "value"},
    )
    d = card.to_dict()
    restored = AgentCard.from_dict(d)
    assert restored.id == "r1"
    assert restored.name == "Roundtrip"
    assert restored.metadata == {"key": "value"}
    assert len(restored.capabilities.skills) == 1
    assert restored.capabilities.skills[0].name == "Skill 1"


def test_agent_card_ensure_id() -> None:
    card = AgentCard(name="No ID")
    card.ensure_id()
    assert card.id and len(card.id) > 10


def test_agent_card_none_fields() -> None:
    card = AgentCard(name="Minimal")
    d = card.to_dict()
    assert "name" in d
    assert "authentication" not in d
    assert "notification" not in d
    assert "tags" not in d


def test_authentication() -> None:
    auth = Authentication(schemes=["oauth2", "basic"], credentials="token")
    assert auth.schemes == ["oauth2", "basic"]


def test_notification() -> None:
    note = AgentNotification(url="https://hooks.test/done", events=["done"])
    assert note.url == "https://hooks.test/done"
    assert "done" in note.events


def test_make_agent_card_without_skills() -> None:
    card = make_agent_card(agent_id="bare", name="Bare Agent")
    assert len(card["capabilities"]["skills"]) == 0


def test_agent_card_serializes_metadata() -> None:
    card = AgentCard(
        id="meta-test",
        name="Meta",
        metadata={"custom": 42, "nested": {"a": 1}},
    )
    d = card.to_dict()
    assert d["metadata"]["custom"] == 42
    assert d["metadata"]["nested"]["a"] == 1