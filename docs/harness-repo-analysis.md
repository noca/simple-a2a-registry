# 项目整体认知报告 — Simple A2A Registry V2

> **版本**：1.0.0  
> **报告类型**：AI 只读扫描认知报告（SDD-B-1）  
> **生成日期**：2026/06/16  
> **地位**：服务于研发工程师及架构师快速上手、规范治理的现状分析底座。

---

## 1. 一句话概述

**Simple A2A Registry V2** 是一套基于 Google A2A (Agent-to-Agent) 协议规范构建的多租户、高复原力、生产级 Agent 注册发现、任务分发与 Kanban/DAG 编排平台。它通过单一系统，无缝融合了 **Agent发现层**（注册与保活）、**异步分发层**（WebSocket Hub-and-Spoke 实时推送）和 **编排引擎层**（9状态 Kanban 状态机、DAG 拓扑、Swarm 黑板和 HITL），被称为 "AI Agent 领域的 Kubernetes"。

---

## 2. 技术栈与运行形态

### 2.1 后端技术栈 (Backend Tech Stack)
* **语言**：Python >= 3.10 (推荐使用 3.11)
* **API 服务**：`aiohttp` 异步高性能 Web 框架，集成 `aiohttp-cors`。
* **持久化**：`SQLAlchemy 2.0`（生产级双引擎：SQLite WAL 模式与 MySQL 运行时无缝切换），搭载 `RetryEngine` 提供指数退避自动防瞬时闪断重试。
* **迁移工具**：`Alembic` 管理关系型 Schema 版本。
* **认证授权**：OAuth 2.1（支持 RS256/HS256 签发的 JWT）、JWKS 密钥端点。
* **指标监控**：`prometheus-client` 实时指标收集，公开 `/metrics` 端点。
* **定时编排**：`croniter` 用于基于 Cron 表达式在后台定时生成编排任务。

### 2.2 前端技术栈 (Frontend Tech Stack)
* **框架**：React + TypeScript + Vite + React Router DOM。
* **UI 组件库**：Ant Design (antd v5)。
* **状态管理**：`Zustand` 极简响应式状态。
* **运行机制**：作为 SPA 构建在 `a2a-admin/` 下，编译为静态 HTML/JS 资源存放至 `data/web`，由后端 aiohttp 静态资源服务进行极速托管。

### 2.3 运行与部署形态
* **单机运行**：命令行 `a2a-registry` 启动；或使用 Python `python -m simple_a2a_registry`。
* **容器化**：基于 `Dockerfile` 的 Multi-stage 构建（编译时拉取 node 进行前端打包与 GCC 依赖编译，运行时合并至 Python-slim 极简无 root 基础镜像，整体镜像大小低于 150MB）。
* **健康度检测**：内置 Kubernetes 友好的优雅停机（SIGTERM 捕获、通知所有在线 WS 连接 Agent 优雅释放任务）与 HTTP 健康检查端点（`/health` 包含 liveness/readiness/startup 探测、DB 连接池深度和活跃 WS 连接计量）。

---

## 3. 目录结构与模块地图

```text
simple-a2a-registry-v2/
├── .wiscode/                       # WisCode 规范治理包
├── a2a-admin/                      # 前端管理后台项目 (React + TS + AntD)
│   ├── src/                        # 前端核心源码
│   └── package.json                # 前端工程配置
├── docs/                           # 核心系统架构文档与设计指南 (单系统三层能力)
│   ├── architecture.md             # 顶层整体架构设计
│   ├── resilient-distribution.md   # 高复原力任务状态机及容灾设计
│   └── product-manual.md           # 1.0 完整的产品使用说明
├── migrations/                     # Alembic 持久化 Schema 变更管理
├── scripts/                        # 容器运行与本地调试脚本 (dev.sh / prod.sh)
├── simple_a2a_registry/            # 后端主程序包
│   ├── database/                   # 关系型数据库底层引擎
│   │   └── engine.py               # Double Engine (SQLite/MySQL) 抽象及闪断重试
│   ├── events/                     # 消息与异步传输总线
│   │   ├── event_bus.py            # EventBus (Publisher-Subscriber 架构)
│   │   └── sse_handler.py          # Server-Sent Events 实时的推流网关
│   ├── orchestration/              # P0 编排引擎核心
│   │   ├── dispatcher.py           # 后台派发线程 (Claim Lock、SLA 定时演算)
│   │   ├── state_machine.py        # 9状态 Kanban 状态机，控制生命周期
│   │   ├── workspace.py            # 共享工作区文件系统
│   │   ├── dependency.py           # DAG 图解析、循环依赖检测
│   │   ├── models.py               # 编排数据对象 (含安全溯源字段持久化映射)
│   │   └── routes.py               # 编排层 REST API (V2 API)
│   ├── security/                   # 安全围栏 (Security Harness)
│   │   ├── ape.py                  # Active Policy Enforcer (主动策略执行器)
│   │   ├── dtm.py                  # Delegated Token Manager (委派 Token 管理器)
│   │   └── pt.py                   # Provenance Tracker (凭据溯源追踪器)
│   ├── auth.py                     # OAuth 2.1 JWT (RS256) 签名、JWKS 与角色授权
│   ├── server.py                   # REST API 路由路由与 WebSockets 主服务
│   ├── store.py                    # Agent 卡片注册与 HTTP 心跳保活库
│   ├── config.py                   # 多源加载配置 (CLI > ENV > YAML)
│   └── cli.py                      # 统一的 argparse 命令行控制端
└── tests/                          # 强大的自动化单元测试与集成测试矩阵
    ├── benchmarks/                 # 性能基准测试包 (吞吐量、Security 哈希开销、SSE 消息延迟等)
    └── test_orchestration_e2e.py   # 核心端到端流程测试
```

---

## 4. 启动入口与主执行链路

```text
【启动阶段 CLI/Main】
        │ python -m simple_a2a_registry / a2a-registry
        ▼
【cli.py】解析命令行参数、加载 YAML 配置文件
        │ (合并环境和默认值：Config 实例)
        ▼
【server.py -> run_server()】
        ├── 1. 初始化结构化日志 (log.py - Text/JSON双形态)
        ├── 2. 建立双数据库引擎 (SQLite WAL/MySQL) & 跑 Alembic 自动 Schema 迁移
        ├── 3. 实例化四大核心 Store 
        │      (Store:Agent注册 | AuditStore:审计 | UserStore:会话 | TaskStore:编排任务)
        ├── 4. 启动后台后台守护协程:
        │      ├── Dispatcher (自动派发 ready 状态的任务)
        │      ├── AnomalyScanner (断联异常、僵死任务监控扫描)
        │      └── SlaCalculator (SLA 预警演算法)
        ├── 5. 挂载 APE (安全策略引擎) 与 DTM (委派 Token 处理器)
        ├── 6. 注册 Aiohttp Middleware 中间件链 (统一异常、请求追踪、限流、Prometheus、OAuth 鉴权)
        ├── 7. 绑定路由：/v1/agents (V1), /v2/tasks (V2), SSE, AdminWS, Metrics
        └── 8. 优雅停止 (优雅终止信号捕获，完成 Dispatcher 优雅释放，关闭 DB 线程池)
```

---

## 5. 核心业务与模块职责拆解

系统按照高内聚、低耦合理念拆分为四个层级，每个模块责任明确：

### 5.1 Agent 发现层 (Discovery Layer)
* **`store.py` (Store)**：维护 Agent 的注册卡片 (A2A Agent Card v1.0)。利用 `Heartbeat` 跟踪 Agent 的存活，未保活 Agent 会在 120s 后标记为离线，300s 后执行自动清理。
* **`validation.py`**：负责对 Agent 提交的 v1.0 Protobuf 兼容的元数据字段、Skill 及 URI 协议绑定做严苛的数据格式及合规性自检校验。

### 5.2 任务分发层 (Distribution Layer)
* **`server.py` (WS Hub-and-Spoke)**：在 Registry 与 Agent 之间建立 WebSocket 长连接。外部客户端发起任务分发时，网关通过 WS Hub 实时将 Task 结构投递至对应在线 Worker。
* **`events/event_bus.py`**：内置发布-订阅（PubSub）机制，内部各模块事件通过 EventBus 解耦广播，支持 Webhook 推送。
* **`events/sse_handler.py`**：为前端看板或上游控制中心提供单向、实时的 HTTP 事件推流通道。

### 5.3 编排引擎层 (Orchestration Layer)
* **`orchestration/state_machine.py`**：驱动 **9状态 Kanban 状态机**：
  `TODO` -> `READY` (前置依赖 DAG 均已成功完成) -> `RUNNING` (被 Worker Claim 锁定并执行中) -> `COMPLETED` / `FAILED` 等。其中第 9 状态为 `DANGLING` (挂线/吊死) 状态。
* **`orchestration/dispatcher.py`**：核心调度循环，使用原子性数据库锁 (Claim Lock)，高并发场景下防止多个 Agent 抢占同一个任务。
* **`orchestration/dependency.py`**：实现 DAG 计算，使用 DFS 检测任务链的循环依赖（有环直接拒绝创建），并在前置节点变成 COMPLETED 时将子节点自动 Promote 为 READY。
* **`orchestration/workspace.py` (WorkspaceManager)**：提供工作隔离文件系统。每个任务或 Swarm 拥有独立的工作目录，支持制品(Artifacts)的输入和输出隔离。

### 5.4 安全治理层 (Security Harness)
这是系统核心加固区，主要由三个模块协同：
* **`security/ape.py` (Active Policy Enforcer)**：强制执行策略检查点。在 Task 的 `Create`, `Claim`, `Complete` 黄金三角路径上实施校验。支持：`audit`（仅审计记录）-> `warn`（警告不拦截）-> `enforce`（强制 APE 403 绝拒）三阶段迁移，在最新分支上已经全面合拢为 **Enforce 模式**。
* **`security/dtm.py` (Delegated Token Manager)**：防止 Agent 的权限越界（提权/代持风险）。当 Task A 派生 Task B 给另一个 Agent 时，DTM 负责签发经过“Scope 衰减”的委派 Token，并在此处引入 `delegation_tokens` 关系型表做密码学签名和防重放（Replay Prevention）校验。
* **`security/pt.py` (Provenance Tracker)**：配合编排层 models 中新增持久化的 `origin_agent`、`delegation_depth` 和 `provenance_chain_id` 等溯源字段，全链路跟踪任务的发起树与委派关系网，实现凭证追踪与鉴权沙箱。

---

## 6. 配置、数据与外部集成

* **多级配置模型**：支持配置无缝级联。CLI 启动参数具有最高优先级，其次为环境变量（如 `A2A_AUTH_ENABLED`），最后为 YAML 配置文件。
* **双模关系型持久化**：
  * **开发环境**：支持极其轻量的 SQLite。自动启用 WAL (Write-Ahead Logging) 模式，多读一写具有很高性能，并通过 `threading.RLock` 保证连接在协程和线程之间的绝对线程安全。
  * **生产环境**：运行时切换至 MySQL (InnoDB 引擎)，使用 SQLAlchemy 线程池（QueuePool）应对高并发事务。
* **跨租户安全隔离**：在所有数据表（`agents`, `tasks` 等）中均内置了 `tenant_id`。API 请求必须在 Header 中携带 `X-Tenant-ID` 或解析 JWT 中的租户身份，底层 ORM 自动拼接租户过滤条件，从而实现多团队、多业务用例下的完美物理和逻辑隔离。

---

## 7. 测试与交付现状

* **覆盖面齐备**：测试套件极为丰富。除了覆盖传统的 CRUD 测试和 E2E 编排测试，还极具前瞻性地包含了：
  * 完整的 **多租户安全级联隔离测试**（如跨租户派发、Scope 边界篡改拦截）。
  * **MySQL 方言和性能模拟测试**（测试高并发与死锁复原）。
  * **SSL/TLS 加密及 JWT 签名对齐测试**。
* **专业的 Benchmark 性能套件**：
  在 `tests/benchmarks/` 下沉淀了极具生产参考意义的性能检测用例，在现代高频硬件下（WSL2 / AMD  Ryzen 9 7945HX）的基准测试数据如下：
  * **任务存储吞吐 (Direct Store)**：写入达到 17,600+ TPS，读取解析达到 106,000+ TPS。
  * **端到端 API 调度**：单纯的 HTTP 任务创建达到 2,400+ TPS；在 “创建-Claim-完成” 的高频率完整生命周期闭环下，吞吐依然保持在近 **700 TPS** 的高水位。
  * **Security 额外开销**：在 APE/DTM 强安全委派开启（Warn mode）时，由于密码学签名和策略链的解析，API 的 TPS 出现 40% - 60% 的损耗（1,800 TPS 左右）。
  * **SSE 实时推流延迟**：P50 延迟低于 **0.43ms**，P99 核心消息的端到端时延被压制在 **1.07ms** 以内，极具高时效实时交互性。

---

## 8. 明显风险与复杂区域（架构坏味道与技术债）

只读扫描中识别出三处核心复杂点，属于系统架构的重要风险区：

### 8.1 强密码学鉴权与高吞吐性能的博弈
* **风险描述**：DTM / APE 对每次任务操作都需要执行公钥密码学解密（RS256 委派 Token）以及数据表重放防范。基准测试结果表明，这会导致 TPS 约 50% 的显著损耗。
* **技术债表现**：由于没有设计内存中的 Token 热缓存，频繁读取物理关系数据库中的 `delegation_tokens` 容易在高吞吐状态下成为 IO 性能瓶颈。

### 8.2 WebSocket 网络闪断导致的 DANGLING（吊死）状态
* **风险描述**：当分布式网络出现瞬时抖动、Agent 的 WebSocket 长连接中断时，正在执行的任务状态会转换为 `DANGLING`，并启动 30s 倒计时心跳重连。如果断网 Agent 未能在宽限期内成功完成重连，Dispatcher 会强行标记任务为 FAILED 并重新派发。
* **架构隐患**：如果网络恢复后原 Agent 仍在本地静默运行并最后提交结果，极易诱发 “裂脑 (Brain-split)” 分布式冲突，甚至造成重复写入或破坏 Workspace 工作区状态数据的不变量。

### 8.3 SQLite 与 MySQL 双底层锁机制差异带来的死锁隐患
* **风险描述**：`engine.py` 内部实现了 SQLite WAL 与 MySQL 的运行时动态数据库切换。然而，SQLite 的库级读写互斥锁与 MySQL 的 InnoDB 行锁在并发处理的边界行为（例如 claim 任务抢占、DAG 触发 promote、SLA 计算等涉及多表更新的操作）大不相同。
* **架构隐患**：多表级联更新与频繁的 DAG 递归推进在高并发 MySQL 环境下非常容易引发 `Deadlock` 异常。虽然通过 `RetryEngine` 进行了多次重试和退避，但未能从根本上将锁机制进行分布式原子抽象。

---

## 9. 建议的阅读顺序

若您是第一次接手该项目的工程师，推荐以下递进式的阅读链路以快速建立全貌认知：

```text
Step 1. 理论及架构全貌学习：
   └── docs/architecture.md （顶层三层能力设计与数据流）
   └── docs/product-manual.md （功能介绍与快速体验、API 规范）

Step 2. 认识核心通信总线：
   └── simple_a2a_registry/server.py （aiohttp server 实例化，认识中间件，了解路由挂载）
   └── simple_a2a_registry/registry_handler.py （WebSocket 长连接、WS Hub 及 Claim/dispatch 核心逻辑）

Step 3. 探究核心调度大脑：
   └── simple_a2a_registry/orchestration/dispatcher.py （Dispatcher 调度环，认领锁 Claim 锁竞争）
   └── simple_a2a_registry/orchestration/state_machine.py （Kanban 9状态状态机的转换不变量）

Step 4. 深入安全加固沙箱：
   └── simple_a2a_registry/security/ape.py （如何使用 Enforce 模式，在 Task API 处构建安全铁网）
   └── simple_a2a_registry/security/dtm.py （如何通过 Scope 衰减和 RS256 签发委派 Token，如何防止 replay）
```

---

## 10. 待确认问题清单 (Pending Confirmation)

在正式落地和改进前，以下事项需要向业务架构团队/运维团队进一步确认：
1. **关于 APE 安全执行模式**：在 wt/p3-c-prod-deploy 分支并入主线后，生产环境的 APE 是否默认切换至 `enforce` 模式？是否有由于 APE 403 强拦截导致旧 Agent 不兼容并被拒绝派发任务的现实例子？
2. **关于 MySQL 的具体实例承载**：目前的 Docker 镜像与 scripts 启动了 MySQL 方言支持，那生产部署采用的主从架构为何？重连重试（`RetryEngine`）次数是否经过极限压测论证？
3. **关于 Workspace 存储隔离与容灾**：Workspace 分配了独立的临时和持久化磁盘空间。在高可用的多副本 Kubernetes 环境下，这些 Workspace 目录是否绑定了共享的网络卷（如 NFS/Ceph/EFS）？如果发生 Pod 漂移，Workspace 状态如何优雅同步？

---

## 11. 风险点清单 (Risk Points)

1. **级联委派栈溢出风险**：DTM 策略中的 `max_delegation_depth` 默认设定为 10。如果任务 DAG 委派层级很深且未能拦截，调用栈递归计算策略会带来 CPU 耗尽风险。
2. **DANGLING 与并发数据污染风险**：在断联重试、宽限期超时后强行标记 FAILED 与后台重派发流程中，对于 Workspace 的未提交数据锁清理存在弱点，可能引起并发覆写与文件损坏。
3. **性能衰减风险**：在大流量生产级别运行中，随着 `delegation_tokens` 审计数据的追加（Append-only），未见对该表的定期过期清理（TTL/Purge）机制，可能会导致该表数据爆炸，严重拖慢 JWT 加密验证与重放检查的响应速度。

---
*本报告遵循 **ai-coding-best-practice** 规范治理 SDD-B B-1 阶段只读扫描准则进行构建。*
