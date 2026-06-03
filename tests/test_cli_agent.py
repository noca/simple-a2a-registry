"""Tests for the CLI agent subcommands (cli_agent.py).

Tests parser construction, argument parsing, formatters, and HTTP error handling.
HTTP calls are mocked — no running server required.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from simple_a2a_registry.cli import build_parser
from simple_a2a_registry.cli_agent import (
    _fmt_age,
    _fmt_ts,
    _print_agent_table,
    _status_icon,
    cmd_agent_get,
    cmd_agent_heartbeat,
    cmd_agent_list,
    cmd_agent_purge_stale,
    cmd_agent_register,
    cmd_agent_stats,
    cmd_agent_toggle,
    cmd_agent_unregister,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def parser():
    return build_parser()


@pytest.fixture
def sample_agents():
    return {
        "agents": [
            {
                "id": "a_11111111-aaaa-4aaa-aaaa-111111111111",
                "name": "AliceBot",
                "status": "alive",
                "lastHeartbeat": 1700000100,
                "tenant": "prod",
                "preferred_channel": "ws",
                "version": "1.0.0",
                "disabled": False,
            },
            {
                "id": "a_22222222-bbbb-4bbb-bbbb-222222222222",
                "name": "BobAgent",
                "status": "stale",
                "lastHeartbeat": 1699900000,
                "tenant": None,
                "preferred_channel": "ws",
                "version": "1.0.0",
                "disabled": False,
            },
            {
                "id": "a_33333333-cccc-4ccc-cccc-333333333333",
                "name": "CharlieService",
                "status": "disabled",
                "lastHeartbeat": 0,
                "tenant": "dev",
                "preferred_channel": "http",
                "version": "2.1.0",
                "disabled": True,
            },
        ],
    }


@pytest.fixture
def sample_agent_detail():
    return {
        "id": "a_11111111-aaaa-4aaa-aaaa-111111111111",
        "name": "AliceBot",
        "description": "A helpful agent for prod operations",
        "status": "alive",
        "version": "1.0.0",
        "tenant": "prod",
        "preferred_channel": "ws",
        "disabled": False,
        "lastHeartbeat": 1700000100,
        "provider": {
            "organization": "Acme Corp",
            "url": "https://a2a.acme.com",
        },
        "supported_interfaces": [
            {
                "url": "wss://agent-hub.acme.com/alice",
                "protocol_binding": "JSONRPC",
                "protocol_version": "1.0",
            },
        ],
        "skills": [
            {"id": "sk_build", "name": "build"},
            {"id": "sk_deploy", "name": "deploy"},
        ],
    }


# ===================================================================
# Parser tests
# ===================================================================


class TestAgentParser:
    def test_parser_builds_successfully(self, parser):
        """Parser fixture creates without error."""
        assert parser is not None

    def test_agent_list_no_args(self, parser):
        """agent list parses with minimal args."""
        ns = parser.parse_args(["agent", "list"])
        assert ns.command == "agent"
        assert ns.agent_command == "list"
        assert ns.skill == ""
        assert ns.tag == ""
        assert ns.query == ""
        assert ns.tenant is None
        assert ns.json is False
        assert ns.server == "http://localhost:8321"

    def test_agent_list_all_filters(self, parser):
        """agent list parses all filter arguments."""
        ns = parser.parse_args([
            "agent", "list",
            "--skill", "build",
            "--tag", "production",
            "--query", "bot",
            "--tenant", "prod",
            "--json",
            "--server", "http://10.0.0.1:8321",
        ])
        assert ns.skill == "build"
        assert ns.tag == "production"
        assert ns.query == "bot"
        assert ns.tenant == "prod"
        assert ns.json is True
        assert ns.server == "http://10.0.0.1:8321"

    def test_agent_get_parses(self, parser):
        """agent get parses agent_id."""
        ns = parser.parse_args(["agent", "get", "a_abc12345"])
        assert ns.command == "agent"
        assert ns.agent_command == "get"
        assert ns.agent_id == "a_abc12345"
        assert ns.server == "http://localhost:8321"

    def test_agent_get_show_alias(self, parser):
        """agent show is aliased to get (argparse sets agent_command to alias name)."""
        ns = parser.parse_args(["agent", "show", "a_xyz"])
        assert ns.agent_command in ("get", "show"), f"expected 'get' or 'show', got {ns.agent_command!r}"
        assert ns.agent_id == "a_xyz"

    def test_agent_get_json(self, parser):
        """agent get supports --json flag."""
        ns = parser.parse_args(["agent", "get", "a_xyz", "--json"])
        assert ns.json is True

    def test_agent_get_custom_server(self, parser):
        """agent get supports --server after subcommand."""
        ns = parser.parse_args(["agent", "get", "a_xyz", "--server", "http://10.0.0.1:9000"])
        assert ns.server == "http://10.0.0.1:9000"

    def test_agent_register_parses(self, parser):
        """agent register parses name and common options."""
        ns = parser.parse_args([
            "agent", "register", "my-bot",
            "--description", "A test bot",
            "--url", "http://agent.example.com/rpc",
            "--tenant", "staging",
            "--json",
        ])
        assert ns.name == "my-bot"
        assert ns.description == "A test bot"
        assert ns.url == "http://agent.example.com/rpc"
        assert ns.tenant == "staging"
        assert ns.json is True

    def test_agent_register_card_file(self, parser):
        """agent register supports --card-file."""
        ns = parser.parse_args([
            "agent", "register", "card-bot", "-c", "/tmp/card.json",
        ])
        assert ns.name == "card-bot"
        assert ns.card_file == "/tmp/card.json"

    def test_agent_register_short_args(self, parser):
        """agent register supports -d, -u, -c short flags."""
        ns = parser.parse_args([
            "agent", "register", "shorty", "-d", "desc", "-u", "http://u", "-c", "/tmp/c.json",
        ])
        assert ns.description == "desc"
        assert ns.url == "http://u"
        assert ns.card_file == "/tmp/c.json"

    def test_agent_unregister_parses(self, parser):
        """agent unregister parses agent_id."""
        ns = parser.parse_args(["agent", "unregister", "a_xyz"])
        assert ns.agent_id == "a_xyz"

    def test_agent_heartbeat_parses(self, parser):
        """agent heartbeat parses agent_id."""
        ns = parser.parse_args(["agent", "heartbeat", "a_xyz"])
        assert ns.agent_id == "a_xyz"

    def test_agent_toggle_parses(self, parser):
        """agent toggle parses agent_id."""
        ns = parser.parse_args(["agent", "toggle", "a_xyz"])
        assert ns.agent_id == "a_xyz"

    def test_agent_stats_parses(self, parser):
        """agent stats parses without args."""
        ns = parser.parse_args(["agent", "stats"])
        assert ns.tenant is None
        assert ns.server == "http://localhost:8321"

    def test_agent_stats_tenant(self, parser):
        """agent stats supports --tenant filter."""
        ns = parser.parse_args(["agent", "stats", "--tenant", "prod"])
        assert ns.tenant == "prod"

    def test_agent_purge_stale_parses(self, parser):
        """agent purge-stale parses without args."""
        ns = parser.parse_args(["agent", "purge-stale"])
        assert ns.command == "agent"
        assert ns.agent_command == "purge-stale"


# ===================================================================
# Formatter tests
# ===================================================================


class TestFormatters:
    def test_status_icon(self):
        assert _status_icon("alive") == "\u25cf"
        assert _status_icon("stale") == "\u25cc"
        assert _status_icon("disabled") == "\u2298"
        assert _status_icon("unknown") == "?"

    def test_fmt_ts_none(self):
        assert _fmt_ts(None) == "-"

    def test_fmt_ts_zero(self):
        assert _fmt_ts(0) == "-"

    def test_fmt_ts_valid(self):
        result = _fmt_ts(1700000000)
        assert "2023-11-14" in result
        assert "UTC" in result

    def test_fmt_age_now(self):
        """Age of a recent timestamp shows seconds."""
        import time
        now = time.time()
        result = _fmt_age(now)
        assert "s ago" in result or "m ago" in result

    def test_fmt_age_old(self):
        """Age of an old timestamp shows days."""
        result = _fmt_age(1000000)
        assert "d ago" in result

    def test_print_agent_table_empty(self, capsys):
        """Empty agent list prints 'No agents found'."""
        _print_agent_table([])
        captured = capsys.readouterr()
        assert "No agents found" in captured.out

    def test_print_agent_table_with_data(self, capsys, sample_agents):
        """Table output includes header row and agent data."""
        _print_agent_table(sample_agents["agents"])
        captured = capsys.readouterr()
        # Should show header
        assert "ID" in captured.out
        assert "Name" in captured.out
        assert "Status" in captured.out
        assert "Tenant" in captured.out
        # Should show agent names
        assert "AliceBot" in captured.out
        assert "BobAgent" in captured.out
        assert "CharlieService" in captured.out
        # Should show tenant values
        assert "prod" in captured.out
        assert "dev" in captured.out
        # Should show channel values
        assert "ws" in captured.out
        assert "http" in captured.out


# ===================================================================
# Command handler tests (with mocked HTTP)
# ===================================================================


class TestCmdAgentList:
    def test_list_success(self, parser, sample_agents, capsys):
        """cmd_agent_list prints formatted table on 200 response."""
        ns = parser.parse_args(["agent", "list", "--tenant", "prod"])

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = sample_agents

        with patch("simple_a2a_registry.cli_agent.requests.get",
                   return_value=mock_resp) as mock_get:
            cmd_agent_list(ns)

        # Verify correct URL and params
        args, kwargs = mock_get.call_args
        assert "/v1/agents" in args[0]
        assert kwargs["params"]["tenant"] == "prod"

        # Verify formatted output
        captured = capsys.readouterr()
        assert "AliceBot" in captured.out
        assert "Total: 3 agent(s)" in captured.out

    def test_list_json_output(self, parser, sample_agents, capsys):
        """--json flag outputs raw JSON."""
        ns = parser.parse_args(["agent", "list", "--json"])

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = sample_agents

        with patch("simple_a2a_registry.cli_agent.requests.get",
                   return_value=mock_resp):
            cmd_agent_list(ns)

        captured = capsys.readouterr()
        parsed = json.loads(captured.out)
        assert len(parsed["agents"]) == 3

    def test_list_connection_error(self, parser, capsys):
        """Connection error prints error and exits."""
        ns = parser.parse_args(["agent", "list"])

        with patch("simple_a2a_registry.cli_agent.requests.get",
                   side_effect=__import__("requests").ConnectionError(
                       "Connection refused"
                   )):
            with pytest.raises(SystemExit):
                cmd_agent_list(ns)

        captured = capsys.readouterr()
        assert "cannot connect" in captured.err.lower()

    def test_list_timeout(self, parser, capsys):
        """Timeout prints error and exits."""
        ns = parser.parse_args(["agent", "list"])

        with patch("simple_a2a_registry.cli_agent.requests.get",
                   side_effect=__import__("requests").Timeout("timed out")):
            with pytest.raises(SystemExit):
                cmd_agent_list(ns)

        captured = capsys.readouterr()
        assert "timed out" in captured.err.lower()

    def test_list_server_error(self, parser, capsys):
        """Server error response prints error detail."""
        ns = parser.parse_args(["agent", "list"])

        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.text = "Internal Server Error"
        mock_resp.json.return_value = {"detail": "Database unavailable"}

        with patch("simple_a2a_registry.cli_agent.requests.get",
                   return_value=mock_resp):
            with pytest.raises(SystemExit):
                cmd_agent_list(ns)

        captured = capsys.readouterr()
        assert "500" in captured.err
        assert "Database unavailable" in captured.err

    def test_list_custom_server(self, parser):
        """--server is used as the base URL."""
        ns = parser.parse_args([
            "agent", "list", "--server", "http://custom:9000",
        ])

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"agents": []}

        with patch("simple_a2a_registry.cli_agent.requests.get",
                   return_value=mock_resp) as mock_get:
            cmd_agent_list(ns)

        args, _ = mock_get.call_args
        assert args[0].startswith("http://custom:9000")


class TestCmdAgentGet:
    def test_get_success(self, parser, sample_agent_detail, capsys):
        """cmd_agent_get prints formatted detail on 200 response."""
        ns = parser.parse_args(["agent", "get", "a_11111111-aaaa-4aaa-aaaa-111111111111"])

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = sample_agent_detail

        with patch("simple_a2a_registry.cli_agent.requests.get",
                   return_value=mock_resp) as mock_get:
            cmd_agent_get(ns)

        # Verify correct endpoint
        args, _ = mock_get.call_args
        assert "/v1/agents/" in args[0]

        # Verify formatted output
        captured = capsys.readouterr()
        assert "AliceBot" in captured.out
        assert "prod" in captured.out
        assert "Acme Corp" in captured.out
        assert "build" in captured.out
        assert "deploy" in captured.out

    def test_get_json(self, parser, sample_agent_detail, capsys):
        """--json flag outputs raw JSON."""
        ns = parser.parse_args(["agent", "get", "a_1111", "--json"])

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = sample_agent_detail

        with patch("simple_a2a_registry.cli_agent.requests.get",
                   return_value=mock_resp):
            cmd_agent_get(ns)

        captured = capsys.readouterr()
        parsed = json.loads(captured.out)
        assert parsed["name"] == "AliceBot"
        assert parsed["provider"]["organization"] == "Acme Corp"

    def test_get_not_found(self, parser, capsys):
        """404 response prints error."""
        ns = parser.parse_args(["agent", "get", "a_nonexistent"])

        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_resp.text = '{"detail": "Agent not found"}'
        mock_resp.json.return_value = {"detail": "Agent not found"}

        with patch("simple_a2a_registry.cli_agent.requests.get",
                   return_value=mock_resp):
            with pytest.raises(SystemExit):
                cmd_agent_get(ns)

        captured = capsys.readouterr()
        assert "404" in captured.err
        assert "not found" in captured.err.lower()


class TestCmdAgentRegister:
    def test_register_success(self, parser, capsys):
        """cmd_agent_register registers successfully."""
        ns = parser.parse_args([
            "agent", "register", "new-bot",
            "--description", "A shiny new bot",
        ])

        mock_resp = MagicMock()
        mock_resp.status_code = 201
        mock_resp.json.return_value = {"id": "a_new1234", "name": "new-bot"}
        mock_resp.text = '{}'

        with patch("simple_a2a_registry.cli_agent.requests.post",
                   return_value=mock_resp) as mock_post:
            cmd_agent_register(ns)

        args, kwargs = mock_post.call_args
        assert "/v1/agents" in args[0]
        assert kwargs["json"]["name"] == "new-bot"
        assert kwargs["json"]["description"] == "A shiny new bot"

        captured = capsys.readouterr()
        assert "registered" in captured.out.lower()
        assert "a_new1234" in captured.out

    def test_register_connection_error(self, parser, capsys):
        """Connection error on register prints error."""
        ns = parser.parse_args(["agent", "register", "broken"])

        with patch("simple_a2a_registry.cli_agent.requests.post",
                   side_effect=__import__("requests").ConnectionError("refused")):
            with pytest.raises(SystemExit):
                cmd_agent_register(ns)

        captured = capsys.readouterr()
        assert "cannot connect" in captured.err.lower()


class TestCmdAgentUnregister:
    def test_unregister_success(self, parser, capsys):
        """cmd_agent_unregister removes agent."""
        ns = parser.parse_args(["agent", "unregister", "a_xyz"])

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {}
        mock_resp.text = ""

        with patch("simple_a2a_registry.cli_agent.requests.delete",
                   return_value=mock_resp) as mock_del:
            cmd_agent_unregister(ns)

        args, _ = mock_del.call_args
        assert "/v1/agents/a_xyz" in args[0]

        captured = capsys.readouterr()
        assert "removed" in captured.out.lower()
        assert "a_xyz" in captured.out

    def test_unregister_json(self, parser, capsys):
        """--json flag on unregister outputs JSON."""
        ns = parser.parse_args(["agent", "unregister", "a_xyz", "--json"])

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"status": "removed"}
        mock_resp.text = '{"status": "removed"}'

        with patch("simple_a2a_registry.cli_agent.requests.delete",
                   return_value=mock_resp):
            cmd_agent_unregister(ns)

        captured = capsys.readouterr()
        # Output has text prefix + JSON; check for both
        assert "removed" in captured.out.lower()
        assert '"status"' in captured.out or "'status'" in captured.out


class TestCmdAgentHeartbeat:
    def test_heartbeat_success(self, parser, capsys):
        """cmd_agent_heartbeat sends heartbeat successfully."""
        ns = parser.parse_args(["agent", "heartbeat", "a_xyz"])

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"status": "alive", "expires_at": 1700003600}

        with patch("simple_a2a_registry.cli_agent.requests.post",
                   return_value=mock_resp) as mock_post:
            cmd_agent_heartbeat(ns)

        args, _ = mock_post.call_args
        assert "/v1/agents/a_xyz/heartbeat" in args[0]

        captured = capsys.readouterr()
        assert "Heartbeat sent" in captured.out
        assert "alive" in captured.out


class TestCmdAgentToggle:
    def test_toggle_disable(self, parser, capsys):
        """cmd_agent_toggle toggles agent."""
        ns = parser.parse_args(["agent", "toggle", "a_xyz"])

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"disabled": True}

        with patch("simple_a2a_registry.cli_agent.requests.post",
                   return_value=mock_resp) as mock_post:
            cmd_agent_toggle(ns)

        args, _ = mock_post.call_args
        assert "/v1/agents/a_xyz/toggle" in args[0]

        captured = capsys.readouterr()
        assert "disabled" in captured.out.lower()

    def test_toggle_enable(self, parser, capsys):
        """Toggle response with disabled=False shows enabled."""
        ns = parser.parse_args(["agent", "toggle", "a_xyz"])

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"disabled": False}

        with patch("simple_a2a_registry.cli_agent.requests.post",
                   return_value=mock_resp):
            cmd_agent_toggle(ns)

        captured = capsys.readouterr()
        assert "enabled" in captured.out.lower()


class TestCmdAgentStats:
    def test_stats_success(self, parser, sample_agents, capsys):
        """cmd_agent_stats prints statistics."""
        ns = parser.parse_args(["agent", "stats"])

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = sample_agents

        with patch("simple_a2a_registry.cli_agent.requests.get",
                   return_value=mock_resp) as mock_get:
            cmd_agent_stats(ns)

        args, kwargs = mock_get.call_args
        assert "/v1/agents" in args[0]
        assert kwargs["params"] == {}

        captured = capsys.readouterr()
        assert "Statistics" in captured.out
        assert "Total" in captured.out
        assert "3" in captured.out
        assert "Alive" in captured.out
        assert "Stale" in captured.out
        assert "Disabled" in captured.out

    def test_stats_with_tenant(self, parser, sample_agents, capsys):
        """Stats with --tenant shows breakdown."""
        ns = parser.parse_args(["agent", "stats", "--tenant", "prod"])

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = sample_agents

        with patch("simple_a2a_registry.cli_agent.requests.get",
                   return_value=mock_resp):
            cmd_agent_stats(ns)

        captured = capsys.readouterr()
        assert "tenant: prod" in captured.out.lower() or "prod" in captured.out


class TestCmdAgentPurgeStale:
    def test_purge_stale_none(self, parser, capsys):
        """No stale agents prints appropriate message."""
        ns = parser.parse_args(["agent", "purge-stale"])

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"agents": [
            {"id": "a_alive", "name": "alive", "status": "alive"},
        ]}

        with patch("simple_a2a_registry.cli_agent.requests.get",
                   return_value=mock_resp):
            cmd_agent_purge_stale(ns)

        captured = capsys.readouterr()
        assert "No stale agents" in captured.out

    def test_purge_stale_removes(self, parser, capsys):
        """Stale agents are removed."""
        ns = parser.parse_args(["agent", "purge-stale"])

        mock_list_resp = MagicMock()
        mock_list_resp.status_code = 200
        mock_list_resp.json.return_value = {"agents": [
            {"id": "a_stale1", "name": "stale1", "status": "stale"},
            {"id": "a_stale2", "name": "stale2", "status": "stale"},
            {"id": "a_alive", "name": "alive", "status": "alive"},
        ]}

        mock_del_resp = MagicMock()
        mock_del_resp.status_code = 200
        mock_del_resp.json.return_value = {}
        mock_del_resp.text = ""

        with patch("simple_a2a_registry.cli_agent.requests.get",
                   return_value=mock_list_resp):
            with patch("simple_a2a_registry.cli_agent.requests.delete",
                       return_value=mock_del_resp) as mock_del:
                cmd_agent_purge_stale(ns)

        # Should have called delete twice
        assert mock_del.call_count == 2

        captured = capsys.readouterr()
        assert "Purged 2" in captured.out


# ===================================================================
# Integration: parser + handler dispatch
# ===================================================================


class TestMainDispatch:
    def test_agent_list_func_assigned(self, parser):
        """agent list subcommand has cmd_agent_list as func."""
        ns = parser.parse_args(["agent", "list"])
        assert ns.func == cmd_agent_list

    def test_agent_get_func_assigned(self, parser):
        """agent get subcommand has cmd_agent_get as func."""
        ns = parser.parse_args(["agent", "get", "a_xyz"])
        assert ns.func == cmd_agent_get

    def test_agent_register_func_assigned(self, parser):
        """agent register subcommand has cmd_agent_register as func."""
        ns = parser.parse_args(["agent", "register", "test-bot"])
        assert ns.func == cmd_agent_register

    def test_agent_unregister_func_assigned(self, parser):
        """agent unregister subcommand has cmd_agent_unregister as func."""
        ns = parser.parse_args(["agent", "unregister", "a_xyz"])
        assert ns.func == cmd_agent_unregister

    def test_agent_heartbeat_func_assigned(self, parser):
        """agent heartbeat subcommand has cmd_agent_heartbeat as func."""
        ns = parser.parse_args(["agent", "heartbeat", "a_xyz"])
        assert ns.func == cmd_agent_heartbeat

    def test_agent_toggle_func_assigned(self, parser):
        """agent toggle subcommand has cmd_agent_toggle as func."""
        ns = parser.parse_args(["agent", "toggle", "a_xyz"])
        assert ns.func == cmd_agent_toggle

    def test_agent_stats_func_assigned(self, parser):
        """agent stats subcommand has cmd_agent_stats as func."""
        ns = parser.parse_args(["agent", "stats"])
        assert ns.func == cmd_agent_stats

    def test_agent_purge_stale_func_assigned(self, parser):
        """agent purge-stale subcommand has cmd_agent_purge_stale as func."""
        ns = parser.parse_args(["agent", "purge-stale"])
        assert ns.func == cmd_agent_purge_stale