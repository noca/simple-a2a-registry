"""Integration tests for the Simple A2A Registry HTTP Server."""
from __future__ import annotations

import json
import time
import tempfile
from pathlib import Path

import pytest
from aiohttp.test_utils import TestServer, TestClient

from simple_a2a_registry.server import create_app
from simple_a2a_registry.store import HEARTBEAT_TIMEOUT

pytestmark = pytest.mark.asyncio


@pytest.fixture
def app_factory():
    """Return a callable that creates a fresh TestClient for each test."""
    factories = []

    async def maker():
        tmpdir_obj = tempfile.TemporaryDirectory()
        factories.append(tmpdir_obj)
        data_dir = tmpdir_obj.name
        profiles_home = Path(tmpdir_obj.name) / "profiles_home"
        profiles_home.mkdir(parents=True, exist_ok=True)
        app = create_app(
            data_dir=data_dir,
            profiles_home=str(profiles_home),
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


class TestHealth:
    async def test_health_returns_healthy(self, app_factory):
        async with await app_factory() as client:
            resp = await client.get("/health")
            assert resp.status == 200
            data = await resp.json()
            assert data["status"] == "healthy"
            assert "uptime_seconds" in data
            assert data["version"] == "1.0.0"

    async def test_health_includes_stats(self, app_factory):
        async with await app_factory() as client:
            await client.post("/v1/agents", json={"name": "Stats Test"})
            resp = await client.get("/health")
            data = await resp.json()
            assert data["stats"]["external_agents"] >= 1


class TestWellKnown:
    async def test_well_known_returns_card(self, app_factory):
        async with await app_factory() as client:
            resp = await client.get("/.well-known/agent-card.json")
            assert resp.status == 200
            data = await resp.json()
            assert data["id"] == "simple-a2a-registry"
            assert data["name"] == "Simple A2A Registry"

    async def test_well_known_cache_header(self, app_factory):
        async with await app_factory() as client:
            resp = await client.get("/.well-known/agent-card.json")
            cc = resp.headers.get("Cache-Control", "")
            assert "max-age=300" in cc


class TestListAgents:
    async def test_empty_list(self, app_factory):
        async with await app_factory() as client:
            resp = await client.get("/v1/agents")
            assert resp.status == 200
            data = await resp.json()
            assert data["total"] == 0
            assert data["agents"] == []

    async def test_list_with_registered_agents(self, app_factory):
        async with await app_factory() as client:
            await client.post("/v1/agents", json={"name": "Alpha"})
            await client.post("/v1/agents", json={"name": "Beta"})
            resp = await client.get("/v1/agents")
            data = await resp.json()
            assert data["total"] == 2

    async def test_list_filter_by_skill(self, app_factory):
        async with await app_factory() as client:
            await client.post("/v1/agents", json={
                "name": "Skilled",
                "capabilities": {"skills": [{"id": "s1", "name": "Data Analysis"}]},
            })
            resp = await client.get("/v1/agents?skill=Data Analysis")
            data = await resp.json()
            assert data["total"] == 1

    async def test_list_filter_by_tag(self, app_factory):
        async with await app_factory() as client:
            await client.post("/v1/agents", json={"name": "Tagged", "tags": ["python"]})
            resp = await client.get("/v1/agents?tag=python")
            data = await resp.json()
            assert data["total"] == 1

    async def test_list_search(self, app_factory):
        async with await app_factory() as client:
            await client.post("/v1/agents", json={"name": "Searchable", "description": "keyword here"})
            resp = await client.get("/v1/agents?q=keyword")
            data = await resp.json()
            assert data["total"] == 1

    async def test_list_pagination(self, app_factory):
        async with await app_factory() as client:
            for i in range(5):
                await client.post("/v1/agents", json={"name": f"Agent-{i}"})
            resp = await client.get("/v1/agents?limit=2&offset=1")
            data = await resp.json()
            assert len(data["agents"]) == 2
            assert data["limit"] == 2
            assert data["offset"] == 1

    async def test_list_max_limit(self, app_factory):
        async with await app_factory() as client:
            resp = await client.get("/v1/agents?limit=9999")
            data = await resp.json()
            assert data["limit"] == 200

    async def test_list_have_status(self, app_factory):
        async with await app_factory() as client:
            await client.post("/v1/agents", json={"name": "Live Check"})
            resp = await client.get("/v1/agents")
            data = await resp.json()
            for agent in data["agents"]:
                assert "status" in agent


class TestGetAgent:
    async def test_get_existing_agent(self, app_factory):
        async with await app_factory() as client:
            post_resp = await client.post("/v1/agents", json={"name": "Find Me", "tags": ["findable"]})
            agent_id = (await post_resp.json())["id"]
            resp = await client.get(f"/v1/agents/{agent_id}")
            assert resp.status == 200
            data = await resp.json()
            assert data["name"] == "Find Me"
            assert data["tags"] == ["findable"]

    async def test_get_nonexistent_agent_404(self, app_factory):
        async with await app_factory() as client:
            resp = await client.get("/v1/agents/nobody-here")
            assert resp.status == 404
            data = await resp.json()
            assert data["error"] == "agent_not_found"


class TestRegisterAgent:
    async def test_register_success(self, app_factory):
        async with await app_factory() as client:
            resp = await client.post("/v1/agents", json={
                "name": "New Agent", "description": "A test agent",
            })
            assert resp.status == 201
            data = await resp.json()
            assert "id" in data
            assert data["message"] == "Agent registered successfully"

    async def test_register_missing_name_400(self, app_factory):
        async with await app_factory() as client:
            resp = await client.post("/v1/agents", json={"description": "No name"})
            assert resp.status == 400

    async def test_register_empty_name_400(self, app_factory):
        async with await app_factory() as client:
            resp = await client.post("/v1/agents", json={"name": ""})
            assert resp.status == 400

    async def test_register_invalid_json_400(self, app_factory):
        async with await app_factory() as client:
            resp = await client.post("/v1/agents", data=b"not json", headers={"Content-Type": "application/json"})
            assert resp.status == 400

    async def test_register_duplicate_name_409(self, app_factory):
        async with await app_factory() as client:
            await client.post("/v1/agents", json={"name": "Unique Name"})
            resp = await client.post("/v1/agents", json={"name": "Unique Name"})
            assert resp.status == 409

    async def test_register_with_full_capabilities(self, app_factory):
        async with await app_factory() as client:
            resp = await client.post("/v1/agents", json={
                "name": "Capable Agent",
                "capabilities": {"skills": [{"id": "s1", "name": "Skill One"}, {"id": "s2", "name": "Skill Two"}]},
            })
            assert resp.status == 201
            data = await resp.json()
            assert len(data["card"]["capabilities"]["skills"]) == 2


class TestHeartbeat:
    async def test_heartbeat_success(self, app_factory):
        async with await app_factory() as client:
            post_resp = await client.post("/v1/agents", json={"name": "Heartbeat Me"})
            agent_id = (await post_resp.json())["id"]
            resp = await client.post(f"/v1/agents/{agent_id}/heartbeat")
            assert resp.status == 203
            data = await resp.json()
            assert data["status"] == "alive"
            assert data["stale_timeout"] == HEARTBEAT_TIMEOUT

    async def test_heartbeat_not_found_404(self, app_factory):
        async with await app_factory() as client:
            resp = await client.post("/v1/agents/nobody/heartbeat")
            assert resp.status == 404

    async def test_heartbeat_stale_returns_410(self, app_factory):
        async with await app_factory() as client:
            post_resp = await client.post("/v1/agents", json={"name": "Stale Test"})
            agent_id = (await post_resp.json())["id"]
            app = client.server.app
            app["store"]._heartbeats[agent_id] = time.time() - HEARTBEAT_TIMEOUT - 10
            resp = await client.post(f"/v1/agents/{agent_id}/heartbeat")
            assert resp.status == 410


class TestUnregister:
    async def test_unregister_success(self, app_factory):
        async with await app_factory() as client:
            post_resp = await client.post("/v1/agents", json={"name": "Remove Me"})
            agent_id = (await post_resp.json())["id"]
            resp = await client.delete(f"/v1/agents/{agent_id}")
            assert resp.status == 200
            get_resp = await client.get(f"/v1/agents/{agent_id}")
            assert get_resp.status == 404

    async def test_unregister_not_found_404(self, app_factory):
        async with await app_factory() as client:
            resp = await client.delete("/v1/agents/nonexistent")
            assert resp.status == 404