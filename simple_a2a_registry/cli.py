"""CLI entry point for the Simple A2A Registry.

Usage:
    a2a-registry [--host HOST] [--port PORT] [--data-dir DIR]
                 [--board-path PATH] [--dispatcher-enabled BOOL]
                 [--dispatcher-interval SEC] [--claim-ttl SEC]
                 [--failure-limit N] [--workspaces-root DIR]
    python -m simple_a2a_registry [options...]
"""
from __future__ import annotations

import argparse
import logging
from pathlib import Path
import sys

from simple_a2a_registry.server import create_app, run_server

logger = logging.getLogger("a2a_registry")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Simple A2A Registry — Agent-to-Agent Registry Server",
    )

    # --- Basic server options ---
    parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="Bind address (default: 0.0.0.0)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8321,
        help="Bind port (default: 8321)",
    )
    parser.add_argument(
        "--data-dir",
        default="~/.simple-a2a-registry",
        help="Persistent data directory (default: ~/.simple-a2a-registry)",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level (default: INFO)",
    )
    parser.add_argument(
        "--log-file",
        default=None,
        help="Log file path (default: stderr). Example: ~/.simple-a2a-registry/server.log",
    )
    parser.add_argument(
        "--version",
        action="store_true",
        help="Show version and exit",
    )

    parser.add_argument(
        "--auth-enabled",
        default=False,
        action=argparse.BooleanOptionalAction,
        help="Enable OAuth 2.1 authentication middleware (default: disabled — dev mode)",
    )

    # --- V2 Orchestration Engine options ---
    parser.add_argument(
        "--board-path",
        default=None,
        help="SQLite database path for the V2 orchestration board "
             "(default: <data-dir>/board.db)",
    )
    parser.add_argument(
        "--dispatcher-enabled",
        default=True,
        action=argparse.BooleanOptionalAction,
        help="Enable the background worker dispatcher (default: enabled)",
    )
    parser.add_argument(
        "--dispatcher-interval",
        type=int,
        default=5,
        help="Dispatcher poll interval in seconds (default: 5)",
    )
    parser.add_argument(
        "--claim-ttl",
        type=int,
        default=900,
        help="Claim lock TTL in seconds (default: 900 / 15 min)",
    )
    parser.add_argument(
        "--failure-limit",
        type=int,
        default=3,
        help="Global default retry limit (default: 3)",
    )
    parser.add_argument(
        "--workspaces-root",
        default=None,
        help="Root directory for scratch workspaces "
             "(default: <data-dir>/workspaces)",
    )

    # --- Deprecated V1 alias ---
    parser.add_argument(
        "--dispatcher",
        dest="dispatcher_enabled_v1",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="[deprecated] Use --dispatcher-enabled instead",
    )

    args = parser.parse_args(argv)

    if args.version:
        print("simple-a2a-registry 1.0.0")
        return

    log_file = args.log_file
    log_kwargs = {
        "level": getattr(logging, args.log_level),
        "format": "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        "datefmt": "%H:%M:%S",
    }
    if log_file:
        log_kwargs["filename"] = str(Path(log_file).expanduser())
    logging.basicConfig(**log_kwargs)

    # Deprecated alias handling
    if args.dispatcher_enabled_v1 is not None:
        logger.warning("--dispatcher is deprecated; use --dispatcher-enabled instead")
        args.dispatcher_enabled = args.dispatcher_enabled_v1

    v2_opts = []
    if args.board_path:
        v2_opts.append(f"board={args.board_path}")
    if args.dispatcher_enabled is False:
        v2_opts.append("dispatcher=off")
    if args.dispatcher_interval != 5:
        v2_opts.append(f"poll_interval={args.dispatcher_interval}s")
    if args.claim_ttl != 900:
        v2_opts.append(f"claim_ttl={args.claim_ttl}s")
    if args.failure_limit != 3:
        v2_opts.append(f"fail_limit={args.failure_limit}")
    if args.workspaces_root:
        v2_opts.append(f"workspaces={args.workspaces_root}")

    auth_info = "🔐 auth enabled" if args.auth_enabled else "🔓 auth disabled (dev)"
    v2_info = f" | V2: {', '.join(v2_opts)}" if v2_opts else " | V2: defaults"
    logger.info(
        "Simple A2A Registry starting on %s:%s (data: %s) %s %s",
        args.host, args.port, args.data_dir, auth_info, v2_info,
    )

    run_server(
        host=args.host,
        port=args.port,
        data_dir=args.data_dir,
        auth_enabled=args.auth_enabled,
        board_path=args.board_path,
        dispatcher_enabled=args.dispatcher_enabled,
        dispatcher_interval=args.dispatcher_interval,
        claim_ttl=args.claim_ttl,
        failure_limit=args.failure_limit,
        workspaces_root=args.workspaces_root,
    )


if __name__ == "__main__":
    main()