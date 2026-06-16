"""Tests for Agent Runtime Contract data models (T2).

Covers serialization (to_dict / from_dict) round-trips, edge cases,
and enum behavior for:
  - InteractionMode
  - SecurityContext
  - OutputContract
  - TaskEnvelope
"""

from __future__ import annotations

from typing import Any, Dict

import pytest

from simple_a2a_registry.orchestration.contract import (
    InteractionMode,
    OutputContract,
    SecurityContext,
    TaskEnvelope,
    _dataclass_to_dict,
)
from simple_a2a_registry.models import AgentSkill


# ---------------------------------------------------------------------------
# InteractionMode enum
# ---------------------------------------------------------------------------


class TestInteractionMode:
    """InteractionMode enum values and string round-trips."""

    def test_values(self) -> None:
        assert InteractionMode.SYNC_CALL.value == "sync_call"
        assert InteractionMode.TASK.value == "task"
        assert InteractionMode.JOB.value == "job"

    def test_from_string(self) -> None:
        assert InteractionMode("sync_call") is InteractionMode.SYNC_CALL
        assert InteractionMode("task") is InteractionMode.TASK
        assert InteractionMode("job") is InteractionMode.JOB

    def test_unknown_value_raises(self) -> None:
        with pytest.raises(ValueError):
            InteractionMode("invalid_mode")

    def test_all_modes_three(self) -> None:
        assert len(InteractionMode) == 3


# ---------------------------------------------------------------------------
# SecurityContext
# ---------------------------------------------------------------------------


class TestSecurityContext:
    """SecurityContext serialization round-trips and edge cases."""

    def test_defaults(self) -> None:
        ctx = SecurityContext()
        assert ctx.effective_scope == ""
        assert ctx.delegation_depth == 0
        assert ctx.deadline_ms == 0
        assert ctx.provenance_chain_id == ""

    def test_custom_values(self) -> None:
        ctx = SecurityContext(
            effective_scope="agent:admin",
            delegation_depth=2,
            deadline_ms=1781599999000,
            provenance_chain_id="chain_abc123",
        )
        assert ctx.effective_scope == "agent:admin"
        assert ctx.delegation_depth == 2
        assert ctx.deadline_ms == 1781599999000
        assert ctx.provenance_chain_id == "chain_abc123"

    def test_to_dict_drops_none(self) -> None:
        ctx = SecurityContext(effective_scope="read-only")
        d = ctx.to_dict()
        assert d["effective_scope"] == "read-only"
        # All int fields default to 0 — they are not None and are serialized
        assert "deadline_ms" in d
        assert d["deadline_ms"] == 0
        assert "delegation_depth" in d
        assert d["delegation_depth"] == 0

    def test_round_trip(self) -> None:
        original = SecurityContext(
            effective_scope="agent:write",
            delegation_depth=1,
            deadline_ms=1781600000000,
            provenance_chain_id="p_xyz",
        )
        d = original.to_dict()
        restored = SecurityContext.from_dict(d)
        assert restored == original

    def test_round_trip_defaults(self) -> None:
        original = SecurityContext()
        d = original.to_dict()
        restored = SecurityContext.from_dict(d)
        assert restored == original


# ---------------------------------------------------------------------------
# OutputContract
# ---------------------------------------------------------------------------


class TestOutputContract:
    """OutputContract serialization round-trips and edge cases."""

    def test_defaults(self) -> None:
        oc = OutputContract()
        assert oc.required_fields == []

    def test_with_fields(self) -> None:
        oc = OutputContract(required_fields=["result", "status", "error"])
        assert oc.required_fields == ["result", "status", "error"]

    def test_to_dict(self) -> None:
        oc = OutputContract(required_fields=["result"])
        d = oc.to_dict()
        assert d["required_fields"] == ["result"]

    def test_round_trip(self) -> None:
        original = OutputContract(required_fields=["a", "b", "c"])
        d = original.to_dict()
        restored = OutputContract.from_dict(d)
        assert restored == original

    def test_round_trip_empty(self) -> None:
        original = OutputContract()
        d = original.to_dict()
        restored = OutputContract.from_dict(d)
        assert restored == original

    def test_from_dict_extra_keys_ignored(self) -> None:
        restored = OutputContract.from_dict({
            "required_fields": ["x"],
            "extra_key": "should be ignored",
        })
        assert restored.required_fields == ["x"]
        assert not hasattr(restored, "extra_key")


# ---------------------------------------------------------------------------
# TaskEnvelope
# ---------------------------------------------------------------------------


class TestTaskEnvelope:
    """TaskEnvelope — full 7+2 field round-trips with nested dataclasses."""

    def test_default_task_mode(self) -> None:
        env = TaskEnvelope()
        assert env.interaction_mode is InteractionMode.TASK

    def test_minimal_full(self) -> None:
        """Create a fully populated envelope and verify every field."""
        env = TaskEnvelope(
            task_id="t_abc123",
            interaction_mode=InteractionMode.SYNC_CALL,
            skill="echo",
            input_schema={"type": "object", "properties": {"msg": {"type": "string"}}},
            input={"msg": "hello"},
            output_contract=OutputContract(required_fields=["response"]),
            security_context=SecurityContext(
                effective_scope="agent:read",
                delegation_depth=0,
                deadline_ms=1781600000000,
                provenance_chain_id="p_123",
            ),
            tenant_id="acme-corp",
            workspace_uri="workspace://acme/agent-1",
        )
        assert env.task_id == "t_abc123"
        assert env.interaction_mode is InteractionMode.SYNC_CALL
        assert env.skill == "echo"
        assert env.input_schema == {"type": "object", "properties": {"msg": {"type": "string"}}}
        assert env.input == {"msg": "hello"}
        assert env.output_contract.required_fields == ["response"]
        assert env.security_context.effective_scope == "agent:read"
        assert env.tenant_id == "acme-corp"
        assert env.workspace_uri == "workspace://acme/agent-1"

    def test_round_trip_full(self) -> None:
        """Full round-trip with all fields populated."""
        original = TaskEnvelope(
            task_id="t_full",
            interaction_mode=InteractionMode.JOB,
            skill="deploy",
            input_schema={"type": "object"},
            input={"env": "prod"},
            output_contract=OutputContract(required_fields=["deployment_id"]),
            security_context=SecurityContext(
                effective_scope="agent:admin",
                delegation_depth=1,
                deadline_ms=1781700000000,
                provenance_chain_id="chain_full",
            ),
            tenant_id="big-corp",
            workspace_uri="workspace://big-corp/deploy-42",
        )
        d = original.to_dict()
        restored = TaskEnvelope.from_dict(d)

        assert restored.task_id == original.task_id
        assert restored.interaction_mode is original.interaction_mode
        assert restored.skill == original.skill
        assert restored.input_schema == original.input_schema
        assert restored.input == original.input
        assert restored.output_contract == original.output_contract
        assert restored.security_context == original.security_context
        assert restored.tenant_id == original.tenant_id
        assert restored.workspace_uri == original.workspace_uri

    def test_round_trip_sync_call(self) -> None:
        """SYNC_CALL mode — minimal envelope."""
        original = TaskEnvelope(
            task_id="t_sync",
            interaction_mode=InteractionMode.SYNC_CALL,
            skill="ping",
            input={"target": "agent-1"},
            output_contract=OutputContract(),
            security_context=SecurityContext(
                effective_scope="agent:ping",
                delegation_depth=0,
                deadline_ms=1781600000000,
                provenance_chain_id="p_sync",
            ),
            tenant_id="default",
        )
        d = original.to_dict()
        restored = TaskEnvelope.from_dict(d)
        assert restored.interaction_mode is InteractionMode.SYNC_CALL
        assert restored.task_id == "t_sync"

    def test_to_dict_interaction_mode_is_string(self) -> None:
        """Ensure interaction_mode is serialized as a string, not an enum."""
        env = TaskEnvelope(interaction_mode=InteractionMode.JOB)
        d = env.to_dict()
        assert d["interaction_mode"] == "job"
        assert isinstance(d["interaction_mode"], str)

    def test_to_dict_drops_none_optional_fields(self) -> None:
        """Optional fields (input_schema, workspace_uri) are dropped when None."""
        env = TaskEnvelope(
            task_id="t_min",
            skill="test",
            input={},
            output_contract=OutputContract(),
            security_context=SecurityContext(),
            tenant_id="t",
        )
        d = env.to_dict()
        assert "input_schema" not in d
        assert "workspace_uri" not in d

    def test_from_dict_none_optional_fields(self) -> None:
        """Missing optional fields are restored as None."""
        restored = TaskEnvelope.from_dict({
            "task_id": "t_opt",
            "interaction_mode": "sync_call",
            "skill": "test",
            "input": {},
            "output_contract": {"required_fields": []},
            "security_context": {
                "effective_scope": "",
                "delegation_depth": 0,
                "deadline_ms": 0,
                "provenance_chain_id": "",
            },
            "tenant_id": "t",
        })
        assert restored.input_schema is None
        assert restored.workspace_uri is None

    def test_empty_input(self) -> None:
        """Empty dict for input is valid."""
        env = TaskEnvelope(
            task_id="t_empty",
            skill="test",
            input={},
            output_contract=OutputContract(),
            security_context=SecurityContext(),
            tenant_id="t",
        )
        d = env.to_dict()
        assert d["input"] == {}

    def test_from_dict_string_interaction_mode(self) -> None:
        """from_dict accepts string interaction_mode."""
        restored = TaskEnvelope.from_dict({
            "task_id": "t_str",
            "interaction_mode": "task",
            "skill": "test",
            "input": {},
            "output_contract": {"required_fields": []},
            "security_context": {
                "effective_scope": "",
                "delegation_depth": 0,
                "deadline_ms": 0,
                "provenance_chain_id": "",
            },
            "tenant_id": "t",
        })
        assert restored.interaction_mode is InteractionMode.TASK

    def test_from_dict_enum_interaction_mode(self) -> None:
        """from_dict also accepts an InteractionMode enum value directly."""
        restored = TaskEnvelope.from_dict({
            "task_id": "t_enum",
            "interaction_mode": InteractionMode.SYNC_CALL,
            "skill": "test",
            "input": {},
            "output_contract": {"required_fields": []},
            "security_context": {
                "effective_scope": "",
                "delegation_depth": 0,
                "deadline_ms": 0,
                "provenance_chain_id": "",
            },
            "tenant_id": "t",
        })
        assert restored.interaction_mode is InteractionMode.SYNC_CALL

    def test_nested_output_contract_from_dict(self) -> None:
        """OutputContract nested inside TaskEnvelope deserializes correctly."""
        restored = TaskEnvelope.from_dict({
            "task_id": "t_nest",
            "interaction_mode": "task",
            "skill": "test",
            "input": {},
            "output_contract": {"required_fields": ["result"]},
            "security_context": {
                "effective_scope": "",
                "delegation_depth": 0,
                "deadline_ms": 0,
                "provenance_chain_id": "",
            },
            "tenant_id": "t",
        })
        assert isinstance(restored.output_contract, OutputContract)
        assert restored.output_contract.required_fields == ["result"]

    def test_nested_security_context_from_dict(self) -> None:
        """SecurityContext nested inside TaskEnvelope deserializes correctly."""
        restored = TaskEnvelope.from_dict({
            "task_id": "t_sec",
            "interaction_mode": "task",
            "skill": "test",
            "input": {},
            "output_contract": {"required_fields": []},
            "security_context": {
                "effective_scope": "agent:admin",
                "delegation_depth": 3,
                "deadline_ms": 999999999999,
                "provenance_chain_id": "chain_sec",
            },
            "tenant_id": "t",
        })
        assert isinstance(restored.security_context, SecurityContext)
        assert restored.security_context.delegation_depth == 3
        assert restored.security_context.provenance_chain_id == "chain_sec"


# ---------------------------------------------------------------------------
# TaskEnvelope — from_dict for missing nested objects
# ---------------------------------------------------------------------------


class TestTaskEnvelopeMissingNested:
    """TaskEnvelope.from_dict handles missing/malformed nested objects."""

    def test_missing_output_contract(self) -> None:
        restored = TaskEnvelope.from_dict({
            "task_id": "t_no_oc",
            "interaction_mode": "task",
            "skill": "test",
            "input": {},
            "tenant_id": "t",
        })
        assert restored.output_contract is None

    def test_missing_security_context(self) -> None:
        restored = TaskEnvelope.from_dict({
            "task_id": "t_no_sc",
            "interaction_mode": "task",
            "skill": "test",
            "input": {},
            "tenant_id": "t",
        })
        assert restored.security_context is None

    def test_output_contract_as_none(self) -> None:
        restored = TaskEnvelope.from_dict({
            "task_id": "t_oc_none",
            "interaction_mode": "task",
            "skill": "test",
            "input": {},
            "output_contract": None,
            "security_context": {
                "effective_scope": "",
                "delegation_depth": 0,
                "deadline_ms": 0,
                "provenance_chain_id": "",
            },
            "tenant_id": "t",
        })
        assert restored.output_contract is None


# ---------------------------------------------------------------------------
# AgentSkill extension
# ---------------------------------------------------------------------------


class TestAgentSkillSchemaExtensions:
    """input_schema / output_schema on AgentSkill."""

    def test_defaults(self) -> None:
        skill = AgentSkill(id="s1", name="Echo", description="echo")
        assert skill.input_schema is None
        assert skill.output_schema is None

    def test_with_schemas(self) -> None:
        skill = AgentSkill(
            id="s2",
            name="FormFiller",
            description="fills forms",
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "age": {"type": "integer"},
                },
            },
            output_schema={
                "type": "object",
                "properties": {
                    "submitted": {"type": "boolean"},
                },
            },
        )
        assert skill.input_schema["properties"]["name"]["type"] == "string"
        assert skill.output_schema["properties"]["submitted"]["type"] == "boolean"

    def test_to_dict_includes_schemas(self) -> None:
        skill = AgentSkill(
            id="s3",
            name="Validator",
            description="validates",
            input_schema={"type": "object"},
            output_schema={"type": "object"},
        )
        d = skill.to_dict()
        assert d["input_schema"] == {"type": "object"}
        assert d["output_schema"] == {"type": "object"}

    def test_to_dict_drops_none_schemas(self) -> None:
        skill = AgentSkill(id="s4", name="Plain", description="plain")
        d = skill.to_dict()
        assert "input_schema" not in d
        assert "output_schema" not in d

    def test_round_trip_via_card(self) -> None:
        """Round-trip through AgentCard serialization preserves schemas."""
        from simple_a2a_registry.models import AgentCard

        skill = AgentSkill(
            id="s5",
            name="SmartSkill",
            description="has schemas",
            input_schema={"type": "object"},
            output_schema={"type": "object"},
        )
        card = AgentCard(
            name="SmartAgent",
            description="test",
            skills=[skill],
        )
        d = card.to_dict()
        restored_card = AgentCard.from_dict(d)
        restored = restored_card.skills[0]
        assert restored.input_schema == {"type": "object"}
        assert restored.output_schema == {"type": "object"}

    def test_from_dict_skill_with_schemas(self) -> None:
        """_dict_to_skill (internal) reconstructs schema fields."""
        from simple_a2a_registry.models import _dict_to_skill

        skill = _dict_to_skill({
            "id": "s6",
            "name": "FromDict",
            "description": "from dict",
            "input_schema": {"type": "object"},
            "output_schema": None,
        })
        assert skill.input_schema == {"type": "object"}
        assert skill.output_schema is None


# ---------------------------------------------------------------------------
# Helper: _dataclass_to_dict
# ---------------------------------------------------------------------------


class TestDataclassToDictHelper:
    """Low-level _dataclass_to_dict helper covers compound types."""

    def test_list_of_dataclasses(self) -> None:
        """Lists of nested dataclasses are serialized."""
        from dataclasses import dataclass

        @dataclass
        class Inner:
            name: str = ""

        @dataclass
        class Outer:
            items: list = None

        outer = Outer(items=[Inner(name="a"), Inner(name="b")])
        d = _dataclass_to_dict(outer)
        assert d["items"] == [{"name": "a"}, {"name": "b"}]

    def test_dict_of_dataclasses(self) -> None:
        """Dicts of nested dataclass values are serialized."""
        from dataclasses import dataclass

        @dataclass
        class Value:
            x: int = 0

        @dataclass
        class Container:
            mapping: dict = None

        c = Container(mapping={"k1": Value(x=42)})
        d = _dataclass_to_dict(c)
        assert d["mapping"] == {"k1": {"x": 42}}

    def test_plain_value(self) -> None:
        """Plain values pass through unchanged."""
        from dataclasses import dataclass

        @dataclass
        class Simple:
            a: str = ""
            b: int = 0

        d = _dataclass_to_dict(Simple(a="hello", b=42))
        assert d == {"a": "hello", "b": 42}


# ---------------------------------------------------------------------------
# Integration: TaskEnvelope with JOB mode and workspace_uri
# ---------------------------------------------------------------------------


class TestTaskEnvelopeIntegration:
    """Integration-level scenarios combining multiple features."""

    def test_job_with_workspace(self) -> None:
        env = TaskEnvelope(
            task_id="t_job_1",
            interaction_mode=InteractionMode.JOB,
            skill="ci-pipeline",
            input={"repo": "myapp", "branch": "main"},
            output_contract=OutputContract(required_fields=["build_id", "status"]),
            security_context=SecurityContext(
                effective_scope="ci:trigger",
                delegation_depth=0,
                deadline_ms=1781800000000,
                provenance_chain_id="chain_ci",
            ),
            tenant_id="eng",
            workspace_uri="workspace://eng/ci-42",
        )
        d = env.to_dict()
        restored = TaskEnvelope.from_dict(d)
        assert restored.interaction_mode is InteractionMode.JOB
        assert restored.workspace_uri == "workspace://eng/ci-42"
        assert "build_id" in restored.output_contract.required_fields
        assert "status" in restored.output_contract.required_fields