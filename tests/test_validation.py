"""Comprehensive tests for input validation and security hardening.

Tests cover:
- Agent Card validation (required fields, types, lengths)
- Path parameter validation (agent_id, task_id, client_id)
- Scope name whitelist validation
- JWT claim validation
- XSS prevention (HTML output encoding)
- Body size limit middleware
- SQL injection prevention (via store parameterized queries)
"""
from __future__ import annotations

import json
import time
import tempfile

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer, TestClient

from simple_a2a_registry.validation import (
    validate_agent_card,
    validate_agent_id,
    validate_task_id,
    validate_client_id,
    validate_scope_name,
    validate_scope_list,
    validate_scope_string,
    validate_jwt_claims,
    sanitize_card_output,
    sanitize_html,
    body_size_limit_middleware_factory,
    path_param_middleware_factory,
    VALID_SCOPES,
)
from simple_a2a_registry.server import create_app


# ======================================================================
# Agent Card validation
# ======================================================================


class TestAgentCardValidation:
    """Validate Agent Card registration payload schema."""

    def test_valid_card_passes(self):
        """A minimal valid card should pass validation with no errors."""
        card = {
            "name": "Test Agent",
            "description": "A test agent",
            "version": "1.0.0",
        }
        errors = validate_agent_card(card)
        assert errors == []

    def test_missing_name(self):
        """Missing 'name' should produce a validation error."""
        card = {"description": "No name"}
        errors = validate_agent_card(card)
        assert len(errors) == 1
        assert errors[0].field == "name"

    def test_empty_name(self):
        """Empty string 'name' should produce a validation error."""
        card = {"name": "", "description": "Empty name"}
        errors = validate_agent_card(card)
        assert len(errors) == 1
        assert errors[0].field == "name"

    def test_name_too_long(self):
        """Name exceeding 255 characters should be rejected."""
        card = {"name": "A" * 256, "description": "Too long name"}
        errors = validate_agent_card(card)
        assert len(errors) == 1
        assert "255" in errors[0].detail
        assert errors[0].field == "name"

    def test_description_too_long(self):
        """Description exceeding 2000 characters should be rejected."""
        card = {"name": "Agent", "description": "B" * 2001}
        errors = validate_agent_card(card)
        assert len(errors) == 1
        assert "2000" in errors[0].detail

    def test_invalid_type_for_description(self):
        """Description must be a string."""
        card = {"name": "Agent", "description": 12345}
        errors = validate_agent_card(card)
        assert len(errors) >= 1
        assert any(e.field == "description" for e in errors)

    def test_documentation_url_too_long(self):
        """URL exceeding 2048 characters should be rejected."""
        card = {"name": "Agent", "description": "OK", "documentation_url": "http://x.com/" + "a" * 2040}
        errors = validate_agent_card(card)
        assert len(errors) == 1
        assert errors[0].field == "documentation_url"

    def test_invalid_version_type(self):
        """Version must be a string."""
        card = {"name": "Agent", "description": "desc", "version": 1}
        errors = validate_agent_card(card)
        assert len(errors) == 1
        assert "version" in errors[0].field

    def test_skills_list_validation(self):
        """Skills must be a list of dicts with required fields."""
        card = {
            "name": "Agent",
            "description": "desc",
            "skills": [
                {"id": "s1", "name": "Skill One", "description": "Does stuff"},
                {"name": "Missing ID", "description": "No id field"},
            ],
        }
        errors = validate_agent_card(card)
        assert len(errors) == 1
        assert errors[0].detail is not None

    def test_skill_field_lengths(self):
        """Skill id and name have max length limits."""
        card = {
            "name": "Agent",
            "description": "desc",
            "skills": [
                {"id": "x" * 129, "name": "Skill", "description": "desc"},
            ],
        }
        errors = validate_agent_card(card)
        assert len(errors) == 1
        assert "128" in errors[0].detail

    def test_skill_tags_limit(self):
        """Skills should not have more than 50 tags."""
        card = {
            "name": "Agent",
            "description": "desc",
            "skills": [
                {"id": "s1", "name": "Skill", "description": "desc",
                 "tags": [str(i) for i in range(51)]},
            ],
        }
        errors = validate_agent_card(card)
        assert len(errors) == 1
        assert "50" in errors[0].detail

    def test_skills_count_limit(self):
        """Skills list should not exceed 200 items."""
        card = {
            "name": "Agent",
            "description": "desc",
            "skills": [
                {"id": f"s{i}", "name": f"Skill {i}", "description": "desc"}
                for i in range(201)
            ],
        }
        errors = validate_agent_card(card)
        assert len(errors) == 1
        assert "200" in errors[0].detail

    def test_skills_must_be_list(self):
        """Skills must be a list."""
        card = {"name": "Agent", "description": "desc", "skills": "not a list"}
        errors = validate_agent_card(card)
        assert len(errors) == 1
        assert "skills" in errors[0].field

    def test_supported_interfaces_validation(self):
        """Each interface requires url, protocol_binding, protocol_version."""
        card = {
            "name": "Agent",
            "description": "desc",
            "supported_interfaces": [
                {"url": "http://example.com/api", "protocol_binding": "JSONRPC", "protocol_version": "1.0"},
                {"url": "http://example2.com/api"},  # missing protocol_binding, protocol_version
            ],
        }
        errors = validate_agent_card(card)
        assert len(errors) == 2
        assert any("protocol_binding" in e.field for e in errors)
        assert any("protocol_version" in e.field for e in errors)

    def test_security_scheme_type_validation(self):
        """Security scheme types must be from known list."""
        card = {
            "name": "Agent",
            "description": "desc",
            "security_schemes": {
                "bad-scheme": {
                    "scheme_type": "INVALID_TYPE",
                },
            },
        }
        errors = validate_agent_card(card)
        assert len(errors) == 1
        assert "INVALID_TYPE" in errors[0].detail

    def test_valid_security_scheme_types(self):
        """All valid security scheme types should pass."""
        card = {
            "name": "Agent",
            "description": "desc",
            "security_schemes": {
                "apikey": {"scheme_type": "apiKey", "api_key": {"name": "X-API-Key"}},
                "http": {"scheme_type": "http", "http_auth": {"scheme": "Bearer"}},
                "oauth2": {"scheme_type": "oauth2", "oauth2": {}},
            },
        }
        errors = validate_agent_card(card)
        assert errors == []

    def test_provider_validation(self):
        """Provider fields have length limits."""
        card = {
            "name": "Agent",
            "description": "desc",
            "provider": {
                "url": "http://valid.url",
                "organization": "O" * 256,  # exceeds 255 limit
            },
        }
        errors = validate_agent_card(card)
        assert len(errors) == 1
        assert "organization" in errors[0].field or "organization" in errors[0].detail

    def test_interfaces_count_limit(self):
        """Supported interfaces should not exceed 10 items."""
        card = {
            "name": "Agent",
            "description": "desc",
            "supported_interfaces": [
                {"url": f"http://example{i}.com", "protocol_binding": "JSONRPC", "protocol_version": "1.0"}
                for i in range(11)
            ],
        }
        errors = validate_agent_card(card)
        assert len(errors) == 1
        assert "10" in errors[0].detail

    def test_default_input_modes_type(self):
        """default_input_modes must be a list."""
        card = {"name": "Agent", "description": "desc", "default_input_modes": "text/plain"}
        errors = validate_agent_card(card)
        assert len(errors) == 1
        assert "default_input_modes" in errors[0].field


# ======================================================================
# Path parameter validation
# ======================================================================


class TestPathParamValidation:
    """Validate URL path parameter formats."""

    def test_valid_agent_ids(self):
        assert validate_agent_id("agent_id", "my-agent-123") is None
        assert validate_agent_id("agent_id", "simple.a2a.registry") is None
        assert validate_agent_id("agent_id", "a") is None
        assert validate_agent_id("agent_id", "test_agent") is None
        assert validate_agent_id("agent_id", "a" * 128) is None

    def test_invalid_agent_ids(self):
        assert validate_agent_id("agent_id", "") is not None
        assert validate_agent_id("agent_id", "-invalid") is not None
        assert validate_agent_id("agent_id", ".invalid") is not None
        assert validate_agent_id("agent_id", "agent@id") is not None
        assert validate_agent_id("agent_id", "agent/id") is not None
        assert validate_agent_id("agent_id", "agent id") is not None
        assert validate_agent_id("agent_id", "a" * 129) is not None

    def test_valid_task_ids(self):
        """UUID v4 and t_<hex> formats should be valid."""
        assert validate_task_id("task_id", "550e8400-e29b-41d4-a716-446655440000") is None
        assert validate_task_id("task_id", "t_abc123456789") is None
        assert validate_task_id("task_id", "t_a1b2c3d4") is None

    def test_invalid_task_ids(self):
        assert validate_task_id("task_id", "") is not None
        assert validate_task_id("task_id", "not-a-uuid") is not None
        assert validate_task_id("task_id", "t_") is not None
        assert validate_task_id("task_id", "abc-123") is not None

    def test_valid_client_ids(self):
        assert validate_client_id("client_id", "client-abc123") is None
        assert validate_client_id("client_id", "simple-a2a-registry") is None

    def test_invalid_client_ids(self):
        assert validate_client_id("client_id", "") is not None
        assert validate_client_id("client_id", "client@id") is not None


# ======================================================================
# Scope validation
# ======================================================================


class TestScopeValidation:
    """Validate scope name whitelist validation."""

    def test_valid_scopes(self):
        for scope in VALID_SCOPES:
            assert validate_scope_name(scope) is None, f"Scope '{scope}' should be valid"

    def test_invalid_scope(self):
        err = validate_scope_name("invalid:scope")
        assert err is not None
        assert "Invalid scope" in err.detail

    def test_empty_scope(self):
        err = validate_scope_name("")
        assert err is not None

    def test_scope_list_valid(self):
        errors = validate_scope_list(["task:read", "task:write", "agent:read"])
        assert errors == []

    def test_scope_list_with_invalid(self):
        errors = validate_scope_list(["task:read", "bad:scope"])
        assert len(errors) == 1

    def test_scope_string_valid(self):
        errors = validate_scope_string("task:read task:write agent:read")
        assert errors == []

    def test_scope_string_with_invalid(self):
        errors = validate_scope_string("task:read bad:scope")
        assert len(errors) == 1

    def test_scope_string_empty(self):
        errors = validate_scope_string("")
        assert errors == []


# ======================================================================
# JWT claim validation
# ======================================================================


class TestJWTClaims:
    """Validate JWT token payload claims."""

    def test_valid_payload(self):
        payload = {
            "iss": "simple-a2a-registry",
            "sub": "client-test",
            "exp": 9999999999,
            "jti": "abc-123",
            "scope": "task:read task:write",
        }
        errors = validate_jwt_claims(payload)
        assert errors == []

    def test_valid_payload_no_scope(self):
        """Scope is optional — valid payload without scope should pass."""
        payload = {
            "iss": "simple-a2a-registry",
            "sub": "client-test",
            "exp": 9999999999,
            "jti": "abc-123",
        }
        errors = validate_jwt_claims(payload)
        assert errors == []

    def test_missing_issuer(self):
        payload = {"sub": "test", "exp": 9999999999}
        errors = validate_jwt_claims(payload)
        assert len(errors) == 1
        assert "iss" in errors[0].field

    def test_empty_subject(self):
        payload = {"iss": "registry", "sub": "", "exp": 9999999999}
        errors = validate_jwt_claims(payload)
        assert len(errors) == 1
        assert "sub" in errors[0].field

    def test_invalid_exp_type(self):
        payload = {"iss": "registry", "sub": "test", "exp": "not-a-number"}
        errors = validate_jwt_claims(payload)
        assert len(errors) == 1
        assert "exp" in errors[0].field

    def test_scope_with_invalid_format(self):
        """Scope must be 'category:action' space-separated."""
        payload = {
            "iss": "simple-a2a-registry",
            "sub": "test",
            "exp": 9999999999,
            "scope": "invalidformat",
        }
        errors = validate_jwt_claims(payload)
        # Produces 2 errors: format invalid + scope not in whitelist
        assert len(errors) == 2
        assert any("format" in e.detail.lower() for e in errors)
        assert any("Invalid scope" in e.detail for e in errors)

    def test_scope_not_in_whitelist(self):
        payload = {
            "iss": "simple-a2a-registry",
            "sub": "test",
            "exp": 9999999999,
            "scope": "admin:super",
        }
        errors = validate_jwt_claims(payload)
        assert len(errors) >= 1
        assert any("Invalid scope" in e.detail for e in errors)

    def test_empty_jti_allowed(self):
        """jti is optional — empty should pass."""
        payload = {
            "iss": "simple-a2a-registry",
            "sub": "test",
            "exp": 9999999999,
        }
        errors = validate_jwt_claims(payload)
        assert errors == []


# ======================================================================
# XSS prevention — HTML output encoding
# ======================================================================


class TestXSSPrevention:
    """Test HTML encoding for XSS prevention in API responses."""

    def test_sanitize_html_escapes_angle_brackets(self):
        result = sanitize_html("<script>alert('xss')</script>")
        assert "&lt;" in result
        assert "&gt;" in result
        assert "<script>" not in result

    def test_sanitize_html_escapes_quotes(self):
        result = sanitize_html('"onclick="evil()"')
        assert "&quot;" in result

    def test_sanitize_html_non_string(self):
        assert sanitize_html(123) == 123
        assert sanitize_html(None) is None
        assert sanitize_html(True) is True

    def test_sanitize_card_output_top_level(self):
        card = {
            "name": '<script>alert("xss")</script>',
            "description": "Normal description",
            "version": "1.0.0",
        }
        result = sanitize_card_output(card)
        assert result["description"] == "Normal description"
        assert "&lt;" in result["name"]
        assert "&gt;" in result["name"]
        assert "<script>" not in result["name"]

    def test_sanitize_card_output_nested(self):
        card = {
            "name": "Agent",
            "skills": [
                {"name": '<img src=x onerror=alert(1)>', "id": "s1", "description": "desc"},
            ],
        }
        result = sanitize_card_output(card)
        skill_name = result["skills"][0]["name"]
        assert "&lt;" in skill_name
        assert "&gt;" in skill_name
        assert "<img" not in skill_name

    def test_sanitize_card_output_lists(self):
        card = {
            "name": "Agent",
            "skills": [
                {"name": "Safe", "id": "s1", "description": "desc",
                 "tags": ['<script>alert(1)</script>', 'normal-tag']},
            ],
        }
        result = sanitize_card_output(card)
        assert "&lt;" in result["skills"][0]["tags"][0]
        assert result["skills"][0]["tags"][1] == "normal-tag"

    def test_sanitize_card_output_double_encoding_guard(self):
        """Already-encoded strings should not be double-encoded."""
        card = {
            "name": "&amp;lt;safe&amp;gt;",
        }
        result = sanitize_card_output(card)
        assert result["name"] == "&amp;lt;safe&amp;gt;"

    def test_sanitize_card_output_deep_depth(self):
        """Deeply nested dicts should not cause stack overflow."""
        deep = {"name": "<script>"}
        d = deep
        for _ in range(25):
            d["nested"] = {}
            d = d["nested"]
        # This shouldn't crash
        result = sanitize_card_output(deep)
        assert result is not None


# ======================================================================
# Body size limit middleware
# ======================================================================


class TestBodySizeLimit:
    """Test middleware that enforces POST/PUT body size limits."""

    @pytest.fixture
    async def _make_client(self):
        """Create a test client for body-size middleware tests."""
        created = []

        async def maker(middlewares, route_cfg):
            app = web.Application(middlewares=middlewares)
            method, path, handler = route_cfg
            app.router.add_route(method, path, handler)
            server = TestServer(app)
            await server.start_server()
            client = TestClient(server)
            created.append((server, client))
            return client

        yield maker

        for server, client in created:
            await client.close()
            await server.close()

    async def test_small_body_passes(self, _make_client):
        """Body under the limit should pass through."""
        async def handler(request):
            return web.json_response({"ok": True})
        client = await _make_client(
            [body_size_limit_middleware_factory(max_body_size=1024)],
            ("POST", "/test", handler),
        )
        resp = await client.post("/test", json={"data": "small"})
        assert resp.status == 200

    async def test_large_body_is_rejected(self, _make_client):
        """Body exceeding the limit should return 413."""
        async def handler(request):
            return web.json_response({"ok": True})
        client = await _make_client(
            [body_size_limit_middleware_factory(max_body_size=100)],
            ("POST", "/test", handler),
        )
        resp = await client.post(
            "/test",
            data="x" * 200,
            headers={"Content-Type": "application/json"},
        )
        assert resp.status == 413
        data = await resp.json()
        assert data["error"] == "payload_too_large"

    async def test_no_content_length_passes(self, _make_client):
        """Requests without Content-Length header should pass through."""
        async def handler(request):
            return web.json_response({"ok": True})
        client = await _make_client(
            [body_size_limit_middleware_factory(max_body_size=100)],
            ("POST", "/test", handler),
        )
        resp = await client.post("/test", data="small")
        assert resp.status == 200

    async def test_get_method_not_checked(self, _make_client):
        """GET requests should not be subject to body size limit."""
        async def handler(request):
            return web.json_response({"ok": True})
        client = await _make_client(
            [body_size_limit_middleware_factory(max_body_size=10)],
            ("GET", "/test", handler),
        )
        resp = await client.get("/test")
        assert resp.status == 200


# ======================================================================
# SQL injection prevention
# ======================================================================


class TestSQLInjectionPrevention:
    """Verify that parameterized queries prevent SQL injection."""

    async def test_sql_injection_in_agent_name(self, app_factory):
        """SQL injection attempt via agent name should be rejected gracefully."""
        async with await app_factory() as client:
            resp = await client.post("/v1/agents", json={
                "name": "'; DROP TABLE agents; --",
                "description": "SQL injection attempt",
            })
            # Should either register the agent safely (201) or reject (400)
            assert resp.status in (201, 400)
            if resp.status == 201:
                get_resp = await client.get("/v1/agents")
                data = await get_resp.json()
                # Name is HTML-encoded, so check for encoded form
                names = [a["name"] for a in data["agents"]]
                assert any("DROP TABLE" in n for n in names)

    async def test_sql_injection_via_agent_id(self, app_factory):
        """SQL injection in agent_id URL path should be rejected."""
        async with await app_factory() as client:
            injection_id = "'; DROP TABLE agents; --"
            resp = await client.get(f"/v1/agents/{injection_id}")
            assert resp.status in (400, 404)

    async def test_sql_injection_via_skill_filter(self, app_factory):
        """SQL injection in query parameter should be handled safely."""
        async with await app_factory() as client:
            resp = await client.get(
                "/v1/agents",
                params={"skill": "'; DROP TABLE agents; --"},
            )
            assert resp.status == 200
            data = await resp.json()
            assert "agents" in data

    async def test_sql_injection_via_task_id(self, app_factory):
        """SQL injection in task_id path param should be rejected."""
        async with await app_factory() as client:
            resp = await client.get("/v1/tasks/'; DROP TABLE oauth_tokens; --")
            assert resp.status in (400, 404)


# ======================================================================
# End-to-end integration: validation in the full app
# ======================================================================


@pytest.fixture
async def app_factory():
    """Return a callable that creates a fresh TestClient for each test."""
    factories = []

    async def maker():
        tmpdir_obj = tempfile.TemporaryDirectory()
        factories.append(tmpdir_obj)
        data_dir = tmpdir_obj.name
        app = create_app(
            data_dir=data_dir,
            base_url="http://localhost:8321",
        )
        server = TestServer(app)
        await server.start_server()
        client = TestClient(server)
        return client

    yield maker

    for f in factories:
        try:
            f.cleanup()
        except Exception:
            pass


class TestEndToEndValidation:
    """End-to-end tests verifying validation middleware in the live app."""

    async def test_agent_with_maxed_fields(self, app_factory):
        """Agent card with fields at (but not exceeding) limits should register."""
        async with await app_factory() as client:
            resp = await client.post("/v1/agents", json={
                "name": "A" * 255,
                "description": "B" * 2000,
                "version": "1.0.0",
            })
            assert resp.status == 201

    async def test_agent_with_too_long_name(self, app_factory):
        """Agent card with name exceeding 255 chars should be rejected."""
        async with await app_factory() as client:
            resp = await client.post("/v1/agents", json={
                "name": "A" * 256,
                "description": "Test agent with too long name",
            })
            assert resp.status == 400
            data = await resp.json()
            assert data["error"] == "validation_error"

    async def test_agent_with_too_long_description(self, app_factory):
        """Agent card with description exceeding 2000 chars should be rejected."""
        async with await app_factory() as client:
            resp = await client.post("/v1/agents", json={
                "name": "Valid Name",
                "description": "B" * 2001,
            })
            assert resp.status == 400

    async def test_xss_in_name_is_encoded(self, app_factory):
        """XSS in agent name should be HTML-encoded in the response."""
        async with await app_factory() as client:
            xss_name = '<script>alert("xss")</script>'
            resp = await client.post("/v1/agents", json={
                "name": xss_name,
                "description": "XSS test agent",
            })
            assert resp.status == 201
            agent_id = (await resp.json())["id"]

            get_resp = await client.get(f"/v1/agents/{agent_id}")
            assert get_resp.status == 200
            data = await get_resp.json()
            assert "&lt;" in data["name"]
            assert "&gt;" in data["name"]
            assert "<script>" not in data["name"]

    async def test_invalid_agent_id_rejected(self, app_factory):
        """Agent ID path with invalid characters should be rejected."""
        async with await app_factory() as client:
            resp = await client.get("/v1/agents/invalid@id!")
            assert resp.status in (400, 404)

    async def test_list_agents_sanitizes_output(self, app_factory):
        """List agents endpoint should HTML-encode string values."""
        async with await app_factory() as client:
            await client.post("/v1/agents", json={
                "name": '<b>Bold Agent</b>',
                "description": 'Safe description',
            })
            resp = await client.get("/v1/agents")
            assert resp.status == 200
            data = await resp.json()
            for agent in data["agents"]:
                if "Bold Agent" in agent.get("name", ""):
                    assert "&lt;" in agent["name"]
                    assert "<b>" not in agent["name"]
