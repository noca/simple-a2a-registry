"""A2A-compliant Agent — wraps the Hermes Coder profile.

Implements the Google A2A (Agent-to-Agent) protocol:
  GET  /.well-known/agent-card.json  — Agent Card (discovery)
  POST /tasks/send                   — submit a task (processed by Hermes CLI)
  GET  /tasks/{id}                   — get task status/result

WebSocket integration with the A2A Registry:
  - Connects to ws://<registry>/v1/agents/{agent_id}/ws on startup
  - Receives tasks from Registry via WebSocket (type: "task")
  - Reports progress and results via WebSocket (task_progress / task_result)
  - Sends "ping" every 30s for keepalive
  - Auto-reconnects with exponential backoff on disconnect

Also uses HTTP heartbeat as a fallback liveness mechanism.

Registers with the A2A Registry on startup and heartbeats continuously.
Each task is forwarded to the local Hermes Agent (coder profile) for
real execution — code writing, debugging, PR management, devops, etc.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
import urllib.request
import urllib.parse
from urllib.error import HTTPError
import time
import subprocess
from datetime import datetime, timezone
from enum import Enum
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from aiohttp import web, ClientSession, WSMsgType, ClientWebSocketResponse

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(message)s",
)
logger = logging.getLogger("a2a.coder-agent")

# ── Configuration ──────────────────────────────────────────────────────────

REGISTRY_URL = "http://localhost:8321"
AGENT_PORT = 9001
AGENT_HOST = "0.0.0.0"
AGENT_URL = f"http://localhost:{AGENT_PORT}"
HEARTBEAT_INTERVAL = 30  # seconds
HERMES_PROFILE = "coder"
HERMES_TIMEOUT = 300  # max seconds for a task

# ── WebSocket Registry client config ─────────────────────────────────────
REGISTRY_WS_URL = f"{REGISTRY_URL.replace('http', 'ws')}/v1/agents"

# ── A2A Agent Card — skills taken from the Hermes Coder profile ───────────

SKILLS = [
    {
        "id": "software-development",
        "name": "Software Development",
        "description": "Write, debug, test, and review code across multiple languages (Python, JavaScript, TypeScript, Go, Rust, etc.)",
        "version": "1.0.0",
        "uri_schemes": [],
    },
    {
        "id": "github-workflow",
        "name": "GitHub Workflow",
        "description": "Manage GitHub workflows: branches, commits, PRs, code reviews, issues, CI/CD pipelines",
        "version": "1.0.0",
        "uri_schemes": [],
    },
    {
        "id": "devops",
        "name": "DevOps",
        "description": "Docker, Kubernetes, CI/CD, infrastructure diagnostics, deployment automation",
        "version": "1.0.0",
        "uri_schemes": [],
    },
    {
        "id": "creative-generation",
        "name": "Creative Content Generation",
        "description": "Generate diagrams, ASCII art, slide decks, infographics, and visual designs",
        "version": "1.0.0",
        "uri_schemes": [],
    },
    {
        "id": "document-generation",
        "name": "Document Generation",
        "description": "Generate markdown docs, technical reports, API references, architecture diagrams",
        "version": "1.0.0",
        "uri_schemes": [],
    },
    {
        "id": "data-analysis",
        "name": "Data Analysis",
        "description": "Analyze data, generate visualizations, run statistical models, process CSV/JSON/YAML",
        "version": "1.0.0",
        "uri_schemes": [],
    },
    {
        "id": "research",
        "name": "Research & Knowledge Retrieval",
        "description": "Search web, retrieve and summarize articles, academic papers, and technical documentation",
        "version": "1.0.0",
        "uri_schemes": [],
    },
]


def build_agent_card() -> Dict[str, Any]:
    """Build the A2A Agent Card for the Coder Agent."""
    return {
        "id": "a2a:coder-agent",
        "name": "Hermes Coder Agent",
        "description": "An A2A-compliant coding agent powered by the Hermes Coder profile. "
                       "Can write, debug, test, and review code; manage GitHub workflows; "
                       "perform DevOps tasks; generate diagrams and documents; "
                       "analyze data; and retrieve knowledge.",
        "url": AGENT_URL,
        "version": "1.0.0",
        "capabilities": {
            "skills": SKILLS,
        },
        "provider": {
            "organization": "Hermes Agent",
            "url": "https://hermes-agent.nousresearch.com",
        },
        "tags": [
            "coder",
            "hermes-cli",
            "software-development",
            "github",
            "devops",
            "creative",
            "research",
        ],
    }


# ── A2A Task States (per A2A spec) ─────────────────────────────────────────

class TaskState(str, Enum):
    SUBMITTED = "submitted"
    WORKING = "working"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"


@dataclass
class A2ATask:
    """An A2A task with its state and result."""
    id: str
    state: TaskState
    query: str
    session_id: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    artifact: Optional[Dict[str, Any]] = None
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "id": self.id,
            "state": self.state.value,
            "query": self.query,
            "sessionId": self.session_id,
            "createdAt": datetime.fromtimestamp(self.created_at, tz=timezone.utc).isoformat(),
            "updatedAt": datetime.fromtimestamp(self.updated_at, tz=timezone.utc).isoformat(),
        }
        if self.artifact:
            d["artifact"] = self.artifact
        if self.error:
            d["error"] = self.error
        return d


# ── In-memory task store ────────────────────────────────────────────────────

_tasks: Dict[str, A2ATask] = {}

# Active subprocesses for cancellation tracking
_active_procs: Dict[str, subprocess.Popen] = {}

# ── WebSocket connection state ────────────────────────────────────────────
_ws_session: Optional[ClientSession] = None
_ws_connection: Optional[ClientWebSocketResponse] = None
_ws_agent_id: str = ""
_ws_close_event = asyncio.Event()


# ── Real Hermes CLI task processing ─────────────────────────────────────────

def _clean_hermes_output(raw: str) -> str:
    """Strip ANSI codes, box-drawing chars, and framing from Hermes output."""
    import re
    # Strip ANSI escape sequences
    text = re.sub(r'\x1b\[[0-9;]*[mK]', '', raw)
    # Strip Unicode box-drawing characters
    text = re.sub(r'[─┌┐└┘├┤┬┴┼╭╮╯╰│╱╲╴╵╶╷╸╹╺╻╼╽╾╿▌▐▀▄█░▒▓■□▪▫▲△▼▽◆◇○●◐◑◒◓◔◕★☆☐☑☒♠♣♥♦]', '', text)
    # Strip common Hermes framing lines
    lines = []
    skip_prefixes = ("┌─", "└─", "╭─", "╰─", "├─", "─", "  ┌─ Reasoning", "  └─", "  ╭─", "  ╰─")
    in_reasoning = False
    in_hermes_header = False
    for line in text.splitlines():
        stripped = line.strip()
        # Skip reasoning blocks
        if stripped.startswith("┌─ Reasoning") or stripped.startswith("╭─ Reasoning"):
            in_reasoning = True
            continue
        if stripped.startswith("└─") or stripped.startswith("╰─"):
            in_reasoning = False
            continue
        if in_reasoning:
            continue
        # Skip Hermes header/footer
        if stripped.startswith("╭─ ⚕ Hermes") or stripped.startswith("╭─ Hermes"):
            in_hermes_header = True
            continue
        if in_hermes_header and stripped.startswith("╰─"):
            in_hermes_header = False
            continue
        if in_hermes_header:
            continue
        # Skip framing lines
        if any(stripped.startswith(p) for p in skip_prefixes):
            continue
        if stripped in ("", "╮", "╯", "│"):
            continue
        lines.append(line)

    text = "\n".join(lines)
    # Collapse multiple blank lines
    text = re.sub(r'\n{3,}', '\n\n', text)
    # Strip leading/trailing whitespace
    return text.strip()


async def process_task(task: A2ATask) -> None:
    """Forward the task to the real local Hermes Coder profile."""
    task.state = TaskState.WORKING
    task.updated_at = time.time()
    logger.info("Spawning Hermes (coder) for task %s: %s", task.id, task.query[:80])

    cmd = [
        "hermes", "chat",
        "-q", task.query,
        "--profile", HERMES_PROFILE,
        "--max-turns", "30",
        "-Q",  # quiet mode
    ]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _active_procs[task.id] = proc

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=HERMES_TIMEOUT
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            task.state = TaskState.FAILED
            task.error = f"Task timed out after {HERMES_TIMEOUT}s"
            task.updated_at = time.time()
            logger.warning("Task %s timed out", task.id)
            _active_procs.pop(task.id, None)
            return

        _active_procs.pop(task.id, None)

        if proc.returncode != 0:
            stderr_text = stderr.decode("utf-8", errors="replace")[:2000]
            logger.warning("Hermes exited with code %d for task %s", proc.returncode, task.id)
            task.state = TaskState.FAILED
            task.error = f"Hermes process exited with code {proc.returncode}: {stderr_text}"
            task.updated_at = time.time()
            return

        # Extract the actual response from Hermes output
        full_output = stdout.decode("utf-8", errors="replace")
        cleaned = _clean_hermes_output(full_output)

        # Also capture stderr for diagnostics
        stderr_text = stderr.decode("utf-8", errors="replace")[:500]

        # Try to extract the session ID from the output
        session_id = ""
        for line in full_output.splitlines():
            if "Resume this session with:" in line:
                session_id = line.strip()
                break

        # Determine which skill was used based on the query
        matched_skill = "software-development"
        query_lower = task.query.lower()
        for skill in SKILLS:
            skill_words = skill["name"].lower().split()
            if any(w in query_lower for w in skill_words):
                matched_skill = skill["id"]
                break

        task.state = TaskState.COMPLETED
        task.updated_at = time.time()
        task.artifact = {
            "parts": [
                {"text": cleaned or "(Hermes returned no output)"}
            ],
            "skill": matched_skill,
            "sessionId": session_id,
            "processingTime": round(task.updated_at - task.created_at, 2),
        }
        logger.info("Task %s completed (skill=%s, time=%ss, output=%d chars)",
                    task.id, matched_skill, task.artifact["processingTime"], len(cleaned))

    except Exception as e:
        logger.exception("Task %s failed with exception", task.id)
        task.state = TaskState.FAILED
        task.error = str(e)[:2000]
        task.updated_at = time.time()
        _active_procs.pop(task.id, None)


# ── WebSocket task processing ────────────────────────────────────────────

async def _send_ws_json(msg: Dict[str, Any]) -> bool:
    """Send a JSON message via the active WebSocket connection. Returns True on success."""
    global _ws_connection
    ws = _ws_connection
    if ws is None or ws.closed:
        logger.debug("WS not connected, dropping message: %s", msg.get("type"))
        return False
    try:
        await ws.send_json(msg)
        return True
    except Exception as e:
        logger.warning("WS send failed: %s", e)
        return False


async def process_ws_task(task_msg: Dict[str, Any]) -> None:
    """Process a task received via WebSocket and report results via WS.

    Task message format:
      {"type":"task","id":"uuid","query":"...","sessionId":"...."}
    """
    task_id = task_msg.get("id", "")
    query = (task_msg.get("query") or "").strip()
    session_id = task_msg.get("sessionId", "")

    if not task_id or not query:
        logger.warning("Invalid WS task message: missing id or query: %s", task_msg)
        return

    # Send progress
    await _send_ws_json({
        "type": "task_progress",
        "id": task_id,
        "status": "working",
    })

    # Build and run the task via Hermes CLI
    cmd = [
        "hermes", "chat",
        "-q", query,
        "--profile", HERMES_PROFILE,
        "--max-turns", "30",
        "-Q",
    ]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _active_procs[task_id] = proc

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=HERMES_TIMEOUT
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            _active_procs.pop(task_id, None)
            await _send_ws_json({
                "type": "task_result",
                "id": task_id,
                "status": "failed",
                "error": f"Task timed out after {HERMES_TIMEOUT}s",
            })
            logger.warning("WS task %s timed out", task_id)
            return

        _active_procs.pop(task_id, None)

        if proc.returncode != 0:
            stderr_text = stderr.decode("utf-8", errors="replace")[:2000]
            logger.warning("Hermes exited with code %d for WS task %s", proc.returncode, task_id)
            await _send_ws_json({
                "type": "task_result",
                "id": task_id,
                "status": "failed",
                "error": f"Hermes process exited with code {proc.returncode}: {stderr_text}",
            })
            return

        # Clean and send result
        full_output = stdout.decode("utf-8", errors="replace")
        cleaned = _clean_hermes_output(full_output)

        await _send_ws_json({
            "type": "task_result",
            "id": task_id,
            "status": "completed",
            "result": {"text": cleaned or "(Hermes returned no output)"},
        })
        logger.info("WS task %s completed (output=%d chars)", task_id, len(cleaned))

    except Exception as e:
        logger.exception("WS task %s failed with exception", task_id)
        _active_procs.pop(task_id, None)
        await _send_ws_json({
            "type": "task_result",
            "id": task_id,
            "status": "failed",
            "error": str(e)[:2000],
        })


# ── HTTP Handlers ───────────────────────────────────────────────────────────

def _json(data: Any, status: int = 200, headers: Optional[Dict] = None) -> web.Response:
    return web.json_response(data, status=status, headers=headers)


async def handle_agent_card(request: web.Request) -> web.Response:
    """GET /.well-known/agent-card.json — A2A Agent discovery."""
    card = build_agent_card()
    return _json(card, headers={"Cache-Control": "public, max-age=300"})


async def handle_send_task(request: web.Request) -> web.Response:
    """POST /tasks/send — submit an A2A task.

    Body (A2A TaskInput):
    ```json
    {
      "query": "Write a Python function to sort a list",
      "sessionId": "optional-session-id"
    }
    ```
    """
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return _json({
            "jsonrpc": "2.0",
            "error": {"code": -32700, "message": "Parse error"},
        }, status=400)

    query = (body.get("query") or "").strip()
    if not query:
        return _json({
            "jsonrpc": "2.0",
            "error": {"code": -32602, "message": "Missing 'query' field"},
        }, status=400)

    session_id = body.get("sessionId", "")
    task = A2ATask(
        id=str(uuid.uuid4()),
        state=TaskState.SUBMITTED,
        query=query,
        session_id=session_id,
    )
    _tasks[task.id] = task

    # Start async processing via real Hermes CLI
    asyncio.create_task(process_task(task))

    return _json({
        "jsonrpc": "2.0",
        "result": task.to_dict(),
    }, status=201)


async def handle_get_task(request: web.Request) -> web.Response:
    """GET /tasks/{taskId} — get task status and result."""
    task_id = request.match_info.get("taskId", "")
    task = _tasks.get(task_id)
    if not task:
        return _json({
            "jsonrpc": "2.0",
            "error": {"code": -32000, "message": f"Task '{task_id}' not found"},
        }, status=404)

    return _json({
        "jsonrpc": "2.0",
        "result": task.to_dict(),
    })


async def handle_cancel_task(request: web.Request) -> web.Response:
    """POST /tasks/{taskId}/cancel — cancel a running task."""
    task_id = request.match_info.get("taskId", "")
    task = _tasks.get(task_id)
    if not task:
        return _json({
            "jsonrpc": "2.0",
            "error": {"code": -32000, "message": f"Task '{task_id}' not found"},
        }, status=404)

    if task.state in (TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELED):
        return _json({
            "jsonrpc": "2.0",
            "error": {"code": -32000, "message": f"Task '{task_id}' is already in state {task.state.value}"},
        }, status=409)

    # Kill the running Hermes subprocess if any
    proc = _active_procs.pop(task_id, None)
    if proc:
        try:
            proc.kill()
        except Exception:
            pass

    task.state = TaskState.CANCELED
    task.updated_at = time.time()
    return _json({
        "jsonrpc": "2.0",
        "result": task.to_dict(),
    })


async def handle_list_skills(request: web.Request) -> web.Response:
    """GET /skills — list all available skills and their descriptions."""
    return _json({
        "skills": SKILLS,
    })


async def handle_health(request: web.Request) -> web.Response:
    """GET /health — health check."""
    return _json({
        "status": "healthy",
        "agent": "a2a:coder-agent",
        "uptime_seconds": round(time.time() - request.app["started_at"], 2),
        "active_tasks": sum(1 for t in _tasks.values() if t.state == TaskState.WORKING),
        "total_tasks_served": len(_tasks),
    })


# ── Registry Integration ────────────────────────────────────────────────────

def register_with_registry() -> str:
    """Register this agent with the A2A Registry. Returns agent_id."""
    card = build_agent_card()
    payload = {
        "name": card["name"],
        "description": card["description"],
        "url": card["url"],
        "tags": card["tags"],
        "capabilities": card["capabilities"],
    }
    req = urllib.request.Request(
        f"{REGISTRY_URL}/v1/agents",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read())
            logger.info("Registered with A2A Registry as '%s'", data["id"])
            return data["id"]
    except HTTPError as e:
        body = e.read().decode()
        if e.code == 409:
            logger.info("Agent already registered, fetching ID...")
            search_url = f"{REGISTRY_URL}/v1/agents?q={urllib.parse.quote('Hermes Coder Agent')}"
            with urllib.request.urlopen(search_url) as resp:
                agents = json.loads(resp.read())["agents"]
                if agents:
                    return agents[0]["id"]
            return "a2a:coder-agent"
        raise RuntimeError(f"Registry registration failed ({e.code}): {body}")


def heartbeat_loop(agent_id: str) -> None:
    """Send heartbeats to the registry in a loop."""
    while True:
        try:
            req = urllib.request.Request(
                f"{REGISTRY_URL}/v1/agents/{agent_id}/heartbeat",
                method="POST",
            )
            with urllib.request.urlopen(req) as resp:
                time.sleep(HEARTBEAT_INTERVAL)
        except Exception as e:
            logger.warning("Heartbeat failed: %s (retrying in %ss)", e, HEARTBEAT_INTERVAL)
            time.sleep(HEARTBEAT_INTERVAL)


# ── WebSocket client loop ────────────────────────────────────────────────

async def ws_connect_loop() -> None:
    """Maintain a persistent WebSocket connection to the Registry.

    - Connects to ws://<registry>/v1/agents/{agent_id}/ws
    - Listens for 'task' messages and dispatches them to process_ws_task
    - Sends a 'ping' every 30 seconds
    - Auto-reconnects with exponential backoff on disconnect
    """
    global _ws_session, _ws_connection

    retry_delay = 1.0  # start at 1s, max 60s
    _ws_session = ClientSession()

    while not _ws_close_event.is_set():
        agent_id = _ws_agent_id
        if not agent_id:
            logger.info("WS: waiting for agent_id to be set...")
            await asyncio.sleep(2)
            continue

        ws_url = f"{REGISTRY_WS_URL}/{agent_id}/ws"
        logger.info("WS: connecting to %s", ws_url)

        try:
            ws = await _ws_session.ws_connect(
                ws_url,
                heartbeat=30.0,  # aiohttp keepalive
            )
        except Exception as e:
            logger.warning("WS: connection failed: %s (retry in %.1fs)", e, retry_delay)
            await asyncio.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 60.0)
            continue

        # Connected — reset backoff
        _ws_connection = ws
        retry_delay = 1.0
        logger.info("WS: connected to Registry")

        # Last ping timestamp for our own 30s ping
        last_ping_time = time.time()

        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                    except json.JSONDecodeError:
                        logger.warning("WS: invalid JSON: %s", msg.data[:200])
                        continue

                    msg_type = data.get("type", "")
                    logger.debug("WS: received type=%s id=%s", msg_type, data.get("id", ""))

                    if msg_type == "task":
                        # Spawn task processing as a fire-and-forget task
                        asyncio.create_task(process_ws_task(data))
                    elif msg_type == "ping":
                        # Respond to server ping (some WS frameworks expect this)
                        pass
                    elif msg_type == "close":
                        logger.info("WS: server requested close")
                        break

                elif msg.type == WSMsgType.PING:
                    await ws.pong()
                elif msg.type == WSMsgType.CLOSED:
                    logger.info("WS: connection closed by server")
                    break
                elif msg.type == WSMsgType.ERROR:
                    logger.warning("WS: connection error")
                    break

                # Send our own ping every 30 seconds to keep the connection alive
                now = time.time()
                if now - last_ping_time >= 30.0:
                    try:
                        await ws.send_json({"type": "ping"})
                        logger.debug("WS: sent ping")
                    except Exception:
                        pass
                    last_ping_time = now

        except asyncio.CancelledError:
            logger.info("WS: loop cancelled")
            break
        except Exception as e:
            logger.warning("WS: connection lost: %s (reconnecting in %.1fs)", e, retry_delay)
        finally:
            _ws_connection = None
            if not ws.closed:
                await ws.close()

        # Reconnect with exponential backoff
        await asyncio.sleep(retry_delay)
        retry_delay = min(retry_delay * 2, 60.0)

    # Clean up session on exit
    await _ws_session.close()
    _ws_session = None


async def ws_shutdown() -> None:
    """Signal the WS loop to shut down gracefully."""
    _ws_close_event.set()
    # Force-close the active connection to unblock the loop
    global _ws_connection
    ws = _ws_connection
    if ws and not ws.closed:
        try:
            await ws.send_json({"type": "close"})
        except Exception:
            pass
        await ws.close()
    _ws_connection = None


# ── Main ────────────────────────────────────────────────────────────────────

def create_app() -> web.Application:
    """Create and configure the A2A Coder Agent application."""
    app = web.Application()
    app["started_at"] = time.time()

    app.router.add_get("/.well-known/agent-card.json", handle_agent_card)
    app.router.add_get("/health", handle_health)
    app.router.add_post("/tasks/send", handle_send_task)
    app.router.add_get("/tasks/{taskId}", handle_get_task)
    app.router.add_post("/tasks/{taskId}/cancel", handle_cancel_task)
    app.router.add_get("/skills", handle_list_skills)

    return app


def main() -> None:
    """Entry point: register, start heartbeat, run HTTP + WS concurrently."""
    logger.info("Registering with A2A Registry at %s...", REGISTRY_URL)
    global _ws_agent_id
    _ws_agent_id = register_with_registry()

    import threading
    hb_thread = threading.Thread(target=heartbeat_loop, args=(_ws_agent_id,), daemon=True)
    hb_thread.start()
    logger.info("Heartbeat thread started (interval=%ss)", HEARTBEAT_INTERVAL)

    async def _run() -> None:
        app = create_app()
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, AGENT_HOST, AGENT_PORT)
        await site.start()
        logger.info("A2A Coder Agent HTTP on %s:%s", AGENT_HOST, AGENT_PORT)

        # Start WS loop in background
        ws_task = asyncio.create_task(ws_connect_loop())

        try:
            # Sleep forever — Ctrl+C / SIGINT cancels this
            await asyncio.Event().wait()
        except (asyncio.CancelledError, KeyboardInterrupt):
            logger.info("Shutting down...")
        finally:
            await ws_shutdown()
            if ws_task and not ws_task.done():
                ws_task.cancel()
                try:
                    await ws_task
                except (asyncio.CancelledError, Exception):
                    pass
            await runner.cleanup()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        logger.info("A2A Coder Agent stopped")


if __name__ == "__main__":
    main()