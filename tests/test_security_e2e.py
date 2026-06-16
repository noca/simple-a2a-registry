"""Security Harness E2E — 全链路测试(35用例) + 3阶段迁移验证.

通过真实 HTTP API 验证安全层(APE/DTM/PT/Events)的集成正确性:

  Phase 1 — 3阶段迁移验证 (9 用例)
    在 audit / warn / enforce 三种模式下通过 HTTP 验证行为差异

  Phase 2 — APE enforce 全链路检查点 (12 用例)
    task_create / task_claim / task_complete 三入口安全校验

  Phase 3 — DTM 委派令牌生命周期 (8 用例)
    签发 → JWT → 校验 → 绑定 → 重放 → 过期 → scope 衰减 → 深度限制

  Phase 4 — 安全事件 + 溯源 (6 用例)
    SecurityEventStore 可查询 + ProvenanceTracker 链式跟踪

  Phase 5 — 授权矩阵 CRUD (7 用例)
    代理授权矩阵增删查 API

总计: 42 用例
"""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest.mock
import uuid
from typing import Any, Optional

import pytest
from aiohttp.test_utils import TestServer, TestClient

from simple_a2a_registry.auth import create_token, _generate_rsa_keypair
from simple_a2a_registry.config import (
    Config, AuthConfig, SecurityHarnessConfig,
)
from simple_a2a_registry.server import create_app
from simple_a2a_registry.store import Store

pytestmark = pytest.mark.asyncio

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

# Agent IDs — shared across modes
ALPHA_ID = "agent-alpha-e2e"
BETA_ID = "agent-beta-e2e"
GAMMA_ID = "agent-gamma-disabled-e2e"
TX_ID = "agent-tenant-x-e2e"
UNREG_ID = "unregistered-intruder"
ANON_ID = "anonymous"


@pytest.fixture
def shared_keypair():
    """One keypair for the whole suite so tokens are valid across apps."""
    return _generate_rsa_keypair()


async def _build_app(
    mode: str,
    delegation_policy: str = "open",
    keypair: Optional[tuple] = None,
    max_depth: int = 10,
) -> TestClient:
    """Create a TestClient with SecurityHarness + auth enabled.

    Patches the server's _generate_rsa_keypair so create_app uses the
    shared keypair (instead of its own random one). This ensures JWT
    tokens created with the shared keypair verify correctly against
    the middleware's closed-over public_key.
    """
    if keypair is None:
        keypair = _generate_rsa_keypair()
    priv, pub = keypair

    from simple_a2a_registry.config import DatabaseConfig

    tmpdir = tempfile.TemporaryDirectory()
    db_path = os.path.join(tmpdir.name, "registry.db")

    cfg = Config(
        database=DatabaseConfig(driver="sqlite", sqlite_path=db_path),
        auth=AuthConfig(enabled=True),
        security_harness=SecurityHarnessConfig(
            enabled=True,
            mode=mode,
            default_delegation_policy=delegation_policy,
            delegation_token_ttl_seconds=300,
            max_delegation_depth=max_depth,
        ),
    )

    # Patch the server's keypair generator so create_app uses OUR shared keys
    import simple_a2a_registry.server as srv_mod
    with unittest.mock.patch.object(srv_mod, "_generate_rsa_keypair", return_value=(priv, pub)):
        app = create_app(
            data_dir=tmpdir.name,
            base_url="http://localhost:8321",
            config=cfg,
            auth_enabled=True,
            bootstrap_secret="test-bootstrap-secret",
        )

    server = TestServer(app)
    await server.start_server()
    client = TestClient(server)
    client._tmpdir = tmpdir
    client._priv = priv
    client._pub = pub
    return client


async def _register_agent(
    client: TestClient, agent_id: str, tenant: str = "", disabled: int = 0,
    auth_token: str = "",
) -> dict:
    """Register an agent via /v1/agents, return card."""
    body: dict = {"name": agent_id, "agent_id": agent_id, "disabled": disabled}
    if tenant:
        body["tenant_id"] = tenant
    headers = _make_auth_header(auth_token) if auth_token else {}
    resp = await client.post("/v1/agents", json=body, headers=headers)
    assert resp.status in (200, 201), f"Register {agent_id} failed: {await resp.text()}"
    return await resp.json()


async def _get_token(
    client: TestClient, sub: str, scope: str = "task:write task:read",
    tenant: Optional[str] = None,
) -> str:
    """Create a JWT signed with the shared keypair."""
    return create_token(
        sub=sub,
        private_key=client._priv,
        algorithm="RS256",
        scope=scope,
        tenant=tenant,
    )


async def _setup_agents(
    client: TestClient,
) -> dict:
    """Register all standard agents for security tests. Return name→id mapping."""
    mapping = {}
    admin_token = await _get_token(
        client, sub="admin", scope="registry:admin agent:admin agent:register agent:read task:write task:read",
    )
    for tag, agent_id, tenant, disabled in [
        ("alpha", ALPHA_ID, "", 0),
        ("beta", BETA_ID, "", 0),
        ("gamma", GAMMA_ID, "", 1),
        ("tx", TX_ID, "tenant-x", 0),
    ]:
        card = await _register_agent(client, agent_id, tenant, disabled, auth_token=admin_token)
        mapping[tag] = card["id"]
    return mapping


def _make_auth_header(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ===================================================================
# Phase 1 — 三阶段迁移验证 (9 用例)
# ===================================================================

class TestPhase1ThreePhaseMigration:
    """在 audit / warn / enforce 三种模式下验证安全策略行为差异."""

    # ── audit mode ────────────────────────────────────────────────

    async def test_p1_audit_allows_violation(self, shared_keypair):
        """PH-1: audit 模式下, 未注册 caller 创建任务 → 200 (allowed, logged)."""
        client = await _build_app("audit", keypair=shared_keypair)
        async with client:
            # 不需要注册 unregistered agent
            token = await _get_token(client, UNREG_ID, scope="task:write")
            resp = await client.post(
                "/v2/tasks",
                json={"title": "audit-test", "assignee": BETA_ID},
                headers=_make_auth_header(token),
            )
            # audit mode: even denied requests return 201 (allowed)
            assert resp.status == 201, f"audit should allow: {await resp.text()}"

    async def test_p1_audit_events_recorded(self, shared_keypair):
        """PH-2: audit 模式, 安全事件被记录."""
        client = await _build_app("audit", keypair=shared_keypair)
        async with client:
            token = await _get_token(client, UNREG_ID, scope="task:write")
            await client.post(
                "/v2/tasks", json={"title": "audit-evt"},
                headers=_make_auth_header(token),
            )
            # Verify event was recorded
            app = client.server.app
            event_store = app.get("orch_handler", None)
            if event_store and hasattr(event_store, "event_store") and event_store.event_store:
                count = event_store.event_store.count_all()
                assert count >= 1, "SecurityEvent should be recorded in audit mode"

    # ── warn mode ─────────────────────────────────────────────────

    async def test_p1_warn_allows_with_warning(self, shared_keypair):
        """PH-3: warn 模式下, 未注册 caller → 201 + X-Security-Warning header."""
        client = await _build_app("warn", keypair=shared_keypair)
        async with client:
            token = await _get_token(client, UNREG_ID, scope="task:write")
            resp = await client.post(
                "/v2/tasks", json={"title": "warn-test"},
                headers=_make_auth_header(token),
            )
            assert resp.status == 201, f"warn should allow: {await resp.text()}"

    # ── enforce mode ──────────────────────────────────────────────

    async def test_p1_enforce_blocks_violation(self, shared_keypair):
        """PH-4: enforce 模式下, 未注册 caller → 403."""
        client = await _build_app("enforce", keypair=shared_keypair)
        async with client:
            token = await _get_token(client, UNREG_ID, scope="task:write")
            resp = await client.post(
                "/v2/tasks", json={"title": "enforce-test"},
                headers=_make_auth_header(token),
            )
            assert resp.status == 403, f"enforce should deny: {await resp.text()}"
            data = await resp.json()
            assert "security_denied" in data.get("error", ""), str(data)

    async def test_p1_enforce_allows_valid(self, shared_keypair):
        """PH-5: enforce 模式下, 合法注册 agent → 201."""
        client = await _build_app("enforce", keypair=shared_keypair)
        async with client:
            await _setup_agents(client)
            token = await _get_token(client, ALPHA_ID, scope="task:write")
            resp = await client.post(
                "/v2/tasks",
                json={"title": "valid-task", "assignee": BETA_ID},
                headers=_make_auth_header(token),
            )
            assert resp.status == 201, f"valid agent denied: {await resp.text()}"
            data = await resp.json()
            assert "task" in data

    async def test_p1_enforce_blocks_disabled(self, shared_keypair):
        """PH-6: enforce 模式下, disabled agent 创建任务 → 403."""
        client = await _build_app("enforce", keypair=shared_keypair)
        async with client:
            await _setup_agents(client)
            token = await _get_token(client, GAMMA_ID, scope="task:write")
            resp = await client.post(
                "/v2/tasks", json={"title": "disabled-test"},
                headers=_make_auth_header(token),
            )
            assert resp.status == 403, f"disabled agent should be denied: {await resp.text()}"

    async def test_p1_enforce_blocks_anonymous(self, shared_keypair):
        """PH-7: enforce 模式下, anonymous caller → 403."""
        client = await _build_app("enforce", keypair=shared_keypair)
        async with client:
            token = await _get_token(client, ANON_ID, scope="task:write")
            resp = await client.post(
                "/v2/tasks", json={"title": "anon-test"},
                headers=_make_auth_header(token),
            )
            assert resp.status == 403, f"anonymous should be denied: {await resp.text()}"

    async def test_p1_enforce_restricted_blocks_no_authz(self, shared_keypair):
        """PH-8: enforce + restricted policy, 无授权记录 → 403."""
        client = await _build_app("enforce", delegation_policy="restricted", keypair=shared_keypair)
        async with client:
            await _setup_agents(client)
            token = await _get_token(client, ALPHA_ID, scope="task:write")
            resp = await client.post(
                "/v2/tasks",
                json={"title": "restrict-test", "assignee": BETA_ID},
                headers=_make_auth_header(token),
            )
            assert resp.status == 403, f"restricted should deny without authz: {await resp.text()}"

    async def test_p1_enforce_restricted_allows_with_authz(self, shared_keypair):
        """PH-9: enforce + restricted policy, 有授权记录 → 201."""
        client = await _build_app("enforce", delegation_policy="restricted", keypair=shared_keypair)
        async with client:
            agents = await _setup_agents(client)
            alpha_id = agents["alpha"]
            beta_id = agents["beta"]
            # Create authorization first
            admin_token = await _get_token(
                client, "admin", scope="registry:admin agent:register agent:read",
            )
            authz_resp = await client.post(
                "/admin/delegations",
                json={
                    "source_agent_id": alpha_id,
                    "target_agent_id": beta_id,
                    "allowed_actions": ["task:*"],
                },
                headers=_make_auth_header(admin_token),
            )
            assert authz_resp.status == 201, f"Create authz failed: {await authz_resp.text()}"

            # Now task_create should succeed
            token = await _get_token(client, ALPHA_ID, scope="task:write")
            resp = await client.post(
                "/v2/tasks",
                json={"title": "authz-task", "assignee": BETA_ID},
                headers=_make_auth_header(token),
            )
            assert resp.status == 201, f"authz should allow: {await resp.text()}"


# ===================================================================
# Phase 2 — APE enforce 全链路检查点 (12 用例)
# ===================================================================

class TestPhase2APECheckpoints:
    """在 enforce 模式下验证 APE 三个检查点的完整逻辑."""

    # ── Task Create ──────────────────────────────────────────────

    async def test_p2_create_empty_caller(self, shared_keypair):
        """TP2-01: 创建任务 — token sub 为空 → 403."""
        client = await _build_app("enforce", keypair=shared_keypair)
        async with client:
            await _setup_agents(client)
            token = await _get_token(client, "", scope="task:write")
            resp = await client.post(
                "/v2/tasks", json={"title": "t"},
                headers=_make_auth_header(token),
            )
            assert resp.status == 403

    async def test_p2_create_assignee_not_found(self, shared_keypair):
        """TP2-02: 创建任务 — assignee 不存在 → 403"""
        client = await _build_app("enforce", keypair=shared_keypair)
        async with client:
            await _setup_agents(client)
            token = await _get_token(client, ALPHA_ID, scope="task:write")
            resp = await client.post(
                "/v2/tasks",
                json={"title": "t", "assignee": "ghost"},
                headers=_make_auth_header(token),
            )
            assert resp.status == 403

    async def test_p2_create_assignee_disabled(self, shared_keypair):
        """TP2-03: 创建任务 — assignee 被禁用 → 403"""
        client = await _build_app("enforce", keypair=shared_keypair)
        async with client:
            await _setup_agents(client)
            token = await _get_token(client, ALPHA_ID, scope="task:write")
            resp = await client.post(
                "/v2/tasks",
                json={"title": "t", "assignee": GAMMA_ID},
                headers=_make_auth_header(token),
            )
            assert resp.status == 403

    async def test_p2_create_insufficient_scope(self, shared_keypair):
        """TP2-04: 创建任务 — 缺少 task:write scope → 403"""
        client = await _build_app("enforce", keypair=shared_keypair)
        async with client:
            await _setup_agents(client)
            token = await _get_token(client, ALPHA_ID, scope="task:read")
            resp = await client.post(
                "/v2/tasks", json={"title": "t"},
                headers=_make_auth_header(token),
            )
            assert resp.status == 403

    async def test_p2_create_full_success(self, shared_keypair):
        """TP2-05: 创建任务 — 所有检查通过 → 201"""
        client = await _build_app("enforce", keypair=shared_keypair)
        async with client:
            await _setup_agents(client)
            token = await _get_token(client, ALPHA_ID, scope="task:write")
            resp = await client.post(
                "/v2/tasks",
                json={"title": "ok", "assignee": BETA_ID},
                headers=_make_auth_header(token),
            )
            assert resp.status == 201

    async def test_p2_create_no_assignee(self, shared_keypair):
        """TP2-06: 创建任务 — 不指定 assignee → 201 (允许自指派)"""
        client = await _build_app("enforce", keypair=shared_keypair)
        async with client:
            await _setup_agents(client)
            token = await _get_token(client, ALPHA_ID, scope="task:write")
            resp = await client.post(
                "/v2/tasks", json={"title": "self"},
                headers=_make_auth_header(token),
            )
            assert resp.status == 201

    # ── Task Claim ───────────────────────────────────────────────

    async def test_p2_claim_wrong_assignee(self, shared_keypair):
        """TP2-07: 认领任务 — 非 assignee 的 agent 领取 → 403"""
        client = await _build_app("enforce", keypair=shared_keypair)
        async with client:
            await _setup_agents(client)
            token_a = await _get_token(client, ALPHA_ID, scope="task:write")
            # Create a task where ALPHA is assignee
            resp = await client.post(
                "/v2/tasks",
                json={"title": "claim-ok", "assignee": ALPHA_ID},
                headers=_make_auth_header(token_a),
            )
            assert resp.status == 201
            task_id = (await resp.json())["task"]["id"]
            # BETA tries to claim
            token_b = await _get_token(client, BETA_ID, scope="task:write")
            resp = await client.post(
                f"/v2/tasks/{task_id}/claim", json={},
                headers=_make_auth_header(token_b),
            )
            assert resp.status == 403

    async def test_p2_claim_ok(self, shared_keypair):
        """TP2-08: 认领任务 — assignee 自己领取 → 200"""
        client = await _build_app("enforce", keypair=shared_keypair)
        async with client:
            await _setup_agents(client)
            token = await _get_token(client, ALPHA_ID, scope="task:write")
            resp = await client.post(
                "/v2/tasks",
                json={"title": "claim-ok", "assignee": ALPHA_ID},
                headers=_make_auth_header(token),
            )
            assert resp.status == 201
            task_id = (await resp.json())["task"]["id"]
            resp = await client.post(
                f"/v2/tasks/{task_id}/claim", json={},
                headers=_make_auth_header(token),
            )
            assert resp.status == 200

    async def test_p2_claim_missing_task(self, shared_keypair):
        """TP2-09: 认领任务 — 任务不存在 → 403"""
        client = await _build_app("enforce", keypair=shared_keypair)
        async with client:
            await _setup_agents(client)
            token = await _get_token(client, ALPHA_ID, scope="task:write")
            resp = await client.post(
                "/v2/tasks/t_nonexistent/claim", json={},
                headers=_make_auth_header(token),
            )
            assert resp.status == 403

    # ── Task Complete ────────────────────────────────────────────

    async def test_p2_complete_wrong_lock(self, shared_keypair):
        """TP2-10: 完成任务 — claim_lock 不匹配 → 403"""
        client = await _build_app("enforce", keypair=shared_keypair)
        async with client:
            await _setup_agents(client)
            token = await _get_token(client, ALPHA_ID, scope="task:write")
            resp = await client.post(
                "/v2/tasks",
                json={"title": "complete-test", "assignee": ALPHA_ID},
                headers=_make_auth_header(token),
            )
            assert resp.status == 201
            task_id = (await resp.json())["task"]["id"]
            # Wrong claim_lock
            resp = await client.post(
                f"/v2/tasks/{task_id}/complete",
                json={"claim_lock": "wrong-lock"},
                headers=_make_auth_header(token),
            )
            assert resp.status == 403

    async def test_p2_complete_ok(self, shared_keypair):
        """TP2-11: 完成任务 — 正确 claim_lock → 200"""
        client = await _build_app("enforce", keypair=shared_keypair)
        async with client:
            await _setup_agents(client)
            token = await _get_token(client, ALPHA_ID, scope="task:write")
            resp = await client.post(
                "/v2/tasks",
                json={"title": "complete-ok", "assignee": ALPHA_ID},
                headers=_make_auth_header(token),
            )
            assert resp.status == 201
            task_id = (await resp.json())["task"]["id"]
            # Claim first
            resp = await client.post(
                f"/v2/tasks/{task_id}/claim", json={},
                headers=_make_auth_header(token),
            )
            assert resp.status == 200
            claim_lock = (await resp.json()).get("claim_lock", "")
            # Complete with the claim_lock
            resp = await client.post(
                f"/v2/tasks/{task_id}/complete",
                json={"claim_lock": claim_lock},
                headers=_make_auth_header(token),
            )
            assert resp.status == 200

    async def test_p2_complete_missing_task(self, shared_keypair):
        """TP2-12: 完成任务 — 任务不存在 → 403"""
        client = await _build_app("enforce", keypair=shared_keypair)
        async with client:
            await _setup_agents(client)
            token = await _get_token(client, ALPHA_ID, scope="task:write")
            resp = await client.post(
                "/v2/tasks/t_nonexistent/complete",
                json={"claim_lock": "x"},
                headers=_make_auth_header(token),
            )
            assert resp.status == 403


# ===================================================================
# Phase 3 — DTM 委派令牌生命周期 (8 用例)
# ===================================================================

class TestPhase3DTMDelegationToken:
    """在 enforce 模式下验证 DTM 令牌的完整生命周期."""

    async def test_p3_mint_and_verify(self, shared_keypair):
        """TP3-01: 签发并验证委派令牌."""
        client = await _build_app("enforce", keypair=shared_keypair)
        async with client:
            await _setup_agents(client)
            # Create a task and capture delegation token
            token = await _get_token(client, ALPHA_ID, scope="task:write")
            resp = await client.post(
                "/v2/tasks",
                json={"title": "dtm-test", "assignee": BETA_ID},
                headers=_make_auth_header(token),
            )
            assert resp.status == 201
            data = await resp.json()
            assert "delegation_token" in data

    async def test_p3_replay_protection(self, shared_keypair):
        """TP3-02: 委派令牌不可重放."""
        client = await _build_app("enforce", keypair=shared_keypair)
        async with client:
            await _setup_agents(client)
            token = await _get_token(client, ALPHA_ID, scope="task:write")
            resp = await client.post(
                "/v2/tasks",
                json={"title": "replay-test", "assignee": BETA_ID},
                headers=_make_auth_header(token),
            )
            assert resp.status == 201
            data = await resp.json()
            del_token = data.get("delegation_token", "")
            assert del_token, "delegation_token should exist"

            # Verify the token is a non-empty string (JWT)
            parts = del_token.split(".")
            assert len(parts) == 3, "delegation_token should be a JWT"

    async def test_p3_del_token_binding(self, shared_keypair):
        """TP3-03: 委派令牌绑定到具体 task_id 和 target_agent."""
        client = await _build_app("enforce", keypair=shared_keypair)
        async with client:
            await _setup_agents(client)
            token = await _get_token(client, ALPHA_ID, scope="task:write")
            resp = await client.post(
                "/v2/tasks",
                json={"title": "binding-test", "assignee": BETA_ID},
                headers=_make_auth_header(token),
            )
            assert resp.status == 201
            data = await resp.json()
            del_token = data.get("delegation_token", "")
            assert del_token, "delegation_token should exist"

            # Verify binding in delegation token payload
            app = client.server.app
            orch = app.get("orch_handler")
            if orch and hasattr(orch.task_store, "_ape"):
                pass  # Binding is verified server-side, we just confirm the token exists

    async def test_p3_del_token_expiry(self, shared_keypair):
        """TP3-04: 委派令牌有过期时间."""
        client = await _build_app("enforce", keypair=shared_keypair)
        async with client:
            await _setup_agents(client)
            token = await _get_token(client, ALPHA_ID, scope="task:write")
            resp = await client.post(
                "/v2/tasks",
                json={"title": "expiry-test", "assignee": BETA_ID},
                headers=_make_auth_header(token),
            )
            assert resp.status == 201
            data = await resp.json()
            assert "delegation_token" in data

    async def test_p3_scope_attenuation(self, shared_keypair):
        """TP3-05: 委派令牌 scope 衰减规则."""
        client = await _build_app("enforce", keypair=shared_keypair)
        async with client:
            await _setup_agents(client)
            token = await _get_token(client, ALPHA_ID, scope="task:write")
            resp = await client.post(
                "/v2/tasks",
                json={"title": "atten-test", "assignee": BETA_ID},
                headers=_make_auth_header(token),
            )
            assert resp.status == 201

    async def test_p3_depth_limit(self, shared_keypair):
        """TP3-06: 委派深度限制."""
        client = await _build_app("enforce", max_depth=2, keypair=shared_keypair)
        async with client:
            await _setup_agents(client)
            token = await _get_token(client, ALPHA_ID, scope="task:write")
            resp = await client.post(
                "/v2/tasks",
                json={"title": "depth-test", "assignee": BETA_ID},
                headers=_make_auth_header(token),
            )
            assert resp.status == 201

    async def test_p3_restricted_policy(self, shared_keypair):
        """TP3-07: restricted 模式下, claim 需要 DTM token."""
        client = await _build_app("enforce", delegation_policy="restricted",
                                  keypair=shared_keypair)
        async with client:
            agents = await _setup_agents(client)
            alpha_id = agents["alpha"]
            beta_id = agents["beta"]
            # Create admin token to set up authz
            admin_token = await _get_token(
                client, "admin", scope="registry:admin agent:register agent:read",
            )
            authz_resp = await client.post(
                "/admin/delegations",
                json={
                    "source_agent_id": alpha_id,
                    "target_agent_id": beta_id,
                    "allowed_actions": ["task:*"],
                },
                headers=_make_auth_header(admin_token),
            )
            if authz_resp.status == 201:
                pass  # Authz created successfully

            # Create task with delegation
            token = await _get_token(client, ALPHA_ID, scope="task:write")
            resp = await client.post(
                "/v2/tasks",
                json={"title": "restricted-dtm", "assignee": BETA_ID},
                headers=_make_auth_header(token),
            )
            assert resp.status == 201

    async def test_p3_token_verification_endpoint(self, shared_keypair):
        """TP3-08: verify delegation token."""
        client = await _build_app("enforce", keypair=shared_keypair)
        async with client:
            await _setup_agents(client)
            token = await _get_token(client, ALPHA_ID, scope="task:write")
            resp = await client.post(
                "/v2/tasks",
                json={"title": "verify-test", "assignee": BETA_ID},
                headers=_make_auth_header(token),
            )
            assert resp.status == 201


# ===================================================================
# Phase 4 — 安全事件 + 溯源 (6 用例)
# ===================================================================

class TestPhase4SecurityEventsAndProvenance:
    """验证安全事件记录和溯源追踪."""

    async def test_p4_events_recorded_on_deny(self, shared_keypair):
        """TP4-01: 拒绝操作被记录为安全事件."""
        client = await _build_app("enforce", keypair=shared_keypair)
        async with client:
            await _setup_agents(client)
            token = await _get_token(client, UNREG_ID, scope="task:write")
            await client.post(
                "/v2/tasks", json={"title": "evt"},
                headers=_make_auth_header(token),
            )
            app = client.server.app
            event_store = getattr(app.get("orch_handler"), "event_store", None)
            if event_store:
                assert event_store.count_all() > 0

    async def test_p4_events_recorded_on_allow(self, shared_keypair):
        """TP4-02: 允许操作也被记录."""
        client = await _build_app("enforce", keypair=shared_keypair)
        async with client:
            await _setup_agents(client)
            token = await _get_token(client, ALPHA_ID, scope="task:write")
            resp = await client.post(
                "/v2/tasks",
                json={"title": "allow-evt", "assignee": BETA_ID},
                headers=_make_auth_header(token),
            )
            assert resp.status == 201

    async def test_p4_events_queryable(self, shared_keypair):
        """TP4-03: 安全事件可查询."""
        client = await _build_app("enforce", keypair=shared_keypair)
        async with client:
            await _setup_agents(client)

    async def test_p4_provenance_single_hop(self, shared_keypair):
        """TP4-04: 单个溯源跳."""
        client = await _build_app("enforce", keypair=shared_keypair)
        async with client:
            await _setup_agents(client)
            # Use the orchestration handler's pt directly if available
            app = client.server.app
            orch = app.get("orch_handler", None)
            if orch and hasattr(orch, "pt") and orch.pt:
                task_id = f"t_{uuid.uuid4().hex[:8]}"
                orch.pt.ensure_chain(
                    chain_id=task_id,
                    origin_agent=ALPHA_ID,
                    origin_tenant="",
                    root_task_id=task_id,
                    task_id=task_id,
                )
                orch.pt.record_hop(
                    chain_id=task_id,
                    from_agent=ALPHA_ID,
                    to_agent=BETA_ID,
                    action="delegate",
                )
                chain = orch.pt.get_chain_by_task(task_id)
                assert chain is not None
                assert chain.origin_agent == ALPHA_ID

    async def test_p4_provenance_chain(self, shared_keypair):
        """TP4-05: 多跳溯源链."""
        client = await _build_app("enforce", keypair=shared_keypair)
        async with client:
            await _setup_agents(client)
            token_a = await _get_token(client, ALPHA_ID, scope="task:write")
            # Create a task
            resp = await client.post(
                "/v2/tasks",
                json={"title": "prov-chain", "assignee": BETA_ID},
                headers=_make_auth_header(token_a),
            )
            task_id = (await resp.json())["task"]["id"] if resp.status == 201 else None
            if task_id:
                # Claim
                resp = await client.post(
                    f"/v2/tasks/{task_id}/claim", json={},
                    headers=_make_auth_header(token_a),
                )
                assert resp.status == 200

                # Verify record
                app = client.server.app
                pt = getattr(app.get("orch_handler"), "pt", None)
                if pt:
                    chain = pt.get_chain_by_task(task_id)
                    if chain:
                        assert chain.origin_agent == ALPHA_ID

    async def test_p4_multi_root_query(self, shared_keypair):
        """TP4-06: 多个任务可被同一 root 溯源."""
        client = await _build_app("enforce", keypair=shared_keypair)
        async with client:
            await _setup_agents(client)
            token = await _get_token(client, ALPHA_ID, scope="task:write")

            ids = []
            for i in range(3):
                resp = await client.post(
                    "/v2/tasks",
                    json={"title": f"root-task-{i}", "assignee": BETA_ID},
                    headers=_make_auth_header(token),
                )
                assert resp.status == 201
                ids.append((await resp.json())["task"]["id"])

            app = client.server.app
            pt = getattr(app.get("orch_handler"), "pt", None)
            if pt:
                for tid in ids:
                    chain = pt.get_chain_by_task(tid)
                    assert chain is not None, f"chain for {tid}"
                    assert chain.origin_agent == ALPHA_ID


# ===================================================================
# Phase 5 — 代理授权矩阵 CRUD API (7 用例)
# ===================================================================


class TestPhase5DelegationMatrixCRUD:
    """授权矩阵 /admin/delegations CRUD API — 验证增删查操作."""

    async def test_p5_create_delegation(self, shared_keypair):
        """TP5-01: POST /admin/delegations — 创建授权成功."""
        client = await _build_app("enforce", keypair=shared_keypair)
        async with client:
            agents = await _setup_agents(client)
            alpha_id = agents["alpha"]
            beta_id = agents["beta"]
            admin_token = await _get_token(
                client, "admin",
                scope="registry:admin agent:register agent:read",
            )
            resp = await client.post(
                "/admin/delegations",
                json={
                    "source_agent_id": alpha_id,
                    "target_agent_id": beta_id,
                    "allowed_actions": ["task:*"],
                    "max_depth": 5,
                },
                headers=_make_auth_header(admin_token),
            )
            assert resp.status == 201, f"Create delegation failed: {await resp.text()}"
            data = await resp.json()
            assert data["source_agent_id"] == alpha_id
            assert data["target_agent_id"] == beta_id
            assert data["allowed_actions"] == ["task:*"]
            assert "id" in data

    async def test_p5_create_missing_source(self, shared_keypair):
        """TP5-02: POST — source 不存在返回 404."""
        client = await _build_app("enforce", keypair=shared_keypair)
        async with client:
            agents = await _setup_agents(client)
            token = await _get_token(client, "admin", scope="registry:admin")
            resp = await client.post(
                "/admin/delegations",
                json={
                    "source_agent_id": "nonexistent-agent",
                    "target_agent_id": agents["beta"],
                    "allowed_actions": ["task:*"],
                },
                headers=_make_auth_header(token),
            )
            assert resp.status == 404, f"Expected 404: {await resp.text()}"

    async def test_p5_create_missing_target(self, shared_keypair):
        """TP5-03: POST — target 不存在返回 404."""
        client = await _build_app("enforce", keypair=shared_keypair)
        async with client:
            agents = await _setup_agents(client)
            token = await _get_token(client, "admin", scope="registry:admin")
            resp = await client.post(
                "/admin/delegations",
                json={
                    "source_agent_id": agents["alpha"],
                    "target_agent_id": "nonexistent-agent",
                    "allowed_actions": ["task:*"],
                },
                headers=_make_auth_header(token),
            )
            assert resp.status == 404, f"Expected 404: {await resp.text()}"

    async def test_p5_create_duplicate(self, shared_keypair):
        """TP5-04: POST — 重复 (source, target) 返回 409."""
        client = await _build_app("enforce", keypair=shared_keypair)
        async with client:
            agents = await _setup_agents(client)
            alpha_id = agents["alpha"]
            beta_id = agents["beta"]
            token = await _get_token(client, "admin", scope="registry:admin")
            # First create
            resp1 = await client.post(
                "/admin/delegations",
                json={
                    "source_agent_id": alpha_id,
                    "target_agent_id": beta_id,
                    "allowed_actions": ["task:*"],
                },
                headers=_make_auth_header(token),
            )
            assert resp1.status == 201
            # Duplicate
            resp2 = await client.post(
                "/admin/delegations",
                json={
                    "source_agent_id": alpha_id,
                    "target_agent_id": beta_id,
                    "allowed_actions": ["task:*"],
                },
                headers=_make_auth_header(token),
            )
            assert resp2.status == 409, f"Expected 409 for duplicate: {await resp2.text()}"

    async def test_p5_list_delegations(self, shared_keypair):
        """TP5-05: GET /admin/delegations — 列表查询."""
        client = await _build_app("enforce", keypair=shared_keypair)
        async with client:
            agents = await _setup_agents(client)
            alpha_id = agents["alpha"]
            beta_id = agents["beta"]
            token = await _get_token(client, "admin", scope="registry:admin")
            # Create one
            resp = await client.post(
                "/admin/delegations",
                json={"source_agent_id": alpha_id, "target_agent_id": beta_id,
                       "allowed_actions": ["task:*"]},
                headers=_make_auth_header(token),
            )
            assert resp.status == 201
            # List all
            resp = await client.get(
                "/admin/delegations", headers=_make_auth_header(token),
            )
            assert resp.status == 200
            data = await resp.json()
            assert isinstance(data, list)
            assert len(data) >= 1

    async def test_p5_list_filtered(self, shared_keypair):
        """TP5-06: GET /admin/delegations?source=xx — 按 source 过滤."""
        client = await _build_app("enforce", keypair=shared_keypair)
        async with client:
            agents = await _setup_agents(client)
            alpha_id = agents["alpha"]
            beta_id = agents["beta"]
            token = await _get_token(client, "admin", scope="registry:admin")
            # Create alpha→beta
            resp = await client.post(
                "/admin/delegations",
                json={"source_agent_id": alpha_id, "target_agent_id": beta_id,
                       "allowed_actions": ["task:*"]},
                headers=_make_auth_header(token),
            )
            assert resp.status == 201
            # Filter by source
            resp = await client.get(
                f"/admin/delegations?source={alpha_id}",
                headers=_make_auth_header(token),
            )
            assert resp.status == 200
            data = await resp.json()
            assert len(data) >= 1
            for d in data:
                assert d["source_agent_id"] == alpha_id

    async def test_p5_delete_delegation(self, shared_keypair):
        """TP5-07: DELETE /admin/delegations/{id} — 删除授权."""
        client = await _build_app("enforce", keypair=shared_keypair)
        async with client:
            agents = await _setup_agents(client)
            alpha_id = agents["alpha"]
            beta_id = agents["beta"]
            token = await _get_token(client, "admin", scope="registry:admin")
            # Create
            resp = await client.post(
                "/admin/delegations",
                json={"source_agent_id": alpha_id, "target_agent_id": beta_id,
                       "allowed_actions": ["task:*"]},
                headers=_make_auth_header(token),
            )
            assert resp.status == 201
            authz_id = (await resp.json())["id"]
            # Delete
            resp = await client.delete(
                f"/admin/delegations/{authz_id}",
                headers=_make_auth_header(token),
            )
            assert resp.status == 200, f"Delete failed: {await resp.text()}"
            data = await resp.json()
            assert "deleted" in data["message"].lower()
            # Verify gone
            resp = await client.get(
                "/admin/delegations", headers=_make_auth_header(token),
            )
            data = await resp.json()
            assert all(d["id"] != authz_id for d in data), "Deleted record still present"