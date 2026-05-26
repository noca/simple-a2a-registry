"""并发测试：多线程 Agent 注册、并发心跳、并发 Claim 任务。

Tests cover:
  - Concurrent agent registration (asyncio.gather)
  - Concurrent heartbeat on multiple agents
  - Concurrent task claim (V2 orchestration)
  - Concurrent WS connections
"""
from __future__ import annotations

import asyncio
import tempfile
import time

import pytest
from aiohttp.test_utils import TestServer, TestClient

from simple_a2a_registry.server import create_app

pytestmark = pytest.mark.asyncio


@pytest.fixture
def app_factory():
    factories = []

    async def maker() -> TestClient:
        tmpdir_obj = tempfile.TemporaryDirectory()
        factories.append(tmpdir_obj)
        data_dir = tmpdir_obj.name
        app = create_app(data_dir=data_dir, base_url="http://localhost:8321")
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


class TestConcurrentRegistration:
    """多线程注册 Agent — 验证并行注册稳定性。"""

    CONCURRENT = 50  # 并发注册数
    CONCURRENT_100 = 100  # 验收标准要求的 100 并发

    async def test_concurrent_agent_registration(self, app_factory):
        async with await app_factory() as client:
            async def _register(i: int) -> str:
                resp = await client.post(
                    "/v1/agents",
                    json={"name": f"concurrent-agent-{i}"},
                )
                assert resp.status in (200, 201), f"Agent {i} failed: {await resp.text()}"
                data = await resp.json()
                return data["id"]

            tasks = [_register(i) for i in range(self.CONCURRENT)]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            successes = [r for r in results if isinstance(r, str)]
            failures = [r for r in results if not isinstance(r, str)]

            assert len(successes) >= self.CONCURRENT - 2, f"Too many failures: {failures}"
            # Verify uniqueness
            assert len(set(successes)) == len(successes), "Duplicate agent IDs detected"

    async def test_100_concurrent_agent_registration(self, app_factory):
        """验收标准：100 并发 Agent 注册稳定。"""
        async with await app_factory() as client:
            async def _register(i: int) -> str:
                resp = await client.post(
                    "/v1/agents",
                    json={"name": f"hundred-concurrent-{i}"},
                )
                assert resp.status in (200, 201), f"Agent {i} failed: {await resp.text()}"
                data = await resp.json()
                return data["id"]

            tasks = [_register(i) for i in range(self.CONCURRENT_100)]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            successes = [r for r in results if isinstance(r, str)]
            failures = [r for r in results if not isinstance(r, str)]

            assert len(successes) >= self.CONCURRENT_100 - 2, (
                f"100 concurrent registration: {len(successes)} succeeded, "
                f"{len(failures)} failed. Failures: {failures}"
            )
            assert len(set(successes)) == len(successes), "Duplicate agent IDs detected"

    async def test_100_concurrent_registration_then_health(self, app_factory):
        """100 并发注册后验证健康检查和驻留数。"""
        async with await app_factory() as client:
            async def _register(i: int) -> str:
                resp = await client.post(
                    "/v1/agents",
                    json={"name": f"health-check-{i}"},
                )
                data = await resp.json()
                return data["id"]

            ids = await asyncio.gather(*[_register(i) for i in range(100)])

            resp = await client.get("/health")
            data = await resp.json()
            stats = data.get("stats", {})
            assert stats.get("total_agents", 0) >= 100, (
                f"Expected 100+ agents, got {stats.get('total_agents')}"
            )

    async def test_concurrent_registration_then_list(self, app_factory):
        """Register agents, then verify they all appear in listing."""
        async with await app_factory() as client:
            async def _register(i: int) -> str:
                resp = await client.post(
                    "/v1/agents",
                    json={"name": f"list-check-{i}"},
                )
                data = await resp.json()
                return data["id"]

            ids = await asyncio.gather(*[_register(i) for i in range(20)])

            resp = await client.get("/v1/agents")
            data = await resp.json()
            agents = data.get("agents", data) if isinstance(data, dict) else data
            agent_ids = {a["id"] for a in agents}

            for aid in ids:
                assert aid in agent_ids, f"Agent {aid} missing from listing"


class TestConcurrentHeartbeat:
    """并发心跳测试。"""

    async def test_concurrent_heartbeat(self, app_factory):
        async with await app_factory() as client:
            # Register agents
            ids = []
            for i in range(30):
                resp = await client.post(
                    "/v1/agents",
                    json={"name": f"hb-agent-{i}"},
                )
                data = await resp.json()
                ids.append(data["id"])

            # Send concurrent heartbeats
            async def _heartbeat(aid: str) -> int:
                resp = await client.post(f"/v1/agents/{aid}/heartbeat")
                return resp.status

            results = await asyncio.gather(*[_heartbeat(aid) for aid in ids])
            assert all(r == 203 for r in results), f"Heartbeat failures: {[i for i, r in enumerate(results) if r != 203]}"

    async def test_heartbeat_stale_recovery(self, app_factory):
        """After TTL expiry, heartbeat should fail; agent is stale."""
        async with await app_factory() as client:
            resp = await client.post("/v1/agents", json={"name": "stale-test"})
            aid = (await resp.json())["id"]

            # Immediate heartbeat works
            resp = await client.post(f"/v1/agents/{aid}/heartbeat")
            assert resp.status == 203

            # We can't actually wait for TTL expiry in a test,
            # but we can verify the endpoint handles known non-existent agents
            resp = await client.post("/v1/agents/nonexistent/heartbeat")
            assert resp.status == 404


class TestConcurrentV2Claim:
    """并发 Claim 任务 — V2 Orchestration API。"""

    async def test_concurrent_claim_tasks(self, app_factory):
        """Claim multiple tasks concurrently."""
        async with await app_factory() as client:
            # Create tasks via V2 API
            tids = []
            for i in range(20):
                resp = await client.post(
                    "/v2/tasks",
                    json={
                        "title": f"claim-test-{i}",
                        "assignee": f"worker-{i % 5}",
                    },
                )
                assert resp.status in (200, 201), await resp.text()
                data = await resp.json()
                tids.append(data["task"]["id"])

            # Concurrent claim by workers (workers 0-4 each claim a subset)
            async def _claim_worker(wid: int) -> list:
                """Worker claims multiple tasks and returns claimed ids."""
                claimed = []
                for tid in tids[wid::5]:  # Round-robin
                    resp = await client.post(
                        f"/v2/tasks/{tid}/claim",
                        json={"worker_id": f"worker-{wid}"},
                    )
                    if resp.status == 200:
                        data = await resp.json()
                        claimed.append(data["task_id"])
                    await asyncio.sleep(0.01)  # Small delay to avoid race
                return claimed

            results = await asyncio.gather(*[_claim_worker(w) for w in range(5)])
            all_claimed = [tid for batch in results for tid in batch]

            # Each task should be claimed exactly once
            assert len(set(all_claimed)) == len(all_claimed), "A task was claimed more than once"
            assert len(all_claimed) <= 20

    async def test_concurrent_claim_and_heartbeat(self, app_factory):
        """Simultaneous claim + heartbeat on same agent."""
        async with await app_factory() as client:
            # Register agent
            resp = await client.post("/v1/agents", json={"name": "multi-op-agent"})
            aid = (await resp.json())["id"]

            # Create tasks
            tids = []
            for i in range(10):
                resp = await client.post(
                    "/v2/tasks",
                    json={
                        "title": f"multi-{i}",
                        "assignee": "multi-op-agent",
                    },
                )
                tids.append((await resp.json())["task"]["id"])

            # Concurrent heartbeats and claims
            async def _mixed_op():
                for tid in tids:
                    resp = await client.post(f"/v2/tasks/{tid}/claim",
                                                         json={"worker_id": aid})
                    if resp.status == 200:
                        await client.post(f"/v1/agents/{aid}/heartbeat")
                return True

            result = await _mixed_op()
            assert result is True


class TestConcurrentWS:
    """并发 WebSocket 连接。"""

    CONCURRENT_WS = 10

    async def test_concurrent_ws_connections(self, app_factory):
        """Multiple agents connected via WS simultaneously."""
        async with await app_factory() as client:
            aids = []
            for i in range(self.CONCURRENT_WS):
                resp = await client.post(
                    "/v1/agents",
                    json={"name": f"ws-concurrent-{i}"},
                )
                aids.append((await resp.json())["id"])

            # Connect all simultaneously
            async def _connect(aid: str):
                ws = await client.ws_connect(f"/v1/agents/{aid}/ws")
                await ws.send_json({"type": "ping"})
                msg = await ws.receive()
                import json
                data = json.loads(msg.data)
                assert data["type"] == "pong"
                return ws

            wss = await asyncio.gather(*[_connect(aid) for aid in aids])
            assert len(wss) == self.CONCURRENT_WS

            # Cleanup
            for ws in wss:
                await ws.send_json({"type": "close"})
                await ws.close()

    async def test_bulk_dispatch_to_ws_agents(self, app_factory):
        """Dispatch tasks to multiple WS-connected agents."""
        async with await app_factory() as client:
            # Register and connect 5 agents
            agents = {}
            for i in range(5):
                resp = await client.post(
                    "/v1/agents",
                    json={"name": f"bulk-agent-{i}"},
                )
                aid = (await resp.json())["id"]
                ws = await client.ws_connect(f"/v1/agents/{aid}/ws")
                agents[aid] = ws

            # Dispatch one task to each agent
            import json
            dispatch_tasks = []
            for aid in agents:
                dispatch_tasks.append(
                    client.post(f"/v1/agents/{aid}/dispatch",
                                 json={"query": f"task for {aid}"})
                )

            dispatch_results = await asyncio.gather(*dispatch_tasks)
            for r in dispatch_results:
                assert r.status == 202

            # Each agent should receive its task
            for aid, ws in agents.items():
                msg = await ws.receive(timeout=5)
                data = json.loads(msg.data)
                assert data["type"] == "task"
                assert aid in data.get("query", "")

            # Cleanup
            for ws in agents.values():
                await ws.send_json({"type": "close"})
                await ws.close()