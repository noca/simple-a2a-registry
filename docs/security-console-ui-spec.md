# webUI 安全特性管理 — 产品规格 v1.0

> 日期: 2026-06-09
> 基于: Security Harness 框架 (APE/DTM/PT/EventStore) 已上线

---

## 一、需求定义

Security Harness 已实现后端安全层（APE 权限引擎、DTM 委派令牌管理、PT 溯源追踪、SecurityEventStore 事件存储），但缺少管理界面。运维人员和 Agent 管理员无法在 webUI 上查看安全事件、溯源链或 Agent 安全状态。

**目标**: 为 webUI 增加安全特性管理能力，让管理员可以查看和审计安全事件、溯源链，并在 Agent 管理页面集成安全状态展示。

## 二、功能范围

### F1: Security Events 页面

**路由**: `/security`
**导航名称**: `Security Events | 🔒`

**功能描述**:
- 以 Antd Table 形式展示 `GET /admin/security-events` 返回的安全事件
- 支持按 event_type / actor / tenant 过滤（Select 下拉框）
- 支持分页（pageSize=25, offset-based）
- 事件行按 decision 着色（allow 绿色 / deny 红色 / block 橙色）
- 事件类型用彩色 Tag 渲染，类似 AuditLog 页面的 `EVENT_COLORS` 模式
- 每行支持展开查看完整 JSON 详情（Modal）
- 顶部显示 total 计数和 Refresh 按钮

**数据字段映射**:

| 字段 | 类型 | 渲染 |
|------|------|------|
| timestamp | float | toLocaleString |
| event_type | string | 彩色 Tag |
| actor | string | 粗体，monospace |
| target | string | code 标签 |
| tenant | string | 浅色标签 |
| decision | string | allow→绿, deny→红, block→橙 |
| reason | string | 省略显示 |
| scope_used | string | 浅色标签 |
| Actions | - | "JSON" 按钮 → Modal |

**调用 API**:

```
GET /admin/security-events?event_type=X&actor=Y&tenant=Z&limit=25&offset=0
  → { events: SecurityEvent[], total: number, limit: number, offset: number }
```

### F2: Task 溯源链集成

**位置**: 在 Tasks 页 (`/tasks`) 的任务详情面板中增加 **Provenance** Tab

**功能描述**:
- 当用户点击某个任务查看详情时，在详情面板/Drawer 中显示第二个 Tab "Provenance"
- 调用 `GET /v2/tasks/{id}/provenance` 获取溯源链
- 显示：origin_agent, origin_tenant, root_task_id, depth, hops
- Hops 用 Timeline 或步骤列表展示：from_agent → to_agent (action=claim)
- 如果没有 provenance chain，显示 "No provenance record"

**调用 API**:

```
GET /v2/tasks/{id}/provenance
  → { task_id, provenance: { chain_id, origin_agent, origin_tenant, root_task_id, depth, hops[] } }
```

**ProvenanceChain 数据字段**:

| 字段 | 类型 | 渲染 |
|------|------|------|
| chain_id | string | monospace, 灰色 |
| origin_agent | string | 粗体 |
| origin_tenant | string | 标签 |
| root_task_id | string | 可点击跳转 |
| depth | int | 数字 |
| hops[] | ProvenanceHop[] | Timeline 组件 |

**ProvenanceHop**:

| 字段 | 类型 | 渲染 |
|------|------|------|
| from_agent | string | 箭头起始 |
| to_agent | string | 箭头终点 |
| action | string | Tag (claim/delegate) |
| scope_at | string | 标签 |
| timestamp | float | toLocaleString |

### F3: API Client 扩展

在 `src/api/client.ts` 中新增：

```typescript
export const securityEventsAPI = {
  list: (params?: Record<string, string>) =>
    api.get('/admin/security-events', { params }).then(r => r.data),
};

// taskAPI 中新增
taskAPI.getProvenance = (id: string) =>
  api.get(`/v2/tasks/${id}/provenance`).then(r => r.data);
```

### F4: 导航 + 路由

- AppLayout.tsx 的 navItems 中增加 `{ key: 'security', icon: '🔒', label: 'Security Events' }`
- App.tsx 中注册路由 `<Route path="/security" element={<ErrorBoundary><SecurityEvents /></ErrorBoundary>} />`

## 三、验收标准

| # | 验收条件 | 验证方式 |
|---|---------|---------|
| 1 | Security Events 页面可访问，显示安全事件列表 | 浏览器访问 /#/security |
| 2 | 事件表格正确显示 timestamp, event_type, actor, target, tenant, decision, reason | 目视检查 |
| 3 | event_type 过滤 Select 下拉可选用并过滤出对应事件 | 选择某类型，表格只显示该类型 |
| 4 | actor 过滤可输入/选择并按 actor 过滤 | 输入 actor 名称过滤 |
| 5 | 分页正常工作，total 计数正确 | 翻页验证 |
| 6 | Refresh 按钮重新加载数据 | 点击后表格更新 |
| 7 | "JSON" 按钮弹出 Modal 显示完整事件 JSON | 点击后 Modal 正确展示 |
| 8 | decision 列为 allow 时绿色、deny 红色、block 橙色 | 目视检查 |
| 9 | Tasks 页中任务详情有 "Provenance" Tab | 打开任务详情，看到 Tab |
| 10 | Provenance Tab 显示 origin_agent, root_task_id, depth, hops | 数据正确渲染 |
| 11 | Hops 按 Timeline 展示 from→to 链路 | 目视检查 |
| 12 | 无 provenance 时显示 "No provenance record" | 对无溯源任务验证 |
| 13 | API client 扩展编译通过，无 TypeScript 错误 | `npx tsc --noEmit` |
| 14 | `npx vite build` 构建成功 | 构建产物到 `data/web/` |

## 四、非功能约束

- 沿用现有 Antd Card/Table/Select/Modal 组件风格
- 安全事件页面配色方案（EVENT_COLORS）参考 AuditLog 页面模式
- 不需要额外的后端 API 改动（后端已有完整端点）
- 构建方式：`npx vite build`（跳过 tsc -b，预存 TS 类型错误）
- 构建产物输出到 `data/web/`

## 五、页面原型（文本描述）

### Security Events 页面布局

```
┌─────────────────────────────────────────────────────┐
│  🔒 Security Events          count: 128  [Refresh]  │
├─────────────────────────────────────────────────────┤
│  Filters: [Event Type ▼] [Actor ▼] [Tenant ▼]      │
├─────────────────────────────────────────────────────┤
│  ┌──────────────────────────────────────────┐       │
│  │ Time          │ Type       │ Actor │ ... │ JSON  │
│  │───────────────│───────────│───────│─────│───────│
│  │ 2026-06-09... │ AUTH_FAIL-│ agent │ ... │ [JSON]│
│  │               │ URE       │ -alpha│     │       │
│  │───────────────│───────────│───────│─────│───────│
│  │ ...                                          │  │
│  └──────────────────────────────────────────┘       │
│  Pagination: ◀ 1 2 3 ... 6 ▶  Total: 128 events    │
└─────────────────────────────────────────────────────┘
```

### Tasks 详细面板 Provenance Tab

```
┌─ Task Detail ──────────────────────────────────────┐
│  [Details] [Provenance]  ← Tabs                    │
├─────────────────────────────────────────────────────┤
│  Origin Agent:  agent-alpha                         │
│  Origin Tenant: default                             │
│  Root Task ID:  t_a1b2c3d4                         │
│  Depth:         2                                   │
│                                                     │
│  ── Delegation Hops ──                              │
│                                                     │
│  agent-alpha                                        │
│     └─ claim ──→ agent-alpha (2026-06-09 10:30)    │
│                  scope: task:write task:read        │
│  ─────────────────────────────────────────────────  │
│  End of chain                                       │
└─────────────────────────────────────────────────────┘
```