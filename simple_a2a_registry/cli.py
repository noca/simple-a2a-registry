"""
CLI entry point for the Simple A2A Registry.

Usage:
    a2a-registry [--host HOST] [--port PORT] [--data-dir DIR] [--profiles-dir DIR]
    python -m simple_a2a_registry [--host HOST] [--port PORT] [--data-dir DIR] [--profiles-dir DIR]
"""
from __future__ import annotations

import argparse
import logging
import sys

from simple_a2a_registry.server import create_app, run_server

logger = logging.getLogger("a2a_registry")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Simple A2A Registry — Agent-to-Agent Registry Server",
    )
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
        "--profiles-dir",
        default=None,
        help="Profiles directory for local agent discovery (optional)",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level (default: INFO)",
    )
    parser.add_argument(
        "--version",
        action="store_true",
        help="Show version and exit",
    )

    args = parser.parse_args(argv)

    if args.version:
        print("simple-a2a-registry 1.0.0")
        return

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    app = create_app(
        data_dir=args.data_dir,
        profiles_home=args.profiles_dir,
    )
    logger.info(
        "Simple A2A Registry starting on %s:%s (data: %s)",
        args.host, args.port, args.data_dir,
    )
    if args.profiles_dir:
        logger.info("Profiles directory for discovery: %s", args.profiles_dir)

    run_server(args.host, args.port, args.data_dir)


if __name__ == "__main__":
    main()