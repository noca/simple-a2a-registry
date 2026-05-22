# Simple A2A Registry

轻量级、符合 Google A2A (Agent-to-Agent) 协议的 Agent 注册中心。
支持 Agent 注册/发现、心跳保活、WebSocket 长连接、任务分发与状态查询。

## 架构概览

```
┌─────────────┐      HTTP/WS       ┌─────────────────────┐
│   Client    │ ──────────────────→ │  A2A Registry       │
│ (调用方)     │                     │  (localhost:8321)   │
└─────────────┘                     │                     │
                                    │  ┌───────────────┐  │
                                    │  │ RegistryStore │  │
                                    │  │ (持久化存储)    │  │
                                    │  └───────────────┘  │
                                    └────────┬────────────┘
                                             │
                      ┌──────────────────────┼──────────────────────┐
                      │                      │                      │
              ┌───────▼───────┐    ┌─────────▼────────┐  ┌─────────▼────────┐
              │  Agent A      │    │   Agent B        │  │   Agent C        │
              │ (HTTP+WS)     │    │  (WS 长连接)      │  │  (HTTP 心跳)     │
              └───────────────┘    └──────────────────┘  └──────────────────┘
```

**核心工作流：**
1. Agent 通过 `POST /v1/agents` 向 Registry 注册
2. Agent 通过 WebSocket (`/v1/agents/{id}/ws`) 建立长连接，或通过 HTTP 心跳保持活跃
3. 客户端通过 `POST /v1/agents/{id}/dispatch` 向已连接的 Agent 分发任务
4. Agent 通过 WebSocket 接收任务、处理、返回结果
5. 客户端通过 `GET /v1/tasks/{id}` 轮询任务状态和结果

> 详细架构说明请参见 [docs/architecture.md](docs/architecture.md)。

## 快速开始

```bash
pip install simple-a2a-registry
a2a-registry
```

打开 http://localhost:8321 查看 Dashboard。

## 命令行

```bash
a2a-registry --host 0.0.0.0 --port 8321 --data-dir ~/.simple-a2a-registry
```

## API 参考

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

### WebSocket 长连接（新增）

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

### 任务分发（新增）

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/v1/agents/{id}/dispatch` | 向 Agent 分发任务，要求 Agent 已通过 WebSocket 连接 |
| GET | `/v1/tasks/{id}` | 查询任务状态和结果 |

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

## 文档

更多详细文档请参见 `docs/` 目录：

| 文档 | 说明 |
|------|------|
| [docs/API.md](docs/API.md) | 完整的 API 参考（含 WS、任务分发、任务状态） |
| [docs/architecture.md](docs/architecture.md) | 架构设计、组件说明、数据流图 |

## Dashboard

打开 http://localhost:8321 查看 Web Dashboard：
- Agent 列表（状态、标签、技能）
- Agent 详情展开面板
- WebSocket 连接标识（WS 徽章）
- 实时刷新（每 15 秒）

## License

MIT