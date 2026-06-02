# 任务分发模型调研与架构优化建议

> 基于 simple-a2a-registry-v2 当前架构的对比分析

---

## 一、当前架构分析

### 1.1 现状概述

simple-a2a-registry-v2 采用 **轮询 + 本地子进程 + WebSocket push** 的混合分发模型：

| 组件 | 实现 | 职责 |
|------|------|------|
| **Dispatcher** | asyncio 后台循环（每 5s 轮询） | TTL 释放 → 重试晋升 → Claim + Spawn |
| **FlowController** | 内存状态机 | 并发限制（max_concurrent=5）、熔断器（3 次失败/5min 冷却）、指数退避重试 |
| **TaskStore** | SQLite/MySQL（BEGIN IMMEDIATE） | 5 张表、8 状态状态机、DAG 依赖链 |
| **Worker 分发** | 三级混合：① 事件总线触发（主通道）→ ② WS push/SSE | HTTP callback（加速）→ ③ 本地子进程 spawn |

### 1.2 分发优先级链

```
ready 任务到来
    │
    ├── 事件总线触发（主通道）
    │   · 任务写入 DB（强一致性，不可丢）
    │   · 发布事件到 Event Bus
    │   · Agent 在下次连接时主动补偿拉取
    │
    ├── WS push / SSE（推送加速器，尽力而为）
    │   · Agent 在线 → 实时推送降低延迟
    │   · 推送失败不影响可靠性（DB 已持久化）
    │   · 不做为重试 / 确认依赖
    │
    ├── HTTP callback
    │   · Agent 注册 callback → 通知其拉取
    │   · 30s timeout + 指数退避
    │
    └── 均不满足 → 等待 Agent 下次心跳时拉取
```

---

## 二、五种分发模型对比

### 2.1 消息队列模型（RabbitMQ / NATS / Redis Streams）

| 维度 | 说明 |
|------|------|
| **核心思想** | Producer 将任务发布到队列/主题，Consumer 订阅消费。天然解耦生产者和消费者 |
| **优势** | ① 完全解耦——发布者不关心谁消费 ② 持久化——宕机不丢消息 ③ 消费组——N 个 Worker 竞争消费实现负载均衡 ④ Dead Letter——失败消息自动隔离 |
| **劣势** | ① 额外运维组件（MQ 集群） ② 缺少任务级状态机——MQ 只管投递不管生命周期 ③ 难以表达 DAG 依赖（需要结合 Workflow 引擎） ④ 回调/确认模式增加复杂度 |
| **与当前架构的关系** | 可替代当前轮询扫描——MQ push 替代 poll，但当前架构的 8 状态状态机是 MQ 不具备的 |
| **最适合场景** | 大量短任务、Worker 弹性伸缩、对任务可见性要求不高的场景 |

### 2.2 事件驱动模型（Event Sourcing + CQRS）

| 维度 | 说明 |
|------|------|
| **核心思想** | 每个状态变更是一个不可变事件（Event），系统状态由事件流重放得到。读/写模型分离（CQRS） |
| **优势** | ① 完整审计——每个状态变更都有事件记录（当前已有 task_events 表） ② 时间旅行——可回溯任意时间点的系统状态 ③ 天然支持 DAG —— 事件链即依赖关系 ④ 可扩展——新 Consumer 订阅事件流而不影响已有逻辑 |
| **劣势** | ① 事件版本管理复杂——schema 演化需要兼容层 ② 最终一致性——CQRS 读模型可能滞后写模型 ③ 学习曲线陡峭 ④ 简单 CRUD 操作反而增加复杂度 |
| **与当前架构的关系** | 当前已有 task_events 事件表，但仅用于审计；可扩展为真正的 Event Sourcing，每个 Dispatcher 动作都经过事件总线 |
| **最适合场景** | 需要完整审计、多 Consumer 订阅、复杂 DAG 工作流的场景 |

### 2.3 声明式调度模型（Airflow / Temporal / Prefect）

| 维度 | 说明 |
|------|------|
| **核心思想** | 用户声明 DAG（有向无环图）定义任务依赖关系，调度引擎自动管理执行顺序、重试、并行度 |
| **优势** | ① DAG 原语——天然支持依赖链（当前已实现基础依赖解析） ② 重试策略丰富——指数退避、间隔、条件重试 ③ 调度日历——定时触发、Cron、事件触发 ④ Backfill——历史数据补偿执行 |
| **劣势** | ① 重量级——Temporal 需要独立集群 ② 调度延迟——调度器本身有周期，不适合亚秒级分发 ③ 资源隔离弱——多个 DAG 共享 Worker 池时的 QoS 问题 ④ Operator 生态绑定——Python-only DSL |
| **与当前架构的关系** | 当前架构已有 DAG 依赖链（parent-child link）和基础依赖晋升机制，可作为声明式调度的基础 |
| **最适合场景** | 复杂工作流编排、数据管道（Pipeline）、定时批处理 |

### 2.4 P2P Agent-to-Agent 通信模型（A2A 标准 vs 替代方案）

| 维度 | A2A 标准 | 替代方案（Google ADK / Agent-to-Agent Mesh） | 当前架构 |
|------|----------|---------------------------------------------|----------|
| **通信模式** | JSON-RPC over HTTP/SSE | gRPC / WebSocket / HTTP 混合 | WS push + HTTP callback |
| **发现机制** | `/.well-known/agent-card.json` | Registry service / DNS-based / Peer discovery | Agent Registry API |
| **任务模型** | Task → Message → Part 流式分段 | 各有不同（ADK 基于 Event，Mesh 基于 Message） | 8 状态 Kanban Task |
| **状态管理** | 无标准——各 Agent 自管 | 无标准 | 集中式 Store（强一致性） |
| **适用场景** | 对外开放——不同组织 Agent 互操作 | 内部集群——同一组织内 Agent 通信 | 集中于 Registry（Gateway 模式） |

**关键发现**：当前架构本质上是「Gateway 模式」而非纯 P2P——Registry 是中心协调点。A2A 更适合作为对外接口协议，对内仍需要集中式状态管理。

### 2.5 Kubernetes-native 模型（Kuberbetes Job / Argo Workflows）

| 维度 | 说明 |
|------|------|
| **核心思想** | 利用 Kubernetes 作为统一调度平面——每个 Worker 是一个 Pod，Kuberbetes 负责调度、重试、资源隔离 |
| **优势** | ① 资源隔离——每个 Worker 独立 Pod/Container ② 天然弹性——Horizontal Pod Autoscaler ③ CRD+Operator——Argo 将 DAG 定义为 CRD，Operator 驱动状态机 ④ 亲和性调度——指定 GPU 节点、地域亲和 |
| **劣势** | ① 重依赖——需要 Kubernetes 集群 ② Pod 启动延迟——冷启动 1-5s，不适合高频短任务 ③ 状态管理弱——Kuberbetes job 本身缺少任务级状态机（需 Argo/Tekton 补充） ④ 调试困难——Pod 日志、事件调试工作流 |
| **与当前架构的关系** | 当前「本地子进程 spawn」模式可演进为「Kubernetes Job 创建」模式，但会增加运维复杂度 |
| **最适合场景** | 已用 Kubernetes、Worker 需 GPU/大规模并行、需要统一资源调度的团队 |

### 2.6 SSE（Server-Sent Events）vs WebSocket（补充）

SSE 是近年快速普及的替代方案，两者核心差异如下：

| 维度 | WebSocket | SSE (Server-Sent Events) |
|------|-----------|-------------------------|
| **方向** | 双向 | 单向（服务端→客户端） |
| **协议** | 独立 TCP connection | 复用 HTTP/2 连接 |
| **断线重连** | 客户端手动实现 | EventSource API 自动重试 |
| **负载均衡** | 需要 sticky session 或网关代理 | 原生支持（每个请求独立） |
| **服务端状态** | 需要维护 per-connection 状态 | 几乎无状态 |
| **实际延迟** | <100ms | <200ms |
| **运维成本** | 高 | 低 |

**关键发现**：SSE 在推送场景上比 WS 更适合当前架构——通知 Worker "有新任务"场景只需单向推送。但 WS 在需要双向交互（Agent 报告进度、请求中断）时仍有优势。现实选择不是二选一，而是：**Agent 端用 WS 做双向控制通道，Registry 侧对 Dashboard/Admin 的实时推送切 SSE**。

---

## 三、当前方案痛点分析

### 3.1 轮询 vs 事件驱动

```
现状：Dispatcher 每隔 5s 轮询 DB → 事件驱动架构
                                   
痛点：                                解决方案方向：
┌─────────────────────────────┐     ┌────────────────────────────┐
│ 5s 延迟——新增任务需等    │     │ 引入事件总线——任务创建   │
│ 1 个 poll cycle 才能发现    │     │ 即发布事件 → 立即触发    │
├─────────────────────────────┤     │ dispatch                     │
│ DB 压力——每 5s 全表/    │     ├────────────────────────────┤
│ 索引扫描就绪任务           │     │ 事件驱动 + DB 状态持久化   │
│ （SELECT * WHERE status=    │     │ 分离——DB 仅作持久化        │
│   'ready'）                 │     │ 不再频繁扫描             │
├─────────────────────────────┤     ├────────────────────────────┤
│ 与用户界面脱节——用户看    │     │ SSE/WS 推送实时状态更新  │
│ 不到实时状态变更            │     │ 推送到 Dashboard/Admin    │
└─────────────────────────────┘     └────────────────────────────┘
```

### 3.2 本地子进程 vs 远程 Worker

```
本地 spawn (Cluster Mode)：
  asyncio.create_subprocess_shell(cmd)
     │
     ├── 优点：零网络延迟、调试方便
     │
     ├── 缺点 1：资源竞争——N 个 Worker 共享 Registry 进程资源
     │    （CPU、内存、DB 连接池、文件描述符）
     │
     ├── 缺点 2：隔离性差——一个 Worker 崩溃可能影响主进程
     │    （虽已用 fire-and-forget asyncio.create_task 封装）
     │
     ├── 缺点 3：Worker 无法独立扩缩容——所有 Worker
     │    运行在同一台机器上
     │
     └── 缺点 4：跨机器分发需额外跳板
          （当前通过 WS push/SSE 或 callback 模式加速通知）
```

### 3.3 单点故障风险

| 风险类型 | 严重程度 | 说明 |
|----------|---------|------|
| **Dispatcher 进程崩溃** | 高 | 所有分发停止——task 停留在 ready 无人处理 |
| **SQLite 数据库损坏** | 高 | 当前 CKANBAN DB 使用 SQLite，磁盘满/掉电易损坏 |
| **内存状态丢失** | 中 | FlowController 状态（熔断器、并发计数）全在内存——重启后丢失。虽可通过 DB 重建（`_reconcile_flow_counts`），但熔断器历史丢失 |
| **MySQL 单点** | 中 | 生产环境 MySQL 一般主从，切换期间不可写 |

### 3.4 伸缩性瓶颈

| 瓶颈 | 当前状态 | 扩展方向 |
|------|---------|---------|
| **DB 写入** | 每个状态变更一次写入 | 可接受——任务变更频率低（秒级） |
| **DB 扫描** | 每 5s 全表扫描 ready 任务 | 高并发时成瓶颈——1000+ ready 任务时索引扫描加重 |
| **Worker 密度** | 单进程内 spawn | 单个 Registry 进程 spawn 过多子进程导致资源耗尽 |
| **WS 连接数** | 单机 aiohttp 管理 | 单个 aiohttp 服务器约支持 10K WS 连接（理论），实际 2-3K |

---

## 四、最佳实践架构建议

### 4.1 推荐的分发模型组合（分层架构）

```
                    ┌────────────────────────────────────────┐
                    │        用户界面 / 外部 API              │
                    │   (HTTP API + Web Dashboard)            │
                    └────────────────┬───────────────────────┘
                                     │
                    ┌────────────────▼───────────────────────┐
                    │      Layer 1: 事件驱动分发总线          │  ← 新增/改造
                    │  ┌──────────────────────────────────┐  │
                    │  │  Event Bus (内存 + DB 持久化)    │  │
                    │  │  · 任务创建 → 事件触发 dispatch  │  │
                    │  │  · TTL 超时 → 事件触发释放       │  │
                    │  │  · 结果回传 → 事件触发依赖晋升   │  │
                    │  └──────────────────────────────────┘  │
                    └────────────────┬───────────────────────┘
                                     │
                    ┌────────────────▼───────────────────────┐
                    │      Layer 2: 声明式调度引擎            │  ← 增强
                    │  ┌──────────────────────────────────┐  │
                    │  │  DAG 引擎 + 8 状态状态机          │  │
                    │  │  · 依赖链解析（已有）               │  │
                    │  │  · 条件分支 / 并行扇出            │  │  ← 新增
                    │  │  · 超时 / 重试策略（已有）         │  │
                    │  └──────────────────────────────────┘  │
                    └────────────────┬───────────────────────┘
                                     │
        ┌────────────────────────────┼────────────────────────────┐
        │                            │                            │
┌───────▼────────┐     ┌────────────▼────────┐     ┌────────────▼────────┐
| Layer 3a:      │     │ Layer 3b:           │     │ Layer 3c:           │
│ WS Push / SSE  │     │ HTTP Callback       │     │ K8s Job / Cmd       │
│ (推送加速器)    │     │ (现有)               │     │ (可选，未来)         │
│ · 尽力而为推送  │     │ · 30s timeout       │     │ · K8s API 创建 Job  │
│ · 主通道在 DB   │     │ · 指数退避           │     │ · 资源预留           │
└────────────────┘     └─────────────────────┘     └─────────────────────┘
```

### 4.2 渐进式迁移路径

**Phase 1（当前可做）— 事件驱动化（1-2 周）**

```
问题：5s 轮询存在延迟，DB 压力大
方案：引入事件总线机制
┌─────────────────────────────────────────────────────┐
│ 1. 在 store.py 中，每个写操作（create_task,         │
│    update_task_status, add_comment）后发布事件       │
│                                                     │
│ 2. 引入 asyncio.Queue 作为内存事件通道               │
│    event_queue = asyncio.Queue()                    │
│                                                     │
│ 3. Dispatcher 从 Queue 获取事件而非轮询 DB：          │
│    while self._running:                             │
│        event = await event_queue.get()              │
│        await self._handle_event(event)              │
│                                                     │
│ 4. 保留原有轮询作为兜底（Safety net）：               │
│    - 如果 Queue get 超时 30s → 轮询一次 DB           │
│    - 防止事件丢失导致的"僵尸 ready"                    │
└─────────────────────────────────────────────────────┘
```

**Phase 2（推荐）— 状态管理增强（2-3 周）**

```
问题：FlowController 状态存在内存中，重启丢失
方案：持久化到 DB + 内存缓存
┌─────────────────────────────────────────────────────┐
│ 1. 新增 AgentFlowState 表（MySQL/SQLite）            │
│    CREATE TABLE IF NOT EXISTS agent_flow_state (    │
│        agent_id      TEXT PRIMARY KEY,              │
│        concurrent_count   INT NOT NULL DEFAULT 0,   │
│        consecutive_failures INT NOT NULL DEFAULT 0, │
│        circuit_tripped_until BIGINT,                │
│        last_heartbeat_at   BIGINT,                  │
│        updated_at     BIGINT NOT NULL               │
│    );                                               │
│                                                     │
│ 2. FlowController 改为 Read-Through Cache：          │
│    - 读：先从内存读，miss 则查询 DB                  │
│    - 写：先写内存，异步写 DB（最终一致性）           │
│    - 重启时从 DB 重建全部状态                        │
│                                                     │
│ 3. 熔断器状态持久化——恢复后自动继续冷却               │
└─────────────────────────────────────────────────────┘
```

**Phase 3（可选）— 多层 Worker 适配（3-4 周）**

```
问题：本地 spawn 不便于跨机器分发、扩缩容
方案：抽象 WorkerExecutor 接口 + 多种实现
┌─────────────────────────────────────────────────────┐
│ 1. 定义 WorkerExecutor 抽象基类：                    │
│    class WorkerExecutor(ABC):                       │
│        async def dispatch(self, task) → str         │
│        async def cancel(self, task_id) → None       │
│        async def get_status(self, task_id) → str    │
│                                                     │
│ 2. 多种实现：                                       │
│    - LocalSubprocessExecutor（现有，改造）           │
│    - KubernetesJobExecutor（新建，可选）             │
│    - DockerSwarmExecutor（新建，可选）               │
│    - CallbackExecutor（现有，增强）                  │
│                                                     │
│ 3. Dispatcher 通过配置选择 Executor：                │
│    config.executor = "local" | "kubernetes"         │
│    # 或混合：                                       │
│    config.executor_map = {                          │
│        "worker-a": "local",                         │
│        "worker-b": "kubernetes",                    │
│    }                                                │
└─────────────────────────────────────────────────────┘
```

### 4.3 关键设计决策

| 决策维度 | 选择 | 理由 |
|---------|------|------|
| **一致性 vs 可用性** | **强一致性（CP）** | TaskStore 使用 `BEGIN IMMEDIATE` 保证原子性——任务状态一旦变更不可丢。对编排引擎而言，一致性高于可用性 |
| **内存 vs 持久化** | **内存缓存 + DB 持久化** | FlowController 状态适合内存中高频读写（每 5s 决策），DB 兜底保证重启不丢失 |
|| **轮询 vs 事件** | **事件为主 + 轮询兜底** | 事件驱动消除延迟和空轮询成本，定期轮询作为 Safety net 防止事件丢失 |
|| **WS/SSE 定位** | **推送加速器，非可靠主通道** | DB 写入 + 事件总线是可靠性基础，WS/SSE 仅是降低延迟的优化。推送失败不影响任务可靠性——Agent 下次连接时从 DB 补偿拉取 |
|| **单机 vs 集群** | **先单机，预留集群接口** | 当前规模下单机足够（事件总线和 DB 不依赖连接数），WorkerExecutor 抽象为将来集群化做好准备 |
| **标准 P2P vs Gateway** | **Gateway（含 Registry）** | A2A 作为对外协议接口，内部使用 Gateway 模式集中管理任务生命周期，便于审计和 HITL |

### 4.4 风险缓解

```ascii
风险                                  缓解措施
────────────────────────────────────────────────────────────
Dispatcher 进程宕机                   systemd 超级守护 / Docker restart always
                                     + Dispatcher 启动时从 DB 重建所有 running 任务
                                     + 标记超过 TTL 的 running 任务为 failed

DB 写入冲突（并发高）                 当前已用 BEGIN IMMEDIATE + RLock
                                     MySQL 生产环境使用 SELECT...FOR UPDATE 代替

事件丢失（事件总线宕机）               Safety net 轮询每 30s 全量扫描一次
                                     事件写入 DB（event sourcing 持久化）

Worker 子进程泄漏                     _watch_worker 超时 24h 自动 Kill
                                     Worker 启动时登记 PID，Registry 关闭时清理

消息队列积压（Phase 3 引入 MQ 后）     Backpressure：限制 Queue 大小
                                     超过阈值启用 Circuit Breaker——暂停接收新任务
```

---

## 五、总结

### 5.1 当前架构的核心优势

1. **成熟的状态机**——8 状态 Kanban 模型覆盖了从创建到归档的完整生命周期，比 MQ 和 Kubernetes Job 都更适合任务编排
2. **DAG 依赖链**——parent/child 链接与自动晋升机制，已实现声明式调度引擎的雏形
3. **三层分发**——事件总线驱动（主通道）→ WS/SSE 推送加速 → HTTP callback / 本地 spawn，覆盖不同 Agent 类型的延迟要求
4. **完整审计**——task_events 表记录每个状态变更，便于追溯

### 5.2 最需要改进的方向（按优先级）

1. **事件驱动化（Phase 1）**——消除 5s 轮询延迟，当前收益最高的改进
2. **状态持久化（Phase 2）**——FlowController 从纯内存改为 DB 持久化，提升健壮性
3. **WorkerExecutor 抽象（Phase 3）**——为未来集群化做准备，降低架构演进阻力

### 5.3 不推荐的方向

- **纯 MQ 模型**——缺少任务级状态机，不适配当前编排引擎的设计
- **纯 P2P A2A**——失去集中式状态管理和审计能力
- **全量迁移到 Kubernetes**——增加不必要的运维复杂性，建议通过 WorkerExecutor 抽象按需引入

> 核心原则：**DB + 事件总线是可靠性主通道，WS/SSE 是延迟加速器——编排引擎保持强一致性（CP），Worker 分发层追求可用性（AP）**