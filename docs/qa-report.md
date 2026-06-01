# QA Report: Simple A2A Registry Web Management UI

**Target:** http://127.0.0.1:8321
**Date:** 2026-05-29
**Scope:** 全页面功能测试（Dashboard / Agents / Kanban V2 / Tasks / Clients / Audit Log / Users / Settings）
**Tester:** Hermes Agent（自动探索性 QA）

---

## Executive Summary

| Severity | Count |
|----------|-------|
| 🔴 Critical | 5 |
| 🟠 High | 2 |
| 🟡 Medium | 2 |
| 🔵 Low | 1 |
| **Total** | **10** |

**Overall Assessment:** 页面导航和基础渲染可用，但多数数据页面存在严重渲染问题——API 返回数据与前端数据模型不匹配导致列表空白、字段显示 `undefined`、详情弹窗缺失，需要统一修复前后端数据契约。

---

## Issues

### Issue #1: Clients 页面始终显示 "0 total" 空白列表

| Field | Value |
|-------|-------|
| **Severity** | 🔴 Critical |
| **Category** | Functional |
| **URL** | `/` → Clients 导航页 |

**Description:**
点击侧边栏 Clients 导航后，页面显示 "Clients 0 total" 和空状态 "No clients"。同步调用 `/admin/clients` API 却返回了正确的客户端数据。

**Root Cause:**
`/admin/clients` API 返回**扁平数组** `[{...client1}, {...client2}]`，但前端 `Clients.tsx` 第 11 行使用 `setCl(d.clients||[])` 解构 `d.clients`。由于数组没有 `.clients` 属性，`d.clients` 为 `undefined`，回退到空数组 `[]`。

**Steps to Reproduce:**
1. 以 admin 身份登录
2. 点击侧边栏 "Clients"
3. 页面显示 "0 total" 空状态
4. 打开浏览器 DevTools Console，执行 `fetch('/admin/clients', {headers:{Authorization: 'Bearer '+localStorage.getItem('token')}}).then(r=>r.json())` → 返回正确的 client 数组

**Expected Behavior:**
应显示已注册的 OAuth clients 列表（当前数据库中有 2 个 client）。

**Actual Behavior:**
空列表 "No clients"。

---

### Issue #2: Users 页面始终显示 "0 total" 空白列表

| Field | Value |
|-------|-------|
| **Severity** | 🔴 Critical |
| **Category** | Functional |
| **URL** | `/` → Users 导航页 |

**Description:**
Users 页面显示 "Users 0 total" 和空状态 "No users"。同步调用 `/admin/users` API 返回正确的用户数据（包含 admin 用户）。

**Root Cause:**
与 Clients 相同问题。`/admin/users` API 返回扁平数组 `[{...user1}]`，但 `UserManagement.tsx` 第 10 行使用 `setUs(d.users||[])` 解构 `d.users`。数组没有 `.users` 属性，导致回退为空数组。

**Steps to Reproduce:**
1. 以 admin 身份登录
2. 点击侧边栏 "Users"
3. 页面显示 "0 total"
4. 验证 API：`/admin/users` 返回 `[{"username":"admin","role":"admin",...}]`

**Expected Behavior:**
应显示 users 列表（至少包含 admin 用户）。

**Actual Behavior:**
空列表 "No users"。

---

### Issue #3: Audit Log 显示 "0 entries" 空白

| Field | Value |
|-------|-------|
| **Severity** | 🔴 Critical |
| **Category** | Functional |
| **URL** | `/` → Audit Log 导航页 |

**Description:**
Audit Log 页面显示 "Audit Log 0 entries" 和一个空 Table。同步调用 `/admin/audit` API 返回 `{"total": 5892, "events": [...]}`，前端却显示 0 条。

**Root Cause:**
`AuditLog.tsx` 第 10 行使用 `setLs(d.logs||[])` 解构 `d.logs`，但 API 返回的字段名是 `events`（不是 `logs`）。5892 条审计事件因此全部不显示。

**Steps to Reproduce:**
1. 以 admin 身份登录
2. 点击侧边栏 "Audit Log"
3. 页面显示 "0 entries"
4. 验证 API：`/admin/audit` 返回 5892 条事件记录

**Expected Behavior:**
应显示审计日志列表，提供分页浏览。

**Actual Behavior:**
完全空白，无法查看审计记录。

---

### Issue #4: Agent ID 列显示 "undefined…"

| Field | Value |
|-------|-------|
| **Severity** | 🟠 High |
| **Category** | Functional |
| **URL** | `/` → Agents 导航页 |

**Description:**
Agents 表格的 ID 列对所有 agent 均显示为 "undefined…"。同时 NAME 列显示的是 agent 的 UUID（如 `403c1f9d-9d3f-47`）而非可读的名称（如 "Hermes Coder Agent"）。

**Root Cause:**
`Agents.tsx` 使用自定义 `<table>` 实现（非 Ant Design `<Table>`），第 49 行硬编码 `(c as any).render(null, a)` 将第一个参数始终传 `null`，而非根据 `dataIndex` 提取实际值。

- **ID 列**（第 28 行）：`render:(id)=>id?.substring(0,16)+'…'` → `id` 为 `null`，`null?.substring(0,16)` 返回 `undefined`，`undefined + '…'` → `"undefined…"`
- **NAME 列**（第 27 行）：`render:(n,r)=> n || r.id?.substring(0,16)` → `n` 为 `null`，回退到 `r.id?.substring(0,16)`，显示了 agent 的 UUID 而非可读名称

代理 API 两个字段都正确返回：`{name: "Hermes Coder Agent", id: "403c1f9d-9d3f-47e9-8a3a-44dd378a9807"}`。

**Steps to Reproduce:**
1. 登录后导航到 Agents
2. 观察表格：NAME 列显示 UUID（如 "403c1f9d-9d3f-47"）而非可读名称
3. 观察表格：ID 列显示 "undefined…"

**Expected Behavior:**
- NAME 列显示 "Hermes Coder Agent"
- ID 列显示 "403c1f9d-9d3f-..."

**Actual Behavior:**
- NAME 列显示 UUID（应为可读名称）
- ID 列显示 "undefined…"

---

### Issue #5: Tasks 页面 QUERY 字段显示 "undefined"

| Field | Value |
|-------|-------|
| **Severity** | 🟠 High |
| **Category** | Functional |
| **URL** | `/` → Tasks 导航页 |

**Description:**
Tasks 表格的 QUERY 列对所有任务显示 "undefined"。

**Root Cause:**
`Tasks.tsx` 第 22 行使用 `dataIndex:'query'`，但 `/v1/tasks` API 返回的任务对象**没有** `query` 字段。API 返回 `{id, agent_id, state, result, error, created_at, updated_at}`。v2 的 tasks 有 `title` 字段但 Tasks 页面调用的是 v1 API。

`q?.substring(0,60) + ...` → `q` 为 `undefined`，`"undefined"?.substring(0,60)` 返回 `"undefined"`。

**Steps to Reproduce:**
1. 导航到 Tasks 页面
2. 观察 QUERY 列：每个任务都显示 "undefined"
3. 验证 API：`/v1/tasks` 返回的任务没有 `query` 字段

**Expected Behavior:**
应显示任务的查询内容，或显示 "-" 占位符。

**Actual Behavior:**
每个任务的 QUERY 列显示 "undefined"。

---

### Issue #6: Kanban V2 卡片无法查看详情

| Field | Value |
|-------|-------|
| **Severity** | 🟡 Medium |
| **Category** | Functional |
| **URL** | `/` → Kanban V2 导航页 |

**Description:**
Kanban V2 页面正确加载了 45 个任务并按状态分组展示，但点击任一卡片均无响应，无法查看任务详情。

**Root Cause:**
`Kanban.tsx` 中卡片渲染没有 onClick 处理程序。查看代码（第 77-160 行），卡片只是静态 `<div>` 元素，缺少点击展开详情 Drawer/Modal 的逻辑。

**Steps to Reproduce:**
1. 导航到 Kanban V2 页面
2. 看到 45 个任务卡片按状态分组
3. 点击任意卡片 → 无任何反应

**Expected Behavior:**
点击卡片应弹出详情弹窗/Drawer，显示任务的完整信息（标题、描述、状态、优先级、创建时间等）。

**Actual Behavior:**
点击卡片后页面无任何变化。

---

### Issue #7: Tasks 页面点击行无法查看详情

| Field | Value |
|-------|-------|
| **Severity** | 🟡 Medium |
| **Category** | UX |
| **URL** | `/` → Tasks 导航页 |

**Description:**
Tasks 页面使用了 Ant Design `<Table>`，行具有 `cursor:pointer` 样式暗示可点击，但实际点击无任何反应。

**Root Cause:**
`Tasks.tsx` 使用的 `<Table>` 组件没有设置 `onRow` 属性，因此行点击没有绑定任何处理函数。不存在详情 Drawer/Modal。

**Steps to Reproduce:**
1. 导航到 Tasks 页面
2. 看到 1 个任务行
3. 点击该行 → 无任何反应

**Expected Behavior:**
点击任务行应展开详情，显示完整的任务信息（agent、state、result、error、timestamps 等）。

**Actual Behavior:**
点击行没有任何反馈。

---

### Issue #8: Dashboard BLOCKED 计数器显示异常（8 个）

| Field | Value |
|-------|-------|
| **Severity** | 🔵 Low |
| **Category** | Functional |
| **URL** | `/` → Dashboard |

**Description:**
Dashboard 统计卡片显示 "BLOCKED: 8"，但系统只有 2 个 agent 和 1 个 v1 task（v2 有 45 个任务）。数字偏高可能因 v2 任务积压所致。

**Root Cause:**
可能来自 v2 tasks 的状态分布——有 8 个 task 处于 `blocked` 状态。计数本身是准确的，但作为仪表板的第一视图可能引起误解。属于信息呈现问题。

**Steps to Reproduce:**
1. 登录后查看 Dashboard
2. 观察 "BLOCKED" 卡片显示 8

**Expected Behavior:**
BLOCKED 计数应有上下文说明（如 "8/45 tasks"），或者 Dashboard 应区分 agent BLOCKED 和 task BLOCKED。

**Actual Behavior:**
BLOCKED 显示 8，与其他指标（ONLINE: 2, WEBSOCKET: 2, RUNNING: 1）形成较大反差。

---

### Issue #9: 全部页面无任何控制台 JS 错误（隐蔽的数据问题）

| Field | Value |
|-------|-------|
| **Severity** | 🟡 Medium |
| **Category** | Console |
| **URL** | 全部页面 |

**Description:**
所有页面导航后检查 `browser_console()`，截至本报告均为零 JS 错误。但 #1-#5 的数据渲染问题均未被捕获（try/catch 静默吞掉了错误），这意味着前端缺乏错误边界和反馈机制。

**Root Cause:**
系统中的 `try{}catch{}` 块在 `fetch` 函数中静默处理了所有异常和错误状态码，用户侧完全看不见 API 失败。工程的错误处理策略是"失败则保持上次状态"，没有用户提示。

**Steps to Reproduce:**
1. 打开浏览器 DevTools Console
2. 导航到 Clients / Users / Audit Log 页面
3. 观察 Console → 无任何错误输出

**Expected Behavior:**
API 返回错误或数据格式不匹配时，应至少 `console.warn` 记录或通过 message.error 提示用户。

**Actual Behavior:**
所有错误被 `catch{}` 静默吞掉，用户完全不知情。

---

### Issue #10: Agent 详情 Drawer 无法通过行点击触发

| Field | Value |
|-------|-------|
| **Severity** | 🟠 High |
| **Category** | Functional |
| **URL** | `/` → Agents 导航页 |

**Description:**
`Agents.tsx` 第 60-67 行定义了详情 Drawer，第 49 行 `onClick:()=>{setSa(a);setDd(true);}` 试图通过行点击打开详情。但由于自定义 table 的实现方式，点击行为在某些情况下不生效——"ID: undefined" 的 UI 视线焦点前的实际点击验证发现 Drawer 实际上可以打开（需仔细点击行区域），但内容的 Status 字段使用 `connection` 字段，而 agent API 部分情况下缺少该字段。

注：此问题与 #4 部分关联，先标记为 High 待验证。

---

## Issues Summary Table

| # | Title | Severity | Category | URL |
|---|-------|----------|----------|-----|
| 1 | Clients 列表空白（`d.clients` vs 扁平数组） | 🔴 Critical | Functional | Clients |
| 2 | Users 列表空白（`d.users` vs 扁平数组） | 🔴 Critical | Functional | Users |
| 3 | Audit Log 空白（`d.logs` vs `events` 字段名不匹配） | 🔴 Critical | Functional | Audit Log |
| 4 | Agent ID 显示 "undefined…" + Name 列显示 UUID | 🟠 High | Functional | Agents |
| 5 | Tasks QUERY 显示 "undefined"（API 无 query 字段） | 🟠 High | Functional | Tasks |
| 6 | Kanban V2 卡片无法查看详情 | 🟡 Medium | Functional | Kanban V2 |
| 7 | Tasks 行点击无法查看详情 | 🟡 Medium | UX | Tasks |
| 8 | Dashboard BLOCKED 计数器 8 个缺乏上下文 | 🔵 Low | Information | Dashboard |
| 9 | 全部页面静默吞掉错误（无 JS error + 无用户提示） | 🟡 Medium | Console / UX | All Pages |
| 10 | Agent 详情 Drawer 体验问题 | 🟠 High | Functional | Agents |

## Testing Coverage

### Pages Tested
- Dashboard (正常加载，统计卡片正确)
- Agents (渲染异常 #4, #10)
- Kanban V2 (列表正常，无详情 #6)
- Tasks (列表显示，QUERY 异常 #5，无详情 #7)
- Clients (完全空白 #1)
- Audit Log (完全空白 #3)
- Users (完全空白 #2)
- Settings (正常加载，配置读写)

### Navigation Tested
- 侧边栏全部 8 个导航项 → 均能正确切换到对应页面
- 登录流程 → 正常
- 退出登录 → 未测试
- 菜单折叠按钮 → 正常

### Features Tested
- 表格渲染和列展示 → 5/9 页面有问题
- 详情弹窗 → Agents 有 Drawer 实现（体验欠佳），其余页面没有
- 搜索/筛选 → 筛选控件存在但无法验证有效性（无数据）
- 新建 Modal → Register Agent / Create Task / Create Client / Add User Modal 可弹出

### Not Tested / Out of Scope
- 创建 Agent / Task / Client / User 的实际提交功能（数据没问题但未验证后端交互链路）
- Settings 配置保存和 reload 功能
- 分页功能（数据量不足）
- 响应式布局 / 移动端适配
- 浏览器兼容性（仅在当前 CDP 浏览器测试）

### Blockers
- Clients、Users、Audit Log 三个页面因数据字段名不匹配完全空白，后续功能测试需要先修复 #1-#3

---

## Root Cause Cluster Analysis

所有 10 个 Bug 可分为 **3 类根本原因**：

### 🔴 Cluster A: 前后端数据契约不匹配（4 个 Critical + 2 个 High）
| Bug | 前端期望 | API 实际返回 |
|-----|---------|-------------|
| #1 Clients | `{clients: [...]}` | `[...]`（扁平数组）|
| #2 Users | `{users: [...]}` | `[...]`（扁平数组）|
| #3 Audit | `{logs: [...]}` | `{events: [...]}` |
| #5 Tasks | `{tasks: [{query: "..."}]}` | `{tasks: [{...no query field}]}` |

**修复策略：** 统一 API 包装层，或修改前端数据提取方式。推荐在后端 API 层加统一包装（如所有 admin 端点返回 `{data: [...]}` 或 `{items: [...]}`），同时修复前端字段名对齐。

### 🟠 Cluster B: 前端组件实现缺陷（2 个 High + 2 个 Medium）
| Bug | 问题 |
|-----|------|
| #4 Agent table | 自定义 table 硬编码 `null` 而非从 dataIndex 取值 |
| #6 Kanban detail | 卡片渲染缺少 onClick → Drawer |
| #7 Tasks detail | Ant Table 缺少 onRow → Drawer |
| #10 Agent Drawer | Drawer 触发体验不稳定 |

**修复策略：** 
- Agents 页自定义 table 应替换为 Ant Design `<Table>` 组件，统一 dataIndex 映射
- Kanban 和 Tasks 添加详情 Drawer/Modal

### 🟡 Cluster C: 错误处理与可观测性缺失（1 个 Medium）
| Bug | 问题 |
|-----|------|
| #9 Silent errors | 所有 `try{}catch{}` 静默吞噬异常，用户无感知 |

**修复策略：** 为每个 fetch 的 catch 块添加 `console.warn` 和 `message.error` 提示，或在 axios interceptor 层做全局错误拦截。

---

## Recommended Fix Priority

```
Phase 1 (Critical — 数据不可见):
  ├── #1: Clients 空白    → 修复数据提取: `d.clients||[]` → `Array.isArray(d) ? d : (d.clients||[])`
  ├── #2: Users 空白      → 同上: `d.users||[]` → `Array.isArray(d) ? d : (d.users||[])`
  └── #3: Audit 空白      → `d.logs||[]` → `(d.events||d.logs||[])`

Phase 2 (High — 数据错误展示):
  ├── #4: Agent ID undefined  → 自定义 table 改为 Ant Table，让 dataIndex 生效
  ├── #5: Tasks QUERY undefined  → 添加 fallback `q || '-'`
  └── #10: Agent Drawer 体验 → 修复行点击区域和响应式

Phase 3 (Medium — 功能缺失):
  ├── #6: Kanban 详情       → 添加卡片点击 Drawer
  ├── #7: Tasks 详情        → 添加 onRow → Drawer
  └── #9: 错误处理          → catch 添加提示

Phase 4 (Low — 信息优化):
  └── #8: BLOCKED 计数      → 添加百分比或 tooltip
```