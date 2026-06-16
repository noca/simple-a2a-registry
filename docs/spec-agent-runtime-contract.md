# spec.md — Agent Runtime Contract（智能体运行时契约）

> **文档状态**：已定稿 (Approved) — §13 决议已确认，不进入 plan 阶段
> **作者**：WisCode（AI Agent）+ 项目维护者
> **日期**：2026/06/16
> **地位**：本文件是「Agent Runtime Contract」这一新功能域的**业务唯一事实来源 (SSOT)**。
> **裁决顺序**：`constitution.md > AGENTS.md > 本 spec.md > plan.md > tasks.md > 既有代码`。
> **阶段声明**：本文件为 SDD A-1 阶段产物，**仅定义业务规则与外部可观察行为，不含任何技术实现方案**。技术实现须另行产出 `plan.md`。

---

## 1. 背景与目标

### 1.1 背景

Simple A2A Registry V2 已具备完整的「任务流转层」——A2A 协议下的注册发现、WebSocket 分发、9 状态 Kanban 编排、Claim Lock、DTM 委派与 APE 强安全。然而在实际接入中暴露出一个结构性缺口：

> **当一个任务被分发给某个 Agent 时，平台没有规定「Agent 应当如何承接、执行并返回这个任务」的标准契约。**

具体表现为三个被混在一起的问题：

1. **粒度缺失**：系统用同一套重量级 Task 模型承接所有工作——既有“读一个文件”这种秒级原子调用，也有“重构整个模块”这种分钟级长任务。小活儿太重、大活儿描述不清。
2. **实时性缺失**：同步的快任务被迫进入异步状态机，客户端只能轮询，体验差；而长任务若按同步思路写又会连接超时。
3. **承接契约缺失**：Agent 开发者面对的是一个“空白 WebSocket”，没有标准告诉他任务长什么样、该怎么接、该返回什么、出错怎么报，导致“Agent 不知道怎么写”。

### 1.2 目标

定义一层 **Agent Runtime Contract（智能体运行时契约）**，使：

* **G1**：任务按交互粒度被显式分级，不同粒度走不同的承接与实时性路径。
* **G2**：任何 Agent 收到的任务都遵循统一的 **Task Envelope（任务信封）** 结构，Agent 据此即可确定性地承接与返回。
* **G3**：Agent 的返回必须满足任务声明的 **Output Contract（返回契约）**，否则视为失败。
* **G4**：在任务进入 Agent 与离开 Agent 的两个边界上，平台施加**执行期内容安全检查**（注入检测 / 敏感数据脱敏），且对 Agent 开发者透明。
* **G5**：契约必须复用并贯穿既有的多租户隔离、Scope 衰减与委派溯源机制，不另造平行体系。

### 1.3 非目标（本 spec 不回答）

* 不定义 MCP 网关的具体桥接实现（另起 spec）。
* 不定义注入检测 / PII 脱敏所采用的具体算法或模型（属 plan 层选型）。
* 不定义分布式 Workspace 存储后端（另起 spec）。
* 不改变既有 A2A V1 注册发现与 V2 编排的对外业务行为。

---

## 2. 术语表 (Glossary) ⭐

| 术语 | 英文 / 字段 | 定义 |
| :--- | :--- | :--- |
| **运行时契约** | Agent Runtime Contract | 规定 Agent 如何承接、执行、返回一个任务的全部业务约定的总称。 |
| **交互粒度** | Interaction Mode | 任务的承接形态分级，取值 `SYNC_CALL` / `TASK` / `JOB`。 |
| **即时调用** | SYNC_CALL | 函数级、原子、同步返回的最小粒度任务，不进入状态机、不需认领。 |
| **作业任务** | TASK | 作业级、异步、需认领并汇报心跳的任务，进入既有 9 状态状态机。 |
| **工程任务** | JOB | 项目级、可被拆解为子任务 DAG 的最大粒度任务。 |
| **任务信封** | Task Envelope | 平台投递给 Agent 的标准化任务结构，结构对所有粒度统一。 |
| **能力声明** | Skill Declaration | Agent 注册时声明的、其可承接的某个具名能力及其输入/输出 Schema。 |
| **返回契约** | Output Contract | 任务对 Agent 返回结果的结构与必填字段的强制约定。 |
| **安全上下文** | Security Context | 信封内携带的委派边界信息（有效 Scope、委派深度、截止时间、溯源链）。 |
| **承接** | Accept / Claim | Agent 接收并对一个任务负责的动作；`SYNC_CALL` 为即时承接，`TASK`/`JOB` 为认领承接。 |
| **执行期安全围栏** | Runtime Guardrail | 在任务进出 Agent 边界施加的内容安全检查（注入检测 / 敏感脱敏）。 |

---

## 3. 用户与角色

| 角色 | 说明 | 与契约的关系 |
| :--- | :--- | :--- |
| **分发方 (Assigner)** | 发起任务的人或上游 Agent | 创建任务时必须声明 `interaction_mode` 与 `output_contract`。 |
| **承接方 (Worker Agent)** | 执行任务的 Agent | 必须按信封承接、按返回契约回传结果。 |
| **平台 (Registry)** | 本注册编排中心 | 构建信封、施加安全围栏、校验返回契约、驱动状态流转。 |
| **租户管理员 (Tenant Admin)** | 多租户下的安全/治理责任人 | 关注委派边界与执行期安全策略，不直接参与单任务执行。 |

---

## 4. 范围边界

### 4.1 In Scope（本次纳入）

* 任务交互粒度三档分级及其承接/实时性语义。
* Task Envelope 的字段构成与语义约定。
* Agent 能力声明（Skill Declaration）与返回契约（Output Contract）的业务约定。
* 安全上下文在信封中的传递与边界约束。
* 执行期安全围栏的进出口行为约定（行为，非算法）。

### 4.2 Out of Scope（本次明确排除）

* MCP 协议网关的桥接实现。
* 注入检测 / 脱敏的具体技术选型与模型。
* 动态 LLM 规划器、条件路由分支（属另一功能域）。
* 分布式对象存储 Workspace 后端。
* Token 计费 / OpenInference Trace（属可观测性功能域）。

---

## 5. 业务不变量 (Business Invariants) ⭐

> 以下不变量在任何场景下都必须成立，违反即为系统进入未定义状态，须按 Fail-Fast 立即拒绝或中止。

* **INV-1（粒度必声明）**：任何任务在创建时必须携带合法的 `interaction_mode`，三档之一，缺省或非法值一律拒绝创建。
* **INV-2（信封完整性）**：投递给 Agent 的信封必须包含 `task_id`、`interaction_mode`、`skill`、`input`、`output_contract`、`security_context`、`tenant_id` 七个必填字段，缺一不可投递。
* **INV-3（粒度—实时性绑定）**：`SYNC_CALL` 不得进入 9 状态状态机、不得要求认领；`TASK`/`JOB` 必须经认领（Claim）方可执行。三者不可混用承接路径。
* **INV-4（返回即校验）**：Agent 返回结果必须通过 `output_contract` 校验方可视为成功；校验失败的返回一律判定为任务失败，不得静默接受。
* **INV-5（租户贯穿）**：信封、承接、返回、安全检查全链路必须携带并校验一致的 `tenant_id`；租户为空或前后不一致一律拒绝。
* **INV-6（委派边界单调收缩）**：信封内 `security_context.effective_scope` 不得宽于分发方自身的 Scope；`delegation_depth` 不得超过 `max_delegation_depth`，溢出必须抛出 `DelegationDepthExceeded`。
* **INV-7（安全围栏不可绕过）**：所有任务的入口（投递前）与出口（返回后）必须经过执行期安全围栏；该检查对 Agent 透明，但任何任务都不得豁免。
* **INV-8（截止即终止）**：`security_context.deadline_ms` 到期后，平台必须将该任务判定为超时失败并按既有策略处理，Agent 在到期后提交的结果不得覆盖已判定状态（防裂脑）。

---

## 6. 核心场景与功能需求

### SCN-01：分发方按粒度创建任务

* **REQ-01.1**：分发方创建任务时必须声明 `interaction_mode`（`SYNC_CALL`/`TASK`/`JOB`）与目标 `skill`。
* **REQ-01.2**：分发方必须为任务提供 `output_contract`，声明返回结果的必填字段与结构约定。
* **REQ-01.3**：当 `interaction_mode = SYNC_CALL` 时，平台不创建状态机任务，直接尝试同步投递并等待结果。
* **REQ-01.4**：当 `interaction_mode = TASK / JOB` 时，平台创建状态机任务，进入既有 `TODO → READY → ...` 流转。

### SCN-02：平台构建并投递 Task Envelope

* **REQ-02.1**：平台必须将任务封装为统一结构的 Task Envelope 后再投递给 Agent（见 §7）。
* **REQ-02.2**：平台在投递前必须填充 `security_context`（基于 DTM 衰减后的有效 Scope、当前委派深度、截止时间、溯源链 ID）。
* **REQ-02.3**：平台在投递前必须对信封 `input` 中的自然语言内容执行入口安全围栏检查（注入检测）。检测命中时，按配置策略拒绝投递或标记告警（具体策略见 §8）。

### SCN-03：Agent 承接任务

* **REQ-03.1**：Agent 收到信封后，依据 `interaction_mode` 决定承接方式：
  * `SYNC_CALL`：立即执行并同步返回，**不**发认领、**不**发心跳。
  * `TASK`/`JOB`：先认领（Claim）获得任务所有权，执行期间周期性汇报心跳。
* **REQ-03.2**：Agent 必须根据信封 `skill` 路由到自身对应的能力实现；信封 `skill` 不在 Agent 能力声明内时，必须显式拒绝承接（Fail-Fast），不得静默忽略。
* **REQ-03.3**：Agent 必须在 `security_context.deadline_ms` 内完成；预计超时应主动放弃并上报，而非静默挂起。

### SCN-04：Agent 返回结果与平台校验

* **REQ-04.1**：Agent 返回结果必须满足信封 `output_contract` 声明的结构（含必填字段）。
* **REQ-04.2**：平台收到返回后必须执行出口安全围栏（敏感数据脱敏 / 泄露检测），再落库与回传分发方。
* **REQ-04.3**：返回不满足 `output_contract` 时，平台必须判定任务失败并记录失败原因，不得静默接受残缺结果。
* **REQ-04.4**：对 `TASK`/`JOB`，结果落库后按既有状态机推进至 `COMPLETED` 并触发下游 DAG Promote；对 `SYNC_CALL`，结果直接同步返回分发方。

### SCN-05：异常与裂脑防护

* **REQ-05.1**：`TASK`/`JOB` 的承接 Agent WebSocket 断连时，沿用既有 `DANGLING` 宽限期机制；宽限期超时按既有策略转 FAILED 并重派发。
* **REQ-05.2（裂脑防护）**：任务一旦因超时或重派发被判定为非原承接方所有，原 Agent 后续提交的结果必须被拒绝（依据 INV-8），不得覆盖现状态或污染 Workspace。
* **REQ-05.3**：`SYNC_CALL` 在投递后若目标 Agent 不在线或在约定同步窗口内无响应，平台必须立即返回明确的失败（而非转异步或挂起）。

---

## 7. Task Envelope 字段约定（业务语义层）

> 本节仅定义字段的**业务语义与必填性**，不规定其物理序列化格式或代码类型（留待 plan.md）。但须遵循 CLAUDE.md：实现时采用 dataclasses，**严禁 Pydantic**。

| 字段 | 必填 | 业务语义 |
| :--- | :---: | :--- |
| `task_id` | ✅ | 任务唯一标识。 |
| `interaction_mode` | ✅ | 粒度档位：`SYNC_CALL`/`TASK`/`JOB`。决定承接路径（INV-3）。 |
| `skill` | ✅ | 目标能力名，须命中承接 Agent 的能力声明。 |
| `input_schema` | ⬜ | 输入结构的 Schema 描述，供 Agent / 模型零脑补理解输入。 |
| `input` | ✅ | 任务实际输入参数。 |
| `output_contract` | ✅ | 返回结构与必填字段约定（INV-4）。 |
| `security_context` | ✅ | 委派边界：`effective_scope` / `delegation_depth` / `deadline_ms` / `provenance_chain_id`。 |
| `tenant_id` | ✅ | 租户标识，全链路一致（INV-5）。 |
| `workspace_uri` | ⬜ | 产出与中间制品的存放位置（`TASK`/`JOB` 常需，`SYNC_CALL` 通常省略）。 |

---

## 8. 验收标准 (Acceptance Criteria)

> 采用 Given / When / Then。所有涉及多分支的判定，实现阶段须按 CLAUDE.md 用**表格驱动测试**覆盖。

* **AC-01（粒度必声明）**
  * Given 一个缺少 `interaction_mode` 或取值非法的创建请求
  * When 提交创建
  * Then 平台拒绝创建并返回明确错误（对应 INV-1）。

* **AC-02（SYNC_CALL 同步直通）**
  * Given 一个 `interaction_mode = SYNC_CALL` 的任务且目标 Agent 在线
  * When 平台投递
  * Then 平台同步返回结果，且该任务**未**在状态机中产生 `TODO/READY/RUNNING` 记录（对应 INV-3、REQ-01.3）。

* **AC-03（TASK 需认领）**
  * Given 一个 `interaction_mode = TASK` 的任务
  * When Agent 未认领即尝试提交结果
  * Then 平台拒绝该提交（对应 INV-3、REQ-03.1）。

* **AC-04（返回契约校验）**
  * Given 一个声明了 `output_contract`（必填 `status`,`result`）的任务
  * When Agent 返回缺少 `result` 字段
  * Then 平台判定任务失败并记录原因，不写入成功状态（对应 INV-4、REQ-04.3）。

* **AC-05（租户贯穿）**
  * Given 信封 `tenant_id = A`
  * When Agent 以 `tenant_id = B` 的身份提交结果
  * Then 平台拒绝（对应 INV-5）。

* **AC-06（委派深度溢出）**
  * Given 一条委派链 `delegation_depth` 已达 `max_delegation_depth`
  * When 再次派生子任务
  * Then 抛出 `DelegationDepthExceeded`，子任务不被创建（对应 INV-6）。

* **AC-07（入口注入围栏）**
  * Given 一个 `input` 中含被检出的注入指令的任务，且围栏策略为 enforce
  * When 平台投递前检查
  * Then 平台拒绝投递并记录安全事件（对应 INV-7、REQ-02.3）。

* **AC-08（出口脱敏围栏）**
  * Given Agent 返回结果中含被检出的敏感数据（如密钥）
  * When 平台执行出口围栏
  * Then 敏感片段被脱敏后再落库与回传（对应 INV-7、REQ-04.2）。

* **AC-09（截止即终止 / 裂脑防护）**
  * Given 一个已因 `deadline_ms` 到期被判超时失败的任务
  * When 原承接 Agent 在到期后提交结果
  * Then 平台拒绝该提交，不覆盖已判定状态（对应 INV-8、REQ-05.2）。

* **AC-10（未知 skill 拒绝）**
  * Given 信封 `skill` 不在承接 Agent 的能力声明中
  * When Agent 处理信封
  * Then Agent 显式拒绝承接（对应 REQ-03.2）。

---

## 9. 非功能需求

* **NFR-1（实时性）**：`SYNC_CALL` 端到端目标 P95 < 同步窗口，**默认 3s**（§13-D1 已定）；`TASK`/`JOB` 沿用既有异步 SLA。
* **NFR-2（安全围栏开销）**：执行期围栏引入的额外延迟须可度量，并纳入既有 benchmark 套件评估。
* **NFR-3（向后兼容）**：契约引入不得破坏既有 A2A V1 / V2 对外行为；未声明 `interaction_mode` 的旧客户端请求**默认按 `TASK` 处理**（§13-D2 已定）。
* **NFR-4（双引擎对齐）**：契约涉及的任何持久化字段须同时兼容 SQLite WAL 与 MySQL 方言（CLAUDE.md 铁律）。

---

## 10. 外部依赖与约束

* 复用既有模块的业务能力（非实现承诺）：状态机、Claim Lock、DTM 委派与 Scope 衰减、APE 检查点、PT 溯源、多租户隔离、Workspace。
* 实现层须遵守 CLAUDE.md：纯 dataclasses、双数据库方言对齐、APE Enforce 绑定、表格驱动测试。
* 安全事件须接入既有 `SecurityEventStore` / 审计链路（行为约定，实现待 plan）。

---

## 11. 残留风险（已知，进入实现前需关注）

> 决议项已迁移至 §13。以下为定稿后仍需在实现阶段持续关注的风险（非阻塞 spec 定稿）。

* **【风险 R1】** 三档粒度增加了承接路径分叉，若 SDK 封装不到位，反而加重 Agent 开发者认知负担——必须以脚手架“填空式”体验作为成功标准。
* **【风险 R2】** 执行期围栏（注入/脱敏）若同步阻塞在投递主路径，可能放大 `SYNC_CALL` 延迟——需在实现阶段评估异步/缓存策略。

---

## 12. 待补充设计（明确推迟，非本 spec 范围）

以下与本契约相邻、但不影响本 spec 内部自洽的设计，按 YAGNI 明确推迟，待各自独立功能域立项时处理：

* MCP 协议网关桥接（与 §7 Envelope 的字段映射）。
* 注入检测 / 脱敏的算法与模型选型（§8 仅定义行为）。
* `SYNC_CALL` 的同步直通在多副本部署下的负载与超时退避细节。

---

## 13. 决议记录 (Resolved Decisions)

> 本节记录 §11 原待确认项（Q1~Q5）的最终决议。决议已固化进正文相应条款，spec 据此定稿。

| 编号 | 原待确认项 | 最终决议 | 决议依据 |
| :--- | :--- | :--- | :--- |
| **D1** | `SYNC_CALL` 默认同步超时窗口 | **3s**（P95 目标），可由配置覆盖 | 同步交互体验上限；已写入 NFR-1。 |
| **D2** | 旧客户端未声明 `interaction_mode` | **默认按 `TASK` 处理** | 最小破坏既有 A2A V2 行为；已写入 NFR-3，与 INV-1 协同——“未声明”按默认值补全，而非视为非法。 |
| **D3** | 入口注入围栏的模式 | **复用 APE 既有 `audit / warn / enforce` 三态迁移**，不另造配置体系 | 极简原则；与既有安全治理同频，降低运维认知负担。 |
| **D4** | `output_contract` 校验强度 | **仅做必填字段（required）校验**，暂不引入完整 JSON Schema 校验 | YAGNI；INV-4 / AC-04 据此收敛为必填字段判定。完整 Schema 校验留作 §12 后续增强。 |
| **D5** | `SYNC_CALL` 是否参与委派链 / PT 溯源 | **参与**：`SYNC_CALL` 同样携带 `security_context` 并生成 provenance 记录，但不进入状态机 | 保证安全溯源全覆盖（INV-7），与 INV-3「不进状态机」并不冲突——溯源与状态机是两条独立链路。 |

> **说明（D2 与 INV-1 的协同）**：INV-1 要求“粒度必声明”指的是**经默认值补全后必须持有合法的 `interaction_mode`**；对未携带该字段的旧请求，平台先按 D2 补默认值 `TASK`，再进入后续校验。显式传入非法值（非三档之一）仍按 INV-1 拒绝。

---

*本 spec 遵循 ai-coding-best-practice 治理规范产出。§13 决议已确认，文档状态为 **已定稿 (Approved)**。按用户指示，本阶段到此为止，**不进入 plan.md（技术方案）阶段**。后续如需实现，应以本定稿 spec 为业务事实来源另行启动 plan。*
