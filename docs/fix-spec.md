# Bug 修复规格说明书

**项目：** Simple A2A Registry Web Management UI
**基准报告：** docs/qa-report.md (2026-05-29)
**专注目录：** web-admin/（当前活跃前端）
**关联目录：** a2a-admin/（一并列出，同根目录下）
**文档版本：** v1

---

## 整体策略

| 策略 | 适用 Bug | 说明 |
|------|----------|------|
| 前端适配层修复 | #1, #2, #3, #5 | API 响应格式不做变动，前端数据提取逻辑加兼容层 |
| 前端组件缺陷修复 | #4, #6, #7, #10 | 后端无法介入，纯前端实现问题 |
| 前端错误处理增强 | #9 | catch 块补充 console.warn + 用户提示 |
| 前端信息呈现优化 | #8 | 计数器加上下文说明 |

后端数据契约统一包装层可作为**远期优化**（Phase 4+），但在当前迭代中遵循最小改动原则。

---

## Phase 1（Critical — 数据不可见）

### Bug #1: Clients 页面空白

| 字段 | 值 |
|------|-----|
| **严重性** | 🔴 Critical |
| **修复方式** | 改前端 |
| **工作量** | S（1行） |

**文件：** `web-admin/src/pages/Clients.tsx` → **第 11 行**

**当前代码：**
```tsx
setCl(d.clients||[]);
```

**修复方案：**
```tsx
setCl(Array.isArray(d) ? d : (d.clients||[]));
```

**实现策略：** 前端数据提取增加扁平数组检测。`Array.isArray(d)` 为 true 时直接用 API 原生返回值（扁平数组），否则回退到 `d.clients` 解构。

**变更影响范围：**
- 仅影响 Clients.tsx 第 11 行的数据提取逻辑
- 不影响其他页面
- 不影响创建/删除/切换 OAuth client 功能

**关联其他目录：** `a2a-admin/src/pages/Clients.tsx` 第 34 行已使用 `Array.isArray(data) ? data : []` 正确修复，无需改动。

---

### Bug #2: Users 页面空白

| 字段 | 值 |
|------|-----|
| **严重性** | 🔴 Critical |
| **修复方式** | 改前端 |
| **工作量** | S（1行） |

**文件：** `web-admin/src/pages/UserManagement.tsx` → **第 10 行**

**当前代码：**
```tsx
setUs(d.users||[]);
```

**修复方案：**
```tsx
setUs(Array.isArray(d) ? d : (d.users||[]));
```

**实现策略：** 同 #1。`Array.isArray(d)` 检测，扁平数组直接使用，对象式则解构 `d.users`。

**变更影响范围：**
- 仅影响 UserManagement.tsx 第 10 行
- 不影响创建/删除/切换用户功能

**关联其他目录：** `a2a-admin/src/pages/UserManagement.tsx` 第 32 行已用 `Array.isArray(data) ? data : data.users || []` 修复。

---

### Bug #3: Audit Log 页面空白

| 字段 | 值 |
|------|-----|
| **严重性** | 🔴 Critical |
| **修复方式** | 改前端 |
| **工作量** | S（1行） |

**文件：** `web-admin/src/pages/AuditLog.tsx` → **第 10 行**

**当前代码：**
```tsx
setLs(d.logs||[]);
```

**修复方案：**
```tsx
setLs(d.events||d.logs||[]);
```

**实现策略：** API 返回字段名为 `events`（非前端预期的 `logs`），在前端提取逻辑中增加 `d.events` 作为首个 fallback 优先级，`d.logs` 作为备选兼容项。

**变更影响范围：**
- 仅影响 AuditLog.tsx 第 10 行
- 不影响筛选、分页等已有功能
- `a2a-admin/src/pages/AuditLog.tsx` 需同步检查是否有相同问题

---

## Phase 2（High — 数据错误展示）

### Bug #4: Agent ID 列显示 "undefined…"，Name 列显示 UUID

| 字段 | 值 |
|------|-----|
| **严重性** | 🟠 High |
| **修复方式** | 改前端 |
| **工作量** | S（1行） |

**文件：** `web-admin/src/pages/Agents.tsx` → **第 49 行**

**当前代码：**
```tsx
(c as any).render(null, a)
```

**修复方案（最小改动）：**
```tsx
(c as any).render((c as any).dataIndex ? a[(c as any).dataIndex] : null, a)
```

**实现策略：** 第 49 行硬编码将 `render` 的第一个参数传为 `null`，导致 render 函数无法拿到该列对应的字段值。改为根据 `c.dataIndex` 动态从 record `a` 中提取对应值。若某列无 `dataIndex`（如 Status/Actions 列），则传 `null` 保持原有行为。

**替代方案（推荐远期）：** 将第 47-49 行的自定义 `<table>` 替换为 Ant Design `<Table>` 组件，dataIndex 自动生效，可消除此类问题。但涉及重构，工作量M，安排到 Phase 3。

**变更影响范围：**
- 仅影响 Agents.tsx 第 49 行的 render 调用逻辑
- ID 列：`id?.substring(0,16)+'…'` 拿到正确 id 值 → 显示 "403c1f9d-9d3f-..."
- Name 列：`n || r.id?.substring(0,16)` 拿到正确 name 值 → 显示 "Hermes Coder Agent"
- 不影响 Register/Edit/Delete Modal、Drawer 详情

**关联其他目录：** `a2a-admin/src/pages/Agents.tsx` 使用 Ant Table，不受影响。

---

### Bug #5: Tasks QUERY 列显示 "undefined"

| 字段 | 值 |
|------|-----|
| **严重性** | 🟠 High |
| **修复方式** | 改前端 |
| **工作量** | S（1行） |

**文件：** `web-admin/src/pages/Tasks.tsx` → **第 22 行**

**当前代码：**
```tsx
render:(q:string)=>...q?.substring(0,60)+(q?.length>60?'...':'')
```

**修复方案：**
```tsx
render:(q:string)=>q ? <div>...q?.substring(0,60)+(q?.length>60?'...':'')</div> : '-'
```

**实现策略：** v1 task API 返回的数据没有 `query` 字段（只有 `{id, agent_id, state, result, error, created_at, updated_at}`）。当 `q` 为 `undefined` 时，`String(undefined).substring(0,60)` 输出字符串 `"undefined"`。修复：显式判断 `q` 是否有值，无值则显示 `'-'` 占位符。

**变更影响范围：**
- 仅影响 Tasks.tsx 第 22 行的 query 列 render 函数
- 不影响 ID/Agent/State/Created 列
- 不影响使用 v2 tasks API 的 Kanban 页面

---

### Bug #10: Agent 详情 Drawer 体验问题

| 字段 | 值 |
|------|-----|
| **严重性** | 🟠 High |
| **修复方式** | 改前端 |
| **工作量** | S |

**文件：** `web-admin/src/pages/Agents.tsx` → **第 63 行**

**当前代码：**
```tsx
React.createElement(Descriptions.Item,{label:'Status'},...sa.connection...sa.status...)
```

**修复方案：**
```tsx
React.createElement(Descriptions.Item,{label:'Status'},
  sa.connection==='websocket'
    ? React.createElement(StatusTag,{status:'alive',pulse:true})
    : React.createElement(StatusTag,{status:sa.disabled?'disabled':(sa.status||'unknown')}))
```

**实现策略：**
1. Status 字段的 fallback 从 `'offline'` 改为 `'unknown'`，避免当 agent 缺少 status/connection 字段时显示误导性的 "offline"
2. #4 修复后行点击区域自然会因为自定义 table 修复（render 传入正确值）而改善点击体验

**变更影响范围：**
- 仅影响 Agent Detail Drawer 中的 Status 展示
- 不影响表格中的 Status 列（第 29 行）

---

## Phase 3（Medium — 功能缺失）

### Bug #6: Kanban V2 卡片无法查看详情

| 字段 | 值 |
|------|-----|
| **严重性** | 🟡 Medium |
| **修复方式** | 改前端 |
| **工作量** | M |

**文件：** `web-admin/src/pages/Kanban.tsx` → **第 99-119 行**

**当前代码：**
```tsx
<Card key={t.id} size="small" ... hoverable>
  <div style={{ fontWeight: 500, ... }}>{t.title}</div>
  ...
</Card>
```

**修复方案：**
1. 添加 state：`const [dd, setDd] = useState(false); const [st, setSt] = useState<any>(null);`
2. 在 status map 循环外添加 Drawer 组件：
```tsx
<Drawer title={st?.title||'Task Detail'} placement="right" width={480}
  onClose={()=>{setDd(false);setSt(null);}} open={dd}>
  {st && <Descriptions column={1} size="small" bordered>
    <Descriptions.Item label="ID">{st.id}</Descriptions.Item>
    <Descriptions.Item label="Status"><StatusTag status={st.status} /></Descriptions.Item>
    <Descriptions.Item label="Assignee">{st.assignee||'-'}</Descriptions.Item>
    <Descriptions.Item label="Priority">P{st.priority??0}</Descriptions.Item>
    <Descriptions.Item label="Created">{new Date(st.created_at*1000).toLocaleString()}</Descriptions.Item>
    <Descriptions.Item label="Body">{st.body||'-'}</Descriptions.Item>
  </Descriptions>}
</Drawer>
```
3. 卡片元素添加 `onClick={() => { setSt(t); setDd(true); }}`

**实现策略：** Kanban 卡片目前只是静态展示元素，没有交互绑定。添加详情 Drawer 和卡片点击事件，点击后以侧边栏形式展示任务完整信息（ID、标题、描述、状态、优先级、创建时间等）。

**变更影响范围：**
- 仅影响 Kanban.tsx
- 需引入 antd `Drawer`, `Descriptions` 组件（已导入部分可用）
- 不影响 Create Task Modal 和搜索刷新功能

**关联其他目录：** `a2a-admin/src/pages/KanbanBoard.tsx` 需同步检查是否同样缺少详情功能。

---

### Bug #7: Tasks 页面行点击无法查看详情

| 字段 | 值 |
|------|-----|
| **严重性** | 🟡 Medium |
| **修复方式** | 改前端 |
| **工作量** | M |

**文件：** `web-admin/src/pages/Tasks.tsx` → **第 22 行附近**

**当前代码：**
```tsx
<Table dataSource={ts} columns={[...]}>
```
缺少 `onRow` 属性和详情 Drawer。

**修复方案：**
1. 添加 state：`const [dt, setDt] = useState<any>(null); const [dd, setDd] = useState(false);`
2. Table 组件添加 `onRow`：
```tsx
onRow: (r: any) => ({ onClick: () => { setDt(r); setDd(true); }, style: { cursor: 'pointer' } })
```
3. 在 Spin 内 Table 下方添加 Drawer：
```tsx
<Drawer title={`Task: ${dt?.id?.substring(0,12)}...`} placement="right" width={480}
  onClose={()=>{setDd(false);setDt(null);}} open={dd}>
  {dt && <Descriptions column={1} size="small" bordered>
    <Descriptions.Item label="ID">{dt.id}</Descriptions.Item>
    <Descriptions.Item label="Agent">{dt.agent_id}</Descriptions.Item>
    <Descriptions.Item label="State"><StatusTag status={dt.state} pulse={dt.state==='working'||dt.state==='forwarded'} /></Descriptions.Item>
    <Descriptions.Item label="Created">{dt.created_at?new Date(dt.created_at*1000).toLocaleString():'-'}</Descriptions.Item>
    <Descriptions.Item label="Updated">{dt.updated_at?new Date(dt.updated_at*1000).toLocaleString():'-'}</Descriptions.Item>
    <Descriptions.Item label="Error">{dt.error||'-'}</Descriptions.Item>
    <Descriptions.Item label="Result">{dt.result||'-'}</Descriptions.Item>
  </Descriptions>}
</Drawer>
```

**实现策略：** Ant Table 具备 `onRow` 行点击回调能力，但当前未绑定。添加行点击 → Drawer 详情展示流程，复用 Agents Drawer 的设计模式。

**变更影响范围：**
- 仅影响 Tasks.tsx
- 需引入 antd `Drawer`, `Descriptions` 组件
- 不影响筛选、刷新功能

---

### Bug #9: 全部页面静默吞噬错误，无用户提示

| 字段 | 值 |
|------|-----|
| **严重性** | 🟡 Medium |
| **修复方式** | 改前端（统一层） |
| **工作量** | M |

**文件：** `web-admin/src/api/client.ts` → **第 8-11 行（axios interceptor）**

**当前代码：**
```tsx
api.interceptors.response.use((r) => r, (e) => {
  if (e.response?.status === 401) { /* clear token */ }
  return Promise.reject(e);
});
```

**修复方案：**
```tsx
api.interceptors.response.use((r) => r, (e) => {
  const data = e.response?.data;
  if (e.response?.status === 401) {
    sessionStorage.removeItem('token');
    localStorage.removeItem('token');
  }
  // 非 401 错误在 console 输出便于调试
  console.warn('[API Error]', e.config?.url, e.response?.status, data?.detail||data||e.message);
  return Promise.reject(e);
});
```

**补充修复：** 各页面组件中的 `catch{}` 空块也应补充至少 `console.warn`。修改范围涉及以下文件：

| 文件 | 行号 | 当前 | 修复后 |
|------|------|------|--------|
| Clients.tsx | 11 | `}catch{}` | `}catch(e){console.warn('[Clients] fetch failed',e)}` |
| UserManagement.tsx | 10 | `}catch{}` | `}catch(e){console.warn('[Users] fetch failed',e)}` |
| AuditLog.tsx | 10 | `}catch{}` | `}catch(e){console.warn('[AuditLog] fetch failed',e)}` |
| Agents.tsx | 17 | `}catch{}` | `}catch(e){console.warn('[Agents] fetch failed',e)}` |
| Tasks.tsx | 10 | `}catch{setTs([]);}` | `}catch(e){console.warn('[Tasks] fetch failed',e);setTs([])}` |
| Kanban.tsx | 25 | `}catch{/* ignore*/}` | `}catch(e){console.warn('[Kanban] fetch failed',e)}` |
| Dashboard.tsx | 13-14 | `if...catch`通过Promise.allSettled处理 | 无需修改（已使用 `status === 'fulfilled'` 判断） |

**实现策略：** 在 axios response interceptor 层增加非隐蔽错误记录（console.warn），同时在每个页面的 fetch catch 块补充调试日志。这确保了：
1. 开发者 DevTools Console 中能看到 API 错误 URL、状态码和错误消息
2. 用户侧不受到多余弹窗干扰（仅 console 级别，不自动弹 message.error，除非操作发起方明确需要——如创建/提交操作已有 message.error 处理）

**变更影响范围：**
- 影响所有页面的 fetch 路径（共 6 个文件各一行 catch 修改）
- 影响 axios interceptor（client.ts 1 处修改）
- 不影响页面正常渲染逻辑

---

## Phase 4（Low — 信息优化）

### Bug #8: Dashboard BLOCKED 计数器无上下文

| 字段 | 值 |
|------|-----|
| **严重性** | 🔵 Low |
| **修复方式** | 改前端 |
| **工作量** | S |

**文件：** `web-admin/src/pages/Dashboard.tsx` → **第 23 行**

**当前代码：**
```tsx
{t:'Blocked',v:vs?.by_status?.blocked??0,i:<CloseCircleOutlined/>,c:'var(--orange)'}
```

**修复方案：**
```tsx
{
  t: 'Blocked',
  v: vs?.by_status?.blocked ?? 0,
  suffix: vs?.total ? `/${vs.total} tasks` : undefined,
  i: <CloseCircleOutlined />,
  c: 'var(--orange)',
}
```

需将 `Statistic` 组件（第 32 行）传参调整为：
```tsx
React.createElement(Statistic, {
  title: ...,
  value: s.v,
  suffix: s.suffix,
  valueStyle: { fontSize: 28, fontWeight: 700, color: s.c },
  prefix: ...
})
```

**实现策略：** Dashboard 的 BLOCKED 计数器取自 `v2/stats` 接口的 `by_status.blocked`，显示的是 v2 tasks 的 blocked 数（8/45），但当前仅显示绝对值 "8"，没有分母上下文。利用 Ant Design Statistic 的 `suffix` 属性显示 `/45 tasks`，消除用户疑惑。

**变更影响范围：**
- 仅影响 Dashboard.tsx 第 23 行统计卡片定义
- 不影响其他统计卡片（Online/Running 等已有明确业务含义）
- `vs?.total` 可通过 `v2/stats` 的 `by_status` 各状态求和得到，或由 API 增加 `total` 字段

**备选方案（更简洁）：** 若 `v2/stats` 不提供 `total` 字段，可改为：
```tsx
const totalTasks = Object.values(vs?.by_status||{}).reduce((a:number,b:any)=>a+(b||0),0);
{suffix: `/${totalTasks} tasks`}
```

---

## 修复总览表

| Phase | # | Bug | 文件 | 行号 | 修复方式 | 工作量 |
|-------|---|-----|------|------|----------|--------|
| Phase 1 | #1 | Clients 空白 | `Clients.tsx` | 11 | 改前端适配层（扁平数组检测） | S |
| Phase 1 | #2 | Users 空白 | `UserManagement.tsx` | 10 | 改前端适配层（扁平数组检测） | S |
| Phase 1 | #3 | Audit Log 空白 | `AuditLog.tsx` | 10 | 改前端适配层（字段名 fallback） | S |
| Phase 2 | #4 | Agent 显示 undefined | `Agents.tsx` | 49 | 改前端（render 传值修复） | S |
| Phase 2 | #5 | Tasks QUERY undefined | `Tasks.tsx` | 22 | 改前端（fallback 占位符） | S |
| Phase 2 | #10 | Agent Drawer 体验 | `Agents.tsx` | 63 | 改前端（status fallback） | S |
| Phase 3 | #6 | Kanban 无详情 | `Kanban.tsx` | 99-119 | 改前端（添加 Drawer + 点击事件） | M |
| Phase 3 | #7 | Tasks 无详情 | `Tasks.tsx` | 22 | 改前端（onRow + Drawer） | M |
| Phase 3 | #9 | 静默错误吞噬 | 6个文件 | catch块 | axios interceptor + catch 补充 console.warn | M |
| Phase 4 | #8 | BLOCKED 无上下文 | `Dashboard.tsx` | 23 | 改前端（Statistic suffix） | S |

## 后端数据契约统一包装层可行性评估

| Bug | 是否可在后端统一层修复 | 说明 |
|-----|----------------------|------|
| #1 | ✅ 可统一 | 后端 `/admin/clients` 返回 `{data: [...]}` 或 `{clients: [...]}` |
| #2 | ✅ 可统一 | 后端 `/admin/users` 同上 |
| #3 | ✅ 可统一 | 后端 `/admin/audit` 返回 `{logs: [...]}`（别名字段） |
| #4 | ❌ 不可 | 后端数据已正确返回，纯前端 render 实现缺陷 |
| #5 | ❌ 不可 | API 本身无 `query` 字段（v1 task 设计如此），后端增加冗余字段不合算 |
| #6 | ❌ 不可 | 纯前端事件绑定缺失 |
| #7 | ❌ 不可 | 纯前端事件绑定缺失 |
| #8 | ❌ 不可 | 纯前端信息呈现问题 |
| #9 | ❌ 不可 | 纯前端 error handling 策略问题 |
| #10 | ❌ 不可 | 纯前端 status 字段 fallback |

**后端统一包装层推荐方案：** 在 Go 后端 admin API handler 层增加一个 middleware/helper 函数，将所有 admin 端点（/admin/clients, /admin/users, /admin/audit）包裹为统一的 `{data: ..., total: ...}` 格式。此方案的优势在于：
- 一次性解决 Cluster A 全部字段名不匹配问题
- 前后端契约标准化，后续新增页面无需重复修复
- 但改动涉及后端代码，评审/部署周期更长

**当前建议：** 按本规格的 Phase 方案（前端逐个修复）先解阻塞，后端统一包装层作为技术债务记录，择机在后续迭代中推进。

---

## git 工作建议

```
# 从项目根目录创建 fix branch
git checkout -b fix/qa-report-phase1-4

# Phase 1 改动: 3 个文件
git add web-admin/src/pages/Clients.tsx
git add web-admin/src/pages/UserManagement.tsx
git add web-admin/src/pages/AuditLog.tsx

# Phase 2 改动: 2 个文件
git add web-admin/src/pages/Agents.tsx
git add web-admin/src/pages/Tasks.tsx

# Phase 3 改动: 3 个文件
git add web-admin/src/pages/Kanban.tsx
git add web-admin/src/pages/Tasks.tsx
git add web-admin/src/api/client.ts

# Phase 4 改动: 1 个文件
git add web-admin/src/pages/Dashboard.tsx

# 按 Phase 分批提交
git commit -m "fix(phase1): 修复 Clients/Users/AuditLog 页面数据字段名不匹配导致空白"
git commit -m "fix(phase2): 修复 Agents/Tasks 表格数据错误展示"
git commit -m "fix(phase3): 添加 Kanban/Tasks 详情 Drawer + 全局错误日志"
git commit -m "fix(phase4): Dashboard BLOCKED 计数增加上下文"
```