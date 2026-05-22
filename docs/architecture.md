# Simple A2A Registry — 架构设计

## 概述

Simple A2A Registry 是一个轻量级的 Agent 注册与发现服务，基于 Google A2A (Agent-to-Agent) 协议。Agent 通过 API 注册，依赖心跳（HTTP 或 WebSocket）保活。

除基础注册/发现功能外，Registry 还支持 **WebSocket 持久连接** 和 **任务分发**，使客户端能够通过 Registry 向 Agent 提交任务并获取结果。

## 组件

```
simple_a2a_registry/
  cli.py          — argparse CLI 入口
  server.py       — aiohttp REST API + WebSocket + 任务分发
  store.py        — 持久化注册状态（JSON 文件）+ 心跳管理
  models.py       — A2A Agent Card 数据模型（无 Pydantic 依赖）
  static/         — Web Dashboard（HTML+JS）
tests/
  test_store.py   — 存储层单元测试
  test_models.py  — 数据模型单元测试
  test_server.py  — HTTP API 集成测试（含 WS/dispatch/task）
```

## 数据流

### 经典模式：HTTP 注册 + 心跳轮询

```
         ┌───────────────┐
         │  External Agent│
         └──┬────────────┘
            │ POST /v1/agents + heartbeat every 2min
            ▼
    ┌───────────────┐         ┌──────────────┐
    │  A2A Registry  │────────▶ registry.json │
    │  (aiohttp)     │         └──────────────┘
    └────────────────┘
```

### 新增模式：WebSocket Hub-and-Spoke + 任务分发

```
         ┌─────────────┐
         │   Client    │
         │  (调用方)    │
         └──────┬──────┘
                │ POST /v1/agents/{id}/dispatch
                ▼
    ┌───────────────────────────────────┐
    │          A2A Registry             │
    │                                   │
    │  ┌──────────────────────────┐     │
    │  │  Task Store (内存)        │     │
    │  │  task_id → {state,result}│     │
    │  └──────────────────────────┘     │
    │                                   │
    │  ┌──────────────────────────┐     │
    │  │  WS Hub (连接管理器)      │     │
    │  │  agent_id ↦ WebSocket    │     │
    │  └──────────────────────────┘     │
    └──────┬────────────────────┬───────┘
           │ WS: {"type":"task"}│        │ Client 轮询
           ▼                    │ GET /v1/tasks/{id}
    ┌──────────────┐            ▼
    │  Agent A     │    ┌──────────────┐
    │ (WS 长连接)   │    │  Client      │
    │              │    │ (轮询结果)    │
    └──────────────┘    └──────────────┘

注意：Agent 也可主动通过 WS 推送结果
      (task_result / task_progress)
```

### 完整工作流：客户端 → Registry → Agent

```
Client                  Registry                Agent (WS 连接)
  │                        │                        │
  │  POST /dispatch        │                        │
  │  ─────────────────────→│                        │
  │  202 {task_id}         │                        │
  │  ←─────────────────────│                        │
  │                        │  WS: {"type":"task"}   │
  │                        │  ─────────────────────→│
  │                        │                        │  处理中
  │                        │  WS: {"type":"task_    │
  │                        │       progress"}       │
  │                        │  ←─────────────────────│
  │                        │                        │  ...处理完成
  │                        │  WS: {"type":"task_    │
  │                        │       result"}          │
  │                        │  ←─────────────────────│
  │  GET /v1/tasks/{id}    │                        │
  │  ─────────────────────→│                        │
  │  200 {state, result}   │                        │
  │  ←─────────────────────│                        │
```

## 关键组件说明

### server.py — RegistryHandler

| 属性/方法 | 说明 |
|-----------|------|
| `_ws_connections` | `Dict[str, WebSocketResponse]` — 管理所有 Agent 的 WS 连接 |
| `_tasks` | `Dict[str, Dict]` — 内存中的任务存储（task_id → 任务状态/结果） |
| `handle_ws()` | WebSocket 端点处理器，支持 ping/pong、task_result、task_progress、close |
| `handle_dispatch()` | 任务分发入口，通过 WS 向 Agent 推送任务 |
| `handle_get_task()` | 任务状态查询 |
| `_cleanup_task()` | 后台任务：每 60 秒清理过期 Agent 和断开的 WS 连接 |

### store.py — A2ARegistryStore

| 机制 | 数值 | 说明 |
|------|------|------|
| `HEARTBEAT_TIMEOUT` | 120 秒 | 超过此时间无心跳的 Agent 标记为 stale |
| `HEARTBEAT_PURGE` | 300 秒 | 超过此时间的 stale Agent 被彻底清除 |
| 持久化 | JSON 文件 | 原子写入（tmp + replace），保存在 `~/.simple-a2a-registry/registry.json` |

### Task Store（内存任务存储）

- 位于 `RegistryHandler._tasks`，纯内存结构
- 任务通过 `handle_dispatch()` 创建，状态流转：`dispatched → forwarded → working → completed/failed`
- Agent 也可以通过 WS 主动推送 `task_result`/`task_progress` 消息来创建或更新任务
- **当前无自动过期机制** — 长时间运行需注意内存增长

### WebSocket Hub

- Hub-and-Spoke 拓扑：Registry 作为中心 Hub，每个 Agent 作为 Spoke 建立一条 WS 连接
- 同一 Agent 的第二个 WS 连接会替换第一个（发送 "replaced" 关闭旧连接）
- 后台清理任务定期清除已关闭的 WS 连接

## 设计决策

| 决策 | 选择 | 原因 |
|------|------|------|
| 数据模型 | Pydantic-free dataclass | 零依赖，仅需 `aiohttp` |
| 持久化 | JSON 原子写入 | 简单可靠，适合单实例场景 |
| 心跳模型 | 120s 超时 / 300s 清理 | 平衡网络波动容忍度和资源回收时效 |
| 任务存储 | 内存 | 适合轻量级、临时性任务追踪 |
| 认证 | 无 | 面向本地/可信网络设计 |
| 任务分发 | WebSocket 推送 | 低延迟、双向通信，优于 HTTP 轮询 |