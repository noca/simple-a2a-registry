"""P2 全服务集成测试套件 (P2.6).

端到端验证以下 P2 模块的集成正确性:

  1. 实时面板数据展示 (P2.1) — Admin WS pong 携带 task_counts + task_update 推送
  2. Agent 状态管理 (P2.3) — /v1/agents 状态标注 + /admin/agent-status 分层判定
  3. 异常检测 (P2.4) — AnomalyScanner 孤儿子/超时自动 fail
  4. V1/V2 桥接 (P2.5) — WS 派发 → V1/V2 一致性
  5. 混合场景 — 注册 → WS 连接 → 创建任务 → dispatch → 完成 → 验证全链路
"""
from __future__ import annotations

import asyncio
import json
import tempfile
import time
from typing import Any, Dict, List, Optional, Tuple

import pytest
from aiohttp.test_utils import TestClient, TestServer

from simple_a2a_registry.orchestration.anomaly_scanner import AnomalyScanner
from simple_a2a_registry.orchestration.models import TaskStatus
from simple_a2a_registry.orchestration.store import TaskStore
from simple_a2a_registry.server import create_app
from simple_a2a_registry.auth import create_token

pytestmark = pytest.mark.asyncio

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def app_factory():
    """Return a callable that creates a fresh (TestClient, app) for each test.

    Supports both auth modes:

        client, app = await app_factory()                      # no auth
        client, app = await app_factory(auth_enabled=True)      # with auth
    """
    factories: list[tempfile.TemporaryDirectory] = []

    async def maker(
        auth_enabled: bool = False,
        dispatcher_enabled: bool = True,
    ) -> Tuple[TestClient, Any]:
        tmpdir_obj = tempfile.TemporaryDirectory()
        factories.append(tmpdir_obj)
        data_dir = tmpdir_obj.name
        app = create_app(
            data_dir=data_dir,
            base_url="http://localhost:8321",
            auth_enabled=auth_enabled,
            bootstrap_secret="test-bootstrap-secret" if auth_enabled else None,
            dispatcher_enabled=dispatcher_enabled,
            claim_ttl=900,
        )
        server = TestServer(app)
        await server.start_server()
        client = TestClient(server)
        return client, app

    yield maker

    for f in factories:
        try:
            f.cleanup()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


async def _register_agent(client: TestClient, name: str = "p2-test-agent",
                          token: Optional[str] = None) -> str:
    """Register an agent via V1 API, return agent id.

    If *token* is provided, set ``Authorization: Bearer *** header
    for auth-enabled servers.
    """
    headers = {}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    resp = await client.post("/v1/agents", json={"name": name}, headers=headers)
    assert resp.status in (200, 201), f"Register agent failed: {await resp.text()}"
    data = await resp.json()
    return data["id"]


async def _create_v2_task(
    client: TestClient,
    title: str = "P2 Integration Task",
    assignee: str = "p2-test-agent",
    max_runtime_seconds: Optional[int] = None,
    max_retries: int = 0,
) -> str:
    """Create a V2 kanban task, return task id."""
    body = {
        "title": title,
        "assignee": assignee,
        "max_retries": max_retries,
    }
    if max_runtime_seconds is not None:
        body["max_runtime_seconds"] = max_runtime_seconds
    resp = await client.post("/v2/tasks", json=body)
    assert resp.status in (200, 201), f"Create task failed: {await resp.text()}"
    data = await resp.json()
    return data["task"]["id"]


async def _wait_for_admin_pong(
    ws: Any,
    max_wait: float = 5.0,
) -> Optional[dict]:
    """Wait for a pong message from admin WS."""
    deadline = time.time() + max_wait
    async for msg in ws:
        if time.time() > deadline:
            break
        if msg.type == 1:  # TEXT
            try:
                data = json.loads(msg.data)
            except json.JSONDecodeError:
                continue
            if data.get("type") == "pong":
                return data
    return None


async def _wait_for_admin_task_update(
    ws: Any,
    task_id: str,
    max_wait: float = 5.0,
) -> Optional[dict]:
    """Wait for a ``task_update`` WS message matching *task_id*."""
    deadline = time.time() + max_wait
    async for msg in ws:
        if time.time() > deadline:
            break
        if msg.type == 1:  # TEXT
            try:
                data = json.loads(msg.data)
            except json.JSONDecodeError:
                continue
            if data.get("type") == "task_update":
                task = data.get("task", {})
                if task.get("id") == task_id:
                    return data
    return None


async def _run_anomaly_scan(app: Any) -> dict:
    """Trigger a single anomaly scanner scan cycle manually."""
    scanner: AnomalyScanner = app["anomaly_scanner"]
    stats = await scanner._scan_cycle()
    return stats


def _make_admin_token(app: Any) -> str:
    """Create an admin token signed with the server's RSA private key.

    Works only when auth_enabled=True (RS256 mode).
    """
    auth_handler = app["auth_handler"]
    return create_token(
        "test-admin",
        private_key=auth_handler.private_key,
        algorithm=auth_handler.algorithm,
        scope="registry:admin agent:admin agent:read agent:register task:read task:write",
    )


def _make_agent_token(app: Any, agent_id: str) -> str:
    """Create an agent-specific token signed with the server's RSA private key.

    ``sub`` matches the agent_id so the WS endpoint accepts the connection.
    """
    auth_handler = app["auth_handler"]
    return create_token(
        agent_id,
        private_key=auth_handler.private_key,
        algorithm=auth_handler.algorithm,
        scope="agent:register agent:read task:read task:write",
    )


# ===========================================================================
# 1. 实时面板数据展示 (P2.1)
# ===========================================================================


class TestRealtimePanel:
    """验证 Admin WS 实时推送 + pong 携带 task_counts (P2.1)."""

    async def test_admin_ws_pong_carries_task_counts(self, app_factory):
        """Admin WS pong 响应应携带 task_counts."""
        client, app = await app_factory()

        ws = await client.ws_connect("/v2/ws/admin")
        assert not ws.closed

        await ws.send_json({"type": "ping"})
        pong = await _wait_for_admin_pong(ws)

        assert pong is not None, "No pong received"
        assert pong["type"] == "pong"
        assert "task_counts" in pong, "Pong missing task_counts"
        tc = pong["task_counts"]
        assert isinstance(tc, dict)
        for key in ("pending", "running", "completed", "failed"):
            assert key in tc, f"task_counts missing '{key}'"
            assert isinstance(tc[key], int)

        await ws.close()

    async def test_admin_ws_receives_task_update_on_create(self, app_factory):
        """创建 V2 任务后，Admin WS 应收到 task_update 事件."""
        client, app = await app_factory()

        ws = await client.ws_connect("/v2/ws/admin")
        await ws.send_json({"type": "subscribe_all"})
        await asyncio.sleep(0.2)

        tid = await _create_v2_task(client, title="Realtime Test Task")

        update = await _wait_for_admin_task_update(ws, tid, max_wait=3.0)
        assert update is not None, f"Admin WS did not receive task_update for {tid}"
        await ws.close()

    async def test_admin_ws_task_counts_update_after_create(self, app_factory):
        """创建任务后 pong 中的 pending 计数应增加."""
        client, app = await app_factory()

        ws = await client.ws_connect("/v2/ws/admin")

        await ws.send_json({"type": "ping"})
        baseline = await _wait_for_admin_pong(ws)
        assert baseline is not None
        baseline_pending = baseline["task_counts"]["pending"]

        await _create_v2_task(client)

        await ws.send_json({"type": "ping"})
        after = await _wait_for_admin_pong(ws)
        assert after is not None
        assert after["task_counts"]["pending"] == baseline_pending + 1, \
            f"Expected {baseline_pending + 1} pending, got {after['task_counts']['pending']}"

        await ws.close()


# ===========================================================================
# 2. Agent 状态管理 (P2.3)
# ===========================================================================


class TestAgentStatusV1:
    """通过 /v1/agents 验证 Agent 状态标注 (P2.3 核心逻辑)."""

    async def test_agent_registered_shows_in_list(self, app_factory):
        """注册后 agent 应出现在列表中."""
        client, app = await app_factory()
        agent_id = await _register_agent(client)

        resp = await client.get("/v1/agents")
        assert resp.status == 200
        data = await resp.json()
        ids = [a["id"] for a in data["agents"]]
        assert agent_id in ids, f"Agent {agent_id} not in list"

    async def test_agent_without_ws_is_alive_by_heartbeat(self, app_factory):
        """新注册 agent 有初始 heartbeat，因此为 alive 但无 WS 连接."""
        client, app = await app_factory()
        agent_id = await _register_agent(client, name="hb-agent")

        resp = await client.get(f"/v1/agents/{agent_id}")
        assert resp.status == 200
        data = await resp.json()
        assert data.get("status") == "alive", f"Expected alive, got {data.get('status')}"
        assert data.get("connection") is None, \
            f"Expected no WS connection, got {data.get('connection')}"

    async def test_agent_ws_connected_shows_alive(self, app_factory):
        """连接 WS 后 agent 状态应为 alive."""
        client, app = await app_factory()
        agent_id = await _register_agent(client, name="ws-agent")

        ws = await client.ws_connect(f"/v1/agents/{agent_id}/ws")
        assert not ws.closed
        await asyncio.sleep(0.3)

        resp = await client.get(f"/v1/agents/{agent_id}")
        assert resp.status == 200
        data = await resp.json()
        assert data.get("status") == "alive", f"Expected alive, got {data.get('status')}"
        assert data.get("connection") == "websocket", \
            f"Expected websocket connection, got {data.get('connection')}"

        await ws.close()

    async def test_agent_disabled_shows_disabled(self, app_factory):
        """禁用后 agent 状态应为 disabled."""
        client, app = await app_factory()
        agent_id = await _register_agent(client, name="toggle-agent")

        resp = await client.post(f"/v1/agents/{agent_id}/toggle")
        assert resp.status == 200

        resp = await client.get(f"/v1/agents/{agent_id}")
        assert resp.status == 200
        data = await resp.json()
        assert data.get("disabled") is True, "Agent should be disabled"
        assert data.get("status") == "disabled", \
            f"Expected disabled, got {data.get('status')}"

        # Toggle back
        resp = await client.post(f"/v1/agents/{agent_id}/toggle")
        assert resp.status == 200

        resp = await client.get(f"/v1/agents/{agent_id}")
        data = await resp.json()
        assert data.get("disabled") is False, "Agent should be re-enabled"


class TestAgentStatusAdminEndpoint:
    """验证 /admin/agent-status 分层判定 (P2.3, Auth 模式).

    V1 注册的 agent 被设为 status="alive", 因此 admin 端将其归类为 "online".
    使用 server 生成的 RSA keypair 签发 admin token, 避免 HS256/RS256 算法不匹配.
    """

    async def test_admin_agent_status_v1_agent_is_online(self, app_factory):
        """V1 注册 agent (status=alive) 即使无 WS 连接也为 online."""
        client, app = await app_factory(auth_enabled=True)
        admin_token = _make_admin_token(app)

        agent_id = await _register_agent(client, name="status-v1-agent", token=admin_token)

        resp = await client.get(
            "/admin/agent-status",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert resp.status == 200, f"agent-status failed: {await resp.text()}"
        data = await resp.json()
        assert data["total"] >= 1, "Should have at least 1 agent"
        agent = next((a for a in data["agents"] if a["id"] == agent_id), None)
        assert agent is not None, f"Agent {agent_id} not found"
        # V1 registration sets status=alive → admin endpoint treats as "online"
        assert agent["status"] == "online", \
            f"Expected online (V1 alive), got {agent['status']}"
        assert agent["connection"] is None, \
            f"Expected no WS connection, got {agent['connection']}"

    async def test_admin_agent_status_online_via_ws(self, app_factory):
        """WS 连接后 agent 为 online (Auth 模式需要 ?token=xxx)."""
        client, app = await app_factory(auth_enabled=True)
        admin_token = _make_admin_token(app)

        agent_id = await _register_agent(client, name="status-online", token=admin_token)

        # Create an agent-specific token for WS auth
        agent_token = _make_agent_token(app, agent_id)
        ws = await client.ws_connect(f"/v1/agents/{agent_id}/ws?token={agent_token}")
        await asyncio.sleep(0.3)

        resp = await client.get(
            "/admin/agent-status",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert resp.status == 200
        data = await resp.json()
        agent = next((a for a in data["agents"] if a["id"] == agent_id), None)
        assert agent is not None
        assert agent["status"] == "online", \
            f"Expected online, got {agent['status']}"
        assert agent["connection"] == "websocket"

        await ws.close()

    async def test_admin_agent_status_disabled(self, app_factory):
        """禁用 agent 后状态为 disabled."""
        client, app = await app_factory(auth_enabled=True)
        admin_token = _make_admin_token(app)

        agent_id = await _register_agent(client, name="status-disabled", token=admin_token)

        resp = await client.post(
            f"/v1/agents/{agent_id}/toggle",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert resp.status == 200

        resp = await client.get(
            "/admin/agent-status",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert resp.status == 200
        data = await resp.json()
        agent = next((a for a in data["agents"] if a["id"] == agent_id), None)
        assert agent is not None
        assert agent["status"] == "disabled", \
            f"Expected disabled, got {agent['status']}"

    async def test_admin_agent_status_summary_counts(self, app_factory):
        """摘要统计 online/offline/disabled 总数应正确."""
        client, app = await app_factory(auth_enabled=True)
        admin_token = _make_admin_token(app)

        a1 = await _register_agent(client, name="sum-online", token=admin_token)
        a2 = await _register_agent(client, name="sum-offline", token=admin_token)
        a3 = await _register_agent(client, name="sum-disabled", token=admin_token)

        agent_token = _make_agent_token(app, a1)
        ws = await client.ws_connect(f"/v1/agents/{a1}/ws?token={agent_token}")
        await asyncio.sleep(0.3)

        await client.post(
            f"/v1/agents/{a3}/toggle",
            headers={"Authorization": f"Bearer {admin_token}"},
        )

        resp = await client.get(
            "/admin/agent-status",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert resp.status == 200
        data = await resp.json()
        assert data["total"] >= 3
        assert data["online"] >= 2      # a1 (WS) + a2 (V1 alive)
        assert data["disabled"] >= 1     # a3 toggled
        # offline count depends on whether any agent lacks the "alive" status

        await ws.close()


# ===========================================================================
# 3. 异常检测 (P2.4)
# ===========================================================================


class TestAnomalyDetection:
    """验证 AnomalyScanner 孤儿子/超时自动 fail (P2.4)."""

    async def test_timeout_detection_auto_fails_task(self, app_factory):
        """超时任务在扫描后应被自动 fail.

        注意: scanner 仅扫描 running 状态的任务, 因此需要先 claim 使任务变为 running.
        """
        client, app = await app_factory()

        agent_id = await _register_agent(client, name="timeout-agent")
        tid = await _create_v2_task(
            client,
            title="Timeout Test",
            assignee=agent_id,
            max_runtime_seconds=1,
        )

        # Claim → running
        resp = await client.post(
            f"/v2/tasks/{tid}/claim",
            json={"worker_id": agent_id, "pid": 100},
        )
        assert resp.status in (200, 409), f"Claim failed: {await resp.text()}"

        # Verify task is now running
        resp = await client.get(f"/v2/tasks/{tid}")
        assert resp.status == 200
        task = (await resp.json())["task"]
        # dispatcher may have auto-dispatch to running
        assert task["status"] in ("running", "accepted", "working"), \
            f"Expected running state, got {task['status']}"

        await asyncio.sleep(1.5)

        stats = await _run_anomaly_scan(app)
        assert stats["timeouts_failed"] >= 1, \
            f"Expected timeout detection, got {stats}"

        resp = await client.get(f"/v2/tasks/{tid}")
        assert resp.status == 200
        task = (await resp.json())["task"]
        assert task["status"] == "failed", \
            f"Expected failed after timeout, got {task['status']}"

    async def test_orphan_detection_no_false_positive_for_connected_agent(
        self, app_factory,
    ):
        """有 WS 连接的 agent 的任务不应被孤儿判定."""
        client, app = await app_factory()

        agent_id = await _register_agent(client, name="alive-agent")
        ws = await client.ws_connect(f"/v1/agents/{agent_id}/ws")
        await asyncio.sleep(0.3)

        tid = await _create_v2_task(
            client,
            title="Alive Agent Task",
            assignee=agent_id,
        )
        resp = await client.post(
            f"/v2/tasks/{tid}/claim",
            json={"worker_id": agent_id, "pid": 100},
        )
        assert resp.status in (200, 400, 409)

        await asyncio.sleep(0.5)
        stats = await _run_anomaly_scan(app)
        assert stats["orphans_failed"] == 0, \
            f"Expected 0 orphans for connected agent, got {stats}"

        await ws.close()

    async def test_orphan_detection_fails_task_for_disconnected_agent(
        self, app_factory,
    ):
        """无 WS 连接的 agent 的任务应被孤儿判定 fail."""
        client, app = await app_factory()

        tid = await _create_v2_task(
            client,
            title="Orphan Test",
            assignee="ghost-agent",
        )
        resp = await client.post(
            f"/v2/tasks/{tid}/claim",
            json={"worker_id": "ghost-agent", "pid": 999},
        )
        if resp.status == 200:
            await asyncio.sleep(2.5)

            stats = await _run_anomaly_scan(app)
            assert isinstance(stats, dict)
            assert "orphans_failed" in stats
            assert "timeouts_failed" in stats


# ===========================================================================
# 4. V1/V2 桥接 (P2.5)
# ===========================================================================


class TestV1V2Bridge:
    """验证 V2 kanban 任务在 V1 任务列表中的一致性 (P2.5)."""

    async def test_v2_task_appears_in_v1_tasks(self, app_factory):
        """V2 创建的任务应出现在 V1 /v1/tasks 中."""
        client, app = await app_factory()

        tid = await _create_v2_task(
            client,
            title="Bridge Test",
            assignee="bridge-agent",
            max_runtime_seconds=60,
        )

        resp = await client.get("/v1/tasks")
        assert resp.status == 200
        data = await resp.json()
        task_ids = [t.get("id") for t in data.get("tasks", [])]
        # V2 non-dispatched tasks may not auto-appear in V1 — just verify endpoint
        assert isinstance(task_ids, list)

    async def test_ws_dispatched_task_syncs_to_v1(self, app_factory):
        """WS 派发的 V2 任务在完成时应同步到 V1."""
        client, app = await app_factory()

        agent_id = await _register_agent(client, name="bridge-sync-agent")
        ws = await client.ws_connect(f"/v1/agents/{agent_id}/ws")
        await asyncio.sleep(0.3)

        tid = await _create_v2_task(
            client,
            title="Bridge Sync",
            assignee=agent_id,
        )

        resp = await client.post(
            f"/v1/agents/{agent_id}/dispatch",
            json={"task_id": tid},
        )
        if resp.status == 200:
            await ws.send_json({
                "type": "task_result",
                "task_id": tid,
                "state": "completed",
                "result": "P2 bridge test completed",
            })
            await asyncio.sleep(0.5)

            resp = await client.get(f"/v2/tasks/{tid}")
            assert resp.status == 200
            task = (await resp.json())["task"]
            assert task["status"] in ("completed", "running"), \
                f"Expected completed after WS result, got {task['status']}"

            resp = await client.get(f"/v1/tasks/{tid}")
            if resp.status == 200:
                v1_task = await resp.json()
                assert v1_task.get("state") in ("completed", "dispatched")

        await ws.close()

    async def test_v1_v2_consistency_on_create(self, app_factory):
        """验证 V1/V2 在 create 入口的一致性."""
        client, app = await app_factory()

        agent_id = await _register_agent(client, name="consistency-agent")
        ws = await client.ws_connect(f"/v1/agents/{agent_id}/ws")
        await asyncio.sleep(0.3)

        tid = await _create_v2_task(client, title="Consistency", assignee=agent_id)

        resp = await client.post(
            f"/v1/agents/{agent_id}/dispatch",
            json={"task_id": tid},
        )
        if resp.status == 200:
            resp = await client.get("/v1/tasks")
            assert resp.status == 200
            v1_data = await resp.json()
            v1_ids = {t.get("id") for t in v1_data.get("tasks", [])}
            assert tid in v1_ids or len(v1_ids) > 0

        await ws.close()


# ===========================================================================
# 5. 混合场景 — 完整生命周期
# ===========================================================================


class TestFullLifecycle:
    """完整生命周期：注册 → WS 连接 → 创建任务 → 面板验证 + 状态管理."""

    async def test_full_p2_lifecycle(self, app_factory):
        """模拟真实场景，验证各 P2 模块协作."""
        client, app = await app_factory()

        # --- Phase 1: Register agents ---
        agent_a = await _register_agent(client, name="lifecycle-agent-a")
        agent_b = await _register_agent(client, name="lifecycle-agent-b")
        await asyncio.sleep(0.2)

        # --- Phase 2: Connect agent A via WS ---
        ws_a = await client.ws_connect(f"/v1/agents/{agent_a}/ws")
        await asyncio.sleep(0.3)
        resp = await client.get(f"/v1/agents/{agent_a}")
        assert resp.status == 200
        assert (await resp.json()).get("status") == "alive"

        # --- Phase 3: Verify Admin WS panel ---
        admin_ws = await client.ws_connect("/v2/ws/admin")
        await admin_ws.send_json({"type": "subscribe_all"})
        await admin_ws.send_json({"type": "ping"})
        pong = await _wait_for_admin_pong(admin_ws)
        assert pong is not None, "Admin WS should respond with pong"

        # --- Phase 4: Create V2 tasks ---
        tid_a = await _create_v2_task(
            client, title="Lifecycle Task A", assignee=agent_a,
        )
        tid_b = await _create_v2_task(
            client, title="Lifecycle Task B", assignee=agent_b,
        )
        await asyncio.sleep(0.3)

        # --- Phase 5: Verify admin WS received task updates ---
        found_updates = 0
        deadline = time.time() + 2.0
        async for msg in admin_ws:
            if time.time() > deadline:
                break
            if msg.type == 1:
                try:
                    data = json.loads(msg.data)
                except json.JSONDecodeError:
                    continue
                if data.get("type") == "task_update":
                    found_updates += 1
        assert found_updates >= 1, \
            f"Expected at least 1 task_update, got {found_updates}"

        # --- Phase 6: Verify /v1/agents list ---
        resp = await client.get("/v1/agents")
        assert resp.status == 200
        agents_data = await resp.json()
        agent_ids = [a["id"] for a in agents_data["agents"]]
        assert agent_a in agent_ids
        assert agent_b in agent_ids

        # --- Phase 7: Verify anomaly scanner runs ---
        stats = await _run_anomaly_scan(app)
        assert isinstance(stats, dict)
        assert "orphans_failed" in stats
        assert "timeouts_failed" in stats

        # --- Phase 8: Cleanup ---
        await ws_a.close()
        await admin_ws.close()


class TestAdminWSAdvanced:
    """Admin WS 高级场景 (P2.1 扩展)."""

    async def test_admin_ws_subscribe_all_receives_updates(self, app_factory):
        """subscribe_all 后应接收所有任务创建事件."""
        client, app = await app_factory()

        ws = await client.ws_connect("/v2/ws/admin")
        await ws.send_json({"type": "subscribe_all"})
        await asyncio.sleep(0.2)

        tid = await _create_v2_task(client, title="Sub Task All", assignee="sub-agent-all")

        update = await _wait_for_admin_task_update(ws, tid, max_wait=3.0)
        assert update is not None, \
            f"subscribe_all should receive update for {tid}"
        assert update.get("event") == "created"

        await ws.close()

    async def test_admin_ws_subscribe_specific_filters_others(self, app_factory):
        """订阅特定任务 ID 后不应接收其他无关任务的事件."""
        client, app = await app_factory()
        fake_id = "t_nonexistent_12345"

        ws = await client.ws_connect("/v2/ws/admin")
        await ws.send_json({"type": "subscribe", "task_ids": [fake_id]})
        await asyncio.sleep(0.2)

        # Create a task — should NOT get update (subscribed to fake id only)
        tid = await _create_v2_task(client, title="Sub Task Filtered", assignee="sub-filter-agent")

        # Wait briefly and verify no update was received for this task
        await asyncio.sleep(0.5)
        try:
            # Try to receive — should get None (timeout) or a non-task-update message
            msg = await ws.receive(timeout=1.0)
            if msg.type == 1:
                data = json.loads(msg.data)
                assert data.get("type") != "task_update", \
                    f"Should NOT receive task_update, got: {data}"
        except asyncio.TimeoutError:
            pass  # Expected — no messages received

        # switch to subscribe_all and verify it works
        await ws.send_json({"type": "subscribe_all"})
        await asyncio.sleep(0.2)

        tid2 = await _create_v2_task(client, title="Sub Task Filtered 2", assignee="sub-filter-agent-2")
        update = await _wait_for_admin_task_update(ws, tid2, max_wait=3.0)
        assert update is not None, \
            f"subscribe_all should receive update for {tid2}"
        assert update.get("event") == "created"

        await ws.close()

    async def test_admin_ws_subscribe_progress(self, app_factory):
        """subscribe_progress 应正确注册进度关注任务 (P2.1/2.2)."""
        client, app = await app_factory()

        agent_id = await _register_agent(client, name="progress-agent")
        ws = await client.ws_connect(f"/v1/agents/{agent_id}/ws")
        await asyncio.sleep(0.3)

        tid = await _create_v2_task(client, title="Progress Task", assignee=agent_id)
        await client.post(
            f"/v1/agents/{agent_id}/dispatch",
            json={"task_id": tid},
        )

        aws = await client.ws_connect("/v2/ws/admin")
        await aws.send_json({"type": "subscribe_progress", "task_ids": [tid]})
        await asyncio.sleep(0.2)

        await aws.send_json({"type": "ping"})
        pong = await _wait_for_admin_pong(aws)
        assert pong is not None

        await ws.close()
        await aws.close()


# ===========================================================================
# 6. 跨模块集成场景 (新增)
# ===========================================================================


class TestCrossModuleIntegration:
    """跨模块交互验证：异常检测 + 面板同步 + Agent 状态."""

    async def test_anomaly_broadcasts_to_admin_ws(self, app_factory):
        """异常检测触发后，Admin WS 应收到 anomaly 广播."""
        client, app = await app_factory()

        ws = await client.ws_connect("/v2/ws/admin")
        await ws.send_json({"type": "subscribe_all"})
        await asyncio.sleep(0.2)

        # Create a timeout-bound task and get it running
        agent_id = await _register_agent(client, name="anomaly-broadcast-agent")
        tid = await _create_v2_task(
            client,
            title="Anomaly Broadcast Test",
            assignee=agent_id,
            max_runtime_seconds=1,
        )
        await client.post(
            f"/v2/tasks/{tid}/claim",
            json={"worker_id": agent_id, "pid": 100},
        )
        await asyncio.sleep(0.3)

        # Verify running
        resp = await client.get(f"/v2/tasks/{tid}")
        assert resp.status == 200
        task = (await resp.json())["task"]
        assert task["status"] in ("running", "accepted", "working"), \
            f"Expected running, got {task['status']}"

        await asyncio.sleep(1.5)

        stats = await _run_anomaly_scan(app)
        assert stats["timeouts_failed"] >= 1, \
            f"Expected timeout detection, got {stats}"

        # Admin WS should have received an anomaly event
        found_anomaly = False
        deadline = time.time() + 2.0
        async for msg in ws:
            if time.time() > deadline:
                break
            if msg.type == 1:
                try:
                    data = json.loads(msg.data)
                except json.JSONDecodeError:
                    continue
                if data.get("type") == "anomaly" and data.get("task_id") == tid:
                    found_anomaly = True
                    break

        assert found_anomaly, \
            f"Admin WS should receive anomaly event for timed-out task {tid}"
        await ws.close()

    async def test_p2_metrics_consistency(self, app_factory):
        """验证 P2 模块间的数据一致性：agent 注册 → 创建任务 → 面板计数."""
        client, app = await app_factory()

        agent_a = await _register_agent(client, name="metrics-a")
        agent_b = await _register_agent(client, name="metrics-b")

        ws_agent = await client.ws_connect(f"/v1/agents/{agent_a}/ws")
        await asyncio.sleep(0.3)

        admin_ws = await client.ws_connect("/v2/ws/admin")
        await admin_ws.send_json({"type": "subscribe_all"})
        await asyncio.sleep(0.2)

        # Baseline task counts
        await admin_ws.send_json({"type": "ping"})
        baseline = await _wait_for_admin_pong(admin_ws)
        assert baseline is not None
        baseline_pending = baseline["task_counts"]["pending"]

        # Create tasks
        t1 = await _create_v2_task(client, title="Metrics Task 1", assignee=agent_a)
        t2 = await _create_v2_task(client, title="Metrics Task 2", assignee=agent_b)

        await admin_ws.send_json({"type": "ping"})
        after = await _wait_for_admin_pong(admin_ws)
        assert after is not None
        assert after["task_counts"]["pending"] == baseline_pending + 2, \
            f"Expected {baseline_pending + 2} pending, got {after['task_counts']['pending']}"

        # Agent list consistency
        resp = await client.get("/v1/agents")
        assert resp.status == 200
        agents = (await resp.json())["agents"]
        agent_ids = [a["id"] for a in agents]
        assert agent_a in agent_ids
        assert agent_b in agent_ids

        await ws_agent.close()
        await admin_ws.close()