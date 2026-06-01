#!/usr/bin/env python3
"""A2A-compliant Agent — wraps OpenCode / Claude Code CLI as external coding agents.

Implements the Google A2A (Agent-to-Agent) protocol:

    GET  /.well-known/agent-card.json  — Agent Card (discovery)
    POST /tasks/send                   — submit a task (processed by CLI)
    GET  /tasks/{id}                   — get task status/result

WebSocket integration with the A2A Registry:
  - Connects to ws://<registry>/v1/agents/{agent_id}/ws on startup
  - Receives tasks from Registry via WebSocket (type: "task")
  - Reports progress and results via WebSocket (task_progress / task_result)
  - Auto-reconnects with exponential backoff on disconnect

Supported backends:
  - opencode run (primary): one-shot coding task via OpenCode CLI
  - claude -p (secondary): one-shot task via Claude Code CLI

Usage:
    # OpenCode agent (default)
    python examples/a2a_opencode_agent.py --backend opencode

    # Claude Code agent
    python examples/a2a_opencode_agent.py --backend claude

    # With Registry auth
    python examples/a2a_opencode_agent.py --backend opencode --auth \\
        --client-id my-agent --client-secret secret-xxx

    # With custom working directory
    python examples/a2a_opencode_agent.py --backend opencode --cwd /path/to/project
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import shlex
import shutil
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from aiohttp import (
    web,
    ClientResponseError,
    ClientSession,
    ClientTimeout,
    ClientWebSocketResponse,
    WSMsgType,
)

logger = logging.getLogger("a2a.cli-agent")

# ── Custom exceptions ────────────────────────────────────────────────────────


class WSNotFoundError(Exception):
    """Raised when WebSocket connection fails with 404 (agent not found)."""


# ── Default Configuration ──────────────────────────────────────────────────

REGISTRY_URL = "http://localhost:8321"
AGENT_PORT = 9002
AGENT_HOST = "0.0.0.0"
HEARTBEAT_INTERVAL = 30  # seconds
CLI_TIMEOUT = 600  # max seconds for a single CLI task (10 min)

_DEFAULT_CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".a2a-cli-agent")
AUTH_CONFIG_PATH: str = os.path.join(_DEFAULT_CONFIG_DIR, "auth.json")
AGENT_CONFIG_PATH: str = os.path.join(_DEFAULT_CONFIG_DIR, "agent.json")

# Loaded at startup
OAUTH_CLIENT_ID = ""
OAUTH_CLIENT_SECRET = ""
OAUTH_ACCESS_TOKEN = ""
OAUTH_TOKEN_EXPIRES_AT = 0.0

REGISTRY_AUTH_ENABLED = True


# ── Config persistence helpers ──────────────────────────────────────────────


def _ensure_config_dir(path: str) -> None:
    parent = os.path.dirname(path)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)


def _load_auth_config(path: str) -> dict:
    """Load auth config from *path*. Returns dict with client_id and client_secret."""
    global OAUTH_CLIENT_ID, OAUTH_CLIENT_SECRET

    # Env vars take precedence
    env_id = os.environ.get("OAUTH_CLIENT_ID", "")
    env_secret = os.environ.get("OAUTH_CLIENT_SECRET", "")
    if env_id and env_secret:
        OAUTH_CLIENT_ID = env_id
        OAUTH_CLIENT_SECRET = env_secret
        return {"client_id": env_id, "client_secret": env_secret}

    try:
        with open(path) as f:
            data = json.load(f)
        OAUTH_CLIENT_ID = data.get("client_id", "")
        OAUTH_CLIENT_SECRET = data.get("client_secret", "")
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        OAUTH_CLIENT_ID = ""
        OAUTH_CLIENT_SECRET = ""

    return {"client_id": OAUTH_CLIENT_ID, "client_secret": OAUTH_CLIENT_SECRET}


def _load_agent_config(path: str) -> str:
    try:
        with open(path) as f:
            data = json.load(f)
        return data.get("agent_id", "")
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return ""


def _save_agent_config(path: str, agent_id: str) -> None:
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


# ── Backend definitions ─────────────────────────────────────────────────────

BACKENDS = {
    "opencode": {
        "name": "OpenCode Agent",
        "description": "An A2A-compliant coding agent powered by OpenCode CLI. "
                       "Can write, debug, test, and review code across multiple languages.",
        "binary": "",  # resolved at runtime via shutil.which
        "cmd_template": ["opencode", "run", "--format", "json"],
        "max_turns_flag": None,
    },
    "claude": {
        "name": "Claude Code Agent",
        "description": "An A2A-compliant coding agent powered by Claude Code CLI. "
                       "Can write, debug, test, and review code across multiple languages.",
        "binary": "",
        "cmd_template": ["claude", "-p"],
        "max_turns_flag": None,
    },
}


def _resolve_backend(backend_name: str) -> dict:
    """Resolve a backend name to its config, verifying the binary exists.

    Returns:
        Backend config dict with resolved ``binary`` absolute path.

    Raises:
        SystemExit: If the backend binary is not found.
    """
    cfg = dict(BACKENDS[backend_name])
    binary_name = cfg["cmd_template"][0]
    binary_path = shutil.which(binary_name)
    if not binary_path:
        logger.error(
            "Backend '%s' requires '%s' which is not installed. "
            "Install it first, e.g.: "
            "`npm install -g @anthropic-ai/claude-code` or "
            "`brew install opencode`.",
            backend_name, binary_name,
        )
        sys.exit(1)
    cfg["binary"] = binary_path
    return cfg


# ── Agent Card ──────────────────────────────────────────────────────────────

CODE_SKILLS = [
    {
        "id": "software-development",
        "name": "Software Development",
        "description": "Write, debug, test, and review code across multiple "
                       "languages (Python, JavaScript, TypeScript, Go, Rust, etc.)",
        "tags": ["code", "dev", "debug"],
    },
    {
        "id": "code-review",
        "name": "Code Review",
        "description": "Review pull requests, provide inline feedback, "
                       "check code quality and security",
        "tags": ["review", "code-quality"],
    },
    {
        "id": "refactoring",
        "name": "Code Refactoring",
        "description": "Restructure and improve existing code without "
                       "changing external behaviour",
        "tags": ["refactor", "cleanup"],
    },
    {
        "id": "testing",
        "name": "Testing",
        "description": "Write and run unit tests, integration tests, "
                       "and end-to-end tests",
        "tags": ["test", "unit-test", "integration"],
    },
    {
        "id": "documentation",
        "name": "Documentation Generation",
        "description": "Generate markdown docs, API references, "
                       "architecture diagrams, README files",
        "tags": ["docs", "readme"],
    },
]


def build_agent_card(backend_name: str) -> Dict[str, Any]:
    """Build the A2A v1.0 Agent Card for this CLI agent.

    Args:
        backend_name: The backend identifier ("opencode" or "claude").

    Returns:
        An A2A v1.0 Agent Card dict.
    """
    cfg = BACKENDS[backend_name]
    card = {
        "name": cfg["name"],
        "description": cfg["description"],
        "supported_interfaces": [
            {
                "url": f"http://localhost:{AGENT_PORT}",
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
            "organization": "A2A Registry",
            "url": "https://github.com/your-org/simple-a2a-registry",
        },
        "default_input_modes": ["text/plain"],
        "default_output_modes": ["text/plain"],
        "skills": CODE_SKILLS,
    }

    if REGISTRY_AUTH_ENABLED:
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


# ── OAuth Token Management ──────────────────────────────────────────────────


def _ensure_token() -> str:
    """Get or refresh an OAuth 2.1 access token (sync)."""
    global OAUTH_ACCESS_TOKEN, OAUTH_TOKEN_EXPIRES_AT

    if not REGISTRY_AUTH_ENABLED:
        return ""

    if not OAUTH_CLIENT_ID or not OAUTH_CLIENT_SECRET:
        logger.warning(
            "OAUTH_CLIENT_ID / OAUTH_CLIENT_SECRET not set — "
            "cannot obtain token."
        )
        return ""

    if OAUTH_ACCESS_TOKEN and time.time() < OAUTH_TOKEN_EXPIRES_AT - 30:
        return OAUTH_ACCESS_TOKEN

    body = (
        f"grant_type=client_credentials"
        f"&client_id={shlex.quote(OAUTH_CLIENT_ID)}"
        f"&client_secret={shlex.quote(OAUTH_CLIENT_SECRET)}"
        f"&scope=task:read+task:write+agent:read+agent:register"
    ).encode()

    import urllib.request
    from urllib.error import HTTPError

    req = urllib.request.Request(
        f"{REGISTRY_URL}/auth/token",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req) as resp:
            result = json.loads(resp.read())
    except HTTPError as e:
        logger.warning("OAuth token request failed (HTTP %s)", e.code)
        return ""
    except Exception as e:
        logger.warning("OAuth token request failed: %s", e)
        return ""

    OAUTH_ACCESS_TOKEN = result["access_token"]
    OAUTH_TOKEN_EXPIRES_AT = time.time() + result.get("expires_in", 3600)
    return OAUTH_ACCESS_TOKEN


def _auth_header() -> Dict[str, str]:
    if not REGISTRY_AUTH_ENABLED:
        return {}
    token = _ensure_token()
    return {"Authorization": f"Bearer {token}"}


# ── A2A Task States ─────────────────────────────────────────────────────────


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
            "createdAt": datetime.fromtimestamp(
                self.created_at, tz=timezone.utc
            ).isoformat(),
            "updatedAt": datetime.fromtimestamp(
                self.updated_at, tz=timezone.utc
            ).isoformat(),
        }
        if self.artifact:
            d["artifact"] = self.artifact
        if self.error:
            d["error"] = self.error
        return d


# ── In-memory task store ────────────────────────────────────────────────────

_tasks: Dict[str, A2ATask] = {}
_active_procs: Dict[str, subprocess.Popen] = {}

# ── WS task lifecycle tracking ───────────────────────────────────────────────
# asyncio.Event per task_id — set when a task_cancel is received
_cancel_events: Dict[str, asyncio.Event] = {}
# asyncio.Task for running process_ws_task — allows direct cancellation
_running_ws_tasks: Dict[str, asyncio.Task] = {}


async def _periodic_progress(
    task_id: str,
    cancel_event: asyncio.Event,
    start_time: float,
    *,
    interval: float = 15.0,
) -> None:
    """Send ``task_progress`` every *interval* seconds while the task runs.

    Percentage is estimated from elapsed time relative to ``CLI_TIMEOUT``
    (capped at 99 % so it never claims completion before the real result).
    Checks ``cancel_event`` on each tick and exits silently if cancellation
    was requested.
    """
    while True:
        await asyncio.sleep(interval)
        if cancel_event.is_set():
            return
        elapsed = time.time() - start_time
        pct = min(int(elapsed / CLI_TIMEOUT * 100), 99)
        ok = await _send_ws_json({
            "type": "task_progress",
            "id": task_id,
            "status": "working",
            "progress": pct,
        })
        if not ok:
            logger.debug("periodic_progress[%s]: WS gone, stopping reporter", task_id)
            return
        logger.debug("periodic_progress[%s]: sent %d%%", task_id, pct)


# ── WebSocket connection state ──────────────────────────────────────────────

_ws_session: Optional[ClientSession] = None
_ws_connection: Optional[ClientWebSocketResponse] = None
_ws_agent_id: str = ""

# ── CLI task execution ──────────────────────────────────────────────────────


def _resolve_working_dir(cwd: str, task_workspace: str = "") -> str:
    """Resolve the working directory for a CLI task.

    Priority:
    1. ``task_workspace`` (from WS task metadata)
    2. ``cwd`` (from CLI ``--cwd`` argument)
    3. Current directory (fallback)
    """
    if task_workspace and os.path.isdir(task_workspace):
        return task_workspace
    if cwd and os.path.isdir(cwd):
        return cwd
    return os.getcwd()


def _parse_opencode_json_output(raw: str) -> Dict[str, Any]:
    """Parse JSON output from ``opencode run --format json``.

    OpenCode with ``--format json`` outputs one JSON event per line, ending
    with a result event.  We extract the final result's content.

    Returns:
        A dict with at least ``text``, and optionally ``sessionId``.
    """
    lines = raw.strip().splitlines()
    result_text_parts = []
    session_id = ""

    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            result_text_parts.append(line)
            continue

        event_type = event.get("type", "")
        if event_type == "result":
            content = event.get("content", "") or event.get("text", "") or ""
            if content:
                result_text_parts.append(content)
        elif event_type == "error":
            content = event.get("content", "") or event.get("text", "") or str(event)
            result_text_parts.append(f"[Error] {content}")
        elif event_type == "session":
            session_id = event.get("id", "") or event.get("sessionId", "")
        elif event_type == "text" or event_type == "message":
            content = event.get("content", "") or event.get("text", "") or ""
            if content:
                result_text_parts.append(content)

    # If no JSON events were parsed, treat entire output as plain text
    if not result_text_parts and raw.strip():
        result_text_parts = [raw.strip()]

    return {
        "text": "\n".join(result_text_parts).strip(),
        "sessionId": session_id,
    }


def _parse_claude_output(raw: str) -> Dict[str, Any]:
    """Parse output from ``claude -p <prompt>``.

    Claude Code in print mode outputs the response directly as text.

    Returns:
        A dict with at least ``text``.
    """
    return {"text": raw.strip()}


def _parse_cli_output(raw: str, backend: str) -> Dict[str, Any]:
    """Route output parsing to the correct backend parser."""
    if backend == "opencode":
        return _parse_opencode_json_output(raw)
    return _parse_claude_output(raw)


async def run_cli_task(
    task_id: str,
    query: str,
    backend_cfg: dict,
    *,
    cwd: str = "",
    task_workspace: str = "",
    timeout: int = CLI_TIMEOUT,
) -> Dict[str, Any]:
    """Execute a task via the backend CLI.

    Args:
        task_id:      Unique task identifier (used for cancellation tracking).
        query:        The task query / prompt.
        backend_cfg:  Resolved backend configuration.
        cwd:          Default working directory.
        task_workspace: Task-specific workspace path (overrides cwd).
        timeout:      Max execution time in seconds.

    Returns:
        Dict with ``text`` (output), ``sessionId`` (optional), ``returncode``.
    """
    work_dir = _resolve_working_dir(cwd, task_workspace)
    cmd = list(backend_cfg["cmd_template"])

    if backend_cfg.get("max_turns_flag"):
        cmd.extend([backend_cfg["max_turns_flag"], "30"])

    # Append the query as the last argument
    if backend_cfg["cmd_template"][0] == "opencode":
        # opencode run [message] — query as positional
        cmd.append(query)
    else:
        # claude -p <prompt>
        cmd.append(query)

    logger.info(
        "Running CLI task: %s (cwd=%s, timeout=%ss)",
        " ".join(cmd[:3]) + " ...",
        work_dir,
        timeout,
    )

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=work_dir,
    )
    # Register for cancellation support
    _active_procs[task_id] = proc

    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=timeout
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        _active_procs.pop(task_id, None)
        return {
            "text": "",
            "error": f"CLI task timed out after {timeout}s",
            "returncode": -1,
            "sessionId": "",
        }
    except asyncio.CancelledError:
        proc.kill()
        await proc.wait()
        _active_procs.pop(task_id, None)
        raise

    _active_procs.pop(task_id, None)

    returncode = proc.returncode or 0
    stdout_text = stdout.decode("utf-8", errors="replace")
    stderr_text = stderr.decode("utf-8", errors="replace")

    parsed = _parse_cli_output(stdout_text, backend_cfg["cmd_template"][0])

    if returncode != 0:
        parsed["error"] = stderr_text[:2000] or f"CLI exited with code {returncode}"
        parsed["returncode"] = returncode
    else:
        parsed["returncode"] = 0
        if stderr_text.strip():
            parsed["stderr"] = stderr_text[:500]

    logger.info(
        "CLI task completed (rc=%s, output=%d chars)",
        returncode,
        len(parsed.get("text", "")),
    )
    return parsed


# ── WebSocket communication ─────────────────────────────────────────────────


async def _send_ws_json(msg: Dict[str, Any]) -> bool:
    """Send a JSON message via the active WebSocket connection."""
    global _ws_connection
    ws = _ws_connection
    if ws is None or ws.closed:
        return False
    try:
        await ws.send_json(msg)
        return True
    except Exception as e:
        logger.warning("WS send failed: %s", e)
        return False


async def process_ws_task(
    task_msg: Dict[str, Any],
    backend_cfg: dict,
    *,
    cwd: str = "",
) -> None:
    """Process a task received via WebSocket and report results via WS.

    Lifecycle (per spec):
      1. task_ack  (immediate — 0.5s within receipt)  → status=accepted
      2. task_progress  (on execution start, then every 15s)  → status=working + progress%
      3. task_complete  (on success)  → status=completed + result + metrics
      4. task_fail      (on error)    → status=failed + error + code
      5. task_cancel    (external signal)  → stop execution, clean up, report canceled

    Supports two message formats:

    **A2A-style (direct query)** — ``tasks/send`` endpoint:
      {"type":"task","id":"uuid","query":"...","sessionId":"..."}

    **Kanban-style (dispatcher)** — Kanban V2 orchestration dispatch:
      {"type":"task","id":"uuid","title":"say hello","body":"...",
       "assignee":"...","workspace_path":"","kanban":true}
    """
    task_id = task_msg.get("id", "")
    query = (
        task_msg.get("body")
        or task_msg.get("query")
        or task_msg.get("title")
        or ""
    ).strip()
    session_id = task_msg.get("sessionId", "")
    workspace_path = task_msg.get("workspace_path", "")

    if not task_id or not query:
        logger.warning("Invalid WS task: missing id or query: %s", task_msg)
        return

    # ── 1. task_ack: acknowledge receipt immediately ──────────────────────
    await _send_ws_json({
        "type": "task_ack",
        "id": task_id,
        "status": "accepted",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
    logger.debug("WS task %s: sent task_ack (accepted)", task_id)

    # ── 2. Set up cancellation tracking ───────────────────────────────────
    cancel_event = asyncio.Event()
    _cancel_events[task_id] = cancel_event
    started_at = time.time()

    # ── 3. task_progress: signal that work has started ────────────────────
    await _send_ws_json({
        "type": "task_progress",
        "id": task_id,
        "status": "working",
        "progress": 0,
    })

    # ── 4. Periodic progress reporter (every 15s) ────────────────────────
    progress_task = asyncio.create_task(
        _periodic_progress(task_id, cancel_event, started_at, interval=15.0),
    )

    # ── 5. Execute via CLI backend ────────────────────────────────────────
    try:
        result = await run_cli_task(
            task_id,
            query,
            backend_cfg,
            cwd=cwd,
            task_workspace=workspace_path,
        )

        # ── 6. Clean up progress reporter ────────────────────────────────
        cancel_event.set()
        await asyncio.wait_for(progress_task, timeout=5.0)
        _cancel_events.pop(task_id, None)
        _running_ws_tasks.pop(task_id, None)

        elapsed = round(time.time() - started_at, 2)
        error = result.get("error")
        returncode = result.get("returncode", 0)

        # Check if task was externally cancelled
        if cancel_event.is_set():
            await _send_ws_json({
                "type": "task_fail",
                "id": task_id,
                "status": "canceled",
                "error": "Task cancelled by server",
                "code": "CANCELED",
                "metrics": {"elapsed_seconds": elapsed},
            })
            logger.info("WS task %s was cancelled (%ss)", task_id, elapsed)
            return

        if error or returncode != 0:
            await _send_ws_json({
                "type": "task_fail",
                "id": task_id,
                "status": "failed",
                "error": error or f"CLI exited with code {returncode}",
                "code": f"EXIT_{returncode}" if returncode else "EXECUTION_ERROR",
                "result": {"text": result.get("text", "")},
                "metrics": {"elapsed_seconds": elapsed},
            })
            logger.warning("WS task %s failed after %ss", task_id, elapsed)
        else:
            await _send_ws_json({
                "type": "task_complete",
                "id": task_id,
                "status": "completed",
                "result": {
                    "text": result.get("text", ""),
                    "sessionId": result.get("sessionId", session_id),
                },
                "metrics": {
                    "elapsed_seconds": elapsed,
                    "output_chars": len(result.get("text", "")),
                },
            })
            text_len = len(result.get("text", ""))
            logger.info("WS task %s completed after %ss (output=%d chars)", task_id, elapsed, text_len)

    except Exception as e:
        logger.exception("WS task %s failed with exception", task_id)
        cancel_event.set()
        if not progress_task.done():
            await asyncio.wait_for(progress_task, timeout=5.0)
        _cancel_events.pop(task_id, None)
        _running_ws_tasks.pop(task_id, None)
        elapsed = round(time.time() - started_at, 2)
        await _send_ws_json({
            "type": "task_fail",
            "id": task_id,
            "status": "failed",
            "error": str(e)[:2000],
            "code": "EXCEPTION",
            "metrics": {"elapsed_seconds": elapsed},
        })


# ── HTTP Handlers ───────────────────────────────────────────────────────────


def _json(data: Any, status: int = 200, headers: Optional[Dict] = None) -> web.Response:
    return web.json_response(data, status=status, headers=headers)


async def handle_agent_card(request: web.Request) -> web.Response:
    """GET /.well-known/agent-card.json — A2A Agent discovery."""
    backend_name = request.app.get("backend_name", "opencode")
    card = build_agent_card(backend_name)
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

    backend_cfg = request.app.get("backend_cfg", {})
    cwd = request.app.get("cwd", "")

    asyncio.create_task(_process_http_task(task, backend_cfg, cwd=cwd))

    return _json({
        "jsonrpc": "2.0",
        "result": task.to_dict(),
    }, status=201)


async def _process_http_task(
    task: A2ATask,
    backend_cfg: dict,
    *,
    cwd: str = "",
) -> None:
    """Process a task received via HTTP and update its state in-memory."""
    task.state = TaskState.WORKING
    task.updated_at = time.time()

    result = await run_cli_task(task.id, task.query, backend_cfg, cwd=cwd)

    error = result.get("error")
    if error:
        task.state = TaskState.FAILED
        task.error = error[:2000]
    else:
        task.state = TaskState.COMPLETED
        task.artifact = {
            "parts": [{"text": result.get("text", "")}],
            "sessionId": result.get("sessionId", task.session_id),
            "processingTime": round(time.time() - task.created_at, 2),
        }

    task.updated_at = time.time()


async def handle_get_task(request: web.Request) -> web.Response:
    """GET /tasks/{taskId} — get task status and result."""
    task_id = request.match_info.get("taskId", "")
    task = _tasks.get(task_id)
    if task is None:
        return _json({
            "jsonrpc": "2.0",
            "error": {"code": -32602, "message": f"Task '{task_id}' not found"},
        }, status=404)
    return _json({
        "jsonrpc": "2.0",
        "result": task.to_dict(),
    })


# ── HTTP Server ─────────────────────────────────────────────────────────────


async def _http_server(
    backend_cfg: dict,
    *,
    host: str = AGENT_HOST,
    port: int = AGENT_PORT,
    cwd: str = "",
) -> web.TCPSite:
    """Start the A2A HTTP server with agent discovery and task endpoints."""
    app = web.Application()
    app["backend_name"] = backend_cfg["cmd_template"][0]
    app["backend_cfg"] = backend_cfg
    app["cwd"] = cwd

    app.router.add_get("/.well-known/agent-card.json", handle_agent_card)
    app.router.add_post("/tasks/send", handle_send_task)
    app.router.add_get("/tasks/{taskId}", handle_get_task)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    logger.info("HTTP server running on http://%s:%d", host, port)
    return site


# ── Registry integration (direct HTTP, no SDK dependency) ───────────────────


async def _http_request(
    method: str,
    url: str,
    *,
    json_data: Optional[Dict] = None,
    headers: Optional[Dict] = None,
    timeout: int = 30,
) -> Tuple[int, Dict[str, Any]]:
    """Make an HTTP request with aiohttp.

    Returns (status_code, body_dict).
    """
    hdrs = {"Content-Type": "application/json"}
    if headers:
        hdrs.update(headers)

    async with ClientSession(headers=hdrs) as session:
        try:
            async with session.request(
                method, url,
                json=json_data,
                timeout=ClientTimeout(total=timeout),
            ) as resp:
                status = resp.status
                try:
                    body = await resp.json()
                except Exception:
                    text = await resp.text()
                    body = {"detail": text[:500]}
                return status, body
        except Exception as e:
            return 0, {"detail": str(e)}


async def _register_with_registry(
    backend_cfg: dict,
    agent_id: str = "",
) -> str:
    """Register (or re-register) this agent with the A2A Registry.

    Args:
        backend_cfg:  Resolved backend configuration.
        agent_id:     Previously stored agent ID (for re-registration).

    Returns:
        The agent ID assigned by the Registry.
    """
    card = build_agent_card(backend_cfg["cmd_template"][0])
    headers = _auth_header()

    # If we have a stored agent_id, try GET first to see if it still exists
    if agent_id:
        status, body = await _http_request(
            "GET", f"{REGISTRY_URL}/v1/agents/{agent_id}",
            headers=headers,
        )
        if status == 200:
            logger.info("Agent '%s' still exists in Registry, reusing", agent_id)
            return agent_id
        logger.info("Agent '%s' not found in Registry, re-registering", agent_id)

    # Register fresh
    status, body = await _http_request(
        "POST", f"{REGISTRY_URL}/v1/agents",
        json_data=card,
        headers=headers,
    )

    if status in (200, 201):
        new_id = body.get("id", "")
        if new_id:
            logger.info("Registered agent '%s' with Registry", new_id)
            _save_agent_config(AGENT_CONFIG_PATH, new_id)
            return new_id
    elif status == 409:
        # Conflict — search by name
        name = card.get("name", "")
        import urllib.parse
        search_url = f"{REGISTRY_URL}/v1/agents?q={urllib.parse.quote(name)}"
        s_status, s_body = await _http_request(
            "GET", search_url, headers=headers,
        )
        if s_status == 200:
            agents = s_body.get("agents", [])
            if agents:
                found_id = agents[0].get("id", "")
                logger.info(
                    "Found existing agent '%s' as '%s'", name, found_id
                )
                _save_agent_config(AGENT_CONFIG_PATH, found_id)
                return found_id

    logger.error(
        "Failed to register agent: HTTP %s: %s", status, body.get("detail", body)
    )
    return ""


async def _heartbeat_loop(agent_id: str) -> None:
    """Periodically heartbeat the Registry to keep the agent alive."""
    while True:
        await asyncio.sleep(HEARTBEAT_INTERVAL)
        if not agent_id:
            continue
        try:
            headers = _auth_header()
            status, body = await _http_request(
                "POST",
                f"{REGISTRY_URL}/v1/agents/{agent_id}/heartbeat",
                headers=headers,
                timeout=10,
            )
            if status in (200, 203):
                logger.debug("Heartbeat OK for '%s'", agent_id)
            elif status == 404:
                logger.warning(
                    "Agent '%s' not found on heartbeat — re-registering", agent_id,
                )
                new_id = await _register_with_registry(
                    {"cmd_template": ["opencode"]}, agent_id
                )
                if new_id:
                    agent_id = new_id
            else:
                logger.warning(
                    "Heartbeat HTTP %s for '%s': %s",
                    status, agent_id, body.get("detail", body),
                )
        except Exception as e:
            logger.warning("Heartbeat error for '%s': %s", agent_id, e)


async def _ws_reconnect_loop(
    agent_id: str,
    backend_cfg: dict,
    *,
    cwd: str = "",
) -> None:
    """Background loop: connect WebSocket, listen for tasks, reconnect on error.

    Implements exponential backoff and automatic re-registration on 404.
    """
    ws_base = REGISTRY_URL.replace("http://", "ws://").replace("https://", "wss://")
    ws_url = f"{ws_base}/v1/agents/{agent_id}/ws"
    delay = 1.0
    max_delay = 60.0

    while True:
        try:
            await _ws_connect_single(ws_url, agent_id, backend_cfg, cwd=cwd)
            # Connected successfully
            delay = 1.0
        except asyncio.CancelledError:
            break
        except WSNotFoundError:
            # Agent doesn't exist — re-register
            logger.warning(
                "Agent '%s' not found on WS reconnect — re-registering", agent_id,
            )
            try:
                backends = BACKENDS.get(backend_cfg["cmd_template"][0], BACKENDS["opencode"])
                cfg = _resolve_backend(backend_cfg["cmd_template"][0])
                new_id = await _register_with_registry(cfg, "")
                if new_id:
                    agent_id = new_id
                    ws_url = f"{ws_base}/v1/agents/{new_id}/ws"
                    delay = 1.0
            except Exception as e:
                logger.error("Re-registration failed: %s", e)
                await asyncio.sleep(delay)
                delay = min(delay * 2.0, max_delay)
        except Exception as e:
            logger.warning(
                "WS error for '%s': %s — reconnecting in %.1fs",
                agent_id, e, delay,
            )
            await asyncio.sleep(delay)
            delay = min(delay * 2.0, max_delay)


async def _ws_connect_single(
    ws_url: str,
    agent_id: str,
    backend_cfg: dict,
    *,
    cwd: str = "",
) -> None:
    """Establish a single WebSocket connection and listen for messages."""
    global _ws_session, _ws_connection

    params: Dict[str, str] = {}
    if REGISTRY_AUTH_ENABLED:
        token = _ensure_token()
        if token:
            params["token"] = token

    if _ws_session is None or _ws_session.closed:
        _ws_session = ClientSession()

    logger.info("Connecting WebSocket for agent '%s'", agent_id)

    try:
        ws = await _ws_session.ws_connect(
            ws_url,
            params=params if params else None,
            heartbeat=30,
        )
    except ClientResponseError as e:
        if e.status == 404:
            raise WSNotFoundError(f"Agent '{agent_id}' not found") from e
        logger.warning("WS connect failed for '%s': %s", agent_id, e)
        raise
    except Exception as e:
        logger.warning("WS connect failed for '%s': %s", agent_id, e)
        raise

    _ws_connection = ws
    logger.info("WebSocket connected for agent '%s'", agent_id)

    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                except json.JSONDecodeError:
                    continue

                msg_type = data.get("type", "")
                if msg_type == "pong":
                    continue
                elif msg_type == "task":
                    logger.info(
                        "Received WS task '%s' for '%s'",
                        data.get("id", "?"), agent_id,
                    )
                    task = asyncio.create_task(
                        process_ws_task(data, backend_cfg, cwd=cwd)
                    )
                    _running_ws_tasks[data.get("id", "")] = task
                elif msg_type == "task_cancel":
                    cancel_id = data.get("id", "")
                    if cancel_id:
                        logger.info(
                            "Received task_cancel for '%s'", cancel_id,
                        )
                        # Signal cancellation via event
                        cancel_ev = _cancel_events.get(cancel_id)
                        if cancel_ev is not None:
                            cancel_ev.set()
                        # Also try to cancel the asyncio task directly
                        running_task = _running_ws_tasks.pop(cancel_id, None)
                        if running_task is not None and not running_task.done():
                            running_task.cancel()
                            logger.info("Cancelled WS task '%s'", cancel_id)
                        # Kill subprocess if tracked
                        proc = _active_procs.pop(cancel_id, None)
                        if proc:
                            try:
                                proc.kill()
                            except Exception:
                                pass
                elif msg_type == "close":
                    break
            elif msg.type == WSMsgType.ERROR:
                logger.error(
                    "WS error for '%s': %s", agent_id, ws.exception()
                )
                break
            elif msg.type == WSMsgType.CLOSED:
                break
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.error("WS listen error for '%s': %s", agent_id, e)
        raise
    finally:
        _ws_connection = None
        if not ws.closed:
            await ws.close()
        raise ConnectionError(f"WebSocket disconnected for '{agent_id}'")


# ── Main entry point ────────────────────────────────────────────────────────


async def amain(args: argparse.Namespace) -> None:
    """Async main — start HTTP server, register, connect WS, heartbeat."""
    global REGISTRY_URL, AGENT_PORT, AGENT_HOST, REGISTRY_AUTH_ENABLED
    global OAUTH_CLIENT_ID, OAUTH_CLIENT_SECRET
    global CLI_TIMEOUT

    # Apply CLI overrides
    if args.registry:
        REGISTRY_URL = args.registry.rstrip("/")
    if args.port:
        AGENT_PORT = args.port
    if args.host:
        AGENT_HOST = args.host
    if args.timeout:
        CLI_TIMEOUT = args.timeout
    REGISTRY_AUTH_ENABLED = args.auth

    # Load auth config
    if args.auth:
        auth = _load_auth_config(args.auth_config)
        OAUTH_CLIENT_ID = auth.get("client_id", "")
        OAUTH_CLIENT_SECRET = auth.get("client_secret", "")
        if args.client_id:
            OAUTH_CLIENT_ID = args.client_id
        if args.client_secret:
            OAUTH_CLIENT_SECRET = args.client_secret

    # Resolve backend
    backend_cfg = _resolve_backend(args.backend)
    logger.info(
        "Starting %s (backend=%s, binary=%s)",
        BACKENDS[args.backend]["name"],
        args.backend,
        backend_cfg["binary"],
    )

    # Start HTTP server
    site = await _http_server(backend_cfg, host=AGENT_HOST, port=AGENT_PORT, cwd=args.cwd)

    # Load persisted agent_id
    agent_id = _load_agent_config(args.agent_config)
    if args.agent_id:
        agent_id = args.agent_id

    # Register with the Registry
    agent_id = await _register_with_registry(backend_cfg, agent_id)
    if not agent_id:
        logger.error("Failed to register agent — exiting")
        return

    logger.info("Agent ID: %s", agent_id)

    # Start heartbeat loop
    asyncio.create_task(_heartbeat_loop(agent_id))

    # Connect WebSocket (this manages reconnection internally)
    ws_base = REGISTRY_URL.replace("http://", "ws://").replace("https://", "wss://")
    ws_url = f"{ws_base}/v1/agents/{agent_id}/ws"

    # Run WS reconnect loop in background task
    ws_task = asyncio.create_task(
        _ws_reconnect_loop(agent_id, backend_cfg, cwd=args.cwd)
    )

    # Also register the WS task handler for HTTP-triggered WS tasks
    # via the dispatch endpoint on the Registry
    logger.info(
        "Agent ready: http://%s:%d  |  %s/v1/agents/%s",
        AGENT_HOST, AGENT_PORT, REGISTRY_URL, agent_id,
    )

    # Keep running until interrupted
    try:
        await asyncio.Event().wait()
    except (asyncio.CancelledError, KeyboardInterrupt):
        logger.info("Shutting down...")
    finally:
        ws_task.cancel()
        try:
            await ws_task
        except (asyncio.CancelledError, Exception):
            pass
        await site.stop()
        if _ws_session and not _ws_session.closed:
            await _ws_session.close()
        if _ws_connection and not _ws_connection.closed:
            await _ws_connection.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="A2A Agent — wraps OpenCode / Claude Code as a Registry agent",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--backend",
        choices=["opencode", "claude"],
        default="opencode",
        help="CLI backend: 'opencode' (default) or 'claude'",
    )
    parser.add_argument(
        "--registry",
        default=os.environ.get("A2A_REGISTRY_URL", "http://localhost:8321"),
        help="Registry URL (default: http://localhost:8321, or $A2A_REGISTRY_URL)",
    )
    parser.add_argument(
        "--host",
        default=AGENT_HOST,
        help=f"HTTP server host (default: {AGENT_HOST})",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=AGENT_PORT,
        help=f"HTTP server port (default: {AGENT_PORT})",
    )
    parser.add_argument(
        "--cwd",
        default="",
        help="Default working directory for CLI tasks (default: current dir)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=CLI_TIMEOUT,
        help=f"Max seconds per CLI task (default: {CLI_TIMEOUT})",
    )
    parser.add_argument(
        "--auth",
        action="store_true",
        default=bool(os.environ.get("OAUTH_CLIENT_ID")),
        help="Enable OAuth auth (default: auto-detect from OAUTH_CLIENT_ID env)",
    )
    parser.add_argument("--client-id", default="", help="OAuth client ID")
    parser.add_argument("--client-secret", default="", help="OAuth client secret")
    parser.add_argument(
        "--auth-config",
        default=AUTH_CONFIG_PATH,
        help=f"Path to auth JSON config (default: {AUTH_CONFIG_PATH})",
    )
    parser.add_argument(
        "--agent-config",
        default=AGENT_CONFIG_PATH,
        help=f"Path to agent ID JSON config (default: {AGENT_CONFIG_PATH})",
    )
    parser.add_argument(
        "--agent-id",
        default="",
        help="Force a specific agent ID (overrides saved config)",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable DEBUG logging",
    )

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    try:
        asyncio.run(amain(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()