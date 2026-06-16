# Security Harness 验收标准 (Acceptance Criteria)

> **版本**: v1.0  
> **负责人**: PM (product-manager)  
> **日期**: 2026-06-09  
> **关联 PRD**: `security-harness-prd.md`  
> **测试方**: Tester  
> **预期测试框架**: pytest + aiohttp test client

---

## 一、P0 验收标准

### AC-P0-01: created_by 不可伪造

| 项目 | 内容 |
|------|------|
| **场景** | 客户端 POST /v2/tasks 时传入伪造 `created_by` |
| **前置** | 客户端拥有有效 JWT token (sub=agent-a, scope=task:write) |
| **操作** | 发送 POST /v2/tasks，body 中传入 `created_by: "admin"` |
| **期望** | 服务端**忽略**请求体中的 created_by，从 JWT sub 推导为 "agent-a" |
| **验证** | 1. 返回的任务记录中 created_by = "agent-a" 2. 如果 body.created_by ≠ token.sub，不报错（静默忽略） |
| **优先级** | **P0** |

---

### AC-P0-02: 未注册 Agent 拒绝创建任务

| 项目 | 内容 |
|------|------|
| **场景** | 未在 agents 表注册的 client_id 创建任务 |
| **前置** | JWT sub = "unknown-agent"，该 agent 未注册 |
| **操作** | POST /v2/tasks |
| **期望** | 403 Forbidden，理由包含 "caller not registered" |
| **优先级** | **P0** |

---

### AC-P0-03: Assignee 不存在时拒绝创建

| 项目 | 内容 |
|------|------|
| **场景** | 任务 assignee 指向一个不存在的 agent |
| **前置** | caller 已注册、scope 有效、assignee="ghost-agent" 不存在 |
| **操作** | POST /v2/tasks {assignee: "ghost-agent"} |
| **期望** | 403 Forbidden，理由包含 "assignee not found" |
| **优先级** | **P0** |

---

### AC-P0-04: Assignee 被禁用时拒绝创建

| 项目 | 内容 |
|------|------|
| **场景** | 任务 assignee 的 agent 被禁用（disabled=1） |
| **前置** | caller 已注册、assignee 存在但 disabled=1 |
| **操作** | POST /v2/tasks {assignee: "disabled-agent"} |
| **期望** | 403 Forbidden，理由包含 "assignee is disabled" |
| **优先级** | **P0** |

---

### AC-P0-05: Scope 不足时拒绝创建

| 项目 | 内容 |
|------|------|
| **场景** | caller 仅持有 task:read，尝试创建任务 |
| **前置** | JWT scope = "task:read" |
| **操作** | POST /v2/tasks |
| **期望** | 403 Forbidden，理由包含 "insufficient scope" 或 "task:write required" |
| **优先级** | **P0** |

---

### AC-P0-06: Tenant 隔离 — 跨租户委派被拒绝

| 项目 | 内容 |
|------|------|
| **场景** | tenant-A 的 agent 委派任务给 tenant-B 的 agent |
| **前置** | caller tenant=A, assignee tenant=B |
| **操作** | POST /v2/tasks {assignee: "cross-tenant-agent"} |
| **期望** | 403 Forbidden，理由包含 "tenant mismatch" |
| **优先级** | **P0** |

---

### AC-P0-07: DelegatedTaskToken 生成与结构验证

| 项目 | 内容 |
|------|------|
| **场景** | 成功创建任务时生成有效的 DelegatedTaskToken |
| **前置** | 所有校验通过 |
| **操作** | POST /v2/tasks (合法请求) |
| **期望** | 1. 返回的任务中包含 `delegation_token` 字段 2. token 是有效 JWT (RS256 签名) 3. token 包含 jti, iss, sub, origin_agent, effective_scope, task_id 等字段 |
| **验证** | 用 Registry 公钥验签，提取 payload 检查必填字段 |
| **优先级** | **P0** |

---

### AC-P0-08: Claim 时 DelegatedTaskToken 验证

| 项目 | 内容 |
|------|------|
| **场景** | Agent claim 任务时提交有效的 DelegatedTaskToken |
| **前置** | 1. 任务存在，assignee = agent-b 2. agent-b 持有有效 delegation_token JWT |
| **操作** | POST /v2/tasks/{id}/claim {delegation_token: "..."} |
| **期望** | 200 OK，任务状态变为 running |
| **优先级** | **P0** |

---

### AC-P0-09: Claim 时令牌过期拒绝

| 项目 | 内容 |
|------|------|
| **场景** | 提交已过期的 DelegatedTaskToken |
| **前置** | token.exp 已过期（等待 > TTL，TTL 默认 300s） |
| **操作** | POST /v2/tasks/{id}/claim {delegation_token: "expired-token"} |
| **期望** | 403 Forbidden，理由包含 "token expired" |
| **优先级** | **P0** |

---

### AC-P0-10: Claim 时令牌任务绑定检查

| 项目 | 内容 |
|------|------|
| **场景** | 提交的 delegation_token.task_id 与目标任务 ID 不匹配 |
| **前置** | token 绑定 task-A，实际 claim task-B |
| **操作** | POST /v2/tasks/{task-b-id}/claim {delegation_token: "token-for-task-a"} |
| **期望** | 403 Forbidden，理由包含 "token task id mismatch" |
| **优先级** | **P0** |

---

### AC-P0-11: Claim 时令牌身份绑定检查

| 项目 | 内容 |
|------|------|
| **场景** | 提交的 delegation_token.sub 与 claimer 身份不匹配 |
| **前置** | token.sub = agent-b，但 claimer 的 JWT sub = agent-c |
| **操作** | POST /v2/tasks/{id}/claim {delegation_token: "..."} (as agent-c) |
| **期望** | 403 Forbidden，理由包含 "token subject mismatch" |
| **优先级** | **P0** |

---

### AC-P0-12: Scope 衰减 — exclude 规则

| 项目 | 内容 |
|------|------|
| **场景** | agent_authorizations 中 scope_restriction 为 {"exclude": ["agent:read"]} |
| **前置** | 原始 scope = "task:read task:write agent:read"，衰减规则 exclude agent:read |
| **操作** | agent-a 委派任务给 agent-b |
| **期望** | 子任务的 effective_scope = "task:read task:write"（agent:read 被移除） |
| **优先级** | **P0** |

---

### AC-P0-13: Scope 衰减 — reduce_to 规则

| 项目 | 内容 |
|------|------|
| **场景** | agent_authorizations 中 scope_restriction 为 {"reduce_to": ["task:read"]} |
| **前置** | 原始 scope = "task:read task:write agent:read" |
| **操作** | agent-a 委派任务给 agent-b |
| **期望** | 子任务的 effective_scope = "task:read"（取交集后仅保留 task:read） |
| **优先级** | **P0** |

---

### AC-P0-14: Scope 衰减 — scope 不可扩张

| 项目 | 内容 |
|------|------|
| **场景** | 衰减规则试图扩张 scope（如 map: {"task:read": "task:write"}） |
| **前置** | 原始 scope = "task:read"，map 规则将 task:read → task:write |
| **操作** | 衰减计算 |
| **期望** | 衰减后 scope = "task:read"（task:write 不在父 scope 中，不可通过 map 添加） |
| **优先级** | **P0** |

---

### AC-P0-15: 3 阶段迁移 — audit 模式不阻塞

| 项目 | 内容 |
|------|------|
| **场景** | security_harness.mode = audit，发送违规请求 |
| **前置** | 配置为 audit 模式，违规操作（如 scope 不足） |
| **操作** | POST /v2/tasks (scope 不足但 audit 模式) |
| **期望** | 操作正常完成（不拒绝），但安全事件记录到 security_events |
| **优先级** | **P0** |

---

### AC-P0-16: 3 阶段迁移 — warn 模式添加响应头

| 项目 | 内容 |
|------|------|
| **场景** | security_harness.mode = warn，发送违规请求 |
| **前置** | 配置为 warn 模式，违规操作 |
| **操作** | POST /v2/tasks (scope 不足但 warn 模式) |
| **期望** | 操作完成，但响应头包含 `X-Security-Warning` |
| **优先级** | **P0** |

---

### AC-P0-17: 3 阶段迁移 — enforce 模式严格拒绝

| 项目 | 内容 |
|------|------|
| **场景** | security_harness.mode = enforce，发送违规请求 |
| **前置** | 配置为 enforce 模式，违规操作 |
| **操作** | POST /v2/tasks (scope 不足) |
| **期望** | 403 Forbidden，操作被完全拒绝 |
| **优先级** | **P0** |

---

### AC-P0-18: 正常流程端到端

| 项目 | 内容 |
|------|------|
| **场景** | 合法的任务创建→分发→认领→完成全流程 |
| **前置** | agent-a 和 agent-b 都已注册且 active，agent-a 有 task:write scope，agent-b 有 task:read scope |
| **操作** | 1. agent-a POST /v2/tasks {assignee: "agent-b"} 2. 验证返回 200 + delegation_token 3. agent-b 用 delegation_token claim 任务 |
| **期望** | 全流程成功，审计事件完整 |
| **验证** | 检查 task 记录：created_by=agent-a, origin_agent=agent-a, delegation_depth=0, effective_scope="task:write" |
| **优先级** | **P0** |

---

### AC-P0-19: agent_authorizations 数据库迁移

| 项目 | 内容 |
|------|------|
| **场景** | Alembic 迁移后 agent_authorizations 表正确创建 |
| **前置** | 空数据库，运行迁移 |
| **操作** | 检查数据库 schema |
| **期望** | agent_authorizations 表存在，字段正确（见 PRD 4.1.4），tasks 表新增 origin_agent, origin_task_id, delegation_depth, delegation_token_hash, effective_scope 字段 |
| **优先级** | **P0** |

---

### AC-P0-20: 委托深度限制

| 项目 | 内容 |
|------|------|
| **场景** | 创建子任务时 delegation_depth >= max_depth |
| **前置** | 父任务 depth=5, max_depth=5 |
| **操作** | 创建子任务（parent_id 指向该父任务） |
| **期望** | 403 Forbidden，理由包含 "delegation depth exceeded" |
| **优先级** | **P0** |

---

## 二、P1 验收标准

### AC-P1-01: ProvenanceTracker — 委托链追踪

| 项目 | 内容 |
|------|------|
| **场景** | 3 级委托链：Human→Agent-A→Agent-B→Agent-C |
| **前置** | 依次创建 3 层任务 |
| **操作** | GET /v2/tasks/{leaf-task-id}/provenance |
| **期望** | 返回完整委托链：origin_agent=human, hops=[(human→A), (A→B), (B→C)], depth=2, 每个 hop 包含 scope 快照 |
| **优先级** | **P1** |

---

### AC-P1-02: SecurityEvent 记录

| 项目 | 内容 |
|------|------|
| **场景** | 每次安全决策产生 SecurityEvent |
| **前置** | 执行任何 APE 决策（allow/deny） |
| **操作** | 触发安全决策后，查询 security_events 表 |
| **期望** | 对应事件的记录存在，包含 event_id, event_type, actor, target, decision, reason, scope_used 等字段 |
| **优先级** | **P1** |

---

### AC-P1-03: SecurityEvent 查询 API — 按任务

| 项目 | 内容 |
|------|------|
| **场景** | 查询指定任务的所有安全事件 |
| **前置** | 任务发生过多次安全决策 |
| **操作** | GET /v2/tasks/{id}/security-events |
| **期望** | 返回该任务相关的所有 SecurityEvent 列表，按时间倒序 |
| **优先级** | **P1** |

---

### AC-P1-04: SecurityEvent 查询 API — 全局审计

| 项目 | 内容 |
|------|------|
| **场景** | 管理员查询全局安全事件 |
| **前置** | 多个任务产生了安全事件 |
| **操作** | GET /admin/security-events?limit=50&offset=0 |
| **期望** | 返回全局按时间倒序的安全事件列表 |
| **优先级** | **P1** |

---

### AC-P1-05: Plugin 钩子 — authorize_task_create

| 项目 | 内容 |
|------|------|
| **场景** | 注册的 plugin 在任务创建前拦截 |
| **前置** | plugin 实现 authorize_task_create，返回 REJECT("custom policy") |
| **操作** | POST /v2/tasks |
| **期望** | 403 Forbidden，理由包含 "custom policy" |
| **优先级** | **P1** |

---

### AC-P1-06: Plugin 钩子 — authorize_task_claim

| 项目 | 内容 |
|------|------|
| **场景** | 注册的 plugin 在任务认领前拦截 |
| **前置** | plugin 实现 authorize_task_claim，返回 REJECT("claim not allowed") |
| **操作** | POST /v2/tasks/{id}/claim |
| **期望** | 403 Forbidden，理由包含 "claim not allowed" |
| **优先级** | **P1** |

---

### AC-P1-07: Plugin 钩子 — on_security_event

| 项目 | 内容 |
|------|------|
| **场景** | 安全事件触发后 plugin 异步收到通知 |
| **前置** | plugin 实现 on_security_event，将事件写入外部存储 |
| **操作** | 触发一次安全决策 |
| **期望** | plugin.on_security_event 被调用，参数中包含有效的 SecurityEvent 实例 |
| **优先级** | **P1** |

---

### AC-P1-08: Dispatcher — 检查 agent disabled

| 项目 | 内容 |
|------|------|
| **场景** | Dispatcher 尝试分发任务给已禁用的 agent |
| **前置** | agent-b 被禁用（disabled=1），有任务分配给 agent-b |
| **操作** | 触发分发流程 |
| **期望** | 分发被拒绝，SecurityEvent 记录 DISPATCH_DENY，理由包含 "agent disabled" |
| **优先级** | **P1** |

---

### AC-P1-09: 授权端点 — CRUD

| 项目 | 内容 |
|------|------|
| **场景** | 管理 agent_authorizations |
| **前置** | 调用者有 registry:admin scope |
| **操作** | 1. POST /auth/delegations 创建授权 2. GET /auth/delegations 查询 3. DELETE /auth/delegations/{id} 撤销 |
| **期望** | 创建返回 201，查询返回列表，撤销返回 204。撤销后的授权不应该再生效 |
| **优先级** | **P1** |

---

### AC-P1-10: 后端兼容性 — 无 token 的旧客户端仍可工作

| 项目 | 内容 |
|------|------|
| **场景** | 缺少 delegation_token 的旧 claim 请求在 open 模式下仍可工作 |
| **前置** | security_harness.default_delegation_policy = open |
| **操作** | POST /v2/tasks/{id}/claim（不提供 delegation_token） |
| **期望** | 200 OK（向后兼容），产生一个 SECURITY_VIOLATION 事件记录 |
| **优先级** | **P1** |

---

## 三、负面测试 (Negative Tests)

### AC-NEG-01: 空请求体

POST /v2/tasks 传入空 body → 400 Bad Request

### AC-NEG-02: 无效 JWT

POST /v2/tasks 传入过期/乱签名 token → 401 Unauthorized

### AC-NEG-03: 任务不存在 claim

POST /v2/tasks/non-existent-id/claim → 404 Not Found

### AC-NEG-04: 双重 claim

同一任务被 claim 两次 → 第二次返回 409 Conflict

### AC-NEG-05: 完成者非 claimer

agent-b claim 任务后，agent-c 尝试 complete → 403 Forbidden

---

## 四、覆盖率要求

| 层级 | 要求 |
|------|------|
| P0 功能 | ✅ 每项 AC 至少一个正向 + 一个负向测试用例 |
| P1 功能 | ✅ 每项 AC 至少一个正向测试用例 |
| APE 校验点 | ✅ 覆盖全部 5 个校验点的拒绝场景 |
| Scope 衰减 | ✅ 覆盖全部 4 种规则 (exclude/reduce_to/map/None) |
| 3 阶段模式 | ✅ 覆盖 audit/warn/enforce 三种模式 |
| 委托链 | ✅ 至少覆盖 3 层嵌套委托 |
| Plugin 钩子 | ✅ 覆盖 REJECT 和 DEFER 两种返回 |
| 端到端 | ✅ 至少 1 个完整 happy path + 1 个完整负向 path |

---

## 五、验收通过条件

1. **所有 P0 验收标准**（AC-P0-01 ~ AC-P0-20）测试通过
2. **所有 P1 验收标准**（AC-P1-01 ~ AC-P1-10）测试通过
3. **负面测试**（AC-NEG-01 ~ AC-NEG-05）全部覆盖
4. 现有测试套件（test_orchestration_api.py 等）不受影响，全部通过
5. 向后兼容性得到验证

---