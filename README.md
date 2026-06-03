# Simple A2A Registry

> **The Kubernetes for AI Agents** — registration, orchestration, and governance for distributed agent ecosystems.

A lightweight, production-ready Agent-to-Agent (A2A) Registry server that enables autonomous AI agents to discover each other, exchange tasks, and collaborate at scale. Built on the [Google A2A protocol](https://github.com/google/A2A) model while extending it with a full Kanban-style orchestration engine, OAuth 2.1 security, multi-tenant isolation, plugin hooks, and Prometheus observability.

---

## Features

| Capability | Description |
|------------|-------------|
| **Agent Registry & Discovery** | Register, list, search agents by name/skill/tag. Agent Card aligned to A2A v1.0 protobuf. |
| **WebSocket Persistent Connection** | Agents maintain long-lived WS connections; server pushes tasks in real-time. Auto-reconnect. |
| **HTTP Heartbeat** | `POST /agents/{id}/heartbeat` — 120s timeout, 300s stale cleanup, Prometheus gauges. |
| **Task Dispatch** | Distribute tasks to connected agents via WS; poll results via HTTP. Progress reporting. |
| **Subprocess Pool** | Persistent subprocess workers per assignee; stdin JSON-line dispatch; auto-restart on crash. |
| **Kanban Orchestration Engine (V2)** | Full 8-state state machine (todo → ready → running → completed / failed / blocked). DAG dependency chains, atomic claim locks, TTL release, retry promotion, auto-spawn. |
| **Swarm Topology** | Create multi-agent coordination DAGs: N parallel workers → verifier → synthesizer, with shared blackboard. |
| **OAuth 2.1 Auth** | JWT RS256/HS256, `client_credentials` grant, scope-based access control (`task:read`, `agent:register`, `registry:admin`, etc.). |
| **Multi-Tenancy** | `X-Tenant-ID` header + `?tenant=` query param. Full data isolation per tenant. |
| **Plugin System** | Hook-based architecture (lifecycle, request, event hooks). Load via `pyproject.toml` entry_points or `config.yaml`. |
| **Rate Limiting** | Token-bucket algorithm. Memory (default) or MySQL-backed. Configurable per-key (IP or client_id). Whitelist support. |
| **Prometheus Metrics** | `/metrics` endpoint with request count, latency histogram, auth operations, DB query duration, agent/WS gauges. |
| **Audit Logging** | Append-only, tamper-evident event tracking with TTL retention. Every sensitive operation logged. |
| **Admin WebSocket** | Real-time task updates to the Admin SPA. Selective task subscription. |
| **Bootstrap Admin** | Auto-creates `simple-a2a-registry` admin client on first start with auto-generated secret. |
| **Web Dashboard** | Built-in SPA at `http://localhost:8321` — Agent list, Kanban board (Board/List views), task detail with dependency chain. |
| **Database Engine** | SQLite (dev, WAL mode) or MySQL (prod, QueuePool). RetryEngine for transient errors. Alembic migrations. |
| **YAML Config** | `~/.simple-a2a-registry/config.yaml` with env override (`A2A_REGISTRY_*`). Sensitive fields auto-masked. |
| **TLS/SSL** | `--certfile` / `--keyfile` for HTTPS. |
| **CORS** | Configurable `Access-Control-Allow-Origin` via `cors_origins`. |

---

## Architecture

```
┌──────────────┐    HTTP/WS     ┌──────────────────────────────────────────────────────┐
│   Client     │ ─────────────→ │  A2A Registry server (localhost:8321)                 │
│  (caller)    │                │                                                        │
└──────────────┘                │  ┌────────────────────────────────────────────────┐   │
                                │  │  Agent Registry & Dispatch                     │   │
                                │  │  Register/list/search/delete agents            │   │
                                │  │  HTTP heartbeat + WebSocket long-connection    │   │
                                │  │  Task push via WS + HTTP result polling        │   │
                                │  ├────────────────────────────────────────────────┤   │
                                │  │  Orchestration Engine (V2 Kanban)              │   │
                                │  │  DAG dependency, state machine, dispatching    │   │
                                │  │  Swarm topology, blackboard, workspace mgmt    │   │
                                │  ├────────────────────────────────────────────────┤   │
                                │  │  Auth & Governance                             │   │
                                │  │  OAuth 2.1 / JWT RS256+HS256 / Scope / Admin   │   │
                                │  │  Multi-tenant, rate limiting, audit logging    │   │
                                │  ├────────────────────────────────────────────────┤   │
                                │  │  Observability                                 │   │
                                │  │  Prometheus metrics, JSON logging, Admin WS    │   │
                                │  ├────────────────────────────────────────────────┤   │
                                │  │  Store (unified DB engine)                     │   │
                                │  │  SQLite (dev)  ←→  MySQL (prod)                │   │
                                │  │  WAL mode + RetryEngine + Alembic migrations   │   │
                                │  └────────────────────────────────────────────────┘   │
                                └────────────────────────┬─────────────────────────────┘
                                                         │
          ┌──────────────────────────────────────────────┼────────────────────────────────┐
          │                                              │                                │
   ┌──────▼──────┐                               ┌──────▼──────┐                  ┌──────▼──────┐
   │  Agent A    │                               │  Agent B    │                  │  Worker     │
   │(HTTP + WS)  │                               │ (WS long)   │                  │ (Kanban)    │
   └─────────────┘                               └─────────────┘                  └─────────────┘
```

### Internal Module Map

```
simple_a2a_registry/
  server.py        — aiohttp app factory, middleware stack, route registration
  cli.py           — argparse CLI entry point
  store.py         — Agent registry + OAuth client/token persistence
  models.py        — Agent Card data models (A2A v1.0 alignment)
  auth.py          — OAuth 2.1: JWT issue/verify, middleware, Admin client CRUD
  config.py        — YAML config loader with env override
  errors.py        — Unified error response format + exception hierarchy
  log.py           — JSON/text structured logging with request_id context
  metrics.py       — Prometheus metrics middleware + endpoint
  rate_limiter.py  — Token bucket rate limiter (memory/MySQL)
  audit.py         — Append-only event audit store
  users.py         — User registry + session management for Web Dashboard
  validation.py    — Input validation helpers
  plugin.py        — Plugin ABC + PluginRegistry (load/dispatch hooks)
  client.py        — A2A Python SDK (sync + async)
  ws_admin.py      — Admin WebSocket hub for real-time task updates
  database/
    engine.py      — DatabaseEngine ABC, SQLiteEngine, MySQLEngine, RetryEngine
  orchestration/
    __init__.py    — Module exports
    models.py      — Task/run/event/comment data models
    store.py       — TaskStore: SQLite task persistence
    state_machine.py — 8-state state machine
    routes.py      — V2 REST API routes
    swarm.py       — Swarm topology creation + blackboard
    swarm_routes.py — Swarm REST API routes
    dispatcher.py  — Background worker dispatcher (poll loop)
    workspace.py   — Workspace allocation/cleanup
    dependency.py  — Cycle detection, dependency resolution
    pool.py        — Worker subprocess pool (P1.5 dispatch)
  static/          — Web Dashboard SPA (HTML + JS)
```

---

## Quick Start

### 1. Install

```bash
pip install simple-a2a-registry
```

Or from source:

```bash
git clone <repo-url>
cd simple-a2a-registry
pip install -e .
```

### 2. Start the server

```bash
a2a-registry
```

Opens on `http://localhost:8321`. Log shows the bootstrap admin secret:

```
INFO  Simple A2A Registry starting on 0.0.0.0:8321 (data: ~/.simple-a2a-registry) 🔓 auth disabled (dev) | V2: defaults
INFO  Bootstrap admin client created:
      client_id:    simple-a2a-registry
      client_secret: auto-generated-xxx
```

### 3. Register an agent (no auth mode)

```bash
curl -s -X POST http://localhost:8321/v1/agents \
  -H "Content-Type: application/json" \
  -d '{"name": "My Agent", "description": "A test agent"}'
```

Response:

```json
{"id": "a2a-xxx", "name": "My Agent", "status": "alive", ...}
```

### 4. List agents

```bash
curl -s http://localhost:8321/v1/agents
```

### 5. Enable auth (production)

```bash
a2a-registry --auth-enabled true
```

### 6. Stopping

`Ctrl+C` triggers graceful shutdown: WS agents notified, in-flight tasks cancelled, DB closed.

### 7. Use the Python SDK

```python
from simple_a2a_registry.client import A2AClient

client = A2AClient(
    registry_url="http://localhost:8321",
    client_id="my-agent",
    client_secret="my-secret",
)

# 1. Health check
health = client.health()
print(f"Registry: {health['version']} uptime={health['uptime_seconds']}s")

# 2. Register an agent (sync)
agent_card = {
    "name": "My Agent",
    "description": "A test agent",
    "version": "1.0.0",
    "default_input_modes": ["text/plain"],
    "default_output_modes": ["text/plain"],
    "skills": [{"id": "echo", "name": "Echo"}],
}
agent_id = client.register_agent(agent_card=agent_card)
print(f"Registered: {agent_id}")

# 3. Heartbeat
client.heartbeat(agent_id)

# 4. List agents
result = client.list_agents()
print(f"Total agents: {result['total']}")

# 5. Clean up
client.deregister_agent(agent_id)
```

Async with WebSocket:

```python
import asyncio

async def demo():
    async with A2AClient(
        registry_url="http://localhost:8321",
        client_id="my-agent",
        client_secret="my-secret",
    ) as client:
        agent_id = await client.async_register_agent(agent_card=agent_card)

        # Set up dispatch handler
        async def handle_task(task):
            task_id = task["id"]
            await client.async_report_progress(task_id, status="working")
            result = {"text": "Task completed"}
            await client.async_report_result(task_id, result)

        client.dispatch_handler = handle_task
        await client.async_connect_websocket(agent_id)
        await asyncio.sleep(3600)  # keep alive

asyncio.run(demo())
```

Run the full example:

```bash
# sync mode
python examples/sdk_usage.py --mode sync

# async mode with WebSocket (default)
python examples/sdk_usage.py --mode async --self-dispatch
```

---

## Configuration

The server accepts configuration from three sources (highest priority first):

| Priority | Source | Example |
|----------|--------|---------|
| 1 | CLI arguments | `--port 9000 --auth-enabled true` |
| 2 | Environment variables | `A2A_REGISTRY_SERVER__PORT=9000` |
| 3 | YAML config file | `~/.simple-a2a-registry/config.yaml` |
| 4 | Code defaults | port 8321, auth disabled |

### CLI Arguments

| Argument | Default | Description |
|----------|---------|-------------|
| `--host` | `0.0.0.0` | Bind address |
| `--port` | `8321` | Bind port |
| `--data-dir` | `~/.simple-a2a-registry` | Data directory |
| `--auth-enabled` | `false` | Enable OAuth 2.1 authentication |
| `--bootstrap-secret` | auto-generated | Bootstrap admin client secret |
| `--board-path` | `<data-dir>/board.db` | V2 orchestration board DB path |
| `--dispatcher-enabled` | `true` | Background worker dispatcher |
| `--dispatcher-interval` | `5` | Dispatcher poll interval (s) |
| `--claim-ttl` | `900` | Claim lock TTL (15 min) |
| `--failure-limit` | `3` | Default retry limit |
| `--workspaces-root` | `<data-dir>/workspaces` | Workspace root directory |
| `--log-format` | `text` | `json` (production) or `text` (dev) |
| `--log-level` | `INFO` | DEBUG / INFO / WARNING / ERROR |
| `--log-file` | stderr | Log file path |
| `--certfile` | — | TLS certificate path |
| `--keyfile` | — | TLS private key path |
| `--version` | — | Show version and exit |

### Environment Variables

Use the `A2A_REGISTRY_` prefix. Nesting uses `__` (double underscore):

```bash
export A2A_REGISTRY_SERVER__PORT=9000
export A2A_REGISTRY_DATABASE__DRIVER=mysql
export A2A_REGISTRY_DATABASE__MYSQL_DSN=mysql+pymysql://user:pass@host/db
export A2A_REGISTRY_AUTH__BOOTSTRAP_SECRET=my-secret
```

### YAML Config File

Place at `~/.simple-a2a-registry/config.yaml`:

```yaml
server:
  host: "0.0.0.0"
  port: 8321
  cors_origins: "*"

auth:
  enabled: false
  bootstrap_secret: ""

database:
  driver: sqlite                    # or "mysql"
  sqlite_path: "~/.simple-a2a-registry/registry.db"
  mysql_dsn: ""                     # mysql+pymysql://user:pass@host/dbname
  pool_size: 5
  max_overflow: 10

rate_limit:
  enabled: false
  default_unauthenticated: 60
  default_authenticated: 300
  storage: memory                   # or "mysql"
  whitelist: []

monitoring:
  metrics_enabled: true

dispatcher:
  poll_interval: 5
  claim_ttl: 900
  failure_limit: 3

plugins:
  my-plugin:
    module: my_package.my_plugin
    config:
      key: value
```

---

## Authentication & Authorization

### Modes

| Mode | Flag | Use Case |
|------|------|----------|
| Dev (default) | `--auth-enabled false` | Local dev, no tokens required |
| Production | `--auth-enabled true` | All endpoints require Bearer token |

### OAuth 2.1 Flow (client_credentials)

```
Admin pre-creates OAuth client → Agent gets client_id/client_secret
  → POST /auth/token → JWT Bearer → use for all API calls
```

### Scope Reference

| Scope | Permission | Endpoints |
|-------|------------|-----------|
| `agent:read` | List/read agents | `GET /v1/agents` |
| `agent:register` | Register agents | `POST /v1/agents` |
| `agent:admin` | Delete/disable agents | `DELETE /v1/agents/{id}` |
| `task:read` | Read tasks | `GET /v1/tasks`, `GET /v2/tasks` |
| `task:write` | Create/modify tasks | Task dispatch, v2 CRUD |
| `registry:admin` | Registry admin | Admin client management, WebSocket |
| `user:read` | Read users | User endpoints |
| `user:write` | Modify users | User CRUD |

### Public endpoints (no auth required)

- `GET /health`
- `GET /.well-known/*`
- `POST /auth/token`

### Key type

- **HS256** (default) — symmetric, auto-generated key. For single-instance deployments.
- **RS256** — asymmetric key pair. Enable by setting `auth.public_key` in config. Required for multi-instance or when agents need to verify tokens without the server.

---

## API Overview

### Agent Management (V1)

| Method | Path | Auth Scope | Description |
|--------|------|------------|-------------|
| GET | `/v1/agents` | `agent:read` | List/search agents (query: `?q=`, `?skill=`, `?tag=`) |
| GET | `/v1/agents/{id}` | `agent:read` | Agent detail |
| POST | `/v1/agents` | `agent:register` | Register agent |
| POST | `/v1/agents/{id}/toggle` | `agent:admin` | Toggle agent disabled/enabled status |
| DELETE | `/v1/agents/{id}` | `agent:admin` | Deregister agent |

### Heartbeat & Connectivity

| Method | Path | Description |
|--------|------|-------------|
| POST | `/v1/agents/{id}/heartbeat` | HTTP heartbeat (updates timestamp, resets timeout) |
| GET | `/v1/agents/{id}/ws` | WebSocket upgrade — persistent connection |

### Task Dispatch (V1)

| Method | Path | Description |
|--------|------|-------------|
| POST | `/v1/agents/{id}/dispatch` | Dispatch task to a WS-connected agent |
| POST | `/v1/agents/{id}/task` | Proxy task (fallback HTTP transport for WS-dispatched agents) |
| GET | `/v1/tasks` | List all tasks |
| GET | `/v1/tasks/{id}` | Task status & result |

### Orchestration Engine (V2 Kanban)

| Method | Path | Description |
|--------|------|-------------|
| POST | `/v2/tasks` | Create task (with optional parent dependencies) |
| GET | `/v2/tasks` | List tasks (`?status=`, `?assignee=`, `?q=`, `?sort=`) |
| GET | `/v2/tasks/{id}` | Task detail (parents, children, runs, comments, events) |
| PATCH | `/v2/tasks/{id}` | Update title/body/assignee/priority/status |
| POST | `/v2/tasks/{id}/claim` | Atomically claim task (worker lock) |
| POST | `/v2/tasks/{id}/complete` | Mark task complete with summary + result |
| POST | `/v2/tasks/{id}/block` | Block task (human-in-the-loop) |
| POST | `/v2/tasks/{id}/unblock` | Unblock task back to running |
| POST | `/v2/tasks/{id}/heartbeat` | Extend claim TTL |
| POST | `/v2/tasks/{id}/comment` | Add comment to task thread |
| DELETE | `/v2/tasks/{id}` | Archive task |
| POST | `/v2/tasks/{id}/depend` | Add parent dependency |
| DELETE | `/v2/tasks/{id}/depend/{parent_id}` | Remove parent dependency |
| GET | `/v2/stats` | Orchestration statistics |

### Swarm (Multi-Agent Topology)

| Method | Path | Description |
|--------|------|-------------|
| POST | `/v2/swarm` | Create swarm topology (workers → verifier → synthesizer) |
| GET | `/v2/swarm/{root_id}` | Get swarm status |
| POST | `/v2/swarm/{root_id}/comment` | Write to swarm blackboard |
| GET | `/v2/swarm/{root_id}/blackboard` | Read swarm blackboard |

### OAuth & Admin

| Method | Path | Auth Scope | Description |
|--------|------|------------|-------------|
| POST | `/auth/token` | public | Acquire JWT token (client_credentials) |
| POST | `/auth/register` | public | Register new OAuth client |
| GET | `/.well-known/oauth-authorization-server` | public | OAuth metadata JSON |
| GET | `/.well-known/jwks.json` | public | JWT public keys |
| POST | `/admin/clients` | `registry:admin` | Create OAuth client |
| GET | `/admin/clients` | `registry:admin` | List OAuth clients |
| GET | `/admin/audit` | `registry:admin` | List audit log entries |
| DELETE | `/admin/clients/{id}` | `registry:admin` | Delete OAuth client |

### Users (Web Dashboard Authentication)

| Method | Path | Description |
|--------|------|-------------|
| POST | `/auth/users` | Create user account |
| POST | `/auth/login` | Login (returns session cookie) |
| POST | `/auth/logout` | Logout |
| GET | `/auth/me` | Current user info |

### System

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Health check (`{"agents_alive": N, "agents_stale": N, ...}`) |
| GET | `/.well-known/agent-card.json` | Registry's own Agent Card |
| GET | `/metrics` | Prometheus metrics (when `monitoring.metrics_enabled`) |
| GET | `/ws/admin` | Admin WebSocket (real-time task updates) |

Full API reference: [docs/API.md](docs/API.md)

---

## Agent Lifecycle

### Registration

```bash
curl -s -X POST http://localhost:8321/v1/agents \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Coder Agent",
    "description": "AI coding assistant",
    "skills": ["Python", "JavaScript"],
    "tags": ["coding", "devops"],
    "agent_card": {
      "url": "http://agent-host:9001/.well-known/agent-card.json"
    }
  }'
```

Response returns an `id` (e.g. `a2a-xxxx`) which is used in all subsequent calls.

### Heartbeat

```bash
curl -s -X POST http://localhost:8321/v1/agents/a2a-xxxx/heartbeat
# → {"id":"a2a-xxxx","status":"alive","stale_timeout":120}
```

- If no heartbeat for **120 seconds**, the agent is marked `stale`.
- If no heartbeat for **300 seconds**, the agent is garbage-collected.
- The `/v1/agents` endpoint supports `?status=alive`, `?status=stale` filters.

### WebSocket Connection

Agent upgrades to WebSocket for real-time task push:

```python
import aiohttp

async with aiohttp.ClientSession() as session:
    async with session.ws_connect(
        "http://localhost:8321/v1/agents/a2a-xxxx/ws"
    ) as ws:
        async for msg in ws:
            data = msg.json()
            if data["type"] == "task":
                task_id = data["id"]
                query = data["query"]
                print(f"Received task {task_id}: {query}")
                # Process task...
                await ws.send_json({
                    "type": "task_result",
                    "id": task_id,
                    "result": {"status": "completed", "output": "..."},
                })
```

---

## Orchestration Engine (V2)

The Orchestration Engine provides a full Kanban-style task lifecycle with SQLite-persisted state, DAG dependency chains, atomic worker claiming, and an auto-spawn dispatcher.

### State Machine

```
          ┌────────────────────────────────────────────┐
          │                                            │
          v                                            │
    ┌──────────┐    ┌─────────┐    ┌──────────┐       │
    │   TODO   │───→│  READY  │───→│ RUNNING  │       │
    └──────────┘    └─────────┘    └──────────┘       │
                      │    ▲           │    │          │
                      │    │           │    │          │
                      │    │     ┌─────▼────▼──┐      │
                      │    │     │  COMPLETED  │      │
                      │    │     └─────────────┘      │
                      │    │                          │
                      │    │     ┌──────────┐         │
                      │    └─────│  FAILED  │         │
                      │          └──────────┘         │
                      │              │                │
                      │         ┌────▼────┐           │
                      │         │ BLOCKED │           │
                      │         └─────────┘           │
                      │           │    ▲              │
                      │           │    │              │
                      │     ┌─────▼────▼──┐           │
                      │     │  ARCHIVED  │            │
                      │     └────────────┘            │
                      │                               │
                      └───────────────────────────────┘
```

### Task States

| State | Meaning |
|-------|---------|
| `todo` | Created; waiting for dependencies to complete |
| `ready` | All parents done; assignee set; waiting to be claimed |
| `running` | Claimed by a worker; actively being processed |
| `completed` | Successfully finished |
| `failed` | Terminated with error (may retry) |
| `blocked` | Human-in-the-loop intervention |
| `archived` | Soft-deleted; retained in DB for audit |
| `cancelled` | Explicitly cancelled |

### Key Concepts

**Claim Lock** — When a worker claims a task, it gets an exclusive lock identified by `claim_lock` (a UUID). All subsequent status transitions require this lock. Expired locks (default 15 min TTL) are released by the dispatcher.

**DAG Dependencies** — A task with `parents` automatically stays in `todo` until all parents reach `completed`. The dispatcher then promotes it to `ready`.

**Retry** — Failed tasks below `max_retries` are auto-promoted back to `ready`. Each attempt creates a `TaskRun` record.

**Worker Dispatcher** — Background loop (default 5s interval) that:
1. Releases expired claim locks (running → failed)
2. Promotes retryable tasks (failed → ready)
3. Claims and spawns processes for ready tasks

### API Example

```bash
# Create task
curl -s -X POST http://localhost:8321/v2/tasks \
  -H "Content-Type: application/json" \
  -d '{
    "title": "Implement login module",
    "body": "Requirements...",
    "assignee": "coder-agent",
    "priority": 1
  }'

# Task is now TODO. When assignee is set on a root task (no parents),
# it auto-promotes to READY.

# List ready tasks
curl -s "http://localhost:8321/v2/tasks?status=ready"

# Claim (worker)
curl -s -X POST http://localhost:8321/v2/tasks/t_xxx/claim \
  -H "Content-Type: application/json" \
  -d '{"worker_id": "coder-1", "pid": 12345}'
# → {"claim_lock": "uuid-xxx", "task": {...}}

# Complete
curl -s -X POST http://localhost:8321/v2/tasks/t_xxx/complete \
  -H "Content-Type: application/json" \
  -d '{
    "claim_lock": "uuid-xxx",
    "summary": "Login module implemented",
    "result": {"files": ["auth.py"]}
  }'
```

---

## Swarm Topology

The Swarm system lets you create multi-agent coordination workflows on top of the V2 task engine. It reuses the same dependency graph, state machine, and store — no new tables.

### Topology

```
     ┌───────────────────────────────────────────────┐
     │         Swarm Root (immediately completed)     │
     │         Shared Blackboard (comments)           │
     └──────────┬──────────┬──────────┬──────────────┘
                │          │          │
         ┌──────▼──┐ ┌─────▼────┐ ┌──▼────────┐
         │Worker 1 │ │Worker 2  │ │Worker N...│   ← parallel execution
         └─────┬───┘ └─────┬────┘ └────┬──────┘
               │           │           │
               └───────────┼───────────┘
                           ▼
                    ┌──────────────┐
                    │   Verifier   │   ← gates: pass → synthesizer / block
                    └──────┬───────┘
                           ▼
                    ┌──────────────┐
                    │ Synthesizer  │   ← final consolidation
                    └──────────────┘
```

### Blackboard

Workers share intermediate results through structured comments on the root task, prefixed with `[swarm:blackboard]`. The `read_blackboard()` function aggregates all entries by key.

### API

```bash
# Create swarm
curl -s -X POST http://localhost:8321/v2/swarm \
  -H "Content-Type: application/json" \
  -d '{
    "goal": "Research and implement OAuth module",
    "workers": [
      {"profile": "researcher", "title": "Research OAuth protocols", "body": "..."},
      {"profile": "coder", "title": "Implement OAuth endpoints", "body": "..."}
    ],
    "verifier_profile": "reviewer",
    "synthesizer_profile": "writer"
  }'

# Check status
curl -s http://localhost:8321/v2/swarm/t_root_id

# Read blackboard
curl -s http://localhost:8321/v2/swarm/t_root_id/blackboard

# Write to blackboard
curl -s -X POST http://localhost:8321/v2/swarm/t_root_id/comment \
  -H "Content-Type: application/json" \
  -d '{"author": "worker-1", "key": "phase1_result", "value": {...}}'
```

---

## Subprocess Pool Manager

Orchestration supports an alternative dispatch path for agents that maintain long-lived subprocess workers — the **Subprocess Pool**. Instead of dispatching tasks one-per-process (P3), the pool keeps N persistent workers running and feeds them task assignments via stdin as JSON-lines.

### Dispatch Priority

| Priority | Path | Description |
|----------|------|-------------|
| **P1** | WebSocket | Agent connected via WS; task pushed in real-time |
| **P1.5** | Subprocess Pool | Assignee in `pool_assignees`; task dispatched to persistent subprocess worker |
| **P2** | Blocked | Agent known but not connected (task held until agent reconnects) |
| **P3** | Legacy Worker Command | One-shot subprocess spawned per task (`worker_command` template) |

### How It Works

```python
# server startup
pool = SubprocessPoolManager(
    pool_assignees=["coder-agent", "reviewer"],
    worker_command="hermes chat --profile {assignee} --pool-worker",
    store=task_store,
    workspace_manager=ws_mgr,
)
await pool.start()
```

Each configured assignee gets one persistent subprocess. The pool manager:
1. Sends task assignments as JSON-lines on the worker's `stdin`
2. Monitors worker health via background watch tasks
3. Auto-restarts workers on crash with a 1s backoff
4. Sends a `{"type": "shutdown"}` message on graceful server shutdown

### Worker Protocol

Pool workers receive task assignments on stdin as JSON:

```json
{"type": "task", "task_id": "t_xxx", "title": "...", "body": "...",
 "assignee": "coder-agent", "workspace_path": "/tmp/workspaces/t_xxx"}
```

Worker responds by calling back to the Orchestration API (`POST /v2/tasks/{id}/claim`, `POST /v2/tasks/{id}/complete`, etc.) over HTTP.

### Configuration

Configure pool assignees via the server's `SubprocessPoolManager` constructor. Pool workers are started during `on_startup` and gracefully shut down during `on_cleanup` with a 5s grace period.

---

## Plugin System

The plugin system allows third-party code to extend the registry at well-defined lifecycle points.

### Available Hooks

**Lifecycle:** `load(config)` → `init(app)` → `before_shutdown(app)`
**Request:** `before_request(request)` → `after_request(request, response)`
**Events:** `on_agent_register`, `on_agent_deregister`, `on_agent_heartbeat`, `on_task_created`, `on_task_completed`, `on_token_issued`, `on_server_start`, `on_server_stop`

### Loading Methods

1. **Entry points** (via `pyproject.toml`):
   ```toml
   [project.entry-points."simple_a2a_registry.plugins"]
   my-plugin = "my_package:MyPlugin"
   ```

2. **Config file** (`config.yaml`):
   ```yaml
   plugins:
     my-plugin:
       module: my_package.my_plugin
       config:
         api_key: "xxx"
   ```

### Minimal Plugin

```python
from simple_a2a_registry.plugin import Plugin

class MyPlugin(Plugin):
    @property
    def name(self) -> str:
        return "my-plugin"

    async def before_request(self, request):
        # Called before every HTTP request
        return None  # let request continue

    async def on_agent_register(self, agent_id: str, card: dict):
        print(f"Agent registered: {agent_id}")
```

---

## Monitoring & Observability

### Prometheus Metrics

Available at `GET /metrics` when `monitoring.metrics_enabled=true` (default).

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `a2a_registry_requests_total` | Counter | `endpoint`, `method`, `status` | HTTP request count |
| `a2a_registry_request_duration_seconds` | Histogram | `endpoint`, `method` | Request latency |
| `a2a_registry_auth_operations_total` | Counter | `operation`, `success` | Auth operations |
| `a2a_registry_agents_alive` | Gauge | — | Active agents count |
| `a2a_registry_agents_stale` | Gauge | — | Stale agents count |
| `a2a_registry_ws_connections` | Gauge | — | Active WS connections |
| `a2a_registry_admin_ws_connections` | Gauge | — | Admin WS connections |
| `a2a_registry_db_pool_size` | Gauge | — | DB pool size |
| `a2a_registry_db_query_duration_seconds` | Histogram | `operation` | DB query latency |

### Structured Logging

- **JSON format** (`--log-format json`): machine-parseable for ELK/Loki
- **Text format** (`--log-format text`, default): human-readable for development
- Each log entry includes `request_id` for request tracing across middleware handlers

### Audit Logging

All sensitive operations (auth events, admin actions, agent CRUD, task state changes) are written to an append-only `audit_log` table with:
- `event_type` — classification (e.g., `agent_register`, `token_issue`, `admin_action`)
- `actor` — who performed the action
- `target` — what was acted upon
- `timestamp` — when it happened
- `success` — whether the operation succeeded

Configurable retention TTL (default 90 days).

---

## Database

### SQLite (Development)

Default driver. Single file at `~/.simple-a2a-registry/registry.db` with WAL mode, 5s busy timeout, and foreign keys enabled.

```bash
a2a-registry  # uses SQLite by default
```

### MySQL (Production)

Set in config or env:

```yaml
database:
  driver: mysql
  mysql_dsn: "mysql+pymysql://user:pass@host:3306/a2a_registry"
  pool_size: 5
  max_overflow: 10
```

Or via env variable:

```bash
export A2A_REGISTRY_DATABASE__DRIVER=mysql
export A2A_REGISTRY_DATABASE__MYSQL_DSN=mysql+pymysql://user:pass@host:3306/a2a_registry
```

The engine layer automatically translates SQLite placeholders (`?` → `%s`) and dialect-specific syntax (`INSERT OR REPLACE` → `REPLACE INTO`, skips `PRAGMA`).

### RetryEngine

A transparent wrapper around the database engine that retries transient errors (database locked, connection lost, timeout) with exponential backoff (3 attempts by default).

### Migrations

Alembic migrations are in `migrations/versions/`. Run with:

```bash
alembic upgrade head
```

Migration script utility: `scripts/migrate_sqlite_to_mysql.py` helps migrate from SQLite to MySQL.

---

## Multi-Tenancy

Every V2 task and agent supports tenant-level isolation:

- **`?tenant=<value>`** query parameter on API calls
- **`X-Tenant-ID`** header for client identity propagation
- Tasks from different tenants are fully isolated in the store
- Tenant is propagated through claims, dispatches, and audits

---

## Rate Limiting

Token bucket algorithm with two backends:

| Backend | Use Case | Description |
|---------|----------|-------------|
| `memory` | Dev / single instance | In-memory dict with `asyncio.Lock` |
| `mysql` | Production / multi-instance | MySQL-backed with `rate_limit_buckets` table |

Key derivation priority:
1. `client_id` from auth token (if authenticated)
2. `X-Forwarded-For` / `X-Real-IP` headers
3. TCP connection `remote`

```yaml
rate_limit:
  enabled: true
  default_unauthenticated: 60    # req/min for public endpoints
  default_authenticated: 300     # req/min for authenticated
  storage: mysql                 # or "memory"
  whitelist: ["my-super-agent"]  # exempted client IDs
```

---

## Python SDK

The [`simple_a2a_registry.client`](simple_a2a_registry/client.py) module provides `A2AClient` — a full-featured SDK for agents to interact with the Registry.

### Sync Usage

```python
from simple_a2a_registry.client import A2AClient

client = A2AClient(
    registry_url="http://localhost:8321",
    client_id="my-agent",
    client_secret="secret-xxx",
)

# Register
agent_id = client.register_agent(
    name="My Agent",
    description="A useful agent",
    skills=["Python"],
)

# Heartbeat
client.heartbeat(agent_id)

# WebSocket (handles auto-reconnect + exponential backoff)
client.dispatch_handler = lambda task: print(f"Got task: {task['id']}")
client.connect_websocket(agent_id)
```

### Async Usage

```python
async with A2AClient(
    registry_url="http://localhost:8321",
    client_id="my-agent",
    client_secret="secret-xxx",
) as client:
    agent_id = await client.async_register_agent(
        name="My Agent", description="Async agent",
    )
    await client.async_connect_websocket(agent_id)
```

Full example: [examples/a2a_coder_agent.py](examples/a2a_coder_agent.py)

---

## Web Dashboard

Open `http://localhost:8321` in a browser for the built-in Dashboard SPA:

- **Agent List** — status (alive/stale), skills, tags, WS connection indicator
- **Agent Detail** — expandable panel with full Agent Card
- **Kanban Board** — Board view (columns by status) and List view (sortable table)
- **Task Detail** — modal with dependency chain, run history, event stream, comment thread
- **Stats** — real-time agent counts and orchestration statistics (refreshed every 15s)

To authenticate Dashboard access (when auth is enabled), use the session-based login via `POST /auth/login`.

---

## Examples

### Full Examples

| File | Description |
|------|-------------|
| `examples/a2a_coder_agent.py` | Full-featured A2A agent: OAuth, WS, AgentCard, Hermes integration |
| `examples/a2a_opencode_agent.py` | OpenCode agent variant with similar protocol |
| `examples/sdk_usage.py` | SDK feature demos |
| `examples/simple_a2a_agent.py` | Minimal agent example |
| `examples/run_agent.py` | Quick-start agent runner |
| `examples/run_a2a_agent.sh` | Shell script to launch agent |
| `examples/test_int_priority.sh` | Integration test for task priority |

### Coder Agent (Full Example)

The [examples/a2a_coder_agent.py](examples/a2a_coder_agent.py) demonstrates a production-grade A2A agent:

- OAuth client credentials with auto-refresh
- WebSocket connection with exponential backoff reconnection
- AgentCard registration and 30s heartbeat
- A2A JSON-RPC over HTTP (port 9001): `POST /tasks/send`, `GET /tasks/{id}`
- Task execution via Hermes CLI (coder profile)

```bash
export OAUTH_CLIENT_ID=client-xxx OAUTH_CLIENT_SECRET=secret-xxx
python examples/a2a_coder_agent.py
```

---

## Development

### Install development dependencies

```bash
pip install -e ".[dev]"
```

### Run tests

```bash
pytest tests/ -v                        # All tests
pytest tests/test_store.py -v           # Storage layer
pytest tests/test_orchestration_api.py  # V2 API
pytest tests/test_server.py             # Integration tests
pytest tests/test_swarm.py              # Swarm tests
pytest -m slow                          # Slow tests (soak, benchmarks)
```

Test categories:

| Test File | What It Covers |
|-----------|----------------|
| `tests/test_store.py` | Agent + OAuth persistence |
| `tests/test_models.py` | Agent Card data models |
| `tests/test_server.py` | HTTP API integration |
| `tests/test_auth.py` | OAuth 2.1 flow |
| `tests/test_orchestration_api.py` | V2 REST API |
| `tests/test_orchestration_store.py` | TaskStore CRUD |
| `tests/test_orchestration_state_machine.py` | State transitions |
| `tests/test_orchestration_e2e.py` | End-to-end orchestration |
| `tests/test_orchestration_integration.py` | Integration tests |
| `tests/test_swarm.py` | Swarm topology |
| `tests/test_dispatcher.py` | Background dispatcher |
| `tests/test_rate_limiter.py` | Token bucket |
| `tests/test_validation.py` | Input validation |
| `tests/test_errors.py` | Error handling |
| `tests/test_log.py` | Logging |
| `tests/test_config.py` | Config loading |
| `tests/test_cors.py` | CORS middleware |
| `tests/test_concurrency.py` | Thread safety |
| `tests/test_users.py` | User management |
| `tests/test_tenant_isolation.py` | Multi-tenant isolation |
| `tests/test_tenant_e2e.py` | Tenant E2E tests |
| `tests/test_token_scope_tenant.py` | Token+scope+tenant |
| `tests/test_token_tenant_bc.py` | Backward compat |
| `tests/test_user_auth_e2e.py` | User auth E2E |
| `tests/test_websocket.py` | WebSocket protocol |
| `tests/test_workspace.py` | Workspace management |
| `tests/test_metrics.py` | (in test_server.py) |
| `tests/test_mysql_compat.py` | MySQL dialect compat |
| `tests/test_tls.py` | TLS/SSL |
| `tests/test_bootstrap_admin.py` | Bootstrap admin client |
| `tests/test_performance_benchmark.py` | Benchmarks |
| `tests/benchmarks/` | Benchmark suites |

### Coverage

```bash
pytest tests/ --cov=simple_a2a_registry --cov-report=html
```

### Code Style

```bash
pip install ruff   # or flake8
ruff check simple_a2a_registry/ tests/
```

### Project Conventions

- Python ≥ 3.10 with `from __future__ import annotations`
- Type hints on all public APIs
- Docstrings in Google-style (or reStructuredText with `Args:`/`Returns:` sections)
- Async everywhere for I/O-bound operations
- Chinese-friendly: docstrings and comments in Chinese for internal modules

---

## Project Structure

```
simple-a2a-registry/
├── simple_a2a_registry/
│   ├── __init__.py          — Package exports
│   ├── __main__.py          — `python -m simple_a2a_registry`
│   ├── server.py            — aiohttp app factory (core)
│   ├── cli.py               — argparse CLI entry
│   ├── store.py             — Agent + OAuth persistence
│   ├── models.py            — A2A Agent Card models
│   ├── auth.py              — OAuth 2.1 (JWT, scopes, admin)
│   ├── config.py            — YAML config + env override
│   ├── errors.py            — Unified error response
│   ├── log.py               — Structured logging
│   ├── metrics.py           — Prometheus middleware
│   ├── rate_limiter.py      — Token bucket rate limiter
│   ├── audit.py             — Append-only audit store
│   ├── users.py             — User registry + sessions
│   ├── validation.py        — Input validation
│   ├── plugin.py            — Plugin ABC + registry
│   ├── client.py            — Python SDK (sync+async)
│   ├── ws_admin.py          — Admin WS hub
│   ├── database/
│   │   └── engine.py        — SQLiteEngine + MySQLEngine + RetryEngine
│   ├── orchestration/
│   │   ├── models.py        — Task/run/event/comment models
│   │   ├── store.py         — TaskStore (SQLite)
│   │   ├── state_machine.py — 8-state state machine
│   │   ├── routes.py        — V2 REST API
│   │   ├── swarm.py         — Swarm topology + blackboard
│   │   ├── swarm_routes.py  — Swarm REST API
│   │   ├── dispatcher.py    — Background worker dispatcher
│   │   ├── workspace.py     — Workspace management
│   │   ├── dependency.py    — Cycle detection + DAG resolution
│   │   └── pool.py          — Worker process pool
│   └── static/              — Web Dashboard SPA
├── examples/
│   ├── a2a_coder_agent.py         — Full Coder Agent
│   ├── a2a_opencode_agent.py      — OpenCode Agent
│   ├── a2a_opencode_agent_README.md
│   ├── sdk_usage.py               — SDK demos
│   ├── simple_a2a_agent.py        — Minimal agent
│   ├── run_agent.py               — Quick runner
│   ├── run_a2a_agent.sh           — Shell runner
│   └── test_int_priority.sh       — Priority test
├── tests/
│   ├── test_store.py              — Storage tests
│   ├── test_server.py             — Integration tests
│   ├── ... (30+ test files)
│   └── benchmarks/                — Benchmark suites
├── migrations/                    — Alembic migrations
├── scripts/
│   └── migrate_sqlite_to_mysql.py — Migration utility
├── docs/
│   ├── API.md                     — Full API reference
│   ├── architecture.md            — System architecture
│   ├── oauth-design.md            — OAuth 2.1 design
│   ├── enterprise-architecture.md — Enterprise deployment
│   ├── plugin-system.md           — Plugin documentation
│   ├── fix-spec.md                — Fix specification
│   └── qa-report.md              — QA report
├── pyproject.toml
└── README.md
```

---

## Design Documents

| Document | Description |
|----------|-------------|
| [docs/architecture.md](docs/architecture.md) | System architecture and design decisions |
| [docs/API.md](docs/API.md) | Complete REST API reference |
| [docs/oauth-design.md](docs/oauth-design.md) | OAuth 2.1 authentication design |
| [docs/enterprise-architecture.md](docs/enterprise-architecture.md) | Enterprise deployment architecture |
| [docs/plugin-system.md](docs/plugin-system.md) | Plugin system documentation |
| [docs/fix-spec.md](docs/fix-spec.md) | Bug fix specification |
| [docs/qa-report.md](docs/qa-report.md) | QA test report |

---

## License

MIT License — see [LICENSE](LICENSE) (or the `license` field in `pyproject.toml`).

---

## Contributing

1. Fork the repository
2. Create a feature branch (`git checkout -b feat/my-feature`)
3. Commit changes (`git commit -am 'feat: add awesome feature'`)
4. Push to the branch (`git push origin feat/my-feature`)
5. Open a Pull Request

Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/):
- `feat:` new feature
- `fix:` bug fix
- `docs:` documentation changes
- `refactor:` code restructuring
- `test:` testing improvements
- `chore:` maintenance tasks