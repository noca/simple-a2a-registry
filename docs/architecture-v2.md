# A2A Registry — Orchestration Engine 架构设计文档（V2）

> **版本：** v2.0-draft  
> **创建日期：** 2026-05-22  
> **适用项目：** simple-a2a-registry-v2  
> **前置文档：** [API.md](API.md)（V1 端点定义）

---

## 目录

1. [概述](#1-概述)
2. [整体架构](#2-整体架构)
3. [数据模型](#3-数据模型)
4. [API 契约](#4-api-契约)
5. [状态机](#5-状态机)
6. [依赖链解析](#6-依赖链解析)
7. [Worker 派发器](#7-worker-派发器)
8. [Workspace 管理器](#8-workspace-管理器)
9. [HITL 设计](#9-hitl-设计)
10. [审计日志](#10-审计日志)
11. [与现有系统的集成](#11-与现有系统的集成)
12. [部署与配置](#12-部署与配置)
13. [测试策略](#13-测试策略)
14. [实施路线图](#14-实施路线图)

---

## 1. 概述

### 1.1 设计目标

Simple A2A Registry V1 实现了 Agent 注册/发现、WebSocket Hub 任务分发、心跳保活等基础功能。V2 的目标是在此基础上，新增一个 **Orchestration Engine（编排引擎）**，使其具备完整的 Kanban 级任务编排能力：

| 目标 | 说明 |
|------|------|
| **任务生命周期管理** | 从创建到归档的完整状态机，支持依赖链、重试、超时释放 |
| **Worker 自动派发** | 基于 Profile 的原子化任务认领与派发，防止重复执行 |
| **多 Agent 协调** | 通过依赖链和 Workspace 隔离，实现多 Agent 分阶段协作 |
| **人机协同** | Block/Unblock 机制、评论线程、通知推送，支持 Human-in-the-Loop |
| **可观测性** | 全事件审计日志、任务运行记录、结构化元数据 |
| **非侵入集成** | Orchestration Engine 作为独立模块，不改动现有 Agent 发现和 WS Hub |

### 1.2 非功能需求

| 维度 | 要求 |
|------|------|
| **持久性** | SQLite 替代 JSON 文件持久化，WAL 模式 + `BEGIN IMMEDIATE` 防并发写冲突 |
| **可用性** | Dispatcher 轮询周期可配置，从故障/重启中自动恢复未完成的任务 |
| **可伸缩** | 单 Board 单 DB，多 Board 通过命名空间隔离；Worker 无状态，可横向扩展 |
| **安全性** | Claim Lock 机制防双重派发；任务隔离通过 Workspace 实现 |
| **兼容性** | 不修改现有 Agent 发现/WS Hub 模块；V1 任务表可迁移 |

### 1.3 术语表

| 术语 | 英文 | 定义 |
|------|------|------|
| **编排引擎** | Orchestration Engine | 负责任务全生命周期管理的核心模块集合 |
| **任务** | Task | 一个可调度的工作单元，持有状态、指派人、依赖关系 |
| **运行时** | Run | 任务被 Worker 认领后的一次执行记录（可重试多次） |
| **看板** | Board | 一组逻辑隔离的任务集合，对应一个独立的 SQLite 数据库 |
| **派发器** | Dispatcher | 定期轮询 `ready` 任务并认领/生成 Worker 进程的守护模块 |
| **认领** | Claim | Worker 对任务的原子的领取操作，防止其他 Worker 同时执行 |
| **工作区** | Workspace | 为每个任务分配的隔离文件系统目录 |
| **指派人** | Assignee | 负责执行任务的 Worker Profile 名称 |
| **依赖链** | Dependency Chain | 父任务→子任务之间的关系，子任务需等待所有父任务完成 |
| **人工介入** | HITL | Human-in-the-Loop，任务在人工阻塞/审查后才能继续 |

---

## 2. 整体架构

### 2.1 模块划分

```
┌──────────────────────────────────────────────────────────┐
│                   HTTP / WebSocket Layer                  │
│  (aiohttp server — 现有 Router + 新增 V2 Endpoints)       │
├────────────┬─────────────┬───────────────────────────────┤
│            │             │                                │
│  ┌─────────┴──────┐  ┌──┴────────────┐  ┌──────────────┴─┐
│  │ Agent Discovery │  │  WS Hub       │  │ Orchestration  │
│  │ (V1 — 不变)     │  │  (V1 — 不变)   │  │ Engine (NEW)   │
│  └─────────┬──────┘  └──┬────────────┘  └────────┬───────┘
│            │             │                        │
│  ┌─────────┴─────────────┴────────────────────────┴───────┐
│  │               Store Layer                               │
│  │  A2ARegistryStore (V1 — JSON)  +  TaskStore (V2 — SQLite)│
│  └─────────────────────────────────────────────────────────┘
└──────────────────────────────────────────────────────────┘
```

### 2.2 Orchestration Engine 内部架构

```
┌─────────────────────────────────────────────────────────────┐
│                  Orchestration Engine                        │
│                                                              │
│  ┌──────────┐  ┌──────────────┐  ┌────────────────────────┐ │
│  │ Task     │  │ Dependency   │  │ Dispatcher              │ │
│  │ State    │◄─│ Resolution   │  │ · Poll Ready Tasks      │ │
│  │ Machine  │  │ · Parent/Child│  │ · Atomic Claim           │ │
│  │          │  │ · Cycle Detect│  │ · TTL Timeout Release   │ │
│  │          │  │ · Auto-Promote│  │ · Failure Limit          │ │
│  └────┬─────┘  └──────────────┘  └────────────┬───────────┘ │
│       │                                         │           │
│       │           ┌──────────────────────────────┘           │
│       ▼           ▼                                          │
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

### 2.3 数据流

```
  Client / Assigner              Dispatcher                  Worker (Agent)
       │                            │                            │
       │ POST /v1/tasks             │                            │
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
  │ ready    │                      │    (UPDATE ... WHERE      │
  └──────────┘                      │     status=ready)         │
                                    │                            │
                                    │    ┌──────────────────┐   │
                                    │    │ status=running    │   │
                                    │    │ workspace=alloc   │   │
                                    │    │ spawn worker proc │   │
                                    │    └──────────────────┘   │
                                    │                            │
                                    │ ◄── Heartbeat ─────────── │
                                    │ ◄── Complete / Block ──── │
       ◄──── 事件 / Run ────────────                             │
```

---

## 3. 数据模型

所有实体存储在同一个 SQLite 数据库文件中。数据库路径可配置，默认位于 Registry 数据目录下的 `board.db`。

### 3.1 ER 概览

```
┌─────────────┐       ┌─────────────┐
│   tasks     │ 1──N  │  task_links │
│             │       │             │
│ (核心实体)   │       │ parent_id   │
│             │       │ child_id    │
└──────┬──────┘       └─────────────┘
       │
       │ 1──N
       │
┌──────┴──────┐       ┌─────────────┐
│  task_runs  │       │ task_comments│
│             │       │             │
│ (执行记录)   │       │ task_id     │
│             │       │ author      │
└─────────────┘       └─────────────┘

┌──────────────────┐
│   task_events    │
│                  │
│ (审计日志流)      │
└──────────────────┘
```

### 3.2 tasks 表

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | TEXT PK | 任务 ID，格式为 `t_` + UUID 短码（如 `t_a1b2c3d4`） |
| `title` | TEXT NOT NULL | 任务标题 |
| `body` | TEXT | 任务描述/规格（Markdown 格式，不超过 8KB） |
| `assignee` | TEXT | 指派的 Worker Profile 名，`NULL` = 未指派 |
| `status` | TEXT NOT NULL | 当前状态：`todo`/`ready`/`running`/`blocked`/`completed`/`failed`/`archived` |
| `priority` | INTEGER | 优先级（数字越大优先级越高，默认 0） |
| `created_by` | TEXT | 创建者标识 |
| `created_at` | INTEGER NOT NULL | 创建时间（Unix 时间戳） |
| `started_at` | INTEGER | 首次被认领的时间 |
| `completed_at` | INTEGER | 完成/失败时间 |
| `workspace_kind` | TEXT | 工作区模式：`scratch`/`dir`/`worktree` |
| `workspace_path` | TEXT | 分配的工作区路径（`scratch` 模式下可 `NULL`） |
| `claim_lock` | TEXT | 当前认领锁，格式 `<worker_id>:<pid>` |
| `claim_expires` | INTEGER | 认领锁过期时间 |
| `tenant` | TEXT | 租户/命名空间标识 |
| `result` | TEXT | 任务完成结果（JSON） |
| `consecutive_failures` | INTEGER | 连续失败计数 |
| `worker_pid` | INTEGER | 当前 Worker 进程 ID |
| `last_failure_error` | TEXT | 最近一次失败的错误摘要 |
| `max_runtime_seconds` | INTEGER | 单次运行超时上限 |
| `last_heartbeat_at` | INTEGER | 最近一次心跳时间 |
| `current_run_id` | INTEGER | 当前活跃的 `task_runs.id` |
| `max_retries` | INTEGER | 最大重试次数（`NULL` = 使用全局默认） |

### 3.3 task_links 表

| 字段 | 类型 | 说明 |
|------|------|------|
| `parent_id` | TEXT | 父任务 ID |
| `child_id` | TEXT | 子任务 ID |
| **PK** | `(parent_id, child_id)` | 联合主键 |

语义：
- 当所有 `parent_id` 指向的任务状态为 `completed` 时，子任务自动从 `todo` 提升为 `ready`
- 一个任务可以有多个父任务（多入度依赖）
- 一个任务可以有多个子任务（扇出）
- `self-link` 和循环依赖被拒绝创建

### 3.4 task_runs 表

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | INTEGER PK AUTO | 运行记录 ID |
| `task_id` | TEXT NOT NULL | 关联任务 |
| `profile` | TEXT | 执行此运行的 Worker Profile |
| `status` | TEXT NOT NULL | 运行状态：`running`/`done`/`blocked`/`crashed`/`timed_out`/`failed`/`released` |
| `claim_lock` | TEXT | 该次运行的认领锁 |
| `claim_expires` | INTEGER | 该次运行的认领过期时间 |
| `worker_pid` | INTEGER | 执行进程的 PID |
| `max_runtime_seconds` | INTEGER | 该次运行的超时设置 |
| `last_heartbeat_at` | INTEGER | 该次运行的最后心跳 |
| `started_at` | INTEGER NOT NULL | 开始时间 |
| `ended_at` | INTEGER | 结束时间 |
| `outcome` | TEXT | 结果：`completed`/`blocked`/`crashed`/`timed_out`/`spawn_failed`/`gave_up`/`reclaimed` |
| `summary` | TEXT | 人类可读的结果摘要（1-3 句话） |
| `metadata` | TEXT | 结构化结果数据（JSON） |
| `error` | TEXT | 错误信息 |

### 3.5 task_comments 表

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | INTEGER PK AUTO | 评论 ID |
| `task_id` | TEXT NOT NULL | 所属任务 |
| `author` | TEXT NOT NULL | 评论作者 |
| `body` | TEXT NOT NULL | 评论正文（Markdown） |
| `created_at` | INTEGER NOT NULL | 创建时间 |

### 3.6 task_events 表

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | INTEGER PK AUTO | 事件 ID |
| `task_id` | TEXT NOT NULL | 关联任务 |
| `run_id` | INTEGER | 关联运行（可空） |
| `kind` | TEXT NOT NULL | 事件类型：`created`/`claimed`/`started`/`heartbeat`/`completed`/`failed`/`blocked`/`unblocked`/`commented`/`archived`/`dependency_promoted` |
| `payload` | TEXT | 事件负载（JSON） |
| `created_at` | INTEGER NOT NULL | 事件发生时间 |

索引：

```sql
CREATE INDEX idx_tasks_assignee_status ON tasks(assignee, status);
CREATE INDEX idx_tasks_status ON tasks(status);
CREATE INDEX idx_tasks_tenant ON tasks(tenant);
CREATE INDEX idx_task_events_task_id ON task_events(task_id, created_at);
CREATE INDEX idx_task_runs_task_id ON task_runs(task_id, started_at);
CREATE INDEX idx_task_comments_task_id ON task_comments(task_id, created_at);
```

### 3.7 SQLite 配置

```sql
PRAGMA journal_mode=WAL;           -- Write-Ahead Logging 模式
PRAGMA busy_timeout=5000;          -- 忙等待超时 5 秒
PRAGMA foreign_keys=ON;            -- 外键约束
```

所有写操作使用 `BEGIN IMMEDIATE` 事务模式，确保并发写场景下不会因多 Writer 冲突而失败。

---

## 4. API 契约

### 4.1 端点一览

所有 V2 端点统一以 `/v2/` 为前缀，与 V1 端点 `/v1/` 共存。

> 🔒 **认证要求**：当 `--auth-enabled true` 时，所有 V2 端点需要 `Authorization: Bearer <token>` header。
> 保护范围见下方 [认证与安全](#14-认证与安全) 章节。

| 方法 | 路径 | 说明 | 状态码 | 认证 | Scope |
|------|------|------|--------|------|-------|
| POST | `/v2/tasks` | 创建任务 | 201 | ✅ | `task:write` |
| GET | `/v2/tasks` | 列表查询 | 200 | ✅ | `task:read` |
| GET | `/v2/tasks/{id}` | 任务详情（含依赖链、评论、运行历史） | 200 | ✅ | `task:read` |
| POST | `/v2/tasks/{id}/claim` | Worker 认领任务 | 200/409 | ✅ | `task:write` |
| POST | `/v2/tasks/{id}/complete` | 完成任务 | 200 | ✅ | `task:write` |
| POST | `/v2/tasks/{id}/block` | 阻塞任务（HITL） | 200 | ✅ | `task:write` |
| POST | `/v2/tasks/{id}/unblock` | 解除阻塞 | 200 | ✅ | `task:write` |
| POST | `/v2/tasks/{id}/heartbeat` | 任务级心跳 | 200 | ✅ | `task:write` |
| POST | `/v2/tasks/{id}/comment` | 添加评论 | 201 | ✅ | `task:write` |
| DELETE | `/v2/tasks/{id}` | 归档任务 | 200 | ✅ | `task:write` |
| POST | `/v2/tasks/{id}/depend` | 添加依赖关系（双向） | 200 | ✅ | `task:write` |
| DELETE | `/v2/tasks/{id}/depend/{parent_id}` | 移除依赖关系 | 200 | ✅ | `task:write` |

### 4.2 端点详情

#### POST /v2/tasks — 创建任务

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
| `body` | string | 否 | 任务描述 |
| `assignee` | string | 否 | 指派的 Worker Profile 名 |
| `priority` | int | 否 | 优先级（默认 0） |
| `parents` | string[] | 否 | 父任务 ID 列表 |
| `workspace_kind` | string | 否 | 工作区模式（默认 scratch） |
| `max_runtime_seconds` | int | 否 | 超时上限（秒） |
| `max_retries` | int | 否 | 最大重试次数 |
| `tenant` | string | 否 | 租户标签 |

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

#### GET /v2/tasks — 列表查询

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
      "started_at": 1712345700
    }
  ]
}
```

---

#### GET /v2/tasks/{id} — 任务详情

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

---

#### POST /v2/tasks/{id}/claim — 认领任务

**请求体：**

```json
{
  "worker_id": "worker-1",
  "pid": 12345
}
```

**语义：** Worker 尝试对状态为 `ready` 的任务进行原子认领。成功后任务状态转为 `running`。

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
| 409 | `claim_conflict` | 任务已被其他 Worker 认领 |
| 400 | `invalid_status` | 任务状态不是 `ready` |

原子认领 SQL：

```sql
BEGIN IMMEDIATE;
UPDATE tasks
SET status = 'running',
    claim_lock = :lock,
    claim_expires = :expires,
    started_at = COALESCE(started_at, :now),
    worker_pid = :pid,
    current_run_id = :run_id
WHERE id = :task_id
  AND status = 'ready'
  AND (claim_lock IS NULL OR claim_expires < :now);
-- 检查影响行数，为 0 表示认领失败
COMMIT;
```

---

#### POST /v2/tasks/{id}/complete — 完成任务

**请求体：**

```json
{
  "result": {"deploy_url": "http://..."},
  "summary": "登录模块实现完成，包含 JWT 认证",
  "metadata": {"lines_of_code": 350, "tests_passed": 12}
}
```

**语义：** 仅当当前 `claim_lock` 匹配时才允许完成，防止误操作。

**响应：** `200 OK`

**错误码：** 403 — `claim_mismatch`（认领锁不匹配）；404 — `task_not_found`

---

#### POST /v2/tasks/{id}/block — 阻塞任务

**请求体：**

```json
{
  "reason": "需要人工审核代码质量"
}
```

**语义：** 将运行中的任务转为 `blocked` 状态。只有任务当前的持有者可以调用。

**响应：** `200 OK`

```json
{
  "task_id": "t_a1b2c3d4",
  "status": "blocked",
  "block_reason": "需要人工审核代码质量"
}
```

---

#### POST /v2/tasks/{id}/unblock — 解除阻塞

**请求体：**

```json
{
  "reason": "审核通过"
}
```

**语义：** 将 `blocked` 任务转回 `running`，允许 Worker 继续执行。

**响应：** `200 OK`

---

#### POST /v2/tasks/{id}/heartbeat — 心跳

**请求体：**

```json
{
  "progress": "已完成 70%，正在处理边界情况",
  "claim_lock": "worker-1:12345"
}
```

**语义：** 延长认领锁的 TTL，更新 `last_heartbeat_at`。Long-running 任务应周期性调用（建议 1-5 分钟）。如果超过 `claim_expires` 未心跳，Dispatcher 会将该任务释放回 `ready` 或 `failed`（根据配置）。

**响应：** `200 OK`

```json
{
  "task_id": "t_a1b2c3d4",
  "claim_expires": 1712347200
}
```

---

#### POST /v2/tasks/{id}/comment — 添加评论

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

---

#### DELETE /v2/tasks/{id} — 归档任务

**语义：** 只有状态为 `completed` 或 `failed` 的任务可以被归档。归档后状态变为 `archived`。

**响应：** `200 OK`

```json
{
  "task_id": "t_a1b2c3d4",
  "status": "archived"
}
```

**错误码：** 400 — `invalid_status`（只有已完成/失败的任务可归档）

---

#### POST /v2/tasks/{id}/depend — 添加依赖

**请求体：**

```json
{
  "parent_id": "t_parent1"
}
```

**语义：** 为当前任务添加一个父依赖。如果父任务全部完成，子任务自动提升为 `ready`。

**响应：** `200 OK`

**错误码：** 400 — `cycle_detected`（将导致循环依赖）；404 — `parent_not_found`

---

#### DELETE /v2/tasks/{id}/depend/{parent_id} — 移除依赖

**语义：** 移除当前任务的一个父依赖。如果移除后仍有其他未完成的父任务，子任务保持 `todo` 状态。

**响应：** `200 OK`

---

## 5. 状态机

### 5.1 状态定义

```
               ┌──────────────────────────────────┐
               │           初始状态                  │
               └────────────────┬─────────────────┘
                                │
                                ▼
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
           │                │     │             │          │
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

### 5.2 状态转换表

| 当前状态 | 触发事件 | 目标状态 | 说明 |
|---------|---------|---------|------|
| — | `创建` | `todo` | 有新任务创建时，自动检查是否有 parent |
| `todo` | `所有 parent 完成` | `ready` | 依赖链引擎自动提升 |
| `ready` | `Worker 认领` | `running` | Dispatcher 或 Worker 直接调用 claim |
| `ready` | `TTL 超时释放` | `ready` | 认领锁过期后释放回 ready 队列（实际保持不变） |
| `running` | `Worker 完成` | `completed` | Worker 主动上报完成 |
| `running` | `Worker 上报失败` | `failed` | Worker 主动上报失败 |
| `running` | `人工阻塞` | `blocked` | 通过 block API |
| `running` | `认领锁 TTL 超时` | `failed` | 超时后根据 `consecutive_failures` 判断 |
| `running` | `Worker 进程崩溃` | `failed` | 通过心跳丢失或 OS 信号检测 |
| `blocked` | `解除阻塞` | `running` | Worker 可继续执行 |
| `blocked` | `认领锁 TTL 超时` | `failed` | 超时未解除则标记失败 |
| `completed` | `归档` | `archived` | 清理资源 |
| `failed` | `归档` | `archived` | 清理资源 |
| `failed` | `自动重试` | `ready` | 低于 `max_retries` 时自动重试（写入新的 run） |
| `archived` | — | — | 所有转换拒绝，终点状态 |

### 5.3 重试策略

- **连续失败计数器** `consecutive_failures` 在每次非成功结束时递增
- 每次 **成功完成** 时计数器清零
- 当 `consecutive_failures <= max_retries` 时，Dispatcher 将任务转回 `ready` 状态等待重新认领
- 当 `consecutive_failures > max_retries` 时，任务保持 `failed`，不再自动调度
- 全局默认 `max_retries = 3`，可通过 `max_retries` 字段按任务覆盖

### 5.4 认领锁 TTL

- 默认 TTL：15 分钟（与 Hermes Kanban 一致）
- Worker 在 Long-running 任务中应每 1-5 分钟发送一次 `heartbeat`
- 心跳会延长 `claim_expires` 为 `now + TTL`
- 超时后，Dispatcher 的下一个轮询周期会释放锁（任务转 `failed`）

---

## 6. 依赖链解析

### 6.1 算法描述

依赖链引擎运行在以下关键时机：

1. **任务创建时**：如果指定了 `parents`，在 `task_links` 中插入对应记录，然后对新任务运行依赖解析
2. **任务完成时**：查询所有以该任务为 parent 的子任务，对其运行依赖解析
3. **依赖添加/删除时**：对受影响的任务运行依赖解析

**依赖解析算法：**

```
function resolve_dependencies(task_id):
    // 查询该任务所有尚未完成的 parent
    uncompleted = SELECT COUNT(*)
                  FROM task_links l
                  JOIN tasks p ON l.parent_id = p.id
                  WHERE l.child_id = task_id
                    AND p.status != 'completed'
                    AND p.status != 'archived'

    // 获取当前任务状态
    current = SELECT status FROM tasks WHERE id = task_id

    if uncompleted == 0:
        // 所有 parent 已完成
        if current == 'todo':
            UPDATE tasks SET status = 'ready' WHERE id = task_id
            emit_event(task_id, 'dependency_promoted', {from: 'todo', to: 'ready'})
    else:
        if current == 'ready':
            // 有新的未完成 parent（如依赖被重新激活），退回 todo
            UPDATE tasks SET status = 'todo' WHERE id = task_id
```

### 6.2 循环检测

每次添加新的依赖关系时，必须对受影响的任务执行循环检测。

**检测算法（DFS + 着色法）：**

```
function detect_cycle(child_id, proposed_parent_id):
    // 在添加 proposed_parent_id → child_id 之前，检测是否会形成环
    // 等价于：从 proposed_parent_id 出发 DFS，如果遇到 child_id 则说明有环
    
    visited = set()
    stack = [proposed_parent_id]
    
    while stack:
        node = stack.pop()
        if node == child_id:
            return True  // 有环
        if node in visited:
            continue
        visited.add(node)
        // 获取该节点作为 child 的所有 parent
        parents = SELECT parent_id FROM task_links WHERE child_id = node
        stack.extend(parents)
    
    return False  // 无环
```

**约束：**
- 自链接（`parent_id == child_id`）直接被拒绝
- 当检测到环时，API 返回 400 `cycle_detected` 错误

### 6.3 多父依赖示例

```
    t_A (设计API)    t_B (写数据库Schema)
          \              /
           \            /
            ▼          ▼
         t_C (实现业务逻辑)
                │
                ▼
         t_D (编写测试)

规则：
- t_C 只有在 t_A 和 t_B 都 completed 后才变为 ready
- t_D 在 t_C completed 后才变为 ready
- 当 t_B 提前完成时，t_C 保持 todo（还在等 t_A）
- 当 t_A 也完成时，t_C 自动变为 ready
```

---

## 7. Worker 派发器

### 7.1 Dispatcher 架构

```
┌─────────────────────────────────────────────────────────┐
│                    Dispatcher                             │
│                                                          │
│   ┌──────────────┐   每 N 秒              ┌───────────┐  │
│   │  Poll Loop   │────────────────────►   │ Claim     │  │
│   │  (asyncio)   │                       │ Ready     │  │
│   └──────┬───────┘                       │ Tasks     │  │
│          │                               └─────┬─────┘  │
│          ▼                                     │        │
│   ┌──────────────┐                             ▼        │
│   │  TTL Release │                   ┌──────────────┐   │
│   │  (超时释放)   │                   │ Spawn Worker │   │
│   └──────┬───────┘                   │ Process      │   │
│          │                           └──────┬───────┘   │
│          ▼                                  │          │
│   ┌──────────────┐                          ▼          │
│   │  Retry Logic │                ┌──────────────────┐  │
│   │  (重试判断)   │                │ Inject Env Vars  │  │
│   └──────────────┘                │ · KANBAN_TASK    │  │
│                                   │ · KANBAN_BOARD   │  │
│                                   │ · WORKSPACE_PATH │  │
│                                   └──────────────────┘  │
└─────────────────────────────────────────────────────────┘
```

### 7.2 Poll Loop 流程

```
每个轮询周期（默认 5 秒）：

1. TTL 清理
   └─ 查找 claim_expires < now 的 running 任务
      ├─ 释放回 running → failed（记录 run outcome='timed_out'）
      └─ 更新 consecutive_failures

2. 重试提升
   └─ 查找 consecutive_failures <= max_retries 的 failed 任务
      ├─ 创建新的 run 记录
      └─ 更新状态为 ready

3. 依赖链解析
   └─ 对已完成的任务，触发子任务的依赖解析

4. 认领并派发
   └─ 查找 status='ready' 且有 assignee 的任务
      ├─ 按 priority 排序（高优先在前）
      ├─ 尝试原子 claim（UPDATE ... WHERE status='ready'）
      └─ 成功则 spawn Worker 进程
         ├─ 分配 Workspace
         ├─ 设置环境变量
         └─ 作为子进程启动
```

### 7.3 原子 Claim 流程

```
                              ┌─────────────┐
                              │  Poll Ready  │
                              │  Tasks       │
                              └──────┬──────┘
                                     │
                                     ▼
                         ┌─────────────────────┐
                         │ BEGIN IMMEDIATE     │
                         │ (获取写锁)           │
                         └──────────┬──────────┘
                                    │
                                    ▼
                    ┌─────────────────────────────┐
                    │ SELECT ... WHERE status='ready'│
                    │ FOR UPDATE                   │
                    │ (锁定该行)                    │
                    └──────────┬──────────────────┘
                              │
                              ▼
                    ┌─────────────────────────────┐
                    │ 检查 claim_lock == NULL     │
                    │ 或 claim_expires < now      │
                    └──────────┬──────────────────┘
                              │
                    ┌─────────┴─────────┐
                    │ 有效              │ 无效
                    ▼                   ▼
           ┌──────────────────┐  ┌──────────────┐
           │ UPDATE SET       │  │ COMMIT       │
           │ status='running' │  │ return 409   │
           │ claim_lock=...   │  │              │
           │ claim_expires=.. │  └──────────────┘
           │ current_run_id=..│
           └────────┬─────────┘
                    │
                    ▼
           ┌──────────────────┐
           │ COMMIT           │
           │ Spawn Worker     │
           └──────────────────┘
```

### 7.4 TTL 管理

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `claim_ttl_seconds` | 900 (15 min) | 认领锁有效期 |
| `dispatcher_poll_interval` | 5s | 轮询间隔 |
| `heartbeat_extend` | 是 | 心跳自动延长 TTL |

### 7.5 重试策略

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `failure_limit` | 3 | 全局默认重试次数上限 |
| `backoff_seconds` | 0 | 当前为即时重试，可扩展为指数退避 |

---

## 8. Workspace 管理器

### 8.1 三种模式

#### Scratch（临时沙盒）

| 属性 | 说明 |
|------|------|
| **路径** | `<workspaces_root>/<task_id>/` |
| **创建** | 任务认领时创建空目录 |
| **使用** | Worker 在其中执行操作 |
| **清理** | 任务归档时删除 |
| **隔离性** | 高，每个任务独立目录 |
| **适用场景** | 短暂的计算任务、推理、分析 |

#### Dir（共享目录）

| 属性 | 说明 |
|------|------|
| **路径** | 由创建者指定（`workspace_path`） |
| **创建** | 创建者预置，Dispatcher 仅检查存在性 |
| **使用** | 多个任务共享同一目录 |
| **清理** | 不自动清理 |
| **适用场景** | 多个 Agent 协作编辑同一代码库 |

#### Worktree（Git Worktree）

| 属性 | 说明 |
|------|------|
| **路径** | `<git_root>/.git/worktrees/<task_id>/` |
| **创建** | `git worktree add <path> <branch>` |
| **使用** | Worker 在此分支上开发 |
| **清理** | `git worktree remove`（任务归档时） |
| **前提** | 项目必须在 Git 仓库中 |
| **适用场景** | 代码变更任务、PR 创建 |

### 8.2 Workspace 生命周期

```
任务创建
    │
    ▼
┌──────────────────┐
│ 未分配 Workspace │（仅 `scratch` 模式延迟分配）
│ (workspace_path  │
│  = NULL)          │
└────────┬─────────┘
         │ Claim 时分配（scratch）或验证（dir/worktree）
         ▼
┌──────────────────┐
│ Workspace 已分配  │
│ ─────────────────│
│ Worker 在其中执行  │
│ 可访问工作区路径   │
└────────┬─────────┘
         │ 任务完成/失败
         ▼
┌──────────────────┐
│ Workspace 保留    │（用于调试和审计）
│ ─────────────────│
│ 仅在归档时清理    │
└────────┬─────────┘
         │ 归档（archive）
         ▼
┌──────────────────┐
│ Workspace 清理    │
│ scratch → 删除目录│
│ worktree → remove │
│ dir → 不清理      │
└──────────────────┘
```

---

## 9. HITL 设计

### 9.1 Block/Unblock 流程

```
           ┌─────────────────────┐
           │  Worker 执行中      │
           │  (running)          │
           └──────────┬──────────┘
                      │ Worker 调用 POST /block
                      │ {"reason": "需要人工审批"}
                      ▼
           ┌─────────────────────┐
           │  blocked            │
           │  ─────────────────  │
           │  block_reason 记录   │
           │  认领锁保留          │
           └─────────────────────┘
                      │
                 ┌────┴────┐
                 │         │
           ┌─────┴──┐  ┌──┴──────┐
           │ 人工    │  │ 认领锁   │
           │ 解除阻塞│  │ 超时     │
           └────┬───┘  └──┬──────┘
                │         │
                ▼         ▼
        ┌──────────┐  ┌─────────┐
        │ running  │  │ failed  │
        │ (继续执行)│  │ (超时)   │
        └──────────┘  └─────────┘
```

### 9.2 评论系统

- **创建评论：** POST `/v2/tasks/{id}/comment`
- **查询评论：** 在 GET `/v2/tasks/{id}` 中附带回
- **作者标识：** `author` 字段表明来源（`system`/`worker`/`user` 等）
- **内容格式：** Markdown（支持富文本和代码块）
- **时间线：** 按 `created_at` 升序排列

### 9.3 通知机制

当 HITL 事件发生时，Orchestration Engine 会记录事件到 `task_events` 表。通知的消费由外部通知系统（如 Gateway Notifier）负责，Engine 本身只产生事件，不负责投递。

Block/Unblock 环境通知的事件：

| 事件 | 说明 | 预期处理 |
|------|------|---------|
| `blocked` | 任务被阻塞 | 通知 Assignee 的人工审批者 |
| `unblocked` | 阻塞解除 | 通知 Worker 可继续执行 |
| `commented` | 新评论 | 通知任务相关方 |

---

## 10. 审计日志

### 10.1 事件模型

每个事件记录都包含：

| 字段 | 说明 |
|------|------|
| **id** | 自增 ID，便于分页和流式消费 |
| **task_id** | 关联的任务 |
| **run_id** | 关联的运行记录（可空，非运行时的事件如 created 为 NULL） |
| **kind** | 事件类型枚举 |
| **payload** | JSON 格式的详细信息 |
| **created_at** | 精确到秒的时间戳 |

### 10.2 事件类型枚举

| kind | 说明 | payload 示例 |
|------|------|-------------|
| `created` | 任务创建 | `{"created_by": "user", "parents": ["t_x"]}` |
| `claimed` | 任务被认领 | `{"worker_id": "worker-1", "pid": 12345}` |
| `started` | 任务开始执行 | `{"run_id": 5, "profile": "coder-agent"}` |
| `heartbeat` | 心跳 | `{"progress": "70%", "claim_expires": 1712347200}` |
| `completed` | 任务完成 | `{"run_id": 5, "outcome": "success"}` |
| `failed` | 任务失败 | `{"run_id": 5, "error": "timeout"}` |
| `blocked` | 任务阻塞 | `{"reason": "需要审批"}` |
| `unblocked` | 阻塞解除 | `{"reason": "审批通过"}` |
| `commented` | 新评论 | `{"comment_id": 6, "author": "reviewer"}` |
| `archived` | 任务归档 | `{}` |
| `dependency_promoted` | 依赖满足提升 | `{"from": "todo", "to": "ready"}` |
| `released` | 认领锁释放（超时） | `{"run_id": 5, "reason": "ttl_expired"}` |
| `retried` | 自动重试 | `{"old_run": 5, "new_run": 6, "attempt": 2}` |

### 10.3 存储设计

- 所有事件写入 `task_events` 表
- `kind` 字段使用字符串常量便于直接查询
- `payload` 字段使用 JSON 文本（SQLite 的 JSON 函数支持原生查询: `json_extract(payload, '$.key')`）
- 复合索引 `(task_id, created_at)` 支持高效的时间线查询

### 10.4 查询接口

审计日志通过 `GET /v2/tasks/{id}` 接口返回（events 数组），暂不提供独立的事件查询端点。外部系统可通过 `./` 目录直接读取 SQLite。

---

## 11. 与现有系统的集成

### 11.1 集成原则

```
┌──────────────────────────────────────────────────────────┐
│                      aiohttp App                          │
│                                                          │
│   ┌─────────────┐   ┌─────────────┐   ┌───────────────┐ │
│   │ V1 Router    │   │ V2 Router   │   │ Static Files  │ │
│   │ /v1/agents   │   │ /v2/tasks   │   │ / (dashboard) │ │
│   │ /v1/tasks    │   │ /v2/tasks/*  │   │               │ │
│   │ /health      │   │             │   │               │ │
│   └──────┬───────┘   └──────┬──────┘   └───────────────┘ │
│          │                  │                             │
│          ▼                  ▼                             │
│   ┌─────────────┐   ┌──────────────────┐                 │
│   │RegistryHndlr│   │OrchestrationHndlr│                 │
│   └──────┬──────┘   └────────┬─────────┘                 │
│          │                  │                             │
│          ▼                  ▼                             │
│   ┌─────────────┐   ┌──────────────────┐                 │
│   │A2ARegStore  │   │ Orchestration    │                 │
│   │(JSON文件)   │   │ Engine (SQLite)  │                 │
│   └─────────────┘   └──────────────────┘                 │
│                                                          │
│   ┌────────────────────────────────────────────────────┐ │
│   │ RegistryHandler._tasks (旧任务存储)                  │ │
│   │ V1 保留，仅用于 WS Hub 任务分发兼容                  │ │
│   └────────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────────┘
```

**核心原则：**

1. **不修改现有模块：** `RegistryHandler` 和 `A2ARegistryStore` 的代码和接口不变
2. **独立 Store：** V2 使用独立的 SQLite 数据库，不与 V1 的 JSON 文件冲突
3. **独立 Handler：** Orchestration Engine 有自己的 Handler 类，不修改 `RegistryHandler`
4. **路由共存：** V1 端点和 V2 端点通过不同的路径前缀共存
5. **共享受限：** 两个系统共享日志系统、配置管理、aiohttp Application 实例

### 11.2 V1 任务存储与 V2 的关系

V1 中 `RegistryHandler._tasks` 是一个 in-memory dict，存储通过 WS Hub 分发/汇报的任务。V2 **不会**迁移这些数据。V1 的任务（WS dispatch 模式）仍由 `RegistryHandler` 管理，V2 的任务（Kanban 编排模式）由新的 Orchestration Engine 管理。

### 11.3 选型对比：JSON vs SQLite

| 维度 | V1 (JSON) | V2 (SQLite) |
|------|-----------|-------------|
| **场景** | Agent 注册与发现 | 任务编排与工作流 |
| **并发** | 单线程 + 文件锁 | WAL + `BEGIN IMMEDIATE` |
| **查询能力** | 全量加载后 Python 过滤 | SQL 条件查询 + 索引 |
| **数据完整性** | 无 | 外键约束 + 事务 |
| **数据量** | 小（Agent 数量有限） | 大（任务、运行、事件） |

---

## 12. 部署与配置

### 12.1 新增配置项

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `--board-path` | string | `<data-dir>/board.db` | SQLite 数据库路径 |
| `--dispatcher-enabled` | bool | `true` | 是否启动 Dispatcher 后台任务 |
| `--dispatcher-interval` | int | `5` | 轮询间隔（秒） |
| `--claim-ttl` | int | `900` | 认领锁默认 TTL（秒） |
| `--failure-limit` | int | `3` | 全局默认重试次数 |
| `--workspaces-root` | string | `<data-dir>/workspaces` | Scratch 工作区根目录 |

#### CLI 更新

在现有 `cli.py` 基础上新增参数：

```python
parser.add_argument("--board-path", help="SQLite 数据库路径")
parser.add_argument("--dispatcher-enabled", action="store_true", default=True)
parser.add_argument("--dispatcher-interval", type=int, default=5)
parser.add_argument("--claim-ttl", type=int, default=900)
parser.add_argument("--failure-limit", type=int, default=3)
parser.add_argument("--workspaces-root", help="工作区根目录")
```

#### Python 包依赖

| 包 | 版本 | 用途 |
|----|------|------|
| `aiohttp` | >=3.9 | HTTP 服务器（已存在） |
| `aiohttp-cors` | >=0.7 | CORS 支持（已存在） |
| 无需新增外部依赖 | — | SQLite 为 Python 标准库 |

### 12.2 启动方式

**完整模式（V1 + V2 Orchestration Engine）：**（默认配置）

```bash
a2a-registry --host 0.0.0.0 --port 8321 --dispatcher-enabled true
```

**仅 Registry 模式（V1 兼容，不启动 Orchestration Engine）：**

```bash
a2a-registry --host 0.0.0.0 --port 8321 --dispatcher-enabled false
```

### 12.3 目录结构变化

```
~/.simple-a2a-registry/          # 数据根目录
├── registry.json                 # V1 Agent 注册数据（不变）
├── board.db                     # V2 SQLite 数据库（新增）
├── board.db-wal                 # SQLite WAL 文件
├── board.db-shm                 # SQLite 共享内存文件
└── workspaces/                  # Scratch 工作区根目录（新增）
    ├── t_a1b2c3d4/              # 每个任务一个目录
    ├── t_e5f6g7h8/
    └── ...
```

---

## 13. 测试策略

### 13.1 测试层次

```
              ┌─────────────────────┐
              │   E2E 测试           │
              │  (完整生命周期测试)   │
              └──────────┬──────────┘
                         │
              ┌──────────┴──────────┐
              │  集成测试            │
              │  (API + Dispatcher) │
              └──────────┬──────────┘
                         │
              ┌──────────┴──────────┐
              │  单元测试            │
              │  (模块级隔离测试)    │
              └─────────────────────┘
```

### 13.2 单元测试（使用现有 pytest + asyncio_mode=auto 配置）

| 模块 | 测试内容 | 关键场景 |
|------|---------|---------|
| **Task Store** | SQLite CRUD | 创建任务、查询过滤、原子 Claim SQL、批处理更新 |
| **State Machine** | 状态转换逻辑 | 合法转换、非法转换、循环检测、并发安全 |
| **Dependency Engine** | 依赖链解析 | 多 parent、扇出扇入、环检测、级联提升 |
| **Dispatcher** | Claim/Heartbeat/TTL | 原子 Claim 争抢、超时释放、重试计数 |
| **Workspace Manager** | 三种模式 | Scratch 创建/清理、Dir 验证、Worktree 创建/清理 |
| **HITL** | Block/Unblock | 阻塞·解禁·TLL 超时、评论读写 |

### 13.3 集成测试

| 场景 | 步骤 |
|------|------|
| **创建带依赖的任务链** | POST 创建 t_A → 创建 t_B (parent=t_A) → t_B 状态为 todo → POST complete t_A → GET t_B 状态变为 ready |
| **并发 Claim 竞争** | 2 个 Worker 同时 claim 同一个 ready 任务 → 仅 1 个成功（409） |
| **TTL 超时释放** | Claim 任务 → 等待 TTL 过期 → 检查任务状态变更为 failed |
| **重试流程** | 创建任务 → Claim → 标记失败 → 检查自动转为 ready → 重试 |
| **Block/Unblock** | 创建任务 → Claim → Block → Task status=blocked → Unblock → Task status=running |
| **Workspace 清理** | 创建 scratch 任务 → Claim → Workspace 目录存在 → Complete → Archive → 目录已删除 |
| **V1/V2 共存** | 使用 V1 注册 Agent → 使用 V2 创建任务 → 两者正常，互不干扰 |

### 13.4 E2E 测试

| 测试 | 描述 |
|------|------|
| **完整 Pipeline** | 创建多阶段任务链 → Dispatcher 自动派发 → 各阶段 Worker 依次执行 → 全部完成后验证结果 |
| **故障恢复** | Worker 中途崩溃 → Dispatcher 检测到超时 → 重试 → 成功完成 |
| **HITL 插入** | Worker 执行中触发 Block → 人工添加评论 → Unblock → Worker 继续 → 完成 |

### 13.5 测试工具建议

- 使用 `pytest-asyncio` 测试异步 Dispatcher 循环
- 使用 `pytest-xdist` 模拟并发 Claim 竞争（可选）
- 使用 `aiohttp.test_utils` 进行 API 端点测试
- 使用内存 SQLite（`:memory:`）加速单元测试

---

## 14. 实施路线图

### Phase 1 — 核心数据层（预计 1 周）

**交付物：**
- [ ] `orchestration/store.py` — SQLite Task Store 实现（CRUD + 查询）
- [ ] `orchestration/models.py` — Task, Run, Comment 等内部数据类
- [ ] `orchestration/state_machine.py` — 状态转换逻辑 + 验证器
- [ ] 单元测试覆盖

**交付标准：** 可通过 Python 直接调用创建和查询任务，状态转换符合定义。

### Phase 2 — API + 依赖链（预计 1 周）

**交付物：**
- [ ] `orchestration/routes.py` — V2 REST API Handler
- [ ] `orchestration/dependency.py` — 依赖链解析引擎（含环检测）
- [ ] 集成到 `create_app()`，V1/V2 路由共存
- [ ] API 集成测试

**交付标准：** 可通过 HTTP 调用创建带依赖的任务，子任务自动提升。

### Phase 3 — Dispatcher + Workspace（预计 1 周）

**交付物：**
- [ ] `orchestration/dispatcher.py` — Poll Loop + Claim + TTL + Retry
- [ ] `orchestration/workspace.py` — Workspace Manager（三种模式）
- [ ] Dispatcher 后台 asyncio 任务（`on_startup` 钩子）
- [ ] 集成测试：Claim 竞争、TTL 超时、重试

**交付标准：** Dispatcher 能自动认领 `ready` 任务并分配 Workspace。

### Phase 4 — HITL + 审计（预计 3 天）

**交付物：**
- [ ] Block/Unblock API 实现
- [ ] 评论系统
- [ ] `task_events` 事件记录
- [ ] Task Detail API 增强（含依赖链、运行历史、事件流）

**交付标准：** 完整的 Block/Unblock 流程可用，每次操作有审计记录。

### Phase 5 — 打磨与文档（预计 3 天）

**交付物：**
- [ ] CLI 参数扩展
- [ ] E2E 测试
- [ ] API 文档更新
- [ ] 异常处理增强
- [ ] 性能基准测试

**交付标准：** 功能完整，文档齐全，可投入生产使用。

### 总体时间线

```
Week 1: ████████████  Phase 1 — 核心数据层
Week 2: ████████████  Phase 2 — API + 依赖链
Week 3: ████████████  Phase 3 — Dispatcher + Workspace
Week 4: ████████      Phase 4 — HITL + 审计
Week 5: ████████      Phase 5 — 打磨与文档
```

---

## 14. 认证与安全

### 14.1 概况

> 本节描述 V2 Orchestration Engine 在 OAuth 2.1 认证环境下的行为。
> 完整的认证架构设计请参见 [docs/architecture.md](architecture.md#oauth-21-认证架构)。
>
> **方案C（Admin 预创建模式）**：Agent 不再通过注册自动获取 OAuth 客户端。Admin 通过 `/admin/clients` 端点或 Web UI 预创建客户端，再通过安全渠道将凭证分发给 Agent。Agent 凭预分配的 client_id/client_secret 获取 Token 后注册。`POST /v1/agents` 注册端点受 `agent:register` scope 保护。

当 `--auth-enabled true` 时，所有 V2 API 端点受 OAuth 2.1 中间件保护。Worker 进程在派发时通过环境变量获取认证上下文。

### 14.2 认证中间件覆盖

| 路径 | 认证 | 说明 |
|------|------|------|
| `/v2/tasks` 及子路径 | ✅ `task:read` / `task:write` | 所有 V2 编排端点 |
| `/v1/agents` | ✅ `agent:read` / `agent:register` / `agent:admin` | V1 Agent 管理（方案C：注册端点受 `agent:register` 保护） |
| `/v1/agents/{agent_id}/heartbeat` | ✅ `agent:read` | 心跳保活 |
| `/v1/agents/{agent_id}/ws` | ✅ `agent:read` | WebSocket 长连接 |
| `/v1/tasks` | ✅ `task:read` | V1 任务查询 |
| `/v1/agents/{agent_id}/dispatch` | ✅ `task:write` | 任务分发 |
| `/admin/clients` | ✅ `registry:admin` | Admin 客户端管理 |
| `/auth/*` | ❌ 公开 | Token 端点/注册 |
| `/.well-known/*` | ❌ 公开 | Discovery 端点 |
| `/health` | ❌ 公开 | 健康检查 |

### 14.3 203 状态码与 Token 认证

V1 心跳端点 `POST /v1/agents/{agent_id}/heartbeat` 在认证启用后：

- **请求**：需携带 `Authorization: Bearer <token>`（scope: `agent:read`）
- **响应**：成功时仍返回 `203 Non-Authoritative Information`
- **语义**：203 表明 Registry 转发了 Agent 自身的状态声明（"alive"），而非 Registry 独立验证的结果。认证中间件验证的是调用方身份，不影响心跳的 203 语义。

### 14.4 Dispatcher 的认证模式

Dispatcher 作为内部组件，在认证启用时采用以下模式工作：

1. **内部绕过**：Dispatcher 是 Registry 内部的 asyncio 任务，不经过 HTTP 认证中间件，直接调用 Store 方法
2. **Worker 认证**：Dispatcher 生成的 Worker 子进程通过环境变量 `HERMES_AUTH_TOKEN` 获取预先签发的 JWT，在被调度调用 `/v2/` 端点时附带 `Authorization: Bearer` header
3. **Token 发放**：Worker Token 由 Dispatcher 在 claim 时生成，包含该 Worker 所需的 scope

### 14.5 安全边界

| 安全关注点 | 措施 |
|-----------|------|
| **端点认证** | AuthMiddleware 拦截所有受保护端点 |
| **Scope 隔离** | Worker Token 只含该 Worker 所需的最小 scope |
| **Token 泄露** | JWT 自带过期时间，短生命周期（默认 1 小时） |
| **Claim 锁定** | 同一 Worker 使用 claim_lock 防双重派发 |
| **Workspace 隔离** | 每个任务独立目录，scratch 模式归档即删除 |
| **配置开关** | `--auth-enabled` 一键切换，开发模式零认证开销 |

---

> **附录 A：Hermes Kanban 对标参考**
>
> | 能力 | Hermes Kanban | 本系统 | 备注 |
> |------|---------------|--------|------|
> | 任务生命周期 | todo→ready→claimed→running→completed/blocked/failed/archived | 一致 | — |
> | 依赖链 | parent/child，多父依赖，自动提升 | 一致 | — |
> | 原子 Claim | UPDATE WHERE status=ready | 一致 | 核心模式 |
> | Worker 派发 | 轮询 + spawn profile 子进程 | 一致 | 可配置 Profile |
> | TTL 超时 | claim 超时自动释放回 ready | 释放为 failed | 微调为符合编排语义 |
> | 重试 | failure_limit，自动重试 | 一致 | — |
> | Workspace | scratch/dir/worktree 三种模式 | 一致 | — |
> | 任务心跳 | 长时间任务定期报进度 | 一致 | — |
> | Block/Unblock | 人工介入阻塞解除 | 一致 | — |
> | 评论线程 | 持久化注释 | 一致 | — |
> | 审计日志 | 每次 run 的 outcome/summary/error | task_events 事件流 | 更精细 |
> | HITL 通知 | 阻塞时推送消息通道 | 产生事件，消费方订阅 | 解耦设计 |
> | 看板切换 | 多 board，命名空间隔离 | tenant 字段隔离 | 简化版 |
>
> **附录 B：文件清单（新增/修改）**
>
> | 文件 | 类型 | 说明 |
> |------|------|------|
> | `simple_a2a_registry/orchestration/__init__.py` | 新增 | 模块入口 |
> | `simple_a2a_registry/orchestration/store.py` | 新增 | SQLite Task Store |
> | `simple_a2a_registry/orchestration/models.py` | 新增 | 内部数据模型 |
> | `simple_a2a_registry/orchestration/state_machine.py` | 新增 | 状态机 |
> | `simple_a2a_registry/orchestration/dependency.py` | 新增 | 依赖链引擎 |
> | `simple_a2a_registry/orchestration/dispatcher.py` | 新增 | Worker 派发器 |
> | `simple_a2a_registry/orchestration/workspace.py` | 新增 | Workspace 管理器 |
> | `simple_a2a_registry/orchestration/routes.py` | 新增 | V2 REST API |
> | `simple_a2a_registry/server.py` | 修改 | 集成 V2 路由和 Dispatcher |
> | `simple_a2a_registry/cli.py` | 修改 | 新增 V2 配置参数 |
