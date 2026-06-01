# Swarm 拓扑迁移方案 — 从 Hermes Kanban Swarm 到 simple-a2a-registry

> 版本：v1
> 日期：2026-05-30
> 作者：product-manager
> 前置依赖：Hermes Kanban Swarm v1 学习笔记（t_cc6c3d42）

---

## 1. 背景与目标

### 1.1 什么是 Kanban Swarm

Hermes Kanban Swarm v1 是一种**并行多 Agent 协作拓扑**，通过 Kanban 依赖引擎实现任务流的分发与汇聚：

```
规划根任务（立即完成，作为黑板/公告板）
  ├── Worker-1（并行，独立就绪）
  ├── Worker-2（并行，独立就绪）
  ├── Worker-N（并行，独立就绪）
  └── Verifier（待所有 Worker 完成 → 就绪）
       └── Synthesizer（待 Verifier 完成 → 就绪）
```

核心机制：

| 机制 | 实现方式 |
|------|----------|
| 并行 Worker | 多个独立任务，无互相依赖 |
| 共享黑板 | Swarm Root 任务的 JSON 结构体评论（`[swarm:blackboard]` 前缀） |
| Verifier 门控 | Worker 完成 → Verifier 触发；Verifier 用 `metadata.gate="pass"` 放行 |
| Synthesizer 汇聚 | Verifier 完成 → Synthesizer 触发，汇总所有输出 |
| Skills 注入 | `--skills` 参数强制加载技能包 |

### 1.2 迁移目标

将上述 Swarm 拓扑能力以**最小侵入**方式接入 simple-a2a-registry，使其：

1. **复用现有 v2/tasks 核心**（创建/依赖/认领/完成/评论/审计）
2. **不引入第二调度器** — 沿用现有 Dispatcher 的 WS/Pool/Poll 派发优先级
3. **不修改现有数据模型** — 只在语义层面约定 JSON 评论前缀和 metadata 门控协议
4. **向后兼容** — 现有 v2/tasks 端点无破坏性变更

### 1.3 设计原则

| 原则 | 说明 |
|------|------|
| **最小依赖** | 不修改核心 TaskStore、状态机、Dispatcher 逻辑 |
| **约定优于配置** | 通过 JSON 评论前缀约定实现黑板，通过 metadata 字段约定实现门控 |
| **API 即语法糖** | Swarm 是任务拓扑的"快捷创建"语义，底层仍是标准 v2/tasks 端点 |
| **渐进可用** | P1 实现基本创建+黑板，P2 实现门控+synthesizer，逐步开放 |

---

## 2. 架构设计

### 2.1 总体架构

```
┌─────────────────────────────────────────────────────────────────┐
│                      simple-a2a-registry                          │
│                                                                   │
│  ┌──────────────────┐  ┌──────────────────┐  ┌───────────────┐  │
│  │     V1 API        │  │     V2 API        │  │  NEW: V2 API  │  │
│  │  Agent 注册/发现   │  │  任务 CRUD/依赖    │  │  /v2/swarm    │  │
│  │  WebSocket Hub    │  │  Claim/Complete   │  │  · POST       │  │
│  │  Heartbeat        │  │  Block/Unblock    │  │  · GET        │  │
│  └──────────────────┘  │  Comment           │  │  · Comment    │  │
│                        └────────┬──────────┘  └───────┬───────┘  │
│                                 │                       │          │
│                                 ▼                       ▼          │
│  ┌──────────────────────────────────────────────────────────────┐ │
│  │                    Orchestration Engine                       │ │
│  │  ┌──────────────┐  ┌──────────────┐  ┌────────────────────┐  │ │
│  │  │  TaskStore    │  │  Dependency   │  │  Dispatcher        │  │ │
│  │  │  (5 表 SQLite)│  │  Resolution   │  │  · TTL Release     │  │ │
│  │  │               │  │  · Auto-Promo │  │  · Retry Promote   │  │ │
│  │  └──────────────┘  │  · Cycle Det   │  │  · WS/Pool/Spawn   │  │ │
│  │                     └──────────────┘  └────────────────────┘  │ │
│  └──────────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────┘
```

### 2.2 核心思路：零侵入复用

simple-a2a-registry 的 v2/tasks 端点**已经具备 Swarm 所需的全部原语**：

| Swarm 能力 | Registry 已有原语 | 复用方式 |
|------------|-------------------|----------|
| 多 Worker 并行 | `POST /v2/tasks` 不设 parents → 立即 ready | 直接复用 |
| 依赖链 | `parents` 参数 + `_resolve_dependencies` 自动提升 | 直接复用 |
| 评论/黑板 | `POST /v2/tasks/{id}/comment` | 约定 JSON 前缀 |
| Verifier 门控 | `POST /v2/tasks/{id}/complete` 传 `metadata` | 约定 gate 语义 |
| Profile/Agent 分配 | `assignee` 字段 | 映射到 WS/Pool/Poll 派发 |

> **结论：不需要新调度器、不需要新数据库表、不需要改状态机。**

---

## 3. API 设计

### 3.1 新增端点：POST /v2/swarm

创建完整的 Swarm 拓扑。内部复用 `POST /v2/tasks` 逻辑。

**请求体：**

```json
{
  "goal": "市场趋势调研并输出报告",
  "workers": [
    {
      "profile": "researcher-a",
      "title": "调研：宏观经济趋势",
      "body": "分析 GDP、利率、通胀趋势",
      "skills": ["web-search"]
    },
    {
      "profile": "researcher-b",
      "title": "调研：行业竞争对手",
      "body": "分析竞品动态和市场份额",
      "skills": ["web-search"]
    }
  ],
  "verifier": {
    "profile": "reviewer-agent",
    "title": "审核：校验调研结果"
  },
  "synthesizer": {
    "profile": "writer-agent",
    "title": "报告生成：综合调研结果"
  },
  "root_title": "Swarm: 市场趋势调研",
  "priority": 0,
  "tenant": "my-org"
}
```

**响应体（201 Created）：**

```json
{
  "swarm": {
    "root_id": "t_abc12345",
    "worker_ids": ["t_abc12346", "t_abc12347"],
    "verifier_id": "t_abc12348",
    "synthesizer_id": "t_abc12349"
  },
  "topology": {
    "root": {"id": "t_abc12345", "status": "completed"},
    "workers": [
      {"id": "t_abc12346", "status": "ready", "assignee": "researcher-a"},
      {"id": "t_abc12347", "status": "ready", "assignee": "researcher-b"}
    ],
    "verifier": {"id": "t_abc12348", "status": "todo", "assignee": "reviewer-agent"},
    "synthesizer": {"id": "t_abc12349", "status": "todo", "assignee": "writer-agent"}
  }
}
```

**实现流程：**

```
POST /v2/swarm
  │
  ├─ 1. 校验请求（至少 1 worker，必填 profile/title）
  │
  ├─ 2. 创建 Root 任务（assignee: "swarm-orchestrator"）
  │     └─ 立即 complete，metadata = {kind: "kanban_swarm_v1", goal, ...}
  │     └─ 写入黑板：[swarm:blackboard]{"key":"topology","value":{...}}
  │
  ├─ 3. 创建 N 个 Worker 任务（parents: [root_id]）
  │     └─ 状态：ready（root 已 done，依赖解析后自动 ready）
  │     └─ body 追加 Swarm 协议说明 + goal 上下文
  │
  ├─ 4. 创建 Verifier 任务（parents: [所有 worker_ids]）
  │     └─ 状态：todo（等待所有 worker 完成）
  │     └─ body 包含门控协议说明
  │
  ├─ 5. 创建 Synthesizer 任务（parents: [verifier_id]）
  │     └─ 状态：todo（等待 verifier 完成）
  │     └─ body 包含汇总说明
  │
  └─ 6. 返回拓扑信息
```

**Swarm 上下文注入（body 后缀）：**

每个 Worker、Verifier、Synthesizer 的 body 自动追加以下上下文：

```
## Swarm 协议
- Swarm 根任务 / 共享黑板：`t_abc12345`
- 所有 Worker 并行执行。通过根任务的结构化评论分享中间成果
- 将机器可读的结构化信息放在完成（complete）的 metadata 中
- 将跨 Worker 的备注用结构化评论（[swarm:blackboard]JSON）放在根任务上
- 目标：[goal 原文]
```

### 3.2 新增端点：GET /v2/swarm/{root_id}

获取 Swarm 拓扑的完整状态：

```json
{
  "swarm": {
    "root_id": "t_abc12345",
    "status": "completed",
    "worker_ids": ["t_abc12346", "t_abc12347"],
    "verifier_id": "t_abc12348",
    "synthesizer_id": "t_abc12349"
  },
  "workers": [
    {"id": "t_abc12346", "status": "completed", "assignee": "researcher-a"},
    {"id": "t_abc12347", "status": "running", "assignee": "researcher-b"}
  ],
  "verifier": {"id": "t_abc12348", "status": "todo", "assignee": "reviewer-agent"},
  "synthesizer": {"id": "t_abc12349", "status": "todo", "assignee": "writer-agent"},
  "blackboard": {
    "topology": {"goal": "...", "worker_ids": [...]},
    "phase1_result": {"summary": "宏观经济方面..."},
    "_authors": {
      "topology": "swarm-orchestrator",
      "phase1_result": "researcher-a"
    }
  }
}
```

### 3.3 新增端点：POST /v2/swarm/{root_id}/comment

向 Swarm 黑板写结构化评论。等价于 `POST /v2/tasks/{root_id}/comment` 但自动添加 `[swarm:blackboard]` 前缀。

```json
// 请求体
{
  "author": "researcher-a",
  "key": "phase1_result",
  "value": {"summary": "宏观经济方面..."}
}

// 实际写入 task_comments 的 body
"[swarm:blackboard] {\"key\": \"phase1_result\", \"value\": {\"summary\": \"宏观经济方面...\"}}"
```

### 3.4 新增端点：GET /v2/swarm/{root_id}/blackboard

读取 Swarm 黑板汇总。按 key 聚合所有 `[swarm:blackboard]` 评论，后写覆盖前写，含作者追溯。

### 3.5 Swarm 端点路由表

| 方法 | 路径 | Scope | 说明 |
|------|------|-------|------|
| POST | `/v2/swarm` | task:write | 创建 Swarm 拓扑 |
| GET | `/v2/swarm/{root_id}` | task:read | 获取 Swarm 状态 |
| POST | `/v2/swarm/{root_id}/comment` | task:write | 写入黑板（结构化） |
| GET | `/v2/swarm/{root_id}/blackboard` | task:read | 读取黑板汇总 |

---

## 4. 数据模型与约定

### 4.1 共享黑板（Blackboard）约定

利用 `task_comments.body` 字段，通过前缀标记区分普通评论与黑板更新。

**前缀：** `[swarm:blackboard]`

**格式：**

```
[swarm:blackboard]{"key":"topology","value":{"goal":"...","worker_ids":[...]}}
```

**聚合规则：**
- 收集所有以 `[swarm:blackboard]` 开头的评论
- JSON 解析前缀后的正文
- 按 `key` 合并：后写覆盖前写
- 记录每个 key 的 `_authors` 供审计追溯

**对比 Hermes 原始实现：**

| 维度 | Hermes Swarm | Registry Swarm |
|------|-------------|----------------|
| 前缀 | `[swarm:blackboard] `（末尾有空格） | `[swarm:blackboard]`（无尾空格，兼容） |
| 载荷格式 | `{"key": "...", "value": ...}` | 同上，完全一致 |
| API 入口 | `kanban_swarm.post_blackboard_update()` | `POST /v2/swarm/{root_id}/comment` |
| 读取入口 | `kanban_swarm.latest_blackboard()` | `GET /v2/swarm/{root_id}/blackboard` |

> **注意：Hermes 的 blackboard 前缀末尾带一个空格**（`"[swarm:blackboard] "` = `BLACKBOARD_PREFIX`）。Registry 实现应当**同时兼容**有尾空格和无尾空格两种格式，以确保 Hermes 创建的任务能在 Registry 中正确读取黑板，反之亦然。

### 4.2 Verifier 门控协议

Verifier 任务完成时通过 `metadata.gate` 字段控制 Swarm 是否放行：

```json
// Verifier 调用 POST /v2/tasks/{verifier_id}/complete 时的 body
{
  "claim_lock": "...",
  "metadata": {"gate": "pass"}
}
```

| `metadata.gate` 值 | 行为 |
|--------------------|------|
| `"pass"` | 验证通过，Synthesizer 自动提升为 ready |
| `"fail"` | 验证失败，Synthesizer 保持 todo，Verifier 应通过评论说明原因。需要人工介入 Unblock 或重新调度 |
| `"revise"` | 需要修订，Verifier block 自身并说明需要修订的内容 |

> 注意：门控是**语义约定**而非强制机制。Registry 的状态机不会自动阻止 Synthesizer 的依赖解析——Verifier 完成（status=completed）即触发 Synthesizer 的 todo→ready 提升。因此：
> - 如果 Verifier 希望阻止 Synthesizer，应当在验证不通过时**阻塞自身**（`POST /v2/tasks/{id}/block`）而非完成
> - 只有真正批准时才完成（同时传 `metadata.gate="pass"`）

这与 Hermes 原始实现的语义一致。

### 4.3 Skills 参数映射

Hermes Swarm 的 `--skills` 强制加载技能包。在 Registry 中，skills 参数附加到任务 body 末尾，作为 Worker 执行时的上下文指引：

```json
{
  "profile": "researcher-a",
  "title": "调研：宏观经济",
  "skills": ["web-search", "data-analysis"]
}
```

Worker 被派发时（通过 WS 消息或池子），skills 信息随任务消息一起发送。Worker 端根据 skills 列表决定加载哪些工具集。

---

## 5. 任务生命周期与派发

### 5.1 从创建到完成的完整流程

```
时间线
│
├── [t=0]  Client → POST /v2/swarm → 创建 5 个任务
│           Root(completed), W1(ready), W2(ready), Verifier(todo), Synth(todo)
│
├── [t=5]  Dispatcher 轮询，发现 W1/ready → 通过 WS 推送给 researcher-a
│           W2/ready → 通过 WS 推送给 researcher-b
│
├── [t=10] W1 running, W2 running
│           W1 完成调研，将中间结果写入黑板：POST /v2/swarm/root/comment
│
├── [t=20] W2 完成调研 → POST /v2/tasks/w2/complete
│
├── [t=21] W1 完成调研 → POST /v2/tasks/w1/complete
│          依赖引擎检测到 W1 和 W2 均 completed
│          → Verifier 从 todo 提升为 ready
│
├── [t=25] Dispatcher 轮询 → 通过 WS 推送给 reviewer-agent
│
├── [t=35] Verifier 审核通过 → POST /v2/tasks/verifier/complete
│          依赖引擎检测到 verifier completed
│          → Synthesizer 从 todo 提升为 ready
│
├── [t=40] Dispatcher 轮询 → 通过 WS 推送给 writer-agent
│
└── [t=60] Synthesizer 完成汇总报告 → 任务全部完成
```

### 5.2 派发优先级（复用现有 Dispatcher 逻辑）

Registry 现有 Dispatcher 已经定义了派发优先级：

| 优先级 | 条件 | 行为 |
|--------|------|------|
| P1 | assignee 有 WS 连接 | 通过 WebSocket 推送任务消息 |
| P1.5 | assignee 在 pool_assignees 中 | 通过 SubprocessPoolManager 派发 |
| P2 | assignee 无 WS 连接 | 阻塞任务，等待外部 Polling Worker |
| P3 | 配置了 worker_command | 通过 legacy worker_command 派发 |

Swarm 创建的任务**完全复用这套派发优先级逻辑**，无需修改。

### 5.3 Worker 端行为

通过 WS 或 Pool 接收到 Swarm 任务的 Worker 应当：

1. 认领任务（`POST /v2/tasks/{id}/claim`）
2. 读取任务 body（含 Swarm 上下文、黑板 root_id、goal）
3. 可选：从黑板读取其他 Worker 的中间结果
4. 执行任务
5. 可选：将中间结果写入黑板（`POST /v2/swarm/{root_id}/comment`）
6. 完成任务（`POST /v2/tasks/{id}/complete`），附上 structured metadata

Verifier Worker 额外：
1. 读取所有 Worker 的完成结果（通过 GET /v2/swarm/{root_id} 获取状态）
2. 读取黑板上的所有中间结果
3. 判断是否通过
4. 通过 → complete（metadata.gate="pass"）；不通过 → block

---

## 6. 文件变更清单

以下为具体的代码变更：

### 6.1 新增文件

| 文件 | 说明 |
|------|------|
| `simple_a2a_registry/orchestration/swarm.py` | Swarm 拓扑创建/黑板/查询核心逻辑（约 200 行） |
| `simple_a2a_registry/orchestration/swarm_routes.py` | Swarm REST API 路由处理器（约 150 行） |

### 6.2 修改文件

| 文件 | 变更内容 | 估算行 |
|------|---------|--------|
| `simple_a2a_registry/orchestration/__init__.py` | 导出 Swarm 类、路由注册函数 | +5 行 |
| `simple_a2a_registry/orchestration/routes.py` | `register_v2_routes()` 添加 Swarm 路由 | +10 行 |
| `simple_a2a_registry/server.py` | 注入 SwarmHandler 并注册路由 | +8 行 |
| `docs/architecture.md` | 更新架构图，增加 Swarm 说明 | +30 行 |

### 6.3 不变更的文件

| 文件 | 理由 |
|------|------|
| `models.py` | AgentCard 模型不变 |
| `orchestration/models.py` | Task/TaskRun/TaskComment 模型完全足够 |
| `orchestration/store.py` | TaskStore 方法完全满足 Swarm 需求 |
| `orchestration/state_machine.py` | 状态机逻辑不变 |
| `orchestration/dispatcher.py` | 派发逻辑不变，复用现有优先级 |
| `orchestration/pool.py` | Pool 逻辑不变 |
| `orchestration/workspace.py` | Workspace 机制不变 |
| `store.py` | Agent Store 不变 |
| 数据库 schema | 不新增表，不修改现有表 |

---

## 7. 安全性分析

### 7.1 Scope 鉴权

| 端点 | scope | 说明 |
|------|-------|------|
| POST /v2/swarm | task:write | 创建任务拓扑 |
| GET /v2/swarm/{root_id} | task:read | 读取 Swarm 状态和黑板 |
| POST /v2/swarm/{root_id}/comment | task:write | 写入黑板 |
| GET /v2/swarm/{root_id}/blackboard | task:read | 读取黑板汇总 |

与现有 `/v2/tasks` 端点使用相同的 scope 体系，无需新增 scope。

### 7.2 黑板数据验证

- 写入黑板时验证 JSON 合法性（body 的 `[swarm:blackboard]` 前缀之后必须是合法 JSON）
- `key` 和 `value` 字段缺失时返回 400
- 黑板读取按 tenant 隔离（复用 TaskStore 已有的 tenant 过滤）

### 7.3 拓扑完整性校验

- POST /v2/swarm 校验至少 1 个 worker
- 每个 worker 必须有 profile 和 title
- verifier 和 synthesizer 必须有 profile
- 不校验 profile 是否存在（由 Dispatcher 的 WS/Pool 机制兜底——无对应 agent 时自动 block）

---

## 8. 风险评估与缓解

| 风险 | 影响 | 概率 | 缓解 |
|------|------|------|------|
| 大量 Worker 并行造成资源竞争 | 高 | 中 | 依赖 Dispatcher 的 claim 原子性（UPDATE ... WHERE status=ready），天然防双重派发 |
| 黑板评论过多导致性能下降 | 低 | 低 | 每次读取按时间汇总，`_authors` 仅保留最新作者；大数据量时可加 LIMIT |
| Verifier 误完成（gate 缺失）导致 Synthesizer 错误触发 | 中 | 中 | 文档约定：Verifier 应当在自己不同意时 block 而非 complete。可考虑在 synthesizer body 强提醒 |
| Hermes 创建的任务与 Registry 的黑板前缀差异 | 低 | 低 | 黑板读取时兼容 `[swarm:blackboard] `（有尾空格）和 `[swarm:blackboard]`（无尾空格）两种 |
| 跨 Registry 部署的黑板不兼容 | 低 | 低 | 黑板机制纯约定，无跨部署依赖 |
| Worker 未实现黑板协议，忽略结构化评论 | 中 | 低 | Worker 端的责任——Swarm 上下文已在 body 中说明。Verifier 作为质量关卡 |

---

## 9. 实施计划（4 阶段，估算 5 人天）

### Phase 1：核心能力（2 人天）

| 任务 | 估算（人天） | 交付物 |
|------|-------------|--------|
| `swarm.py` — `create_swarm()` 核心逻辑（创建 5 卡拓扑） | 1 | 可用的单元测试 |
| `swarm.py` — 黑板读写（`post_blackboard_update()`, `latest_blackboard()`） | 0.5 | 黑板测试 |
| `swarm_routes.py` — `POST /v2/swarm` + `GET /v2/swarm/{root_id}` | 0.5 | 集成测试 |

### Phase 2：黑板与查询（1 人天）

| 任务 | 估算（人天） | 交付物 |
|------|-------------|--------|
| `POST /v2/swarm/{root_id}/comment` — 结构化黑板写入 | 0.5 | 集成测试 |
| `GET /v2/swarm/{root_id}/blackboard` — 黑板聚合读取 | 0.5 | 集成测试（含后写覆盖、多作者） |

### Phase 3：端到端测试与文档（1.5 人天）

| 任务 | 估算（人天） | 交付物 |
|------|-------------|--------|
| 端到端测试：创建 Swarm → Worker 并行完成 → Verifier 门控 → Synthesizer 汇总 | 1 | E2E 测试脚本 |
| 更新 `docs/architecture.md`、补充 `docs/API.md` 的 Swarm API 部分 | 0.5 | 文档更新 |

### Phase 4：A2A 协议扩展（0.5 人天，可选）

| 任务 | 估算（人天） | 交付物 |
|------|-------------|--------|
| 在 Registry 的 Agent Card 中增加 `swarm` skill 声明 | 0.25 | Agent Card 更新 |
| 可选：通过 WS 推送 Swarm 状态变更通知 | 0.25 | WS 消息类型文档 |

### 总计：5 人天（Phase 1-3 必选，Phase 4 可选）

---

## 10. 与 Hermes Swarm 的差异对比

| 维度 | Hermes Kanban Swarm | Registry Swarm（迁移后） |
|------|---------------------|------------------------|
| **创建方式** | `hermes kanban swarm` CLI，直接写 SQLite | `POST /v2/swarm` REST API |
| **依赖引擎** | 复用 Kanban 依赖引擎 | 复用 Registry 的 `parents` + `_resolve_dependencies` |
| **黑板机制** | `kanban_swarm.py` 的 `post_blackboard_update()` / `latest_blackboard()` | `POST /v2/swarm/{root_id}/comment` + `GET /v2/swarm/{root_id}/blackboard` |
| **Worker 派发** | Hermes Dispatcher（profile → spawn） | Registry Dispatcher（assignee → WS/Pool/Poll） |
| **Skills 管理** | `--skills` 注入到 Worker session | `skills` 元数据随任务消息传递 |
| **门控协议** | `metadata={"gate": "pass"}` | 同上，语义完全兼容 |
| **可观测性** | Hermes Dashboard + 事件表 | Registry 的事件表 + Admin UI |
| **幂等性** | 通过 `idempotency_key` | 通过 existing 拓扑检测（读取黑板的 `topology` key） |
| **评论前缀** | `[swarm:blackboard] `（有尾空格） | 兼容两种形式 |

---

## 11. 附录：swarm.py 核心接口设计

```python
"""Swarm topology management — create, query, blackboard."""

@dataclass(frozen=True)
class SwarmWorkerSpec:
    profile: str
    title: str
    body: str = ""
    skills: list[str] = field(default_factory=list)
    priority: int = 0
    max_runtime_seconds: Optional[int] = None

@dataclass(frozen=True)
class SwarmCreated:
    root_id: str
    worker_ids: list[str]
    verifier_id: str
    synthesizer_id: str

def create_swarm(
    store: TaskStore,
    *,
    goal: str,
    workers: list[SwarmWorkerSpec],
    verifier_profile: str,
    synthesizer_profile: str,
    root_title: Optional[str] = None,
    verifier_title: str = "Verify swarm outputs",
    synthesizer_title: str = "Synthesize swarm outputs",
    tenant: Optional[str] = None,
    created_by: str = "swarm-orchestrator",
    priority: int = 0,
) -> SwarmCreated:
    """
    创建 Swarm 拓扑。
    1. 创建 Root 并立即 complete（metadata=swarm_v1）
    2. 创建 Workers（parents=[root]）
    3. 创建 Verifier（parents=worker_ids）
    4. 创建 Synthesizer（parents=[verifier]）
    5. 写入黑板拓扑信息
    """

def post_blackboard(
    store: TaskStore,
    root_id: str,
    *,
    author: str,
    key: str,
    value: Any,
) -> TaskComment:
    """向 Swarm 黑板写入 key→value 更新（通过 [swarm:blackboard] 前缀）"""

def read_blackboard(
    store: TaskStore,
    root_id: str,
) -> dict:
    """读取 Swarm 黑板，聚合所有 [swarm:blackboard] 评论，后写覆盖前写"""
```

---

## 附录 A：blackboard 前缀兼容性说明

Hermes 源码定义：
```python
BLACKBOARD_PREFIX = "[swarm:blackboard] "  # 含尾随空格
```

Registry 实现应当使用**相同的字符串常量**。读取黑板时使用 `startswith(BLACKBOARD_PREFIX)` 匹配，确保 Hermes 创建的任务和 Registry 创建的任务可以互相读取黑板。