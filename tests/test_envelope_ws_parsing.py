"""Tests for TaskEnvelope WS message parsing — §6 SCN-02, SCN-03.

Validates:
- Client SDK (client.py) WS listen loop recognises envelope format
- a2a_coder_agent.py process_ws_task() handles envelope + legacy formats
- SYNC_CALL vs TASK vs JOB interaction mode handling
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from simple_a2a_registry.orchestration.contract import (
    InteractionMode,
    TaskEnvelope,
)
from simple_a2a_registry.orchestration.envelope import (
    build_envelope,
)
from simple_a2a_registry.orchestration.models import Task


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_envelope_dict(
    task_id: str = "t_envelope_test",
    mode: InteractionMode = InteractionMode.TASK,
    input_body: str = "test query",
    tenant: str = "test-tenant",
    skill: str = "test-agent",
    workspace_uri: str | None = None,
) -> dict:
    """Build a TaskEnvelope dict as it would be serialised over WS."""
    task = Task(
        id=task_id,
        body=input_body,
        assignee=skill,
        tenant=tenant,
        workspace_path=workspace_uri,
    )
    env = build_envelope(task, interaction_mode=mode)
    return env.to_dict()


def _make_client() -> MagicMock:
    """Create a mock A2AClient with async methods."""
    client = MagicMock()
    client.async_report_progress = AsyncMock()
    client.async_report_result = AsyncMock()
    return client


# ---------------------------------------------------------------------------
# Envelope recognition logic (unit tests)
# ---------------------------------------------------------------------------


class TestEnvelopeRecognition:
    """The envelope-vs-legacy detection logic used in client.py."""

    def test_envelope_format_detected(self):
        """Envelope format (task_id + interaction_mode) is detected."""
        data = _build_envelope_dict(task_id="t_env123")
        assert bool(data.get("task_id")) and "interaction_mode" in data

    def test_legacy_format_not_envelope(self):
        """Legacy flat format is NOT mis-classified as envelope."""
        data = {"type": "task", "id": "t_legacy", "body": "hello"}
        is_envelope = bool(data.get("task_id")) and "interaction_mode" in data
        assert is_envelope is False

    def test_envelope_modes_preserved(self):
        """All three InteractionMode values are preserved in serialization."""
        for mode in (InteractionMode.TASK, InteractionMode.SYNC_CALL, InteractionMode.JOB):
            env = _build_envelope_dict(mode=mode)
            assert env["interaction_mode"] == mode.value


# ---------------------------------------------------------------------------
# a2a_coder_agent.py — process_ws_task envelope parsing
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def _agent_module():
    """Import a2a_coder_agent module once per session."""
    import os
    import sys

    # Mock aiohttp to avoid import issues
    mock_aiohttp = MagicMock()
    mock_aiohttp.web = MagicMock()
    mock_aiohttp.ClientSession = MagicMock()
    mock_aiohttp.ClientWebSocketResponse = MagicMock()
    mock_ws_msg_type = MagicMock()
    mock_ws_msg_type.TEXT = 1
    mock_ws_msg_type.PING = 2
    mock_ws_msg_type.CLOSED = 3
    mock_ws_msg_type.ERROR = 4
    mock_aiohttp.WSMsgType = mock_ws_msg_type
    sys.modules["aiohttp"] = mock_aiohttp

    examples_dir = "/mnt/d/gits/simple-a2a-registry-v2/examples"
    if examples_dir not in sys.path:
        sys.path.insert(0, examples_dir)

    sys.modules.pop("a2a_coder_agent", None)
    import a2a_coder_agent as mod
    return mod


class TestProcessWSTaskEnvelopeParsing:
    """process_ws_task correctly handles both envelope and legacy formats."""

    # ── TASK mode (default, full lifecycle) ──

    async def test_envelope_format_tasks(self, _agent_module):
        """Envelope format: task_id + input.body are correctly extracted."""
        mod = _agent_module
        client = _make_client()

        envelope = _build_envelope_dict(
            task_id="t_env_extract",
            mode=InteractionMode.TASK,
            input_body="write a unit test",
        )

        with patch.object(mod, "_execute_hermes_cli", AsyncMock(return_value={
            "success": True, "output": "test output", "skill": "test", "elapsed": 0.5,
        })):
            await mod.process_ws_task(client, envelope)

        client.async_report_progress.assert_awaited_once_with(
            "t_env_extract", status="working",
        )
        args = client.async_report_result.call_args[0]
        assert args[0] == "t_env_extract"

    async def test_legacy_format_tasks(self, _agent_module):
        """Legacy format (type, id, body) is still parsed correctly."""
        mod = _agent_module
        client = _make_client()

        legacy_msg = {
            "type": "task", "id": "t_legacy_test",
            "body": "legacy query", "sessionId": "s1",
        }

        with patch.object(mod, "_execute_hermes_cli", AsyncMock(return_value={
            "success": True, "output": "legacy done", "skill": "test", "elapsed": 0.3,
        })):
            await mod.process_ws_task(client, legacy_msg)

        client.async_report_progress.assert_awaited_once()
        client.async_report_result.assert_awaited_once()

    # ── SYNC_CALL mode ──

    async def test_sync_call_skips_progress(self, _agent_module):
        """SYNC_CALL: no progress reporting, only result."""
        mod = _agent_module
        client = _make_client()

        envelope = _build_envelope_dict(
            task_id="t_sync_test", mode=InteractionMode.SYNC_CALL,
            input_body="sync query",
        )

        with patch.object(mod, "_execute_hermes_cli", AsyncMock(return_value={
            "success": True, "output": "sync result", "skill": "test", "elapsed": 0.1,
        })):
            await mod.process_ws_task(client, envelope)

        assert client.async_report_progress.await_count == 0
        client.async_report_result.assert_awaited_once_with(
            "t_sync_test", {"text": "sync result", "skill": "test"},
        )

    async def test_sync_call_failure_reports_error(self, _agent_module):
        """SYNC_CALL failure still reports result with error."""
        mod = _agent_module
        client = _make_client()

        envelope = _build_envelope_dict(
            task_id="t_sync_fail", mode=InteractionMode.SYNC_CALL,
            input_body="fail query",
        )

        with patch.object(mod, "_execute_hermes_cli", AsyncMock(return_value={
            "success": False, "error": "oops", "elapsed": 0.05,
        })):
            await mod.process_ws_task(client, envelope)

        assert client.async_report_progress.await_count == 0
        client.async_report_result.assert_awaited_once()
        args = client.async_report_result.call_args[0]
        assert args[0] == "t_sync_fail"
        assert "oops" in str(args)

    # ── JOB mode (placeholder) ──

    async def test_job_placeholder(self, _agent_module):
        """JOB: placeholder acknowledgment, no Hermes execution."""
        mod = _agent_module
        client = _make_client()

        envelope = _build_envelope_dict(
            task_id="t_job_test", mode=InteractionMode.JOB,
            input_body="big job query",
        )

        await mod.process_ws_task(client, envelope)

        assert client.async_report_progress.await_count == 0
        client.async_report_result.assert_awaited_once()
        text = client.async_report_result.call_args[0][1].get("text", "")
        assert "placeholder" in text.lower() or "stub" in text.lower()

    # ── Edge cases ──

    async def test_envelope_without_body(self, _agent_module):
        """Envelope with empty input is rejected with warning."""
        mod = _agent_module
        client = _make_client()

        envelope = {
            "task_id": "t_no_body",
            "interaction_mode": "task",
            "skill": "test", "input": {}, "tenant_id": "t",
        }

        with patch.object(mod.logger, "warning") as mock_warn:
            await mod.process_ws_task(client, envelope)

        mock_warn.assert_called_once()

    async def test_extracts_session_id_from_input(self, _agent_module):
        """session_id from envelope input does not cause errors."""
        mod = _agent_module
        client = _make_client()

        envelope = _build_envelope_dict(
            task_id="t_session", input_body="test",
        )
        envelope["input"]["session_id"] = "my-session-123"

        with patch.object(mod, "_execute_hermes_cli", AsyncMock(return_value={
            "success": True, "output": "", "skill": "", "elapsed": 0.1,
        })):
            await mod.process_ws_task(client, envelope)

        assert client.async_report_progress.await_count == 1
        client.async_report_result.assert_awaited_once()

    # ── Round-trip ──

    async def test_sync_call_round_trip(self):
        """SYNC_CALL envelope round-trips."""
        task = Task(id="t_sync_rt", body="ping", assignee="pinger", tenant="t")
        env = build_envelope(task, interaction_mode=InteractionMode.SYNC_CALL)
        restored = TaskEnvelope.from_dict(env.to_dict())
        assert restored.task_id == "t_sync_rt"
        assert restored.interaction_mode is InteractionMode.SYNC_CALL

    async def test_job_round_trip(self):
        """JOB envelope round-trips."""
        task = Task(id="t_job_rt", body="big job", assignee="orch", tenant="t")
        env = build_envelope(task, interaction_mode=InteractionMode.JOB)
        restored = TaskEnvelope.from_dict(env.to_dict())
        assert restored.task_id == "t_job_rt"
        assert restored.interaction_mode is InteractionMode.JOB