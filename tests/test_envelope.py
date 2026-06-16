"""Tests for TaskEnvelope builder — envelope.py (§6 SCN-02, SCN-03).

Covers ``build_envelope`` and ``build_envelope_from_dtm`` with:
- Full mapping from Task fields to TaskEnvelope fields
- SecurityContext synthesis from task provenance fields
- DTM-based envelope construction
- Edge cases: no body, no tenant, no security_context
- Ingress security fence placeholder always returns True
"""

from __future__ import annotations

from typing import Any, Dict

import pytest

from simple_a2a_registry.orchestration.contract import (
    InteractionMode,
    OutputContract,
    SecurityContext,
    TaskEnvelope,
)
from simple_a2a_registry.orchestration.envelope import (
    build_envelope,
    build_envelope_from_dtm,
    check_ingress_security_fence,
)
from simple_a2a_registry.orchestration.models import Task
from simple_a2a_registry.security.dtm import DelegatedTaskToken


# ---------------------------------------------------------------------------
# build_envelope — from Task object
# ---------------------------------------------------------------------------


class TestBuildEnvelope:
    """build_envelope() from a kanban Task object."""

    def test_task_default_fields(self) -> None:
        """Minimal Task produces a valid TaskEnvelope with defaults."""
        task = Task(id="t_abc123", title="Test")
        env = build_envelope(task)

        assert env.task_id == "t_abc123"
        assert env.interaction_mode is InteractionMode.TASK
        assert env.skill == ""  # assignee is empty
        assert env.input == {}  # no body → empty input
        assert isinstance(env.output_contract, OutputContract)
        assert env.output_contract.required_fields == []
        assert isinstance(env.security_context, SecurityContext)
        assert env.tenant_id == ""
        assert env.workspace_uri is None

    def test_full_task_mapping(self) -> None:
        """Every Task field maps to the correct TaskEnvelope field."""
        task = Task(
            id="t_full",
            title="Full test",
            body="Process report",
            assignee="report-agent",
            tenant="acme-corp",
            workspace_path="/tmp/ws/report-42",
            effective_scope="agent:read",
            delegation_depth=1,
            provenance_chain_id="chain_abc",
        )
        env = build_envelope(task)

        assert env.task_id == "t_full"
        assert env.skill == "report-agent"  # falls back to assignee
        assert env.input == {"body": "Process report"}
        assert env.tenant_id == "acme-corp"
        assert env.workspace_uri == "workspace://acme-corp/tmp/ws/report-42"
        assert env.security_context.effective_scope == "agent:read"
        assert env.security_context.delegation_depth == 1
        assert env.security_context.provenance_chain_id == "chain_abc"

    def test_json_body_parsed(self) -> None:
        """JSON body is parsed into the input dict."""
        task = Task(
            id="t_json",
            body='{"command": "deploy", "env": "prod"}',
            assignee="deployer",
            tenant="t",
        )
        env = build_envelope(task)
        assert env.input == {"command": "deploy", "env": "prod"}

    def test_non_dict_json_body(self) -> None:
        """Non-dict JSON values are wrapped under 'body' key."""
        task = Task(
            id="t_str",
            body='"just a string"',
            assignee="a",
            tenant="t",
        )
        env = build_envelope(task)
        assert env.input == {"body": "just a string"}

    def test_with_explicit_security_context(self) -> None:
        """Explicit SecurityContext is used instead of synthesising."""
        task = Task(id="t_sc", assignee="test", tenant="t")
        explicit = SecurityContext(
            effective_scope="admin:full",
            delegation_depth=5,
            deadline_ms=999999999999,
            provenance_chain_id="explicit_chain",
        )
        env = build_envelope(task, security_context=explicit)
        assert env.security_context is explicit
        assert env.security_context.effective_scope == "admin:full"
        assert env.security_context.delegation_depth == 5
        assert env.security_context.deadline_ms == 999999999999

    def test_with_task_dict_extra_fields(self) -> None:
        """Extra task_dict fields are merged into input."""
        task = Task(id="t_extra", body="base", assignee="a", tenant="t")
        env = build_envelope(task, task_dict={"query": "hello", "sessionId": "s1"})
        assert env.input["body"] == "base"
        assert env.input["query"] == "hello"
        assert env.input["session_id"] == "s1"

    def test_interaction_mode_override(self) -> None:
        """interaction_mode parameter overrides the default TASK."""
        task = Task(id="t_sync", assignee="a", tenant="t")
        env = build_envelope(task, interaction_mode=InteractionMode.SYNC_CALL)
        assert env.interaction_mode is InteractionMode.SYNC_CALL

    def test_workspace_uri_without_tenant(self) -> None:
        """workspace_path without tenant produces a plain path."""
        task = Task(id="t_ws", workspace_path="/tmp/ws/test", assignee="a")
        env = build_envelope(task)
        assert env.workspace_uri == "/tmp/ws/test"  # no tenant prefix

    def test_no_workspace_path(self) -> None:
        """None workspace_path yields None workspace_uri."""
        task = Task(id="t_nows", assignee="a", tenant="t")
        env = build_envelope(task)
        assert env.workspace_uri is None


# ---------------------------------------------------------------------------
# build_envelope_from_dtm — with DelegatedTaskToken
# ---------------------------------------------------------------------------


class TestBuildEnvelopeFromDTM:
    """build_envelope_from_dtm() with a DelegatedTaskToken."""

    def test_dtm_security_context(self) -> None:
        """DTM fields populate the SecurityContext correctly."""
        task = Task(id="t_dtm", assignee="worker-a", tenant="acme")
        dtm = DelegatedTaskToken(
            jti="tok_123",
            sub="worker-a",
            effective_scope="agent:write",
            depth=2,
            exp=5000000.0,
            origin_agent="req-bot",
            origin_tenant="acme",
            delegation_chain=[
                {"agent": "req-bot", "action": "delegate", "jti": "chain_origin"},
            ],
            task_id="t_dtm",
        )
        env = build_envelope_from_dtm(task, dtm)

        assert env.security_context.effective_scope == "agent:write"
        assert env.security_context.delegation_depth == 2
        assert env.security_context.deadline_ms == int(5000000.0 * 1000)
        assert env.security_context.provenance_chain_id == "chain_origin"

    def test_dtm_empty_delegation_chain(self) -> None:
        """Empty delegation chain uses jti as provenance chain id."""
        task = Task(id="t_dtm2", assignee="worker-b", tenant="acme")
        dtm = DelegatedTaskToken(
            jti="tok_456",
            sub="worker-b",
            effective_scope="agent:read",
            depth=0,
            exp=6000000.0,
            origin_agent="req-bot",
            origin_tenant="acme",
            delegation_chain=[],
            task_id="t_dtm2",
        )
        env = build_envelope_from_dtm(task, dtm)

        assert env.security_context.provenance_chain_id == "tok_456"

    def test_dtm_interaction_mode_override(self) -> None:
        """interaction_mode override works with DTM path."""
        task = Task(id="t_dtm3", assignee="c", tenant="t")
        dtm = DelegatedTaskToken(
            jti="tok_789",
            sub="c",
            effective_scope="agent:read",
            depth=0,
            exp=7000000.0,
            origin_agent="req",
            origin_tenant="t",
            task_id="t_dtm3",
        )
        env = build_envelope_from_dtm(
            task, dtm, interaction_mode=InteractionMode.SYNC_CALL,
        )
        assert env.interaction_mode is InteractionMode.SYNC_CALL


# ---------------------------------------------------------------------------
# Ingress security fence (placeholder)
# ---------------------------------------------------------------------------


class TestSecurityFencePlaceholder:
    """check_ingress_security_fence is a placeholder that always passes."""

    async def test_always_allows(self) -> None:
        """Placeholder fence always returns True."""
        env = TaskEnvelope(
            task_id="t_fence",
            interaction_mode=InteractionMode.TASK,
            skill="test",
            input={},
            output_contract=OutputContract(),
            security_context=SecurityContext(),
            tenant_id="t",
        )
        result = await check_ingress_security_fence(env)
        assert result is True


# ---------------------------------------------------------------------------
# to_dict serialization of envelope
# ---------------------------------------------------------------------------


class TestEnvelopeToDict:
    """TaskEnvelope produced by build_envelope serializes correctly."""

    def test_to_dict_includes_correct_fields(self) -> None:
        """to_dict output has all required envelope fields."""
        task = Task(
            id="t_dict",
            body='{"cmd": "build"}',
            assignee="builder",
            tenant="test-corp",
            provenance_chain_id="p_chain",
        )
        env = build_envelope(task)
        d = env.to_dict()

        assert d["task_id"] == "t_dict"
        assert d["interaction_mode"] == "task"
        assert d["skill"] == "builder"
        assert d["input"] == {"cmd": "build"}
        assert "output_contract" in d
        assert "security_context" in d
        assert d["tenant_id"] == "test-corp"
        assert "workspace_uri" not in d  # None dropped

    def test_to_dict_security_context_nested(self) -> None:
        """SecurityContext is serialised as nested dict."""
        task = Task(id="t_sec", assignee="a", tenant="t",
                    provenance_chain_id="chain_1")
        env = build_envelope(task)
        d = env.to_dict()

        sc = d["security_context"]
        assert isinstance(sc, dict)
        assert sc["provenance_chain_id"] == "chain_1"

    def test_sync_call_round_trip(self) -> None:
        """SYNC_CALL envelope round-trips correctly."""
        task = Task(
            id="t_sync_r",
            body='{"msg": "ping"}',
            assignee="pinger",
            tenant="default",
        )
        env = build_envelope(
            task, interaction_mode=InteractionMode.SYNC_CALL,
        )
        d = env.to_dict()
        restored = TaskEnvelope.from_dict(d)

        assert restored.task_id == "t_sync_r"
        assert restored.interaction_mode is InteractionMode.SYNC_CALL


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEnvelopeEdgeCases:
    """Edge cases for the envelope builder."""

    def test_empty_task(self) -> None:
        """Task with only default fields still produces a valid envelope."""
        task = Task()
        env = build_envelope(task)
        assert env.task_id == ""  # no id set
        assert env.interaction_mode is InteractionMode.TASK
        assert isinstance(env.security_context, SecurityContext)

    def test_body_as_none(self) -> None:
        """None body produces empty input dict."""
        task = Task(id="t_body_none", body=None, assignee="a", tenant="t")
        env = build_envelope(task)
        assert env.input == {}

    def test_metadata_extracts_skill(self) -> None:
        """Skill from metadata overrides assignee."""
        task = Task(
            id="t_meta",
            assignee="default-assignee",
            metadata='{"skill": "reporter"}',
            tenant="t",
        )
        env = build_envelope(task)
        assert env.skill == "reporter"

    def test_metadata_extracts_input_schema(self) -> None:
        """input_schema from metadata is included."""
        task = Task(
            id="t_schema",
            assignee="a",
            metadata='{"input_schema": {"type": "object", "properties": {"x": {"type": "string"}}}}',
            tenant="t",
        )
        env = build_envelope(task)
        assert env.input_schema == {"type": "object", "properties": {"x": {"type": "string"}}}
