# Simple A2A Registry — API 参考

## 基础 URL

默认：`http://localhost:8321`

> 🔒 **认证模式**：默认 `--auth-enabled false`（开发模式，所有端点无需认证）。
> 开启 `--auth-enabled true` 后，受保护端点需要 `Authorization: Bearer *** header。
> Scope 要求见下文各端点说明。

---

## 认证与公开端点

### 端点分类

| 类型 | 说明 | 认证要求 |
|------|------|---------|
| 公开端点 | 健康检查、服务发现、Token 获取 | 始终无需认证 |
| 受保护端点 | Agent 管理、任务操作、Admin 管理 | 需 `Authorization: Bearer *** |

### 公开端点一览

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/health` | 健康检查与统计 |
| GET | `/.well-known/agent-card.json` | Registry 自身的 A2A Agent Card |
| GET | `/.well-known/oauth-authorization-server` | OAuth 2.0 Authorization Server 元数据 |
| GET | `/.well-known/jwks.json` | JWT 公钥（RS256 模式） |
| POST | `/auth/token` | 获取 JWT access_token |
| POST | `/auth/register` | 注册 OAuth 客户端 |

### Authorization Header

所有受保护端点要求：

```
Authorization: Bearer ***
```

### Scope 速查表

| Scope | 描述 | 适用端点 |
|-------|------|---------|
| `task:read` | 读取任务列表和详情 | 任务查询端点多 |
| `task:write` | 创建和修改任务 | 任务操作端多点 |
| `agent:read` | 读取 Agent 列表和详情 | Agent 查询端点多 |
| `agent:register` | 注册新 Agent | `POST /v1/agents` |
| `agent:admin` | 管理 Agent（删除/禁用） | `DELETE /v1/agents/{id}` |
| `registry:admin` | Registry 管理操作 | Admin 客户端端点多 |

---

## OAuth 2.1 认证端点

### 获取 Token

```
POST /auth/token
Content-Type: application/x-www-form-urlencoded
```

**请求参数：**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `grant_type` | string | 是 | `client_credentials` / `authorization_code` |
| `client_id` | string | 是 | 客户端 ID（Admin 预创建时获得） |
| `client_secret` | string | 是 | 客户端密钥（仅创建时可见） |
| `scope` | string | 否 | 请求的权限范围（空格分隔，如 `task:read task:write`） |
| `code` | string | 否 | Authorization Code（`authorization_code` 时必填） |
| `code_verifier` | string | 否 | PKCE code verifier |

**Client Credentials 示例：**

```bash
curl -s -X POST http://localhost:8321/auth/token \
  -d "grant_type=client_credentials" \
  -d "client_id=client-xxx" \
  -d "client_secret=***" \
  -d "scope=task:read task:write"
```

**响应：** `200 OK`
```json
{
  "access_token": "eyJhbG...NiIs...",
  "token_type": "Bearer",
  "expires_in": 3600,
  "scope": "task:read task:write"
}
```

**错误码：**

| 状态码 | error | 说明 |
|--------|-------|------|
| 400 | `invalid_request` | 缺少必填参数 |
| 400 | `invalid_grant` | client_id/client_secret 无效 |
| 400 | `invalid_scope` | scope 超出允许范围 |

### 注册客户端

```
POST /auth/register
Content-Type: application/json
```

**请求体：**
```json
{
  "agent_card_id": "coder-agent",
  "allowed_scopes": ["task:read", "task:write"],
  "description": "My A2A Agent"
}
```

**字段说明：**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `agent_card_id` | string | 否 | 关联的 Agent name（用于 WS auth 校验） |
| `allowed_scopes` | string[] | 否 | 允许的 scope 列表（默认全部） |
| `description` | string | 否 | 客户端描述 |

**响应：** `201 Created`
```json
{
  "client_id": "client-abc123",
  "client_secret": "***",
  "allowed_scopes": ["task:read", "task:write"]
}
```

> **注意：** 生产环境推荐 Admin 预创建模式。Admin 通过 `/admin/clients` 创建客户端，Agent 使用预分配的凭据获取 Token 后注册。不再支持通过 `POST /v1/agents` 自动创建 OAuth 客户端。

---

## Agent 管理端点

### 列出 Agent

```
GET /v1/agents
GET /v1/agents?skill=<技能名>
GET /v1/agents?tag=<标签>
GET /v1/agents?q=<搜索词>
GET /v1/agents?limit=20&offset=0
```

**查询参数：**

| 参数 | 类型 | 说明 |
|------|------|------|
| `skill` | string | 按技能名称过滤（子串匹配） |
| `tag` | string | 按标签精确匹配 |
| `q` | string | 全字段全文搜索 |
| `limit` | int | 最大返回条数（默认 50，最大 200） |
| `offset` | int | 分页偏移（默认 0） |

**响应附加字段：** 如果 Agent 当前通过 WebSocket 连接，返回的卡片中附加 `"connection": "websocket"` 和 `"status": "alive"`。

**认证：** ✅ `agent:read`
**响应：** `200 OK`

### 获取 Agent

```
GET /v1/agents/{agent_id}
```

返回 Agent Card。如果该 Agent 通过 WebSocket 连接，会附加 `connection` 和 `status` 字段。

**认证：** ✅ `agent:read`
**错误码：** 404 — `agent_not_found`

### 注册 Agent

```
POST /v1/agents
```

**请求体：** A2A Agent Card JSON，至少需要 `name` 字段。

```json
{
  "name": "My Agent",
  "description": "A test agent",
  "url": "http://localhost:9001",
  "capabilities": {
    "skills": [
      { "id": "skill-1", "name": "Skill One" }
    ]
  },
  "tags": ["python", "test"]
}
```

**响应：** `201 Created`
```json
{
  "message": "Agent registered successfully",
  "id": "generated-uuid",
  "card": { ... }
}
```

**认证：** ✅ `agent:register`

**错误码：**

| 状态码 | error | 说明 |
|--------|-------|------|
| 400 | `invalid_json` | 请求体不是合法 JSON |
| 400 | `validation_error` | 缺少或空 name 字段 |
| 409 | `agent_exists` | 同名 Agent 已存在（使用 `?force=true` 覆盖） |

### 注销 Agent

```
DELETE /v1/agents/{agent_id}
```

注销 Agent。如果有 WebSocket 连接，自动关闭连接。

**认证：** ✅ `agent:admin`
**响应：** `200 OK`
**错误码：** 404 — `agent_not_found`

---

## 心跳与连接端点

### HTTP 心跳

```
POST /v1/agents/{agent_id}/heartbeat
```

**响应：** `203 Non-Authoritative Information`
```json
{
  "id": "agent-uuid",
  "status": "alive",
  "last_heartbeat": 1712345678.9,
  "expires_at": 1712345798.9,
  "stale_timeout": 120
}
```

**认证：** ✅ `agent:read`

**错误码：**

| 状态码 | error | 说明 |
|--------|-------|------|
| 404 | `agent_not_found` | Agent 不存在 |
| 410 | `agent_stale` | Agent 已过期 |

### WebSocket 长连接

```
GET /v1/agents/{agent_id}/ws
```

Agent 通过 WebSocket 与 Registry 建立持久连接。相比于 HTTP 心跳轮询，WebSocket 使 Registry 能够主动向 Agent 推送任务。

**连接前提：** Agent 必须先通过 `POST /v1/agents` 注册。
**认证：** ✅ WebSocket endpoint 通过查询参数 `?token=<jwt>` 传递 Bearer token。

**行为：**
- 同一 Agent 的第二个 WS 连接会替换第一个（旧连接收到 `{"type":"close","reason":"replaced"}` 后关闭）
- 连接建立时自动更新 Agent 的心跳时间
- 连接断开时自动从活跃连接列表中移除

#### WS 消息协议

**Registry → Agent（服务端推送）：**

| type | 说明 |
|------|------|
| `task` | 分发任务：`{"type":"task","id":"uuid","query":"...","sessionId":"..."}` |
| `close` | 连接被替换/关闭通知：`{"type":"close","reason":"replaced"}` |

**Agent → Registry（客户端上报）：**

| type | 说明 |
|------|------|
| `ping` | 保活心跳，Registry 回复 `{"type":"pong"}` |
| `task_result` | 报告任务完成：`{"type":"task_result","id":"...","status":"completed","result":{...}}` |
| `task_progress` | 报告任务进度：`{"type":"task_progress","id":"...","status":"working"}` |
| `close` | Agent 主动关闭连接 |

> 即使任务不是通过 Registry dispatch 发起的，Agent 也可以主动通过 `task_result` / `task_progress` 汇报结果。Registry 会自动创建对应的任务记录（auto-created）。

---

## 任务分发端点

### 分发任务到 Agent

```
POST /v1/agents/{agent_id}/dispatch
```

通过 Registry 向已连接的 Agent 分发任务。Registry 将任务通过 WebSocket 推送给 Agent，并返回一个 `task_id` 供客户端轮询结果。

**前置条件：** Agent 必须已通过 WebSocket 连接（否则返回 503）。

**请求体：**
```json
{
  "query": "写一个 Python 排序函数",
  "sessionId": "可选会话 ID"
}
```

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `query` | string | 是 | 任务描述 |
| `sessionId` | string | 否 | 会话 ID，用于关联多轮对话 |

**认证：** ✅ `task:write`

**响应：** `202 Accepted`
```json
{
  "task_id": "uuid",
  "agent_id": "...",
  "state": "forwarded",
  "query": "...",
  "created_at": 1700000000.0
}
```

**错误码：** 503 — Agent 未通过 WebSocket 连接

### 查询任务

```
GET /v1/tasks
GET /v1/tasks/{task_id}
```

查询通过 `dispatch` 分发的任务状态和结果。

**认证：** ✅ `task:read`

**查询参数** (`GET /v1/tasks`)：

| 参数 | 类型 | 说明 |
|------|------|------|
| `limit` | int | 分页大小（默认 50） |
| `offset` | int | 分页偏移 |

**响应：**
```json
{
  "task_id": "uuid",
  "agent_id": "...",
  "state": "completed",
  "query": "...",
  "result": { "...": "..." },
  "created_at": 1700000000.0,
  "updated_at": 1700000030.0
}
```

> **注意：** 任务分发端点（`/v1/agents/{id}/dispatch`）适用于轻量级、即时性的任务场景。对于需要编排引擎的复杂场景（依赖链、重试、Worker 调度），请使用编排任务端点（见下文）。

---

## 编排引擎端点

编排引擎提供完整的 Kanban 级任务编排能力：任务生命周期管理、依赖链、Worker 自动派发、HITL。

所有编排端点以 `/v2/` 前缀。

### 端点一览

| 方法 | 路径 | 说明 | 认证 | Scope |
|------|------|------|------|-------|
| POST | `/v2/tasks` | 创建任务 | ✅ | `task:write` |
| GET | `/v2/tasks` | 列表查询 | ✅ | `task:read` |
| GET | `/v2/tasks/{id}` | 任务详情（含依赖链、运行记录、事件流） | ✅ | `task:read` |
| POST | `/v2/tasks/{id}/claim` | Worker 原子认领 | ✅ | `task:write` |
| POST | `/v2/tasks/{id}/complete` | 完成任务 | ✅ | `task:write` |
| POST | `/v2/tasks/{id}/block` | 阻塞任务（HITL） | ✅ | `task:write` |
| POST | `/v2/tasks/{id}/unblock` | 解除阻塞 | ✅ | `task:write` |
| POST | `/v2/tasks/{id}/heartbeat` | 任务级心跳 | ✅ | `task:write` |
| POST | `/v2/tasks/{id}/comment` | 添加评论 | ✅ | `task:write` |
| DELETE | `/v2/tasks/{id}` | 归档任务 | ✅ | `task:write` |
| POST | `/v2/tasks/{id}/depend` | 添加依赖关系 | ✅ | `task:write` |
| DELETE | `/v2/tasks/{id}/depend/{parent_id}` | 移除依赖关系 | ✅ | `task:write` |
| GET | `/v2/stats` | 编排引擎统计 | ✅ | `task:read` |

### 创建任务

```
POST /v2/tasks
```

**请求体：**
```json
{
  "title": "实现登录模块",
  "body": "## 需求\n实现用户登录功能...",
  "assignee": "coder-agent",
  "priority": 1,
  "parents": ["t_parent1", "t_parent2"],
  "workspace_kind": "scratch",
  "max_runtime_seconds": 600,
  "max_retries": 3,
  "tenant": "project-x"
}
```

**字段说明：**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `title` | string | 是 | 任务标题 |
| `body` | string | 否 | 任务描述（Markdown） |
| `assignee` | string | 否 | 指派的 Worker Profile 名 |
| `priority` | int | 否 | 优先级（默认 0） |
| `parents` | string[] | 否 | 父任务 ID 列表（创建时自动建立依赖） |
| `workspace_kind` | string | 否 | 工作区模式：`scratch` / `dir` / `worktree` |
| `max_runtime_seconds` | int | 否 | 单次运行超时上限（秒） |
| `max_retries` | int | 否 | 最大重试次数 |
| `tenant` | string | 否 | 租户/命名空间标签 |

**响应：** `201 Created`
```json
{
  "task": {
    "id": "t_a1b2c3d4",
    "title": "实现登录模块",
    "status": "todo",
    "assignee": "coder-agent",
    "priority": 1,
    "created_at": 1712345678,
    "parents": ["t_parent1", "t_parent2"]
  }
}
```

**错误码：**

| 状态码 | error | 说明 |
|--------|-------|------|
| 400 | `validation_error` | 缺少必填字段或格式错误 |
| 400 | `cycle_detected` | `parents` 中存在循环依赖 |
| 400 | `parent_not_found` | 指定的父任务不存在 |

### 列表查询

```
GET /v2/tasks
```

**查询参数：**

| 参数 | 类型 | 说明 |
|------|------|------|
| `status` | string | 过滤状态（可多选，逗号分隔：`ready,running`） |
| `assignee` | string | 按指派人精确匹配 |
| `tenant` | string | 按租户精确匹配 |
| `parent_id` | string | 按父任务 ID 过滤 |
| `q` | string | 全文搜索（标题+正文） |
| `limit` | int | 分页大小（默认 50，最大 200） |
| `offset` | int | 分页偏移（默认 0） |
| `sort` | string | 排序字段：`created_at`/`priority`/`started_at`（默认 `-created_at`） |

**响应：** `200 OK`
```json
{
  "total": 42,
  "limit": 50,
  "offset": 0,
  "tasks": [
    {
      "id": "t_a1b2c3d4",
      "title": "实现登录模块",
      "status": "running",
      "assignee": "coder-agent",
      "priority": 1,
      "created_at": 1712345678,
      "started_at": 1712345700
    }
  ]
}
```

### 任务详情

```
GET /v2/tasks/{id}
```

**响应：** `200 OK`
```json
{
  "task": {
    "id": "t_a1b2c3d4",
    "title": "实现登录模块",
    "body": "## 需求\n实现用户登录功能...",
    "assignee": "coder-agent",
    "status": "running",
    "priority": 1,
    "created_by": "user-admin",
    "created_at": 1712345678,
    "started_at": 1712345700,
    "workspace_kind": "scratch",
    "workspace_path": "/tmp/kanban-ws/t_a1b2c3d4",
    "claim_lock": "worker-1:12345",
    "claim_expires": 1712346600,
    "max_runtime_seconds": 600,
    "max_retries": 3,
    "consecutive_failures": 0,
    "current_run_id": 5
  },
  "parents": [
    {"id": "t_parent1", "title": "设计数据库", "status": "completed"}
  ],
  "children": [
    {"id": "t_child1", "title": "编写测试", "status": "todo"}
  ],
  "runs": [
    {
      "id": 5,
      "status": "running",
      "profile": "coder-agent",
      "started_at": 1712345700,
      "last_heartbeat_at": 1712345760
    }
  ],
  "comments": [
    {
      "id": 1,
      "author": "reviewer",
      "body": "请添加单元测试",
      "created_at": 1712345800
    }
  ],
  "events": [
    {
      "id": 10,
      "kind": "started",
      "created_at": 1712345700,
      "payload": {"profile": "coder-agent", "pid": 12345}
    }
  ]
}
```

**错误码：** 404 — `task_not_found`

### Worker 认领任务

```
POST /v2/tasks/{id}/claim
```

Worker 尝试对状态为 `ready` 的任务进行原子认领。成功后任务状态转为 `running`。

**请求体：**
```json
{
  "worker_id": "worker-1",
  "pid": 12345
}
```

**响应：** `200 OK`
```json
{
  "task_id": "t_a1b2c3d4",
  "claim_lock": "worker-1:12345",
  "status": "running",
  "workspace_path": "/tmp/kanban-ws/t_a1b2c3d4"
}
```

**错误码：** 409 — 任务已被其他 Worker 认领

### 完成任务

```
POST /v2/tasks/{id}/complete
```

**请求体：**
```json
{
  "worker_id": "worker-1",
  "outcome": "completed",
  "summary": "登录模块开发完成",
  "result": {"files": ["login.py", "test_login.py"], "coverage": 0.95}
}
```

**响应：** `200 OK` — 任务状态转为 `completed`

### 阻塞与解除阻塞

```
POST /v2/tasks/{id}/block
POST /v2/tasks/{id}/unblock
```

**请求体（block）：**
```json
{
  "reason": "需要人工审查代码",
  "worker_id": "coder-agent"
}
```

**响应：** `200 OK`
- Block: 任务状态转为 `blocked`
- Unblock: 任务状态回到 `running` 或 `ready`

### 任务级心跳

```
POST /v2/tasks/{id}/heartbeat
```

延长任务认领锁的 TTL，防止任务因超时被释放。

**请求体：**
```json
{
  "worker_id": "worker-1"
}
```

### 评论

```
POST /v2/tasks/{id}/comment
```

**请求体：**
```json
{
  "author": "reviewer",
  "body": "请添加单元测试"
}
```

**响应：** `201 Created`

### 依赖管理

```
# 添加依赖
POST /v2/tasks/{id}/depend
{
  "parent_id": "t_parent1"
}

# 移除依赖
DELETE /v2/tasks/{id}/depend/{parent_id}
```

**响应：** `200 OK`

**错误码：** 400 — `cycle_detected`（添加时检测到循环依赖）

### 归档任务

```
DELETE /v2/tasks/{id}
```

将任务转为终点状态 `archived`，不再参与调度。

**响应：** `200 OK`

### 编排引擎统计

```
GET /v2/stats
```

**响应：** `200 OK`
```json
{
  "total": 42,
  "todo": 3,
  "ready": 5,
  "running": 2,
  "blocked": 1,
  "completed": 28,
  "failed": 2,
  "archived": 1
}
```

---

## Admin 管理端点

> 以下端点需 `registry:admin` scope（Registry 管理员权限）。

### 创建客户端

```
POST /admin/clients
Authorization: Bearer *** (need registry:admin scope)
Content-Type: application/json
```

**请求体：**
```json
{
  "description": "My Agent",
  "allowed_scopes": ["agent:register", "agent:read", "task:read", "task:write"],
  "agent_card_id": "coder-agent"
}
```

**字段说明：**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `description` | string | 否 | 客户端描述 |
| `allowed_scopes` | string[] | 否 | 允许的 scope 列表（默认全部） |
| `agent_card_id` | string | 否 | 关联的 Agent name（用于 WS auth 校验） |

**响应：** `201 Created`
```json
{
  "client_id": "client-a1b2c3d4e5f6",
  "client_secret": "***",
  "agent_card_id": "coder-agent",
  "scopes": ["task:read", "task:write"],
  "created_at": 1712345678.0
}
```

> **安全提示：** `client_secret` 仅在创建时返回一次，无法再次获取。Admin 应通过安全渠道将其分发给 Agent。

**错误码：**

| 状态码 | error | 说明 |
|--------|-------|------|
| 401 | `unauthorized` | 缺少或无效的 Bearer Token |
| 403 | `insufficient_scope` | Token 缺少 `registry:admin` scope |

### 列出客户端

```
GET /admin/clients
Authorization: Bearer *** (need registry:admin scope)
```

**响应：** `200 OK`
```json
[
  {
    "client_id": "client-a1b2c3d4e5f6",
    "description": "My Agent",
    "scopes": ["task:read", "task:write"],
    "agent_card_id": "coder-agent",
    "token_count": 3,
    "created_at": 1712345678.0
  }
]
```

> 列表响应不包含 `client_secret`（机密信息不返回）。

### 删除客户端

```
DELETE /admin/clients/{client_id}
Authorization: Bearer *** (need registry:admin scope)
```

**行为：** 删除客户端时自动吊销该客户端的所有 Token。

**响应：** `200 OK`
```json
{
  "message": "Client deleted successfully",
  "client_id": "client-a1b2c3d4e5f6"
}
```

**错误码：**

| 状态码 | error | 说明 |
|--------|-------|------|
| 404 | `client_not_found` | 客户端不存在 |
| 403 | `insufficient_scope` | Token 缺少 `registry:admin` scope |

---

## 系统端点

### 健康检查

```
GET /health
```

返回服务器健康状态和统计。

**认证：** ❌ 公开

**响应：**
```json
{
  "status": "healthy",
  "version": "1.0.0",
  "uptime_seconds": 123.45,
  "stats": {
    "total_agents": 5,
    "alive_agents": 4,
    "stale_agents": 1,
    "connected_via_ws": 1
  }
}
```

### Registry Agent Card

```
GET /.well-known/agent-card.json
```

返回 Registry 自身的 A2A Agent Card，供 Agent 发现使用。

**认证：** ❌ 公开