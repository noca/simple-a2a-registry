# A2A Registry 管理后台 — UI/UX 设计系统规范 v2.0

> macOS 原生风格（Apple Design Language）—— 适用于管理后台的轻量、透明、专注的视觉系统。
> 风格关键词：极简、留白、毛玻璃、侧边栏导航、低信息密度。

---

## 1. 设计定位

| 维度 | 定义 |
|------|------|
| **产品类型** | Agent 注册与编排管理后台（类 macOS 系统偏好设置布局） |
| **用户角色** | Admin（运维/平台管理员）+ Agent 开发者 |
| **信息密度** | 低 — 聚焦核心信息，减少视觉噪音，大量留白 |
| **视觉风格** | macOS Ventura+ 原生风格：浅灰底 + 白卡片 + 毛玻璃 + 极简 |
| **品牌调性** | 专业、克制、优雅 — 像 Apple 系统工具一样可靠 |

---

## 2. Design Tokens（色彩系统）

### 2.1 主色调 & 渐变色

```css
/* Accent — macOS 蓝色 + 紫色渐变 */
--accent:          #007AFF  /* macOS 系统蓝 */
--accent-gradient: linear-gradient(135deg, #007AFF, #AF52DE)
--accent-hover:    #0066D6  /* 蓝色悬停 */
--accent-light:    #007AFF0D  /* 蓝色 5% 透明底 */

/* 渐变引用色 */
--gradient-start: #007AFF  /* 蓝 */
--gradient-end:   #AF52DE  /* 紫 */
```

### 2.2 语义色

```css
--green:   #30D158  /* 成功/在线 — macOS 系统绿 */
--yellow:  #FFD60A  /* 警告/待处理 — macOS 系统黄 */
--orange:  #FF9F0A  /* 阻塞/异常 — macOS 系统橙 */
--red:     #FF453A  /* 失败/错误 — macOS 系统红 */
--purple:  #BF5AF2  /* 运行中/特殊 — macOS 系统紫 */
--blue:    #007AFF  /* 信息标记 */
```

### 2.3 中性色（浅色主题 — 主要）

```css
/* 背景系统 */
--bg:           #F5F5F7  /* 页面背景 — macOS 经典浅灰 */
--bg-sidebar:   rgba(255,255,255,0.72)  /* 侧边栏 — 半透明白 */
--bg-card:      #FFFFFF  /* 卡片纯白 */
--bg-card-glass: rgba(255,255,255,0.65)  /* 毛玻璃卡片 */
--bg-hover:     rgba(0,0,0,0.035)  /* 悬停底色 */
--bg-selected:  rgba(0,122,255,0.08)  /* 选中态 */

/* 分割 & 边框 */
--separator:    rgba(0,0,0,0.08)  /* 极细分隔线 */
--border-light: rgba(0,0,0,0.06)  /* 超浅边框 */
--border:       rgba(0,0,0,0.12)  /* 标准边框 */

/* 文字 */
--text:         #1D1D1F  /* 主文字 — macOS 深灰 */
--text-secondary: #86868B  /* 辅助文字 */
--text-tertiary:  #C7C7CC  /* 占位/禁用文字 */
--text-inverse:   #FFFFFF  /* 深色背景上的文字 */
```

### 2.4 深色主题（备选 — macOS 深色模式）

```css
--bg:            #1C1C1E
--bg-sidebar:    rgba(44,44,46,0.72)
--bg-card:       #2C2C2E
--bg-hover:      rgba(255,255,255,0.06)
--separator:     rgba(255,255,255,0.12)
--text:          #F5F5F7
--text-secondary: #98989D
```

**本规范以浅色主题为主要输出方向**，深色模式通过 CSS 变量切换。

---

## 3. 排版系统（San Francisco）

```css
font-family: -apple-system, BlinkMacSystemFont, 'SF Pro Text', 'SF Pro Icons', 'Helvetica Neue', sans-serif;
font-mono: 'SF Mono', 'Menlo', monospace;
```

| 层级 | 字号 | 字重 | 行高 | 用途 |
|------|------|------|------|------|
| Page Title | 22px | 600 (Semibold) | 1.3 | 页面主标题 |
| Section Title | 16px | 600 | 1.4 | 区块分组标题 |
| Card Title | 14px | 500 | 1.4 | 卡片标题/列表项 |
| Body | 13px | 400 | 1.5 | 正文内容/描述 |
| Body Small | 12px | 400 | 1.4 | 辅助信息/元数据 |
| Caption | 11px | 400 | 1.3 | 标签/脚注/状态 |
| Caption Bold | 11px | 600 | 1.3 | 状态标签/徽章 |
| Stat Value | 28px | 700 | 1.1 | 统计数值 |
| Mono | 12px | 400 | 1.5 | 代码/ID/密钥 |

---

## 4. 间距系统（8px 基座，4px 微调）

```css
--space-1: 4px    /* 微间距：标签内/图标间隙 */
--space-2: 8px    /* 基础网格：组件间距 */
--space-3: 12px   /* 紧凑间距 */
--space-4: 16px   /* 标准间距：卡片内距 */
--space-5: 20px   /* 区块间距 */
--space-6: 24px   /* 大区块间距 */
--space-8: 32px   /* 页面边缘留白 */
--space-10: 40px  /* 超大留白 */
```

**规则:**
- 卡片内边距 16-20px
- 列表项垂直间距 12px
- 区块间间距 24-32px
- 页面边缘留白 32-40px

---

## 5. 阴影 & 层级

遵循 macOS 的图层悬浮语义，三层阴影递进：

```css
/* z-1: 默认卡片（低层） */
--shadow-sm: 0 1px 3px rgba(0,0,0,0.06), 0 1px 2px rgba(0,0,0,0.04)

/* z-2: 浮动面板/弹窗 */
--shadow-md: 0 4px 12px rgba(0,0,0,0.08), 0 2px 4px rgba(0,0,0,0.04)

/* z-3: 模态/对话框 */
--shadow-lg: 0 12px 40px rgba(0,0,0,0.12), 0 4px 12px rgba(0,0,0,0.06)
```

**圆角系统:**

```css
--radius-sm: 6px    /* 小元素：标签/输入框/按钮 */
--radius-md: 10px   /* 标准卡片圆角 */
--radius-lg: 14px   /* 弹窗/面板 */
--radius-full: 9999px  /* 圆形/胶囊 */
```

---

## 6. 布局架构

### 6.1 通用页面结构（macOS 偏好设置风格）

```
┌──────────────────────────────────────────┐
│  ┌──────────────────┐ ┌──────────────┐  │
│  │   侧边栏 Sidebar  │ │   主区域     │  │
│  │  ┌──────────┐     │ │   Content   │  │
│  │  │ Logo     │     │ │             │  │
│  │  ├──────────┤     │ │  ┌───────┐  │  │
│  │  │ 导航项 1 │     │ │  │ 工具栏 │  │  │
│  │  │ 导航项 2 │     │ │  ├───────┤  │  │
│  │  │ 导航项 3 │     │ │  │ 内容区 │  │  │
│  │  │ ...      │     │ │  │       │  │  │
│  │  ├──────────┤     │ │  └───────┘  │  │
│  │  │ 设置/退出│     │ │             │  │
│  │  └──────────┘     │ └──────────────┘  │
│  └──────────────────┘                    │
│  ┌─────────────────────────────────────┐ │
│  │ 状态栏 Status Bar（可选）            │ │
│  └─────────────────────────────────────┘ │
└──────────────────────────────────────────┘
```

- 侧边栏固定宽度：220px，毛玻璃背景 `--bg-sidebar`
- 主区域：剩余宽度，最小 780px
- 侧边栏和主区域之间无分割线，用轻微背景色差区分

### 6.2 侧边栏导航

- 当前页：accent 蓝圆角底色 `rgba(0,122,255,0.08)` + 左侧 3px accent 实线
- 悬停态：极浅灰色 `rgba(0,0,0,0.035)`
- 未选中：无背景
- 含图标（emoji 占位）+ 名称 + 可选徽章
- 底部固定区域：用户信息 + 切换主题按钮

### 6.3 页面布局

| 页面 | 布局方式 |
|------|---------|
| Dashboard | 统计卡片（auto-fill grid）+ 图表（2 列 grid）+ 列表（2 列 grid） |
| Agents | 搜索栏 + 视图切换 + 卡片网格 / 表格 |
| Kanban V2 | 水平滚动 8 列看板 |
| Tasks | 筛选栏 + 分页表格 |
| Clients | 搜索栏 + 卡片列表 + 创建弹窗 |
| Audit Log | 时间筛选 + 事件列表 + JSON 详情展开 |
| Settings | 左侧类别 + 右侧编辑区域 |
| User Mgmt | 表格 + 创建表单 + 角色选择 |

---

## 7. 组件系统

### 7.1 侧边栏导航项

```html
<!-- 选中态 -->
<a class="nav-item active">📊 Dashboard</a>

<!-- 未选中 -->
<a class="nav-item">🤖 Agents</a>
```

- 高度 36px, 圆角 6px, 内距 8px 12px
- 选中态: accent 蓝底色 8% + 左侧 3px accent 指示条
- 间距 2px

### 7.2 统计卡片 (Stat Card)

- 白色背景 `--bg-card`，圆角 `--radius-md` (10px)
- 阴影 `--shadow-sm`
- 内距 16px 20px
- Label: 12px 常规，次级文字色
- Value: 28px 700 字重
- 可选左侧色点指示器（8px 圆点，含微光动画）
- 悬停: 阴影增强至 `--shadow-md`

### 7.3 按钮系统

| 类型 | 风格 | 用途 |
|------|------|------|
| Primary | 渐变蓝紫色底 + 白字，圆角 8px，内距 8px 16px | 创建/注册/保存 |
| Secondary | 透明 + 1px 细边框 + 主文字色 | 取消/次级操作 |
| Ghost | 纯文字 + 悬停有底色 | 最小操作/链接 |
| Icon Only | 36x36 圆形，悬停有底色 | 纯图标操作 |
| Toggle | macOS 风格开关（pill 形状 + 滑钮） | 启用/禁用 |

### 7.4 输入框

- 背景 `--bg`（浅灰），边框 `rgba(0,0,0,0.1)`
- 圆角 8px，内距 10px 14px
- Focus 态: 蓝色 `#007AFF` 边框 + 极淡蓝色光晕
- Placeholder: `--text-tertiary`
- macOS 系统的无边框感

### 7.5 表格

- 无边框表格，行之间用极细 `--separator` 分隔
- 表头: 11px 大写，间距 0.5px，次级文字色
- 行: 13px body，hover 时极浅灰底色
- 无竖线，极简风格

### 7.6 状态标签

- 圆角 6px，内距 2px 8px，11px 600 字重
- 左侧 6px 色点 + 文字
- 色点含 pulse 动画（在线/运行中状态）

| 状态 | 色点 | 背景 | 文字色 |
|------|------|------|--------|
| Online / Alive | 🟢 #30D158 | rgba(48,209,88,0.1) | #248A3D |
| Stale | 🟡 #FFD60A | rgba(255,214,10,0.12) | #AD7D00 |
| Offline | 🔴 #FF453A | rgba(255,69,58,0.1) | #BF3A2E |
| Running | 🟣 #BF5AF2 | rgba(191,90,242,0.1) | #8B3DB8 |
| Completed | 🟢 #30D158 | rgba(48,209,88,0.1) | #248A3D |
| Blocked | 🟠 #FF9F0A | rgba(255,159,10,0.1) | #B37500 |
| Todo | ⚪ #86868B | rgba(0,0,0,0.05) | #6B6B70 |

### 7.7 标签 (Tag / Badge)

- 圆角 6px
- 用于：Agent capabilities、Scope 标识、版本标签
- 浅蓝底 `rgba(0,122,255,0.08)` + accent 蓝字
- 或浅灰底 `rgba(0,0,0,0.05)` + 次级字

### 7.8 Agent 卡片

- 网格布局: `auto-fill, minmax(300px, 1fr)`，间距 12px
- 白色卡片，圆角 10px，阴影 `--shadow-sm`
- 包含: 名称(14px bold) + ID(mono 11px) + 在线状态 + 能力标签 + 元数据行
- 右下角操作按钮组（编辑/删除等）
- 悬停: 阴影增强至 `--shadow-md`

### 7.9 Kanban 看板列

- 列宽 260px 固定，可水平滚动
- 每列顶部色标条 3px 高
- 卡片: 白底，圆角 8px，内距 12px，阴影 `--shadow-sm`
- 卡片含: 标题 + 标签 + 时间 + assignee 头像
- 拖拽中卡片半透明

### 7.10 弹窗 (Modal)

- 圆角 14px，阴影 `--shadow-lg`
- 半透明遮罩 `rgba(0,0,0,0.3)` + backdrop blur
- 头部: 标题 + 关闭按钮（macOS 红绿灯风格圆点）
- 底部: 操作按钮对齐右侧

### 7.11 Toast / 通知

- 左上角弹出（macOS 通知风格）
- 毛玻璃背景 `rgba(255,255,255,0.8)` + blur + 阴影
- 圆角 10px，内距 12px 16px
- 停留 3s 后 fade out

---

## 8. 交互原则

### 8.1 状态转换

```
Agent:   Online (绿) ↔ Stale (黄, >5min) ↔ Offline (红)
Task:    Todo → Ready → Running → Completed / Blocked / Failed / Cancelled
Client:  Active ↔ Disabled
Auth:    Logged In → Token 过期 → 弹出登录 → Re-auth
```

### 8.2 反馈机制

- **写操作反馈:** 3 秒 Toast 提示，成功/失败对应不同语义色
- **错误处理:** 内联组件级错误提示，非弹窗
- **加载态:** 组件级骨架屏（Skeleton），毛玻璃 shimmer 效果
- **空状态:** 居中插画 + 简短文案 + CTA 按钮

### 8.3 数据刷新

- Dashboard: 每 30s 自动轮询
- Kanban: 操作后刷新当前列
- Agents: 手动刷新 + 注册后自动刷新
- Audit Log: SSE 推送 + 手动加载历史

---

## 9. 动效规范

| 场景 | 时长 | 缓动函数 | 说明 |
|------|------|---------|------|
| 页面切换 | 200ms | ease-out | 淡入 |
| 侧边栏展开 | 150ms | ease-out | 仅宽度变化 |
| 弹窗出现 | 250ms | ease-out | 缩放 + 淡入 |
| 弹窗消失 | 200ms | ease-in | 淡出 |
| 卡片悬停 | 150ms | ease | 阴影过渡 |
| 状态指示器 | 2s infinite | ease-in-out | 脉冲动画 |
| Toggle 切换 | 200ms | spring-like | 滑钮过渡 |
| Toast 出现/消失 | 250ms / 3s 停留 | ease-out | 滑动 + 淡入 |

---

## 10. 响应式断点

| 断点 | 宽度 | 变化 |
|------|------|------|
| Desktop | ≥1024px | 侧边栏展开，全布局 |
| Tablet | 768-1023px | 侧边栏可折叠，Kanban 水平滚动 |
| Mobile | <768px | 侧边栏抽屉形式，表格水平滚动 |

---

## 11. 现有 v1 设计诊断 & v2 改进

| # | v1 问题 | v2 解决 |
|---|---------|---------|
| 1 | GitHub 暗色主题，工业感重 | → macOS 原生浅色，柔和平滑 |
| 2 | Tab 导航栏信息密度高 | → 侧边栏导航，更清晰的分组 |
| 3 | 8px 圆角偏锐利 | → 10-14px 更大圆角，更柔和 |
| 4 | 统计卡无留白，密集 | → 增大间距，降低密度，加阴影层次 |
| 5 | 按钮样式偏 GitHub 风格 | → macOS 渐变按钮 + 无边框感 |
| 6 | 缺少毛玻璃/半透明效果 | → backdrop-filter blur 贯穿全系统 |
| 7 | 动效仅 150ms 偏快 | → 增加动效时长，更舒缓 |
| 8 | 字体栈含 Segoe/Roboto | → 纯 SF / Apple 系统字体 |

---

## 12. 设计交付物清单

| # | 文件 | 用途 |
|---|------|------|
| 1 | `01-design-system-spec.md` | 设计规范文档（本文） |
| 2 | `02-dashboard-overview.html` | Dashboard 概览页设计稿 |
| 3 | `03-agents-management.html` | Agent 管理页设计稿 |
| 4 | `04-kanban-board.html` | Kanban 看板页设计稿 |
| 5 | `05-clients-audit.html` | OAuth 客户端 + 审计日志页设计稿 |
| 6 | `06-settings-users.html` | 系统配置 + 用户管理页设计稿 |

---

> **修订历史:** v2.0 — 2026-05-29，macOS 原生风格全面重构，侧边栏布局，毛玻璃系统