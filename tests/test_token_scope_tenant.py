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
            scope="agent:read agent:register",
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
            scope="agent:read agent:register",
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
            scope="agent:read agent:register",
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

    async def test_token_with_tenant_sees_empty_tenant_agents_for_backward_compat(self, app_with_auth):
        """Token 有 tenant=A → 也能看到空 tenant（旧数据向后兼容）。

        这是向后兼容行为：tenant isolation 引入前注册的 agent 有 tenant_id=''，
        这些旧 agent 应被所有非 admin token 可见，确保迁移无中断。
        """
        ctx = app_with_auth
        store = ctx["store"]
        store.register_agent(_agent_card("DefaultAgent"), tenant="")

        token_a = _make_token(
            sub=ctx["client_a"]["client_id"],
            private_key=ctx["private_key"],
            tenant=TENANT_A,
            scope="agent:read",
        )

        result = await _list_with_token(ctx["client"], token_a)
        names = [a["name"] for a in result["body"]["agents"]]
        assert "DefaultAgent" in names, (
            f"Token A 应能看到空 tenant 的旧 agent（向后兼容）: {names}"
        )


# ======================================================================
# P6-A5-2+3+4-1: 同租户内正常协作测试 — tenant1 的 agent A 和 B 可互查，
#                 但看不到 tenant2 的 agent
# ======================================================================


class TestIntraTenantCollaboration:
    """同租户内 agent 相互可见性测试。

    覆盖场景:
      1. Token A1 列出 agent → 看到本租户 (A1+A2) 两个 agent
      2. Token A2 列出 agent → 看到本租户 (A1+A2) 两个 agent（双向可见）
      3. Token B1 列出 agent → 只看到自己的 agent（跨租户隔离）
      4. Token A1 获取 Agent A2 详情 → 同租户详情可见
      5. Token A2 获取 Agent A1 详情 → 同租户详情可见（双向详情）
      6. Token A1 给 Agent A2 发 heartbeat → 同租户可操作
      7. Token A1 不能获取 Agent B1 详情 → 跨租户详情不可见
      8. Token A1 不能给 Agent B1 发 heartbeat → 跨租户操作不可见
      9. Token A1 Toggle Agent A2 → 同租户内禁用/启用可协作
     10. Token A1 搜索只能在本租户内返回结果
    """

    async def _setup_three_agents(self, ctx):
        """Helper: 为 Tenant A 注册两个 agent (A1, A2)，为 Tenant B 注册一个 agent (B1)。

        返回:
            token_a1, token_a2, token_b1, aid_a1, aid_a2, aid_b1
        """
        # --- 为 Tenant A 注册两个 client ---
        client_a1_info = ctx["client_a"]  # 已有
        client_a2_info = ctx["store"].register_client(
            agent_card_id="agent-a2-for-intra",
            allowed_scopes=list(SCOPES.keys()),
            description="Tenant A second client (A2)",
            tenant=TENANT_A,
        )

        # --- 为 Tenant B 注册一个 client ---
        client_b1_info = ctx["store"].register_client(
            agent_card_id="agent-b1-for-intra",
            allowed_scopes=list(SCOPES.keys()),
            description="Tenant B client (B1)",
            tenant=TENANT_B,
        )

        # 签发 token
        token_a1 = _make_token(
            sub=ctx["client_a"]["client_id"],
            private_key=ctx["private_key"],
            tenant=TENANT_A,
            scope="agent:register agent:read agent:admin task:read task:write agent:write token:admin",
        )
        token_a2 = _make_token(
            sub=client_a2_info["client_id"],
            private_key=ctx["private_key"],
            tenant=TENANT_A,
            scope="agent:register agent:read agent:admin task:read task:write agent:write token:admin",
        )
        token_b1 = _make_token(
            sub=client_b1_info["client_id"],
            private_key=ctx["private_key"],
            tenant=TENANT_B,
            scope="agent:register agent:read agent:admin task:read task:write agent:write token:admin",
        )

        # Token A1 注册 Agent A1
        reg_a1 = await _register_with_token(
            ctx["client"], token_a1, "IntraAgent-A1",
        )
        assert reg_a1["status"] == 201, f"注册 IntraAgent-A1 失败: {reg_a1['body']}"
        aid_a1 = reg_a1["body"]["id"]

        # Token A2 注册 Agent A2 —— 同一租户内不同 client 注册的 agent
        reg_a2 = await _register_with_token(
            ctx["client"], token_a2, "IntraAgent-A2",
        )
        assert reg_a2["status"] == 201, f"注册 IntraAgent-A2 失败: {reg_a2['body']}"
        aid_a2 = reg_a2["body"]["id"]

        # Token B1 注册 Agent B1 —— 属于 tenant2
        reg_b1 = await _register_with_token(
            ctx["client"], token_b1, "ExtAgent-B1",
        )
        assert reg_b1["status"] == 201, f"注册 ExtAgent-B1 失败: {reg_b1['body']}"
        aid_b1 = reg_b1["body"]["id"]

        return token_a1, token_a2, token_b1, aid_a1, aid_a2, aid_b1

    # ----------------------------------------------------------------
    # 1. 同租户内列表可见性 — Token A1 看到两个 agent
    # ----------------------------------------------------------------

    async def test_token_a1_lists_own_tenant_agents(self, app_with_auth):
        """Token A1 列出 → 看到本租户两个 agent (A1+A2)，看不到 tenant2 的。"""
        ctx = app_with_auth
        token_a1, token_a2, token_b1, aid_a1, aid_a2, aid_b1 = await self._setup_three_agents(ctx)

        result = await _list_with_token(ctx["client"], token_a1)
        assert result["status"] == 200
        names = [a["name"] for a in result["body"]["agents"]]

        # 应看到本租户的两个 agent
        assert "IntraAgent-A1" in names, (
            f"Token A1 应看到 IntraAgent-A1: {names}"
        )
        assert "IntraAgent-A2" in names, (
            f"Token A1 应看到同租户的 IntraAgent-A2: {names}"
        )
        # 不应看到 tenant2 的 agent
        assert "ExtAgent-B1" not in names, (
            f"Token A1 不应看到 tenant2 的 agent: {names}"
        )
        assert len(names) == 2, (
            f"Token A1 应正好看到 2 个 agent（本租户），实际 {len(names)}: {names}"
        )

    # ----------------------------------------------------------------
    # 2. 同租户内列表可见性 — Token A2 看到两个 agent（双向可见）
    # ----------------------------------------------------------------

    async def test_token_a2_lists_own_tenant_agents_bidirectional(self, app_with_auth):
        """Token A2 列出 → 也看到本租户两个 agent（双向可见）。"""
        ctx = app_with_auth
        token_a1, token_a2, token_b1, aid_a1, aid_a2, aid_b1 = await self._setup_three_agents(ctx)

        result = await _list_with_token(ctx["client"], token_a2)
        assert result["status"] == 200
        names = [a["name"] for a in result["body"]["agents"]]

        # Token A2 也应看到同租户的两个 agent（双向可见）
        assert "IntraAgent-A1" in names, (
            f"Token A2 应看到同租户的 IntraAgent-A1: {names}"
        )
        assert "IntraAgent-A2" in names, (
            f"Token A2 应看到自己注册的 IntraAgent-A2: {names}"
        )
        # 不应看到 tenant2 的 agent
        assert "ExtAgent-B1" not in names, (
            f"Token A2 不应看到 tenant2 的 agent: {names}"
        )
        assert len(names) == 2, (
            f"Token A2 应正好看到 2 个 agent（本租户），实际 {len(names)}: {names}"
        )

    # ----------------------------------------------------------------
    # 3. 跨租户隔离 — Token B1 看不到 tenant1 的 agent
    # ----------------------------------------------------------------

    async def test_tenant_b_sees_only_own_agents(self, app_with_auth):
        """Token B1 (tenant2) 列出 → 只看到自己的 agent，看不到 tenant1 的两个。"""
        ctx = app_with_auth
        token_a1, token_a2, token_b1, aid_a1, aid_a2, aid_b1 = await self._setup_three_agents(ctx)

        result = await _list_with_token(ctx["client"], token_b1)
        assert result["status"] == 200
        names = [a["name"] for a in result["body"]["agents"]]

        # 应看到自己的 agent
        assert "ExtAgent-B1" in names, (
            f"Token B1 应看到 ExtAgent-B1: {names}"
        )
        # 不应看到 tenant1 的 agent
        assert "IntraAgent-A1" not in names, (
            f"Token B1 不应看到 tenant1 的 IntraAgent-A1: {names}"
        )
        assert "IntraAgent-A2" not in names, (
            f"Token B1 不应看到 tenant1 的 IntraAgent-A2: {names}"
        )
        assert len(names) == 1, (
            f"Token B1 应正好看到 1 个 agent（自己的），实际 {len(names)}: {names}"
        )

    # ----------------------------------------------------------------
    # 4. 同租户内详情可见 — Token A1 获取 Agent A2 详情
    # ----------------------------------------------------------------

    async def test_intra_tenant_detail_visible(self, app_with_auth):
        """Token A1 获取 Agent A2 详情 → 成功（同租户详情可见）。"""
        ctx = app_with_auth
        token_a1, token_a2, token_b1, aid_a1, aid_a2, aid_b1 = await self._setup_three_agents(ctx)

        # Token A1 获取 Agent A2 的详情（同租户内不同 client）
        result = await _get_agent_with_token(ctx["client"], token_a1, aid_a2)
        assert result["status"] == 200, (
            f"Token A1 获取同租户 Agent A2 详情应返回 200: {result['status']}"
        )
        assert result["body"]["name"] == "IntraAgent-A2", (
            f"返回的 agent 名称应为 IntraAgent-A2: {result['body'].get('name')}"
        )

    # ----------------------------------------------------------------
    # 5. 同租户双向详情 — Token A2 获取 Agent A1 详情
    # ----------------------------------------------------------------

    async def test_intra_tenant_detail_bidirectional(self, app_with_auth):
        """Token A2 获取 Agent A1 详情 → 成功（双向详情可见）。"""
        ctx = app_with_auth
        token_a1, token_a2, token_b1, aid_a1, aid_a2, aid_b1 = await self._setup_three_agents(ctx)

        result = await _get_agent_with_token(ctx["client"], token_a2, aid_a1)
        assert result["status"] == 200, (
            f"Token A2 获取同租户 Agent A1 详情应返回 200: {result['status']}"
        )
        assert result["body"]["name"] == "IntraAgent-A1", (
            f"返回的 agent 名称应为 IntraAgent-A1: {result['body'].get('name')}"
        )

    # ----------------------------------------------------------------
    # 6. 同租户内 Heartbeat — Token A1 给 Agent A2 发 heartbeat
    # ----------------------------------------------------------------

    async def test_intra_tenant_heartbeat(self, app_with_auth):
        """Token A1 给 Agent A2 发 heartbeat → 成功（同租户可操作）。"""
        ctx = app_with_auth
        token_a1, token_a2, token_b1, aid_a1, aid_a2, aid_b1 = await self._setup_three_agents(ctx)

        result = await _heartbeat_with_token(ctx["client"], token_a1, aid_a2)
        assert result["status"] == 203, (
            f"Token A1 给同租户 Agent A2 发 heartbeat 应返回 203: {result['status']}"
        )

    # ----------------------------------------------------------------
    # 7. 跨租户详情不可见 — Token A1 不能获取 Agent B1 详情
    # ----------------------------------------------------------------

    async def test_cross_tenant_detail_invisible(self, app_with_auth):
        """Token A1 获取 Agent B1 详情 → 403/404（跨租户隔离）。"""
        ctx = app_with_auth
        token_a1, token_a2, token_b1, aid_a1, aid_a2, aid_b1 = await self._setup_three_agents(ctx)

        result = await _get_agent_with_token(ctx["client"], token_a1, aid_b1)
        assert result["status"] in (403, 404), (
            f"Token A1 获取 tenant2 的 Agent B1 详情应返回 403/404: {result['status']}"
        )

    # ----------------------------------------------------------------
    # 8. 跨租户 Heartbeat 不可见 — Token A1 不能给 Agent B1 发 heartbeat
    # ----------------------------------------------------------------

    async def test_cross_tenant_heartbeat_blocked(self, app_with_auth):
        """Token A1 给 Agent B1 发 heartbeat → 403/404（跨租户操作不可见）。"""
        ctx = app_with_auth
        token_a1, token_a2, token_b1, aid_a1, aid_a2, aid_b1 = await self._setup_three_agents(ctx)

        result = await _heartbeat_with_token(ctx["client"], token_a1, aid_b1)
        assert result["status"] in (403, 404), (
            f"Token A1 给 tenant2 的 Agent B1 发 heartbeat 应返回 403/404: {result['status']}"
        )

    # ----------------------------------------------------------------
    # 9. 同租户内 Toggle — Token A1 切换 Agent A2 的禁用状态
    # ----------------------------------------------------------------

    async def test_intra_tenant_toggle(self, app_with_auth):
        """Token A1 切换 Agent A2 的禁用状态 → 成功（同租户可协作）。"""
        ctx = app_with_auth
        token_a1, token_a2, token_b1, aid_a1, aid_a2, aid_b1 = await self._setup_three_agents(ctx)

        # 先确认 Agent A2 当前为启用状态
        detail = await _get_agent_with_token(ctx["client"], token_a1, aid_a2)
        assert detail["status"] == 200
        initial_disabled = detail["body"].get("disabled", False)

        # Token A1 toggle Agent A2（同租户操作）
        resp = await ctx["client"].post(
            f"/v1/agents/{aid_a2}/toggle",
            headers={"Authorization": f"Bearer {token_a1}"},
        )
        assert resp.status == 200, (
            f"Token A1 toggle 同租户 Agent A2 应返回 200: {resp.status}"
        )

        # 验证状态已切换
        detail2 = await _get_agent_with_token(ctx["client"], token_a1, aid_a2)
        assert detail2["status"] == 200
        new_disabled = detail2["body"].get("disabled", False)
        assert new_disabled != initial_disabled, (
            f"Toggle 后 disabled 状态应改变: initial={initial_disabled}, new={new_disabled}"
        )

    # ----------------------------------------------------------------
    # 10. 同租户内搜索 — 搜索只在本租户内有效
    # ----------------------------------------------------------------

    async def test_intra_tenant_search_isolation(self, app_with_auth):
        """Token A1 搜索 agent → 只能搜到本租户的 agent，看不到 tenant2 的。"""
        ctx = app_with_auth
        token_a1, token_a2, token_b1, aid_a1, aid_a2, aid_b1 = await self._setup_three_agents(ctx)

        # Token A1 搜索 "IntraAgent" 前缀
        params = {"q": "IntraAgent"}
        resp = await ctx["client"].get(
            "/v1/agents",
            params=params,
            headers={"Authorization": f"Bearer {token_a1}"},
        )
        assert resp.status == 200
        data = await resp.json()
        names = [a["name"] for a in data["agents"]]

        # 应搜到本租户的两个 agent
        assert "IntraAgent-A1" in names, f"搜索应命中 IntraAgent-A1: {names}"
        assert "IntraAgent-A2" in names, f"搜索应命中 IntraAgent-A2: {names}"
        # tenant2 的 agent 不应该出现（即使名字也匹配）
        assert "ExtAgent-B1" not in names, (
            f"搜索不应返回 tenant2 的 agent: {names}"
        )


# ======================================================================
# P6-A5-2+3+4-3: Admin 跨租户查看测试 — admin scope 能查所有租户，
#                 不传 tenant 时默认展示全部
# ======================================================================


class TestAdminCrossTenantAccess:
    """registry:admin scope — 跨租户全操作测试。

    覆盖场景:
      1. Admin token 列出 agent → 显示所有租户的 agent
      2. Admin token + ?tenant=X → 仅显示特定租户的 agent
      3. Admin token 获取任意 agent 详情 → 跨租户可见
      4. Admin token 发送 heartbeat 到任意 agent → 跨租户可操作
      5. Admin token toggle 任意 agent → 跨租户可操作
      6. Admin token 删除任意 agent → 跨租户可操作
      7. Admin token 注册 agent → 不指定 tenant 时属于默认租户
      8. 非 admin token 隔离在 admin 操作后不受影响
    """

    async def _setup(self, ctx):
        """Helper: 使用 store 直接为两个租户各注册一个 agent。"""
        store = ctx["store"]
        aid_a = store.register_agent(
            _agent_card("AdminTestA", tenant=TENANT_A), tenant=TENANT_A,
        )
        aid_b = store.register_agent(
            _agent_card("AdminTestB", tenant=TENANT_B), tenant=TENANT_B,
        )
        return aid_a, aid_b

    def _admin_token(self, ctx):
        """签发 registry:admin scope 的 token。"""
        return _make_token(
            sub=ctx["client_a"]["client_id"],
            private_key=ctx["private_key"],
            scope="registry:admin agent:read agent:register agent:admin task:read",
        )

    # ----------------------------------------------------------------
    # 1. 列表 — admin 能看到所有租户的 agent
    # ----------------------------------------------------------------

    async def test_admin_list_shows_all_tenants(self, app_with_auth):
        """admin token → GET /v1/agents → 返回所有租户的 agent。"""
        ctx = app_with_auth
        aid_a, aid_b = await self._setup(ctx)
        admin_token = self._admin_token(ctx)

        result = await _list_with_token(ctx["client"], admin_token)
        assert result["status"] == 200
        names = [a["name"] for a in result["body"]["agents"]]
        assert "AdminTestA" in names, f"Admin 应看到 Tenant A 的 agent: {names}"
        assert "AdminTestB" in names, f"Admin 应看到 Tenant B 的 agent: {names}"
        assert len(names) == 2, (
            f"Admin 应看到 2 个 agent（两个租户各一个），"
            f"实际 {len(names)}: {names}"
        )

    # ----------------------------------------------------------------
    # 2. 列表 — admin + ?tenant=X 过滤
    # ----------------------------------------------------------------

    async def test_admin_list_with_tenant_filter(self, app_with_auth):
        """admin token + ?tenant=TENANT_A → 仅显示 Tenant A 的 agent。"""
        ctx = app_with_auth
        aid_a, aid_b = await self._setup(ctx)
        admin_token = self._admin_token(ctx)

        # admin + ?tenant=TENANT_A → 只返回 Tenant A
        result = await _list_with_token(
            ctx["client"], admin_token, tenant_param=TENANT_A,
        )
        assert result["status"] == 200
        names = [a["name"] for a in result["body"]["agents"]]
        assert "AdminTestA" in names, (
            f"Admin+?tenant=A 应看到 Tenant A: {names}"
        )
        assert "AdminTestB" not in names, (
            f"Admin+?tenant=A 不应看到 Tenant B: {names}"
        )
        assert len(names) == 1, (
            f"Admin+?tenant=A 应正好 1 个 agent: {names}"
        )

        # admin + ?tenant=TENANT_B → 只返回 Tenant B
        result = await _list_with_token(
            ctx["client"], admin_token, tenant_param=TENANT_B,
        )
        assert result["status"] == 200
        names = [a["name"] for a in result["body"]["agents"]]
        assert "AdminTestB" in names
        assert "AdminTestA" not in names
        assert len(names) == 1

    # ----------------------------------------------------------------
    # 3. 详情 — admin 可以获取任意租户的 agent 详情
    # ----------------------------------------------------------------

    async def test_admin_get_any_tenant_agent(self, app_with_auth):
        """admin token → GET /v1/agents/{id} → 任意租户 agent 详情可见。"""
        ctx = app_with_auth
        aid_a, aid_b = await self._setup(ctx)
        admin_token = self._admin_token(ctx)

        # 获取 Tenant A 的 agent
        result_a = await _get_agent_with_token(ctx["client"], admin_token, aid_a)
        assert result_a["status"] == 200, (
            f"Admin 获取 Tenant A agent 应返回 200: {result_a['status']}"
        )
        assert result_a["body"]["name"] == "AdminTestA"

        # 获取 Tenant B 的 agent
        result_b = await _get_agent_with_token(ctx["client"], admin_token, aid_b)
        assert result_b["status"] == 200, (
            f"Admin 获取 Tenant B agent 应返回 200: {result_b['status']}"
        )
        assert result_b["body"]["name"] == "AdminTestB"

    # ----------------------------------------------------------------
    # 4. Heartbeat — admin 可以给任意租户的 agent 发 heartbeat
    # ----------------------------------------------------------------

    async def test_admin_heartbeat_any_tenant_agent(self, app_with_auth):
        """admin token → POST /v1/agents/{id}/heartbeat → 任意租户成功。"""
        ctx = app_with_auth
        aid_a, aid_b = await self._setup(ctx)
        admin_token = self._admin_token(ctx)

        # 给 Tenant A 的 agent 发 heartbeat
        hb_a = await _heartbeat_with_token(ctx["client"], admin_token, aid_a)
        assert hb_a["status"] == 203, (
            f"Admin heartbeat Tenant A agent 应返回 203: {hb_a['status']}"
        )

        # 给 Tenant B 的 agent 发 heartbeat
        hb_b = await _heartbeat_with_token(ctx["client"], admin_token, aid_b)
        assert hb_b["status"] == 203, (
            f"Admin heartbeat Tenant B agent 应返回 203: {hb_b['status']}"
        )

    # ----------------------------------------------------------------
    # 5. Toggle — admin 可以 toggle 任意租户的 agent
    # ----------------------------------------------------------------

    async def test_admin_toggle_any_tenant_agent(self, app_with_auth):
        """admin token → POST /v1/agents/{id}/toggle → 任意租户可 toggle。"""
        ctx = app_with_auth
        aid_a, aid_b = await self._setup(ctx)
        admin_token = self._admin_token(ctx)

        # Toggle Tenant A 的 agent
        resp = await ctx["client"].post(
            f"/v1/agents/{aid_a}/toggle",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert resp.status == 200, (
            f"Admin toggle Tenant A agent 应返回 200: {resp.status}"
        )

        # Toggle Tenant B 的 agent
        resp = await ctx["client"].post(
            f"/v1/agents/{aid_b}/toggle",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert resp.status == 200, (
            f"Admin toggle Tenant B agent 应返回 200: {resp.status}"
        )

    # ----------------------------------------------------------------
    # 6. Unregister — admin 可以删除任意租户的 agent
    # ----------------------------------------------------------------

    async def test_admin_unregister_any_tenant_agent(self, app_with_auth):
        """admin token → DELETE /v1/agents/{id} → 任意租户可删除。"""
        ctx = app_with_auth
        aid_a, aid_b = await self._setup(ctx)
        admin_token = self._admin_token(ctx)

        # 删除 Tenant B 的 agent（确保 admin 有权限）
        del_result = await _unregister_with_token(
            ctx["client"], admin_token, aid_b,
        )
        assert del_result["status"] == 200, (
            f"Admin 删除 Tenant B agent 应返回 200: {del_result['status']}"
        )

        # Tenant A 的 agent 仍然存在
        store = ctx["store"]
        assert store.get_agent(aid_a) is not None, (
            "Tenant A 的 agent 在删除 Tenant B 后应仍然存在"
        )
        assert store.get_agent(aid_b) is None, (
            "Tenant B 的 agent 应已被 Admin 删除"
        )

    # ----------------------------------------------------------------
    # 7. Admin 注册 agent — 不指定 tenant 时属于默认租户
    # ----------------------------------------------------------------

    async def test_admin_register_without_tenant(self, app_with_auth):
        """admin token 注册 agent → 不传 tenant → agent 属于空租户。"""
        ctx = app_with_auth
        admin_token = self._admin_token(ctx)

        reg = await _register_with_token(
            ctx["client"], admin_token, "AdminRegisteredBot",
        )
        assert reg["status"] == 201, (
            f"Admin 注册 agent 失败: {reg['body']}"
        )
        agent_id = reg["body"]["id"]

        # 数据库中 tenant_id 应为空字符串
        with ctx["store"]._tx("DEFERRED") as engine:
            row = engine.execute(
                "SELECT tenant_id FROM agents WHERE id=?",
                (agent_id,),
            ).fetchone()
        assert row is not None
        assert row["tenant_id"] == "", (
            f"Admin 注册的 agent tenant_id 应为空，"
            f"实际为 {row['tenant_id']!r}"
        )

    # ----------------------------------------------------------------
    # 8. Non-admin token 在 admin 操作后依然隔离
    # ----------------------------------------------------------------

    async def test_non_admin_isolation_preserved_after_admin_ops(self, app_with_auth):
        """admin 执行跨租户操作后，非 admin token 的隔离不受影响。"""
        ctx = app_with_auth
        aid_a, aid_b = await self._setup(ctx)
        admin_token = self._admin_token(ctx)

        # 签发 non-admin token for Tenant A
        token_a = _make_token(
            sub=ctx["client_a"]["client_id"],
            private_key=ctx["private_key"],
            tenant=TENANT_A,
            scope="agent:read agent:register",
        )

        # Admin 先做一些跨租户操作
        await _heartbeat_with_token(ctx["client"], admin_token, aid_a)
        await _heartbeat_with_token(ctx["client"], admin_token, aid_b)
        resp = await ctx["client"].post(
            f"/v1/agents/{aid_a}/toggle",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert resp.status == 200

        # Tenant A 仍然只能看到自己的 agent
        result = await _list_with_token(ctx["client"], token_a)
        assert result["status"] == 200
        names = [a["name"] for a in result["body"]["agents"]]
        assert "AdminTestA" in names, (
            f"Tenant A 应能看到自己的 agent: {names}"
        )
        assert "AdminTestB" not in names, (
            f"Tenant A 在 admin 操作后仍不应看到 Tenant B: {names}"
        )

# ======================================================================
# P6-A5-2+3+4-4: Backward compat test — non-admin token sees empty-tenant
# legacy agents via list/get/search, but write ops follow strict isolation.
# ======================================================================


class TestBackwardCompatNonAdminTokenSeesEmptyTenantAgents:
    """Non-admin token backward compatibility tests.

    Old agents with tenant_id='' (registered before tenant isolation was
    introduced) must still be visible to all non-admin tokens for query
    operations (list/get/search). However, write operations follow strict
    isolation: non-admin tokens must not heartbeart/toggle/unregister
    agents that don't belong to their tenant, including empty-tenant agents.
    """

    async def _setup_legacy_and_tenanted(self, ctx, store=None):
        s = store or ctx["store"]
        aid_legacy = s.register_agent(
            _agent_card("LegacyBot"), tenant="",
        )
        aid_a = s.register_agent(
            _agent_card("AlphaBot"), tenant=TENANT_A,
        )
        aid_b = s.register_agent(
            _agent_card("BetaBot"), tenant=TENANT_B,
        )
        return aid_legacy, aid_a, aid_b

    async def test_token_a_lists_own_and_legacy_agents(self, app_with_auth):
        """Token A lists sees own + legacy empty-tenant agents."""
        ctx = app_with_auth
        aid_legacy, aid_a, aid_b = await self._setup_legacy_and_tenanted(ctx)
        token_a = _make_token(
            sub=ctx["client_a"]["client_id"],
            private_key=ctx["private_key"],
            tenant=TENANT_A,
            scope="agent:read",
        )
        result = await _list_with_token(ctx["client"], token_a)
        assert result["status"] == 200
        names = [a["name"] for a in result["body"]["agents"]]
        assert "AlphaBot" in names
        assert "LegacyBot" in names
        assert "BetaBot" not in names

    async def test_token_b_lists_own_and_legacy_agents(self, app_with_auth):
        """Token B also sees legacy empty-tenant agents."""
        ctx = app_with_auth
        aid_legacy, aid_a, aid_b = await self._setup_legacy_and_tenanted(ctx)
        token_b = _make_token(
            sub=ctx["client_b"]["client_id"],
            private_key=ctx["private_key"],
            tenant=TENANT_B,
            scope="agent:read",
        )
        result = await _list_with_token(ctx["client"], token_b)
        assert result["status"] == 200
        names = [a["name"] for a in result["body"]["agents"]]
        assert "BetaBot" in names
        assert "LegacyBot" in names
        assert "AlphaBot" not in names

    async def test_token_a_gets_legacy_agent_detail(self, app_with_auth):
        """Token A gets detail of legacy empty-tenant agent."""
        ctx = app_with_auth
        aid_legacy, aid_a, aid_b = await self._setup_legacy_and_tenanted(ctx)
        token_a = _make_token(
            sub=ctx["client_a"]["client_id"],
            private_key=ctx["private_key"],
            tenant=TENANT_A,
            scope="agent:read",
        )
        result = await _get_agent_with_token(ctx["client"], token_a, aid_legacy)
        assert result["status"] == 200
        assert result["body"]["name"] == "LegacyBot"

    async def test_token_b_gets_legacy_agent_detail(self, app_with_auth):
        """Token B also gets detail of legacy empty-tenant agent."""
        ctx = app_with_auth
        aid_legacy, aid_a, aid_b = await self._setup_legacy_and_tenanted(ctx)
        token_b = _make_token(
            sub=ctx["client_b"]["client_id"],
            private_key=ctx["private_key"],
            tenant=TENANT_B,
            scope="agent:read",
        )
        result = await _get_agent_with_token(ctx["client"], token_b, aid_legacy)
        assert result["status"] == 200
        assert result["body"]["name"] == "LegacyBot"

    async def test_multiple_legacy_agents_all_visible(self, app_with_auth):
        """Multiple legacy agents all visible to Token A."""
        ctx = app_with_auth
        store = ctx["store"]
        for i in range(3):
            store.register_agent(_agent_card(f"LegacyAgent-{i}"), tenant="")
        store.register_agent(_agent_card("AClient"), tenant=TENANT_A)
        token_a = _make_token(
            sub=ctx["client_a"]["client_id"],
            private_key=ctx["private_key"],
            tenant=TENANT_A,
            scope="agent:read",
        )
        result = await _list_with_token(ctx["client"], token_a)
        names = [a["name"] for a in result["body"]["agents"]]
        assert len(names) == 4
        for i in range(3):
            assert f"LegacyAgent-{i}" in names
        assert "AClient" in names

    async def test_search_finds_legacy_agents(self, app_with_auth):
        """Search by Token A finds legacy empty-tenant agents."""
        ctx = app_with_auth
        await self._setup_legacy_and_tenanted(ctx)
        token_a = _make_token(
            sub=ctx["client_a"]["client_id"],
            private_key=ctx["private_key"],
            tenant=TENANT_A,
            scope="agent:read",
        )
        params = {"q": "Legacy"}
        resp = await ctx["client"].get(
            "/v1/agents", params=params,
            headers={"Authorization": f"Bearer {token_a}"},
        )
        assert resp.status == 200
        data = await resp.json()
        names = [a["name"] for a in data["agents"]]
        assert "LegacyBot" in names
        assert "BetaBot" not in names

    async def test_token_a_cannot_heartbeat_legacy_agent(self, app_with_auth):
        """Write isolation: cannot heartbeat legacy empty-tenant agent."""
        ctx = app_with_auth
        aid_legacy, aid_a, aid_b = await self._setup_legacy_and_tenanted(ctx)
        token_a = _make_token(
            sub=ctx["client_a"]["client_id"],
            private_key=ctx["private_key"],
            tenant=TENANT_A,
            scope="agent:read agent:register",
        )
        result_own = await _heartbeat_with_token(ctx["client"], token_a, aid_a)
        assert result_own["status"] == 203
        result_legacy = await _heartbeat_with_token(ctx["client"], token_a, aid_legacy)
        assert result_legacy["status"] in (403, 404)

    async def test_token_a_cannot_toggle_legacy_agent(self, app_with_auth):
        """Write isolation: cannot toggle legacy empty-tenant agent."""
        ctx = app_with_auth
        aid_legacy, aid_a, aid_b = await self._setup_legacy_and_tenanted(ctx)
        token_a = _make_token(
            sub=ctx["client_a"]["client_id"],
            private_key=ctx["private_key"],
            tenant=TENANT_A,
            scope="agent:admin agent:read agent:register",
        )
        resp = await ctx["client"].post(
            f"/v1/agents/{aid_legacy}/toggle",
            headers={"Authorization": f"Bearer {token_a}"},
        )
        assert resp.status in (403, 404)
        resp = await ctx["client"].post(
            f"/v1/agents/{aid_a}/toggle",
            headers={"Authorization": f"Bearer {token_a}"},
        )
        assert resp.status == 200

    async def test_token_a_cannot_unregister_legacy_agent(self, app_with_auth):
        """Write isolation: cannot unregister legacy empty-tenant agent."""
        ctx = app_with_auth
        aid_legacy, aid_a, aid_b = await self._setup_legacy_and_tenanted(ctx)
        token_a = _make_token(
            sub=ctx["client_a"]["client_id"],
            private_key=ctx["private_key"],
            tenant=TENANT_A,
            scope="agent:admin agent:read agent:register",
        )
        result = await _unregister_with_token(ctx["client"], token_a, aid_legacy)
        assert result["status"] in (403, 404)
        store = ctx["store"]
        assert store.get_agent(aid_legacy) is not None
        result = await _unregister_with_token(ctx["client"], token_a, aid_a)
        assert result["status"] == 200

    async def test_all_legacy_db_all_tokens_see_all(self, app_with_auth):
        """All-legacy DB: both tokens see all agents."""
        ctx = app_with_auth
        store = ctx["store"]
        store.register_agent(_agent_card("OldOne"), tenant="")
        store.register_agent(_agent_card("OldTwo"), tenant="")
        token_a = _make_token(
            sub=ctx["client_a"]["client_id"],
            private_key=ctx["private_key"],
            tenant=TENANT_A,
            scope="agent:read",
        )
        token_b = _make_token(
            sub=ctx["client_b"]["client_id"],
            private_key=ctx["private_key"],
            tenant=TENANT_B,
            scope="agent:read",
        )
        result_a = await _list_with_token(ctx["client"], token_a)
        names_a = [a["name"] for a in result_a["body"]["agents"]]
        assert len(names_a) == 2
        assert "OldOne" in names_a and "OldTwo" in names_a
        result_b = await _list_with_token(ctx["client"], token_b)
        names_b = [a["name"] for a in result_b["body"]["agents"]]
        assert len(names_b) == 2
        assert "OldOne" in names_b and "OldTwo" in names_b
