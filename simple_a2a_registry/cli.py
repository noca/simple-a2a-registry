"""CLI entry point for the Simple A2A Registry.

Usage::

    a2a-registry [--version]
    a2a-registry server [options...]
    a2a-registry task <subcommand> [options...]
    a2a-registry history <subcommand> [options...]
    a2a-registry workflow <subcommand> [options...]
    python -m simple_a2a_registry [options...]        (default: start server)

Subcommands:

    server    Start the registry HTTP server (default when no subcommand given)
    task      View, search, and filter orchestration tasks
    history   Query audit log events (timeline query)
    workflow  Apply, validate, and inspect declarative YAML workflows
"""

from __future__ import annotations

import argparse
import logging
import sys

from simple_a2a_registry import __version__
from simple_a2a_registry.cli_task import build_task_parser

try:
    from simple_a2a_registry.cli_agent import build_agent_parser
except ImportError:
    build_agent_parser = None

try:
    from simple_a2a_registry.cli_history import build_history_parser
except ImportError:
    build_history_parser = None

try:
    from simple_a2a_registry.cli_workflow import build_workflow_parser
except ImportError:
    build_workflow_parser = None

from simple_a2a_registry.config import load_config
from simple_a2a_registry.log import setup_logging
from simple_a2a_registry.server import run_server

logger = logging.getLogger("a2a_registry")


# ---------------------------------------------------------------------------
# Parser builders
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level argument parser with subcommands."""
    parser = argparse.ArgumentParser(
        description="Simple A2A Registry — Agent-to-Agent Registry Server & CLI SDK",
    )

    parser.add_argument(
        "--version",
        action="store_true",
        help="Show version and exit",
    )

    subparsers = parser.add_subparsers(
        dest="command",
        title="subcommands",
        description="Available subcommands (run without a subcommand to start the server)",
    )

    # --- server subcommand ---
    build_server_parser(subparsers)

    # --- task subcommand ---
    build_task_parser(subparsers)

    # --- agent subcommand (optional) ---
    if build_agent_parser:
        build_agent_parser(subparsers)

    # --- history subcommand (optional) ---
    if build_history_parser:
        build_history_parser(subparsers)

    # --- workflow subcommand (optional) ---
    if build_workflow_parser:
        build_workflow_parser(subparsers)

    return parser


def build_server_parser(subparsers: argparse._SubParsersAction) -> None:
    """Add the ``server`` subcommand parser (and also used as default)."""
    sp = subparsers.add_parser(
        "server",
        help="Start the registry HTTP server",
        description="Start the Simple A2A Registry HTTP server with the V2 orchestration engine.",
    )

    # --- Basic server options ---
    sp.add_argument(
        "--host",
        default="0.0.0.0",
        help="Bind address (default: 0.0.0.0)",
    )
    sp.add_argument(
        "--port",
        type=int,
        default=8321,
        help="Bind port (default: 8321)",
    )
    sp.add_argument(
        "--data-dir",
        default="~/.simple-a2a-registry",
        help="Persistent data directory (default: ~/.simple-a2a-registry)",
    )
    sp.add_argument(
        "--log-format",
        default="text",
        choices=["json", "text"],
        help="Log output format: json (production/ELK) or text (development) (default: text)",
    )
    sp.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level (default: INFO)",
    )
    sp.add_argument(
        "--log-file",
        default=None,
        help="Log file path (default: stderr). Example: ~/.simple-a2a-registry/server.log",
    )
    sp.add_argument(
        "--auth-enabled",
        default=False,
        action=argparse.BooleanOptionalAction,
        help="Enable OAuth 2.1 authentication middleware (default: disabled — dev mode)",
    )
    sp.add_argument(
        "--bootstrap-secret",
        default=None,
        help="Bootstrap client secret for the 'simple-a2a-registry' admin account "
             "(default: auto-generated on first start, logged to stdout)",
    )

    # --- V2 Orchestration Engine options ---
    sp.add_argument(
        "--board-path",
        default=None,
        help="SQLite database path for the V2 orchestration board "
             "(default: <data-dir>/board.db)",
    )
    sp.add_argument(
        "--dispatcher-enabled",
        default=True,
        action=argparse.BooleanOptionalAction,
        help="Enable the background worker dispatcher (default: enabled)",
    )
    sp.add_argument(
        "--dispatcher-interval",
        type=int,
        default=5,
        help="Dispatcher poll interval in seconds (default: 5)",
    )
    sp.add_argument(
        "--claim-ttl",
        type=int,
        default=900,
        help="Claim lock TTL in seconds (default: 900 / 15 min)",
    )
    sp.add_argument(
        "--failure-limit",
        type=int,
        default=3,
        help="Global default retry limit (default: 3)",
    )
    sp.add_argument(
        "--workspaces-root",
        default=None,
        help="Root directory for scratch workspaces "
             "(default: <data-dir>/workspaces)",
    )

    # --- Deprecated V1 alias ---
    sp.add_argument(
        "--dispatcher",
        dest="dispatcher_enabled_v1",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="[deprecated] Use --dispatcher-enabled instead",
    )


# ---------------------------------------------------------------------------
# Server start
# ---------------------------------------------------------------------------


def _start_server(args: argparse.Namespace) -> None:
    """Execute the server subcommand with parsed arguments."""
    log_file = getattr(args, "log_file", None)
    setup_logging(
        log_format=getattr(args, "log_format", "text"),
        level=getattr(args, "log_level", "INFO").lower(),
        output="stdout",
        log_file=log_file,
        suppress_noisy=True,
    )

    # Deprecated alias handling
    if getattr(args, "dispatcher_enabled_v1", None) is not None:
        logger.warning("--dispatcher is deprecated; use --dispatcher-enabled instead")
        args.dispatcher_enabled = args.dispatcher_enabled_v1

    v2_opts = []
    if getattr(args, "board_path", None):
        v2_opts.append(f"board={args.board_path}")
    if getattr(args, "dispatcher_enabled", True) is False:
        v2_opts.append("dispatcher=off")
    if getattr(args, "dispatcher_interval", 5) != 5:
        v2_opts.append(f"poll_interval={args.dispatcher_interval}s")
    if getattr(args, "claim_ttl", 900) != 900:
        v2_opts.append(f"claim_ttl={args.claim_ttl}s")
    if getattr(args, "failure_limit", 3) != 3:
        v2_opts.append(f"fail_limit={args.failure_limit}")
    if getattr(args, "workspaces_root", None):
        v2_opts.append(f"workspaces={args.workspaces_root}")

    auth_enabled = getattr(args, "auth_enabled", False)
    auth_info = "🔐 auth enabled" if auth_enabled else "🔓 auth disabled (dev)"
    v2_info = f" | V2: {', '.join(v2_opts)}" if v2_opts else " | V2: defaults"

    host = getattr(args, "host", "0.0.0.0")
    port = getattr(args, "port", 8321)
    data_dir = getattr(args, "data_dir", "~/.simple-a2a-registry")

    logger.info(
        "Simple A2A Registry starting on %s:%s (data: %s) %s %s",
        host, port, data_dir, auth_info, v2_info,
    )

    run_server(
        host=host,
        port=port,
        data_dir=data_dir,
        auth_enabled=auth_enabled,
        bootstrap_secret=getattr(args, "bootstrap_secret", None),
        board_path=getattr(args, "board_path", None),
        dispatcher_enabled=getattr(args, "dispatcher_enabled", True),
        dispatcher_interval=getattr(args, "dispatcher_interval", 5),
        claim_ttl=getattr(args, "claim_ttl", 900),
        failure_limit=getattr(args, "failure_limit", 3),
        workspaces_root=getattr(args, "workspaces_root", None),
        config=load_config(),
    )

# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.version:
        print(__version__)
        return

    # Dispatch to subcommand handler
    if args.command in ("task", "agent", "history", "workflow"):
        if hasattr(args, "func"):
            args.func(args)
        else:
            sub_name = args.command
            choices = {
                "task": "list, show",
                "agent": "list, get, register, unregister, heartbeat, toggle, stats, purge-stale",
                "history": "list, show",
                "workflow": "apply, validate, show",
            }
            parser.error(
                f"{sub_name} subcommand required: {choices.get(sub_name, '...')}"
            )
    elif args.command == "server":
        _start_server(args)
    else:
        # No subcommand given — backward compat: start server
        _start_server(args)


if __name__ == "__main__":
    main()