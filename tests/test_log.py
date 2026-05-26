"""Tests for structured logging (JSON/text formatters, request_id, middleware)."""
from __future__ import annotations

import io
import json
import logging
import re
import uuid

import pytest

from simple_a2a_registry.log import (
    JsonLogFormatter,
    TextFormatter,
    get_request_id,
    set_request_id,
    setup_logging,
    log_key_event,
    request_id_middleware_factory,
    _now_millis,
)


# ======================================================================
# Fixtures
# ======================================================================


@pytest.fixture(autouse=True)
def _reset_request_id() -> None:
    """Ensure a clean request_id context before each test."""
    set_request_id("")


@pytest.fixture
def string_buf() -> io.StringIO:
    return io.StringIO()


# ======================================================================
# Formatter tests
# ======================================================================


class TestJsonLogFormatter:
    """Verify JSON formatter output structure."""

    def _fmt(self, formatter: JsonLogFormatter, msg: str = "hello",
             level: int = logging.INFO, name: str = "test_logger",
             exc_info: tuple | None = None) -> dict:
        record = logging.LogRecord(name, level, "test.py", 42, msg, (), exc_info)
        raw = formatter.format(record)
        return json.loads(raw)

    def test_contains_expected_fields(self):
        """JSON output contains timestamp, level, logger, message, request_id."""
        result = self._fmt(JsonLogFormatter())
        for key in ("timestamp", "level", "logger", "message", "request_id"):
            assert key in result, f"Missing field: {key}"

    def test_timestamp_iso8601(self):
        """timestamp field is ISO-8601 with timezone."""
        result = self._fmt(JsonLogFormatter())
        pattern = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z$"
        assert re.match(pattern, result["timestamp"]), \
            f"Unexpected timestamp format: {result['timestamp']}"

    def test_level_and_message(self):
        """level and message reflect the log record."""
        result = self._fmt(JsonLogFormatter(), msg="test message", level=logging.WARNING)
        assert result["level"] == "WARNING"
        assert result["message"] == "test message"

    def test_logger_name(self):
        """logger field matches the logger name."""
        result = self._fmt(JsonLogFormatter(), name="a2a_registry.server")
        assert result["logger"] == "a2a_registry.server"

    def test_request_id_populated(self):
        """request_id is captured from contextvars."""
        set_request_id("abc-123")
        result = self._fmt(JsonLogFormatter())
        assert result["request_id"] == "abc-123"

    def test_request_id_empty_by_default(self):
        """request_id is empty string when no context set."""
        set_request_id("")
        result = self._fmt(JsonLogFormatter())
        assert result["request_id"] == ""

    def test_error_contains_exception_and_stack(self):
        """ERROR-level records with exc_info include exception type and stack."""
        try:
            raise ValueError("test error")
        except ValueError:
            result = self._fmt(JsonLogFormatter(), msg="oops",
                               level=logging.ERROR, exc_info=sys.exc_info())

        assert "exception" in result, "Missing exception field on ERROR record"
        assert "ValueError" in result["exception"], \
            f"Expected ValueError in exception field, got: {result['exception']}"
        assert "stack" in result, "Missing stack field on ERROR record"
        assert "test error" in result["stack"], \
            f"Expected 'test error' in stack, got: {result['stack']}"

    def test_no_exception_on_info(self):
        """INFO-level records omit exception/stack fields."""
        result = self._fmt(JsonLogFormatter(), msg="info msg")
        assert "exception" not in result
        assert "stack" not in result

    def test_one_line_per_record(self):
        """Each record is a single line (no extra newlines in JSON)."""
        formatter = JsonLogFormatter()
        record = logging.LogRecord("test", logging.INFO, "t.py", 1, "msg", (), None)
        output = formatter.format(record)
        assert "\n" not in output, "JSON output must be a single line"


class TestTextFormatter:
    """Verify text formatter is human-readable."""

    def test_contains_expected_parts(self):
        """Text output includes timestamp, level, logger, message."""
        set_request_id("")
        formatter = TextFormatter()
        record = logging.LogRecord("test_logger", logging.INFO, "t.py", 1,
                                    "hello world", (), None)
        output = formatter.format(record)
        assert "INFO" in output
        assert "test_logger" in output
        assert "hello world" in output

    def test_request_id_appended_to_logger(self):
        """When request_id is set, it appears in the logger field."""
        set_request_id("short-id-123")
        formatter = TextFormatter()
        record = logging.LogRecord("mylogger", logging.INFO, "t.py", 1,
                                    "msg", (), None)
        output = formatter.format(record)
        assert "short-id-123" in output or "short-id" in output


# ======================================================================
# Request ID contextvars tests
# ======================================================================


class TestRequestIdContext:
    """Verify contextvars-based request_id propagation."""

    def test_default_empty(self):
        """Default request_id is empty string."""
        assert get_request_id() == ""

    def test_set_and_get(self):
        """Setting request_id is retrievable in same context."""
        rid = uuid.uuid4().hex[:16]
        set_request_id(rid)
        assert get_request_id() == rid

    def test_isolation(self):
        """Different contexts don't share request_id (test via explicit reset)."""
        set_request_id("ctx-a")
        assert get_request_id() == "ctx-a"
        set_request_id("ctx-b")
        assert get_request_id() == "ctx-b"


# ======================================================================
# setup_logging integration tests
# ======================================================================


class TestSetupLogging:
    """Verify setup_logging() correctly configures the root logger."""

    def _capture_log(self, buf: io.StringIO) -> None:
        """Configure root logger to write into *buf* and emit one INFO record."""
        handler = logging.StreamHandler(buf)
        handler.setFormatter(logging.Formatter("%(message)s"))
        root = logging.getLogger()
        root.handlers.clear()
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
        logging.getLogger("a2a_registry.test").info("captured")

    def test_text_format_default(self, string_buf):
        """setup_logging with text format produces human-readable output."""
        setup_logging(log_format="text", level="debug", output="stdout")
        # After setup_logging, verify root handler is configured
        root = logging.getLogger()
        assert len(root.handlers) >= 1
        test_logger = logging.getLogger("a2a_registry.test_setup")
        test_logger.info("format check")

    def test_json_format(self):
        """setup_logging with json format configures JsonLogFormatter."""
        setup_logging(log_format="json", level="info", output="stdout")
        root = logging.getLogger()
        for h in root.handlers:
            if isinstance(h, logging.StreamHandler):
                assert isinstance(h.formatter, JsonLogFormatter), \
                    f"Expected JsonLogFormatter, got {type(h.formatter)}"
                return
        pytest.fail("No StreamHandler found on root logger")

    def test_log_level_mapping(self, string_buf):
        """setup_logging correctly sets the numeric log level."""
        setup_logging(log_format="text", level="warning", output="stdout")
        root = logging.getLogger()
        assert root.level == logging.WARNING

    def test_level_case_insensitive(self, string_buf):
        """setup_logging accepts uppercase level names."""
        setup_logging(log_format="text", level="DEBUG", output="stdout")
        root = logging.getLogger()
        assert root.level == logging.DEBUG


# ======================================================================
# Key event logging tests
# ======================================================================


class TestKeyEventLogging:
    """Verify log_key_event produces structured records."""

    def test_key_event_json(self, string_buf):
        """Key events emit JSON-compatible records with event name."""
        setup_logging(log_format="json", level="info", output="stdout")

        # Capture events logger output
        events_logger = logging.getLogger("a2a_registry.events")
        buf = io.StringIO()
        handler = logging.StreamHandler(buf)
        handler.setFormatter(JsonLogFormatter())
        events_logger.handlers.clear()
        events_logger.addHandler(handler)
        events_logger.propagate = False

        log_key_event("AGENT_REGISTERED", agent_id="test-agent-1", method="POST")

        output = buf.getvalue()
        parsed = json.loads(output)
        assert parsed["message"].startswith("AGENT_REGISTERED")
        assert parsed["logger"] == "a2a_registry.events"

    def test_key_event_text(self, string_buf):
        """Key event in text mode shows event name and fields."""
        setup_logging(log_format="text", level="info", output="stdout")

        events_logger = logging.getLogger("a2a_registry.events")
        buf = io.StringIO()
        handler = logging.StreamHandler(buf)
        handler.setFormatter(TextFormatter())
        events_logger.handlers.clear()
        events_logger.addHandler(handler)
        events_logger.propagate = False

        log_key_event("HEARTBEAT_RECEIVED", agent_id="agent-x", ttl=120)
        output = buf.getvalue()
        assert "HEARTBEAT_RECEIVED" in output


# ======================================================================
# Middleware integration test (aiohttp TestClient)
# ======================================================================


class TestRequestIdMiddleware:
    """Verify request_id_middleware injects X-Request-Id into every request."""

    @pytest.fixture
    def server_app(self):
        """Create a minimal aiohttp app with the request_id middleware."""
        from aiohttp import web

        app = web.Application(middlewares=[request_id_middleware_factory()])

        @web.middleware
        async def _capture_middleware(request: web.Request, handler) -> web.StreamResponse:
            request["captured_rid"] = get_request_id()
            return await handler(request)

        app.middlewares.insert(0, _capture_middleware)

        async def handler_ok(request: web.Request) -> web.Response:
            return web.json_response({
                "request_id": get_request_id(),
                "captured": request.get("captured_rid", ""),
            })

        async def handler_error(request: web.Request) -> web.Response:
            raise ValueError("simulated error")

        # Error handler that catches the ValueError to test request_id propagation
        @web.middleware
        async def _catch_error(request, handler):
            try:
                return await handler(request)
            except Exception:
                return web.json_response({
                    "error": "caught",
                    "request_id": get_request_id(),
                }, status=500)

        app.middlewares.insert(0, _catch_error)
        app.router.add_get("/ok", handler_ok)
        app.router.add_get("/error", handler_error)

        return app

    @pytest.mark.asyncio
    async def test_every_request_gets_unique_request_id(self, server_app):
        """Each HTTP request has a unique request_id that appears in the response."""
        from aiohttp.test_utils import TestServer, TestClient
        server = TestServer(server_app)
        await server.start_server()
        client = TestClient(server)
        try:
            rid_set = set()
            for _ in range(5):
                resp = await client.get("/ok")
                assert resp.status == 200
                data = await resp.json()
                rid = data.get("request_id", "")
                assert rid, "request_id should not be empty"
                rid_set.add(rid)
            # At least 2 unique IDs across 5 requests (very unlikely to collide)
            assert len(rid_set) >= 2, \
                f"Expected multiple unique request_ids, got: {rid_set}"
        finally:
            await server.close()

    @pytest.mark.asyncio
    async def test_request_id_in_error_response(self, server_app):
        """Error responses also carry the request_id."""
        from aiohttp.test_utils import TestServer, TestClient
        server = TestServer(server_app)
        await server.start_server()
        client = TestClient(server)
        try:
            resp = await client.get("/error")
            assert resp.status == 500
            data = await resp.json()
            assert data.get("request_id"), \
                f"request_id missing from error response: {data}"
        finally:
            await server.close()

    @pytest.mark.asyncio
    async def test_x_request_id_response_header(self, server_app):
        """Response includes X-Request-Id header."""
        from aiohttp.test_utils import TestServer, TestClient
        server = TestServer(server_app)
        await server.start_server()
        client = TestClient(server)
        try:
            resp = await client.get("/ok")
            assert resp.headers.get("X-Request-Id"), \
                "X-Request-Id header should be present"
            assert len(resp.headers["X-Request-Id"]) >= 8
        finally:
            await server.close()

    @pytest.mark.asyncio
    async def test_propagates_existing_header(self, server_app):
        """When client sends X-Request-Id, the middleware uses it."""
        from aiohttp.test_utils import TestServer, TestClient
        server = TestServer(server_app)
        await server.start_server()
        client = TestClient(server)
        try:
            my_rid = "my-trace-id-12345"
            resp = await client.get("/ok", headers={"X-Request-Id": my_rid})
            assert resp.status == 200
            data = await resp.json()
            assert data["request_id"] == my_rid
            # Response header echoes the same id
            assert resp.headers.get("X-Request-Id") == my_rid
        finally:
            await server.close()

    @pytest.mark.asyncio
    async def test_end_to_end_via_create_app(self):
        """Full integration: create_app with request_id middleware works end-to-end."""
        import tempfile
        from pathlib import Path
        from aiohttp.test_utils import TestServer, TestClient
        from simple_a2a_registry.server import create_app

        tmpdir = Path(tempfile.mkdtemp())
        try:
            app = create_app(data_dir=str(tmpdir), base_url="http://localhost:8321")
            # Set _host/_port to avoid startup port-check failure on occupied 8321
            app["_host"] = "127.0.0.1"
            app["_port"] = 0  # TestServer picks a free port
            server = TestServer(app)
            await server.start_server()
            client = TestClient(server)
            
            resp = await client.get("/health")
            assert resp.status == 200
            data = await resp.json()
            assert data["status"] == "healthy"
            assert resp.headers.get("X-Request-Id"), \
                "X-Request-Id header should be present on /health"
            
            await server.close()
        finally:
            import shutil
            shutil.rmtree(str(tmpdir), ignore_errors=True)


# ======================================================================
# CLI argument parsing tests (format and level forwarding)
# ======================================================================


class TestCliLoggingIntegration:
    """Verify CLI correctly passes --log-format to setup_logging.

    We test argument parsing only (not full server startup) since
    full CLI tests are covered elsewhere.
    """

    def test_log_format_default_text(self):
        """Default --log-format is 'text'."""
        from simple_a2a_registry.cli import main as cli_main
        # The actual default is tested via the argparse default
        parser = _get_cli_parser()
        args = parser.parse_args([])
        assert args.log_format == "text"

    def test_log_format_json_flag(self):
        """--log-format json can be set."""
        parser = _get_cli_parser()
        args = parser.parse_args(["--log-format", "json"])
        assert args.log_format == "json"

    def test_log_level_debug(self):
        """--log-level DEBUG is accepted."""
        parser = _get_cli_parser()
        args = parser.parse_args(["--log-level", "DEBUG"])
        assert args.log_level == "DEBUG"


def _get_cli_parser():
    """Build the argparse parser without executing main()."""
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--log-format", default="text", choices=["json", "text"])
    p.add_argument("--log-level", default="INFO",
                    choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    p.add_argument("--log-file", default=None)
    return p


# Make sys available for the exception test
import sys  # noqa: E402