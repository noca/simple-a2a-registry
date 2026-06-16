# Simple A2A Registry V2 — AI 协作规范 (CLAUDE.md)

> 本文件规定了 AI Agent（如 Claude Code、WisCode、Cline 等）在本代码库中进行开发、调试、重构与测试时必须遵循的指令、技术栈约定与代码风格。

---

## 🚀 构建与测试命令

### 1. 后端服务 (Python / aiohttp)
```bash
# 启动开发服务器（SQLite, 无认证模式，DEBUG 日志）
bash scripts/dev.sh

# 启动生产服务器（需要根据配置加载认证）
bash scripts/prod.sh

# 运行全部后端测试（Pytest）
pytest

# 运行特定测试文件
pytest tests/test_models.py

# 运行性能基准测试 (Benchmarks)
pytest tests/benchmarks/ -v --tb=short

# 代码分析与检查 (若适用)
ruff check .  # 或 flake8 .
```

### 2. 前端管理后台 (React / Vite)
```bash
# 进入前端工作区
cd a2a-admin

# 启动前端开发服务器 (Vite)
npm run dev

# 运行前端 Lint 代码检查
npm run lint

# 构建前端静态资源 (输出成果至 data/web)
npm run build
```

---

## 🛠️ 技术栈与规范

### 后端 (Backend)
- **核心语言**: Python >= 3.10 (推荐使用 3.11)
- **Web 异步框架**: `aiohttp` 3.9+ 
- **关系型 ORM**: `SQLAlchemy 2.0`（双引擎适配：SQLite WAL / MySQL 运行时无缝切换）
- **数据库迁移**: `Alembic`
- **安全防线**: APE (Active Policy Enforcer, **Enforce 模式**) + DTM (委派 Token 管理) + PT (溯源追踪)
- **指标体系**: Prometheus (`/metrics` 端点)

### 前端 (Frontend)
- **主框架**: React 19 + TypeScript + React Router DOM v7
- **UI 框架**: Ant Design (antd v5)
- **状态流管理**: Zustand
- **构建工具**: Vite 8

---

## 📐 代码风格与约定

### 1. 命名约定 (Naming Conventions)
* **Python**:
  * 文件与模块: 小写下划线 `snake_case.py` (例如 `state_machine.py`)
  * 类名: 首字母大写驼峰 `PascalCase` (例如 `TaskStore`)
  * 函数与变量: 小写下划线 `snake_case` (例如 `check_task_create`)
  * 常量: 大写下划线 `UPPER_SNAKE_CASE` (例如 `HEARTBEAT_TIMEOUT`)
* **TypeScript / React**:
  * 组件文件: `PascalCase.tsx`
  * 状态/钩子/辅助函数: `camelCase.ts`
  * 样式、属性: `camelCase`

### 2. 关键代码规则
* **不可用 Pydantic**: 本代码库后端采用纯 `dataclasses` (配合 Python 3 延迟注解 `from __future__ import annotations`)。编写数据模型时，**严禁引入 Pydantic**，以保持代码包的纯净度和对齐 a2a v1.0 协议规范。
* **双模关系型引擎对齐**: 任何底层数据的更新都应当保证 SQLite WAL 和 MySQL 方言的向后兼容。在编写 SQL/Schema 脚本时，必须针对两种方言提供匹配。
* **安全策略钩子机制**: APE (安全策略引擎) 已全面采用 **Enforce 强拦截模式**。任何新增的任务操作必须在相应的路由入口绑定 APE Checkpoint，一旦鉴权失败强制拦截并抛出 APE 403 拒绝错误。
* **表格驱动测试**: 凡涉及状态机转换、复杂的 APE 鉴权边界等具有多分支、多输入状态的逻辑，在编写测试时，**强制使用表格驱动测试 (Table-Driven Tests)** 风格，以提升验证覆盖度。

---

## ⚠️ 常见陷阱与故障预防

1. **WebSocket 状态 DANGLING**: 
   当 Agent WebSocket 长连接断开时，任务会流转入 `DANGLING` 状态，拥有 30 秒的心跳重连宽限期。若宽限期超时，Dispatcher 会重置为 FAILED 并重新派发。在修改重发机制时，务必处理好文件 Workspace 的隔离加锁，以防并发覆盖带来的“分裂脑”污染。
2. **多租户隔离约束**:
   所有的 API 路由与底层 ORM 查询都必须严密对齐 `tenant_id`。如果一个请求未带 `X-Tenant-ID` 或从 JWT 解码出的 tenant 为空，在 APE 策略检查下将会被强制拒绝。

---

## 🚫 严格禁止事项

1. **禁止在规范治理阶段修改任何业务代码**：仅允许编辑和生成规范文件、文档或配置辅助。
2. **禁止静默通过不合规的委派**：任务委派链（Provenance Chain）深度绝对不能超过 `max_delegation_depth` 限制，一经溢出必须抛出 `DelegationDepthExceeded` 异常。
3. **禁止将 SQLite 独占锁逻辑带到 MySQL 架构中**：在高并发场景下，避免使用粗粒度的全库锁，而应借助 `Claim Lock` 行级乐观锁来解决任务争抢。
