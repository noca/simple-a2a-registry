# plan.md — Agent Runtime Contract 技术实现方案

> **文档状态**：草稿 — 待评审
> **来源**：docs/spec-agent-runtime-contract.md (SSOT)
> **日期**：2026/06/16
> **裁决顺序**：constitution.md > AGENTS.md > spec.md > 本 plan.md > tasks.md > 既有代码

---

## 目录

1. [差距分析 (Gap Analysis)](#1-差距分析)
2. [总体架构](#2-总体架构)
3. [模块分解](#3-模块分解)
4. [数据模型变更](#4-数据模型变更)
5. [API 变更](#5-api-变更)
6. [安全围栏设计](#6-安全围栏设计)
7. [迁移策略](#7-迁移策略)
8. [改动清单](#8-改动清单)
9. [性能考量 (NFR-2)](#9-性能考量)
10. [残留风险](#10-残留风险)

---

## 1. 差距分析

### 1.1 既有能力 (已实现、可复用)

| 模块 | 当前状态 | 复用方式 |
| :--- | :--- | :--- |
| Task 9 状态机 | `orchestration/models.py` + `state_machine.py` — 完整 | TASK/JOB 直接复用 |
| Claim Lock | `store.claim_task()` — 行级乐观锁, 15min TTL | 不变 |
| WebSocket 投递 | `dispatcher._dispatch_via_ws()` — 推送 task msg | 扩展为 Envelope 格式 |
| Callback 投递 | `dispatcher._dispatch_via_callback()` — HTTP POST | 扩展为 Envelope 格式 |
| DTM 委派管理 | `security/dtm.py` — 完整 | 复用并接入 Envelope SecurityContext |
| PT 溯源 | `security/pt.py` — 完整 | 复用 |
| APE 安全策略点 | `security/ape.py` — check_task_create/claim/complete | 新增 checkpoints（围栏、输出校验） |
| SecurityEventStore | `security/events.py` — 完整 | 围栏事件接入 |
| Agent 注册与 AgentCard | `registry_handler.py` + `models.py` — 完整 | 扩展 Skill Declaration |
| AgentSkill | `models.AgentSkill` — 已有 id/name/description/tags | 增加 input_schema/output_schema |

### 1.2 差距 (需新增/修改)

| 功能点 | 当前 | 目标 | 影响面 |
| :--- | :--- | :--- | :--- |
| **Interaction Mode** | Task 无粒度分类 | `SYNC_CALL`/`TASK`/`JOB` 三档 | 模型、DB schema、routes、dispatcher、client SDK |
| **Task Envelope** | 投递内容为扁平 dict (`{type,id,title,body,…}`) | 统一 7+2 字段 Envelope | 新模块 `contract.py`；dispatcher 投递逻辑 |
| **SYNC_CALL 同步路径** | 不走同步通路 | 新建 REST 端点 + 直通投递 | 新 route `POST /v2/sync-call`；WS 响应超时逻辑 |
| **Output Contract** | 无返回契约校验 | 必填字段校验 + 拒绝残缺结果 | 新校验模块；complete 路径加校验点 |
| **Security Context** | 字段散落在 Task 上 | 统一 `SecurityContext` 结构体；新增 `deadline_ms` | 模型新增 `dataclass`；Envelope 构建逻辑 |
| **Skill Declaration** | AgentSkill 缺少 schema | 增加 `input_schema`/`output_schema`；dispatch 时 skill 匹配 | AgentSkill 模型；dispatcher 投递前校验 |
| **入口围栏 (注入检测)** | 无 | 投递前对 `input` 执行注入检测 | 新模块 `guardrail.py`；APE checkpoint 或独立 filter |
| **出口围栏 (脱敏)** | 无 | 返回后对 `result` 执行敏感数据脱敏 | 同上；complete 路径加 filter |
| **Deadline 强制超时** | 无精确 deadline 机制 | `deadline_ms` 到期后自动裁定 FAILED | dispatcher poll cycle 增加 expiry check |
| **裂脑防护 (前任提交拒绝)** | 仅靠 claim_lock 行锁 | 增加 task_id + 状态 + 时间戳多维判定 | store.complete 路径增强 |
| **SDK 客户端适配** | 未感知 Envelope/粒度 | 新增 `SYNC_CALL` 发送、Envelope 解析、OutputContract 填充 | `client.py` 扩展 |
| **Skill 不明拒绝** | 无 | Agent 收到未知 skill 信封时显式拒绝 | SDK 客户端侧行为（服务端只做投递匹配） |

---

## 2. 总体架构

```
┌─────────────────────────────────────────────────────────┐
│                   分发方 (Assigner)                       │
│  POST /v2/tasks (interaction_mode, skill, output_contract)│
│  POST /v2/sync-call (同上, 但走同步路径)                  │
└──────────────┬──────────────────────────────────────────┘
               │
               ▼
┌─────────────────────────────────────────────────────────┐
│  1. Routes Layer (routes.py)                             │
│     ┌──────────────────┐  ┌───────────────────┐         │
│     │ APE Check:        │  │ Guardrail Entry:   │         │
│     │ create/claim/     │  │ injection detect   │         │
│     │ complete          │  │ on input           │         │
│     └────────┬─────────┘  └────────┬──────────┘         │
│              │                     │                      │
│              ▼                     ▼                      │
│  2. Envelope Builder (contract.py)                       │
│     ┌─────────────────────────────────────────────────┐ │
│     │ build_envelope(task) → TaskEnvelope              │ │
│     │   - security_context from DTM+PT                │ │
│     │   - output_contract from task.output_contract   │ │
│     └────────────────┬────────────────────────────────┘ │
│                      │                                   │
│                      ▼                                   │
│  3. Dispatch Layer  (dispatcher.py 扩展)                │
│     ┌────────────┬───────────────┬──────────────────┐   │
│     │ SYNC_CALL  │ TASK          │ JOB              │   │
│     │ → 同步直通  │ → 既有 WS/   │ → 同 TASK        │   │
│     │    Route    │ Callback 投递 │    + 子任务 DAG  │   │
│     └────────────┴───────────────┴──────────────────┘   │
│                      │                                   │
└──────────────────────┼───────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────┐
│                  承接 Agent (Worker)                      │
│  收到 TaskEnvelope → 解析 → skill routing → 执行 → 返回  │
└──────────────────────┬──────────────────────────────────┘
                       │  (返回结果)
                       ▼
┌─────────────────────────────────────────────────────────┐
│  4. Complete Path (routes.py + contract.py)             │
│     ┌──────────────────┐  ┌───────────────────┐        │
│     │ OutputContract    │  │ Guardrail Exit:   │        │
│     │ 校验 (必填字段)     │  │ 脱敏/泄露检测       │        │
│     └────────┬─────────┘  └────────┬──────────┘        │
│              │                     │                      │
│              ▼                     ▼                      │
│  5. Store Layer (store.py)                              │
│     ┌─────────────────────────────────────────────────┐ │
│     │ update_task_status() + 状态机推进                │ │
│     │ SYNC_CALL: 直接返回 (不进状态机)                  │ │
│     │ TASK/JOB:   状态机推进 → COMPLETED               │ │
│     └─────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────┘
```

### 2.1 核心原则

- **Entity/Action 分离**：Envelope 是数据实体，Dispatch/Validate/Guardrail 是动作
- **复用为先**：TASK 模式 ≈ 当前 kanban Task + 新 Envelope + 安全围栏；不重构既有行为
- **双引擎对齐**：所有 DB 变更同时产出 SQLite + MySQL 方言
- **dataclasses 禁止 Pydantic**：严格遵循 CLAUDE.md §2「关键代码规则」

---

## 3. 模块分解

### 3.1 模型层 — 新增 `simple_a2a_registry/contract/models.py`

#### 3.1.1 InteractionMode (enum)

```python
class InteractionMode(str, Enum):
    SYNC_CALL = "SYNC_CALL"   # 函数级、原子、同步返回
    TASK      = "TASK"        # 作业级、异步、需认领
    JOB       = "JOB"         # 项目级、可拆解子任务
```

#### 3.1.2 SecurityContext (dataclass)

```python
@dataclass
class SecurityContext:
    effective_scope: str = ""             # 当前有效 scope
    delegation_depth: int = 0             # 当前委派深度
    max_delegation_depth: int = 10        # 最大委派深度
    deadline_ms: int = 0                  # Unix 毫秒时间戳截止线
    provenance_chain_id: str = ""         # 溯源链 ID
```

#### 3.1.3 OutputContract (dataclass)

```python
@dataclass
class OutputContract:
    required_fields: List[str] = field(default_factory=list)  # 必填字段名列表
    
    def validate(self, result: dict) -> List[str]:
        """返回缺失的必填字段列表。空列表 = 校验通过。"""
        return [f for f in self.required_fields if f not in result]
```

D4 决议：**仅做必填字段 required 校验**，暂不引入完整 JSON Schema。

#### 3.1.4 TaskEnvelope (dataclass)

```python
@dataclass
class TaskEnvelope:
    task_id: str                                    # REQUIRED
    interaction_mode: str                           # REQUIRED: SYNC_CALL|TASK|JOB
    skill: str                                      # REQUIRED: 目标能力名
    input_schema: Optional[dict] = None             # OPTIONAL: 输入 Schema
    input: dict = field(default_factory=dict)       # REQUIRED: 任务输入
    output_contract: OutputContract = field(default_factory=OutputContract)  # REQUIRED
    security_context: SecurityContext = field(default_factory=SecurityContext)  # REQUIRED
    tenant_id: str = ""                             # REQUIRED
    workspace_uri: Optional[str] = None             # OPTIONAL: TASK/JOB 产出位置
```

#### 3.1.5 SkillDeclaration (dataclass) — 扩展 AgentSkill

```python
@dataclass
class SkillDeclaration:
    id: str                                         # REQUIRED: 能力标识
    name: str                                       # REQUIRED: 能力名称
    description: str = ""                           # REQUIRED: 能力描述
    input_schema: Optional[dict] = None             # OPTIONAL: 输入 Schema (JSON Schema)
    output_schema: Optional[dict] = None            # OPTIONAL: 输出 Schema 的 required 字段
```

**注意**：`SkillDeclaration` 不新建模型，而是**扩展既有 `AgentSkill`**。仅增加 `input_schema`/`output_schema` 两个可选字段。现有 AgentSkill 原有字段不变。

### 3.2 业务模块 — 新增 `simple_a2a_registry/contract/envelope.py`

```python
def build_envelope(
    task: Task,
    dtm: Optional[DelegatedTokenManager] = None,
    pt: Optional[ProvenanceTracker] = None,
) -> TaskEnvelope:
    """
    从 Task 模型构造 TaskEnvelope。
    
    1. 读取 task 的 interaction_mode, skill, input, output_contract
    2. 填充 SecurityContext:
       - effective_scope: 从 task.effective_scope 或 DTM 衰减结果
       - delegation_depth: task.delegation_depth
       - max_delegation_depth: task.max_delegation_depth 或默认 10
       - deadline_ms: task.deadline_ms (新字段)
       - provenance_chain_id: task.provenance_chain_id 或 PT 查找
    3. 返回完整 Envelope
    """

def serialize_envelope(envelope: TaskEnvelope) -> dict:
    """TaskEnvelope → dict (JSON 序列化)"""

def deserialize_envelope(data: dict) -> TaskEnvelope:
    """dict → TaskEnvelope (反序列化)"""
```

### 3.3 业务模块 — 新增 `simple_a2a_registry/contract/validate.py`

```python
def validate_output_contract(
    output_contract: Union[dict, OutputContract],
    result: dict,
) -> List[str]:
    """
    校验返回结果是否满足 output_contract。
    
    Args:
        output_contract: OutputContract dict 或 dataclass
        result: Agent 返回的结果 dict
    
    Returns:
        缺失的必填字段列表。空列表 = 校验通过。
    """

def validate_interaction_mode(mode: str) -> bool:
    """校验 interaction_mode 是否为合法三档之一。"""
```

### 3.4 业务模块 — 新增 `simple_a2a_registry/contract/guardrail.py`

```python
# ── 入口围栏 ──

INJECTION_PATTERNS: List[re.Pattern] = [
    # 系统指令注入标记 (placeholder — plan 不选型具体算法)
    re.compile(r'(?i)(ignore|disregard|override)\s+(all\s+)?(previous|prior)\s+(instructions)'),
    # Prompt 泄露标记
    re.compile(r'(?i)(reveal|output|print|show)\s+(your|the)\s+(prompt|system\s+prompt|instructions)'),
]

def check_input_guardrail(
    input_data: dict,
    patterns: Optional[List[re.Pattern]] = None,
) -> GuardrailResult:
    """
    入口围栏：对 input 中的自然语言字符串做注入检测。
    
    Args:
        input_data: 任务输入 dict
        patterns: 可覆盖检测模式，默认 INJECTION_PATTERNS
    
    Returns:
        GuardrailResult(hit: bool, matched_pattern: str | None, detail: str)
    """

# ── 出口围栏 ──

SENSITIVE_PATTERNS: List[re.Pattern] = [
    re.compile(r'(?i)(AKIA[0-9A-Z]{16})'),            # AWS Access Key
    re.compile(r'(?i)(sk-[a-zA-Z0-9]{32,})'),          # OpenAI / 类似 API Key
    re.compile(r'(?i)(-----BEGIN\s+(RSA\s+)?PRIVATE KEY-----)'),  # Private Key
    re.compile(r'(?i)(password\s*[:=]\s*["\']?[^"\'\s]+)'),       # Password leak
    re.compile(r'(?i)(token\s*[:=]\s*["\']?[a-zA-Z0-9_-]{16,})'), # Token leak
]

def redact_sensitive_data(
    result: dict,
    patterns: Optional[List[re.Pattern]] = None,
    redact_with: str = "***REDACTED***",
) -> tuple[dict, list[dict]]:
    """
    出口围栏：对返回结果递归扫描敏感数据并脱敏。
    
    Returns:
        (redacted_result, [{"pattern": str, "field_path": str, "severity": str}, ...])
    """

@dataclass
class GuardrailResult:
    hit: bool = False
    matched_pattern: Optional[str] = None
    detail: str = ""
    redactions: List[dict] = field(default_factory=list)
```

**算法选型**决议：plan 不做具体算法选型，以上仅为占位实现——基于正则的静态检测。后续可替换为 ML 模型（SGEL / 自编码器），通过策略配置切换。

### 3.5 Dispatcher 扩展 — 修改 `simple_a2a_registry/orchestration/dispatcher.py`

关键变更点：

#### 3.5.1 `_dispatch_single_task` 增加 Envelope 构建

```python
async def _dispatch_single_task(self, task: Task) -> int:
    # ... 现有 flow control gate ...
    
    # 构建 TaskEnvelope
    from simple_a2a_registry.contract.envelope import build_envelope
    from simple_a2a_registry.contract.guardrail import check_input_guardrail
    
    envelope = build_envelope(task, self.dtm, self.pt)
    
    # 入口围栏 (INV-7)
    guardrail_result = check_input_guardrail(envelope.input)
    if guardrail_result.hit:
        mode = self.ape.config.mode if self.ape else "enforce"
        if mode == "enforce":
            logger.warning("Entry guardrail blocked task %s: %s", task.id, guardrail_result.detail)
            self.store.update_task_status(task.id, TaskStatus.FAILED.value,
                result=f"Entry guardrail blocked: {guardrail_result.detail}")
            return 0
        elif mode == "warn":
            logger.warning("Entry guardrail WARN on task %s: %s", task.id, guardrail_result.detail)
            # 仍继续投递，但记录安全事件
    
    # 根据 interaction_mode 分发
    if envelope.interaction_mode == InteractionMode.SYNC_CALL.value:
        return await self._dispatch_sync_call(task, envelope)
    else:
        # TASK / JOB — 走既有 WS/Callback/WorkerCommand 路径
        # 但投递内容从扁平 dict 改为序列化的 Envelope
        ...
```

#### 3.5.2 新增 `_dispatch_sync_call` 同步投递

```python
async def _dispatch_sync_call(self, task: Task, envelope: TaskEnvelope) -> int:
    """
    SYNC_CALL 投递：不进状态机、不认领、直接投递并等待同步响应。
    
    流程:
    1. 目标 Agent 必须在 WS 在线 → 通过 WS 发送 Envelope
    2. 在任务级别的 asyncio.Event 上等待响应 (超时窗口 3s, 见 NFR-1 D1)
    3. 收到响应后执行出口围栏 + OutputContract 校验
    4. 结果通过 SSE / HTTP 回传给调用方
    5. 超时未响应 → 返回 SYNC_TIMEOUT 错误
    
    注意：此方法不操作 TaskStore 的状态机，仅通过 EventBus 回传结果。
    """
```

#### 3.5.3 Deadline 强制超时 — `deadline_ms` 检查

在 dispatcher poll cycle 中新增一步：

```python
async def _poll_cycle(self):
    # ... 现有 TTL Release, Retry Promotion ...
    
    # 新增: Deadline expiry — 检查 running task 的 deadline_ms 是否已过期
    try:
        deadline_expired = self.store.release_deadline_expired(tenant=self.config.tenant)
        if deadline_expired:
            logger.info("Deadline expiry: %d task(s) marked failed", deadline_expired)
        stats["deadline_expired"] = deadline_expired
    except Exception:
        logger.exception("Deadline expiry step failed")
    
    # ... Claim + Spawn ...
```

#### 3.5.4 Skill 匹配

在 `_dispatch_single_task` 入口增加 skill 与 Agent 能力声明的匹配检查：

```python
if self.registry_store:
    agent_card = self.registry_store.get_agent(task.assignee)
    if agent_card:
        declared_skills = agent_card.get("skills", [])
        declared_skill_ids = {s.get("id", "") for s in declared_skills}
        if task.skill and task.skill not in declared_skill_ids:
            logger.warning(
                "Skill mismatch: task '%s' requires skill '%s' but agent '%s' declares %s",
                task.id, task.skill, task.assignee, declared_skill_ids,
            )
            self.store.update_task_status(
                task.id, TaskStatus.FAILED.value,
                result=f"Agent '{task.assignee}' does not declare skill '{task.skill}'",
            )
            return 0
```

### 3.6 Store 扩展 — `simple_a2a_registry/orchestration/store.py`

#### 3.6.1 `release_deadline_expired()`

```python
def release_deadline_expired(self, tenant: Optional[str] = None) -> int:
    """
    扫描所有 running 任务，检查 deadline_ms 是否已过期。
    
    deadline_ms 为 Unix 毫秒时间戳截止线。当前时间 (毫秒) > deadline_ms 即视为过期。
    过期任务标记为 FAILED，记录超时原因。
    
    INV-8 保证：Agent 在到期后提交的结果不得覆盖已判定状态。
    """
```

#### 3.6.2 SYNC_CALL 创建 — 不进状态机

在 `create_task` 中增加条件：

```python
if interaction_mode == InteractionMode.SYNC_CALL.value:
    # SYNC_CALL 创建后不进入 TODO/READY 状态机
    task.status = "sync_pending"  # 新增过渡状态，仅用于同步等待
```

### 3.7 Routes 扩展 — `simple_a2a_registry/orchestration/routes.py`

#### 3.7.1 新增端点: `POST /v2/sync-call`

```python
async def handle_sync_call(self, request: web.Request) -> web.Response:
    """
    POST /v2/sync-call — 同步执行 SYNC_CALL。
    
    请求体:
    {
        "skill": "calculator",
        "input": {"expression": "1 + 1"},
        "output_contract": {"required_fields": ["result"]},
        "assignee": "my-agent",  # 可选，不指定则平台选择合适 Agent
        "timeout_ms": 3000       # 可选，默认 3000 (D1)
    }
    
    响应 (成功):
    {
        "status": "completed",
        "task_id": "t_xxx",
        "result": {"result": 2}
    }
    
    响应 (超时):
    {
        "status": "sync_timeout",
        "task_id": "t_xxx",
        "error": "Agent did not respond within 3000ms"
    }
    
    实现:
    1. 创建临时 Task 记录 (interaction_mode=SYNC_CALL, status=sync_pending)
    2. 构建 TaskEnvelope
    3. 入口围栏检查
    4. 投递到目标 Agent 的 WS 连接
    5. 在 asyncio.Event 上等待 timeout_ms
    6. 出口围栏 + OutputContract 校验
    7. 归档临时 Task 记录
    8. 返回结果或超时错误
    """
```

#### 3.7.2 扩展 `handle_create_task` — 增加 `interaction_mode` 等字段

当前 create 端点接受 `title, body, assignee, priority, parents, ...`。修改：

- 新增可选字段 `interaction_mode` (缺省按 D2 决议默认为 `TASK`)
- 新增可选字段 `skill` (路由到能力)
- 新增可选字段 `input` (JSON dict)
- 新增可选字段 `output_contract` (dict)
- 新增可选字段 `deadline_ms` (毫秒时间戳)

INV-1 校验：`interaction_mode` 缺省/默认补 `TASK`（D2 协同）；显式非法值拒绝创建。

```python
# 在 handle_create_task 末尾创建前:
interaction_mode = body.get("interaction_mode", TaskStatus.TASK.value)  # D2: 默认 TASK
if interaction_mode not in [m.value for m in InteractionMode]:
    return _json_error(400, "invalid_interaction_mode", 
        f"Must be one of: {', '.join(m.value for m in InteractionMode)}")

# 传给 create_task
task = self.store.create_task(
    ...,
    interaction_mode=interaction_mode,
    skill=body.get("skill"),
    input_data=body.get("input"),
    output_contract=body.get("output_contract"),
    deadline_ms=body.get("deadline_ms"),
)
```

#### 3.7.3 扩展 `handle_complete` — 出口围栏 + OutputContract 校验

```python
async def handle_complete(self, request: web.Request) -> web.Response:
    # ... 现有 APE checkpoint ...
    
    # 读取 output_contract
    task = self.store.get_task(task_id)
    if task and task.output_contract:
        output_contract = json.loads(task.output_contract)
        
        # 出口围栏
        redacted_result, redactions = redact_sensitive_data(result_dict)
        if redactions:
            logger.info("Exit guardrail redacted %d sensitive data in task %s", len(redactions), task_id)
            # 记录安全事件
            if self.event_store:
                self.event_store.record(
                    event_type="SENSITIVE_DATA_DETECTED",
                    actor=request.get("agent_id", "unknown"),
                    target=task.assignee or "",
                    decision="allow",
                    reason=f"Exit guardrail redacted {len(redactions)} item(s)",
                    task_id=task_id,
                )
        
        # OutputContract 校验
        required = output_contract.get("required_fields", [])
        missing = [f for f in required if f not in redacted_result]
        if missing:
            return _json_error(400, "output_contract_violation",
                f"Missing required fields: {', '.join(missing)}")
        
        result_str = json.dumps(redacted_result)  # 用脱敏后的结果
    else:
        result_str = json.dumps(result) if isinstance(result, dict) else str(result)
    
    # ... 继续现有 update_task_status ...
```

### 3.8 SDK 扩展 — `simple_a2a_registry/client.py`

#### 3.8.1 新增同步调用方法

```python
def sync_call(
    self,
    skill: str,
    input_data: dict,
    output_contract: Optional[dict] = None,
    assignee: Optional[str] = None,
    timeout_ms: int = 3000,
    tenant: Optional[str] = None,
) -> dict:
    """
    POST /v2/sync-call — 同步调用 Agent 能力。
    
    返回:
    {
        "status": "completed" | "sync_timeout",
        "task_id": "t_xxx",
        "result": {}  # completed 时存在
    }
    """

async def async_sync_call(
    self,
    skill: str,
    input_data: dict,
    output_contract: Optional[dict] = None,
    assignee: Optional[str] = None,
    timeout_ms: int = 3000,
    tenant: Optional[str] = None,
) -> dict:
    """async 版本的 sync_call。"""
```

#### 3.8.2 扩展 `dispatch_handler` — 接收 Envelope

当前 `dispatch_handler` 接收扁平 dict。扩展为自动解析 TaskEnvelope：

```python
def _on_ws_message(self, message: dict) -> None:
    if message.get("type") == "task":
        # 解析 Envelope
        from simple_a2a_registry.contract.envelope import deserialize_envelope
        envelope = deserialize_envelope(message)
        if self.dispatch_handler:
            self.dispatch_handler(envelope)
    elif message.get("type") == "sync_response":
        # SYNC_CALL 同步响应 — 触发 Event
        ...
```

#### 3.8.3 扩展 SDK 注册 — 声明 skill schemas

```python
def register_agent_with_skills(
    self,
    name: str,
    description: str,
    skills: List[dict],  # 含 input_schema/output_schema
    ...
) -> str:
    """注册 Agent 并声明能力 Schema。"""
```

### 3.9 AgentSkill 扩展 — `simple_a2a_registry/models.py`

为 `AgentSkill` 增加两个可选字段：

```python
@dataclass
class AgentSkill:
    id: str
    name: str
    description: str
    tags: List[str] = field(default_factory=list)
    examples: Optional[List[str]] = None
    input_modes: Optional[List[str]] = None
    output_modes: Optional[List[str]] = None
    security_requirements: Optional[List[SecurityRequirement]] = None
    uri_schemes: List[str] = field(default_factory=list)
    # ── 新增 ──
    input_schema: Optional[dict] = None    # JSON Schema 格式的输入描述
    output_schema: Optional[dict] = None   # JSON Schema 格式的输出描述
```

---

## 4. 数据模型变更

### 4.1 tasks 表 — SQLite

```sql
-- 新增列 (ALTER 迁移，非新建表)
ALTER TABLE tasks ADD COLUMN interaction_mode TEXT NOT NULL DEFAULT 'TASK';
ALTER TABLE tasks ADD COLUMN skill TEXT;
ALTER TABLE tasks ADD COLUMN input_data TEXT;       -- JSON blob
ALTER TABLE tasks ADD COLUMN output_contract TEXT;  -- JSON blob
ALTER TABLE tasks ADD COLUMN deadline_ms INTEGER;
```

### 4.2 tasks 表 — MySQL

```sql
ALTER TABLE tasks ADD COLUMN interaction_mode VARCHAR(20) NOT NULL DEFAULT 'TASK';
ALTER TABLE tasks ADD COLUMN skill VARCHAR(255);
ALTER TABLE tasks ADD COLUMN input_data TEXT;       -- JSON blob
ALTER TABLE tasks ADD COLUMN output_contract TEXT;  -- JSON blob
ALTER TABLE tasks ADD COLUMN deadline_ms BIGINT;
```

### 4.3 迁移脚本

新增 `migrations/versions/` 下的 Alembic 迁移文件。

### 4.4 新数据关系

```
Task (扩展)
├── interaction_mode: str          # SYNC_CALL | TASK | JOB
├── skill: str                      # 目标能力名
├── input_data: JSON (str)         # 任务输入
├── output_contract: JSON (str)    # 返回契约
├── deadline_ms: int               # Unix 毫秒截止线
└── (现有字段复用: security_context 用既有 delegation_depth, effective_scope)

AgentSkill (扩展)
├── input_schema: JSON (Optional)  # 输入 Schema
└── output_schema: JSON (Optional) # 输出 Schema
```

---

## 5. API 变更

### 5.1 新端点

| 方法 | 路径 | 说明 | 章节 |
| :--- | :--- | :--- | :--- |
| POST | `/v2/sync-call` | 同步调用 Agent 能力 | §3.7.1 |

### 5.2 既有端点变更

| 端点 | 变更内容 |
| :--- | :--- |
| POST `/v2/tasks` | 新增 request 字段: `interaction_mode`, `skill`, `input`, `output_contract`, `deadline_ms` |
| POST `/v2/tasks/{id}/complete` | 新增出口围栏 + OutputContract 校验 |

### 5.3 WebSocket 消息格式变更

`v2/tasks` WebSocket 投递消息体从扁平 dict 升级为 TaskEnvelope 格式：

```json
{
    "type": "task",
    "envelope": {
        "task_id": "t_xxx",
        "interaction_mode": "TASK",
        "skill": "code-review",
        "input": {"repo": "org/repo", "pr": 42},
        "output_contract": {"required_fields": ["summary", "issues"]},
        "security_context": {
            "effective_scope": "task:read task:write",
            "delegation_depth": 1,
            "max_delegation_depth": 10,
            "deadline_ms": 1700000000000,
            "provenance_chain_id": "t_yyy"
        },
        "tenant_id": "acme-corp",
        "workspace_uri": "/data/workspaces/t_xxx"
    }
}
```

---

## 6. 安全围栏设计

### 6.1 配置体系

复用 APE 既有 `audit / warn / enforce` 三态迁移 (D3 决议)，通过 `APEConfig` 扩展：

```python
@dataclass
class APEConfig:
    mode: str = "warn"                # audit | warn | enforce
    
    # ── 入口围栏注入检测 ──
    injection_detection: bool = True
    injection_detection_mode: str = ""  # 为空时继承 mode
    
    # ── 出口围栏脱敏 ──
    sensitive_data_redaction: bool = True
    sensitive_data_redaction_mode: str = ""  # 为空时继承 mode
    
    # ── 既有字段 ──
    default_delegation_policy: str = "open"
    max_delegation_depth: int = 10
```

### 6.2 围栏调用链

```
入口 (投递前):
  Routes
    → APE check_task_create (既有) 
    → Envelope 构建
    → Guardrail check_input (新增) 
        ├─ hit + enforce → 拒绝创建, 记录 SecurityEvent
        ├─ hit + warn    → 允许创建, 记录 SecurityEvent + X-Security-Warning header
        └─ no hit        → 正常投递

出口 (返回后):
  Routes handle_complete
    → APE check_task_complete (既有)
    → Guardrail redact_sensitive_data (新增)
        ├─ 脱敏结果
        └─ 记录 SecurityEvent (敏感数据被脱敏)
    → OutputContract validate (新增)
        ├─ 缺字段 → 400 output_contract_violation, 判定 FAILED
        └─ 通过   → 正常写入 COMPLETED
```

### 6.3 性能代价控制 (NFR-2)

- 围栏检查同步阻塞在投递/完成主路径（R2 风险）
- 引入 `timer` 埋点：每次围栏调用记录 `guardrail_duration_ms` 到 metrics
- 集成到既有 benchmark: `tests/benchmarks/test_security_overhead.py`
- 建议 P99 guardrail latency < 50ms（纯正则模式）或 < 200ms（含 ML 模型）

---

## 7. 迁移策略

### 7.1 兼容性保证 (NFR-3)

| 场景 | 处理方式 | 依据 |
| :--- | :--- | :--- |
| 旧客户端创建任务（无 `interaction_mode`） | 默认补 `TASK` | D2 决议 |
| 旧客户端完成（无 `output_contract`） | 跳过 OutputContract 校验 | 向后兼容 |
| 旧 AgentSkill 无 `input_schema` | 视为接受任意输入 | 向后兼容 |
| 旧 Agent 收到新 Envelope | SDK 客户端自动解析，兼容旧扁平 dict | 兼容 |

### 7.2 分阶段实施建议

**阶段 1 (核心模型 + Envelope + TASK 增强)**
- `contract/models.py`: InteractionMode, SecurityContext, OutputContract, TaskEnvelope
- 扩展 `AgentSkill` 加 `input_schema`/`output_schema`
- 扩展 routes `handle_create_task`: 接受新字段
- 扩展 store `create_task`: 持久化新字段
- DB 迁移脚本

**阶段 2 (SYNC_CALL 同步路径)**
- `POST /v2/sync-call` 新端点
- `dispatcher._dispatch_sync_call()` 实现
- Client SDK `sync_call()` / `async_sync_call()`
- SYNC_CALL 超时处理

**阶段 3 (安全围栏)**
- `contract/guardrail.py`: 入口注入检测 + 出口脱敏
- 接入 handle_complete: OutputContract 校验 + 出口围栏
- 接入 dispatcher: 入口围栏
- 安全事件接入

**阶段 4 (增强 TASK + JOB)**
- Dispatcher skill 匹配检查
- Deadline 强制超时 (`release_deadline_expired`)
- 裂脑防护强化 (deadline 后拒绝)
- Dispatcher 投递任务扁平 dict → Envelope 格式
- Benchmark 集成

---

## 8. 改动清单

### 8.1 新增文件

| 文件路径 | 职责 | 估计行数 |
| :--- | :--- | :--- |
| `simple_a2a_registry/contract/__init__.py` | 包初始 | 5 |
| `simple_a2a_registry/contract/models.py` | InteractionMode, SecurityContext, OutputContract, TaskEnvelope | 150 |
| `simple_a2a_registry/contract/envelope.py` | build_envelope(), serialize/deserialize | 100 |
| `simple_a2a_registry/contract/validate.py` | validate_output_contract(), validate_interaction_mode() | 80 |
| `simple_a2a_registry/contract/guardrail.py` | check_input_guardrail(), redact_sensitive_data() | 200 |
| `tests/test_contract_models.py` | 模型 dataclass 测试 | 100 |
| `tests/test_contract_envelope.py` | Envelope 构建/序列化测试 | 100 |
| `tests/test_contract_validate.py` | OutputContract 校验测试 | 80 |
| `tests/test_contract_guardrail.py` | 围栏测试 | 120 |
| `tests/test_sync_call.py` | SYNC_CALL 端到端测试 | 150 |
| `tests/benchmarks/test_guardrail_overhead.py` | 围栏性能基准 | 80 |
| `migrations/versions/xxxx_add_runtime_contract_fields.py` | DB 迁移 | 80 |

**合计新增：~1,245 行**

### 8.2 修改文件

| 文件路径 | 修改内容 | 估计改行数 |
| :--- | :--- | :--- |
| `simple_a2a_registry/models.py` | AgentSkill 增加 input_schema/output_schema | +20 |
| `simple_a2a_registry/orchestration/models.py` | Task 增加 interaction_mode, skill, input_data, output_contract, deadline_ms | +30 |
| `simple_a2a_registry/orchestration/store.py` | create_task 增加新字段; release_deadline_expired(); 迁移 ALTER | +120 |
| `simple_a2a_registry/orchestration/routes.py` | handle_create_task 扩展; handle_complete 加围栏+校验; 新增 sync-call | +200 |
| `simple_a2a_registry/orchestration/dispatcher.py` | _dispatch_single_task 加 Envelope+skill+围栏; 新增 _dispatch_sync_call | +180 |
| `simple_a2a_registry/client.py` | sync_call()/async_sync_call(); _on_ws_message 解析 Envelope; 注册扩展 | +150 |
| `simple_a2a_registry/security/ape.py` | APEConfig 增加围栏策略字段 | +10 |
| `simple_a2a_registry/security/events.py` | SecurityEventType 增加 SENSITIVE_DATA_DETECTED 等 | +10 |
| `simple_a2a_registry/orchestration/workflow.py` | JOB 子任务 DAG 分解逻辑 | +100 |
| `simple_a2a_registry/orchestration/state_machine.py` | 增加 sync_pending 过渡状态 | +5 |
| `tests/test_orchestration_store.py` | 新字段创建/查询测试 | +80 |
| `tests/test_orchestration_api.py` | 新端点集成测试 | +100 |

**合计修改：~1,005 行**

### 8.3 总计

**新增文件：12 个** | **修改文件：12 个** | **总计约 2,250 行**

---

## 9. 性能考量 (NFR-2)

### 9.1 围栏开销

| 操作 | 预期耗时 | 说明 |
| :--- | :---: | :--- |
| 入口注入检测 (正则, 100KB input) | < 10ms | 纯字符串模式匹配 |
| 入口注入检测 (ML 模型) | < 200ms | 视模型推理而定 |
| 出口脱敏 (正则, 100KB result) | < 15ms | 需递归扫描 dict |
| OutputContract 校验 | < 1ms | 仅集合差集操作 |

### 9.2 SYNC_CALL 延迟预算 (P95 < 3s)

```
[分发方] → API Gateway → [Routes] → [Guardrail 入口] → [WS 投递] → [Agent 执行] → [WS 返回] → [Guardrail 出口] → [OutputContract] → [分发方]
  5ms         5ms           10ms           5ms              200-2000ms        5ms             10ms                1ms           5ms
                                                                                               └──────────────────────────────────┘
                                                                                               P95 总预算: < 3,000ms
```

### 9.3 Benchmark 集成

`tests/benchmarks/test_guardrail_overhead.py`：

```python
def test_guardrail_latency_under_load(benchmark):
    """测量围栏在高负载下的 P95 延迟"""
    result = benchmark(check_input_guardrail, large_input)
    assert result < 50  # P95 < 50ms
```

现有 `tests/benchmarks/test_security_overhead.py` 中扩展围栏场景。

---

## 10. 残留风险 (已知，实现中需关注)

| 风险 | 影响 | 缓解措施 |
| :--- | :--- | :--- |
| **R1 (来源 spec §11 R1)**：三档粒度增加承接路径分叉，SDK 封装不到位加重 Agent 认知负担 | 开发者体验 | SDK 提供 `fill_envelope()` 填充式 API；脚手架模板 |
| **R2 (来源 spec §11 R2)**：围栏同步阻塞在投递主路径，放大 SYNC_CALL 延迟 | P95 < 3s 达标 | 阶段 1 正则同步实现；后续可异步化/缓存/并行 |
| **R3**：SYNC_CALL 在多副本部署下的并发冲突 | 返回值错乱 | SYNC_CALL 临时 Task 加 claim_lock 类似的 per-call token 绑定 |
| **R4**：output_contract 校验仅 required fields 阶段 (D4)，后续需升级为完整 JSON Schema | 业务扩展 | OutputContract 保留 `schema` 可选字段，阶段 2 开放 |
| **R5**：旧 Agent 不识别 Envelope 新格式 | 投递失败 | `agent_version` 协商：旧 Agent 继续收扁平 dict，新 Agent 收 Envelope |

---