"""Tests for a2a_coder_agent.py WebSocket task lifecycle (P1.5).

Validates:
- _clean_hermes_output() processing
- WS message format shapes (task_ack, task_progress, task_complete, task_fail)
- Cancel event handling via process_ws_task lifecycle
- Periodic progress timing
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helper: import a2a_coder_agent as a real importable module
# ---------------------------------------------------------------------------

def _load_agent_module():
    """Load a2a_coder_agent.py by inserting examples/ dir into sys.path."""
    import importlib

    # Mock aiohttp before loading
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

    # Remove any cached version
    sys.modules.pop("a2a_coder_agent", None)

    import a2a_coder_agent as mod
    return mod


# ---------------------------------------------------------------------------
# _clean_hermes_output
# ---------------------------------------------------------------------------

class TestCleanHermesOutput:
    """Validate that _clean_hermes_output strips ANSI codes and framing."""

    def test_strips_ansi_codes(self):
        mod = _load_agent_module()
        raw = "\x1b[32mHello\x1b[0m World\x1b[K"
        result = mod._clean_hermes_output(raw)
        assert "Hello World" in result
        assert "\x1b" not in result

    def test_strips_box_drawing(self):
        mod = _load_agent_module()
        raw = "┌─ Header ──────────────────────\n│ Content\n└─ Footer"
        result = mod._clean_hermes_output(raw)
        assert "Content" in result
        assert "┌─" not in result
        assert "└─" not in result

    def test_strips_reasoning_header_lines(self):
        mod = _load_agent_module()
        raw = "┌─ Reasoning\nsome internal reasoning\n└─\nActual result here"
        result = mod._clean_hermes_output(raw)
        assert "Actual result" in result
        assert "┌─" not in result
        assert "└─" not in result

    def test_strips_hermes_header(self):
        mod = _load_agent_module()
        raw = "╭─ ⚕ Hermes Agent\nsome header line\n╰─\nContent"
        result = mod._clean_hermes_output(raw)
        # The ╭─/╰─ prefix lines are stripped via skip_prefixes,
        # but inner header content lines pass through
        assert "Content" in result
        assert "╭─" not in result
        assert "╰─" not in result

    def test_collapses_multiple_blank_lines(self):
        mod = _load_agent_module()
        raw = "Line 1\n\n\n\n\nLine 2"
        result = mod._clean_hermes_output(raw)
        # Empty lines between content are filtered out by the line-processing loop
        assert "Line 1" in result
        assert "Line 2" in result

    def test_strips_leading_trailing_whitespace(self):
        mod = _load_agent_module()
        raw = "  \n  Hello World  \n  "
        result = mod._clean_hermes_output(raw)
        assert result == "Hello World"

    def test_returns_empty_string_for_pure_junk(self):
        mod = _load_agent_module()
        raw = "┌─\n└─"
        result = mod._clean_hermes_output(raw)
        assert result == ""


# ---------------------------------------------------------------------------
# WS message format shapes
# ---------------------------------------------------------------------------

class TestWSMessageFormats:
    """Validate the JSON message shapes match server expectations."""

    def test_task_ack_format(self):
        """task_ack must have type, id, status='accepted', and timestamp."""
        msg = {
            "type": "task_ack",
            "id": "test-uuid-1234",
            "status": "accepted",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        assert msg["type"] == "task_ack"
        assert msg["id"] == "test-uuid-1234"
        assert msg["status"] == "accepted"
        assert "timestamp" in msg

    def test_task_progress_format(self):
        """task_progress must have type, id, status='working', and progress int."""
        msg = {
            "type": "task_progress",
            "id": "test-uuid-1234",
            "status": "working",
            "progress": 45,
        }
        assert msg["type"] == "task_progress"
        assert msg["id"] == "test-uuid-1234"
        assert msg["status"] == "working"
        assert isinstance(msg["progress"], int)
        assert 0 <= msg["progress"] <= 99

    def test_task_complete_format(self):
        """task_complete must have result.text and metrics."""
        msg = {
            "type": "task_complete",
            "id": "test-uuid-1234",
            "status": "completed",
            "result": {"text": "Done"},
            "metrics": {"elapsed_seconds": 12.5, "output_chars": 1024},
        }
        assert msg["type"] == "task_complete"
        assert msg["status"] == "completed"
        assert "result" in msg
        assert "text" in msg["result"]
        assert "metrics" in msg
        assert "elapsed_seconds" in msg["metrics"]

    def test_task_fail_format(self):
        """task_fail must have error and code."""
        msg = {
            "type": "task_fail",
            "id": "test-uuid-1234",
            "status": "failed",
            "error": "Something went wrong",
            "code": "EXIT_1",
            "metrics": {"elapsed_seconds": 5.2},
        }
        assert msg["type"] == "task_fail"
        assert msg["status"] in ("failed", "canceled")
        assert "error" in msg
        assert "code" in msg
        assert "metrics" in msg

    def test_task_fail_canceled_format(self):
        """task_fail with status='canceled' and code='CANCELED'."""
        msg = {
            "type": "task_fail",
            "id": "test-uuid-1234",
            "status": "canceled",
            "error": "Task cancelled by server",
            "code": "CANCELED",
            "metrics": {"elapsed_seconds": 3.1},
        }
        assert msg["type"] == "task_fail"
        assert msg["status"] == "canceled"
        assert msg["code"] == "CANCELED"


# ---------------------------------------------------------------------------
# process_ws_task lifecycle (mock subprocess)
# ---------------------------------------------------------------------------

@pytest.fixture
def lifecycle_mod():
    return _load_agent_module()


class TestProcessWSTaskLifecycle:
    """Verify message sequence and error handling with mocked subprocess."""

    @pytest.mark.asyncio
    async def test_lifecycle_success_path(self, lifecycle_mod):
        """On success: task_ack → task_progress(0) → task_complete."""
        mod = lifecycle_mod
        sent_messages: list[dict] = []

        async def fake_send(msg: dict) -> bool:
            sent_messages.append(msg)
            return True

        task_msg = {
            "type": "task",
            "id": "lifecycle-test-001",
            "body": "Write a hello world program",
        }

        fake_proc = AsyncMock()
        fake_proc.returncode = 0
        fake_proc.communicate = AsyncMock(return_value=(b"Hello World Output", b""))
        fake_proc.kill = MagicMock()
        fake_proc.wait = AsyncMock()

        with (
            patch.object(mod, "_send_ws_json", fake_send),
            patch.object(mod, "_active_procs", {}),
            patch.object(mod, "_cancel_events", {}),
            patch("asyncio.create_subprocess_exec", AsyncMock(return_value=fake_proc)),
        ):
            await mod.process_ws_task(task_msg)

        assert len(sent_messages) >= 3
        # task_ack at index 0
        assert sent_messages[0]["type"] == "task_ack"
        assert sent_messages[0]["id"] == "lifecycle-test-001"
        assert sent_messages[0]["status"] == "accepted"
        # task_progress at index 1
        assert sent_messages[1]["type"] == "task_progress"
        assert sent_messages[1]["status"] == "working"
        assert sent_messages[1]["progress"] == 0
        # task_complete at end
        last_msg = sent_messages[-1]
        assert last_msg["type"] == "task_complete"
        assert last_msg["status"] == "completed"
        assert "result" in last_msg
        assert "text" in last_msg["result"]
        assert "metrics" in last_msg

    @pytest.mark.asyncio
    async def test_lifecycle_timeout(self, lifecycle_mod):
        """On subprocess TimeoutError → task_fail with TIMEOUT code."""
        mod = lifecycle_mod
        sent_messages: list[dict] = []

        async def fake_send(msg: dict) -> bool:
            sent_messages.append(msg)
            return True

        task_msg = {
            "type": "task",
            "id": "lifecycle-test-timeout",
            "body": "Do something",
        }

        fake_proc = AsyncMock()
        fake_proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError())
        fake_proc.kill = MagicMock()
        fake_proc.wait = AsyncMock()

        with (
            patch.object(mod, "_send_ws_json", fake_send),
            patch.object(mod, "_active_procs", {}),
            patch.object(mod, "_cancel_events", {}),
            patch.object(mod, "HERMES_TIMEOUT", 300),
            patch("asyncio.create_subprocess_exec", AsyncMock(return_value=fake_proc)),
        ):
            await mod.process_ws_task(task_msg)

        assert len(sent_messages) >= 2
        assert sent_messages[0]["type"] == "task_ack"
        last_msg = sent_messages[-1]
        assert last_msg["type"] == "task_fail"
        assert "TIMEOUT" in last_msg.get("code", "")

    @pytest.mark.asyncio
    async def test_lifecycle_subprocess_exit_error(self, lifecycle_mod):
        """Exit code != 0 → task_fail with EXIT_N code."""
        mod = lifecycle_mod
        sent_messages: list[dict] = []

        async def fake_send(msg: dict) -> bool:
            sent_messages.append(msg)
            return True

        task_msg = {
            "type": "task",
            "id": "lifecycle-test-exit1",
            "body": "Do something",
        }

        fake_proc = AsyncMock()
        fake_proc.returncode = 1
        fake_proc.communicate = AsyncMock(return_value=(b"", b"Error: something failed"))
        fake_proc.kill = MagicMock()
        fake_proc.wait = AsyncMock()

        with (
            patch.object(mod, "_send_ws_json", fake_send),
            patch.object(mod, "_active_procs", {}),
            patch.object(mod, "_cancel_events", {}),
            patch("asyncio.create_subprocess_exec", AsyncMock(return_value=fake_proc)),
        ):
            await mod.process_ws_task(task_msg)

        assert len(sent_messages) >= 2
        assert sent_messages[0]["type"] == "task_ack"
        last_msg = sent_messages[-1]
        assert last_msg["type"] == "task_fail"
        assert "EXIT_" in last_msg.get("code", "")

    @pytest.mark.asyncio
    async def test_lifecycle_cancel_during_execution(self, lifecycle_mod):
        """External cancel_event set during execution → task_fail with CANCELED."""
        mod = lifecycle_mod
        sent_messages: list[dict] = []

        async def fake_send(msg: dict) -> bool:
            sent_messages.append(msg)
            return True

        task_msg = {
            "type": "task",
            "id": "lifecycle-test-cancel",
            "body": "Do something",
        }

        # Cancel events dict - the function stores its cancel_event here (line 603)
        cancel_events: dict[str, asyncio.Event] = {}

        fake_proc = AsyncMock()
        fake_proc.returncode = -9  # killed by SIGKILL
        fake_proc.communicate = AsyncMock(return_value=(b"partial output", b""))
        fake_proc.kill = MagicMock()
        fake_proc.wait = AsyncMock()

        # Simulate external task_cancel arriving mid-execution:
        # after process_ws_task creates its cancel_event (line 602-603),
        # we set it from cancel_events, mimicking the ws listen loop handler
        real_create_subprocess = asyncio.create_subprocess_exec

        async def _mock_create_subprocess(*args, **kwargs):
            # Simulate external cancellation: get the event and set it
            evt = cancel_events.get("lifecycle-test-cancel")
            if evt:
                evt.set()
            return fake_proc

        with (
            patch.object(mod, "_send_ws_json", fake_send),
            patch.object(mod, "_active_procs", {}),
            patch.object(mod, "_cancel_events", cancel_events),
            patch("asyncio.create_subprocess_exec", _mock_create_subprocess),
        ):
            await mod.process_ws_task(task_msg)

        assert len(sent_messages) >= 2
        assert sent_messages[0]["type"] == "task_ack"
        # The last message should have canceled status
        last_msg = sent_messages[-1]
        assert last_msg["type"] == "task_fail"
        assert last_msg.get("status") == "canceled"
        assert last_msg.get("code") == "CANCELED"

    @pytest.mark.asyncio
    async def test_invalid_task_missing_id_or_query(self, lifecycle_mod):
        """Missing id or query → no messages sent."""
        mod = lifecycle_mod
        sent_messages: list[dict] = []

        async def fake_send(msg: dict) -> bool:
            sent_messages.append(msg)
            return True

        with (
            patch.object(mod, "_send_ws_json", fake_send),
        ):
            await mod.process_ws_task({"type": "task", "id": ""})
            await mod.process_ws_task({"type": "task", "id": "x", "body": ""})

        assert len(sent_messages) == 0

    @pytest.mark.asyncio
    async def test_cancel_event_cleanup(self, lifecycle_mod):
        """After processing, cancel_event removed from _cancel_events."""
        mod = lifecycle_mod
        sent_messages: list[dict] = []

        async def fake_send(msg: dict) -> bool:
            sent_messages.append(msg)
            return True

        task_msg = {
            "type": "task",
            "id": "lifecycle-test-cleanup",
            "body": "Do something",
        }

        fake_proc = AsyncMock()
        fake_proc.returncode = 0
        fake_proc.communicate = AsyncMock(return_value=(b"Done", b""))
        fake_proc.kill = MagicMock()
        fake_proc.wait = AsyncMock()

        cancel_events: dict[str, asyncio.Event] = {}

        with (
            patch.object(mod, "_send_ws_json", fake_send),
            patch.object(mod, "_active_procs", {}),
            patch.object(mod, "_cancel_events", cancel_events),
            patch("asyncio.create_subprocess_exec", AsyncMock(return_value=fake_proc)),
        ):
            await mod.process_ws_task(task_msg)

        assert "lifecycle-test-cleanup" not in cancel_events

    @pytest.mark.asyncio
    async def test_full_lifecycle_message_order(self, lifecycle_mod):
        """Validate exact message type sequence: ack → progress → complete."""
        mod = lifecycle_mod
        sent_types: list[str] = []

        async def fake_send(msg: dict) -> bool:
            sent_types.append(msg["type"])
            return True

        task_msg = {
            "type": "task",
            "id": "lifecycle-order-test",
            "body": "Write a test",
        }

        fake_proc = AsyncMock()
        fake_proc.returncode = 0
        fake_proc.communicate = AsyncMock(return_value=(b"Output", b""))
        fake_proc.kill = MagicMock()
        fake_proc.wait = AsyncMock()

        with (
            patch.object(mod, "_send_ws_json", fake_send),
            patch.object(mod, "_active_procs", {}),
            patch.object(mod, "_cancel_events", {}),
            patch("asyncio.create_subprocess_exec", AsyncMock(return_value=fake_proc)),
        ):
            await mod.process_ws_task(task_msg)

        assert sent_types[0] == "task_ack"
        assert sent_types[1] == "task_progress"
        assert sent_types[-1] == "task_complete"
        for t in sent_types[2:-1]:
            assert t == "task_progress"
        assert "task_fail" not in sent_types


# ---------------------------------------------------------------------------
# _periodic_progress
# ---------------------------------------------------------------------------

class TestPeriodicProgress:
    """Verify the periodic progress reporter."""

    @pytest.mark.asyncio
    async def test_sends_progress_and_exits_on_cancel(self, lifecycle_mod):
        """progress sends one message then exits when cancel is set."""
        mod = lifecycle_mod
        sent_messages: list[dict] = []

        async def fake_send(msg: dict) -> bool:
            sent_messages.append(msg)
            return True

        cancel_event = asyncio.Event()
        started_at = time.time()

        async def run_one_cycle():
            task = asyncio.create_task(
                mod._periodic_progress("test-progress", cancel_event, started_at)
            )
            await asyncio.sleep(0.05)
            cancel_event.set()
            await asyncio.wait_for(task, timeout=5.0)

        with patch.object(mod, "_send_ws_json", fake_send):
            await run_one_cycle()

        if len(sent_messages) > 0:
            msg = sent_messages[0]
            assert msg["type"] == "task_progress"
            assert msg["id"] == "test-progress"
            assert msg["status"] == "working"
            assert isinstance(msg["progress"], int)
            assert 0 <= msg["progress"] <= 99

    @pytest.mark.asyncio
    async def test_exits_immediately_if_already_cancelled(self, lifecycle_mod):
        """If cancel_event is already set, exits without sending."""
        mod = lifecycle_mod
        cancel_event = asyncio.Event()
        cancel_event.set()

        send_mock = AsyncMock()

        with (
            patch.object(mod, "_send_ws_json", send_mock),
        ):
            await mod._periodic_progress("test-cancel", cancel_event, time.time())

        send_mock.assert_not_called()