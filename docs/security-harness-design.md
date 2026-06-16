# 多 Agent 协作安全执行 Harness 框架设计

> **版本**: v1.0  
> **日期**: 2026-06-09  
> **背景**: 基于 Kanban 多 Agent 人机协作运维效率优化研究项目 — simple-a2a-registry 安全扩展

---

## 一、问题定义

### 1.1 单 Agent HITL 安全的边界

在当前 simple-a2a-registry 中，单 Agent 与人类协作（HITL）的安全机制已经相对完善：

| 安全场景 | 当前保护 | 有效性 |
|---------|---------|-------|
| Token 窃取 | JWT RS256 签名 + 过期 + JWKS 验证 | ✅ |
| 未授权 API 调用 | `require_scope` + AuthMiddleware 强制 | ✅ |
| 任务劫持 | claim_lock 原子锁 + 匹配检查 | ✅ |
| 并发安全 | 原子 SQL + RLock | ✅ |
| 审计追踪 | append-only audit log + task_events | ✅ |
| 输入攻击 | body size limit + 路径参数正则 | ✅ |

### 1.2 多 Agent A2A 的安全鸿沟

当引入 A2A 协议级 Agent 间协作时，出现 **四类未覆盖的安全场景**：

```
场景 A: 越权委派
  Agent-A (task:read) ──创建任务──→ Agent-B (可执行任何操作)
  问题: Agent-A 的 read-only scope 未传播，Agent-B 以自身权限执行写操作

场景 B: 身份伪造
  Agent-A ──POST /v2/tasks {created_by: "admin"}──→ Registry
  问题: created_by 来自请求体而非 token，可伪造

场景 C: 无追溯链
  Agent-A → 任务给 Agent-B → Agent-B 创建子任务给 Agent-C
  问题: Agent-C 无法知道最终委托人是谁，审计断链

场景 D: 无委托深度控制
  Agent-A → Agent-B → Agent-C → Agent-D → ...（无限递归）
  问题: 无 max_delegation_depth 限制，可能形成递归攻击
```

### 1.3 核心命题

> **如何在 A2A 协议框架下，构建一个可独立审计的安全执行层，使得 Agent 间的任务委派始终在发起者的授权边界内执行，且任何安全决策都可被事后归因和验证？**

---

## 二、研究基础综述

### 2.1 A2A 协议的安全定位

A2A v1.0 协议 [Section 13 Security Considerations] 明确了安全边界：

- **协议明确要求的**: Data Access and Authorization Scoping（13.1）、Push Notification Security（13.2）、Extended Agent Card Access Control（13.3）、Transport Security（13.4）
- **协议明确不规定的**: "Authorization models are agent-defined" — 具体的委托模型、scope 传播、权限级别均由实现者自行定义
- **协议定位**: A2A 是通信协议而非安全框架，securitySchemes 仅声明认证方式，不规定授权策略

这意味 simple-a2a-registry 作为 A2A Registry，**有空间也有责任** 填补认证之上的授权层。

### 2.2 关键研究成果

#### 论文 1: Grimlock — eBPF Agent Guard（arXiv:2605.27488）

> "identity, authorization, provenance, and delegation are often pushed into application code, where they become difficult to enforce consistently and difficult to audit."

| Grimlock 核心机制 | 本框架映射 |
|------------------|-----------|
| eBPF-enforced traffic interception | Registry 中间件层的请求拦截 |
| TLS 1.3 channel bindings | JWT claim_binding (channel_id) |
| Short-lived scope tokens | DelegatedTaskToken（短有效期） |
| Destination-side re-validation | 任务 claim 时的 scope 验签 |
| Least-privilege delegation | Scope attenuation + call chain |

#### 论文 2: Authorization-Execution Gap（arXiv:2605.11003）

> 提出 AEG（授权-执行鸿沟）的三类结构性来源：
> 1. **Delegation-level incompleteness** — 委托时 scope 描述不完整或未传递
> 2. **Channel-level corruption** — 通信通道被篡改或劫持
> 3. **Composition-level fragmentation** — 多层委托后权限碎片化

**方法论结论**: 必须运行时拦截而非仅靠前置过滤或事后审计。

#### 论文 3: RFC 8693 OAuth 2.0 Token Exchange

OAuth 2.0 代理交换模式（on-behalf-of）：
- 中间服务用 incoming token + 自身份 → 交换一个 delegated token
- 支持 `scope`（授权收缩）、`audience`（目标资源）、`actor_token`（原始委托人）
- 直接可映射到 Agent A → Registry → Agent B 的委托场景

### 2.3 现有安全能力的完整差距分析

已在 12 个核心模块中扫描出以下安全缺口（完整报告见代码库分析）：

| # | 缺口 | 严重度 | 来源 |
|---|------|-------|------|
| 1 | `created_by` 来自请求体而非 token | **严重** | orchestration/routes.py, store.py |
| 2 | 无 Agent→Agent 授权矩阵 | **严重** | store.py (agent 注册表) |
| 3 | 无委托链追踪字段 | **高** | orchestration/models.py |
| 4 | 缺少 `on_task_pre_dispatch` 钩子 | **高** | plugin.py |
| 5 | 分派器未检查 agent disabled 状态 | **中** | orchestration/dispatcher.py |
| 6 | 无 scope 衰减传播机制 | **高** | auth.py（仅全局 scope） |
| 7 | 无 action-level scope 验证 | **中** | orchestration/routes.py |
| 8 | 无委托深度限制 | **高** | orchestration/store.py |

---

## 三、Harness 架构总览

### 3.1 设计原则

| 原则 | 含义 |
|------|------|
| **层次化** | Harness 是 Registry 中间层，不侵入 Agent 运行时 |
| **不可绕过** | 关键策略点在 middleware/dispatcher 中强制，非可选 |
| **可审计** | 每个安全决策产生可验证的审计证据 |
| **渐进衰减** | Scope 在委托链中只缩不扩（Principle of Least Authority） |
| **来源追溯** | 每个任务携带完整的委托链信息 |
| **插件可扩展** | 通过 plugin hook 支持自定义策略引擎 |

### 3.2 三层架构

```
                     ┌─────────────────────────────────────────┐
                     │          Agent / Human Client           │
                     └────────────────┬────────────────────────┘
                                      │  A2A HTTP / WS
                     ┌────────────────▼────────────────────────┐
                     │         Layer 1: Auth Middleware         │
                     │  (现有 OAuth 2.1 + session auth)        │
                     │  提取: agent_id, scopes, tenant         │
                     └────────────────┬────────────────────────┘
                                      │ 注入到 request
                     ┌────────────────▼────────────────────────┐
                     │    Layer 2: Security Harness (新增)     │
                     │                                          │
                     │  ┌────────────────────────────────────┐  │
                     │  │ Authorization Policy Engine (APE)  │  │
                     │  │ - Caller validation (identity)     │  │
                     │  │ - Scope checking (permission)      │  │
                     │  │ - Tenant isolation                 │  │
                     │  │ - Delegation depth control         │  │
                     │  └────────────────────────────────────┘  │
                     │                                          │
                     │  ┌────────────────────────────────────┐  │
                     │  │ Delegation Token Manager (DTM)     │  │
                     │  │ - Token minting (JWT)              │  │
                     │  │ - Scope attenuation                │  │
                     │  │ - Call chain propagation           │  │
                     │  └────────────────────────────────────┘  │
                     │                                          │
                     │  ┌────────────────────────────────────┐  │
                     │  │ Provenance Tracker (PT)            │  │
                     │  │ - Task lineage DAG                 │  │
                     │  │ - Origin agent resolution          │  │
                     │  │ - Security event graph             │  │
                     │  └────────────────────────────────────┘  │
                     └────────────────┬────────────────────────┘
                                      │ 已验证的 request
                     ┌────────────────▼────────────────────────┐
                     │         Layer 3: Business Logic         │
                     │   (V1 agent registry / V2 orchestration)│
                     └─────────────────────────────────────────┘
```

### 3.3 组件通信流

```
请求流（V2 任务创建）:

Client/Agent-A ───→ Auth Middleware
                         │ 解析 JWT → {sub: "agent-a", scope: "task:write", tenant: "t1"}
                         ▼
                    APE.validate_create_task(payload, identity)
                         │ 检查: agent-a 是否已注册?
                         │ 检查: assignee (agent-b) 是否存在且 active?
                         │ 检查: task:write scope 是否匹配?
                         │ 检查: tenant 一致性?
                         │ 检查: 委托深度 (非首次则检查 parent_depth)
                         ▼
                    DTM.mint_delegation_token(identity, task, chain)
                         │ 生成 DelegatedTaskToken: JWT {iss, sub, scope_attenuated,
                         │   chain=[...], tenant, exp, jti}
                         ▼
                    TaskStore.create_task(task, delegation_token)
                         │ 存储: task + delegation_token_hash
                         ▼
                    PT.record_event("TASK_CREATE", task_id, chain)
                         │ 写入 provenance graph
                         ▼
                    Agent-B 收到任务 (via WS / pool / callback)
                         [携带 DelegatedTaskToken]
```

---

## 四、核心组件详细设计

### 4.1 授权策略引擎 (APE)

#### 4.1.1 校验点矩阵

| 端点 | 拦截点 | 校验内容 |
|------|-------|---------|
| `POST /v2/tasks` (创建) | routes.py 入口 | ① caller identity ≠ 伪造 ② assignee 存在且 active ③ scope 覆盖 ④ tenant 一致 ⑤ 委托深度 < max |
| `POST /v2/tasks/{id}/claim` | routes.py + store.py | ① caller = assignee ② delegation_token 匹配 ③ 令牌未过期 |
| `POST /v2/tasks/{id}/complete` | routes.py + store.py | ① claim_lock 匹配 ② caller = claimer |
| `POST /v1/agents` (注册) | routes.py | ① caller 有 agent:register ② 不重复注册 |
| `POST /v1/agents/{id}/dispatch` | routes.py | ① caller 有权操作该 agent |

#### 4.1.2 Caller Identity Resolution

**核心变更**: `created_by` 必须从 JWT token 的 `sub` 字段推导，不再接受客户端请求体传入。

```python
# 当前（不安全）:
task_data = await request.json()
created_by = task_data.get("created_by", "anonymous")  # 可伪造

# 新（强制推导）:
token_payload = request["token_payload"]
caller_id = token_payload["sub"]  # 从 JWT sub 强制提取
tenant = token_payload.get("tenant", request.headers.get("X-Tenant-ID", ""))
```

#### 4.1.3 Agent-to-Agent Authorization Matrix

新增 `agent_authorizations` 表定义哪些 Agent 可以委派任务给哪些 Agent：

```sql
CREATE TABLE agent_authorizations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_agent_id TEXT NOT NULL,     -- 授权方（谁可以委派）
    target_agent_id TEXT NOT NULL,     -- 被授权方（谁可以接收）
    allowed_actions TEXT NOT NULL,     -- 允许的动作类别（JSON array: ["*", "code_review", "deploy"]）
    max_depth INTEGER DEFAULT 5,       -- 最大委托深度
    scope_restriction TEXT,            -- scope 收缩规则（JSON: 传入 scope 的缩减表达）
    tenant_id TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMP,              -- 授权过期时间
    UNIQUE(source_agent_id, target_agent_id, tenant_id)
);
```

授权规则匹配逻辑：

```
Agent-A → 创建任务 → Agent-B:
  1. 查 agent_authorizations WHERE source=agent-a, target=agent-b
  2. 如果无记录 → 默认拒绝（仅 super-admin 可以无约束委派）
  3. 如果有记录 → 检查 allowed_actions 是否包含任务动作
  4. 如果包含 → 根据 scope_restriction 衰减 scope
  5. 如果不包含 → 拒绝，返回 403
```

**默认策略（开放阶段/生产阶段）**:

| 模式 | 策略 | 配置项 |
|------|------|--------|
| Dev | 允许所有委派（向后兼容） | `auth.delegation_policy: open` |
| Prod | 仅允许明确授权的委派 | `auth.delegation_policy: restricted` |
| Custom | 通过 plugin hook 自定义 | `plugins.policy_engine` |

### 4.2 委托令牌管理器 (DTM)

#### 4.2.1 DelegatedTaskToken 数据结构

```python
@dataclass
class DelegatedTaskToken:
    """委托任务令牌 — 随任务在 Agent 间传播的授权凭证"""
    # 令牌元数据
    jti: str                    # 唯一令牌 ID（防重放）
    iss: str                    # 签发者 = Registry 自身 ID
    sub: str                    # 当前操作者（当前 Agent ID）
    iat: datetime               # 签发时间
    exp: datetime               # 过期时间（默认 5min）

    # 委托链
    origin_agent: str           # 原始发起者（最初创建任务的 Agent/Human）
    origin_tenant: str          # 原始租户
    delegation_chain: list[dict]  # 完整委托链 [{agent, action, scope, timestamp}, ...]

    # 授权边界
    effective_scope: str        # 当前有效 scope（空格分隔，与 OAuth scope 格式一致）
    attenuated_from: str        # 父级 scope（审计用）
    max_depth: int              # 最大深度
    depth: int                  # 当前深度

    # 约束
    allowed_callees: list[str] | None  # 允许委托的下级 Agent（None=无限制）
    task_id: str                # 绑定的任务 ID
```

#### 4.2.2 Scope 衰减规则

Scope 在委托链中只衰减不扩张。衰减规则定义在 `agent_authorizations.scope_restriction`：

```
衰减示例:

原始 scope:  "task:read task:write agent:read"
    ↓
Agent-A → Agent-B 的 scope_restriction: {"exclude": ["agent:read"]}
    ↓
衰减后 scope: "task:read task:write"
    ↓
Agent-B → Agent-C 的 scope_restriction: {"reduce_to": ["task:read"]}
    ↓
衰减后 scope: "task:read"
```

**守卫规则**（在 DTM.mint_delegation_token 中强制）：

```python
def _attenuate_scope(parent_scope: str, restriction: dict | None) -> str:
    """
    attenuation_rules:
      {"exclude": ["agent:read"]}      → 从父 scope 中移除指定 scope
      {"reduce_to": ["task:read"]}     → 保留交集
      {"map": {"admin": "read"}}       → scope 降级映射
      None / {}                         → 继承父 scope（不收缩）
    """
    parent_scopes = set(parent_scope.split())
    if restriction is None:
        return parent_scope  # 不收缩
    if "exclude" in restriction:
        return " ".join(parent_scopes - set(restriction["exclude"]))
    if "reduce_to" in restriction:
        allowed = set(restriction["reduce_to"])
        return " ".join(parent_scopes & allowed)  # 取交集
    if "map" in restriction:
        mapped = set()
        for s in parent_scopes:
            mapped.add(restriction["map"].get(s, s))
        return " ".join(mapped)
    return parent_scope
```

#### 4.2.3 令牌传输机制

```
任务创建时:
  Registry 内部生成 DelegatedTaskToken
    → 在 Task 模型中存储 token_hash（SHA256(jti + sub + scope)）
    → 在任务分发时通过 WS/HTTP 携带完整 token（JWT 格式）
    → 接收方 Agent 在 claim 时提供 token 给 Registry 验证

验证流程:
  Agent-B claim 任务时提供 DelegatedTaskToken JWT
    → Registry 验证: signature (RS256), exp, jti 未重复使用
    → 验证: token.task_id == claimed_task_id
    → 验证: token.sub == Agent-B's client_id
    → 原始委托人从 token.origin_agent 恢复
```

### 4.3 来源追踪器 (PT)

#### 4.3.1 委托链数据结构

扩展 `orchestration/models.py` 中的任务模型：

```python
@dataclass
class ProvenanceChain:
    """任务委托链 — 附在每个任务上的完整溯源信息"""
    chain_id: str                       # 链 ID（从根任务到叶任务共享）
    origin_agent: str                   # 原始发起者
    origin_tenant: str                  # 原始租户
    root_task_id: str                   # 根任务 ID（整棵 DAG 的起点）
    parent_task_id: str | None          # 父任务 ID（直接上级）
    depth: int                          # 当前深度（根=0）
    hops: list[ProvenanceHop]           # 历史跳转记录（不可变追加）
    delegation_token_hash: str          # 授权令牌哈希

@dataclass
class ProvenanceHop:
    """委托跳转 — 每次 Agent→Agent 传递的审计记录"""
    from_agent: str
    to_agent: str
    action: str                         # 创建的任务标题/动作
    scope_at: str                       # 当时的有效 scope
    timestamp: datetime
    token_jti: str                      # 使用的 DelegatedTaskToken ID
```

#### 4.3.2 可视化委托链

```
根任务 (origin: user-alice, scope: "*")  depth=0
  │ 委托给 Agent-B (衰减后 scope: "task:* agent:read")
  ▼
子任务-1 (created_by: agent-b, scope: "task:* agent:read")  depth=1
  │ 委托给 Agent-C (衰减后 scope: "task:read")
  ▼
子任务-2 (created_by: agent-c, scope: "task:read")  depth=2
  └── 执行 agent-c 只能在 task:read 范围内操作
```

### 4.4 插件钩子扩展

新增 4 个安全相关钩子，使安全策略引擎可通过 plugin 扩展：

```python
class Plugin(ABC):
    """插件基类 — 新增安全钩子"""

    # === 现有钩子 ===
    async def before_request(self, request): ...
    async def after_request(self, request, response): ...
    async def on_task_created(self, task): ...
    async def on_task_completed(self, task): ...

    # === 新增安全决策钩子 [v1.1] ===

    @hook_point
    async def authorize_task_create(
        self,
        caller: AgentIdentity,
        task_data: dict,
        delegation_chain: list | None,
    ) -> AuthzDecision:
        """
        任务创建决策点 — 钩子返回 AuthzDecision
        返回 ACCEPT → 继续
        返回 REJECT(reason) → 立即拒绝，403
        返回 DEFER → 由 APE 默认策略决定
        """
        return AuthzDecision.DEFER

    @hook_point
    async def authorize_task_claim(
        self,
        claimer: AgentIdentity,
        task: Task,
        delegation_token: DelegatedTaskToken | None,
    ) -> AuthzDecision:
        """任务 Claim 决策点"""
        return AuthzDecision.DEFER

    @hook_point
    async def authorize_dispatch(
        self,
        task: Task,
        assignee: AgentIdentity,
        dispatch_path: str,  # "ws" | "pool" | "callback"
    ) -> AuthzDecision:
        """分发前决策点"""
        return AuthzDecision.DEFER

    @hook_point
    async def on_security_event(
        self,
        event: SecurityEvent,
    ):
        """安全事件通知（异步，不阻止）"""
        pass


@dataclass
class AuthzDecision:
    """授权决策结果"""
    class Outcome(Enum):
        ACCEPT = "accept"
        REJECT = "reject"
        DEFER = "defer"        # 交回默认策略

    outcome: Outcome
    reason: str = ""
    override_scope: str | None = None  # 可覆盖任务 scope
```

### 4.5 安全事件系统

统一安全事件类型（新增 `security_event` 类型字段，与 audit 和 task_events 集成）：

```python
@dataclass
class SecurityEvent:
    """安全事件 — 所有安全决策的统一审计记录"""
    event_id: str
    event_type: SecurityEventType
    timestamp: datetime
    actor: str                  # 触发者 agent_id
    target: str                 # 作用对象（task_id / agent_id）
    tenant: str
    decision: str               # "allow" | "deny" | "block"
    reason: str                 # 决策原因
    scope_used: str             # 使用的 scope
    delegation_chain: list | None  # 委托链快照
    metadata: dict              # 扩展信息

class SecurityEventType(Enum):
    # 认证事件
    AUTH_SUCCESS = "auth_success"
    AUTH_FAILURE = "auth_failure"

    # 授权事件
    TASK_CREATE_ALLOW = "task_create_allow"
    TASK_CREATE_DENY = "task_create_deny"
    TASK_CLAIM_ALLOW = "task_claim_allow"
    TASK_CLAIM_DENY = "task_claim_deny"
    DISPATCH_ALLOW = "dispatch_allow"
    DISPATCH_DENY = "dispatch_deny"

    # 委托事件
    DELEGATION_ISSUED = "delegation_issued"
    DELEGATION_VERIFIED = "delegation_verified"
    DELEGATION_EXPIRED = "delegation_expired"
    DELEGATION_REJECTED = "delegation_rejected"

    # 策略事件
    POLICY_OVERRIDE = "policy_override"      # 管理员覆盖
    POLICY_BYPASS = "policy_bypass"          # 绕过程序（dev 模式）
    SCOPE_ATTENUATION = "scope_attenuation"  # scope 衰减记录
    DEPTH_LIMIT_HIT = "depth_limit_hit"      # 委托深度达到上限
```

---

## 五、变更影响与适配策略

### 5.1 数据模型变更

| 表 | 变更类型 | 说明 |
|----|---------|------|
| `tasks`（已有） | 修改字段 | `created_by` 改为 NOT NULL 且从 token 推导 |
| `tasks`（已有） | 新增字段 | `origin_agent`, `origin_task_id`, `delegation_depth`, `delegation_token_hash` |
| `tasks`（已有） | 新增字段 | `allowed_callers`（JSON: 可操作此任务的 agent 列表） |
| NEW: `agent_authorizations` | 新增表 | Agent→Agent 授权矩阵 |
| NEW: `delegation_tokens` | 新增表 | 委托令牌记录（短 TTL, 用于重放检测） |
| NEW: `security_events` | 新增表 | 安全事件审计（与 audit_log 分离但关联） |
| `oauth_clients`（已有） | 新增字段 | `allowed_delegation_depth`（该 client 的委托深度上限） |

### 5.2 迁移策略：向后兼容的 3 阶段

#### Phase 1 — 加固层（纯审计，不拒绝）

只记录安全策略违规，不阻止操作：

```
所有 APE 决策点 → log SECURITY_VIOLATION → 操作继续
目的: 建立基线，收集违规模式，避免 Dev 环境断裂
```

#### Phase 2 — 告警层（警告但允许）

```
所有 APE 决策点 → log SECURITY_VIOLATION → 添加 X-Security-Warning header → 操作继续
目的: 给 Agent 开发者可见性，逐步修正
```

#### Phase 3 — 强制层（拒绝违规）

```
所有 APE 决策点 → 根据策略严格执行 → 违规返回 403
目的: 生产强制安全
```

配置控制：

```yaml
# config.yaml
security_harness:
  mode: audit | warn | enforce   # 默认 enforce
  default_delegation_policy: open | restricted  # 默认 open (dev)
```

### 5.3 API 变更

#### 新增端点

| Method | Path | 说明 |
|--------|------|------|
| `POST` | `/auth/delegations` | 创建 Agent→Agent 授权 |
| `GET` | `/auth/delegations` | 查询授权列表 |
| `DELETE` | `/auth/delegations/{id}` | 撤销授权 |
| `GET` | `/v2/tasks/{id}/provenance` | 获取任务委托链 |
| `GET` | `/v2/tasks/{id}/security-events` | 获取任务安全事件 |
| `GET` | `/admin/security-events` | 安全事件审计查询 |

#### 现有端点变更

| 端点 | 变更 |
|------|------|
| `POST /v2/tasks` | 输入：`created_by` 从请求体移除（从 token 推导）；`assignee` 必须与 token scope 一致 |
| `POST /v2/tasks/{id}/claim` | 输入：可选增加 `delegation_token` 字段 |
| `V2 tasks 响应` | 输出：增加 `provenance`、`delegation_chain`、`effective_scope` 字段 |

---

## 六、与 A2A 协议的对齐

| A2A 协议要求 | 本框架实现 |
|-------------|-----------|
| **Section 13.1**: 授权检查在每个操作前 | APE 在每个路由入口强制 |
| **Section 13.1**: 基于身份/角色/项目/租户的授权 | agent_authorizations + tenant 隔离 |
| **Section 13.4**: 加密传输 | HTTPS 已有（TLS） |
| **Section 13.4**: 输入验证 | validation.py 已覆盖 |
| **Section 13.4**: 审计日志 | security_events + audit_log |
| **Section 13.4**: 速率限制 | rate_limiter.py + flow_control.py 已覆盖 |
| **Agent Card securitySchemes**: 声明认证方式 | OAuth 2.1 client_credentials + 扩展 agent_authorizations |
| **Agent Card**: Skills 声明 | 可映射到 allowed_actions |

**超出 A2A 协议的部分**（本框架的差异化优势）：

| 能力 | A2A 协议 | 本框架 |
|------|---------|-------|
| 委托链追踪 | 未定义 | ProvenanceTracker |
| Scope 衰减传播 | 未定义 | DelegationTokenManager |
| Agent 间授权矩阵 | 未定义 | agent_authorizations 表 |
| 执行时授权策略 | 仅要求 "agent-defined" | APE + plugin hooks |
| 安全事件系统 | 未定义 | SecurityEvent 统一模型 |

---

## 七、实现工作量估算

| 模块 | 改动范围 | 预估工作量 | 优先级 |
|------|---------|-----------|-------|
| `orchestration/models.py` | 新增 ProvenanceChain, DelegatedTaskToken 模型 | ~100 行 | P0 |
| `orchestration/store.py` | `create_task()` 强制 identity；`claim_task()` 验 token；新增授权表 CRUD | ~300 行 | P0 |
| `orchestration/routes.py` | 移除 `created_by` 请求体输入；注入 APE 校验 | ~150 行 | P0 |
| NEW: `security/ape.py` | 授权策略引擎核心逻辑 | ~400 行 | P0 |
| NEW: `security/dtm.py` | 委托令牌生成/验证/衰减 | ~300 行 | P0 |
| NEW: `security/pt.py` | 来源追踪 + 委托链管理 | ~250 行 | P1 |
| NEW: `security/events.py` | 安全事件模型 + 存储 | ~150 行 | P1 |
| `plugin.py` | 新增 4 个安全钩子 | ~80 行 | P1 |
| `auth.py` | 允许 delegation token 端点 | ~100 行 | P1 |
| `orchestration/dispatcher.py` | 分发前检查 scope + disabled 状态 | ~50 行 | P1 |
| `migrations/` | Alembic 迁移脚本 | ~50 行 | P0 |
| 测试 | 30+ 新增测试用例 | ~1500 行 | P0 |

**总计**: ~3,300 行新增 + 约 700 行修改。预估 3-5 个开发日。

---

## 八、风险评估

| 风险 | 影响 | 缓解措施 |
|------|------|---------|
| Phase 2→3 切换时现有 Agent 崩溃 | 高 | `default_delegation_policy: open` 默认保持向后兼容 |
| 委托令牌短 TTL 造成功耗 | 中 | TTL 可配置（默认 5min）；令牌验证缓存 |
| 授权矩阵管理复杂 | 中 | 提供 WebUI 管理页面 + 批量导入 |
| 非 Registry 路径的 A2A 直连绕过 | 高 | 本框架仅覆盖经过 Registry 的委托；直连 A2A 需要 Agent 自身安全实现 |

---

## 九、路线图

```
P0 (隔离的安全层)
  ├── 强制 created_by 从 token 推导
  ├── agent_authorizations 表 + CRUD API
  ├── APE 基础校验点（创建/claim）
  ├── DelegatedTaskToken 生成/验证
  └── 3 阶段迁移模式（audit → warn → enforce）

P1 (可观测性与审计)
  ├── ProvenanceTracker 委托链追踪
  ├── SecurityEvent 统一模型 + 查询 API
  ├── 安全相关的 plugin hooks
  └── 分发前 agent disabled 检查

P2 (治理与策略)
  ├── WebUI 授权管理页面
  ├── 批量授权导入/导出
  ├── 安全策略模板（预设规则集）
  └── 安全事件告警集成（推推/Webhook）
```

---

## 十、参考

1. Google A2A Protocol Specification v1.0 — Section 13 (Security Considerations)
2. Google A2A Enterprise Documentation — Enterprise Implementation of A2A
3. Grimlock: Guarding High-Agency Systems with eBPF and Attested Channels (arXiv:2605.27488, 2026)
4. The Authorization-Execution Gap Is a Major Safety and Security Problem in Open-World Agents (arXiv:2605.11003, 2026)
5. RFC 8693 — OAuth 2.0 Token Exchange
6. RFC 9396 — OAuth 2.0 Rich Authorization Requests (RAR)
7. Miller et al. — Capability-based Financial Instruments (object-capability model)
8. Google Macaroons: Cookies with Contextual Caveats for Delegated Authorization (2014)
9. SPIFFE/SPIRE — Secure Production Identity Framework for Everyone (CNCF)