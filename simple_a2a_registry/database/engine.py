"""Database engine abstraction layer for Simple A2A Registry.

Provides a sqlite3-like interface (``DatabaseEngine``) so existing
``Store`` and ``TaskStore`` code works unchanged while the underlying
database can be SQLite (dev) or MySQL (prod).

Drivers
-------
* ``sqlite`` — SQLAlchemy + pysqlite (sync, backward-compatible).
* ``mysql``  — SQLAlchemy + pymysql  (sync, production).

Connection pool
---------------
SQLiteEngine uses NullPool (one connection — equivalent to current behavior).
MySQLEngine  uses QueuePool (configurable pool_size / max_overflow).
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Dict, List, Optional

from sqlalchemy import (
    create_engine as _sa_create_engine,
    Connection,
    CursorResult as _SACursorResult,
    Engine as _SAEngine,
)
from sqlalchemy.pool import NullPool, QueuePool

from simple_a2a_registry.config import DatabaseConfig

logger = logging.getLogger("a2a_registry.database")

# ---------------------------------------------------------------------------
# CursorResult — adapter so callers use ``.fetchone()`` / ``.fetchall()``
#               on SQLAlchemy results as they did on ``sqlite3.Cursor``.
# ---------------------------------------------------------------------------


class CursorResult:
    """Wraps a SQLAlchemy ``CursorResult`` as a sqlite3-cursor-like object.

    Examples
    --------
    .. code-block:: python

        cur = engine.execute(\"SELECT * FROM agents WHERE id=?\", (aid,))
        row = cur.fetchone()            # → dict-like row or None
        rows = cur.fetchall()           # → list of dict-like rows
        count = cur.rowcount            # → number of affected rows
        rid = cur.lastrowid             # → last inserted row id
    """

    def __init__(self, result: Optional[_SACursorResult]) -> None:
        self._result = result

    # -- fetch helpers ------------------------------------------------------

    def fetchone(self) -> Optional[Dict[str, Any]]:
        if self._result is None:
            return None
        row = self._result.fetchone()
        if row is None:
            return None
        return dict(row._mapping) if hasattr(row, "_mapping") else dict(row)

    def fetchall(self) -> List[Dict[str, Any]]:
        if self._result is None:
            return []
        return [
            dict(row._mapping) if hasattr(row, "_mapping") else dict(row)
            for row in self._result.fetchall()
        ]

    # -- properties ---------------------------------------------------------

    @property
    def rowcount(self) -> int:
        if self._result is None:
            return 0
        return self._result.rowcount  # type: ignore[return-value]

    @property
    def lastrowid(self) -> int:
        if self._result is None:
            return 0
        try:
            return self._result.lastrowid or 0
        except Exception:
            return 0


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------


class DatabaseEngine(ABC):
    """Abstract database engine — provides a sqlite3-compatible interface.

    Lifecycle
    ---------
    1. Create via ``create_engine(config)``.
    2. Call ``connect()`` to open the connection.
    3. Use ``execute()``, ``executescript()`` for queries.
    4. Use ``begin()`` / ``commit()`` / ``rollback()`` for transactions.
    5. Call ``close()`` on shutdown.
    """

    @abstractmethod
    def connect(self) -> None:
        """Open / initialise the database connection."""

    @abstractmethod
    def close(self) -> None:
        """Close the database connection."""

    @abstractmethod
    def execute(self, sql: str, params: tuple = ()) -> CursorResult:
        """Execute a single SQL statement.

        Args:
            sql:    SQL string (supports ``?`` placeholders).
            params: Tuple of positional parameters.

        Returns:
            A :class:`CursorResult` with ``fetchone`` / ``fetchall`` support.
        """

    @abstractmethod
    def executescript(self, sql: str) -> None:
        """Execute multiple SQL statements (DDL batch)."""

    @abstractmethod
    def begin(self, mode: str = "") -> None:
        """Begin a transaction.

        Args:
            mode:  ``IMMEDIATE`` / ``EXCLUSIVE`` (SQLite only; ignored for MySQL).
        """

    @abstractmethod
    def commit(self) -> None:
        """Commit the active transaction."""

    @abstractmethod
    def rollback(self) -> None:
        """Roll back the active transaction."""

    @property
    @abstractmethod
    def driver(self) -> str:
        """Return the driver name (``\"sqlite\"`` or ``\"mysql\"``)."""

    @abstractmethod
    def raw_connection(self) -> Any:
        """Return the underlying DB-API connection for special operations."""


# ---------------------------------------------------------------------------
# SQLiteEngine
# ---------------------------------------------------------------------------


class SQLiteEngine(DatabaseEngine):
    """SQLite backend — wraps SQLAlchemy with ``sqlite+pysqlite``.

    Maintains backward compatibility with the existing ``Store`` behaviour:
    single connection, WAL mode, and ``BEGIN IMMEDIATE`` transactions.

    Connection pool: ``NullPool`` (single connection — matches current code).
    """

    def __init__(self, db_path: str) -> None:
        if db_path == ":memory:":
            self._db_path = ":memory:"
        else:
            self._db_path = str(Path(db_path).expanduser().resolve())
        self._conn: Optional[Connection] = None
        self._engine: Optional[_SAEngine] = None

    # -- interface ----------------------------------------------------------

    def connect(self) -> None:
        engine = _sa_create_engine(
            f"sqlite:///{self._db_path}",
            poolclass=NullPool,
            connect_args={"check_same_thread": False},
        )
        self._engine = engine
        conn = engine.connect()
        self._conn = conn

        # Enable WAL mode & foreign keys (like current Store does)
        conn.exec_driver_sql("PRAGMA journal_mode=WAL")
        conn.exec_driver_sql("PRAGMA busy_timeout=5000")
        conn.exec_driver_sql("PRAGMA foreign_keys=ON")
        conn.commit()

        logger.info("SQLiteEngine connected to %s", self._db_path)

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None
        if self._engine:
            self._engine.dispose()
            self._engine = None

    def execute(self, sql: str, params: tuple = ()) -> CursorResult:
        if self._conn is None:
            raise RuntimeError("Engine not connected — call connect() first")
        result = self._conn.exec_driver_sql(sql, params)
        return CursorResult(result)

    def executescript(self, sql: str) -> None:
        if self._conn is None:
            raise RuntimeError("Engine not connected — call connect() first")
        for statement in sql.split(";"):
            stripped = statement.strip()
            if stripped:
                self._conn.exec_driver_sql(stripped)

    def begin(self, mode: str = "") -> None:
        if self._conn is None:
            raise RuntimeError("Engine not connected — call connect() first")
        if mode:
            self._conn.exec_driver_sql(f"BEGIN {mode}")
        else:
            self._conn.exec_driver_sql("BEGIN")

    def commit(self) -> None:
        if self._conn is None:
            raise RuntimeError("Engine not connected — call connect() first")
        self._conn.commit()

    def rollback(self) -> None:
        if self._conn is None:
            raise RuntimeError("Engine not connected — call connect() first")
        self._conn.rollback()

    @property
    def driver(self) -> str:
        return "sqlite"

    def raw_connection(self) -> Any:
        if self._conn is None:
            raise RuntimeError("Engine not connected")
        return self._conn.connection


# ---------------------------------------------------------------------------
# MySQLEngine
# ---------------------------------------------------------------------------


class MySQLEngine(DatabaseEngine):
    """MySQL backend — wraps SQLAlchemy with ``mysql+pymysql``.

    Connection pool: ``QueuePool`` with configurable ``pool_size`` /
    ``max_overflow``.  ``pool_pre_ping`` is enabled to detect stale
    connections before use.
    """

    def __init__(self, config: DatabaseConfig) -> None:
        self._config = config
        self._conn: Optional[Connection] = None
        self._engine: Optional[_SAEngine] = None

    def connect(self) -> None:
        cfg = self._config
        dsn = cfg.mysql_dsn
        if not dsn:
            raise ValueError(
                "MySQL driver requires a `mysql_dsn` connection string. "
                "Set `database.mysql_dsn` in config.yaml or "
                "`A2A_REGISTRY_DATABASE__MYSQL_DSN` env variable."
            )

        engine = _sa_create_engine(
            dsn,
            poolclass=QueuePool,
            pool_size=cfg.pool_size,
            max_overflow=cfg.max_overflow,
            pool_pre_ping=True,
            pool_recycle=3600,
            pool_timeout=30,
        )
        self._engine = engine
        conn = engine.connect()
        self._conn = conn

        logger.info(
            "MySQLEngine connected (pool_size=%d, max_overflow=%d)",
            cfg.pool_size, cfg.max_overflow,
        )

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None
        if self._engine:
            self._engine.dispose()
            self._engine = None

    def execute(self, sql: str, params: tuple = ()) -> CursorResult:
        if self._conn is None:
            raise RuntimeError("Engine not connected — call connect() first")
        # Convert ? placeholders to %s for MySQL
        mysql_sql = self._translate_sql(sql)
        result = self._conn.exec_driver_sql(mysql_sql, params)
        return CursorResult(result)

    @staticmethod
    def _translate_sql(sql: str) -> str:
        """Translate SQLite-compatible SQL to MySQL dialect."""
        # Placeholders: ? → %s
        sql = sql.replace("?", "%s")
        # INSERT OR REPLACE → REPLACE
        sql = sql.replace("INSERT OR REPLACE INTO", "REPLACE INTO")
        sql = sql.replace("INSERT OR IGNORE INTO", "INSERT IGNORE INTO")
        return sql

    def executescript(self, sql: str) -> None:
        if self._conn is None:
            raise RuntimeError("Engine not connected — call connect() first")
        for statement in sql.split(";"):
            stripped = statement.strip()
            if not stripped:
                continue
            # Skip PRAGMA (SQLite-only)
            if stripped.upper().startswith("PRAGMA"):
                continue
            self._conn.exec_driver_sql(self._translate_sql(stripped))

    def begin(self, mode: str = "") -> None:
        if self._conn is None:
            raise RuntimeError("Engine not connected — call connect() first")
        self._conn.exec_driver_sql("BEGIN")

    def commit(self) -> None:
        if self._conn is None:
            raise RuntimeError("Engine not connected — call connect() first")
        self._conn.commit()

    def rollback(self) -> None:
        if self._conn is None:
            raise RuntimeError("Engine not connected — call connect() first")
        self._conn.rollback()

    @property
    def driver(self) -> str:
        return "mysql"

    def raw_connection(self) -> Any:
        if self._conn is None:
            raise RuntimeError("Engine not connected")
        return self._conn.connection


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def create_engine(db_config: DatabaseConfig) -> DatabaseEngine:
    """Factory: return a ``DatabaseEngine`` instance based on *db_config.driver*.

    Args:
        db_config:  The ``database`` section from the global :class:`Config`.

    Returns:
        A :class:`SQLiteEngine` or :class:`MySQLEngine`.

    Raises:
        ValueError: If *driver* is not ``\"sqlite\"`` or ``\"mysql\"``.
    """
    driver = db_config.driver.lower()
    if driver == "sqlite":
        return SQLiteEngine(db_config.sqlite_path)
    elif driver == "mysql":
        return MySQLEngine(db_config)
    else:
        raise ValueError(
            f"Unsupported database driver: {driver!r}. "
            f"Use 'sqlite' (dev) or 'mysql' (production)."
        )


# ---------------------------------------------------------------------------
# RetryEngine — transparent wrapper that retries transient DB errors
# ---------------------------------------------------------------------------


import time
import logging

_retry_logger = logging.getLogger("a2a_registry.database.retry")


class RetryEngine(DatabaseEngine):
    """Transparent wrapper around a ``DatabaseEngine`` that retries transient
    ``execute()`` failures with exponential backoff (3 attempts).

    Transient errors include:
    - ``sqlite3.OperationalError`` (database locked, busy)
    - ``pymysql.err.OperationalError`` (connection lost, server gone)
    - Any exception whose name or message contains "timeout" or "lock"

    Non-transient exceptions (e.g. syntax errors) are re-raised immediately.
    All other lifecycle methods (connect, close, begin, commit, rollback)
    delegate directly to the wrapped engine without retry.
    """

    def __init__(self, engine: DatabaseEngine,
                 max_retries: int = 3,
                 base_delay: float = 0.1) -> None:
        self._engine = engine
        self._max_retries = max_retries
        self._base_delay = base_delay

    # -- Pass-through lifecycle ------------------------------------------------

    def connect(self) -> None:
        self._engine.connect()

    def close(self) -> None:
        self._engine.close()

    def begin(self, mode: str = "") -> None:
        self._engine.begin(mode)

    def commit(self) -> None:
        self._engine.commit()

    def rollback(self) -> None:
        self._engine.rollback()

    def executescript(self, sql: str) -> None:
        self._engine.executescript(sql)

    @property
    def driver(self) -> str:
        return self._engine.driver

    def raw_connection(self) -> Any:
        return self._engine.raw_connection()

    # -- Retry-aware execute ---------------------------------------------------

    def execute(self, sql: str, params: tuple = ()) -> CursorResult:
        last_exc = None
        for attempt in range(self._max_retries + 1):
            try:
                return self._engine.execute(sql, params)
            except Exception as e:
                if not self._is_transient(e):
                    raise
                last_exc = e
                if attempt < self._max_retries:
                    delay = self._base_delay * (2 ** attempt)
                    _retry_logger.warning(
                        "DB execute retry %d/%d: %s — retrying in %.1fs",
                        attempt + 1, self._max_retries, e, delay,
                    )
                    time.sleep(delay)
                else:
                    _retry_logger.error(
                        "DB execute failed after %d retries: %s",
                        self._max_retries + 1, e,
                    )
        raise last_exc  # type: ignore[misc]

    @staticmethod
    def _is_transient(e: Exception) -> bool:
        exc_name = type(e).__module__ + "." + type(e).__name__
        return any(
            kw in exc_name.lower() or kw in str(e).lower()
            for kw in ["operationalerror", "databaselocked", "timeout", "lock"]
        )