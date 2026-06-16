# Security Harness 产品规格文档 (PRD)

> **版本**: v1.0  
> **负责人**: PM (product-manager)  
> **日期**: 2026-06-09  
> **下游**: Coder → Tester  
> **父级设计**: `docs/security-harness-design.md` (架构设计文档)  
> **关联任务**: Coder 实现 t_4ae88c8d

---

## 一、文档目的

本 PRD 面向 Coder，将架构设计文档翻译为**可执行的实现规格**。Coder 应按此规格实现 P0 + P1 功能，Tester 按此规格编写验收测试。

---

## 二、产品背景与目标

### 2.1 问题陈述

当前 simple-a2a-registry 在单 Agent + 人类协作（HITL）场景下有完善的安全机制（JWT 认证、scope 校验、审计日志），但引入 A2A 协议级 Agent 间协作后，出现四个未覆盖的安全鸿沟：

| 鸿沟 | 影响 | 严重度 |
|------|------|--------|
| 越权委派 | Read-only Agent 可委派任务给可写 Agent，突破自身权限 | 严重 |
| 身份伪造 | `created_by` 来自请求体而非 token，可伪造身份 | 严重 |
| 溯源断链 | 多层委托后无法追溯最终委托人 | 高 |
| 无限递归 | 无 max_delegation_depth 限制，可递归攻击 | 高 |

### 2.2 产品目标

构建一个可独立审计的安全执行层（Security Harness），确保 Agent 间任务委派始终在发起者的授权边界内执行，且任何安全决策都可被事后归因和验证。

### 2.3 成功标准

- 所有 P0 强制策略点不可绕过
- 委托链可追踪至原始发起者
- 安全策略支持逐级强制（audit → warn → enforce 三级模式）
- 向后兼容——现有功能不受影响

---

## 三、范围定义

### 3.1 P0 (核心安全层 — 必须实现)

| 模块 | 说明 | 预估行数 |
|------|------|---------|
| APE — 授权策略引擎 | 调用者身份强制、scope 验证、租户隔离、深度控制 | ~400 行 |
| DTM — 委托令牌管理器 | DelegatedTaskToken 生成/验证/scope 衰减 | ~300 行 |
| 模型扩展 | Task 模型新增 provenance/scope 字段 | ~100 行 |
| 路由改造 | `created_by` 从请求体移除、注入 APE 校验 | ~150 行 |
| agent_authorizations 表 | Agent→Agent 授权矩阵 CRUD | ~200 行 |
| 3 阶段迁移模式 | audit → warn → enforce 配置驱动 | ~80 行 |
| 数据库迁移 | Alembic 迁移脚本 | ~50 行 |

**总计 P0**: ~1280 行

### 3.2 P1 (可观测性与审计 — 应实现)

| 模块 | 说明 | 预估行数 |
|------|------|---------|
| PT — ProvenanceTracker | 委托链追踪、溯源 DAG | ~250 行 |
| SecurityEvent 系统 | 统一安全事件模型 + 存储 + 查询 API | ~150 行 |
| Plugin 安全钩子 | 4 个授权决策钩子 + AuthzDecision | ~80 行 |
| Dispatcher 加固 | 分发前检查 scope + disabled 状态 | ~50 行 |
| DTM 授权端点 | `/auth/delegations` CRUD API | ~100 行 |

**总计 P1**: ~630 行

### 3.3 P2 (治理 — 不在此版本范围)

WebUI 授权管理、批量导入导出、告警集成等。

---

## 四、详细功能规格

### 4.1 授权策略引擎 (APE)

#### 4.1.1 文件结构

```
simple_a2a_registry/
  security/              # 新增包
    __init__.py
    ape.py               # APE 核心
    dtm.py               # DTM 核心
    pt.py                # PT 核心（P1）
    events.py            # 安全事件模型（P1）
    errors.py            # 安全相关异常
```

#### 4.1.2 APE 校验点

每个校验点必须**在路由入口处强制执行**，不可绕过：

**校验点 1**: `POST /v2/tasks` 创建任务
```
输入: identity (来自 middleware token payload)
      task_data (不含 created_by)
      
校验顺序:
  1. caller identity 有效性 → token 中的 sub 必须是已注册 agent
  2. assignee (task_data.assignee) 是否存在且 active(disabled=0)
  3. caller 的 scope 是否覆盖任务操作 → 必须有 task:write
  4. tenant 一致性 → caller.tenant == assignee.tenant
  5. 如果是子任务 (有 parent_id) → 检查 delegation_depth < max_depth
  6. 如果有 agent_authorizations 记录 → 检查 allowed_actions 覆盖
  7. 调用 plugin hooks → authorize_task_create (DEFER 则继续默认策略)
  8. 全部通过 → 生成 DelegatedTaskToken → 继续创建
```

**校验点 2**: `POST /v2/tasks/{id}/claim` 认领任务
```
校验顺序:
  1. caller 的 sub == task.assignee
  2. delegation_token 存在且有效 (签名/过期/任务绑定)
  3. token.task_id == claimed_task_id
  4. token.sub == caller.sub
  5. 调用 plugin hooks → authorize_task_claim (DEFER 则继续)
```

**校验点 3**: `POST /v2/tasks/{id}/complete` 完成任务
```
校验顺序:
  1. claim_lock 匹配 (caller 持有该锁)
  2. caller.sub == claimer 记录
```

#### 4.1.3 Caller Identity Resolution (关键变更)

```python
# 当前（不安全）:
task_data = await request.json()
created_by = task_data.get("created_by", "anonymous")  # ← 可伪造

# 新（强制）:
token_payload = request.get("token_payload", {})
caller_id = token_payload.get("sub", "")  # ← 从 JWT 强制提取
# 如果 sub 为空 → 401
# 如果 sub 为 "anonymous" → 401（除非 dev 模式）
# 如果 sub 不是已注册 agent → 403

tenant = token_payload.get("tenant", request.headers.get("X-Tenant-ID", ""))
```

**要求**: `created_by` 字段在 POST body 中**必须被忽略**。即使客户端传入也无效。

#### 4.1.4 Agent-to-Agent Authorization Matrix

```sql
CREATE TABLE agent_authorizations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source_agent_id TEXT NOT NULL,
    target_agent_id TEXT NOT NULL,
    allowed_actions TEXT NOT NULL,     -- JSON array: ["*", "code_review", "deploy"]
    max_depth       INTEGER DEFAULT 5,
    scope_restriction TEXT,            -- JSON: {"exclude": [...], "reduce_to": [...], "map": {...}}
    tenant_id       TEXT NOT NULL DEFAULT '',
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    expires_at      TIMESTAMP,
    UNIQUE(source_agent_id, target_agent_id, tenant_id)
);
```

**授权匹配逻辑**:
```
1. 查 agent_authorizations WHERE source=caller_id, target=assignee_id
2. 无记录 → 取决于 delegation_policy 配置:
   - "restricted" → 拒绝 (403)
   - "open" (默认, 开发模式) → 允许 (向后兼容)
3. 有记录且过期 → 拒绝
4. 有记录 → 检查 allowed_actions 是否覆盖任务动作
5. 通过 → 根据 scope_restriction 衰减 scope
```

#### 4.1.5 3 阶段迁移模式

```yaml
# config.yaml
security_harness:
  mode: audit | warn | enforce   # 默认 enforce
  default_delegation_policy: open | restricted  # 默认 open (开发兼容)
```

| 模式 | 行为 | 适用场景 |
|------|------|---------|
| `audit` | 记录违规到 security_events，不阻塞操作 | 建立基线 |
| `warn` | 记录 + 添加 `X-Security-Warning` 响应头 | 逐步修正 |
| `enforce` | 严格拒绝违规（403） | 生产环境 |

---

### 4.2 委托令牌管理器 (DTM)

#### 4.2.1 DelegatedTaskToken 数据结构

```python
@dataclass
class DelegatedTaskToken:
    jti: str                    # 唯一令牌 ID (uuid4)
    iss: str                    # 签发者 = "simple-a2a-registry"
    sub: str                    # 当前操作者 (assignee agent_id)
    iat: datetime               # 签发时间
    exp: datetime               # 过期时间 (默认 5min, 可配置)

    # 委托链
    origin_agent: str           # 原始发起者 (根任务创建者)
    origin_tenant: str          # 原始租户
    delegation_chain: list[dict]  # [{agent, action, scope, timestamp}, ...]

    # 授权边界
    effective_scope: str        # 当前有效 scope (空格分隔)
    attenuated_from: str        # 父级 scope (审计用)
    max_depth: int              # 最大深度
    depth: int                  # 当前深度 (根=0)

    # 约束
    allowed_callees: list[str] | None  # 允许委托的下级 (None=无限制)
    task_id: str                # 绑定的任务 ID

    # 方法
    def to_jwt(self, private_key: str) -> str: ...
    @staticmethod
    def from_jwt(token: str, public_key: str) -> DelegatedTaskToken: ...
```

#### 4.2.2 Scope 衰减规则

实现 `DTM._attenuate_scope(parent_scope, restriction)`:

```python
def _attenuate_scope(parent_scope: str, restriction: dict | None) -> str:
    """
    衰减规则:
      {"exclude": ["agent:read"]}       → 移除指定 scope
      {"reduce_to": ["task:read"]}      → 取交集
      {"map": {"admin": "read"}}        → scope 映射降级
      None / {}                         → 继承父 scope
    """
```

**强制守卫**:
- 衰减后的 scope 必须是原始 scope 的子集（不可扩张）
- 如果衰减后 scope 为空字符串 → 拒绝委派

#### 4.2.3 令牌生命周期

```
1. 创建任务时: DTM.mint_delegation_token(identity, task_data, parent_chain) 
   → 生成 JWT, 存储 token_hash 到 tasks 表
   
2. 分发任务时: 完整 JWT 通过分发通道 (WS/pool/callback) 传递给 assignee

3. 认领任务时: Agent 提交 JWT, DTM.verify_delegation_token(token, task_id, agent_id)
   → 验证: RS256 签名, exp, jti 未重复使用
   → 验证: token.task_id == task_id
   → 验证: token.sub == agent_id (claiming agent)

4. 子任务创建时: 父任务的 token + 衰减规则 → DTM.mint_delegation_token(...) 创建子 token
```

**`delegation_tokens` 表**（防重放）:
```sql
CREATE TABLE delegation_tokens (
    jti         TEXT PRIMARY KEY,
    task_id     TEXT NOT NULL,
    sub         TEXT NOT NULL,
    origin_agent TEXT NOT NULL,
    scope       TEXT NOT NULL,
    depth       INTEGER NOT NULL DEFAULT 0,
    expires_at  INTEGER NOT NULL,
    used_at     TIMESTAMP,         -- 首次使用时间（可选）
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

---

### 4.3 数据模型变更 (P0 + P1)

#### 4.3.1 `tasks` 表新增字段

| 字段 | 类型 | 默认值 | 说明 | 优先级 |
|------|------|--------|------|--------|
| `origin_agent` | TEXT | '' | 原始发起者 | P0 |
| `origin_task_id` | TEXT | NULL | 根任务 ID | P0 |
| `delegation_depth` | INTEGER | 0 | 当前委托深度 | P0 |
| `delegation_token_hash` | TEXT | NULL | 委托令牌 SHA256 | P0 |
| `effective_scope` | TEXT | '' | 任务执行时的 scope | P0 |
| `provenance_chain_id` | TEXT | NULL | 委托链 ID | P1 |

#### 4.3.2 `orchestration/models.py` 新增模型

**ProvenanceChain** (P1):
```python
@dataclass
class ProvenanceChain:
    chain_id: str
    origin_agent: str
    origin_tenant: str
    root_task_id: str
    parent_task_id: str | None
    depth: int
    hops: list[ProvenanceHop]

@dataclass
class ProvenanceHop:
    from_agent: str
    to_agent: str
    action: str
    scope_at: str
    timestamp: datetime
    token_jti: str
```

**SecurityEvent** (P1):
```python
@dataclass
class SecurityEvent:
    event_id: str
    event_type: SecurityEventType
    timestamp: datetime
    actor: str
    target: str
    tenant: str
    decision: str       # "allow" | "deny" | "block"
    reason: str
    scope_used: str
    delegation_chain: list | None
    metadata: dict
```

---

### 4.4 API 变更

#### 4.4.1 新增端点

| Method | Path | 说明 | P0/P1 |
|--------|------|------|-------|
| `POST` | `/auth/delegations` | 创建 Agent→Agent 授权 | P1 |
| `GET` | `/auth/delegations` | 查询授权列表（支持 filter） | P1 |
| `DELETE` | `/auth/delegations/{id}` | 撤销授权 | P1 |
| `GET` | `/v2/tasks/{id}/provenance` | 获取任务委托链 | P1 |
| `GET` | `/v2/tasks/{id}/security-events` | 获取任务安全事件 | P1 |
| `GET` | `/admin/security-events` | 安全事件审计查询 | P1 |

#### 4.4.2 现有端点变更

| 端点 | 变更 | P0/P1 |
|------|------|-------|
| `POST /v2/tasks` | `created_by` 从请求体**移除**（从 token 推导）；`assignee` 必须存在且 active | P0 |
| `POST /v2/tasks/{id}/claim` | 可选增加 `delegation_token` 字段验证 | P0 |
| V2 任务响应 | 增加 `provenance`、`delegation_chain`、`effective_scope` 字段 | P0/P1 |

---

### 4.5 Plugin 钩子扩展 (P1)

新增 4 个安全钩子到 `Plugin` 基类：

```python
@hook_point
async def authorize_task_create(self, caller, task_data, delegation_chain) -> AuthzDecision:
    """任务创建决策点"""
    return AuthzDecision.DEFER

@hook_point
async def authorize_task_claim(self, claimer, task, delegation_token) -> AuthzDecision:
    """任务 Claim 决策点"""
    return AuthzDecision.DEFER

@hook_point
async def authorize_dispatch(self, task, assignee, dispatch_path) -> AuthzDecision:
    """分发前决策点"""
    return AuthzDecision.DEFER

@hook_point
async def on_security_event(self, event: SecurityEvent):
    """安全事件通知（异步，不阻止）"""
```

`AuthzDecision` 结果类型：
```python
@dataclass
class AuthzDecision:
    class Outcome(Enum):
        ACCEPT = "accept"
        REJECT = "reject"
        DEFER = "defer"       # 交回默认策略
    outcome: Outcome
    reason: str = ""
    override_scope: str | None = None
```

---

### 4.6 Dispatcher 加固 (P1)

在 `dispatcher.py` 中，分发前增加：

1. **Agent disabled 检查**: 如果 `assignee.disabled == 1` → 拒绝分发，记录事件
2. **Scope 检查**: 如果任务的 `effective_scope` 为空且模式为 `enforce` → 拒绝
3. **委托令牌验证**: 如果是委托任务，验证 token 有效性

---

## 五、非功能性需求

| 需求 | 要求 | 优先级 |
|------|------|--------|
| 向后兼容 | 现有 API 用户无需修改代码 | P0 |
| 性能 | APE 校验 < 5ms/请求（百万级 agent_authorizations 除外） | P1 |
| 令牌 TTL | DelegatedTaskToken 默认 5min，可配置 | P0 |
| 审计完整性 | 每个安全决策必须产生 SecurityEvent 记录 | P1 |
| 降级安全 | 数据库连接失败时安全层拒绝而非放行（fail closed） | P0 |
| 无单点 | 纯内存校验，不依赖外部策略服务 | P0 |

---

## 六、配置项

```yaml
# config.yaml
security_harness:
  mode: enforce                    # audit | warn | enforce
  default_delegation_policy: open  # open | restricted
  delegation_token_ttl_seconds: 300
  max_delegation_depth: 10
```

---

## 七、依赖关系

```
P0 实现
  → 新增 security/ 包 (ape.py, dtm.py, errors.py)
  → 修改 orchestration/models.py (Task 新增字段)
  → 修改 orchestration/store.py (created_by 强制、token 存储)
  → 修改 orchestration/routes.py (注入 APE 校验)
  → 新增 migration (agent_authorizations 表 + tasks 字段)
  → 修改 config.py (读取 security_harness 配置)
  
P1 实现
  → 新增 security/pt.py, events.py
  → 修改 plugin.py (新增 4 个钩子)
  → 修改 orchestration/dispatcher.py (disabled 检查)
  → 新增 /auth/delegations 路由
  → 新增 /v2/tasks/{id}/provenance 路由
  → 新增 /v2/tasks/{id}/security-events 路由
  → 修改 auth.py (delegation 端点集成)
```
