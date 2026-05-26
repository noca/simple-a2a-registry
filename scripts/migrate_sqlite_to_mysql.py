#!/usr/bin/env python3
"""SQLite → MySQL data migration script.

Transfers all data from existing SQLite databases (registry + board)
to a MySQL database configured via the application config.

Usage
-----
  # Using environment variables for MySQL DSN
  A2A_REGISTRY_DATABASE__DRIVER=mysql \\
    A2A_REGISTRY_DATABASE__MYSQL_DSN="mysql+pymysql://user:pass@host:3306/a2a_registry" \\
    python scripts/migrate_sqlite_to_mysql.py

  # Or using a config.yaml with database.mysql_dsn set
  python scripts/migrate_sqlite_to_mysql.py --config ~/.simple-a2a-registry/config.yaml

  # Override SQLite source paths manually
  python scripts/migrate_sqlite_to_mysql.py \\
    --sqlite-registry path/to/registry.db \\
    --sqlite-board path/to/board.db \\
    --mysql-dsn "mysql+pymysql://user:pass@localhost:3306/a2a_registry"
"""

import argparse
import logging
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

# Ensure the project is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from simple_a2a_registry.config import load_config

logger = logging.getLogger("migrate")

# Tables in registry.db (Store)
REGISTRY_TABLES = [
    "agents",
    "oauth_clients",
    "oauth_tokens",
    "auth_codes",
]

# Tables in board.db (TaskStore)
BOARD_TABLES = [
    "tasks",
    "task_links",
    "task_runs",
    "task_comments",
    "task_events",
]


def run_alembic_migration(mysql_url: str) -> None:
    """Run Alembic migrations on the MySQL target to create/update schema."""
    import subprocess
    env = {
        **{k: v for k, v in __import__("os").environ.items()},
        "ALEMBIC_CONFIG": str(Path(__file__).resolve().parent.parent / "alembic.ini"),
    }
    # Override the sqlalchemy.url via env (alembic reads from config)
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=str(Path(__file__).resolve().parent.parent),
        capture_output=True,
        text=True,
        env={**env, "sqlalchemy.url": mysql_url},
    )
    if result.returncode != 0:
        logger.error("Alembic migration failed:\n%s\n%s", result.stdout, result.stderr)
        raise RuntimeError("Alembic migration failed")
    logger.info("Alembic migration completed successfully")
    for line in result.stdout.splitlines():
        logger.info("  %s", line)


def _copy_table(
    src_engine: Engine,
    dst_engine: Engine,
    table: str,
    *,
    chunk_size: int = 500,
    truncate_first: bool = False,
) -> int:
    """Copy all rows from *src_engine*.*table* to *dst_engine*.*table*.

    Returns:
        Number of rows copied.
    """
    if truncate_first:
        with dst_engine.begin() as conn:
            conn.execute(text(f"TRUNCATE TABLE {table}"))

    # Fetch column list
    with src_engine.connect() as conn:
        row = conn.execute(text(f"SELECT * FROM {table} LIMIT 0"))
        columns = row.keys()

    # Read in chunks
    total = 0
    offset = 0
    col_list = ", ".join(columns)
    placeholders = ", ".join([f":{c}" for c in columns])

    with dst_engine.begin() as dst_conn:
        while True:
            with src_engine.connect() as src_conn:
                rows = src_conn.execute(
                    text(f"SELECT * FROM {table} LIMIT {chunk_size} OFFSET {offset}")
                ).fetchall()

            if not rows:
                break

            batch = [dict(row._mapping) for row in rows]
            dst_conn.execute(
                text(f"INSERT IGNORE INTO {table} ({col_list}) VALUES ({placeholders})"),
                batch,
            )
            total += len(batch)
            offset += chunk_size
            logger.info("  %s: copied %d rows (offset %d)", table, total, offset)

    logger.info("  %s: done — %d rows total", table, total)
    return total


def migrate(
    sqlite_registry: str,
    sqlite_board: str,
    mysql_dsn: str,
    *,
    skip_schema: bool = False,
) -> Dict[str, int]:
    """Run the full migration from SQLite to MySQL.

    Args:
        sqlite_registry: Path to registry.db.
        sqlite_board: Path to board.db.
        mysql_dsn: MySQL connection string (``mysql+pymysql://...``).
        skip_schema: If True, skip Alembic schema provisioning.

    Returns:
        Dict mapping table name → row count copied.
    """
    # Validate SQLite files exist
    reg_path = Path(sqlite_registry).expanduser()
    board_path = Path(sqlite_board).expanduser()
    if not reg_path.exists():
        raise FileNotFoundError(f"SQLite registry DB not found: {reg_path}")
    if not board_path.exists():
        raise FileNotFoundError(f"SQLite board DB not found: {board_path}")

    # Connect to SQLite sources
    src_registry = create_engine(f"sqlite:///{reg_path}", connect_args={"check_same_thread": False})
    src_board = create_engine(f"sqlite:///{board_path}", connect_args={"check_same_thread": False})

    # Connect to MySQL target
    dst = create_engine(
        mysql_dsn,
        pool_pre_ping=True,
        pool_recycle=3600,
    )

    # Verify MySQL connectivity
    with dst.connect() as conn:
        conn.execute(text("SELECT 1"))
    logger.info("MySQL connection OK")

    # Run Alembic migrations to ensure schema exists
    if not skip_schema:
        run_alembic_migration(mysql_dsn)

    # Copy registry tables
    logger.info("=== Copying registry tables ===")
    totals: Dict[str, int] = {}
    for table in REGISTRY_TABLES:
        totals[table] = _copy_table(src_registry, dst, table, truncate_first=True)

    # Copy board tables (order matters: tasks first, then dependent tables)
    logger.info("=== Copying board tables ===")
    for table in BOARD_TABLES:
        totals[table] = _copy_table(
            src_board, dst, table,
            truncate_first=(table == "tasks"),
        )

    return totals


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Migrate from SQLite to MySQL",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--config", "-c", default=None,
        help="Path to config.yaml (uses database section for SQLite path and MySQL DSN)",
    )
    parser.add_argument(
        "--sqlite-registry", default=None,
        help="Path to SQLite registry.db (default: from config or ~/.simple-a2a-registry/registry.db)",
    )
    parser.add_argument(
        "--sqlite-board", default=None,
        help="Path to SQLite board.db (default: from config or ~/.simple-a2a-registry/board.db)",
    )
    parser.add_argument(
        "--mysql-dsn", default=None,
        help="MySQL DSN (e.g. mysql+pymysql://user:pass@host:3306/db)",
    )
    parser.add_argument(
        "--skip-schema", action="store_true",
        help="Skip Alembic schema provisioning (useful if schema already exists)",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Verbose output",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Resolve config
    cfg = load_config(args.config) if args.config else load_config()

    sqlite_registry = args.sqlite_registry or cfg.database.sqlite_path
    sqlite_board = args.sqlite_board or cfg.orchestration.board_path
    mysql_dsn = args.mysql_dsn or cfg.database.mysql_dsn

    if not mysql_dsn:
        print(
            "ERROR: No MySQL DSN configured.\n"
            "Set it via:\n"
            "  --mysql-dsn argument\n"
            "  A2A_REGISTRY_DATABASE__MYSQL_DSN env variable\n"
            "  database.mysql_dsn in config.yaml",
            file=sys.stderr,
        )
        sys.exit(1)

    logger.info("Source registry DB: %s", sqlite_registry)
    logger.info("Source board DB:    %s", sqlite_board)
    logger.info("Target MySQL:       %s", mysql_dsn)

    start = time.time()
    try:
        totals = migrate(
            sqlite_registry,
            sqlite_board,
            mysql_dsn,
            skip_schema=args.skip_schema,
        )
        elapsed = time.time() - start
        total_rows = sum(totals.values())
        logger.info("Migration complete in %.1f seconds — %d total rows transferred", elapsed, total_rows)
        for table, count in totals.items():
            logger.info("  %s: %d rows", table, count)
    except Exception as e:
        logger.exception("Migration failed: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()