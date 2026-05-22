"""Example: register a simple agent with the A2A Registry and send heartbeats."""
import json
import time
import urllib.request
import urllib.error

REGISTRY_URL = "http://localhost:8321"


def register_agent() -> str:
    """Register this agent and return its assigned ID."""
    payload = {
        "name": "Example Agent",
        "description": "An example A2A-compatible agent",
        "url": "http://example-agent.local:9000",
        "tags": ["example", "demo"],
        "capabilities": {
            "skills": [
                {"id": "greet", "name": "Greeting", "description": "Say hello"},
            ],
        },
    }
    req = urllib.request.Request(
        f"{REGISTRY_URL}/v1/agents",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read())
        print(f"Registered as: {data['id']}")
        return data["id"]


def send_heartbeat(agent_id: str) -> None:
    """Send a heartbeat to keep the registration alive."""
    req = urllib.request.Request(
        f"{REGISTRY_URL}/v1/agents/{agent_id}/heartbeat",
        method="POST",
    )
    try:
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read())
            print(f"Heartbeat: {data['status']} (expires at {data['expires_at']})")
    except urllib.error.HTTPError as e:
        print(f"Heartbeat failed: {e.code} {e.read().decode()}")


def list_agents() -> list:
    """List all registered agents."""
    with urllib.request.urlopen(f"{REGISTRY_URL}/v1/agents") as resp:
        return json.loads(resp.read())["agents"]


if __name__ == "__main__":
    agent_id = register_agent()

    for _ in range(3):
        time.sleep(2)
        send_heartbeat(agent_id)

    agents = list_agents()
    print(f"\nTotal agents: {len(agents)}")
    for a in agents:
        print(f"  - {a['name']} ({a['id']}) [{a['status']}]")