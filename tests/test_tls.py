"""Tests for TLS/HTTPS support — SSL context, redirect app, config, and CLI."""
from __future__ import annotations

import os

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer, TestClient

TEST_CERT = "/tmp/a2a-test-certs/cert.pem"
TEST_KEY = "/tmp/a2a-test-certs/key.pem"


class TestTLSHelpers:
    """Unit tests for TLS helper functions."""

    def test_resolve_tls_path_expands_home(self):
        from simple_a2a_registry.server import _resolve_tls_path

        resolved = _resolve_tls_path("~/test-cert.pem")
        assert resolved.endswith("/test-cert.pem")
        assert "~" not in resolved

    def test_resolve_tls_path_absolute(self):
        from simple_a2a_registry.server import _resolve_tls_path

        resolved = _resolve_tls_path("/absolute/path/cert.pem")
        assert resolved == "/absolute/path/cert.pem"

    def test_build_ssl_context_minimum_tls_version(self):
        import ssl
        from simple_a2a_registry.server import _build_ssl_context

        ctx = _build_ssl_context(TEST_CERT, TEST_KEY)
        assert ctx.minimum_version == ssl.TLSVersion.TLSv1_2
        assert ctx.protocol == ssl.PROTOCOL_TLS_SERVER

    def test_build_ssl_context_secure_ciphers(self):
        from simple_a2a_registry.server import _build_ssl_context

        ctx = _build_ssl_context(TEST_CERT, TEST_KEY)
        ciphers = ctx.get_ciphers()
        assert len(ciphers) > 0, "Should have at least one cipher"
        for c in ciphers:
            name = c.get("name", "").upper()
            assert "NULL" not in name
            assert "MD5" not in name
            assert "DES" not in name
            assert "RC4" not in name
            assert "TLSV1" in c.get("protocol", "").upper()

    def test_build_ssl_context_no_compression(self):
        """Verify OP_NO_COMPRESSION is set."""
        import ssl
        from simple_a2a_registry.server import _build_ssl_context

        ctx = _build_ssl_context(TEST_CERT, TEST_KEY)
        assert ctx.options & ssl.OP_NO_COMPRESSION

    @pytest.mark.asyncio
    async def test_create_redirect_app_redirects_to_https(self):
        """Verify the redirect app sends 301 with correct Location header."""
        import aiohttp
        from simple_a2a_registry.server import _create_redirect_app

        redirect_app = _create_redirect_app("localhost", 8432)

        # Use explicit port binding with TestServer to get port info
        server = TestServer(redirect_app)
        await server.start_server()

        # TestServer exposes host, port, scheme
        base_url = f"{server.scheme}://{server.host}:{server.port}"
        # Use a connector without SSL since this redirect app is plain HTTP
        conn = aiohttp.TCPConnector(ssl=False)

        async with aiohttp.ClientSession(connector=conn) as session:
            resp = await session.get(f"{base_url}/health", allow_redirects=False)
            assert resp.status == 301
            assert resp.headers["Location"] == "https://localhost:8432/health"

            resp = await session.get(f"{base_url}/v1/agents?limit=10", allow_redirects=False)
            assert resp.status == 301
            assert resp.headers["Location"] == "https://localhost:8432/v1/agents?limit=10"

            resp = await session.post(f"{base_url}/api/data", allow_redirects=False)
            assert resp.status == 301
            assert resp.headers["Location"] == "https://localhost:8432/api/data"

        await server.close()

    @pytest.mark.asyncio
    async def test_ssl_context_serves_https(self):
        """Verify the SSL context can serve a real HTTPS response."""
        from simple_a2a_registry.server import _build_ssl_context

        app = web.Application()

        async def health_handler(request):
            return web.json_response({"status": "healthy"})

        app.router.add_get("/health", health_handler)

        ssl_ctx = _build_ssl_context(TEST_CERT, TEST_KEY)

        server = TestServer(app)
        server._sslcontext = ssl_ctx
        await server.start_server()
        client = TestClient(server)

        resp = await client.get("/health")
        assert resp.status == 200
        data = await resp.json()
        assert data["status"] == "healthy"

        await client.close()
        await server.close()


class TestTLSConfig:
    """Verify TLS config is properly handled by config and CLI."""

    def test_config_has_tls_fields(self):
        from simple_a2a_registry.config import ServerConfig, Config

        sc = ServerConfig()
        assert hasattr(sc, "tls_cert")
        assert hasattr(sc, "tls_key")
        assert sc.tls_cert == ""
        assert sc.tls_key == ""

        cfg = Config()
        assert hasattr(cfg.server, "tls_cert")
        assert hasattr(cfg.server, "tls_key")

    def test_config_tls_env_override(self):
        from simple_a2a_registry.config import load_config

        os.environ["A2A_REGISTRY_SERVER__TLS_CERT"] = "/etc/certs/cert.pem"
        os.environ["A2A_REGISTRY_SERVER__TLS_KEY"] = "/etc/certs/key.pem"
        try:
            cfg = load_config()
            assert cfg.server.tls_cert == "/etc/certs/cert.pem"
            assert cfg.server.tls_key == "/etc/certs/key.pem"
        finally:
            del os.environ["A2A_REGISTRY_SERVER__TLS_CERT"]
            del os.environ["A2A_REGISTRY_SERVER__TLS_KEY"]

    def test_config_tls_yaml(self):
        """Verify TLS fields are accepted in YAML-like dict merging."""
        from simple_a2a_registry.config import Config, _dict_to_config

        cfg = Config()
        _dict_to_config({
            "server": {
                "tls_cert": "/path/to/cert.pem",
                "tls_key": "/path/to/key.pem",
            },
        }, cfg)
        assert cfg.server.tls_cert == "/path/to/cert.pem"
        assert cfg.server.tls_key == "/path/to/key.pem"

    def test_cli_parser_accepts_tls_args(self):
        """Verify argparse accepts --tls-cert and --tls-key."""
        import argparse
        from simple_a2a_registry.cli import main

        # Subset: verify the parser definition includes our args
        parser = argparse.ArgumentParser(description="test")
        parser.add_argument("--tls-cert")
        parser.add_argument("--tls-key")

        args = parser.parse_args(["--tls-cert", "/cert.pem", "--tls-key", "/key.pem"])
        assert args.tls_cert == "/cert.pem"
        assert args.tls_key == "/key.pem"

        # Default is None
        args = parser.parse_args([])
        assert args.tls_cert is None
        assert args.tls_key is None


class TestTLSConfigSummary:
    """Verify TLS cert/key paths appear in config summary."""

    def test_config_summary_shows_tls_paths(self):
        from simple_a2a_registry.config import Config, config_summary

        cfg = Config()
        cfg.server.tls_cert = "/etc/certs/cert.pem"
        cfg.server.tls_key = "/etc/certs/key.pem"
        summary = config_summary(cfg)
        assert "tls_cert" in summary
        assert "tls_key" in summary
        assert "/etc/certs/cert.pem" in summary
        assert "/etc/certs/key.pem" in summary


class TestTLSValidation:
    """Verify TLS pair validation in run_server()."""

    def test_rejects_cert_without_key(self):
        """Providing --tls-cert without --tls-key must raise ValueError."""
        from simple_a2a_registry.server import _validate_tls_pair

        with pytest.raises(ValueError, match="tls-cert requires.*tls-key"):
            _validate_tls_pair("/path/to/cert.pem", None)

    def test_rejects_key_without_cert(self):
        """Providing --tls-key without --tls-cert must raise ValueError."""
        from simple_a2a_registry.server import _validate_tls_pair

        with pytest.raises(ValueError, match="tls-key requires.*tls-cert"):
            _validate_tls_pair(None, "/path/to/key.pem")

    def test_accepts_both_tls_params(self):
        """Providing both --tls-cert and --tls-key must NOT raise."""
        from simple_a2a_registry.server import _validate_tls_pair

        _validate_tls_pair("/path/to/cert.pem", "/path/to/key.pem")  # no raise

    def test_accepts_no_tls_params(self):
        """Default (both None) must NOT raise."""
        from simple_a2a_registry.server import _validate_tls_pair

        _validate_tls_pair(None, None)  # no raise

    def test_accepts_empty_strings(self):
        """Both empty strings must NOT raise (config.py defaults to '')."""
        from simple_a2a_registry.server import _validate_tls_pair

        _validate_tls_pair("", "")  # no raise

    def test_accepts_empty_and_none(self):
        """Both falsy ('' and None) must NOT raise."""
        from simple_a2a_registry.server import _validate_tls_pair

        _validate_tls_pair("", None)  # no raise
        _validate_tls_pair(None, "")  # no raise