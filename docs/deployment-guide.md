# simple-a2a-registry 部署文档

> 版本: v2.0 (Phase 1-5 完成)
> 更新: 2026-06-03

---

## 环境要求

| 组件 | 版本要求 | 说明 |
|------|---------|------|
| Python | >= 3.10 | 核心运行环境 |
| SQLite | >= 3.38 | 内置，需支持 JSON 函数 |
| pip | >= 21.0 | 依赖管理 |
| 操作系统 | Linux / macOS / WSL2 | Windows 需 WSL |

## 快速启动

### 1. 安装

```bash
git clone git@github.com:noca/simple-a2a-registry.git
cd simple-a2a-registry
pip install -e .
```

### 2. 启动服务器

```bash
# 默认模式（SQLite 持久化到当前目录）
a2a-registry

# 指定端口和数据库路径
a2a-registry --port 8080 --db /data/registry.db

# 开发模式（内存数据库，日志调试）
A2A_AUTH_DISABLED=1 a2a-registry --db :memory:
```

### 3. 注册 Agent

```bash
curl -X POST http://localhost:8000/v1/agents \
  -H "Content-Type: application/json" \
  -d '{
    "client_id": "worker-1",
    "client_secret": "my-secret",
    "name": "Worker Agent 1",
    "capabilities": ["task/read", "task/write"]
  }'
```

### 4. 创建并派发任务

```bash
# 创建任务
curl -X POST http://localhost:8000/v1/tasks \
  -H "Content-Type: application/json" \
  -d '{
    "name": "hello-task",
    "payload": {"command": "echo hello"},
    "assignee": "worker-1",
    "priority": 5
  }'

# 派发任务
curl -X POST http://localhost:8000/v1/tasks/{task_id}/dispatch
```

---

## 生产部署建议

### 持久化配置

```bash
# 使用固定路径的 SQLite 数据库（数据持久化）
a2a-registry --db /var/lib/a2a-registry/registry.db
```

SQLite 适合单机部署，**不建议 NFS 等网络文件系统**。如需高可用，考虑：
- SQLite + WAL 模式（默认启用）
- 主从冷备（定期 copy db 文件）
- 外部 PostgreSQL 适配（需额外开发）

### 认证配置

```bash
# 创建管理员凭据
export ADMIN_PASSWORD="your-strong-password"

# 启动时开启认证
a2a-registry --auth-enabled --bootstrap-secret "$ADMIN_PASSWORD"
```

### 日志与监控

```bash
# 日志级别
export A2A_LOG_LEVEL=info  # debug | info | warning | error

# 健康检查端点
curl http://localhost:8000/health

# SLA 面板
curl http://localhost:8000/admin/sla

# Dashboard (浏览器打开)
open http://localhost:8000/admin
```

### 系统服务配置 (systemd)

```ini
[Unit]
Description=simple-a2a-registry
After=network.target

[Service]
Type=simple
User=a2a
ExecStart=/usr/local/bin/a2a-registry --db /var/lib/a2a-registry/registry.db
Restart=always
RestartSec=5
Environment=A2A_LOG_LEVEL=info
Environment=ADMIN_PASSWORD=your-password

[Install]
WantedBy=multi-user.target
```

---

## 认证与安全

### OAuth2 Client Credentials

```
POST /v1/token
Content-Type: application/x-www-form-urlencoded

grant_type=client_credentials&client_id=worker-1&client_secret=my-secret

→ {"access_token": "eyJ...", "token_type": "bearer", "expires_in": 3600}
```

所有 API 端点需在 Header 携带 Token：

```
Authorization: Bearer eyJ...
```

### 权限作用域

| Scope | 权限 |
|-------|------|
| `task:read` | 读取任务列表和详情 |
| `task:write` | 创建、更新、派发任务 |
| `agent:register` | 注册和管理 Agent |
| `admin:read` | 查看 Dashboard 和管理面板 |
| `admin:write` | 系统配置和管理操作 |

---

## CLI 使用指南

### task 命令

```bash
# 列出所有任务
a2a-registry task list

# 查看任务详情
a2a-registry task show <task-id>

# 按状态过滤
a2a-registry task list --status running

# 输出为 JSON
a2a-registry task list --json
```

### agent 命令

```bash
# 列出所有 Agent
a2a-registry agent list

# 查看 Agent 详情
a2a-registry agent show <agent-id>

# 注册 Agent
a2a-registry agent register --client-id worker-1 --name "Worker 1"

# 查看 Agent 统计
a2a-registry agent stats <agent-id>
```

### history 命令

```bash
# 查看审计日志
a2a-registry history list

# 按事件类型过滤
a2a-registry history list --event-type task_completed

# 查看事件详情
a2a-registry history show <event-id>
```

### workflow 命令

```bash
# 执行声明式工作流
a2a-registry workflow run examples/sequential-workflow.yaml

# 验证工作流定义
a2a-registry workflow validate examples/diamond-workflow.yaml
```

---

## 体系结构

```
┌─────────────────────────────────────────────────┐
│                   HTTP/WS API                     │
├─────────────────────────────────────────────────┤
│   Dispatcher  │  FlowController  │   Event Bus   │
├─────────────────────────────────────────────────┤
│                  TaskStore (DB)                   │
├─────────────────────────────────────────────────┤
│  WorkerExecutors        │    Dashboard (SSE)     │
│  (Local/Callback/...)   │    + Audit + SLA       │
└─────────────────────────────────────────────────┘
```

### 关键组件

| 组件 | 文件 | 职责 |
|------|------|------|
| Dispatcher | `orchestration/dispatcher.py` | DB 轮询派发任务，支持重试/熔断/条件分支 |
| FlowController | `orchestration/flow_controller.py` | 事件驱动 Flow 执行，状态持久化 |
| Event Bus | (集成在 store.py) | asyncio.Queue 事件分发 + 30s 轮询兜底 |
| Worker Executor | `worker_executor.py` | Local/Callback/Extensible 三种执行模式 |
| SLA | `orchestration/sla.py` | 成功率窗口 + 趋势回归分析 |
| Workflow Engine | `orchestration/workflow.py` | YAML 声明式工作流编排 |
| Dashboard | `static/` | HTML 管理面板 + SSE 实时推送 |
| CLI | `cli*.py` | task/agent/history/workflow 子命令 |

---

## 常见问题

### Q: 服务启动失败

**检查：**
1. Python 版本 `python --version` >= 3.10
2. SQLite 版本 `sqlite3 --version` >= 3.38
3. 端口是否被占用 `lsof -i :8000`
4. 数据库路径是否可写

### Q: 任务一直 pending

**检查：**
1. Dispatcher 是否启动（日志搜索 "dispatcher started"）
2. Agent 是否注册且在线（`a2a-registry agent list`）
3. 熔断是否触发（`a2a-registry task show <id>` 查看 circuit_state）
4. 并行上限是否达到（调整 max_spawn / max_in_progress）

### Q: SLA 数据为空

**解释：** SLA 从历史任务数据计算。无任务时的空结果是正常的。
触发几个任务后再查询即可。

### Q: 数据库性能

SQLite 设计上限：
- 单库容量 < 100MB 时性能最佳
- 日任务量 < 10万 无需优化
- 超出建议定期归档（archive_old_tasks）