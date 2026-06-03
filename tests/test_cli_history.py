"""Tests for cli_history.py — audit log history CLI subcommands.

Tests cover:
- Argument parsing for ``history list`` and ``history show``
- Output formatting helpers
- HTTP error handling
- Formatter edge cases (empty results, None timestamps, etc.)
"""

from __future__ import annotations

import sys
from io import StringIO
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest

from simple_a2a_registry.cli_history import (
    build_history_parser,
    _fmt_ts,
    _event_icon,
    _print_event_table,
    _print_event_detail,
    cmd_history_list,
    cmd_history_show,
    _api_get,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def parser() -> Any:
    """Build the history parser (using argparse subparsers)."""
    import argparse
    parent = argparse.ArgumentParser()
    subs = parent.add_subparsers()
    build_history_parser(subs)
    return parent


@pytest.fixture
def sample_events() -> list:
    """A small list of sample audit events matching the API response shape."""
    return [
        {
            "id": 1,
            "timestamp": 1700000000.0,
            "event_type": "AGENT_REGISTER",
            "actor": "agent-1",
            "target": "agent-1",
            "detail": "{\"name\": \"Bot\"}",
            "success": True,
            "tenant_id": "",
        },
        {
            "id": 2,
            "timestamp": 1700000100.0,
            "event_type": "TASK_DISPATCH",
            "actor": "dispatcher",
            "target": "task_t_abc123",
            "detail": "{\"priority\": 5}",
            "success": True,
            "tenant_id": "tenant-x",
        },
        {
            "id": 3,
            "timestamp": 1700000200.0,
            "event_type": "AUTH_FAILURE",
            "actor": "unknown",
            "target": "10.0.0.1",
            "detail": "invalid token",
            "success": False,
            "tenant_id": "",
        },
    ]


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


class TestParseList:
    """``history list`` argument parsing."""

    def test_basic(self, parser: Any) -> None:
        args = parser.parse_args(["history", "list"])
        assert args.history_command == "list"
        assert args.event_type is None
        assert args.actor is None
        assert args.since is None
        assert args.until is None
        assert args.limit == 100
        assert args.offset == 0
        assert args.json is False

    def test_all_filters(self, parser: Any) -> None:
        args = parser.parse_args([
            "history", "list",
            "--event-type", "AGENT_REGISTER",
            "--actor", "agent-1",
            "--since", "1700000000",
            "--until", "1700001000",
            "--limit", "50",
            "--offset", "10",
            "--json",
        ])
        assert args.event_type == "AGENT_REGISTER"
        assert args.actor == "agent-1"
        assert args.since == 1700000000.0
        assert args.until == 1700001000.0
        assert args.limit == 50
        assert args.offset == 10
        assert args.json is True


class TestParseShow:
    """``history show <event-id>`` argument parsing."""

    def test_basic(self, parser: Any) -> None:
        args = parser.parse_args(["history", "show", "42"])
        assert args.history_command == "show"
        assert args.event_id == "42"
        assert args.json is False

    def test_with_json(self, parser: Any) -> None:
        args = parser.parse_args(["history", "show", "99", "--json"])
        assert args.event_id == "99"
        assert args.json is True


# ---------------------------------------------------------------------------
# Formatter helpers
# ---------------------------------------------------------------------------


class TestFmtTs:
    """``_fmt_ts`` timestamp formatting."""

    def test_none(self) -> None:
        assert _fmt_ts(None) == "-"

    def test_known_timestamp(self) -> None:
        # 1700000000 = 2023-11-14 22:13:20 UTC
        result = _fmt_ts(1700000000.0)
        assert "2023-11-14" in result
        assert "UTC" in result


class TestEventIcon:
    """``_event_icon`` visual icon mapping."""

    def test_known_types(self) -> None:
        assert _event_icon("AGENT_REGISTER") == "◈"
        assert _event_icon("AUTH_FAILURE") == "✗"
        assert _event_icon("TASK_DISPATCH") == "▶"

    def test_unknown_type(self) -> None:
        assert _event_icon("SOME_UNKNOWN_TYPE") == "?"


# ---------------------------------------------------------------------------
# Print Event Table
# ---------------------------------------------------------------------------


class TestPrintEventTable:
    """``_print_event_table`` table output."""

    def test_empty(self) -> None:
        out = StringIO()
        with patch("sys.stdout", out):
            _print_event_table([], total=0, limit=100, offset=0, server="http://localhost:8321")
        assert "No audit events found." in out.getvalue()

    def test_with_events(self, sample_events: list) -> None:
        out = StringIO()
        with patch("sys.stdout", out):
            _print_event_table(sample_events, total=3, limit=100, offset=0, server="http://localhost:8321")
        output = out.getvalue()
        # Header
        assert "ID" in output
        assert "Timestamp" in output
        assert "Event Type" in output
        # Event data
        assert "AGENT_REGISTER" in output
        assert "TASK_DISPATCH" in output
        assert "AUTH_FAILURE" in output
        # Footer
        assert "Showing 3 of 3 events" in output

    def test_pagination_hint(self):
        """Shows next-page hint when total > offset + limit."""
        out = StringIO()
        with patch("sys.stdout", out):
            _print_event_table([{"id": "1", "event_type": "TEST", "actor": "a", "target": "b", "success": True}],
                               total=150, limit=100, offset=0, server="http://localhost:8321")
        assert "Use --offset 100 to see next page" in out.getvalue()


# ---------------------------------------------------------------------------
# Print Event Detail
# ---------------------------------------------------------------------------


class TestPrintEventDetail:
    """``_print_event_detail`` single event output."""

    def test_basic(self, sample_events: list) -> None:
        out = StringIO()
        with patch("sys.stdout", out):
            _print_event_detail(sample_events[0])
        output = out.getvalue()
        assert "Event #1" in output
        assert "AGENT_REGISTER" in output
        assert "Bot" in output  # detail JSON content

    def test_failed_event(self, sample_events: list) -> None:
        out = StringIO()
        with patch("sys.stdout", out):
            _print_event_detail(sample_events[2])
        output = out.getvalue()
        assert "AUTH_FAILURE" in output
        assert "invalid token" in output
        assert "✗" in output


# ---------------------------------------------------------------------------
# HTTP helper (_api_get)
# ---------------------------------------------------------------------------


class TestApiGet:
    """``_api_get`` HTTP request helper."""

    @patch("requests.get")
    def test_success(self, mock_get: MagicMock) -> None:
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {"events": []}
        result = _api_get("/admin/audit")
        assert result == {"events": []}
        mock_get.assert_called_once_with(
            "http://localhost:8321/admin/audit",
            params=None,
            timeout=30,
        )

    @patch("requests.get")
    def test_with_params(self, mock_get: MagicMock) -> None:
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {"total": 5}
        result = _api_get("/admin/audit", params={"event_type": "AGENT_REGISTER", "limit": 10})
        assert result == {"total": 5}
        mock_get.assert_called_once_with(
            "http://localhost:8321/admin/audit",
            params={"event_type": "AGENT_REGISTER", "limit": 10},
            timeout=30,
        )

    @patch("requests.get")
    def test_404(self, mock_get: MagicMock) -> None:
        mock_get.return_value.status_code = 404
        mock_get.return_value.text = "Not Found"
        mock_get.return_value.json.side_effect = ValueError
        with pytest.raises(SystemExit):
            _api_get("/admin/audit/999")

    @patch("requests.get")
    def test_connection_error(self, mock_get: MagicMock) -> None:
        from requests.exceptions import ConnectionError as ReqConnError
        mock_get.side_effect = ReqConnError("connection refused")
        with pytest.raises(SystemExit):
            _api_get("/admin/audit")


# ---------------------------------------------------------------------------
# Command handlers (with mocked HTTP)
# ---------------------------------------------------------------------------


class TestCmdHistoryList:
    """``cmd_history_list`` with mocked API calls."""

    @patch("simple_a2a_registry.cli_history._api_get")
    def test_json_output(self, mock_api: MagicMock) -> None:
        mock_api.return_value = {"events": [], "total": 0, "limit": 100, "offset": 0}
        args = MagicMock()
        args.event_type = None
        args.actor = None
        args.since = None
        args.until = None
        args.limit = 100
        args.offset = 0
        args.server = "http://localhost:8321"
        args.json = True

        out = StringIO()
        with patch("sys.stdout", out):
            cmd_history_list(args)
        assert '"events"' in out.getvalue()

    @patch("simple_a2a_registry.cli_history._api_get")
    def test_table_output(self, mock_api: MagicMock) -> None:
        mock_api.return_value = {
            "events": [{"id": 1, "timestamp": 1700000000.0, "event_type": "AGENT_REGISTER",
                         "actor": "agent-1", "target": "agent-1", "success": True,
                         "detail": "", "tenant_id": ""}],
            "total": 1,
            "limit": 100,
            "offset": 0,
        }
        args = MagicMock()
        args.event_type = None
        args.actor = None
        args.since = None
        args.until = None
        args.limit = 100
        args.offset = 0
        args.server = "http://localhost:8321"
        args.json = False

        out = StringIO()
        with patch("sys.stdout", out):
            cmd_history_list(args)
        assert "AGENT_REGISTER" in out.getvalue()


class TestCmdHistoryShow:
    """``cmd_history_show`` with mocked API calls."""

    @patch("simple_a2a_registry.cli_history._api_get")
    def test_show_json(self, mock_api: MagicMock) -> None:
        mock_api.return_value = {
            "events": [
                {"id": 42, "timestamp": 1700000000.0, "event_type": "AGENT_REGISTER",
                 "actor": "bot", "target": "bot", "success": True,
                 "detail": "{}", "tenant_id": ""},
            ],
            "total": 1,
        }
        args = MagicMock()
        args.event_id = "42"
        args.server = "http://localhost:8321"
        args.json = True

        out = StringIO()
        with patch("sys.stdout", out):
            cmd_history_show(args)
        assert '"id": 42' in out.getvalue()

    @patch("simple_a2a_registry.cli_history._api_get")
    def test_show_not_found(self, mock_api: MagicMock) -> None:
        mock_api.return_value = {"events": [], "total": 0}
        args = MagicMock()
        args.event_id = "999"
        args.server = "http://localhost:8321"
        args.json = False

        with pytest.raises(SystemExit):
            cmd_history_show(args)

    def test_invalid_event_id(self) -> None:
        args = MagicMock()
        args.event_id = "not-a-number"
        with pytest.raises(SystemExit):
            cmd_history_show(args)


# ---------------------------------------------------------------------------
# Parser validation: error on missing subcommand
# ---------------------------------------------------------------------------


class TestParserErrors:
    """Parser error handling for missing/invalid subcommands."""

    def test_missing_subcommand(self, parser: Any) -> None:
        with pytest.raises(SystemExit):
            parser.parse_args(["history"])

    def test_invalid_subcommand(self, parser: Any) -> None:
        with pytest.raises(SystemExit):
            parser.parse_args(["history", "invalid-sub"])