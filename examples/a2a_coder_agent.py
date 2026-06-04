"""A2A-compliant Agent — wraps the Hermes Coder profile.

Implements the Google A2A (Agent-to-Agent) protocol using the A2AClient SDK:

  GET  /.well-known/agent-card.json  — Agent Card (discovery)
  POST /tasks/send                   — submit a task (processed by Hermes CLI)
  GET  /tasks/{id}                   — get task status/result

WebSocket integration with the A2A Registry via the A2AClient SDK:
  - Connects via A2AClient.async_connect_websocket() on startup
  - Receives tasks from Registry via the SDK's dispatch_handler callback
  - Reports progress/results via SDK's async_report_progress/async_report_result
  - Auto-reconnect with exponential backoff (managed by the SDK)

Each task is forwarded to the local Hermes Agent (coder profile) for
real execution — code writing, debugging, PR management, devops, etc.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

from aiohttp import web

# Add project root to path for direct script execution
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from simple_a2a_registry.client import (
    A2AClient,
    A2AClientError,
    RegistryError,
    NotFoundError,
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

# ── Config file paths (overridable via CLI args) ──────────────────────────
_DEFAULT_CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".a2a-coder")
AGENT_CONFIG_PATH: str = os.path.join(_DEFAULT_CONFIG_DIR, "agent.json")


def _ensure_config_dir(path: str) -> None:
    """Create parent dir for *path* if it doesn't exist."""
    parent = os.path.dirname(path)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)


def _load_agent_config(path: str) -> str:
    """Load agent_id from *path*.

    Returns the stored agent_id, or empty string if not found.
    """
    try:
        with open(path) as f:
            data = json.load(f)
        aid = data.get("agent_id", "")
        if aid:
            logger.debug("Loaded agent_id '%s' from %s", aid, path)
        return aid
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return ""


def _save_agent_config(path: str, agent_id: str) -> None:
    """Atomically write agent_id to *path*."""
    _ensure_config_dir(path)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump({"agent_id": agent_id}, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        logger.info("Saved agent_id '%s' to %s", agent_id, path)
    except OSError as e:
        logger.warning("Failed to save agent_id to %s: %s", path, e)


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


def build_agent_card(auth_enabled: bool = True) -> Dict[str, Any]:
    """Build the A2A v1.0 Agent Card for the Coder Agent.

    When *auth_enabled* is True, includes an ``OAuth2SecurityScheme``
    declaring the agent's supported OAuth flows.
    """
    card = {
        "name": "Hermes Coder Agent",
        "description": "An A2A-compliant coding agent powered by the Hermes Coder profile. "
                       "Can write, debug, test, and review code; manage GitHub workflows; "
                       "perform DevOps tasks; generate diagrams and documents; "
                       "analyze data; and retrieve knowledge.",
        "supported_interfaces": [
            {
                "url": AGENT_URL,
                "protocol_binding": "JSONRPC",
                "protocol_version": "1.0",
            },
        ],
        "version": "1.0.0",
        "capabilities": {
            "streaming": False,
            "push_notifications": True,
        },
        "provider": {
            "organization": "Hermes Agent",
            "url": "https://hermes-agent.nousresearch.com",
        },
        "default_input_modes": ["text/plain"],
        "default_output_modes": ["text/plain"],
        "skills": SKILLS,
    }

    if auth_enabled:
        card["security_schemes"] = {
            "registry-oauth": {
                "scheme_type": "oauth2",
                "description": "OAuth 2.1 client_credentials grant for Registry API access",
                "oauth2": {
                    "flows": {
                        "client_credentials": {
                            "token_url": f"{REGISTRY_URL}/auth/token",
                            "scopes": {
                                "task:read": "Read task list and details",
                                "task:write": "Create and modify tasks",
                                "agent:read": "Read agent list and details",
                                "agent:register": "Register new agents",
                            },
                        },
                    },
                },
            },
        }

    return card


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
_active_procs: Dict[str, subprocess.Popen] = {}
_cancel_events: Dict[str, asyncio.Event] = {}


# ── Real Hermes CLI task processing ─────────────────────────────────────────

def _clean_hermes_output(raw: str) -> str:
    """Strip ANSI codes, box-drawing chars, and framing from Hermes output."""
    import re
    # Strip ANSI escape sequences
    text = re.sub(r'\x1b\[[0-9;]*[mK]', '', raw)
    # Strip Unicode box-drawing characters
    text = re.sub(
        r'[─┌┐└┘├┤┬┴┼╭╮╯╰│╱╲╴╵╶╷╸╹╺╻╼╽╾╿▌▐▀▄█░▒▓■□▪▫▲△▼▽◆◇○●◐◑◒◓◔◕★☆☐☑☒♠♣♥♦]',
        '', text,
    )
    lines = []
    skip_prefixes = ("┌─", "└─", "╭─", "╰─", "├─", "─", "  ┌─ Reasoning", "  └─", "  ╭─", "  ╰─")
    in_reasoning = False
    in_hermes_header = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("┌─ Reasoning") or stripped.startswith("╭─ Reasoning"):
            in_reasoning = True
            continue
        if stripped.startswith("└─") or stripped.startswith("╰─"):
            in_reasoning = False
            continue
        if in_reasoning:
            continue
        if stripped.startswith("╭─ ⚕ Hermes") or stripped.startswith("╭─ Hermes"):
            in_hermes_header = True
            continue
        if in_hermes_header and stripped.startswith("╰─"):
            in_hermes_header = False
            continue
        if in_hermes_header:
            continue
        if any(stripped.startswith(p) for p in skip_prefixes):
            continue
        if stripped in ("", "╮", "╯", "│"):
            continue
        lines.append(line)

    text = "\n".join(lines)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


async def process_http_task(task: A2ATask) -> None:
    """Forward an HTTP-submitted task to the local Hermes Coder profile."""
    task.state = TaskState.WORKING
    task.updated_at = time.time()
    logger.info("Spawning Hermes (coder) for HTTP task %s: %s", task.id, task.query[:80])

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
            logger.warning("HTTP task %s timed out", task.id)
            _active_procs.pop(task.id, None)
            return

        _active_procs.pop(task.id, None)

        if proc.returncode != 0:
            stderr_text = stderr.decode("utf-8", errors="replace")[:2000]
            logger.warning("Hermes exited with code %d for HTTP task %s", proc.returncode, task.id)
            task.state = TaskState.FAILED
            task.error = f"Hermes process exited with code {proc.returncode}: {stderr_text}"
            task.updated_at = time.time()
            return

        full_output = stdout.decode("utf-8", errors="replace")
        cleaned = _clean_hermes_output(full_output)

        # Try to extract the session ID from the output
        session_id = ""
        for line in full_output.splitlines():
            if "Resume this session with:" in line:
                session_id = line.strip()
                break

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
        logger.info("HTTP task %s completed (skill=%s, time=%ss, output=%d chars)",
                    task.id, matched_skill, task.artifact["processingTime"], len(cleaned))

    except Exception as e:
        logger.exception("HTTP task %s failed with exception", task.id)
        task.state = TaskState.FAILED
        task.error = str(e)[:2000]
        task.updated_at = time.time()
        _active_procs.pop(task.id, None)


# ── WS task processing via A2AClient SDK ────────────────────────────────────

async def _execute_hermes_cli(query: str) -> Dict[str, Any]:
    """Run Hermes CLI with *query* and return the result dict.

    Returns:
        On success: ``{"success": True, "output": str, "elapsed": float}``
        On failure: ``{"success": False, "error": str, "elapsed": float}``
        On timeout: ``{"success": False, "error": "timeout", "elapsed": float}``
    """
    cmd = [
        "hermes", "chat",
        "-q", query,
        "--profile", HERMES_PROFILE,
        "--max-turns", "30",
        "-Q",
    ]

    started_at = time.time()

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=HERMES_TIMEOUT
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return {
                "success": False,
                "error": f"Task timed out after {HERMES_TIMEOUT}s",
                "elapsed": round(time.time() - started_at, 2),
            }

        if proc.returncode != 0:
            stderr_text = stderr.decode("utf-8", errors="replace")[:2000]
            return {
                "success": False,
                "error": f"Hermes process exited with code {proc.returncode}: {stderr_text}",
                "elapsed": round(time.time() - started_at, 2),
            }

        full_output = stdout.decode("utf-8", errors="replace")
        cleaned = _clean_hermes_output(full_output)

        matched_skill = "software-development"
        query_lower = query.lower()
        for skill in SKILLS:
            skill_words = skill["name"].lower().split()
            if any(w in query_lower for w in skill_words):
                matched_skill = skill["id"]
                break

        return {
            "success": True,
            "output": cleaned or "(Hermes returned no output)",
            "skill": matched_skill,
            "elapsed": round(time.time() - started_at, 2),
        }

    except Exception as e:
        return {
            "success": False,
            "error": str(e)[:2000],
            "elapsed": round(time.time() - started_at, 2),
        }


async def process_ws_task(client: A2AClient, data: Dict[str, Any]) -> None:
    """Process a task received via WebSocket, using A2AClient SDK for reporting.

    Lifecycle:
      1. Send task_progress (working)  → client.async_report_progress()
      2. Run Hermes CLI
      3. Send task_result             → client.async_report_result()
    """
    task_id = data.get("id", "")
    query = (
        data.get("body") or data.get("query") or data.get("title") or ""
    ).strip()
    session_id = data.get("sessionId", "")

    if not task_id or not query:
        logger.warning("Invalid WS task message: missing id or query: %s", data)
        return

    logger.info("WS task %s: starting (query=%s)", task_id, query[:80])

    # ── 1. Report progress (working) ──
    await client.async_report_progress(task_id, status="working")

    # ── 2. Set up cancellation tracking ──
    cancel_event = asyncio.Event()
    _cancel_events[task_id] = cancel_event
    started_at = time.time()

    # ── 3. Run Hermes CLI ──
    result = await _execute_hermes_cli(query)

    # ── 4. Check if externally cancelled ──
    # Note: WS task_cancel from the server is not dispatched by the SDK;
    # external cancellation is handled via the HTTP /tasks/{id}/cancel endpoint
    # which sets the cancel event (if registered in _cancel_events).
    # The Hermes subprocess from _execute_hermes_cli is already cleaned up
    # inside that function, so we just check the event and report accordingly.
    if cancel_event.is_set():
        await client.async_report_result(
            task_id,
            {"text": "(cancelled)"},
            error="Task cancelled by server",
        )
        logger.info("WS task %s was cancelled (after %ss)", task_id,
                     round(time.time() - started_at, 2))
    elif result["success"]:
        await client.async_report_result(
            task_id,
            {
                "text": result["output"],
                "skill": result["skill"],
                "processingTime": result["elapsed"],
            },
        )
        logger.info("WS task %s completed (skill=%s, time=%ss, output=%d chars)",
                    task_id, result["skill"], result["elapsed"], len(result["output"]))
    else:
        await client.async_report_result(
            task_id,
            {"text": result["error"]},
            error=result["error"],
        )
        logger.warning("WS task %s failed (%s)", task_id, result["error"])

    # Clean up
    _cancel_events.pop(task_id, None)


# ── HTTP Handlers ───────────────────────────────────────────────────────────

def _json(data: Any, status: int = 200, headers: Optional[Dict] = None) -> web.Response:
    return web.json_response(data, status=status, headers=headers)


async def handle_agent_card(request: web.Request) -> web.Response:
    """GET /.well-known/agent-card.json — A2A Agent discovery."""
    client = request.app.get("client")
    auth_enabled = client._auth_enabled if client else True
    card = build_agent_card(auth_enabled=auth_enabled)
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
    asyncio.create_task(process_http_task(task))

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

    # Signal cancellation via event (for WS task processing)
    cancel_evt = _cancel_events.get(task_id)
    if cancel_evt:
        cancel_evt.set()

    task.state = TaskState.CANCELED
    task.updated_at = time.time()
    return _json({
        "jsonrpc": "2.0",
        "result": task.to_dict(),
    })


async def handle_list_skills(request: web.Request) -> web.Response:
    """GET /skills — list all available skills."""
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


# ── Background heartbeat task ─────────────────────────────────────────────

async def heartbeat_loop(client: A2AClient, agent_id: str) -> None:
    """Periodically send heartbeats to the registry."""
    while True:
        try:
            await client.async_heartbeat(agent_id)
            logger.debug("Heartbeat sent for agent '%s'", agent_id)
        except Exception as e:
            logger.warning("Heartbeat failed for '%s': %s", agent_id, e)
        await asyncio.sleep(HEARTBEAT_INTERVAL)


# ── Application factory ────────────────────────────────────────────────────

def create_app(client: A2AClient) -> web.Application:
    """Create and configure the A2A Coder Agent application."""
    app = web.Application()
    app["started_at"] = time.time()
    app["client"] = client

    app.router.add_get("/.well-known/agent-card.json", handle_agent_card)
    app.router.add_get("/health", handle_health)
    app.router.add_post("/tasks/send", handle_send_task)
    app.router.add_get("/tasks/{taskId}", handle_get_task)
    app.router.add_post("/tasks/{taskId}/cancel", handle_cancel_task)
    app.router.add_get("/skills", handle_list_skills)

    return app


# ── Main ────────────────────────────────────────────────────────────────────

def main() -> None:
    """Entry point: connect to Registry, register, start services."""
    parser = argparse.ArgumentParser(description="A2A Coder Agent")
    parser.add_argument("--debug", action="store_true", help="Enable DEBUG-level logging")
    parser.add_argument("--log-file", default=None, help="Log to file instead of stderr")
    parser.add_argument("--no-auth", dest="auth", action="store_false", default=True,
                        help="Disable OAuth authentication (default: enabled)")
    parser.add_argument("--client-id", default=os.environ.get("OAUTH_CLIENT_ID", ""),
                        help="OAuth client ID (or $OAUTH_CLIENT_ID)")
    parser.add_argument("--client-secret", default=os.environ.get("OAUTH_CLIENT_SECRET", ""),
                        help="OAuth client secret (or $OAUTH_CLIENT_SECRET)")
    parser.add_argument("--agent-config", default=AGENT_CONFIG_PATH,
                        help=f"Path to agent config file (default: {AGENT_CONFIG_PATH})")
    parser.add_argument("--registry", default=os.environ.get("A2A_REGISTRY_URL", REGISTRY_URL),
                        help="Registry base URL (default: http://localhost:8321, or $A2A_REGISTRY_URL)")
    args = parser.parse_args()

    log_level = logging.DEBUG if args.debug else logging.INFO
    log_kwargs = {
        "level": log_level,
        "format": "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        "datefmt": "%H:%M:%S",
    }
    if args.log_file:
        log_kwargs["filename"] = str(Path(args.log_file).expanduser())
    logging.basicConfig(**log_kwargs)
    logger.setLevel(log_level)
    logging.getLogger("a2a_registry").setLevel(log_level)

    _save_config = args.agent_config

    async def _run() -> None:
        # ── 1. Create the A2AClient SDK instance ──
        auth_enabled = args.auth and bool(args.client_id and args.client_secret)
        async with A2AClient(
            registry_url=args.registry,
            client_id=args.client_id,
            client_secret=args.client_secret,
            auth_enabled=auth_enabled,
        ) as client:
            logger.info("Created A2AClient for %s (auth=%s)", args.registry, auth_enabled)

            # ── 2. Check health ──
            try:
                health = await client.async_health()
                logger.info("Registry health: %s (v%s, uptime=%ss)",
                            health["status"], health["version"], health["uptime_seconds"])
            except Exception as e:
                logger.warning("Registry health check failed: %s", e)
                logger.warning("Continuing anyway...")

            # ── 3. Build Agent Card ──
            agent_card = build_agent_card(auth_enabled=auth_enabled)

            # ── 4. Try to reuse stored agent_id ──
            agent_id = _load_agent_config(_save_config)
            if agent_id:
                logger.info("Found stored agent_id '%s' — validating...", agent_id)
                try:
                    info = await client.async_get_agent(agent_id)
                    if info.get("id"):
                        logger.info("Reusing existing agent_id '%s'", agent_id)
                except (NotFoundError, RegistryError):
                    logger.warning("Stored agent_id '%s' no longer valid — re-registering", agent_id)
                    agent_id = ""
                except Exception as e:
                    logger.warning("Agent lookup failed: %s — will re-register", e)
                    agent_id = ""

            # ── 5. Register (or re-register) ──
            if not agent_id:
                logger.info("Registering with A2A Registry at %s...", args.registry)
                try:
                    agent_id = await client.async_register_agent(agent_card=agent_card)
                    logger.info("Registered as '%s'", agent_id)
                    _save_agent_config(_save_config, agent_id)
                except RegistryError as e:
                    if e.status == 409:
                        logger.info("Agent already registered (409) — finding by name")
                        try:
                            result = await client.async_list_agents(q=agent_card["name"])
                            agents = result.get("agents", [])
                            if agents:
                                agent_id = agents[0]["id"]
                                _save_agent_config(_save_config, agent_id)
                                logger.info("Found existing agent: '%s'", agent_id)
                            else:
                                logger.warning("409 but no agent found — using name as fallback ID")
                                agent_id = agent_card["name"]
                        except Exception as e2:
                            logger.warning("Failed to search for agent: %s", e2)
                            agent_id = agent_card["name"]
                    else:
                        logger.error("Registration failed: %s", e)
                        logger.error("Exiting — cannot continue without registration")
                        return
                except Exception as e:
                    logger.error("Registration failed: %s", e)
                    return

            # ── 6. Set up dispatch handler for WS tasks ──
            async def on_task(data: Dict[str, Any]) -> None:
                """Dispatch handler called by the SDK for each incoming WS task."""
                asyncio.create_task(process_ws_task(client, data))

            client.dispatch_handler = on_task
            logger.info("Dispatch handler set up")

            # ── 7. Connect WebSocket ──
            try:
                await client.async_connect_websocket(agent_id)
                logger.info("WebSocket connecting for agent '%s' (background)", agent_id)
            except Exception as e:
                logger.warning("WebSocket connection failed: %s", e)

            # ── 8. Start heartbeat loop ──
            asyncio.create_task(heartbeat_loop(client, agent_id))
            logger.info("Heartbeat loop started (interval=%ss)", HEARTBEAT_INTERVAL)

            # ── 9. Start HTTP server ──
            app = create_app(client)
            runner = web.AppRunner(app)
            await runner.setup()
            site = web.TCPSite(runner, AGENT_HOST, AGENT_PORT)
            await site.start()
            logger.info("A2A Coder Agent HTTP on %s:%s", AGENT_HOST, AGENT_PORT)

            # ── 10. Wait forever (Ctrl+C to exit) ──
            try:
                await asyncio.Event().wait()
            except (asyncio.CancelledError, KeyboardInterrupt):
                logger.info("Shutting down...")
            finally:
                await runner.cleanup()
                logger.info("A2A Coder Agent stopped")

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        logger.info("A2A Coder Agent stopped")


if __name__ == "__main__":
    main()