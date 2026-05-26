# Simple A2A Registry

轻量级、符合 Google A2A (Agent-to-Agent) 协议的 Agent 注册中心。
支持 Agent 注册/发现、心跳保活、WebSocket 长连接、任务分发与状态查询，
以及 V2 **Kanban 编排引擎**（Orchestration Engine）。

## 架构概览

```
┌─────────────┐      HTTP/WS       ┌──────────────────────────────────────┐
│   Client    │ ──────────────────→ │  A2A Registry (localhost:8321)      │
│ (调用方)     │                     │                                      │
└─────────────┘                     │  ┌────────────────────────────────┐  │
                                    │  │ V1: Agent Registry & Dispatch  │  │
                                    │  │  (注册/发现/WS/心跳/任务分发)    │  │
                                    │  ├────────────────────────────────┤  │
                                    │  │ V2: Orchestration Engine       │  │
                                    │  │  (Kanban 任务编排/派发/工作区)   │  │
                                    │  └────────────────────────────────┘  │
                                    └──────────────────┬───────────────────┘
                                                        │
                         ┌──────────────────────────────┼──────────────────────────────┐
                         │                               │                              │
                 ┌───────▼───────┐             ┌─────────▼────────┐      ┌─────────────▼──┐
                 │  Agent A      │             │   Agent B        │      │  Worker C     │
                 │ (HTTP+WS)     │             │  (WS 长连接)      │      │ (V2 Profile)  │
                 └───────────────┘             └──────────────────┘      └───────────────┘
```

**V1 核心工作流（认证模式）：**
1. **Admin 预创建**：管理员通过 CLI 或 Web UI 预创建 OAuth 2.1 client credentials（client_id + client_secret）
2. **分发凭证**：Admin 将 credentials 通过安全渠道（配置文件、环境变量）分发给 Agent
3. **获取 Token**：Agent 凭 client_id/client_secret 调用 `POST /auth/token` 获取 Bearer Token
4. **注册 Agent**：Agent 使用 Bearer Token（需 `agent:register` scope）调用 `POST /v1/agents` 注册
5. Agent 通过 WebSocket (`/v1/agents/{id}/ws`) 建立长连接，或通过 HTTP 心跳保持活跃
6. 客户端通过 `POST /v1/agents/{id}/dispatch` 向已连接的 Agent 分发任务
7. Agent 通过 WebSocket 接收任务、处理、返回结果
8. 客户端通过 `GET /v1/tasks/{id}` 轮询任务状态和结果

**V2 编排引擎：** 可选的 Kanban 级任务编排系统，支持任务生命周期管理、依赖链、自动 Worker 派发、HITL 人工介入等。详见下方 [V2 Orchestration Engine](#v2-orchestration-engine) 章节。

> 详细架构说明请参见 [docs/architecture.md](docs/architecture.md)（V1）和 [docs/architecture-v2.md](docs/architecture-v2.md)（V2）。

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

启动后日志会显示 `🔐 auth enabled`。所有受保护端点需要 Bearer Token。

### Admin 预创建 Client Credentials

认证模式下，Agent 不再通过注册自动获取凭证。Admin 需先创建 OAuth 2.1 客户端，再将凭证分发给 Agent。

**方式一：Web UI**
1. 打开 Dashboard http://localhost:8321
2. 进入「OAuth Clients」管理页面
3. 点击「创建客户端」，填写名称和允许的 Scopes
4. 系统生成 client_id 和 client_secret（仅显示一次）

**方式二：CLI（管理命令）**
```bash
# 管理命令通过 Registry 启动后的 WebSocket 管理通道或直接操作数据文件
# 创建客户端（需要 registry:admin scope 的 Token）
curl -s -X POST http://localhost:8321/admin/clients \
  -H "Authorization: Bearer ***" \
  -H "Content-Type: application/json" \
  -d '{
    "description": "My Agent",
    "allowed_scopes": ["agent:register", "agent:read", "task:read", "task:write"]
  }'

# 响应包含 client_id 和 client_secret（仅此一次）
# { "client_id": "client-a1b2c3d4e5f6", "client_secret": "***" }

# 查看所有客户端
curl -s http://localhost:8321/admin/clients \
  -H "Authorization: Bearer *** | jq .

# 删除客户端
curl -s -X DELETE http://localhost:8321/admin/clients/client-a1b2c3d4e5f6 \
  -H "Authorization: Bearer ***
```

### Agent 注册 + 获取 Token（认证模式）

Admin 预创建 client 后，Agent 按以下流程注册：

```bash
# 1. Admin 预创建的 client_id 和 client_secret 已通过安全渠道获取

# 2. 获取 Access Token
TOKEN=$(curl -s -X POST http://localhost:8321/auth/token \
  -d "grant_type=client_credentials" \
  -d "client_id=<ADMIN_CREATED_CLIENT_ID>" \
  -d "client_secret=<ADMIN_CREATED_CLIENT_SECRET>" \
  -d "scope=agent:register agent:read" | jq -r '.access_token')

# 3. 使用 Token 注册 Agent（POST /v1/agents 需要 agent:register scope）
curl -s -X POST http://localhost:8321/v1/agents \
  -H "Authorization: Bearer *** \
  -H "Content-Type: application/json" \
  -d '{
    "name": "My Agent",
    "description": "A test agent",
    "supported_interfaces": [{"url": "http://localhost:9001", "type": "a2a"}],
    "capabilities": {"streaming": false},
    "skills": [{"id": "skill-1", "name": "Skill One"}],
    "default_input_modes": ["text/plain"],
    "default_output_modes": ["text/plain"]
  }'

# 注意：注册响应不再返回 client_id/client_secret（已由 Admin 预创建）
# Agent 注册时声明的 security_schemes 用于告知其他 Agent 如何与它安全通信
# 而非用于在本 Registry 创建 OAuth 客户端

# 4. 使用 Token 调用其他受保护 API
curl -s -H "Authorization: Bearer $TOKEN" \
  http://localhost:8321/v1/agents | jq .
```

## 命令行

```bash
# V1 基础模式
a2a-registry --host 0.0.0.0 --port 8321 --data-dir ~/.simple-a2a-registry

# 完整模式（V1 + V2 编排引擎）
a2a-registry --host 0.0.0.0 --port 8321 --dispatcher-enabled true

# 仅 V1 模式（关闭编排引擎）
a2a-registry --host 0.0.0.0 --port 8321 --dispatcher-enabled false
```

### V2 编排引擎参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--board-path` | `<data-dir>/board.db` | SQLite 数据库路径 |
| `--dispatcher-enabled` | `true` | 是否启动后台 Worker 派发器 |
| `--dispatcher-interval` | `5` | 派发器轮询间隔（秒） |
| `--claim-ttl` | `900` | 认领锁 TTL（秒，15 分钟） |
| `--failure-limit` | `3` | 全局默认重试次数 |
| `--workspaces-root` | `<data-dir>/workspaces` | Scratch 工作区根目录 |
| `--auth-enabled` | `false` | 启用 OAuth 2.1 认证中间件（生产环境建议开启） |

### 已废弃参数

| 参数 | 替代 | 说明 |
|------|------|------|
| `--dispatcher` | `--dispatcher-enabled` | V1 兼容别名，使用时会打印警告 |

## Dashboard

打开 http://localhost:8321 查看 Web Dashboard：

- Agent 列表（状态、标签、技能）
- Agent 详情展开面板
- WebSocket 连接标识（WS 徽章）
- V2 Kanban 看板（Board/List 双视图）
- 任务详情弹窗（依赖链、运行记录、事件流、评论）
- V1+V2 统一统计
- 实时刷新（每 15 秒）

---

## V2 Orchestration Engine

V2 在 V1 的基础上新增 **Orchestration Engine（编排引擎）**，提供完整的 Kanban 级任务编排能力：

| 能力 | 说明 |
|------|------|
| **任务生命周期管理** | 从创建到归档的完整状态机，支持依赖链、重试、超时释放 |
| **Worker 自动派发** | 基于 Profile 的原子化任务认领与派发，防止重复执行 |
| **多 Agent 协调** | 通过依赖链和 Workspace 隔离，实现多 Agent 分阶段协作 |
| **人机协同** | Block/Unblock 机制、评论线程，支持 Human-in-the-Loop |
| **可观测性** | 全事件审计日志、任务运行记录、结构化元数据 |
| **OAuth 2.1 认证** | 基于 JWT 的 Bearer Token 认证，支持 Client Credentials / Authorization Code + PKCE |
| **Agent Card v1.0** | 数据模型对齐 A2A v1.0 protobuf 规范，含 SecurityScheme 和 Capabilities 重构 |
| **非侵入集成** | 不改动 V1 的 Agent 发现和 WS Hub 模块 |

### 数据模型

V2 使用独立 SQLite 数据库，通过 WAL 模式 + `BEGIN IMMEDIATE` 事务保证并发安全。

| 表 | 说明 |
|----|------|
| `tasks` | 核心任务实体（含状态、指派人、认领锁、优先级、工作区等） |
| `task_links` | 父子依赖关系（多入度、扇出、自动提升） |
| `task_runs` | 每次 Worker 执行的运行记录（含 outcome/summary/error/metadata） |
| `task_comments` | 评论线程（Markdown 格式、作者追踪） |
| `task_events` | 审计事件流（创建/认领/完成/阻塞/重试等事件） |

### 状态机

```
                    ┌─────────────────────┐
           ┌───────│       todo          │◄──── 有未完成的 parent
           │       │ 待执行（依赖未满足）  │
           │       └──────────┬──────────┘
           │                  │ 所有 parent 完成
           │                  ▼
           │       ┌─────────────────────┐
           │       │       ready         │◄──── TTL 超时释放回来
           │       │ 准备就绪（可被认领）  │
           │       └──────────┬──────────┘
           │                  │ Worker 原子认领
           │                  ▼
           │       ┌─────────────────────┐
           │       │      running        │
           │       │ 执行中              │──────┬──────────┐
           │       └─────────────────────┘      │          │
           │                │     ▲             │          │
           │           ┌────┘     └────┐        │          │
           │           ▼               ▼        ▼          │
           │  ┌──────────────┐  ┌──────────┐  ┌─────────┐ │
           │  │   blocked    │  │completed │  │ failed  │ │
           │  │ 人工阻塞等待   │  │ 成功完成  │  │ 失败    │ │
           │  └──────┬───────┘  └──────────┘  └────┬────┘ │
           │         │               │              │      │
           │         ▼               ▼              ▼      │
           │  ┌─────────────────────────────────────────┐  │
           │  │               archived                  │──┘
           │  │  归档（终点状态，不再调度）               │
           │  └─────────────────────────────────────────┘
           └─────────────────────────────────────────────────┘
```

### V2 API 端點一览

所有 V2 端点统一以 `/v2/` 为前缀，与 V1 端点共存。认证启用时需 `Authorization: Bearer ***` header。

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/v2/tasks` | 创建任务 |
| GET | `/v2/tasks` | 列表查询 |
| GET | `/v2/tasks/{id}` | 任务详情（含依赖链、评论、运行历史、事件流） |
| POST | `/v2/tasks/{id}/claim` | Worker 原子认领 |
| POST | `/v2/tasks/{id}/complete` | 完成任务 |
| POST | `/v2/tasks/{id}/block` | 阻塞任务（HITL） |
| POST | `/v2/tasks/{id}/unblock` | 解除阻塞 |
| POST | `/v2/tasks/{id}/heartbeat` | 任务心跳（延长 TTL） |
| POST | `/v2/tasks/{id}/comment` | 添加评论 |
| DELETE | `/v2/tasks/{id}` | 归档任务 |
| POST | `/v2/tasks/{id}/depend` | 添加依赖关系 |
| DELETE | `/v2/tasks/{id}/depend/{parent_id}` | 移除依赖关系 |

### V2 统计端點

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/v2/stats` | 编排引擎统计（按状态分组） |

详细 V2 API 请参见 [docs/architecture-v2.md](docs/architecture-v2.md#4-api-契约)。

### 使用示例

```bash
# 1. 创建任务
curl -s -X POST http://localhost:8321/v2/tasks \
  -H "Content-Type: application/json" \
  -d '{
    "title": "实现登录模块",
    "body": "## 需求\n实现用户登录功能...",
    "assignee": "coder-agent",
    "priority": 1
  }'

# 2. 查询任务列表
curl -s "http://localhost:8321/v2/tasks?status=ready"

# 3. 获取任务详情（含依赖链、运行记录、评论、事件）
curl -s "http://localhost:8321/v2/tasks/t_a1b2c3d4"

# 4. 创建带依赖的任务
curl -s -X POST http://localhost:8321/v2/tasks \
  -H "Content-Type: application/json" \
  -d '{
    "title": "编写测试",
    "parents": ["t_parent_id"]
  }'

# 5. 查询 V2 统计
curl -s "http://localhost:8321/v2/stats"
```

---

## V1 API 参考

### Agent 管理

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/v1/agents` | 列出/搜索 Agent（支持 `?skill=`、`?tag=`、`?q=`、`?limit=`、`?offset=`） |
| GET | `/v1/agents/{id}` | 获取 Agent 详情 |
| POST | `/v1/agents` | 注册 Agent（Body: `{"name": "...", "capabilities": {...}}`） |
| DELETE | `/v1/agents/{id}` | 注销 Agent |

### 心跳保活

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/v1/agents/{id}/heartbeat` | 发送心跳（成功返回 203） |

### WebSocket 长连接

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/v1/agents/{id}/ws` | Agent WebSocket 长连接端点 |

**WebSocket 消息协议：**

| 方向 | type | 说明 |
|------|------|------|
| Agent → Registry | `ping` | Registry 回复 `pong` |
| Agent → Registry | `task_result` | 报告任务完成：`{"type":"task_result","id":"...","status":"completed","result":{...}}` |
| Agent → Registry | `task_progress` | 报告任务进度：`{"type":"task_progress","id":"...","status":"working"}` |
| Agent → Registry | `close` | 主动关闭连接 |
| Registry → Agent | `task` | 分发任务：`{"type":"task","id":"...","query":"...","sessionId":"..."}` |
| Registry → Agent | `close` | 连接被替换/关闭通知：`{"type":"close","reason":"replaced"}` |

### 任务分发

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/v1/agents/{id}/dispatch` | 向 Agent 分发任务，要求 Agent 已通过 WebSocket 连接 |
| GET | `/v1/tasks` | 列出 V1 任务 |
| GET | `/v1/tasks/{id}` | 查询 V1 任务状态和结果 |

**分发请求体：**
```json
{
  "query": "写一个 Python 排序函数",
  "sessionId": "可选会话 ID"
}
```

**分发响应（202）：**
```json
{
  "task_id": "uuid",
  "agent_id": "...",
  "state": "forwarded",
  "query": "...",
  "created_at": 1700000000.0
}
```

### 其他

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/health` | 健康检查（含统计：total_agents、alive_agents、connected_via_ws 等） |
| GET | `/.well-known/agent-card.json` | Registry 自身的 A2A Agent Card |
| POST | `/v1/agents/{id}/task` | 代理任务跳转（已废弃，推荐使用 /dispatch 替代） |

## 使用示例

### 注册 Agent（curl）

```bash
# 注册 Agent
curl -s -X POST http://localhost:8321/v1/agents \
  -H "Content-Type: application/json" \
  -d '{"name": "My Agent", "description": "A test agent"}'

# 响应: {"message":"Agent registered successfully","id":"...","card":{...}}
```

### 列出 Agent（curl）

```bash
# 列出所有 Agent
curl -s http://localhost:8321/v1/agents

# 按技能过滤
curl -s "http://localhost:8321/v1/agents?skill=Software+Development"

# 全文搜索
curl -s "http://localhost:8321/v1/agents?q=test"
```

### 发送心跳（curl）

```bash
curl -s -X POST http://localhost:8321/v1/agents/AGENT_ID/heartbeat
# 响应 (203): {"id":"...","status":"alive","stale_timeout":120}
```

### 分发任务并轮询结果（curl）

```bash
# 1. 分发任务给已 WS 连接的 Agent
TASK_ID=$(curl -s -X POST http://localhost:8321/v1/agents/AGENT_ID/dispatch \
  -H "Content-Type: application/json" \
  -d '{"query": "Write hello world in Python"}' | jq -r '.task_id')

echo "Task ID: $TASK_ID"

# 2. 轮询结果
curl -s "http://localhost:8321/v1/tasks/$TASK_ID" | jq .
```

### 健康检查（curl）

```bash
curl -s http://localhost:8321/health | jq .
# 快速检查 WS 连接数
curl -s http://localhost:8321/health | jq '.stats.connected_via_ws'
```

### Python 示例

### 注册 Agent 并建立 WebSocket 连接

```python
import asyncio
import json
from aiohttp import ClientSession, ClientWebSocketResponse

async def agent_example():
    async with ClientSession() as session:
        # 1. 注册
        resp = await session.post("http://localhost:8321/v1/agents", json={
            "name": "My Agent",
            "description": "A test agent",
        })
        agent_id = (await resp.json())["id"]
        print(f"Registered as: {agent_id}")

        # 2. 建立 WebSocket 长连接
        ws = await session.ws_connect(
            f"http://localhost:8321/v1/agents/{agent_id}/ws"
        )
        print("WebSocket connected")

        # 3. 保持连接，接收任务
        async for msg in ws:
            data = json.loads(msg.data)
            if data["type"] == "task":
                print(f"Received task: {data['query']}")
                # 处理任务...
                await ws.send_json({
                    "type": "task_result",
                    "id": data["id"],
                    "status": "completed",
                    "result": {"text": "done"},
                })
            elif data["type"] == "ping":
                await ws.send_json({"type": "pong"})

asyncio.run(agent_example())
```

### 分发任务并轮询结果

```python
import requests

# 1. 分发任务
resp = requests.post(
    "http://localhost:8321/v1/agents/{agent_id}/dispatch",
    json={"query": "Write hello world in Python"},
)
task_id = resp.json()["task_id"]
print(f"Task dispatched: {task_id}")

# 2. 轮询结果
import time
while True:
    resp = requests.get(f"http://localhost:8321/v1/tasks/{task_id}")
    task = resp.json()
    print(f"State: {task['state']}")
    if task["state"] in ("completed", "failed"):
        print(f"Result: {task.get('result')}")
        break
    time.sleep(1)
```

### V2 编排引擎示例

```bash
# 0. 获取 Token（认证模式需要）
TOKEN=$(curl -s -X POST http://localhost:8321/auth/token \
  -d "grant_type=client_credentials" \
  -d "client_id=<CLIENT_ID>" \
  -d "client_secret=<CLIENT_SECRET>" \
  -d "scope=task:read task:write" | jq -r '.access_token')

# 认证模式：所有 V2 端点需 -H "Authorization: Bearer $TOKEN"

# 1. 创建任务
curl -s -X POST http://localhost:8321/v2/tasks \
  -H "Content-Type: application/json" \
  ${TOKEN:+-H "Authorization: Bearer $TOKEN"} \
  -d '{
    "title": "设计数据库 Schema",
    "assignee": "designer-agent"
  }'

# 2. 创建依赖子任务
curl -s -X POST http://localhost:8321/v2/tasks \
  -H "Content-Type: application/json" \
  ${TOKEN:+-H "Authorization: Bearer $TOKEN"} \
  -d '{
    "title": "实现业务逻辑",
    "assignee": "coder-agent",
    "parents": ["t_parent_task_id"]
  }'

# 3. 查询状态
curl -s ${TOKEN:+-H "Authorization: Bearer $TOKEN"} \
  "http://localhost:8321/v2/tasks?status=todo,ready,running"

# 4. 获取编排引擎统计
curl -s ${TOKEN:+-H "Authorization: Bearer $TOKEN"} \
  http://localhost:8321/v2/stats | jq .
```

## 文档

更多详细文档请参见 `docs/` 目录：

| 文档 | 说明 |
|------|------|
| [docs/API.md](docs/API.md) | V1 + V2 完整 API 参考（含 OAuth 2.1 认证端点） |
| [docs/architecture.md](docs/architecture.md) | V1 架构设计、Agent Card v1.0 数据模型变更、OAuth 2.1 认证架构、安全集成流程 |
| [docs/architecture-v2.md](docs/architecture-v2.md) | V2 编排引擎架构设计（状态机、API 契约、Dispatcher、Workspace、HITL、认证与安全） |
| [docs/a2a-v1-agent-card-oauth-design.md](docs/a2a-v1-agent-card-oauth-design.md) | Agent Card v1.0 + OAuth 2.1 设计文档（含 PM Review） |

## License

MIT
