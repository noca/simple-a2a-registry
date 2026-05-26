# Simple A2A Registry

> **The Kubernetes for AI Agents — registration, orchestration, and governance for distributed agent ecosystems.**

轻量级、符合 Google A2A (Agent-to-Agent) 协议的 Agent 注册中心与编排引擎。
支持 Agent 注册/发现、心跳保活、WebSocket 长连接、任务分发与状态查询，
以及 Kanban 编排引擎（Orchestration Engine）。

## 架构概览

```
┌─────────────┐      HTTP/WS       ┌──────────────────────────────────────────┐
│   Client    │ ──────────────────→ │  A2A Registry (localhost:8321)          │
│ (调用方)     │                     │                                          │
└─────────────┘                     │  ┌────────────────────────────────────┐  │
                                    │  │ Agent Registry & Dispatch          │  │
                                    │  │ (注册/发现/WS/心跳/任务分发)        │  │
                                    │  ├────────────────────────────────────┤  │
                                    │  │ Orchestration Engine              │  │
                                    │  │ (Kanban 编排/依赖/DAG/Worker 派发) │  │
                                    │  ├────────────────────────────────────┤  │
                                    │  ├────────────────────────────────────┤  │
                                    │  │ Auth & Governance                 │  │
                                    │  │ (OAuth 2.1/JWT RS256/Scope 鉴权/Admin) │  │
                                    │  ├────────────────────────────────────┤  │
                                    │  │ Store (统一 SQLite)                  │  │
                                    │  │ registry.db: agents / oauth_clients │  │
                                    │  │ / oauth_tokens / auth_codes         │  │
                                    │  │ WAL 模式 + 线程安全 RLock            │  │
                                    │  │ 自动迁移旧 JSON 数据                  │  │
                                    │  └────────────────────────────────────┘  │
                                    └──────────────────┬───────────────────────┘
                                                        │
                         ┌──────────────────────────────┼──────────────────────────────┐
                         │                               │                              │
                 ┌───────▼───────┐             ┌─────────▼────────┐      ┌─────────────▼──┐
                 │  Agent A      │             │   Agent B        │      │  Worker       │
                 │ (HTTP+WS)     │             │  (WS 长连接)      │      │ (Kanban Worker)│
                 └───────────────┘             └──────────────────┘      └───────────────┘
```

## 核心能力

### 1. Agent 注册与发现

Agent 通过 REST API 注册，通过 HTTP 心跳或 WebSocket 保活。支持按技能、标签、全文搜索 Agent。

- **Agent Card v1.0** — 数据模型对齐 A2A v1.0 protobuf 规范
- **HTTP 心跳** — `POST /v1/agents/{id}/heartbeat`，120s 超时 / 300s 清理
- **WebSocket 长连接** — Agent 通过 WS 建立持久连接，支持主动任务推送
- **健康检查** — `GET /health`，返回总 Agent 数、活跃数、WS 连接数等统计

### 2. 任务分发

客户端通过 Registry 向已连接的 Agent 分发任务，Agent 通过 WebSocket 实时接收，处理完成后返回结果。

**工作流：**
1. 客户端 `POST /agents/{id}/dispatch` 提交任务
2. Registry 通过 WebSocket 推送 `{"type": "task", ...}` 给 Agent
3. Agent 处理中，可上报 `task_progress` 进度
4. Agent 完成后上报 `task_result`
5. 客户端 `GET /tasks/{id}` 轮询结果

### 3. 编排引擎（Orchestration Engine）

完整的 Kanban 级任务编排能力，提供任务生命周期管理、依赖链、Worker 自动派发、人机协同。

| 能力 | 说明 |
|------|------|
| **任务生命周期管理** | 从创建到归档的完整 8 状态状态机，依赖链、重试、超时释放 |
| **Worker 自动派发** | 基于 Profile 的原子化任务认领与派发，防重复执行 |
| **多 Agent 协调** | 通过依赖链（DAG）和 Workspace 隔离，多 Agent 分阶段协作 |
| **人机协同** | Block/Unblock 机制、评论线程，Human-in-the-Loop |
| **可观测性** | 全事件审计日志、任务运行记录、结构化元数据 |
| **非侵入集成** | 不改动 Agent 发现和 WS Hub 模块 |

详见 [docs/architecture.md](docs/architecture.md)。

### 4. OAuth 2.1 认证与授权

所有 API 端点可选 JWT Bearer Token 保护，基于 Scope 的细粒度鉴权。

| 模式 | 说明 |
|------|------|
| 开发模式 | `--auth-enabled false`（默认），所有端点无需认证 |
| 生产模式 | `--auth-enabled true`，受保护端点需 Bearer Token |

**Scope 体系：**

| Scope | 权限 | 适用端点 |
|-------|------|---------|
| `task:read` | 读取任务 | 任务查询 |
| `task:write` | 创建/修改任务 | 任务操作、分发 |
| `agent:read` | 读取 Agent | Agent 查询 |
| `agent:register` | 注册 Agent | 注册端点 |
| `agent:admin` | 删除/禁用 Agent | Agent 管理 |
| `registry:admin` | Registry 管理 | Admin 客户端管理 |

**认证流程（方案C — Admin 预创建模式）：**
1. Admin 通过 CLI/API/Web UI 预创建 OAuth 客户端凭据
2. Admin 将 `client_id` / `client_secret` 分发给 Agent
3. Agent 凭 credentials 调用 `POST /auth/token` 获取 JWT Bearer Token
4. Agent 使用 Token（需要 `agent:register` scope）调用 `POST /v1/agents` 注册
5. Token 用于调用其他受保护端点

公开端点（无需认证）：`/health`、`/.well-known/*`、`/auth/*`

## 快速开始

```bash
pip install simple-a2a-registry
a2a-registry
```

打开 http://localhost:8321 查看 Dashboard。

### 开启认证（生产环境）

```bash
a2a-registry --auth-enabled true
```
启动后日志会显示 `🔐 auth enabled`。

### 注册 Agent（无认证模式）

```bash
curl -s -X POST http://localhost:8321/v1/agents \
  -H "Content-Type: application/json" \
  -d '{"name": "My Agent", "description": "A test agent"}'
```

### 注册 Agent（认证模式）

```bash
# Admin 预创建客户端
curl -s -X POST http://localhost:8321/admin/clients \
  -H "Authorization: Bearer <ADMIN_TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{"description": "My Agent", "allowed_scopes": ["agent:register", "agent:read"]}'

# Agent 获取 Token
TOKEN=$(curl -s -X POST http://localhost:8321/auth/token \
  -d "grant_type=client_credentials" \
  -d "client_id=<CLIENT_ID>" \
  -d "client_secret=<CLIENT_SECRET>" \
  -d "scope=agent:register agent:read" | jq -r '.access_token')

# Agent 注册
curl -s -X POST http://localhost:8321/v1/agents \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name": "My Agent", "description": "A test agent"}'
```

## 命令行参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--host` | `0.0.0.0` | 监听地址 |
| `--port` | `8321` | 监听端口 |
| `--data-dir` | `~/.simple-a2a-registry` | 数据目录 |
| `--auth-enabled` | `false` | 开启 OAuth 2.1 认证 |
| `--bootstrap-secret` | 自动生成 | 指定 Registry 服务账号（simple-a2a-registry）的 client_secret；不传则自动生成并打印到日志 |
| `--board-path` | `<data-dir>/board.db` | 编排引擎 SQLite 数据库路径 |
| `--dispatcher-enabled` | `true` | 后台 Worker 派发器开关 |
| `--dispatcher-interval` | `5` | 派发器轮询间隔（秒） |
| `--claim-ttl` | `900` | 认领锁 TTL（15 分钟） |
| `--failure-limit` | `3` | 全局默认重试次数 |
| `--workspaces-root` | `<data-dir>/workspaces` | 工作区根目录 |

## API 端点速览

### Agent 管理
| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/v1/agents` | 列出/搜索 Agent |
| GET | `/v1/agents/{id}` | Agent 详情 |
| POST | `/v1/agents` | 注册 Agent |
| DELETE | `/v1/agents/{id}` | 注销 Agent |

### 心跳与连接
| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/v1/agents/{id}/heartbeat` | HTTP 心跳 |
| GET | `/v1/agents/{id}/ws` | WebSocket 持久连接 |

### 任务
| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/v1/agents/{id}/dispatch` | 分发任务到 WS 连接的 Agent |
| GET | `/v1/tasks` | 任务列表 |
| GET | `/v1/tasks/{id}` | 任务状态与结果 |

### 编排引擎
| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/v2/tasks` | 创建编排任务 |
| GET | `/v2/tasks` | 任务列表查询 |
| GET | `/v2/tasks/{id}` | 任务详情（含依赖链、运行记录、事件流） |
| POST | `/v2/tasks/{id}/claim` | Worker 原子认领 |
| POST | `/v2/tasks/{id}/complete` | 完成任务 |
| POST | `/v2/tasks/{id}/block` | 阻塞任务（HITL） |
| POST | `/v2/tasks/{id}/unblock` | 解除阻塞 |
| POST | `/v2/tasks/{id}/heartbeat` | 任务级心跳 |
| POST | `/v2/tasks/{id}/comment` | 添加评论 |
| DELETE | `/v2/tasks/{id}` | 归档任务 |
| POST | `/v2/tasks/{id}/depend` | 添加依赖关系 |
| DELETE | `/v2/tasks/{id}/depend/{parent_id}` | 移除依赖关系 |
| GET | `/v2/stats` | 编排引擎统计 |

### OAuth 与 Admin
| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/auth/token` | 获取 JWT Token |
| POST | `/auth/register` | 注册 OAuth 客户端 |
| GET | `/.well-known/oauth-authorization-server` | OAuth 元数据 |
| GET | `/.well-known/jwks.json` | JWT 公钥 |
| POST | `/admin/clients` | Admin 创建客户端 |
| GET | `/admin/clients` | Admin 列出客户端 |
| DELETE | `/admin/clients/{id}` | Admin 删除客户端 |

### 系统
| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/health` | 健康检查 |
| GET | `/.well-known/agent-card.json` | Registry 自身 Agent Card |

完整 API 参考请参见 [docs/API.md](docs/API.md)。

## Dashboard

打开 http://localhost:8321 查看 Web Dashboard：

- Agent 列表（状态、标签、技能、WS 连接标识）
- Agent 详情展开面板
- Kanban 看板（Board / List 双视图）
- 任务详情弹窗（依赖链、运行记录、事件流、评论）
- 实时统计与刷新（每 15 秒）

## 使用示例

### 列出 Agent

```bash
curl -s http://localhost:8321/v1/agents
curl -s "http://localhost:8321/v1/agents?skill=Software+Development"
curl -s "http://localhost:8321/v1/agents?q=test"
```

### 发送心跳

```bash
curl -s -X POST http://localhost:8321/v1/agents/AGENT_ID/heartbeat
# 响应: {"id":"...","status":"alive","stale_timeout":120}
```

### 分发任务并轮询结果

```bash
# 分发
TASK_ID=$(curl -s -X POST http://localhost:8321/v1/agents/AGENT_ID/dispatch \
  -H "Content-Type: application/json" \
  -d '{"query": "Write hello world in Python"}' | jq -r '.task_id')

# 轮询结果
curl -s "http://localhost:8321/v1/tasks/$TASK_ID" | jq .
```

### 创建编排任务

```bash
curl -s -X POST http://localhost:8321/v2/tasks \
  -H "Content-Type: application/json" \
  -d '{
    "title": "实现登录模块",
    "body": "## 需求\n实现用户登录功能...",
    "assignee": "coder-agent",
    "priority": 1
  }'

# 带依赖的任务
curl -s -X POST http://localhost:8321/v2/tasks \
  -H "Content-Type: application/json" \
  -d '{
    "title": "编写测试",
    "parents": ["t_parent_id"]
  }'
```

### Python 示例

```python
import requests
import os

# 认证模式：从环境变量读取凭据
CLIENT_ID = os.environ.get("OAUTH_CLIENT_ID", "client-xxx")
CLIENT_SECRET = os.environ.get("OAUTH_CLIENT_SECRET", "***")

# 获取 Token
resp = requests.post(
    "http://localhost:8321/auth/token",
    data={
        "grant_type": "client_credentials",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "scope": "agent:register agent:read",
    },
)
token = resp.json()["access_token"]

# 注册 Agent
headers = {"Authorization": f"Bearer {token}"}
resp = requests.post(
    "http://localhost:8321/v1/agents",
    headers=headers,
    json={"name": "My Agent", "description": "A test agent"},
)
agent_id = resp.json()["id"]
print(f"Registered as: {agent_id}")
```

## A2A Coder Agent（示例）

`examples/a2a_coder_agent.py` 是一个完整的 A2A 协议兼容 Agent 示例，用于 Hermes Coder Profile：

- **OAuth Client Credentials 认证** — 从配置文件/环境变量加载凭据，自动获取并刷新 JWT Token
- **AgentCard 注册** — 启动时向 Registry 注册 AgentCard，30 秒心跳保活
- **WebSocket 长连接** — 接收 Registry 推送任务（type: "task"），完成后上报结果
- **A2A JSONRPC over HTTP** — 提供 `POST /tasks/send`、`GET /tasks/{id}` 等 A2A 标准端点（端口 9001）
- **自动重连** — WebSocket 断开时自动重连，指数退避

```bash
# 启动 Coder Agent
export OAUTH_CLIENT_ID=client-xxx OAUTH_CLIENT_SECRET=*** && python examples/a2a_coder_agent.py
```

## 项目结构

```
simple_a2a_registry/
  cli.py          — argparse CLI 入口
  server.py       — aiohttp REST API + WebSocket + 编排引擎
  store.py        — 统一 SQLite 持久化（Store 类：Agent 注册 + OAuth 客户端管理）
  models.py       — A2A Agent Card 数据模型
  orchestration/  — 编排引擎模块
    task_store.py   — SQLite 任务存储
    dispatcher.py   — Worker 派发器
    state_machine.py— 8 状态状态机
    routes.py       — API 路由
    workspace.py    — 工作区管理器
  static/         — Web Dashboard（HTML+JS）
  examples/
    a2a_coder_agent.py — A2A 兼容 Coder Agent（OAuth 认证 + WS 长连接 + A2A JSONRPC）
tests/
  test_store.py   — 存储层测试
  test_models.py  — 数据模型测试
  test_server.py  — HTTP API 集成测试
docs/
  architecture.md — 系统架构设计
  API.md          — API 参考
  oauth-design.md — OAuth 2.1 认证设计
```