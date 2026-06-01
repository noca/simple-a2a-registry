# Simple A2A Registry — 架构设计

> 单一系统，三层能力：Agent 发现 + 任务分发 + 编排引擎

## 概述

Simple A2A Registry 是一个轻量级的 Agent 注册与编排服务，基于 Google A2A (Agent-to-Agent) 协议。提供 Agent 注册/发现、心跳保活、WebSocket 长连接、任务分发，以及完整的 Kanban 级任务编排能力。

三层架构：

| 层面 | 能力 | 说明 |
|------|------|------|
| Agent 发现层 | 注册/发现/心跳/WS 连接 | Agent 通过 API 注册，HTTP 心跳或 WS 保活，按技能/标签搜索 |
| 任务分发层 | 消息分发/结果回传 | 客户端通过 Registry 向 WS 连接的 Agent 分发任务，轮询结果 |
| 编排引擎层 | 任务管理/Worker 调度/依赖链 | 8 状态状态机、DAG 依赖链、自动 Worker 派发、HITL |

---

## 整体架构

```
┌──────────────────────────────────────────────────────────────┐
│                     HTTP / WebSocket Layer                     │
│                     (aiohttp server)                          │
├──────────────────────────────────────────────────────────────┤
│                                                               │
│  ┌──────────────────────┐  ┌────────────┐  ┌───────────────┐ │
│  │   Agent Discovery    │  │   WS Hub   │  │ Orchestration │ │
│  │   · 注册/注销        │  │  · 长连接   │  │ Engine        │ │
│  │   · 心跳保活         │  │  · 消息路由  │  │ · 任务状态机   │ │
│  │   · 搜索/发现        │  │  · 连接管理  │  │ · 依赖链解析   │ │
│  └────────┬─────────────┘  └─────┬──────┘  │ · Worker 派发  │ │
│           │                      │         │ · Workspace    │ │
│           │                      │         │ · HITL         │ │
│           │                      │         │ · 审计日志     │ │
│           │                      │         └───────┬───────┘ │
│  ┌────────┴──────────────────────┴─────────────────┴────────┐ │
│  │             Store Layer (Dual Database Engine)             │ │
│  │   SQLite (WAL) ←→ MySQL (QueuePool) — 运行时切换          │ │
│  │   RetryEngine: 指数退避重试, Alembic 迁移管理              │ │
│  │   Store 类: agents / oauth_clients / oauth_tokens /       │ │
│  │   auth_codes / audit_log (WAL 模式, 线程安全 RLock)        │ │
│  │   启动时自动从旧 registry.json / auth.json 迁移            │ │
│  └───────────────────────────────────────────────────────────┘ │
│                                                               │
│  ┌────────────────────────────────────────────────────────┐  │
│  │  Plugin System                                          │  │
│  │  · 生命周期钩子: load/init/before_shutdown              │  │
│  │  · 请求钩子: before_request/after_request               │  │
│  │  · 事件钩子: on_agent_register/task_created 等          │  │
│  │  · 加载方式: entry_points / config.yaml 声明            │  │
│  └────────────────────────────────────────────────────────┘  │
│                                                               │
│  ┌────────────────────────────────────────────────────────┐  │
│  │  Rate Limiting · Audit · Multi-Tenancy                  │  │
│  │  Token Bucket (memory/MySQL) · Append-only Audit        │  │
│  │  X-Tenant-ID Header · Tenant 数据隔离                    │  │
│  └────────────────────────────────────────────────────────┘  │
│                                                               │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │               Auth & Governance                           │  │
│  │   OAuth 2.1 中间件 · JWT(RS256/HS256) · Scope 鉴权       │  │
│  │   Admin 客户端管理 · 公开端点豁免                         │  │
│  └──────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────┘
```

---

## 模块划分

```
simple_a2a_registry/
  cli.py          — argparse CLI 入口（含全部参数）
  server.py       — aiohttp REST API + WebSocket + 认证中间件 + 编排路由
  store.py        — Agent + OAuth 持久化（Store 类: Agent/心跳 + OAuth 客户端/Token）
  models.py       — A2A Agent Card 数据模型（无 Pydantic 依赖）
  auth.py         — OAuth 2.1: JWT 签发/校验、中间件、Admin 客户端 CRUD
  config.py       — YAML 配置加载器（CLI > ENV > YAML 三优先级）
  errors.py       — 统一错误响应格式 + 异常类层次
  log.py          — 结构化日志（JSON/Text 双模式 + request_id contextvars）
  metrics.py      — Prometheus 指标中间件 + /metrics 端点
  rate_limiter.py — Token Bucket 限流器（memory/MySQL 双后端）
  audit.py        — Append-only 审计事件存储
  users.py        — 用户注册与会话管理（Web Dashboard 认证）
  validation.py   — 输入校验辅助函数
  plugin.py       — Plugin ABC + PluginRegistry（钩子加载/派发）
  client.py       — A2A Python SDK（sync + async）
  ws_admin.py     — Admin WebSocket Hub（实时任务更新推送）
  database/
    engine.py     — DatabaseEngine ABC + SQLiteEngine + MySQLEngine + RetryEngine
  orchestration/  — 编排引擎模块
    task_store.py   — SQLite/MySQL 任务存储（5 张表）
    dispatcher.py   — 后台 Worker 派发器
    state_machine.py— 8 状态状态机
    routes.py       — 编排 API 路由
    swarm.py        — Swarm 拓扑 + 黑板
    swarm_routes.py — Swarm REST API 路由
    workspace.py    — 工作区管理器
    dependency.py   — 循环依赖检测 + DAG 解析
    pool.py         — Worker 进程池
  static/         — Web Dashboard（HTML+JS）
examples/
  a2a_coder_agent.py — A2A 兼容 Coder Agent（OAuth + WS 长连接 + A2A JSONRPC）
  a2a_opencode_agent.py — OpenCode Agent 变体
  sdk_usage.py        — SDK 功能演示
  simple_a2a_agent.py — 最小 Agent 示例
  run_agent.py        — 快速启动脚本
tests/
  test_store.py   — 存储层单元测试
  test_models.py  — 数据模型单元测试
  test_server.py  — HTTP API 集成测试
  test_auth.py    — OAuth 2.1 流程测试
  test_orchestration_api.py  — V2 REST API 测试
  test_orchestration_store.py — TaskStore CRUD 测试
  test_orchestration_state_machine.py — 状态转换测试
  test_orchestration_e2e.py   — 端到端编排测试
  test_swarm.py   — Swarm 拓扑测试
  test_dispatcher.py  — Dispatcher 测试
  test_rate_limiter.py— Token Bucket 测试
  test_validation.py  — 输入校验测试
  test_errors.py      — 错误处理测试
  test_log.py         — 日志测试
  test_config.py      — 配置加载测试
  test_cors.py        — CORS 中间件测试
  test_concurrency.py — 线程安全测试
  test_users.py       — 用户管理测试
  test_tenant_isolation.py  — 多租户隔离测试
  test_tenant_e2e.py        — 租户端到端测试
  test_token_scope_tenant.py— Token+Scope+租户组合测试
  test_token_tenant_bc.py   — 向后兼容测试
  test_user_auth_e2e.py     — 用户认证端到端测试
  test_websocket.py   — WebSocket 协议测试
  test_workspace.py   — 工作区管理测试
  test_metrics.py     — Prometheus 指标（在 test_server.py 中）
  test_mysql_compat.py— MySQL 方言兼容测试
  test_tls.py         — TLS/SSL 测试
  test_bootstrap_admin.py   — 启动管理客户端测试
  test_performance_benchmark.py — 性能基准测试
  benchmarks/         — 基准测试套件
```
```

---

## 数据流

### Agent 发现流程（HTTP 心跳）

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

### 任务分发流程（WebSocket Hub-and-Spoke）

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
           ▼                    │ GET /tasks/{id}
    ┌──────────────┐            ▼
    │  Agent A     │    ┌──────────────┐
    │ (WS 长连接)   │    │  Client      │
    │              │    │ (轮询结果)    │
    └──────────────┘    └──────────────┘
```

### 编排引擎任务流

```
  Client / Assigner              Dispatcher                  Worker (Agent)
       │                            │                            │
       │ POST /v2/tasks             │                            │
       │ (创建任务, 含 parent,      │                            │
       │  assignee)                 │                            │
       ▼                            │                            │
  ┌──────────┐                      │                            │
  │ status=  │                      │                            │
  │ todo     │                      │                            │
  └────┬─────┘                      │                            │
       │                            │                            │
       │ (如果有 parent,            │                            │
       │  等待所有 parent 完成)      │                            │
       ▼                            │                            │
  ┌──────────┐                      │                            │
  │ status=  │ ──── 轮询 ─────────▶ │ ─── Atomic Claim ────────▶│
  │ ready    │                      │    (UPDATE ... WHERE       │
  └──────────┘                      │     status=ready)          │
                                    │                            │
                                    │    ┌──────────────────┐   │
                                    │    │ status=running    │ │
                                    │    │ workspace=alloc   │   │
                                    │    │ spawn worker proc │   │
                                    │    └──────────────────┘   │
                                    │                            │
                                    │ ◄── Heartbeat ─────────── │
                                    │ ◄── Complete / Block ──── │
       ◄──── 事件 / Run ────────────                             │
```

---

## Agent 注册与发现

### Agent Card v1.0 数据模型

Agent Card 数据结构对齐 A2A v1.0 protobuf 规范（[a2a.proto](https://github.com/a2aproject/A2A/blob/main/specification/a2a.proto)）。

**核心类型：**

- `AgentCard` — name, description, supported_interfaces, provider, version, capabilities, skills, security_schemes, security_requirements, default_input_modes, default_output_modes, etc.
- `AgentSkill` — id, name, description, tags, examples, input_modes, output_modes, security_requirements
- `AgentCapabilities` — streaming, push_notifications, extensions
- `SecurityScheme` — apiKey / http / oauth2 / openIdConnect / mutualTls
- `OAuthFlows` — authorization_code + PKCE, client_credentials, device_code

**(注：完整字段定义参见 `models.py` 和 [oauth-design.md](oauth-design.md)）**

### 心跳保活

| 机制 | 数值 | 说明 |
|------|------|------|
| `HEARTBEAT_TIMEOUT` | 120 秒 | 超过此时间无心跳的 Agent 标记为 stale |
| `HEARTBEAT_PURGE` | 300 秒 | 超过此时间的 stale Agent 被彻底清除 |
| 持久化 | SQLite (registry.db) | agents 表，`BEGIN IMMEDIATE` 事务保证并发安全 |

### WebSocket Hub

- Hub-and-Spoke 拓扑：Registry 作为中心 Hub，每个 Agent 一条 WS 连接
- 同一 Agent 的第二条 WS 连接会替换第一条（发送 `replaced` 关闭旧连接）
- 后台清理任务定期清除已关闭的连接

### WS 消息协议

**Registry → Agent（推送）：**

| type | 说明 |
|------|------|
| `task` | 分发任务：`{"type":"task","id":"uuid","query":"...","sessionId":"..."}` |
| `close` | 连接被替换/关闭通知：`{"type":"close","reason":"replaced"}` |

**Agent → Registry（上报）：**

| type | 说明 |
|------|------|
| `ping` | 保活心跳，Registry 回复 `pong` |
| `task_result` | 任务完成：`{"type":"task_result","id":"...","status":"completed","result":{...}}` |
| `task_progress` | 进度上报：`{"type":"task_progress","id":"...","status":"working"}` |
| `close` | 主动关闭连接 |

> Agent 也可主动通过 `task_result` / `task_progress` 汇报任务结果，Registry 自动创建对应的任务记录。

---

## 编排引擎（Orchestration Engine）

### 设计目标

| 目标 | 说明 |
|------|------|
| 任务生命周期管理 | 从创建到归档的完整状态机，依赖链、重试、超时释放 |
| Worker 自动派发 | 基于 Profile 的原子化任务认领与派发，防止重复执行 |
| 多 Agent 协调 | 通过依赖链和 Workspace 隔离，实现多 Agent 分阶段协作 |
| 人机协同 | Block/Unblock 机制、评论线程，Human-in-the-Loop |
| 可观测性 | 全事件审计日志、任务运行记录、结构化元数据 |

### 非功能设计

| 维度 | 要求 |
|------|------|
| 持久性 | SQLite WAL 模式 + `BEGIN IMMEDIATE` 防并发写冲突 |
| 可用性 | Dispatcher 轮询周期可配置，从重启中自动恢复未完成的任务 |
| 安全性 | Claim Lock 机制防双重派发；Workspace 隔离 |
| 兼容性 | 不修改 Agent 发现/WS Hub 模块 |

### 编排引擎内部架构

```
┌─────────────────────────────────────────────────────────────┐
│                  Orchestration Engine                        │
│                                                              │
│  ┌──────────┐  ┌──────────────┐  ┌────────────────────────┐ │
│  │ Task     │  │ Dependency   │  │ Dispatcher              │ │
│  │ State    │◄─│ Resolution   │  │ · Poll Ready Tasks      │ │
│  │ Machine  │  │ · Parent/Child│  │ · Atomic Claim           │ │
│  │ (8状态)   │  │ · Cycle Detect│  │ · TTL Timeout Release   │ │
│  │          │  │ · Auto-Promote│  │ · Failure Limit          │ │
│  └────┬─────┘  └──────────────┘  └────────────┬───────────┘ │
│       │                                         │           │
│       ▼                                         ▼           │
│  ┌──────────┐  ┌──────────────┐  ┌────────────────────────┐ │
│  │ Task     │  │ Workspace    │  │ Audit Trail             │ │
│  │ Store    │  │ Manager      │  │ · Event Store           │ │
│  │ (SQLite) │  │ · Scratch    │  │ · Run History           │ │
│  │ WAL+BIMM │  │ · Dir        │  │ · Query API             │ │
│  │          │  │ · Worktree   │  │                         │ │
│  └──────────┘  └──────────────┘  └────────────────────────┘ │
│                                                              │
│  ┌────────────────────────────────────────────────────────┐  │
│  │ HITL Subsystem                                         │  │
│  │ · Block / Unblock · Comments · Notifications           │  │
│  └────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
```

### 数据模型（SQLite）

使用独立 SQLite 数据库（默认 `<data-dir>/board.db`），WAL 模式 + `BEGIN IMMEDIATE` 事务。

**5 张表：**

| 表 | 说明 |
|----|------|
| `tasks` | 核心任务实体（ID/标题/状态/指派人/认领锁/优先级/工作区/重试计数等） |
| `task_links` | 父子依赖关系（联合主键 parent_id + child_id，拒绝自链接和循环依赖） |
| `task_runs` | 每次 Worker 执行的运行记录（开始/结束/超时/结果/错误/元数据） |
| `task_comments` | 评论线程（ID/任务/作者/正文/时间） |
| `task_events` | 审计事件流（创建/认领/完成/阻塞/重试等事件，JSON 负载） |

**索引：**

```sql
CREATE INDEX idx_tasks_assignee_status ON tasks(assignee, status);
CREATE INDEX idx_tasks_status ON tasks(status);
CREATE INDEX idx_tasks_tenant ON tasks(tenant);
CREATE INDEX idx_task_events_task_id ON task_events(task_id, created_at);
CREATE INDEX idx_task_runs_task_id ON task_runs(task_id, started_at);
```

**SQLite 配置：**

```sql
PRAGMA journal_mode=WAL;           -- Write-Ahead Logging
PRAGMA busy_timeout=5000;          -- 忙等待超时 5 秒
PRAGMA foreign_keys=ON;            -- 外键约束
```

### 状态机

```
                    ┌─────────────────────┐
           ┌───────│       todo          │◄──── 有未完成的 parent
           │       │ 待执行（依赖未满足）  │
           │       └──────────┬──────────┘
           │                  │ 所有 parent 完成
           │                  ▼
           │       ┌─────────────────────┐
           │       │       ready         │◄──── TTL 超时释放回来 · 失败重试
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

**有效转换：**

| 当前状态 | 可转换到 | 触发器 |
|----------|---------|--------|
| `todo` | `ready` | 所有 parent 完成 |
| `ready` | `running`, `blocked`, `failed` | Worker claim / Dispatcher block / 异常 |
| `running` | `completed`, `failed`, `blocked` | Worker 完成 / 失败 / 阻塞 |
| `blocked` | `running`, `ready`, `failed` | Unblock / 重新分配 / 放弃 |
| `failed` | `ready`, `archived` | 重试（低于 limit） / 放弃 |
| `completed` | `archived` | 归档 |
| `archived` | — | 终点状态 |

### 依赖链解析

- 父子关系通过 `task_links` 表存储
- 一个任务可以有多个父任务（多入度）和多个子任务（扇出）
- 当所有 parent 完成时，子任务自动从 `todo` 提升为 `ready`
- `self-link` 和循环依赖会在创建时被拒绝
- 依赖创建使用双向插入（`INSERT ... ON CONFLICT DO NOTHING` 保证幂等）

### Worker 派发器（Dispatcher）

后台守护线程，以可配置间隔（默认 5 秒）轮询任务：

```
poll_cycle()
  ├── TTL Release: 过期认领 → 状态回退 ready
  ├── Retry Promotion: 失败任务低于重试次数 → ready
  ├── Ready Claim & Spawn:
  │    对每个 ready 任务：
  │      ├── Priority 1: assignee 有 WS 连接 → 通过 WS 推送任务
  │      └── Priority 2: assignee 无 WS 连接 → 阻塞任务 (blocked)
  └── next cycle (sleep poll_interval)
```

**Claim Lock 机制：**
- UPDATE `tasks SET status='running', claim_lock=... WHERE status='ready' AND id=?`
- 原子操作防止双重派发
- TTL 超时（默认 900s / 15min）自动释放

### Workspace 工作区管理器

| 模式 | 说明 |
|------|------|
| `scratch` | 为任务分配随机临时目录，任务完成后自动清理 |
| `dir` | 固定路径，不改动 |
| `worktree` | Git worktree 模式（为任务创建独立的 Git 工作树） |

### HITL（Human-in-the-Loop）

- **Block** — 任务暂停等待人工审查，状态转为 `blocked`
- **Unblock** — 解除阻塞，任务回到 `ready` 或 `running`
- **Comments** — 评论线程（Markdown 格式，作者追踪），支持标记 `@assignee` 的评论

### 审计日志

所有操作通过 `task_events` 表记录：

| 事件类型 | 说明 |
|----------|------|
| `created` | 任务创建 |
| `claimed` | Worker 认领 |
| `started` | 开始执行 |
| `heartbeat` | 任务心跳 |
| `completed` | 成功完成 |
| `failed` | 失败 |
| `blocked` | 人工阻塞 |
| `unblocked` | 解除阻塞 |
| `commented` | 添加评论 |
| `archived` | 归档 |
| `dependency_promoted` | 依赖链提升 |

---

## OAuth 2.1 认证与授权

### 架构概览

```
┌─────────────────────────────────────────────────────────────┐
│                     A2A Registry                             │
│                                                             │
│  ┌──────────────────┐    ┌──────────────────────────────┐  │
│  │ AuthMiddleware    │    │ AuthHandler                  │  │
│  │ ───────────────── │    │ ─────────────────────       │  │
│  │ 拦截受保护端点      │    │ POST /auth/token            │  │
│  │ Bearer token 校验  │    │ POST /auth/register         │  │
│  │ Scope 鉴权        │    │ GET /.well-known/oauth-*    │  │
│  │ request['agent_id]│    │ GET /.well-known/jwks.json  │  │
│  └────────┬─────────┘    └─────────────┬────────────────┘  │
│           │                            │                    │
│           ▼                            ▼                    │
│  ┌──────────────────────────────────────────────────────┐  │
│  │               Store (统一 SQLite)                      │  │
│  │  agents 表 │ oauth_clients 表 │ oauth_tokens 表        │  │
│  │  auth_codes 表 (WAL 模式, 线程安全 RLock)               │  │
│  │  启动时自动从旧 auth.json / registry.json 迁移          │  │
│  └──────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
```

### JWT Token 结构

```json
{
  "iss": "simple-a2a-registry",
  "sub": "agent-1",
  "aud": ["simple-a2a-registry", "agent-2"],
  "exp": 1712345678,
  "iat": 1712342078,
  "scope": "task:read task:write",
  "jti": "unique-token-id"
}
```

### 密钥管理

| 阶段 | 算法 | 说明 |
|------|------|------|
| 开发/测试 | HS256 (HMAC) | 对称密钥，启动时自动生成 |
| 生产 | RS256 (RSA) | 非对称密钥对，JWKS 端点供客户端验证 |

### Scope 设计

| Scope | 描述 | 适用端点 |
|-------|------|---------|
| `task:read` | 读取任务列表和详情 | 编排任务查询、直接任务查询 |
| `task:write` | 创建和修改任务 | 任务创建、分发、操作 |
| `agent:read` | 读取 Agent 列表和详情 | Agent 查询 |
| `agent:register` | 注册新 Agent | POST /v1/agents |
| `agent:admin` | 管理 Agent（删除/禁用） | DELETE /v1/agents/{id} |
| `registry:admin` | Registry 管理操作 | POST/GET/DELETE /admin/clients |

### 公开端点

以下端点始终无需认证：
- `GET /health` — 健康检查
- `GET /.well-known/agent-card.json` — Registry Agent Card
- `GET /.well-known/oauth-authorization-server` — OAuth 元数据
- `GET /.well-known/jwks.json` — JWT 公钥
- `POST /auth/token` — Token 获取
- `POST /auth/register` — 客户端注册

### 认证模式对比

| 模式 | 参数 | 行为 | 适用场景 |
|------|------|------|---------|
| 开发 | `--auth-enabled false`（默认） | 所有端点开放 | 本地开发/测试 |
| 生产 | `--auth-enabled true` | 受保护端点需 Bearer Token | 生产部署 |

### 安全集成流程（方案C：Admin 预创建）

```
┌─────────────────────────────────┐
│ 1. Admin 预创建 OAuth 客户端     │
│    └─ POST /admin/clients       │
│       → 获取 client_id/secret   │
├─────────────────────────────────┤
│ 2. Admin 分发凭据到 Agent        │
│    └─ 配置文件/环境变量/Secret   │
├─────────────────────────────────┤
│ 3. Agent 获取 Token             │
│    └─ POST /auth/token          │
│       → 返回 JWT access_token   │
├─────────────────────────────────┤
│ 4. Agent 注册                   │
│    └─ POST /v1/agents           │
│       (Authorization: Bearer)   │
│       → 返回 agent_id           │
├─────────────────────────────────┤
│ 5. Agent 发现                   │
│    └─ GET /.well-known/card.json│
│       → 获取其他 Agent 的安全方案│
├─────────────────────────────────┤
│ 6. 调用受保护 API               │
│    └─ Authorization: Bearer     │
│       + scope 检查              │
└─────────────────────────────────┘
```

---

## 设计决策

| 决策 | 选择 | 原因 |
|------|------|------|
| 数据模型 | Pydantic-free dataclass | 零依赖，仅需 `aiohttp` |
| Agent 持久化 | SQLite (registry.db, WAL 模式) | 统一存储，并发安全，启动时自动从旧 JSON 迁移 |
| 任务持久化 | SQLite WAL 模式 | 并发安全，无需外部数据库 |
| 心跳 | 120s 超时 / 300s 清理 | 平衡网络波动容忍度和资源回收时效 |
| 直接任务存储 | 内存 | 适合轻量级、临时性任务追踪 |
| 认证 | OAuth 2.1 JWT (RS256/HS256) | A2A v1.0 规范要求 SecurityScheme 集成 |
| 任务分发 | WebSocket 推送 | 低延迟、双向通信，优于 HTTP 轮询 |
| 编排派发 | 原子 UPDATE + 后台轮询 | 防双重派发，Worker 无状态可水平扩展 |

---

## 数据库引擎层

Registry 使用双数据库引擎架构，支持运行时在 SQLite（开发）和 MySQL（生产）之间切换。

### 引擎架构

```python
# simple_a2a_registry/database/engine.py
DatabaseEngine (ABC)
  ├── SQLiteEngine    — 开发/单机部署，WAL 模式，线程安全 RLock
  └── MySQLEngine     — 生产/集群部署，QueuePool，utf8mb4

RetryEngine          — 透明包装器，指数退避重试（3 次默认）
```

### 引擎选择机制

| 场景 | 驱动 | 配置示例 |
|------|------|---------|
| 开发/测试 | `sqlite+aiosqlite` | `database.driver: sqlite` |
| 生产/集群 | `mysql+aiomysql` | `database.driver: mysql` |

引擎通过 `DatabaseEngine.create(config)` 工厂方法在启动时根据配置选择。

### RetryEngine

透明包装器，拦截瞬态错误（数据库锁定、连接断开、超时）并自动重试：

- 指数退避策略：`2^attempt` 秒（1s, 2s, 4s）
- 默认最多 3 次尝试
- 超出限制则抛出自定义异常

### Alembic 迁移

```
migrations/
  env.py              — 支持 SQLite + MySQL 双目标
  versions/
    0001_initial_schema.py
    ...
```

迁移工作流：
```bash
alembic upgrade head                          # 应用所有迁移
ALEMBIC_DATABASE_URL="mysql+aiomysql://..." alembic upgrade head  # 应用到 MySQL
```

数据迁移脚本 `scripts/migrate_sqlite_to_mysql.py` 提供从 SQLite → MySQL 的增量迁移，采用批量事务 + 断点续传策略。

---

## 插件系统

插件系统允许第三方代码在定义好的生命周期点扩展 Registry 功能。

### 架构概览

```
Plugin (ABC)
  ├── name() → str                    — 唯一插件标识
  ├── load(config)                    — 加载阶段：读取配置
  ├── init(app)                       — 初始化阶段：注册路由/中间件
  ├── before_request(request)         — 请求前钩子
  ├── after_request(request, response)— 请求后钩子
  └── before_shutdown(app)            — 关闭前清理

PluginRegistry
  ├── discover()                      — 扫描 entry_points + config.yaml
  ├── dispatch(hook, *args)           — 按优先级依次派发钩子
  └── get_plugin(name) → Plugin       — 获取已加载的插件实例
```

### 钩子类型

| 类别 | 钩子 | 触发时机 |
|------|------|---------|
| 生命周期 | `load(config)` | 服务启动时 |
| | `init(app)` | router 注册完成后 |
| | `before_shutdown(app)` | 服务关闭时 |
| 请求拦截 | `before_request(request)` | 每个 HTTP 请求前 |
| | `after_request(request, response)` | 每个 HTTP 请求后 |
| 事件通知 | `on_agent_register(agent_id, card)` | Agent 注册时 |
| | `on_agent_deregister(agent_id)` | Agent 注销时 |
| | `on_agent_heartbeat(agent_id)` | Agent 心跳时 |
| | `on_task_created(task)` | 任务创建时 |
| | `on_task_completed(task, result)` | 任务完成时 |
| | `on_token_issued(token, client_id)` | Token 签发时 |
| | `on_server_start()` | 服务就绪时 |
| | `on_server_stop()` | 服务停止时 |

### 加载方式

1. **Entry Points**（`pyproject.toml`）— 显式声明
2. **Config 文件**（`config.yaml`）— 运行时动态加载

---

## Admin WebSocket Hub

为 Admin Dashboard 提供实时任务状态更新推送，由 `ws_admin.py` 实现。

### 连接管理

- 独立于 Agent WS Hub，专用于 Admin SPA 监控
- 支持多个 Admin 客户端同时订阅
- 每个客户端可选择关注特定任务或接收全部事件

### 推送事件

| 事件类型 | 触发条件 | 说明 |
|----------|---------|------|
| `task_created` | 任务创建 | 推送任务摘要 |
| `task_updated` | 状态变更 | 推送新旧状态 + assignee |
| `task_completed` | 任务完成 | 推送任务 ID + 结果 |
| `task_comment` | 新评论 | 推送评论内容和作者 |
| `ping` | 每 30 秒 | 服务端保活心跳 |

### 认证

Admin WS 通过查询参数 `?token=<jwt>` 传递 Bearer Token，要求 `registry:admin` scope。

---

## 限流系统

基于 Token Bucket 算法，支持双后端存储。

### 架构

```
RateLimiter (ABC)
  ├── MemoryRateLimiter     — 内存字典 + asyncio.Lock
  └── MySQLRateLimiter      — MySQL 表 + 行级锁
```

### Key 推导优先级

1. `client_id`（认证 Token 中的 sub 字段）
2. `X-Forwarded-For` / `X-Real-IP` 头
3. TCP 连接远端地址

### 限流配置

```yaml
rate_limit:
  enabled: true
  default_unauthenticated: 60    # 未认证端点每分钟 60 次
  default_authenticated: 300     # 认证端点每分钟 300 次
  storage: mysql                 # memory / mysql
  whitelist: ["my-super-agent"]  # 豁免客户端 ID
```

### 响应头

限流后的 HTTP 响应包含以下头：
- `X-RateLimit-Limit` — 每分钟配额
- `X-RateLimit-Remaining` — 剩余配额
- `X-RateLimit-Reset` — 配额重置时间戳
- `Retry-After` — 超出配额后的建议等待时间（秒）

---

## 审计日志系统

所有敏感操作写入 `audit_log` 表，采用 Append-only 设计。

### 事件记录字段

| 字段 | 类型 | 说明 |
|------|------|------|
| `event_type` | string | 分类标识（如 `agent_register`、`token_issue`、`admin_action`） |
| `actor` | string | 操作者标识（client_id 或 user_id） |
| `target` | string | 操作目标（agent_id、task_id、client_id） |
| `timestamp` | float | 操作时间戳 |
| `success` | bool | 操作是否成功 |
| `detail` | string | 操作详情描述 |

### 记录的事件

| 事件类型 | 触发条件 |
|----------|---------|
| `agent_register` | Agent 注册成功/失败 |
| `agent_deregister` | Agent 注销 |
| `token_issue` | JWT Token 签发 |
| `token_revoke` | Token 吊销 |
| `task_create` | 编排任务创建 |
| `task_complete` | 任务完成 |
| `task_block` | 任务阻塞 |
| `admin_action` | Admin 管理操作（创建/删除客户端） |

### 保留策略

默认保留 90 天，通过 `audit.ttl_days` 配置项可调。到期事件由后台任务清理。

---

## 多租户隔离

支持在多项目环境中隔离 Agent 和任务数据。

### 租户标识传递

- **HTTP Header**：`X-Tenant-ID` — 请求级租户标签
- **查询参数**：`?tenant=<value>` — API 显式指定
- **Token 属性**：JWT Token 中可附加 `tenant` 声明

### 隔离范围

| 资源 | 隔离方式 | 说明 |
|------|---------|------|
| Agent | `tenant` 列过滤 | 每个 Agent 所属租户，查询时自动过滤 |
| V2 任务 | `tenant` 列过滤 | 任务创建时记录租户，查询/调度按租户隔离 |
| OAuth Token | `tenant` 声明 | Token 签发时记录请求中的租户 |
| 审计日志 | `tenant` 字段 | 审计事件带租户标签 |

### 租户传播

- 创建任务时，通过请求头或查询参数设定租户
- 子任务（Swarm Worker、依赖链）自动继承父任务的租户
- Dispatcher 按租户过滤 ready 任务
- 审计日志记录每条事件所属租户

### 兼容性

未指定租户时（`tenant=""`），行为等同于传统单租户模式。向后兼容所有现有客户端。

---

## 用户系统

Web Dashboard 使用基于 Session 的认证体系，与 OAuth 2.1 API 认证独立。

### 架构

```
UserRegistry
  ├── create_user(username, password, ...)  — 创建账户
  ├── authenticate(username, password)       — 密码验证
  ├── create_session(user_id) → session_id  — 创建 Session
  └── validate_session(session_id) → user    — 验证 Session

SessionStore
  └── user_sessions (内存/SQLite 持久化)
```

### 用户-API 认证关系

| 认证方式 | 使用场景 | Token 类型 |
|----------|---------|-----------|
| OAuth 2.1 (client_credentials) | Agent/Client API 调用 | JWT Bearer Token |
| Session Cookie | Web Dashboard SPA | 服务端 Session |

两个认证体系并行运行，互不干扰。

---

## Web Dashboard

内置 Web Dashboard，路径 `/`，默认 http://localhost:8321：

- Agent 列表（状态、标签、技能、WS 徽章）
- Agent 详情展开面板
- Kanban 看板（Board / List 双视图）
- 任务详情弹窗（依赖链、运行记录、事件流、评论）
- OAuth Clients 管理页面
- 全局统计（Agent 数、WS 连接数、任务分布）
- 实时刷新（每 15 秒）