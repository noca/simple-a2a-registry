"""P6-A5-3: token scope 仅限本租户生效的权限测试

验证 JWT token 中的 tenant 声明决定 API 请求的可见/操作范围：

覆盖场景:
  1. Token scoped to Tenant A — 仅能列出 Tenant A 的 agent
  2. Token scoped to Tenant A — 不能看到 Tenant B 的 agent
  3. Token scoped to Tenant A — 不能访问 Tenant B 的 agent 详情
  4. Token 的 tenant claim 比 X-Tenant-ID header 更权威（防 spoofing）
  5. Token 的 tenant claim 比 ?tenant= query 参数更权威（防 spoofing）
  6. Token scoped to Tenant A — 注册的 agent 自动属于 Tenant A
  7. Token scoped to Tenant A — 不能操作 Tenant B 的 agent（heartbeat/unregister）
  8. Token 无 tenant 字段 — 向后兼容，能看到所有 agent（admin 范围）
  9. Token scoped to Tenant A — token 中不包含非本租户的 client 信息
"""
from __future__ import annotations

import json
import tempfile
import time
import uuid

import pytest
from aiohttp.test_utils import TestServer, TestClient

from simple_a2a_registry.server import create_app
from simple_a2a_registry.store import Store
from simple_a2a_registry.auth import (
    create_token,
    SCOPES,
    ISSUER,
    TOKEN_EXPIRY_SECONDS,
)

pytestmark = pytest.mark.asyncio

TENANT_A = "acme-corp"
TENANT_B = "globex-inc"
TENANT_DEFAULT = ""


# ======================================================================
# Fixtures
# ======================================================================


@pytest.fixture(scope="module")
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def app_with_auth():
    """创建启用了 OAuth 认证的测试 app。

    create_app(auth_enabled=True) 内部生成 RS256 key pair，
    测试函数通过 app["auth_handler"].private_key 获取私钥用于签发 token。
    """
    tmpdir_obj = tempfile.TemporaryDirectory()
    data_dir = tmpdir_obj.name

    app = create_app(
        data_dir=data_dir,
        base_url="http://localhost:8321",
        auth_enabled=True,
    )
    store: Store = app["store"]
    auth_handler = app["auth_handler"]

    server = TestServer(app)
    await server.start_server()
    client = TestClient(server)

    # 注册两个测试 client — 分别属于不同租户，以及一个无 tenant 的 client
    client_a_info = store.register_client(
        agent_card_id="agent-a",
        allowed_scopes=list(SCOPES.keys()),
        description="Tenant A client",
        tenant=TENANT_A,
    )
    client_b_info = store.register_client(
        agent_card_id="agent-b",
        allowed_scopes=list(SCOPES.keys()),
        description="Tenant B client",
        tenant=TENANT_B,
    )
    client_default_info = store.register_client(
        agent_card_id="agent-default",
        allowed_scopes=list(SCOPES.keys()),
        description="Default tenant client",
        tenant="",
    )

    result = {
        "client": client,
        "store": store,
        "auth_handler": auth_handler,
        "private_key": auth_handler.private_key,
        "client_a": client_a_info,
        "client_b": client_b_info,
        "client_default": client_default_info,
        "tmpdir": tmpdir_obj,
    }

    yield result

    await client.close()
    await server.close()
    tmpdir_obj.cleanup()


# ======================================================================
# Helper functions
# ======================================================================


def _make_token(
    sub: str,
    private_key: str,
    *,
    tenant: str | None = None,
    scope: str | None = None,
) -> str:
    """签发 JWT token。

    Args:
        sub: 主题（client_id）
        private_key: RSA 私钥 PEM
        tenant: None=不包含 tenant 字段，非空字符串=写入 tenant claim
        scope: 可选的 scope 字符串
    """
    return create_token(
        sub=sub,
        private_key=private_key,
        algorithm="RS256",
        scope=scope or " ".join(SCOPES.keys()),
        tenant=tenant if tenant else None,
    )


def _agent_card(name: str, *, tenant: str = "") -> dict:
    """构建 AgentCard dict。"""
    return {
        "name": name,
        "description": f"Agent {name} for tenant {tenant!r}",
        "interfaces": [{
            "url": f"https://agent.{name.lower()}.example.com",
            "protocol_binding": "JSONRPC",
            "protocol_version": "1.0",
        }],
    }


async def _register_with_token(
    client: TestClient, token: str, name: str, *, tenant_in_body: str = ""
) -> dict:
    """使用 Bearer token 注册 agent。

    注册时 body 中可以含 tenant 字段（模拟同 token client 注册同一租户）。
    但若 token 已有 tenant claim，server 应优先使用 token 的 tenant。
    """
    card = _agent_card(name)
    if tenant_in_body:
        card["tenant"] = tenant_in_body

    resp = await client.post(
        "/v1/agents",
        json=card,
        headers={"Authorization": f"Bearer {token}"},
    )
    return {"status": resp.status, "body": await resp.json()}


async def _list_with_token(
    client: TestClient, token: str, *, tenant_param: str | None = None,
    spoof_header: str | None = None,
) -> dict:
    """使用 Bearer token 列出 agent。

    Args:
        token: Bearer token
        tenant_param: ?tenant= 查询参数
        spoof_header: X-Tenant-ID header（模拟跨租户 spoofing）
    """
    params = {}
    if tenant_param is not None:
        params["tenant"] = tenant_param

    headers = {"Authorization": f"Bearer {token}"}
    if spoof_header is not None:
        headers["X-Tenant-ID"] = spoof_header

    resp = await client.get("/v1/agents", params=params, headers=headers)
    return {"status": resp.status, "body": await resp.json()}


async def _get_agent_with_token(
    client: TestClient, token: str, agent_id: str,
    *, spoof_header: str | None = None,
) -> dict:
    """使用 Bearer token 获取 agent 详情。"""
    headers = {"Authorization": f"Bearer {token}"}
    if spoof_header is not None:
        headers["X-Tenant-ID"] = spoof_header

    resp = await client.get(f"/v1/agents/{agent_id}", headers=headers)
    return {"status": resp.status, "body": await resp.json()}


async def _heartbeat_with_token(
    client: TestClient, token: str, agent_id: str,
) -> dict:
    """使用 Bearer token 发送 heartbeat。"""
    resp = await client.post(
        f"/v1/agents/{agent_id}/heartbeat",
        headers={"Authorization": f"Bearer {token}"},
    )
    return {"status": resp.status, "body": await resp.json()}


async def _unregister_with_token(
    client: TestClient, token: str, agent_id: str,
) -> dict:
    """使用 Bearer token 删除 agent。"""
    resp = await client.delete(
        f"/v1/agents/{agent_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    return {"status": resp.status, "body": await resp.json()}


# ======================================================================
# Tests
# ======================================================================


class TestTokenScopedToListTenantAgents:
    """Token scoped to Tenant A 仅能列出 Tenant A 的 agent。"""

    async def _setup_two_tenants(self, ctx):
        """Helper: 为两个租户各注册一个 agent。"""
        token_a = _make_token(
            sub=ctx["client_a"]["client_id"],
            private_key=ctx["private_key"],
            tenant=TENANT_A,
            scope="agent:register agent:read",
        )
        token_b = _make_token(
            sub=ctx["client_b"]["client_id"],
            private_key=ctx["private_key"],
            tenant=TENANT_B,
            scope="agent:register agent:read",
        )

        reg_a = await _register_with_token(
            ctx["client"], token_a, "AgentA",
        )
        assert reg_a["status"] == 201, f"注册 AgentA 失败: {reg_a['body']}"
        aid_a = reg_a["body"]["id"]

        reg_b = await _register_with_token(
            ctx["client"], token_b, "AgentB",
        )
        assert reg_b["status"] == 201, f"注册 AgentB 失败: {reg_b['body']}"
        aid_b = reg_b["body"]["id"]

        return token_a, token_b, aid_a, aid_b

    async def test_token_a_sees_own_agents(self, app_with_auth):
        """Token A 列出 agent → 仅能看到 Tenant A 的 agent。"""
        ctx = app_with_auth
        token_a, token_b, aid_a, aid_b = await self._setup_two_tenants(ctx)

        # Token A 列出 — 应只看到 AgentA
        result = await _list_with_token(ctx["client"], token_a)
        assert result["status"] == 200
        names = [a["name"] for a in result["body"]["agents"]]
        assert len(names) == 1, (
            f"Token A 应只看到 1 个 agent（自己的），实际看到 {len(names)}: {names}"
        )
        assert "AgentA" in names
        assert "AgentB" not in names, (
            "Token A 不应看到 Tenant B 的 agent"
        )

    async def test_token_b_sees_own_agents(self, app_with_auth):
        """Token B 列出 agent → 仅能看到 Tenant B 的 agent。"""
        ctx = app_with_auth
        token_a, token_b, aid_a, aid_b = await self._setup_two_tenants(ctx)

        result = await _list_with_token(ctx["client"], token_b)
        assert result["status"] == 200
        names = [a["name"] for a in result["body"]["agents"]]
        assert len(names) == 1, (
            f"Token B 应只看到 1 个 agent（自己的），实际看到 {len(names)}: {names}"
        )
        assert "AgentB" in names
        assert "AgentA" not in names, (
            "Token B 不应看到 Tenant A 的 agent"
        )

    async def test_list_one_tenant_when_both_have_many(self, app_with_auth):
        """多 agent 场景 — Token A 只看到本租户的 3 个 agent。"""
        ctx = app_with_auth
        token_a = _make_token(
            sub=ctx["client_a"]["client_id"],
            private_key=ctx["private_key"],
            tenant=TENANT_A,
            scope="agent:register agent:read",
        )
        token_b = _make_token(
            sub=ctx["client_b"]["client_id"],
            private_key=ctx["private_key"],
            tenant=TENANT_B,
            scope="agent:register agent:read",
        )

        # Tenant A 注册 3 个 agent
        for i in range(3):
            r = await _register_with_token(
                ctx["client"], token_a, f"AgentA-{i}",
            )
            assert r["status"] == 201

        # Tenant B 注册 2 个 agent
        for i in range(2):
            r = await _register_with_token(
                ctx["client"], token_b, f"AgentB-{i}",
            )
            assert r["status"] == 201

        # Token A 应只看到 3 个
        result = await _list_with_token(ctx["client"], token_a)
        assert result["status"] == 200
        assert len(result["body"]["agents"]) == 3, (
            f"Token A 应看到 3 个 agent，实际 {len(result['body']['agents'])}"
        )
        for a in result["body"]["agents"]:
            assert "AgentA" in a["name"]

    async def test_no_agents_for_tenant_returns_empty(self, app_with_auth):
        """Token 租户下无 agent → 返回空列表，不是 404。"""
        ctx = app_with_auth
        token_a = _make_token(
            sub=ctx["client_a"]["client_id"],
            private_key=ctx["private_key"],
            tenant=TENANT_A,
        )

        result = await _list_with_token(ctx["client"], token_a)
        assert result["status"] == 200
        assert result["body"]["agents"] == []
        assert result["body"]["total"] == 0


class TestCrossTenantDetailIsolation:
    """Token scoped to Tenant A 不能访问 Tenant B 的 agent 详情。"""

    async def _setup_one_each(self, ctx):
        """Helper: 为 Tenant A 和 Tenant B 各注册一个 agent。"""
        token_a = _make_token(
            sub=ctx["client_a"]["client_id"],
            private_key=ctx["private_key"],
            tenant=TENANT_A,
            scope="agent:register agent:read",
        )
        token_b = _make_token(
            sub=ctx["client_b"]["client_id"],
            private_key=ctx["private_key"],
            tenant=TENANT_B,
            scope="agent:register agent:read",
        )

        reg_a = await _register_with_token(
            ctx["client"], token_a, "SecretA-Info",
        )
        aid_a = reg_a["body"]["id"]

        reg_b = await _register_with_token(
            ctx["client"], token_b, "SecretB-Info",
        )
        aid_b = reg_b["body"]["id"]

        return token_a, token_b, aid_a, aid_b

    async def test_token_a_can_get_own_agent(self, app_with_auth):
        """Token A 可以获取自己租户的 agent 详情。"""
        ctx = app_with_auth
        token_a, token_b, aid_a, aid_b = await self._setup_one_each(ctx)

        result = await _get_agent_with_token(ctx["client"], token_a, aid_a)
        assert result["status"] == 200
        assert result["body"]["name"] == "SecretA-Info"

    async def test_token_a_cannot_get_tenant_b_agent(self, app_with_auth):
        """Token A 试图获取 Tenant B 的 agent 详情 → 403/404。"""
        ctx = app_with_auth
        token_a, token_b, aid_a, aid_b = await self._setup_one_each(ctx)

        result = await _get_agent_with_token(ctx["client"], token_a, aid_b)
        assert result["status"] in (403, 404), (
            f"Token A 获取 Tenant B 的 agent 应返回 403/404，"
            f"实际 {result['status']}"
        )

    async def test_token_b_cannot_get_tenant_a_agent(self, app_with_auth):
        """Token B 试图获取 Tenant A 的 agent 详情 → 403/404。"""
        ctx = app_with_auth
        token_a, token_b, aid_a, aid_b = await self._setup_one_each(ctx)

        result = await _get_agent_with_token(ctx["client"], token_b, aid_a)
        assert result["status"] in (403, 404), (
            f"Token B 获取 Tenant A 的 agent 应返回 403/404，"
            f"实际 {result['status']}"
        )

    async def test_detail_404_for_nonexistent_agent(self, app_with_auth):
        """不存在的 agent — 即使是本租户 tenant 也应返回 404。"""
        ctx = app_with_auth
        token_a = _make_token(
            sub=ctx["client_a"]["client_id"],
            private_key=ctx["private_key"],
            tenant=TENANT_A,
        )

        result = await _get_agent_with_token(
            ctx["client"], token_a, "nonexistent-agent-id",
        )
        assert result["status"] == 404


class TestTokenTenantOverridesHeader:
    """Token 的 tenant claim 比 X-Tenant-ID header 更权威（防 spoofing）。"""

    async def test_token_tenant_wins_over_spoofed_header(self, app_with_auth):
        """即使请求携带 X-Tenant-ID: globex-inc，Token A 仍只看到本租户 agent。"""
        ctx = app_with_auth

        # 注册两个租户的 agent —— 使用 store 直接注册确保测试独立
        store = ctx["store"]
        store.register_agent(_agent_card("AcmeAgent"), tenant=TENANT_A)
        store.register_agent(_agent_card("GlobexAgent"), tenant=TENANT_B)

        token_a = _make_token(
            sub=ctx["client_a"]["client_id"],
            private_key=ctx["private_key"],
            tenant=TENANT_A,
        )

        # 尝试用错误 header spoof
        result = await _list_with_token(
            ctx["client"], token_a,
            spoof_header=TENANT_B,  # X-Tenant-ID: globex-inc
        )
        assert result["status"] == 200
        names = [a["name"] for a in result["body"]["agents"]]
        assert "AcmeAgent" in names
        assert "GlobexAgent" not in names, (
            "Token A 即使收到 X-Tenant-ID=B 也不应看到 Tenant B 的 agent"
        )

    async def test_token_tenant_wins_over_spoofed_header_detail(self, app_with_auth):
        """即使 X-Tenant-ID spoofing，Token A 仍不能获取 Tenant B 的 agent 详情。"""
        ctx = app_with_auth
        store = ctx["store"]
        aid_a = store.register_agent(
            _agent_card("AcmePrivate"), tenant=TENANT_A,
        )
        aid_b = store.register_agent(
            _agent_card("GlobexPrivate"), tenant=TENANT_B,
        )

        token_a = _make_token(
            sub=ctx["client_a"]["client_id"],
            private_key=ctx["private_key"],
            tenant=TENANT_A,
        )

        # 获取自己租户的 — 应该可以
        result_own = await _get_agent_with_token(
            ctx["client"], token_a, aid_a,
            spoof_header=TENANT_B,  # 即使带了错误 header
        )
        assert result_own["status"] == 200, (
            f"Token A 获取自己租户的 agent 应成功，即使 header spoofing: "
            f"{result_own['status']}"
        )

        # 获取其他租户的 — 应被拒绝
        result_cross = await _get_agent_with_token(
            ctx["client"], token_a, aid_b,
            spoof_header=TENANT_B,
        )
        assert result_cross["status"] in (403, 404), (
            f"Token A 即使 header spoofing 也不应获取 Tenant B 的 agent: "
            f"{result_cross['status']}"
        )


class TestTokenTenantOverridesQueryParam:
    """Token 的 tenant claim 比 ?tenant= 查询参数更权威。"""

    async def test_token_tenant_wins_over_query_param(self, app_with_auth):
        """即使 ?tenant=globex-inc，Token A 仍只看到本租户 agent。"""
        ctx = app_with_auth
        store = ctx["store"]
        store.register_agent(_agent_card("AcmeBot"), tenant=TENANT_A)
        store.register_agent(_agent_card("GlobexBot"), tenant=TENANT_B)

        token_a = _make_token(
            sub=ctx["client_a"]["client_id"],
            private_key=ctx["private_key"],
            tenant=TENANT_A,
        )

        # 尝试用 ?tenant=globex-inc 参数 spoof
        result = await _list_with_token(
            ctx["client"], token_a,
            tenant_param=TENANT_B,
        )
        assert result["status"] == 200
        names = [a["name"] for a in result["body"]["agents"]]
        assert "AcmeBot" in names, (
            "Token A 即使 ?tenant=B 也应看到自己的 AcmeBot"
        )
        assert "GlobexBot" not in names, (
            "Token A 即使 ?tenant=B 也不应看到 Tenant B 的 agent"
        )


class TestRegistrationScopedByTokenTenant:
    """Token scoped to Tenant A 注册的 agent 自动属于 Tenant A。"""

    async def test_register_with_token_a_creates_tenant_a_agent(self, app_with_auth):
        """用 Token A 注册 agent → agent 属于 Tenant A。"""
        ctx = app_with_auth
        token_a = _make_token(
            sub=ctx["client_a"]["client_id"],
            private_key=ctx["private_key"],
            tenant=TENANT_A,
            scope="agent:register",
        )

        reg = await _register_with_token(ctx["client"], token_a, "NewAcmeBot")
        assert reg["status"] == 201, f"注册失败: {reg['body']}"
        agent_id = reg["body"]["id"]

        # 验证 DB 中 tenant_id 是 Tenant A
        db_card = ctx["store"].get_agent(agent_id)
        assert db_card is not None
        with ctx["store"]._tx("DEFERRED") as engine:
            row = engine.execute(
                "SELECT tenant_id FROM agents WHERE id=?", (agent_id,),
            ).fetchone()
        assert row is not None
        assert row["tenant_id"] == TENANT_A, (
            f"DB 中 agent 的 tenant_id 应为 {TENANT_A!r}，"
            f"实际为 {row['tenant_id']!r}"
        )

    async def test_register_token_body_tenant_overridden_by_token(self, app_with_auth):
        """即使 body 中指定其他 tenant，Token A 的 tenant 仍优先。"""
        ctx = app_with_auth
        token_a = _make_token(
            sub=ctx["client_a"]["client_id"],
            private_key=ctx["private_key"],
            tenant=TENANT_A,
            scope="agent:register",
        )

        # body 中写 tenant=B — 但 token 中有 tenant=A，应使用 token 的
        reg = await _register_with_token(
            ctx["client"], token_a, "OverriddenBot",
            tenant_in_body=TENANT_B,
        )
        assert reg["status"] == 201
        agent_id = reg["body"]["id"]

        with ctx["store"]._tx("DEFERRED") as engine:
            row = engine.execute(
                "SELECT tenant_id FROM agents WHERE id=?", (agent_id,),
            ).fetchone()
        assert row is not None
        assert row["tenant_id"] == TENANT_A, (
            f"Token 的 tenant claim 应覆盖 body 中的 tenant 字段: "
            f"期望 {TENANT_A!r}，实际 {row['tenant_id']!r}"
        )

    async def test_register_without_token_tenant_uses_body(self, app_with_auth):
        """Token 无 tenant 字段 → agent 的 tenant 取自 body。"""
        ctx = app_with_auth
        token_default = _make_token(
            sub=ctx["client_default"]["client_id"],
            private_key=ctx["private_key"],
            # tenant=None — 无 tenant 字段
        )

        reg = await _register_with_token(
            ctx["client"], token_default, "BodyTenantBot",
            tenant_in_body=TENANT_A,
        )
        assert reg["status"] == 201
        agent_id = reg["body"]["id"]

        with ctx["store"]._tx("DEFERRED") as engine:
            row = engine.execute(
                "SELECT tenant_id FROM agents WHERE id=?", (agent_id,),
            ).fetchone()
        assert row is not None
        assert row["tenant_id"] == TENANT_A, (
            f"Token 无 tenant 时 agent 应从 body 获取 tenant: "
            f"期望 {TENANT_A!r}，实际 {row['tenant_id']!r}"
        )


class TestCrossTenantActionIsolation:
    """Token scoped to Tenant A 不能操作 Tenant B 的 agent。"""

    async def test_cannot_heartbeat_other_tenant_agent(self, app_with_auth):
        """Token A 不能给 Tenant B 的 agent 发送 heartbeat。"""
        ctx = app_with_auth

        # 用 store 直接注册两个 agent
        store = ctx["store"]
        aid_a = store.register_agent(_agent_card("AcmeAlive"), tenant=TENANT_A)
        aid_b = store.register_agent(_agent_card("GlobexAlive"), tenant=TENANT_B)

        token_a = _make_token(
            sub=ctx["client_a"]["client_id"],
            private_key=ctx["private_key"],
            tenant=TENANT_A,
            scope="agent:read agent:register",
        )

        # 给自己的 agent heartbeat — 正常
        result_own = await _heartbeat_with_token(
            ctx["client"], token_a, aid_a,
        )
        assert result_own["status"] == 203, (
            f"自己的 agent heartbeat 应返回 203: {result_own['status']}"
        )

        # 给 Tenant B 的 agent heartbeat — 应失败
        result_cross = await _heartbeat_with_token(
            ctx["client"], token_a, aid_b,
        )
        assert result_cross["status"] in (403, 404), (
            f"跨租户 heartbeat 应返回 403/404: {result_cross['status']}"
        )

    async def test_cannot_unregister_other_tenant_agent(self, app_with_auth):
        """Token A 不能删除 Tenant B 的 agent。"""
        ctx = app_with_auth
        store = ctx["store"]
        aid_a = store.register_agent(
            _agent_card("AcmeDelete"), tenant=TENANT_A,
        )
        aid_b = store.register_agent(
            _agent_card("GlobexDelete"), tenant=TENANT_B,
        )

        token_a = _make_token(
            sub=ctx["client_a"]["client_id"],
            private_key=ctx["private_key"],
            tenant=TENANT_A,
            scope="agent:admin agent:register",
        )

        # 删除 Tenant B 的 agent — 应失败
        result_cross = await _unregister_with_token(
            ctx["client"], token_a, aid_b,
        )
        assert result_cross["status"] in (403, 404), (
            f"跨租户删除 agent 应返回 403/404: {result_cross['status']}"
        )

        # Tenant B 的 agent 应仍然存在
        card = store.get_agent(aid_b)
        assert card is not None, "Tenant B 的 agent 不应被删除"

    async def test_token_without_tenant_can_see_all(self, app_with_auth):
        """无 tenant 字段的 token → 能看见所有 agent（向后兼容）。"""
        ctx = app_with_auth
        store = ctx["store"]
        store.register_agent(_agent_card("AgentA"), tenant=TENANT_A)
        store.register_agent(_agent_card("AgentB"), tenant=TENANT_B)
        store.register_agent(_agent_card("AgentDefault"), tenant="")

        token_default = _make_token(
            sub=ctx["client_default"]["client_id"],
            private_key=ctx["private_key"],
            # tenant=None — 无 tenant 字段
        )

        # 无 tenant 的 token 应能看到所有 3 个 agent（向后兼容）
        result = await _list_with_token(ctx["client"], token_default)
        assert result["status"] == 200
        names = [a["name"] for a in result["body"]["agents"]]
        assert len(names) == 3, (
            f"无 tenant 的 token 应看到所有 3 个 agent，实际 {len(names)}: {names}"
        )


class TestTokenScopeConsistency:
    """Token scope 一致性与边界场景。"""

    async def test_token_tenant_registered_agent_seen_by_own_token(self, app_with_auth):
        """Token A 注册的 agent → Token A 能在列表和详情中看见。"""
        ctx = app_with_auth

        token_a = _make_token(
            sub=ctx["client_a"]["client_id"],
            private_key=ctx["private_key"],
            tenant=TENANT_A,
            scope="agent:register agent:read",
        )

        # 注册
        reg = await _register_with_token(
            ctx["client"], token_a, "ConsistencyBot",
        )
        assert reg["status"] == 201
        agent_id = reg["body"]["id"]

        # 列表可见
        list_result = await _list_with_token(ctx["client"], token_a)
        names = [a["name"] for a in list_result["body"]["agents"]]
        assert "ConsistencyBot" in names

        # 详情可见
        detail_result = await _get_agent_with_token(
            ctx["client"], token_a, agent_id,
        )
        assert detail_result["status"] == 200
        assert detail_result["body"]["name"] == "ConsistencyBot"

    async def test_two_tokens_different_tenants_no_leak(self, app_with_auth):
        """两个不同租户的 token 分别使用 — agent 列表完全隔离。"""
        ctx = app_with_auth

        # 创建两个 token
        token_a = _make_token(
            sub=ctx["client_a"]["client_id"],
            private_key=ctx["private_key"],
            tenant=TENANT_A,
            scope="agent:register agent:read",
        )
        token_b = _make_token(
            sub=ctx["client_b"]["client_id"],
            private_key=ctx["private_key"],
            tenant=TENANT_B,
            scope="agent:register agent:read",
        )

        # 各注册一个 agent
        await _register_with_token(ctx["client"], token_a, "AcmeOnly")
        await _register_with_token(ctx["client"], token_b, "GlobexOnly")

        # 各自只能看到自己的
        a_result = await _list_with_token(ctx["client"], token_a)
        a_names = [a["name"] for a in a_result["body"]["agents"]]
        assert "AcmeOnly" in a_names
        assert "GlobexOnly" not in a_names

        b_result = await _list_with_token(ctx["client"], token_b)
        b_names = [b["name"] for b in b_result["body"]["agents"]]
        assert "GlobexOnly" in b_names
        assert "AcmeOnly" not in b_names

    async def test_same_tenant_different_clients_see_each_other(self, app_with_auth):
        """同一租户内不同 client → agent 相互可见。"""
        ctx = app_with_auth

        # 为同一个租户注册两个不同的 client
        client_a2_info = ctx["store"].register_client(
            agent_card_id="agent-a2",
            allowed_scopes=list(SCOPES.keys()),
            description="Tenant A second client",
            tenant=TENANT_A,
        )

        token_a1 = _make_token(
            sub=ctx["client_a"]["client_id"],
            private_key=ctx["private_key"],
            tenant=TENANT_A,
            scope="agent:register agent:read",
        )
        token_a2 = _make_token(
            sub=client_a2_info["client_id"],
            private_key=ctx["private_key"],
            tenant=TENANT_A,
            scope="agent:register agent:read",
        )

        # Client A1 注册一个 agent
        await _register_with_token(ctx["client"], token_a1, "SharedAgent")

        # Client A2 也能看到它（同一租户）
        result = await _list_with_token(ctx["client"], token_a2)
        names = [a["name"] for a in result["body"]["agents"]]
        assert "SharedAgent" in names, (
            f"同一租户内不同 client 应互相可见: {names}"
        )

    async def test_token_with_tenant_does_not_see_default_tenant_agents(self, app_with_auth):
        """Token 有 tenant=A → 不应看到默认租户（空字符串）的 agent。

        这是严格隔离的行为：指定了 tenant 的 token 只看到精确匹配租户的 agent。
        """
        ctx = app_with_auth
        store = ctx["store"]
        store.register_agent(_agent_card("DefaultAgent"), tenant="")

        token_a = _make_token(
            sub=ctx["client_a"]["client_id"],
            private_key=ctx["private_key"],
            tenant=TENANT_A,
        )

        result = await _list_with_token(ctx["client"], token_a)
        names = [a["name"] for a in result["body"]["agents"]]
        assert "DefaultAgent" not in names, (
            f"Token A 不应看到默认租户的 agent: {names}"
        )