"""Long-running example agent that continuously heartbeats with the A2A Registry."""
import json
import time
import urllib.request
import urllib.error

REGISTRY_URL = "http://localhost:8321"
HEARTBEAT_INTERVAL = 30  # seconds between heartbeats


def register_agent() -> str:
    """Register this agent and return its assigned ID."""
    payload = {
        "name": "Example Agent",
        "description": "A long-running A2A-compatible example agent",
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


def send_heartbeat(agent_id: str) -> bool:
    """Send a heartbeat. Returns True if alive."""
    req = urllib.request.Request(
        f"{REGISTRY_URL}/v1/agents/{agent_id}/heartbeat",
        method="POST",
    )
    try:
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read())
            print(f"[{time.strftime('%H:%M:%S')}] Heartbeat: {data['status']}")
            return True
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        print(f"[{time.strftime('%H:%M:%S')}] Heartbeat failed: {e.code} {body}")
        return False


if __name__ == "__main__":
    agent_id = register_agent()
    print(f"Heartbeating every {HEARTBEAT_INTERVAL}s...")

    while True:
        if not send_heartbeat(agent_id):
            break
        time.sleep(HEARTBEAT_INTERVAL)