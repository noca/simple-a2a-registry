# Simple A2A Registry — API 参考

## 基础 URL

默认：`http://localhost:8321`

---

## 端点一览

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
| **GET** | **`/v1/tasks/{task_id}`** | **查询任务状态和结果** |
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

## WebSocket 长连接（新增）

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

## 任务分发（新增）

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

## 任务状态查询（新增）

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