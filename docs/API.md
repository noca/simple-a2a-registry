# Simple A2A Registry — API 参考

## 基础 URL

默认：`http://localhost:8321`

---

## V1 端点一览

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/health` | 健康检查 |
| GET | `/.well-known/agent-card.json` | Registry 自身的 A2A Agent Card |
| GET | `/v1/agents` | 列出/搜索 Agent |
| GET | `/v1/agents/{agent_id}` | 获取 Agent 详情 |
| POST | `/v1/agents` | 注册 Agent |
| DELETE | `/v1/agents/{agent_id}` | 注销 Agent |
| POST | `/v1/agents/{agent_id}/heartbeat` | Agent 心跳保活 |
| **GET** | **`/v1/agents/{agent_id}/ws`** | **Agent WebSocket 长连接** |
| **POST** | **`/v1/agents/{agent_id}/dispatch`** | **通过 WS 分发任务到 Agent** |
| **GET** | **`/v1/tasks`** | **查询 V1 任务列表** |
| **GET** | **`/v1/tasks/{task_id}`** | **查询 V1 任务状态和结果** |
| ~~POST~~ | ~~`/v1/agents/{agent_id}/task`~~ | ~~代理任务到 Agent URL（已废弃）~~ |

---

## 健康检查

```
GET /health
```

返回服务器健康状态和统计。

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

**字段说明：**

---

## Well-Known Agent Card

```
GET /.well-known/agent-card.json
```

返回 Registry 自身的 A2A Agent Card，供发现使用。

---

## Agent 管理

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

**响应中的附加字段：** 如果 Agent 当前通过 WebSocket 连接，会在返回的卡片中附加 `"connection": "websocket"` 和 `"status": "alive"`。

### 获取 Agent

```
GET /v1/agents/{agent_id}
```

返回 Agent Card。如果该 Agent 通过 WebSocket 连接，会附加 `connection` 和 `status` 字段。

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

**错误码：**
| 状态码 | error | 说明 |
|--------|-------|------|
| 400 | `invalid_json` | 请求体不是合法 JSON |
| 400 | `validation_error` | 缺少或空 name 字段 |
| 409 | `agent_exists` | 同名 Agent 已存在 |

### 注销 Agent

```
DELETE /v1/agents/{agent_id}
```

注销 Agent。如果该 Agent 有 WebSocket 连接，会自动关闭连接。

**响应：** `200 OK`

```json
{
  "message": "Agent unregistered successfully",
  "id": "agent-uuid"
}
```

**错误码：**
| 状态码 | error | 说明 |
|--------|-------|------|
| 404 | `agent_not_found` | Agent 不存在 |

---

## 心跳保活

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

**错误码：**
| 状态码 | error | 说明 |
|--------|-------|------|
| 404 | `agent_not_found` | Agent 不存在 |
| 410 | `agent_stale` | Agent 已过期，无法发送心跳 |

---

## WebSocket 长连接

```
GET /v1/agents/{agent_id}/ws
```

Agent 通过 WebSocket 与 Registry 建立持久连接。相比于 HTTP 心跳轮询，WebSocket 使 Registry 能够主动向 Agent 推送任务。

**连接前提：** Agent 必须先通过 `POST /v1/agents` 注册。

**行为：**
- 同一 Agent 的第二个 WS 连接会替换第一个（旧连接收到 `{"type":"close","reason":"replaced"}` 后关闭）
- 连接建立时自动更新 Agent 的心跳时间
- 连接断开时自动从活跃连接列表中移除

### WebSocket 消息协议

#### Registry → Agent（服务端推送）

| type | 方向 | 说明 |
|------|------|------|
| `task` | Registry → Agent | 分发任务：`{"type":"task","id":"uuid","query":"...","sessionId":"..."}` |
| `close` | Registry → Agent | 连接被替换/关闭通知：`{"type":"close","reason":"replaced"}` |

#### Agent → Registry（客户端上报）

| type | 方向 | 说明 |
|------|------|------|
| `ping` | Agent → Registry | Registry 回复 `{"type":"pong"}` |
| `task_result` | Agent → Registry | 报告任务完成：`{"type":"task_result","id":"...","status":"completed","result":{...}}` |
| `task_progress` | Agent → Registry | 报告任务进度：`{"type":"task_progress","id":"...","status":"working"}` |
| `close` | Agent → Registry | Agent 主动关闭连接 |

**注意：** 即使任务不是通过 Registry dispatch 发起的，Agent 也可以主动通过 `task_result` / `task_progress` 消息汇报结果。Registry 会自动创建对应的任务记录（auto-created）。

---

## 任务分发

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

**响应：** `202 Accepted`

```json
{
  "task_id": "uuid",
  "agent_id": "agent-uuid",
  "state": "forwarded",
  "query": "写一个 Python 排序函数",
  "created_at": 1700000000.0
}
```

**错误码：**
| 状态码 | error | 说明 |
|--------|-------|------|
| 400 | `invalid_json` | 请求体不是合法 JSON |
| 400 | `validation_error` | 缺少 `query` 字段 |
| 404 | `agent_not_found` | Agent 不存在 |
| 503 | `agent_not_connected` | Agent 未通过 WebSocket 连接 |
| 502 | `dispatch_failed` | 通过 WebSocket 向 Agent 转发任务失败 |

---

## 任务状态查询

```
GET /v1/tasks/{task_id}
```

查询任务的当前状态和结果。

**任务状态流转：**
```
dispatched → forwarded → working → completed / failed
```

**响应：** `200 OK`

```json
{
  "id": "uuid",
  "agent_id": "agent-uuid",
  "query": "写一个 Python 排序函数",
  "session_id": "",
  "state": "completed",
  "result": { "text": "print('hello world')" },
  "error": null,
  "created_at": 1700000000.0,
  "updated_at": 1700000010.0,
  "dispatched_at": 1700000000.1
}
```

**字段说明：**
| 字段 | 类型 | 说明 |
|------|------|------|
| `state` | string | 任务状态：`dispatched` / `forwarded` / `working` / `completed` / `failed` |
| `result` | object/null | 任务完成时的结果数据 |
| `error` | string/null | 任务失败时的错误信息 |

**错误码：**
| 状态码 | error | 说明 |
|--------|-------|------|
| 404 | `task_not_found` | 任务 ID 不存在 |

---

## ~~代理任务~~（已废弃）

```
POST /v1/agents/{agent_id}/task  （已废弃，请使用 /dispatch 替代）
```

此端点仅返回 Agent 的目标 URL 信息，并未实现真正的任务代理转发。
推荐使用 `/v1/agents/{agent_id}/dispatch`（基于 WebSocket 的任务分发）。

---

# V2 编排引擎 — API

V2 提供 **Orchestration Engine（编排引擎）**，支持 Kanban 级任务生命周期管理。所有 V2 端点统一以 `/v2/` 为前缀。

## V2 端点一览

| 方法 | 路径 | 说明 | 状态码 |
|------|------|------|--------|
| POST | `/v2/tasks` | 创建任务 | 201 |
| GET | `/v2/tasks` | 列表查询 | 200 |
| GET | `/v2/tasks/{id}` | 任务详情（含依赖链、评论、运行历史、事件流） | 200 |
| POST | `/v2/tasks/{id}/claim` | Worker 认领任务 | 200 |
| POST | `/v2/tasks/{id}/complete` | 完成任务 | 200 |
| POST | `/v2/tasks/{id}/block` | 阻塞任务（HITL） | 200 |
| POST | `/v2/tasks/{id}/unblock` | 解除阻塞 | 200 |
| POST | `/v2/tasks/{id}/heartbeat` | 任务级心跳（延长认领锁 TTL） | 200 |
| POST | `/v2/tasks/{id}/comment` | 添加评论 | 201 |
| DELETE | `/v2/tasks/{id}` | 归档任务 | 200 |
| POST | `/v2/tasks/{id}/depend` | 添加依赖关系（双向） | 200 |
| DELETE | `/v2/tasks/{id}/depend/{parent_id}` | 移除依赖关系 | 200 |
| GET | `/v2/stats` | 编排引擎统计 | 200 |

---

## POST /v2/tasks — 创建任务

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
| `priority` | int | 否 | 优先级（数字越大越高，默认 0） |
| `parents` | string[] | 否 | 父任务 ID 列表 |
| `workspace_kind` | string | 否 | 工作区模式：`scratch`/`dir`/`worktree`（默认 `scratch`） |
| `workspace_path` | string | 否 | 共享工作区路径（`dir` 模式使用） |
| `max_runtime_seconds` | int | 否 | 单次运行超时上限（秒） |
| `max_retries` | int | 否 | 最大重试次数 |
| `tenant` | string | 否 | 租户标签（命名空间隔离） |
| `created_by` | string | 否 | 创建者标识 |

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

---

## GET /v2/tasks — 列表查询

```
GET /v2/tasks?status=ready,running&assignee=coder-agent&q=登录&limit=20&offset=0
```

**查询参数：**

| 参数 | 类型 | 说明 |
|------|------|------|
| `status` | string | 过滤状态（可多选，逗号分隔） |
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
      "started_at": 1712345700,
      "completed_at": null,
      "tenant": null
    }
  ]
}
```

---

## GET /v2/tasks/{id} — 任务详情

**路径参数：** `id` — 任务 ID

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
    "completed_at": null,
    "workspace_kind": "scratch",
    "workspace_path": "/tmp/kanban-ws/t_a1b2c3d4",
    "claim_lock": "worker-1:12345",
    "claim_expires": 1712346600,
    "max_runtime_seconds": 600,
    "max_retries": 3,
    "consecutive_failures": 0,
    "current_run_id": 5,
    "result": null
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
      "last_heartbeat_at": 1712345760,
      "outcome": null
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
      "run_id": 5,
      "created_at": 1712345700,
      "payload": {"profile": "coder-agent", "pid": 12345}
    }
  ]
}
```

**错误码：** 404 — `task_not_found`

---

## POST /v2/tasks/{id}/claim — 认领任务

Worker 尝试对状态为 `ready` 的任务进行原子认领。成功后任务状态转为 `running`，并分配 Workspace。

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
  "claim_expires": 1712346600,
  "workspace_path": "/tmp/kanban-ws/t_a1b2c3d4"
}
```

**错误码：**

| 状态码 | error | 说明 |
|--------|-------|------|
| 409 | `claim_conflict` | 任务已被其他 Worker 认领或状态不是 `ready` |
| 404 | `task_not_found` | 任务不存在 |

---

## POST /v2/tasks/{id}/complete — 完成任务

仅当当前 `claim_lock` 匹配时才允许完成，防止误操作。

**请求体：**

```json
{
  "claim_lock": "worker-1:12345",
  "result": {"deploy_url": "http://..."},
  "summary": "登录模块实现完成，包含 JWT 认证",
  "metadata": {"lines_of_code": 350, "tests_passed": 12}
}
```

**字段说明：**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `claim_lock` | string | 否 | 认领锁，用于验证身份 |
| `result` | object | 否 | 任务结果数据 |
| `summary` | string | 否 | 人类可读的结果摘要（1-3 句话） |
| `metadata` | object | 否 | 结构化结果数据 |

**响应：** `200 OK`

```json
{
  "task_id": "t_a1b2c3d4",
  "status": "completed"
}
```

**错误码：**

| 状态码 | error | 说明 |
|--------|-------|------|
| 403 | `claim_mismatch` | 认领锁不匹配 |
| 400 | `invalid_status` | 当前状态不允许完成 |
| 404 | `task_not_found` | 任务不存在 |

---

## POST /v2/tasks/{id}/block — 阻塞任务（HITL）

将运行中的任务转为 `blocked` 状态，等待人工介入。只有任务当前的持有者可以调用。

**请求体：**

```json
{
  "claim_lock": "worker-1:12345",
  "reason": "需要人工审核代码质量"
}
```

**响应：** `200 OK`

```json
{
  "task_id": "t_a1b2c3d4",
  "status": "blocked",
  "block_reason": "需要人工审核代码质量"
}
```

**错误码：**

| 状态码 | error | 说明 |
|--------|-------|------|
| 403 | `claim_mismatch` | 认领锁不匹配 |
| 400 | `invalid_status` | 当前状态不允许阻塞 |

---

## POST /v2/tasks/{id}/unblock — 解除阻塞

将 `blocked` 任务转回 `running`，允许 Worker 继续执行。

**请求体：**

```json
{
  "reason": "审核通过"
}
```

**响应：** `200 OK`

```json
{
  "task_id": "t_a1b2c3d4",
  "status": "running"
}
```

**错误码：**

| 状态码 | error | 说明 |
|--------|-------|------|
| 400 | `invalid_status` | 当前状态不是 `blocked` |
| 404 | `task_not_found` | 任务不存在 |

---

## POST /v2/tasks/{id}/heartbeat — 任务心跳

延长认领锁的 TTL，更新 `last_heartbeat_at`。Long-running 任务应周期性调用（建议 1-5 分钟）。如果超过 `claim_expires` 未心跳，Dispatcher 会将该任务释放为 `failed`。

**请求体：**

```json
{
  "claim_lock": "worker-1:12345",
  "progress": "已完成 70%，正在处理边界情况"
}
```

**响应：** `200 OK`

```json
{
  "task_id": "t_a1b2c3d4",
  "claim_expires": 1712347200
}
```

**错误码：**

| 状态码 | error | 说明 |
|--------|-------|------|
| 403 | `claim_mismatch` | 认领锁不匹配 |
| 404 | `task_not_found` | 任务不存在 |

---

## POST /v2/tasks/{id}/comment — 添加评论

**请求体：**

```json
{
  "author": "reviewer",
  "body": "代码审查通过，但需要补充文档"
}
```

**响应：** `201 Created`

```json
{
  "comment_id": 6,
  "created_at": 1712346000
}
```

**错误码：**

| 状态码 | error | 说明 |
|--------|-------|------|
| 400 | `validation_error` | 缺少 `body` 字段 |
| 404 | `task_not_found` | 任务不存在 |

---

## DELETE /v2/tasks/{id} — 归档任务

只有状态为 `completed`、`failed` 或 `cancelled` 的任务可以被归档。归档后状态变为 `archived`，同时清理 Workspace（scratch 模式删除目录，worktree 模式执行 `git worktree remove`）。

**响应：** `200 OK`

```json
{
  "task_id": "t_a1b2c3d4",
  "status": "archived"
}
```

**错误码：**

| 状态码 | error | 说明 |
|--------|-------|------|
| 400 | `invalid_status` | 只有已完成/失败/取消的任务可归档 |
| 404 | `task_not_found` | 任务不存在 |

---

## POST /v2/tasks/{id}/depend — 添加依赖

为当前任务添加一个父依赖。如果父任务全部完成，子任务自动提升为 `ready`。

**请求体：**

```json
{
  "parent_id": "t_parent1"
}
```

**响应：** `200 OK`

```json
{
  "task_id": "t_a1b2c3d4",
  "parent_id": "t_parent1",
  "status": "dependency_added"
}
```

**错误码：**

| 状态码 | error | 说明 |
|--------|-------|------|
| 400 | `cycle_detected` | 将导致循环依赖 |
| 400 | `parent_not_found` | 指定的父任务不存在 |
| 404 | `task_not_found` | 子任务不存在 |

---

## DELETE /v2/tasks/{id}/depend/{parent_id} — 移除依赖

移除当前任务的一个父依赖。如果移除后仍有其他未完成的父任务，子任务保持 `todo` 状态。

**路径参数：** `id` — 子任务 ID，`parent_id` — 父任务 ID

**响应：** `200 OK`

```json
{
  "task_id": "t_a1b2c3d4",
  "parent_id": "t_parent1",
  "status": "dependency_removed"
}
```

**错误码：**

| 状态码 | error | 说明 |
|--------|-------|------|
| 404 | `dependency_not_found` | 指定的依赖关系不存在 |

---

## GET /v2/stats — 编排引擎统计

返回 V2 编排引擎的总体统计信息。

**响应：** `200 OK`

```json
{
  "total": 50,
  "by_status": {
    "todo": 10,
    "ready": 5,
    "running": 3,
    "blocked": 1,
    "completed": 25,
    "failed": 4,
    "archived": 2
  }
}
```

---

## 错误响应的统一格式

所有 V2 端点使用统一的 JSON 错误格式：

```json
{
  "error": "error_code",
  "detail": "Human-readable error description"
}
```

---

## V2 状态码速查

| 状态码 | 含义 |
|--------|------|
| 200 | 操作成功 |
| 201 | 创建成功 |
| 400 | 请求参数错误（`validation_error`、`cycle_detected`、`parent_not_found`、`invalid_status`） |
| 403 | 权限不足（`claim_mismatch`） |
| 404 | 资源不存在（`task_not_found`、`dependency_not_found`） |
| 409 | 冲突（`claim_conflict`） |
