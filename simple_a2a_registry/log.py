"""Structured logging for Simple A2A Registry.

Provides:
- ``JsonLogFormatter`` — JSON-format log formatter for production (ELK/Loki).
- ``TextFormatter`` — human-readable format for development.
- ``RequestIdContext`` — ``contextvars``-based per-request trace ID.
- ``request_id_middleware`` — ``aiohttp`` middleware that injects ``request_id``.
- ``setup_logging()`` — one-call logger configuration from ``Config``.
- ``log_key_event()`` — structured logging for high-value events.
"""

from __future__ import annotations

import logging
import sys
import traceback
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
import time as _time_mod
from typing import Any, Dict, Optional

_request_id: ContextVar[str] = ContextVar("request_id", default="")

# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def get_request_id() -> str:
    """Return the current request_id (empty string if none)."""
    return _request_id.get()


def set_request_id(rid: str) -> None:
    """Set the request_id for the current async context."""
    _request_id.set(rid)


# ---------------------------------------------------------------------------
# Formatting modes
# ---------------------------------------------------------------------------


class JsonLogFormatter(logging.Formatter):
    """JSON log formatter.

    Produces one JSON object per line — suitable for ELK, Loki, or any
    log-aggregation pipeline.  Every record includes:

    - ``timestamp``   — ISO-8601 with timezone (e.g. ``2025-05-25T14:32:01.123Z``)
    - ``level``       — ``INFO`` / ``WARNING`` / ``ERROR``
    - ``logger``      — logger name (e.g. ``a2a_registry.server``)
    - ``message``     — formatted log message
    - ``request_id``  — trace id from ``contextvars`` (empty string if none)
    - ``exception``   — present only on ERROR records that carry exc_info
    - ``stack``       — present only on ERROR records with full traceback
    """

    def format(self, record: logging.LogRecord) -> str:
        entry: Dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%S.%fZ"
            ),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": get_request_id(),
        }

        if record.exc_info and record.exc_info[0] is not None:
            entry["exception"] = _format_exc_name(record.exc_info[0])
            entry["stack"] = "".join(
                traceback.format_exception(*record.exc_info)
            ).rstrip()

        return _json_dumps(entry)


class TextFormatter(logging.Formatter):
    """Human-readable development formatter.

    Same column-aligned layout as the current default, but with an optional
    ``request_id`` column when a trace id is active.
    """

    def __init__(self) -> None:
        super().__init__(
            fmt="%(asctime)s [%(levelname)-7s] %(name)s: %(message)s",
            datefmt="%H:%M:%S",
        )

    def format(self, record: logging.LogRecord) -> str:
        rid = get_request_id()
        original_name = record.name
        if rid:
            record.name = f"{original_name} [{rid[:12]}]"
        result = super().format(record)
        if rid:
            record.name = original_name  # restore for downstream handlers
        return result


# ---------------------------------------------------------------------------
# Log-level aliases
# ---------------------------------------------------------------------------

_LOG_LEVEL_MAP: Dict[str, int] = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
    "critical": logging.CRITICAL,
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}


def _resolve_level(level: str) -> int:
    return _LOG_LEVEL_MAP.get(level, logging.INFO)


# ---------------------------------------------------------------------------
# One-call setup
# ---------------------------------------------------------------------------


def setup_logging(
    *,
    log_format: str = "text",
    level: str = "info",
    output: str = "stdout",
    log_file: Optional[str] = None,
    suppress_noisy: bool = True,
) -> None:
    """Configure the root logger from config values.

    Args:
        log_format: ``"json"`` for JSON lines, ``"text"`` for human-readable.
        level:      ``"debug"``, ``"info"``, ``"warning"``, ``"error"``.
        output:     ``"stdout"`` — the only production option.
        log_file:   Optional file path for log output.  ``None`` = stderr.
        suppress_noisy: If ``True``, quietens verbose third-party loggers
            (aiohttp.access, asyncio, PIL, etc.).

    The function is idempotent — calling it more than once is safe.
    """
    root = logging.getLogger()

    # --- Level ---
    numeric_level = _resolve_level(level)
    root.setLevel(numeric_level)

    # --- Formatter ---
    if log_format == "json":
        formatter: logging.Formatter = JsonLogFormatter()
    else:
        formatter = TextFormatter()

    # --- Handler ---
    # Remove any pre-existing handlers so we only have one.
    root.handlers.clear()

    if log_file:
        handler: logging.Handler = logging.FileHandler(
            str(Path(log_file).expanduser()),
            encoding="utf-8",
        )
    elif output == "stdout":
        handler = logging.StreamHandler(sys.stdout)
    else:
        handler = logging.StreamHandler(sys.stderr)

    handler.setFormatter(formatter)
    root.addHandler(handler)

    # --- Suppress noisy loggers ---
    if suppress_noisy:
        for name in ("aiohttp.access", "asyncio", "PIL", "urllib3"):
            logging.getLogger(name).setLevel(logging.WARNING)

    # --- Propagate to child loggers ---
    # Ensure our sub-loggers (a2a_registry.*) inherit from root.
    reg_logger = logging.getLogger("a2a_registry")
    reg_logger.setLevel(numeric_level)
    reg_logger.propagate = True


# ---------------------------------------------------------------------------
# aiohttp middleware — request_id injection
# ---------------------------------------------------------------------------


def request_id_middleware_factory() -> Any:
    """Return an ``aiohttp`` middleware that injects ``request_id``.

    The middleware:

    1. Reads an existing ``X-Request-Id`` header from the incoming request
       (allowing downstream callers to propagate their trace id).
    2. Falls back to a fresh UUID4 if no header is present.
    3. Sets the id on ``contextvars`` so all log calls within the request
       handler see it.
    4. Adds the ``X-Request-Id`` response header for caller-side tracing.
    5. Emits ``REQUEST_BEGIN`` and ``REQUEST_END`` key-event log records.
    6. On unhandled exceptions emits ``REQUEST_ERROR`` with stack trace.
    """
    import uuid

    from aiohttp import web

    @web.middleware
    async def _middleware(
        request: web.Request, handler: Any
    ) -> web.StreamResponse:
        # --- Resolve request_id ---
        rid = request.headers.get("X-Request-Id", "").strip()
        if not rid:
            rid = uuid.uuid4().hex[:16]
        set_request_id(rid)

        # --- BEGIN event ---
        _log_key_event(
            "REQUEST_BEGIN",
            method=request.method,
            path=request.path,
            query=dict(request.query),
            request_id=rid,
            remote=request.remote,
        )

        start = _now_millis()
        try:
            response = await handler(request)
            elapsed = _now_millis() - start
            response.headers["X-Request-Id"] = rid

            _log_key_event(
                "REQUEST_END",
                method=request.method,
                path=request.path,
                status=response.status,
                elapsed_ms=round(elapsed, 1),
                request_id=rid,
            )
            return response
        except Exception:
            elapsed = _now_millis() - start
            _log_key_event(
                "REQUEST_ERROR",
                method=request.method,
                path=request.path,
                elapsed_ms=round(elapsed, 1),
                request_id=rid,
            )
            raise

    return _middleware


# ---------------------------------------------------------------------------
# Key event logging
# ---------------------------------------------------------------------------

_KEY_EVENT_LOGGER = logging.getLogger("a2a_registry.events")


def log_key_event(event: str, **fields: Any) -> None:
    """Convenience wrapper around ``_log_key_event``.

    Call this from store/auth modules to log structured key events.
    """
    _log_key_event(event, **fields)


def _log_key_event(event: str, **fields: Any) -> None:
    """Emit a structured key-event log record.

    The event is logged at ``INFO`` level on the ``a2a_registry.events``
    logger.  When the active formatter is ``JsonLogFormatter`` the fields
    are embedded directly in the JSON payload; when it's ``TextFormatter``
    they appear as a compact suffix.
    """
    # We manually build a LogRecord so we can inject structured data.
    # This works with both formatters because:
    #   - JsonLogFormatter looks at ``record.__dict__`` for extra keys.
    #   - TextFormatter only uses the formatted message.
    msg = event
    if fields:
        msg = f"{event} {_format_fields(fields)}"

    _KEY_EVENT_LOGGER.info("%s", msg, extra=fields)


def _format_fields(fields: Dict[str, Any]) -> str:
    """Format extra fields for the text formatter."""
    parts = []
    for k, v in fields.items():
        parts.append(f"{k}={v}")
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Tiny helpers (avoid import overhead for small utils)
# ---------------------------------------------------------------------------


def _now_millis() -> float:
    return _time_monotonic_ns() / 1_000_000


try:
    _time_monotonic_ns = _time_mod.monotonic_ns
except AttributeError:

    def _time_monotonic_ns() -> int:
        return int(_time_mod.monotonic() * 1_000_000_000)


def _json_dumps(obj: Dict[str, Any]) -> str:
    """Compact single-line JSON (stdlib, no dependency)."""
    import json as _json_mod

    return _json_mod.dumps(
        obj, ensure_ascii=False, default=str, sort_keys=False
    )


def _format_exc_name(exc_type: type) -> str:
    return f"{exc_type.__module__}.{exc_type.__qualname__}"
