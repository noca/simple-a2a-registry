# Swarm 拓扑可视化 — UI 设计文档

> 基于 A2A Registry 设计系统 v2.0（macOS 原生风格）
> 接入方案：作为 KanbanBoard Tab 扩展，非独立路由页面

---

## 1. 设计定位

| 维度 | 定义 |
|------|------|
| **入口** | KanbanBoard 页面内新增「Swarm」Tab，无独立路由 |
| **用户场景** | 运维人员查看 Agent 编排拓扑、追踪任务流水线状态 |
| **信息密度** | 中 — 拓扑图高密度但保持视觉清朗，侧边面板承载详情 |
| **交互模式** | 查看为主 → 点击节点 → 侧边面板展开详情 → 可选操作 |
| **刷新策略** | 条件性轮询 10s，用户 Toggle 默认 OFF（设计决定 t_7f49549d） |

---

## 2. 页面整体布局

### 2.1 Tab 切换（KanbanBoard 页面内）

```
┌────────────────────────────────────────────────────────────┐
│  Dashboard  Agents  Kanban  Tasks  Clients  ...           │ 【侧边栏】
├────────────────────────────────────────────────────────────┤
│  [Kanban] [Swarm] ● ← 两级 Tab，Swarm 为二级 Tab         │ 【Tab Bar】
├────────────────────────────────────────────────────────────┤
│  ┌──────────────────────────────────────────────────────┐  │
│  │ Page Title: Swarm Topology      [⚡Auto-refresh] [＋创建]│  │ 【工具栏】
│  ├──────────────────────────────────────────────────────┤  │
│  │ ┌─────────────────────────────────────────────────┐  │  │
│  │ │  Swarm List (最近 5 个)                         │  │  │ 【Swarm 列表】
│  │ │  ┌────┬──────────┬───────┬──────┬────────────┐ │  │  │
│  │ │  │ ID │ Name     │ Nodes │ Status│ Created    │ │  │  │
│  │ │  ├────┼──────────┼───────┼──────┼────────────┤ │  │  │
│  │ │  │ #1 │ prod-s1  │ 8     │ ●运行 │ 2026-05-30 │ │  │  │
│  │ │  │ #2 │ test-s1  │ 5     │ ●完成 │ 2026-05-29 │ │  │  │
│  │ │  └────┴──────────┴───────┴──────┴────────────┘ │  │  │
│  │ └─────────────────────────────────────────────────┘  │  │
│  │                                                       │  │
│  │ ┌────────────────────────────┐ ┌──────────────────┐  │  │
│  │ │  Topology Graph            │ │  Node Info       │  │  │ 【主区域】
│  │ │                            │ │  ──────────      │  │  │  左: SVG 图
│  │ │    [Root] ────┐           │ │  Name: Worker B  │  │  │  右: 信息面板
│  │ │               │           │ │  Role: Worker    │  │  │
│  │ │    ┌────┐────┼────┐     │ │  Status: ●running │  │  │
│  │ │    │    │    │    │     │ │  Tasks: 4/35 done  │  │  │
│  │ │  [W1] [W2] [W3] [W4]   │ │  ID: wrk-b-001    │  │  │
│  │ │    │    │    │    │     │ │  ──────────        │  │  │
│  │ │    └────┘────┼────┘     │ │  [📋 Blackboard]  │  │  │
│  │ │               │           │ │  ...              │  │  │
│  │ │           [Verifier]     │ └──────────────────┘  │  │
│  │ │               │           │                       │  │
│  │ │          [Synthesizer]   │                       │  │
│  │ └────────────────────────────┘                       │  │
│  └──────────────────────────────────────────────────────┘  │
└────────────────────────────────────────────────────────────┘
```

### 2.2 内容分区

| 分区 | 占比 | 说明 |
|------|------|------|
| **工具栏** | 40px | 页面标题 + Auto-refresh Toggle + 创建按钮 |
| **Swarm 列表** | ~180px | 紧凑表格展示已创建的 swarm，可折叠 |
| **拓扑图主区** | flex-1 | 左侧 SVG 拓扑图 (flex: 1.3) |
| **信息面板** | 320px | 右侧固定宽度面板，含节点详情 + Blackboard |

---

## 3. 组件拆分

### 3.1 组件树

```
SwarmTopology (Page Component)
├── SwarmToolbar
│   ├── PageTitle (复用组件)
│   ├── RefreshToggle
│   └── CreateSwarmButton
├── SwarmList
│   ├── SwarmListRow (x N)
│   └── SwarmStatusBadge (复用 status-badge)
├── TopologyViewer (SVG 容器)
│   ├── TopologyNode (x N)
│   │   ├── NodeIcon
│   │   ├── NodeLabel
│   │   └── StatusDot (复用 status-dot)
│   └── TopologyEdge (x N, 含箭头)
│       ├── EdgeLine
│       └── ArrowHead (SVG marker)
├── NodeInfoPanel (右侧面板)
│   ├── NodeHeader
│   ├── StatusSection
│   ├── TaskProgress
│   └── NodeMetadata
└── BlackboardPanel
    ├── BlackboardEntry (x N)
    └── BlackboardInput
```

### 3.2 组件职责

| 组件 | 职责 | Props 接口 |
|------|------|-----------|
| `SwarmTopology` | 页面容器，数据获取，状态管理 | `swarmId?: string` |
| `SwarmToolbar` | 标题、刷新控制、创建入口 | `onCreate, autoRefresh, onToggleRefresh` |
| `SwarmList` | 现有 swarm 列表，点击选中 | `swarms[], selectedId, onSelect` |
| `TopologyViewer` | SVG 容器，节点坐标计算，响应式 | `nodes[], edges[], onNodeClick` |
| `TopologyNode` | 单个节点渲染（颜色/形状/状态） | `node: NodeData, x, y, onClick` |
| `TopologyEdge` | 有向连线 + 箭头 | `from, to, animated?: boolean` |
| `NodeInfoPanel` | 节点详情、状态、元数据 | `node: NodeData \| null` |
| `BlackboardPanel` | Blackboard 评论列表 | `entries[], swarmId, onPostComment` |

---

## 4. 拓扑图视觉设计

### 4.1 节点类型与颜色

| 类型 | 颜色 | Hex | 图标 | 说明 |
|------|------|-----|------|------|
| **Root** | Accent 蓝 | `#007AFF` | ◉ | 入口节点，圆角矩形 |
| **Workers** | 紫色 | `#BF5AF2` | ⚙ | 工作节点组，可多个 |
| **Verifier** | 橙色 | `#FF9F0A` | ✓ | 验证节点 |
| **Synthesizer** | 绿色 | `#30D158` | ⊕ | 汇合节点 |

### 4.2 节点形状

所有节点统一使用 **圆角矩形**（border-radius: 10px），以保持与卡片系统一致。通过颜色和图标区分类型。

```
┌─────────────────┐
│  ◉ Root Agent   │  ← 名称居中
│  ● running      │  ← 状态标签
└─────────────────┘
```

- **宽**: 140px（固定宽度，适应各类型名称）
- **高**: 56px（含内距 12px top/bottom）
- **圆角**: 10px (var(--radius-md))
- **阴影**: 0 1px 3px rgba(0,0,0,0.06)
- **边框**: 2px solid 对应类型色

### 4.3 状态着色（StatusDot）

节点右下角的圆形状态标识：

| 状态 | 颜色 | Hex |
|------|------|-----|
| ready | 蓝色 | `#007AFF` |
| running | 紫色 | `#BF5AF2` |
| completed | 绿色 | `#30D158` |
| blocked | 橙色 | `#FF9F0A` |
| failed | 红色 | `#FF453A` |

状态点直径: 10px，带 2px 白色外层光环。

### 4.4 布局算法（固定分层 DAG）

采用最简单的 **分层线性布局**，不依赖复杂图布局库：

```
Row 0:                     [Root]
                             │
Row 1:      [Worker 1] [Worker 2] [Worker 3] [Worker 4]
               │          │          │          │
Row 2:               [Verifier]
                        │
Row 3:             [Synthesizer]
```

**布局规则：**
- 4 行固定层级：Root → Workers → Verifier → Synthesizer
- Workers 行：居中排列，节点间距 24px
- Worker ≥ 7 时自动两行排列（设计决定 t_7f49549d）
- 垂直间距：64px（行间距）
- 画布 padding：40px

### 4.5 连线设计

```
    ┌────────────────┐
    │   Root Agent   │
    └───────┬────────┘
            │ ← 2px 实线，颜色同起点节点
     ┌──────┼──────┐
     │      │      │
 ┌───▼──┐ ┌───▼──┐ ┌───▼──┐
 │  W1  │ │  W2  │ │  W3  │
 └──────┘ └──────┘ └──────┘
```

- **线宽**: 2px
- **颜色**: `rgba(0,0,0,0.15)`（浅灰色，减少视觉噪音）
- **箭头**: 三角箭头 (size: 8px)，用 SVG marker 实现
- **弯曲**: Workers → Verifier 用贝塞尔曲线合并，避免线重叠
- **动画**: running 状态下线为虚线 + 流动动画（可选）

---

## 5. 节点信息面板

右侧面板设计（固定宽度 320px），点击节点时滑入：

```
┌──────────────────────────────────┐
│ ◉ Worker B                [×关闭]│  ← NodeHeader
├──────────────────────────────────┤
│ 状态                            │
│ ● running          在线 3min    │  ← StatusSection
├──────────────────────────────────┤
│ 任务进度                        │
│ ████████░░░░░░░░ 4/35 (11%)    │  ← TaskProgress
├──────────────────────────────────┤
│ 属性                            │
│ ID         wrk-b-001           │
│ Role       worker (llm)        │
│ Provider   openai/gpt-4o       │
│ 创建时间   2026-05-30 10:23    │  ← NodeMetadata
├──────────────────────────────────┤
│ 📋 Blackboard (2)               │  ← BlackboardPanel
│ ┌────────────────────────────┐ │
│ │ 🤖 Worker B: Task #12     │ │
│ │ 完成，输出缓存至 /tmp/...  │ │
│ │ 10:25:30                   │ │
│ ├────────────────────────────┤ │
│ │ 🤖 Verifier: Check passed │ │
│ │ Hallucination score: 0.02 │ │
│ │ 10:25:45                   │ │
│ └────────────────────────────┘ │
│ [输入框...]  [发送]            │  ← BlackboardInput
└──────────────────────────────────┘
```

**交互规则：**
- 默认显示 Root 节点信息
- 点击任意节点 → 面板内容切换，无页面跳转
- 面板可折叠（点击 × 关闭 ≈ 回退到 Root 信息）
- Blackboard 支持滚动（max-height: 300px）

---

## 6. 状态与空态设计

### 6.1 空态 (No Swarms)

```
┌──────────────────────────────────────┐
│                                      │
│              ⚡                       │
│     还没有任何 Swarm 编排             │
│  创建一个新的 Swarm 来编排 Agent      │
│                                      │
│        [＋ 创建第一个 Swarm]          │
│                                      │
└──────────────────────────────────────┘
```

- 中心对齐，macOS 大留白风格
- 图标: emoji ⚡ 64px
- 标题: 16px Semibold
- 描述: 13px Secondary
- CTA 按钮: Primary 风格

### 6.2 加载态

- Skeleton 卡片（脉动动画）替代实际内容
- 拓扑图区域显示灰色矩形占位

### 6.3 错误态

- Inline Banner 提示（非弹窗）
- 重试按钮
- 保留上次成功加载的数据（stale-while-revalidate）

---

## 7. 交互细节

### 7.1 节点悬停 (Hover)

- 节点放大 1.05x (transform: scale)
- 阴影加深: `0 4px 12px rgba(0,0,0,0.12)`
- 边框加亮
- 连到该节点的线高亮（其他线变淡）

### 7.2 节点点击

- 选中态：外发光 `box-shadow: 0 0 0 3px rgba(0,122,255,0.2)`
- 右侧信息面板切换内容
- URL hash 记录选中节点（支持刷新恢复）

### 7.3 Auto-refresh Toggle

- macOS 风格 Switch（圆角滑块）
- 默认 OFF
- ON 时每 10s 轮询 `/v2/swarm/{root_id}`
- 轮询期间节点状态动画（running 节点脉冲呼吸效果）
- 页面不可见时暂停轮询（`document.hidden`）

### 7.4 创建 Swarm

- 点击「创建」→ Modal 弹窗
- 输入: Swarm Name（必填）+ Root Agent（从 Agents 列表选择）
- 高级选项（折叠）: 默认 Worker 数、超时时间
- 提交 → 创建并跳转到新 Swarm 拓扑图

---

## 8. 响应式适配

| 断点 | 行为 |
|------|------|
| ≥1200px | 拓扑图左 + 信息面板右，正常布局 |
| 900–1199px | 信息面板折叠为底部 Drawer |
| <900px | 列表 + 拓扑图纵向堆叠，节点缩小 0.85x |

---

## 9. API 交互规范

| 端点 | 方法 | 用途 | 请求/响应 |
|------|------|------|-----------|
| `/v2/swarm` | POST | 创建新 swarm | 见后端文档 |
| `/v2/swarm/{root_id}` | GET | 获取拓扑状态+黑板 | 返回 nodes[] + edges[] + blackboard[] |
| `/v2/swarm` | GET | 列表所有 swarm | 返回 swarms[]（含 summary 统计） |
| `/v2/swarm/{root_id}/blackboard` | POST | 发布评论 | `{role, content}` |

**前端数据模型：**

```typescript
interface SwarmSummary {
  root_id: string;
  name: string;
  status: 'running' | 'completed' | 'failed';
  node_count: number;
  created_at: string;
}

interface TopologyNode {
  id: string;
  name: string;
  role: 'root' | 'worker' | 'verifier' | 'synthesizer';
  status: 'ready' | 'running' | 'completed' | 'blocked' | 'failed';
  provider?: string;
  task_progress?: { done: number; total: number };
  metadata?: Record<string, string>;
}

interface TopologyEdge {
  from: string;  // node id
  to: string;    // node id
}

interface BlackboardEntry {
  id: string;
  role: string;
  content: string;
  timestamp: string;
}
```

---

## 10. 组件 Props 接口定义

```typescript
// 核心组件 Props

interface SwarmTopologyProps {
  /** 可选初始 swarm ID，从 URL hash 恢复 */
  initialSwarmId?: string;
}

interface SwarmToolbarProps {
  title: string;
  autoRefresh: boolean;
  onToggleRefresh: (on: boolean) => void;
  onCreate: () => void;
}

interface SwarmListProps {
  swarms: SwarmSummary[];
  selectedId: string | null;
  onSelect: (id: string) => void;
  loading?: boolean;
}

interface TopologyViewerProps {
  nodes: TopologyNode[];
  edges: TopologyEdge[];
  selectedNodeId: string | null;
  onNodeClick: (nodeId: string) => void;
  width?: number;
  height?: number;
}

interface TopologyNodeProps {
  node: TopologyNode;
  x: number;
  y: number;
  selected: boolean;
  onClick: () => void;
}

interface TopologyEdgeProps {
  from: { x: number; y: number };
  to: { x: number; y: number };
  animated?: boolean;
}

interface NodeInfoPanelProps {
  node: TopologyNode | null;
  onClose: () => void;
}

interface BlackboardPanelProps {
  entries: BlackboardEntry[];
  swarmId: string;
  onPost: (content: string) => Promise<void>;
}
```

---

## 11. 实施建议

### 阶段 1（核心，1天）
- SwarmToolbar + SwarmList（表格）
- TopologyViewer + 静态 SVG 节点布局
- API 集成（列表 + 拓扑）

### 阶段 2（交互，1天）
- 节点点击 → 信息面板
- 连线箭头 + 贝塞尔曲线
- 状态着色 + status-dot

### 阶段 3（高级，1天）
- Blackboard 评论功能
- Auto-refresh + 动画
- 创建 Swarm Modal
- 空态/加载态/错误态

### 阶段 4（打磨，0.5天）
- 悬停高亮
- 响应式适配
- 深色模式兼容
- 性能优化（大型拓扑的虚拟化）