"""Tests for the CLI task subcommands (cli_task.py).

Tests parser construction, argument parsing, formatters, and HTTP error handling.
HTTP calls are mocked — no running server required.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from simple_a2a_registry.cli import build_parser
from simple_a2a_registry.cli_task import (
    _fmt_ts,
    _print_task_detail,
    _print_task_table,
    _status_icon,
    cmd_task_list,
    cmd_task_show,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def parser():
    return build_parser()


@pytest.fixture
def sample_tasks():
    return {
        "total": 3,
        "limit": 50,
        "offset": 0,
        "tasks": [
            {
                "id": "t_aaa11111",
                "title": "Setup CI/CD pipeline",
                "status": "running",
                "assignee": "devops",
                "priority": 2,
                "created_at": 1700000000,
                "started_at": 1700000100,
                "completed_at": None,
                "tenant": "prod",
            },
            {
                "id": "t_bbb22222",
                "title": "Write API documentation",
                "status": "todo",
                "assignee": "writer",
                "priority": 1,
                "created_at": 1700000200,
                "started_at": None,
                "completed_at": None,
                "tenant": None,
            },
            {
                "id": "t_ccc33333",
                "title": "Review PR #42",
                "status": "completed",
                "assignee": "senior-dev",
                "priority": 3,
                "created_at": 1699999000,
                "started_at": 1699999200,
                "completed_at": 1700000500,
                "tenant": None,
            },
        ],
    }


@pytest.fixture
def sample_task_detail():
    return {
        "task": {
            "id": "t_aaa11111",
            "title": "Setup CI/CD pipeline",
            "body": "Configure GitHub Actions for automated testing and deployment.",
            "status": "running",
            "assignee": "devops",
            "priority": 2,
            "tenant": "prod",
            "created_by": "admin",
            "created_at": 1700000000,
            "started_at": 1700000100,
            "completed_at": None,
            "workspace_kind": "scratch",
            "workspace_path": "/tmp/workspace/t_aaa11111",
            "max_runtime_seconds": 3600,
            "max_retries": 3,
            "consecutive_failures": 0,
            "current_run_id": 42,
            "result": None,
        },
        "parents": [
            {"id": "t_root0000", "title": "Infra setup", "status": "completed"},
        ],
        "children": [
            {"id": "t_dep1111", "title": "Deploy to staging", "status": "todo"},
        ],
        "runs": [
            {
                "id": 42,
                "task_id": "t_aaa11111",
                "profile": "devops",
                "status": "running",
                "worker_pid": 12345,
                "started_at": 1700000100,
                "ended_at": None,
                "outcome": None,
                "summary": None,
                "error": None,
            },
            {
                "id": 41,
                "task_id": "t_aaa11111",
                "profile": "devops",
                "status": "done",
                "worker_pid": 12300,
                "started_at": 1699999000,
                "ended_at": 1700000000,
                "outcome": "completed",
                "summary": "First attempt, completed successfully",
                "error": None,
            },
        ],
        "comments": [
            {
                "id": 1,
                "task_id": "t_aaa11111",
                "author": "admin",
                "body": "Please prioritize this task",
                "created_at": 1700000050,
            },
        ],
        "events": [
            {
                "id": 10,
                "task_id": "t_aaa11111",
                "run_id": 41,
                "kind": "created",
                "payload": '{"assignee": "devops"}',
                "created_at": 1700000000,
            },
            {
                "id": 11,
                "task_id": "t_aaa11111",
                "run_id": 41,
                "kind": "claimed",
                "payload": None,
                "created_at": 1700000010,
            },
        ],
    }


# ===================================================================
# Parser tests
# ===================================================================


class TestTaskParser:
    def test_parser_builds_successfully(self, parser):
        """Parser fixture creates without error."""
        assert parser is not None

    def test_task_list_no_args(self, parser):
        """task list parses with minimal args."""
        ns = parser.parse_args(["task", "list"])
        assert ns.command == "task"
        assert ns.task_command == "list"
        assert ns.status is None
        assert ns.assignee is None
        assert ns.limit == 50
        assert ns.offset == 0
        assert ns.server == "http://localhost:8321"

    def test_task_list_all_filters(self, parser):
        """task list parses all filter arguments."""
        ns = parser.parse_args([
            "task", "list",
            "--status", "running,completed",
            "--assignee", "coder",
            "--tenant", "prod",
            "--parent-id", "t_parent123",
            "--q", "deploy",
            "--limit", "10",
            "--offset", "20",
            "--sort", "priority",
            "--json",
            "--server", "http://localhost:9000",
        ])
        assert ns.status == "running,completed"
        assert ns.assignee == "coder"
        assert ns.tenant == "prod"
        assert ns.parent_id == "t_parent123"
        assert ns.q == "deploy"
        assert ns.limit == 10
        assert ns.offset == 20
        assert ns.sort == "priority"
        assert ns.json is True
        assert ns.server == "http://localhost:9000"

    def test_task_show_parses(self, parser):
        """task show parses task_id."""
        ns = parser.parse_args(["task", "show", "t_abc12345"])
        assert ns.command == "task"
        assert ns.task_command == "show"
        assert ns.task_id == "t_abc12345"
        assert ns.server == "http://localhost:8321"

    def test_task_show_json(self, parser):
        """task show supports --json flag."""
        ns = parser.parse_args(["task", "show", "t_xyz", "--json"])
        assert ns.task_id == "t_xyz"
        assert ns.json is True

    def test_task_show_custom_server(self, parser):
        """task show supports --server flag."""
        ns = parser.parse_args(["task", "show", "t_xyz", "--server", "http://10.0.0.1:8321"])
        assert ns.server == "http://10.0.0.1:8321"


# ===================================================================
# Formatter tests
# ===================================================================


class TestFormatters:
    def test_status_icon(self):
        assert _status_icon("todo") == "○"
        assert _status_icon("running") == "▶"
        assert _status_icon("completed") == "✓"
        assert _status_icon("failed") == "✗"
        assert _status_icon("unknown") == "?"

    def test_fmt_ts_none(self):
        assert _fmt_ts(None) == "-"

    def test_fmt_ts_valid(self):
        result = _fmt_ts(1700000000)
        assert "2023-11-14" in result  # Known timestamp
        assert "UTC" in result

    def test_print_task_table_empty(self, capsys):
        """Empty task list prints 'No tasks found'."""
        _print_task_table([], total=0, limit=50, offset=0, server="http://localhost:8321")
        captured = capsys.readouterr()
        assert "No tasks found" in captured.out

    def test_print_task_table_with_data(self, capsys, sample_tasks):
        """Table output includes header row and task data."""
        _print_task_table(
            sample_tasks["tasks"],
            total=sample_tasks["total"],
            limit=sample_tasks["limit"],
            offset=sample_tasks["offset"],
            server="http://localhost:8321",
        )
        captured = capsys.readouterr()
        # Should show header
        assert "ID" in captured.out
        assert "Status" in captured.out
        assert "Assignee" in captured.out
        assert "Title" in captured.out
        # Should show task titles
        assert "CI/CD" in captured.out
        assert "API documentation" in captured.out
        # Should show pagination info
        assert "Showing 3 of 3 tasks" in captured.out

    def test_print_task_table_pagination_hint(self, capsys):
        """When total > limit+offset, shows next-page hint."""
        tasks = [
            {"id": "t_001", "title": "Task 1", "status": "todo",
             "assignee": None, "priority": 0, "created_at": 1,
             "started_at": None, "completed_at": None, "tenant": None},
        ]
        _print_task_table(tasks, total=100, limit=1, offset=0,
                          server="http://localhost:8321")
        captured = capsys.readouterr()
        assert "Showing 1 of 100 tasks" in captured.out
        assert "Use --offset 1 to see next page" in captured.out

    def test_print_task_detail(self, capsys, sample_task_detail):
        """Detail output includes all sections."""
        d = sample_task_detail
        _print_task_detail(
            d["task"], d["parents"], d["children"],
            d["runs"], d["comments"], d["events"],
        )
        captured = capsys.readouterr()
        # Title
        assert "CI/CD" in captured.out
        # Sections
        assert "Parents" in captured.out
        assert "Children" in captured.out
        assert "Runs" in captured.out
        assert "Comments" in captured.out
        assert "Events" in captured.out
        # Body
        assert "GitHub Actions" in captured.out


# ===================================================================
# Command handler tests (with mocked HTTP)
# ===================================================================


class TestCmdTaskList:
    def test_list_success(self, parser, sample_tasks):
        """cmd_task_list prints formatted table on 200 response."""
        ns = parser.parse_args(["task", "list", "--limit", "5"])

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = sample_tasks

        with patch("simple_a2a_registry.cli_task.requests.get",
                   return_value=mock_resp) as mock_get:
            cmd_task_list(ns)

        # Verify correct URL called
        args, kwargs = mock_get.call_args
        assert "/v2/tasks" in args[0]
        assert kwargs["params"]["limit"] == 5

    def test_list_json_output(self, parser, sample_tasks, capsys):
        """--json flag outputs raw JSON."""
        ns = parser.parse_args(["task", "list", "--json"])

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = sample_tasks

        with patch("simple_a2a_registry.cli_task.requests.get",
                   return_value=mock_resp):
            cmd_task_list(ns)

        captured = capsys.readouterr()
        parsed = json.loads(captured.out)
        assert parsed["total"] == 3
        assert len(parsed["tasks"]) == 3

    def test_list_connection_error(self, parser, capsys):
        """Connection error prints error and exits."""
        ns = parser.parse_args(["task", "list"])

        with patch("simple_a2a_registry.cli_task.requests.get",
                   side_effect=__import__("requests").ConnectionError(
                       "Connection refused"
                   )):
            with pytest.raises(SystemExit):
                cmd_task_list(ns)

        captured = capsys.readouterr()
        assert "cannot connect" in captured.err.lower()

    def test_list_timeout(self, parser, capsys):
        """Timeout prints error and exits."""
        ns = parser.parse_args(["task", "list"])

        with patch("simple_a2a_registry.cli_task.requests.get",
                   side_effect=__import__("requests").Timeout("timed out")):
            with pytest.raises(SystemExit):
                cmd_task_list(ns)

        captured = capsys.readouterr()
        assert "timed out" in captured.err.lower()

    def test_list_server_error(self, parser, capsys):
        """Server error response prints error detail."""
        ns = parser.parse_args(["task", "list"])

        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.text = "Internal Server Error"
        mock_resp.json.return_value = {"detail": "Something went wrong"}

        with patch("simple_a2a_registry.cli_task.requests.get",
                   return_value=mock_resp):
            with pytest.raises(SystemExit):
                cmd_task_list(ns)

        captured = capsys.readouterr()
        assert "500" in captured.err
        assert "Something went wrong" in captured.err

    def test_list_custom_server(self, parser):
        """--server is used as the base URL."""
        ns = parser.parse_args([
            "task", "list", "--server", "http://10.0.0.1:9000",
        ])

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"total": 0, "tasks": []}

        with patch("simple_a2a_registry.cli_task.requests.get",
                   return_value=mock_resp) as mock_get:
            cmd_task_list(ns)

        args, _ = mock_get.call_args
        assert args[0].startswith("http://10.0.0.1:9000")


class TestCmdTaskShow:
    def test_show_success(self, parser, sample_task_detail, capsys):
        """cmd_task_show prints formatted detail on 200 response."""
        ns = parser.parse_args(["task", "show", "t_aaa11111"])

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = sample_task_detail

        with patch("simple_a2a_registry.cli_task.requests.get",
                   return_value=mock_resp) as mock_get:
            cmd_task_show(ns)

        # Verify correct endpoint
        args, _ = mock_get.call_args
        assert "/v2/tasks/t_aaa11111" in args[0]

        # Verify formatted output
        captured = capsys.readouterr()
        assert "CI/CD" in captured.out
        assert "Parents" in captured.out
        assert "Children" in captured.out
        assert "Runs" in captured.out

    def test_show_json(self, parser, sample_task_detail, capsys):
        """--json flag outputs raw JSON."""
        ns = parser.parse_args(["task", "show", "t_aaa11111", "--json"])

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = sample_task_detail

        with patch("simple_a2a_registry.cli_task.requests.get",
                   return_value=mock_resp):
            cmd_task_show(ns)

        captured = capsys.readouterr()
        parsed = json.loads(captured.out)
        assert parsed["task"]["id"] == "t_aaa11111"

    def test_show_not_found(self, parser, capsys):
        """404 response prints error."""
        ns = parser.parse_args(["task", "show", "t_nonexistent"])

        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_resp.text = '{"detail": "Task not found"}'
        mock_resp.json.return_value = {"detail": "Task not found"}

        with patch("simple_a2a_registry.cli_task.requests.get",
                   return_value=mock_resp):
            with pytest.raises(SystemExit):
                cmd_task_show(ns)

        captured = capsys.readouterr()
        assert "404" in captured.err
        assert "not found" in captured.err.lower()


# ===================================================================
# Integration: parser + handler dispatch
# ===================================================================


class TestMainDispatch:
    def test_task_list_func_assigned(self, parser):
        """task list subcommand has cmd_task_list as func."""
        ns = parser.parse_args(["task", "list"])
        assert ns.func == cmd_task_list

    def test_task_show_func_assigned(self, parser):
        """task show subcommand has cmd_task_show as func."""
        ns = parser.parse_args(["task", "show", "t_xyz"])
        assert ns.func == cmd_task_show