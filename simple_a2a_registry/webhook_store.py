"""Webhook Subscription Engine — outbound event-driven push.

Provides database-backed webhook subscriptions, HMAC-SHA256 signed delivery
with exponential backoff retry, automatic disable on failure threshold, and
a plugin hook for event-driven trigger.

Schema
------
webhook_subscriptions:
    id, url, events (JSON array), secret, enabled, retry_count,
    last_failure_at, consecutive_failures, created_at, tenant

webhook_deliveries:
    id, subscription_id, event_type, payload (JSON), status,
    response_code, attempt, error_message, delivered_at
"""

from __future__ import annotations

import hashlib
import hmac
import asyncio
import json
import logging
import secrets
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import aiohttp

from simple_a2a_registry.database import DatabaseEngine

logger = logging.getLogger("a2a_registry.webhook")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_CONSECUTIVE_FAILURES = 10  # auto-disable after this many consecutive fails
RETRY_DELAYS = [2, 4, 8]       # exponential backoff in seconds
DEFAULT_TIMEOUT = 10           # seconds per delivery attempt

# ---------------------------------------------------------------------------
# Dataclass records
# ---------------------------------------------------------------------------


@dataclass
class WebhookSubscription:
    """A webhook subscription — defines which events to deliver where."""
    id: str
    url: str
    events: List[str]        # e.g. ["task_created", "agent_registered"]
    secret: str              # HMAC-SHA256 signing secret
    enabled: bool = True
    retry_count: int = 0     # total retries across all deliveries
    consecutive_failures: int = 0
    last_failure_at: Optional[float] = None
    created_at: float = 0.0
    tenant: str = ""


@dataclass
class WebhookDelivery:
    """A single webhook delivery attempt record."""
    id: str
    subscription_id: str
    event_type: str
    payload: Optional[Dict[str, Any]] = None
    status: str = "pending"  # pending | success | failed
    response_code: int = 0
    attempt: int = 1
    error_message: str = ""
    delivered_at: Optional[float] = None


# ---------------------------------------------------------------------------
# SQL schema — SQLite
# ---------------------------------------------------------------------------

_SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA busy_timeout=5000;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS webhook_subscriptions (
    id                    TEXT PRIMARY KEY,
    url                   TEXT NOT NULL,
    events                TEXT NOT NULL DEFAULT '[]',
    secret                TEXT NOT NULL,
    enabled               INTEGER NOT NULL DEFAULT 1,
    retry_count           INTEGER NOT NULL DEFAULT 0,
    consecutive_failures  INTEGER NOT NULL DEFAULT 0,
    last_failure_at       REAL,
    created_at            REAL NOT NULL,
    tenant                TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS webhook_deliveries (
    id                TEXT PRIMARY KEY,
    subscription_id   TEXT NOT NULL,
    event_type        TEXT NOT NULL,
    payload           TEXT,
    status            TEXT NOT NULL DEFAULT 'pending',
    response_code     INTEGER NOT NULL DEFAULT 0,
    attempt           INTEGER NOT NULL DEFAULT 1,
    error_message     TEXT NOT NULL DEFAULT '',
    delivered_at      REAL,
    FOREIGN KEY (subscription_id) REFERENCES webhook_subscriptions(id)
);

CREATE INDEX IF NOT EXISTS idx_wh_deliveries_sub ON webhook_deliveries(subscription_id);
CREATE INDEX IF NOT EXISTS idx_wh_deliveries_status ON webhook_deliveries(status);
CREATE INDEX IF NOT EXISTS idx_wh_sub_events ON webhook_subscriptions(enabled);
"""

# ---------------------------------------------------------------------------
# SQL schema — MySQL
# ---------------------------------------------------------------------------

_SCHEMA_SQL_MYSQL = """
CREATE TABLE IF NOT EXISTS webhook_subscriptions (
    id                    VARCHAR(36) PRIMARY KEY,
    url                   TEXT NOT NULL,
    events                JSON NOT NULL,
    secret                VARCHAR(255) NOT NULL,
    enabled               TINYINT(1) NOT NULL DEFAULT 1,
    retry_count           INT NOT NULL DEFAULT 0,
    consecutive_failures  INT NOT NULL DEFAULT 0,
    last_failure_at       DOUBLE,
    created_at            DOUBLE NOT NULL,
    tenant                VARCHAR(255) NOT NULL DEFAULT ''
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS webhook_deliveries (
    id                VARCHAR(36) PRIMARY KEY,
    subscription_id   VARCHAR(36) NOT NULL,
    event_type        VARCHAR(64) NOT NULL,
    payload           JSON,
    status            VARCHAR(16) NOT NULL DEFAULT 'pending',
    response_code     INT NOT NULL DEFAULT 0,
    attempt           INT NOT NULL DEFAULT 1,
    error_message     TEXT NOT NULL DEFAULT '',
    delivered_at      DOUBLE,
    FOREIGN KEY (subscription_id) REFERENCES webhook_subscriptions(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE INDEX idx_wh_deliveries_sub ON webhook_deliveries(subscription_id);
CREATE INDEX idx_wh_deliveries_status ON webhook_deliveries(status);
CREATE INDEX idx_wh_sub_enabled ON webhook_subscriptions(enabled);
"""


# ---------------------------------------------------------------------------
# Schema creation helper
# ---------------------------------------------------------------------------


def _maybe_create_webhook_schema(engine: DatabaseEngine) -> None:
    """Create webhook tables on first connect."""
    if engine.driver == "sqlite":
        engine.executescript(_SCHEMA_SQL)
        engine.commit()
    elif engine.driver == "mysql":
        for statement in _SCHEMA_SQL_MYSQL.split(";"):
            stripped = statement.strip()
            if not stripped:
                continue
            try:
                engine.execute(stripped)
            except Exception:
                pass  # ignore "already exists" errors
        engine.commit()


# ---------------------------------------------------------------------------
# Signature helpers
# ---------------------------------------------------------------------------


def _sign_payload(payload: bytes, secret: str) -> str:
    """Compute HMAC-SHA256 signature of *payload* using *secret*.

    Returns the hex digest.
    """
    return hmac.new(
        secret.encode("utf-8"),
        payload,
        hashlib.sha256,
    ).hexdigest()


def _generate_secret() -> str:
    """Generate a cryptographically random HMAC secret."""
    return secrets.token_hex(32)


# ---------------------------------------------------------------------------
# WebhookStore
# ---------------------------------------------------------------------------


class WebhookStore:
    """Database-backed store for webhook subscriptions and delivery logs.

    Thread-safe via the underlying engine's locking (``BEGIN IMMEDIATE``
    transactions for SQLite).
    """

    def __init__(self, engine: DatabaseEngine) -> None:
        self._engine = engine

    # ------------------------------------------------------------------
    # Subscriptions CRUD
    # ------------------------------------------------------------------

    def create_subscription(
        self,
        url: str,
        events: List[str],
        secret: Optional[str] = None,
        tenant: str = "",
    ) -> WebhookSubscription:
        """Create a new webhook subscription.

        Args:
            url: The target URL for webhook delivery.
            events: List of event types to subscribe to (e.g. ``["task_created"]``).
            secret: Optional HMAC secret. Auto-generated if omitted.
            tenant: Optional tenant namespace.

        Returns:
            The created :class:`WebhookSubscription`.
        """
        sub_id = f"wh_{uuid.uuid4().hex[:12]}"
        now = time.time()
        secret_val = secret or _generate_secret()

        self._engine.begin("IMMEDIATE")
        try:
            self._engine.execute(
                """INSERT INTO webhook_subscriptions
                   (id, url, events, secret, enabled, retry_count,
                    consecutive_failures, last_failure_at, created_at, tenant)
                   VALUES (?, ?, ?, ?, 1, 0, 0, NULL, ?, ?)""",
                (sub_id, url, json.dumps(events), secret_val, now, tenant),
            )
            self._engine.commit()
        except Exception:
            self._engine.rollback()
            raise

        return WebhookSubscription(
            id=sub_id,
            url=url,
            events=events,
            secret=secret_val,
            enabled=True,
            created_at=now,
            tenant=tenant,
        )

    def get_subscription(self, sub_id: str) -> Optional[WebhookSubscription]:
        """Get a subscription by ID."""
        cur = self._engine.execute(
            "SELECT * FROM webhook_subscriptions WHERE id = ?",
            (sub_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return self._row_to_subscription(row)

    def list_subscriptions(self, tenant: str = "") -> List[WebhookSubscription]:
        """List all subscriptions, optionally filtered by tenant."""
        if tenant:
            cur = self._engine.execute(
                "SELECT * FROM webhook_subscriptions WHERE tenant = ? ORDER BY created_at DESC",
                (tenant,),
            )
        else:
            cur = self._engine.execute(
                "SELECT * FROM webhook_subscriptions ORDER BY created_at DESC"
            )
        return [self._row_to_subscription(row) for row in cur.fetchall()]

    def delete_subscription(self, sub_id: str) -> bool:
        """Delete a subscription. Returns True if deleted."""
        self._engine.begin("IMMEDIATE")
        try:
            # Delete associated deliveries first
            self._engine.execute(
                "DELETE FROM webhook_deliveries WHERE subscription_id = ?",
                (sub_id,),
            )
            cur = self._engine.execute(
                "DELETE FROM webhook_subscriptions WHERE id = ?",
                (sub_id,),
            )
            deleted = cur.rowcount > 0
            self._engine.commit()
            return deleted
        except Exception:
            self._engine.rollback()
            raise

    def disable_subscription(self, sub_id: str) -> bool:
        """Disable a subscription. Returns True if updated."""
        self._engine.begin("IMMEDIATE")
        try:
            cur = self._engine.execute(
                "UPDATE webhook_subscriptions SET enabled = 0 WHERE id = ?",
                (sub_id,),
            )
            updated = cur.rowcount > 0
            self._engine.commit()
            return updated
        except Exception:
            self._engine.rollback()
            raise

    def enable_subscription(self, sub_id: str) -> bool:
        """Re-enable a subscription. Returns True if updated."""
        self._engine.begin("IMMEDIATE")
        try:
            cur = self._engine.execute(
                "UPDATE webhook_subscriptions SET enabled = 1, "
                "consecutive_failures = 0 WHERE id = ?",
                (sub_id,),
            )
            updated = cur.rowcount > 0
            self._engine.commit()
            return updated
        except Exception:
            self._engine.rollback()
            raise

    def get_subscriptions_for_event(self, event_type: str, tenant: str = "") -> List[WebhookSubscription]:
        """Get enabled subscriptions that match *event_type*.

        Queries subscriptions where ``events`` contains the event type
        (JSON array containment).
        """
        # For SQLite, use LIKE on the serialised JSON array.
        # For small-scale webhook use this is adequate. A production
        # system on MySQL can use JSON_CONTAINS() or a proper join table.
        if tenant:
            cur = self._engine.execute(
                "SELECT * FROM webhook_subscriptions WHERE enabled = 1 "
                "AND tenant = ? ORDER BY created_at ASC",
                (tenant,),
            )
        else:
            cur = self._engine.execute(
                "SELECT * FROM webhook_subscriptions WHERE enabled = 1 "
                "ORDER BY created_at ASC"
            )

        results: List[WebhookSubscription] = []
        for row in cur.fetchall():
            sub = self._row_to_subscription(row)
            if event_type in sub.events:
                results.append(sub)
        return results

    # ------------------------------------------------------------------
    # Deliveries CRUD
    # ------------------------------------------------------------------

    def log_delivery(
        self,
        subscription_id: str,
        event_type: str,
        payload: Optional[Dict[str, Any]] = None,
        status: str = "pending",
        response_code: int = 0,
        attempt: int = 1,
        error_message: str = "",
    ) -> str:
        """Log a webhook delivery attempt.

        Returns:
            The delivery record ID.
        """
        delivery_id = f"d_{uuid.uuid4().hex[:12]}"
        now = time.time() if status != "pending" else None

        self._engine.begin("IMMEDIATE")
        try:
            self._engine.execute(
                """INSERT INTO webhook_deliveries
                   (id, subscription_id, event_type, payload, status,
                    response_code, attempt, error_message, delivered_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    delivery_id,
                    subscription_id,
                    event_type,
                    json.dumps(payload) if payload else None,
                    status,
                    response_code,
                    attempt,
                    error_message,
                    now,
                ),
            )
            self._engine.commit()
        except Exception:
            self._engine.rollback()
            raise

        return delivery_id

    def list_deliveries(
        self,
        subscription_id: str,
        limit: int = 50,
        offset: int = 0,
    ) -> List[WebhookDelivery]:
        """List delivery records for a subscription, newest first."""
        cur = self._engine.execute(
            "SELECT * FROM webhook_deliveries WHERE subscription_id = ? "
            "ORDER BY delivered_at DESC LIMIT ? OFFSET ?",
            (subscription_id, limit, offset),
        )
        return [self._row_to_delivery(row) for row in cur.fetchall()]

    def count_deliveries(self, subscription_id: str) -> int:
        """Count delivery records for a subscription."""
        cur = self._engine.execute(
            "SELECT COUNT(*) AS cnt FROM webhook_deliveries WHERE subscription_id = ?",
            (subscription_id,),
        )
        row = cur.fetchone()
        return row["cnt"] if row else 0

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_subscription(row: dict) -> WebhookSubscription:
        return WebhookSubscription(
            id=row["id"],
            url=row["url"],
            events=json.loads(row["events"]) if isinstance(row["events"], str) else row["events"],
            secret=row["secret"],
            enabled=bool(row["enabled"]),
            retry_count=row["retry_count"],
            consecutive_failures=row["consecutive_failures"],
            last_failure_at=row.get("last_failure_at"),
            created_at=row["created_at"],
            tenant=row.get("tenant", ""),
        )

    @staticmethod
    def _row_to_delivery(row: dict) -> WebhookDelivery:
        payload_raw = row.get("payload")
        payload = None
        if payload_raw:
            try:
                payload = json.loads(payload_raw) if isinstance(payload_raw, str) else payload_raw
            except (json.JSONDecodeError, TypeError):
                payload = None

        return WebhookDelivery(
            id=row["id"],
            subscription_id=row["subscription_id"],
            event_type=row["event_type"],
            payload=payload,
            status=row["status"],
            response_code=row["response_code"],
            attempt=row["attempt"],
            error_message=row.get("error_message", ""),
            delivered_at=row.get("delivered_at"),
        )


# ---------------------------------------------------------------------------
# Webhook Delivery Engine
# ---------------------------------------------------------------------------


class WebhookDeliveryEngine:
    """Delivers webhook events to subscribed endpoints.

    Features:
      - HMAC-SHA256 signed payloads
      - 3x exponential backoff retry (2s, 4s, 8s)
      - Automatic disable on 10 consecutive failures
      - Async delivery via aiohttp
    """

    def __init__(
        self,
        store: WebhookStore,
        http_session: aiohttp.ClientSession,
        on_delivery_status: Optional[callable] = None,
    ) -> None:
        """Initialise the delivery engine.

        Args:
            store: The :class:`WebhookStore` instance.
            http_session: aiohttp ``ClientSession`` for HTTP calls.
            on_delivery_status: Optional callback ``(sub_id, event_type, status, detail)``
                called after each delivery attempt. Used to push status to Admin WS.
        """
        self._store = store
        self._session = http_session
        self._on_delivery_status = on_delivery_status

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def deliver_event(
        self,
        event_type: str,
        payload: Dict[str, Any],
        tenant: str = "",
    ) -> List[Dict[str, Any]]:
        """Deliver *event_type* to every matching subscription.

        Returns:
            List of result dicts ``{subscription_id, status, response_code}``
            for each matching subscription.
        """
        subs = self._store.get_subscriptions_for_event(event_type, tenant)
        if not subs:
            logger.debug("No webhook subscriptions for event '%s' (tenant=%s)", event_type, tenant)
            return []

        results: List[Dict[str, Any]] = []
        for sub in subs:
            result = await self._deliver_to_subscription(sub, event_type, payload)
            results.append(result)

        if subs:
            logger.info(
                "Delivered event '%s' to %d webhook subscription(s)",
                event_type, len(subs),
            )

        return results

    async def _deliver_to_subscription(
        self,
        sub: WebhookSubscription,
        event_type: str,
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Deliver an event to a single subscription, with retries.

        Returns:
            Dict with ``subscription_id``, ``status``, ``response_code``.
        """
        if not sub.enabled:
            return {
                "subscription_id": sub.id,
                "status": "skipped",
                "response_code": 0,
            }

        # Build the signed payload
        body = json.dumps({
            "event_type": event_type,
            "payload": payload,
            "timestamp": time.time(),
        }).encode("utf-8")

        signature = _sign_payload(body, sub.secret)
        headers = {
            "Content-Type": "application/json",
            "X-A2A-Signature": signature,
            "User-Agent": "A2A-Registry-Webhook/1.0",
        }

        last_error = ""
        max_attempts = len(RETRY_DELAYS) + 1  # initial attempt + retries
        status_code = 0

        for attempt in range(1, max_attempts + 1):
            error_msg = ""

            try:
                async with self._session.post(
                    sub.url,
                    data=body,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=DEFAULT_TIMEOUT),
                ) as resp:
                    status_code = resp.status
                    if 200 <= status_code < 300:
                        # Success — log and return
                        self._store.log_delivery(
                            subscription_id=sub.id,
                            event_type=event_type,
                            payload=payload,
                            status="success",
                            response_code=status_code,
                            attempt=attempt,
                        )
                        self._update_consecutive_failures(sub.id, succeeded=True)

                        self._notify_status(sub.id, event_type, "success", {
                            "response_code": status_code,
                            "attempt": attempt,
                        })

                        return {
                            "subscription_id": sub.id,
                            "status": "success",
                            "response_code": status_code,
                        }
                    else:
                        error_msg = f"HTTP {status_code}"
            except asyncio.TimeoutError:
                error_msg = "timeout"
            except aiohttp.ClientError as e:
                error_msg = f"connection_error: {e}"
            except Exception as e:
                error_msg = f"unexpected: {e}"

            last_error = error_msg

            # Log the failed attempt
            self._store.log_delivery(
                subscription_id=sub.id,
                event_type=event_type,
                payload=payload,
                status="failed",
                response_code=status_code,
                attempt=attempt,
                error_message=error_msg,
            )

            logger.warning(
                "Webhook delivery attempt %d/%d failed for %s (sub=%s): %s",
                attempt, max_attempts, sub.url, sub.id, error_msg,
            )

            self._notify_status(sub.id, event_type, "failed", {
                "response_code": status_code,
                "attempt": attempt,
                "error": error_msg,
            })

            if attempt < max_attempts:
                delay = RETRY_DELAYS[attempt - 1]
                await asyncio.sleep(delay)

        # All attempts failed — update consecutive_failures
        self._update_consecutive_failures(sub.id, succeeded=False)

        return {
            "subscription_id": sub.id,
            "status": "failed",
            "response_code": status_code,
            "error": last_error,
        }

    def _update_consecutive_failures(self, sub_id: str, succeeded: bool) -> None:
        """Update consecutive_failures counter and auto-disable if threshold reached."""
        sub = self._store.get_subscription(sub_id)
        if sub is None:
            return

        self._store._engine.begin("IMMEDIATE")
        try:
            if succeeded:
                self._store._engine.execute(
                    "UPDATE webhook_subscriptions SET consecutive_failures = 0 WHERE id = ?",
                    (sub_id,),
                )
            else:
                now = time.time()
                cur = self._store._engine.execute(
                    "UPDATE webhook_subscriptions SET "
                    "consecutive_failures = consecutive_failures + 1, "
                    "retry_count = retry_count + 1, "
                    "last_failure_at = ? "
                    "WHERE id = ?",
                    (now, sub_id),
                )

                # Check if threshold exceeded
                updated = self._store._engine.execute(
                    "SELECT consecutive_failures FROM webhook_subscriptions WHERE id = ?",
                    (sub_id,),
                )
                row = updated.fetchone()
                if row and row["consecutive_failures"] >= MAX_CONSECUTIVE_FAILURES:
                    self._store._engine.execute(
                        "UPDATE webhook_subscriptions SET enabled = 0 WHERE id = ?",
                        (sub_id,),
                    )
                    logger.warning(
                        "Webhook subscription %s auto-disabled after %d consecutive failures",
                        sub_id, MAX_CONSECUTIVE_FAILURES,
                    )

            self._store._engine.commit()
        except Exception:
            self._store._engine.rollback()
            raise

    def _notify_status(
        self,
        sub_id: str,
        event_type: str,
        status: str,
        detail: Dict[str, Any],
    ) -> None:
        """Call the status callback if configured."""
        if self._on_delivery_status:
            try:
                self._on_delivery_status(sub_id, event_type, status, detail)
            except Exception:
                logger.debug("Webhook status callback error", exc_info=True)
