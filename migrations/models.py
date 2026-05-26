"""SQLAlchemy table metadata matching the existing DDL schemas.

These are used by Alembic autogenerate to detect schema changes.
Both registry (Store) and board (TaskStore) tables are included.
"""

from sqlalchemy import (
    Column, Integer, String, Text, Float, BigInteger, Index,
    MetaData, Table,
)

# ---------------------------------------------------------------------------
# Naming convention for Alembic-generated constraints
# ---------------------------------------------------------------------------

naming_convention = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

metadata = MetaData(naming_convention=naming_convention)

# ===================================================================
# Registry tables (from simple_a2a_registry/store.py)
# ===================================================================

agents = Table(
    "agents",
    metadata,
    Column("id", String(255), primary_key=True),
    Column("card_json", Text, nullable=False),
    Column("heartbeat_at", Float, nullable=False, server_default="0"),
    Column("registered_at", String(255), nullable=False),
    Column("created_at", Float, nullable=False),
    mysql_engine="InnoDB",
    mysql_charset="utf8mb4",
)

Index("idx_agents_heartbeat", agents.c.heartbeat_at)

oauth_clients = Table(
    "oauth_clients",
    metadata,
    Column("client_id", String(255), primary_key=True),
    Column("client_secret_hash", String(255), nullable=False),
    Column("allowed_scopes", Text, nullable=False),
    Column("agent_card_id", String(255), nullable=False, server_default=""),
    Column("created_at", Float, nullable=False),
    Column("description", Text, nullable=False),
    mysql_engine="InnoDB",
    mysql_charset="utf8mb4",
)

oauth_tokens = Table(
    "oauth_tokens",
    metadata,
    Column("jti", String(255), primary_key=True),
    Column("client_id", String(255), nullable=False),
    Column("scope", Text, nullable=False),
    Column("expires_at", Float, nullable=False),
    mysql_engine="InnoDB",
    mysql_charset="utf8mb4",
)

Index("idx_oauth_tokens_client_id", oauth_tokens.c.client_id)

auth_codes = Table(
    "auth_codes",
    metadata,
    Column("code", String(255), primary_key=True),
    Column("client_id", String(255), nullable=False),
    Column("code_challenge", String(255), nullable=False),
    Column("code_challenge_method", String(255), nullable=False),
    Column("redirect_uri", Text, nullable=False),
    Column("scope", Text, nullable=False),
    Column("created_at", Float, nullable=False),
    mysql_engine="InnoDB",
    mysql_charset="utf8mb4",
)

# ===================================================================
# Board tables (from simple_a2a_registry/orchestration/store.py)
# ===================================================================

tasks = Table(
    "tasks",
    metadata,
    Column("id", String(255), primary_key=True),
    Column("title", String(255), nullable=False),
    Column("body", Text),
    Column("assignee", String(255)),
    Column("status", String(50), nullable=False, server_default="todo"),
    Column("priority", Integer, nullable=False, server_default="0"),
    Column("created_by", String(255)),
    Column("created_at", BigInteger, nullable=False),
    Column("started_at", BigInteger),
    Column("completed_at", BigInteger),
    Column("workspace_kind", String(50)),
    Column("workspace_path", String(1024)),
    Column("claim_lock", String(255)),
    Column("claim_expires", BigInteger),
    Column("tenant", String(255)),
    Column("result", Text),
    Column("consecutive_failures", Integer, nullable=False, server_default="0"),
    Column("worker_pid", Integer),
    Column("last_failure_error", Text),
    Column("max_runtime_seconds", Integer),
    Column("last_heartbeat_at", BigInteger),
    Column("current_run_id", Integer),
    Column("max_retries", Integer),
    mysql_engine="InnoDB",
    mysql_charset="utf8mb4",
)

Index("idx_tasks_assignee_status", tasks.c.assignee, tasks.c.status)
Index("idx_tasks_status", tasks.c.status)
Index("idx_tasks_tenant", tasks.c.tenant)

task_links = Table(
    "task_links",
    metadata,
    Column("parent_id", String(255), primary_key=True),
    Column("child_id", String(255), primary_key=True),
    mysql_engine="InnoDB",
    mysql_charset="utf8mb4",
)

task_runs = Table(
    "task_runs",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("task_id", String(255), nullable=False),
    Column("profile", String(255)),
    Column("status", String(50), nullable=False, server_default="running"),
    Column("claim_lock", String(255)),
    Column("claim_expires", BigInteger),
    Column("worker_pid", Integer),
    Column("max_runtime_seconds", Integer),
    Column("last_heartbeat_at", BigInteger),
    Column("started_at", BigInteger, nullable=False),
    Column("ended_at", BigInteger),
    Column("outcome", String(50)),
    Column("summary", Text),
    Column("metadata", Text),
    Column("error", Text),
    mysql_engine="InnoDB",
    mysql_charset="utf8mb4",
)

Index("idx_task_runs_task_id", task_runs.c.task_id, task_runs.c.started_at)

task_comments = Table(
    "task_comments",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("task_id", String(255), nullable=False),
    Column("author", String(255), nullable=False),
    Column("body", Text, nullable=False),
    Column("created_at", BigInteger, nullable=False),
    mysql_engine="InnoDB",
    mysql_charset="utf8mb4",
)

Index("idx_task_comments_task_id", task_comments.c.task_id, task_comments.c.created_at)

task_events = Table(
    "task_events",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("task_id", String(255), nullable=False),
    Column("run_id", Integer),
    Column("kind", String(100), nullable=False),
    Column("payload", Text),
    Column("created_at", BigInteger, nullable=False),
    mysql_engine="InnoDB",
    mysql_charset="utf8mb4",
)

Index("idx_task_events_task_id", task_events.c.task_id, task_events.c.created_at)