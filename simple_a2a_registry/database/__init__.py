"""Database engine abstraction — SQLite/MySQL dual implementation.

Supports runtime switching via the ``driver`` config field:

    driver: sqlite   → SQLiteEngine  (dev, backward-compatible)
    driver: mysql    → MySQLEngine   (production)

Engine factory::

    engine = create_engine(config.database)
"""

from simple_a2a_registry.database.engine import (
    DatabaseEngine,
    CursorResult,
    SQLiteEngine,
    MySQLEngine,
    RetryEngine,
    create_engine,
)

__all__ = [
    "DatabaseEngine",
    "CursorResult",
    "SQLiteEngine",
    "MySQLEngine",
    "RetryEngine",
    "create_engine",
]
