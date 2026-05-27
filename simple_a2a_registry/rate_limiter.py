"""Rate limiter for A2A Registry — Token Bucket algorithm with pluggable storage.

Provides:
- ``MemoryTokenBucket`` — in-memory token bucket (default, zero deps)
- ``MySQLTokenBucket`` — MySQL-backed token bucket (production)
- ``rate_limit_middleware_factory`` — aiohttp middleware that enforces
  per-IP / per-client_id rate limits with config-driven defaults.
- ``RateLimitConfig`` — re-exported from config.py so callers don't
  need to import config directly.
"""

from __future__ import annotations

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, List, Optional, Set

from aiohttp import web

logger = logging.getLogger("a2a_registry.rate_limiter")

# ---------------------------------------------------------------------------
# Bucket representation
# ---------------------------------------------------------------------------


class TokenBucket(ABC):
    """Abstract token bucket — tracks tokens and refill rate."""

    @abstractmethod
    async def consume(
        self, key: str, tokens: int, rate: int, burst: int,
    ) -> tuple[bool, int, float]:
        """Try to consume *tokens* from the bucket identified by *key*.

        Args:
            key: Unique bucket identifier (IP or client_id).
            tokens: Number of tokens to consume (usually 1 per request).
            rate: Token refill rate per minute.
            burst: Maximum burst size (capacity).

        Returns:
            ``(allowed, remaining, reset_at)`` — ``allowed`` is ``True``
            if the request should be permitted, ``remaining`` is the
            number of tokens left after consumption, and ``reset_at``
            is a Unix timestamp of when the bucket will be full again.
        """
        ...


# ---------------------------------------------------------------------------
# In-memory Token Bucket (default)
# ---------------------------------------------------------------------------


class MemoryTokenBucket(TokenBucket):
    """Thread-safe in-memory token bucket using an ``asyncio.Lock``."""

    def __init__(self) -> None:
        self._buckets: Dict[str, float] = {}     # key -> current tokens
        self._last_refill: Dict[str, float] = {}  # key -> last refill time
        self._rates: Dict[str, tuple[int, int]] = {}  # key -> (rate_per_min, burst)
        self._lock = asyncio.Lock()

    async def consume(
        self, key: str, tokens: int, rate: int, burst: int,
    ) -> tuple[bool, int, float]:
        async with self._lock:
            now = time.time()
            current = self._buckets.get(key)
            last_refill = self._last_refill.get(key, now)

            if current is None:
                # First request — full bucket
                current = float(burst)
                self._rates[key] = (rate, burst)
            else:
                # Refill based on elapsed time
                elapsed = now - last_refill
                refill = elapsed * (rate / 60.0)
                current = min(current + refill, burst)
                self._rates[key] = (rate, burst)

            allowed = False
            if current >= tokens:
                current -= tokens
                allowed = True

            self._buckets[key] = current
            self._last_refill[key] = now

            # Calculate reset time: when the bucket would be full again
            deficit = burst - current
            reset_at = now + (deficit / (rate / 60.0)) if rate > 0 else now + 60

            return allowed, int(current), reset_at


# ---------------------------------------------------------------------------
# MySQL-backed Token Bucket (production)
# ---------------------------------------------------------------------------


class MySQLTokenBucket(TokenBucket):
    """MySQL-backed token bucket.

    Expects a table ``rate_limit_buckets`` with schema::

        CREATE TABLE IF NOT EXISTS rate_limit_buckets (
            bucket_key VARCHAR(255) PRIMARY KEY,
            tokens DOUBLE NOT NULL DEFAULT 0,
            rate INT NOT NULL DEFAULT 60,
            burst INT NOT NULL DEFAULT 60,
            last_refill DOUBLE NOT NULL DEFAULT 0,
            updated_at DOUBLE NOT NULL DEFAULT 0
        );
    """

    CREATE_TABLE_SQL = """
        CREATE TABLE IF NOT EXISTS rate_limit_buckets (
            bucket_key VARCHAR(255) PRIMARY KEY,
            tokens DOUBLE NOT NULL DEFAULT 0,
            rate INT NOT NULL DEFAULT 60,
            burst INT NOT NULL DEFAULT 60,
            last_refill DOUBLE NOT NULL DEFAULT 0,
            updated_at DOUBLE NOT NULL DEFAULT 0
        )
    """

    def __init__(self, engine: Any) -> None:
        """*engine* is the shared database engine from ``database/engine.py``."""
        self._engine = engine
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        try:
            conn = self._engine.connect()
            conn.execute(self.CREATE_TABLE_SQL)
            conn.close()
        except Exception as e:
            logger.warning("Failed to create rate_limit_buckets table (non-fatal): %s", e)

    async def consume(
        self, key: str, tokens: int, rate: int, burst: int,
    ) -> tuple[bool, int, float]:
        now = time.time()

        def _do_consume() -> tuple[bool, int, float]:
            conn = self._engine.connect()
            try:
                row = conn.execute(
                    "SELECT tokens, last_refill, rate, burst FROM rate_limit_buckets "
                    "WHERE bucket_key = ?",
                    (key,),
                ).fetchone()

                if row is None:
                    # Insert new bucket
                    current = float(burst)
                    conn.execute(
                        "INSERT INTO rate_limit_buckets (bucket_key, tokens, rate, burst, last_refill, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (key, current - tokens, rate, burst, now, now),
                    )
                    conn.commit()
                    allowed_ = tokens <= burst
                    remaining_ = int(current - tokens)
                    deficit_ = burst - (current - tokens)
                    reset_at_ = now + (deficit_ / (rate / 60.0)) if rate > 0 else now + 60
                    return allowed_, max(0, remaining_), reset_at_

                current = float(row[0])
                last_refill = float(row[1])
                stored_rate = int(row[2])
                stored_burst = int(row[3])
                effective_rate = rate if rate > 0 else stored_rate
                effective_burst = burst if burst > 0 else stored_burst

                # Refill
                elapsed = now - last_refill
                refill = elapsed * (effective_rate / 60.0)
                current = min(current + refill, effective_burst)

                allowed_ = False
                if current >= tokens:
                    current -= tokens
                    allowed_ = True

                conn.execute(
                    "UPDATE rate_limit_buckets SET tokens = ?, last_refill = ?, updated_at = ? "
                    "WHERE bucket_key = ?",
                    (current, now, now, key),
                )
                conn.commit()

                deficit_ = effective_burst - current
                reset_at_ = now + (deficit_ / (effective_rate / 60.0)) if effective_rate > 0 else now + 60

                return allowed_, int(current), reset_at_
            finally:
                conn.close()

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _do_consume)


# ---------------------------------------------------------------------------
# Rate limiter middleware
# ---------------------------------------------------------------------------


def _get_client_key(request: web.Request) -> str:
    """Determine the rate limit key for a request.

    Priority:
    1. client_id from auth token (if authenticated, ``request["agent_id"]``)
    2. IP address from ``X-Forwarded-For`` / ``X-Real-IP``
    3. ``remote`` from the TCP connection
    """
    agent_id = request.get("agent_id", "")
    if agent_id and agent_id != "anonymous":
        return f"client:{agent_id}"

    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return f"ip:{forwarded.split(',')[0].strip()}"

    real_ip = request.headers.get("X-Real-IP", "")
    if real_ip:
        return f"ip:{real_ip.strip()}"

    peer = request.remote or "unknown"
    return f"ip:{peer}"


def _is_whitelisted(request: web.Request, whitelist: Set[str]) -> bool:
    """Check if the request's client_id is whitelisted."""
    agent_id = request.get("agent_id", "")
    if agent_id and agent_id in whitelist:
        return True
    # Also check by IP key
    key = _get_client_key(request)
    if key.startswith("client:"):
        cid = key[len("client:"):]
        return cid in whitelist
    return False


def _public_path(path: str) -> bool:
    """Paths that should be rate limited at the lower unauthenticated rate."""
    return (
        path.startswith("/auth/")
        or path.startswith("/.well-known/")
        or path == "/health"
        or path == "/"
        or path == "/metrics"
    )


def rate_limit_middleware_factory(
    *,
    enabled: bool,
    default_unauthenticated: int = 60,
    default_authenticated: int = 300,
    storage: str = "memory",
    whitelist: Optional[List[str]] = None,
    engine: Any = None,
) -> Callable:
    """Create an aiohttp middleware that enforces per-key rate limits.

    Args:
        enabled: Master switch — when ``False`` the middleware is a no-op.
        default_unauthenticated: Default rate (req/min) for public endpoints.
        default_authenticated: Default rate (req/min) for authenticated endpoints.
        storage: Backend storage — ``"memory"`` or ``"mysql"``.
        whitelist: List of client_ids exempt from rate limiting.
        engine: Database engine (required when ``storage="mysql"``).

    Returns:
        An aiohttp middleware coroutine.
    """
    if not enabled:
        @web.middleware
        async def _noop_middleware(
            request: web.Request, handler: Any,
        ) -> web.StreamResponse:
            return await handler(request)
        return _noop_middleware

    bucket: TokenBucket
    if storage == "mysql":
        if engine is None:
            logger.error("rate_limit.storage=mysql but no database engine provided — falling back to memory")
            bucket = MemoryTokenBucket()
        else:
            bucket = MySQLTokenBucket(engine)
    else:
        bucket = MemoryTokenBucket()

    whitelist_set: Set[str] = set(whitelist or [])
    logger.info(
        "Rate limiter enabled: unauthenticated=%d req/min, authenticated=%d req/min, "
        "storage=%s, whitelist=%d client(s)",
        default_unauthenticated, default_authenticated, storage, len(whitelist_set),
    )

    @web.middleware
    async def _rate_limit_middleware(
        request: web.Request, handler: Any,
    ) -> web.StreamResponse:
        # Exempt CORS preflight and WebSocket upgrade from rate limiting
        if request.method == "OPTIONS":
            return await handler(request)

        key = _get_client_key(request)

        # Whitelist check
        if _is_whitelisted(request, whitelist_set):
            return await handler(request)

        # Determine rate based on auth status and path
        agent_id = request.get("agent_id", "")
        is_authenticated = bool(agent_id) and agent_id != "anonymous"

        rate = default_authenticated if is_authenticated else default_unauthenticated
        # For authenticated users hitting public paths, use unauthenticated rate
        if is_authenticated and _public_path(request.path):
            rate = default_unauthenticated

        allowed, remaining, reset_at = await bucket.consume(key, 1, rate, rate)

        # Build response headers
        headers: Dict[str, str] = {
            "X-RateLimit-Limit": str(rate),
            "X-RateLimit-Remaining": str(remaining),
            "X-RateLimit-Reset": str(int(reset_at)),
        }

        if not allowed:
            retry_after = max(1, int(reset_at - time.time()))
            headers["Retry-After"] = str(retry_after)
            return web.json_response(
                {
                    "error": "too_many_requests",
                    "detail": f"Rate limit exceeded. Try again in {retry_after} second(s).",
                    "rate_limit": {
                        "limit": rate,
                        "remaining": 0,
                        "reset_at": int(reset_at),
                    },
                },
                status=429,
                headers=headers,
            )

        response = await handler(request)

        # Inject rate limit headers into the response
        if hasattr(response, "headers"):
            for hdr_key, hdr_val in headers.items():
                response.headers[hdr_key] = hdr_val
            response.headers["Vary"] = _merge_vary(
                response.headers.get("Vary", ""),
                "Origin",
            )

        return response

    return _rate_limit_middleware


def _merge_vary(existing: str, new_val: str) -> str:
    """Add *new_val* to an existing ``Vary`` header value if not already present."""
    parts = [p.strip() for p in existing.split(",") if p.strip()]
    if new_val not in parts:
        parts.append(new_val)
    return ", ".join(parts)


# ---------------------------------------------------------------------------
# Re-export for convenience
# ---------------------------------------------------------------------------

from simple_a2a_registry.config import RateLimitConfig  # noqa: F811, E402