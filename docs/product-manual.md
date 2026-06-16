# Simple A2A Registry 产品使用手册

> **版本**: 1.0.0 | **更新日期**: 2026-06-15
>
> "AI Agent 的 Kubernetes" — 轻量级、生产可用的 Agent-to-Agent 注册、编排与治理平台

---

## 目录

1. [产品简介](#1-产品简介)
2. [快速开始](#2-快速开始)
3. [架构概览](#3-架构概览)
4. [安装与部署](#4-安装与部署)
5. [配置参考](#5-配置参考)
6. [API 使用指南](#6-api-使用指南)
7. [CLI 使用指南](#7-cli-使用指南)
8. [Agent 开发指南](#8-agent-开发指南)
9. [Kanban 编排深入](#9-kanban-编排深入)
10. [安全管理](#10-安全管理)
11. [多租户](#11-多租户)
12. [扩展性](#12-扩展性)
13. [监控与运维](#13-监控与运维)
14. [测试与验证](#14-测试与验证)
15. [故障排除](#15-故障排除)
16. [FAQ](#16-faq)

---

## 1. 产品简介

### 1.1 产品定位

Simple A2A Registry 是一个基于 Google A2A (Agent-to-Agent) 协议的轻量级 Agent 注册与编排服务平台。它将分散的 AI Agent 统一注册、发现、任务分发和流程编排整合为单一系统，让开发者像使用 Kubernetes 管理容器一样管理 AI Agent。

### 1.2 核心理念

| 维度 | 说明 |
|------|------|
| **注册与发现** | Agent 通过 HTTP API 注册自身能力（技能、标签），其他 Agent 或客户端可按需搜索发现 |
| **任务分发** | 支持 HTTP 心跳轮询和 WebSocket 长连接两种模式，灵活适配不同场景 |
| **Kanban 编排** | 完整的 8 状态任务状态机，支持 DAG 依赖链、自动派发、重试和人工介入 |
| **安全治理** | OAuth 2.1 认证、Scope 权限控制、Security Harness 策略引擎、审计日志 |
| **多租户隔离** | 租户级数据完全隔离，适用于多团队/多项目场景 |

### 1.3 适用场景

- **AI Agent 协同工作**：多个 AI Agent 协作完成复杂任务（编码 -> 审查 -> 合成）
- **运维自动化平台**：调度监控脚本、故障处理流程、变更执行
- **CI/CD Pipeline**：任务依赖链编排，自动触发后续阶段
- **企业内部 Agent 治理**：统一注册、权限管控、审计追踪

### 1.4 核心能力速览

| 能力 | 说明 |
|------|------|
| Agent 注册/发现 | 按名称/技能/标签搜索，A2A Agent Card 标准 |
| HTTP 心跳 | 120s 超时，300s 自动清理，Prometheus 监控 |
| WebSocket 长连接 | 实时任务推送，自动重连，指数退避 |
| Kanban 编排引擎 | 8 状态状态机，DAG 依赖链，Claim Lock 原子锁定 |
| Swarm 拓扑 | 多 Agent 并行工作 -> 验证 -> 合成，共享黑版 |
| Subprocess Pool | 持久 Worker 进程池，JSON-Line 协议通信 |
| OAuth 2.1 | JWT (RS256/HS256)，Scope 鉴权，Admin 客户端管理 |
| 定时任务 (Cron) | Cron 表达式自动创建编排任务 |
| 声明式工作流 | YAML 定义多任务工作流，一键提交 |
| Agent Memory | 三级命名空间（个人/共享/全局），TTL 过期 |
| 事件总线 + SSE | 实时事件流，Webhook 回调 |
| 内置 Web Dashboard | Agent 列表、Kanban 看板、任务详情、实时统计 |
| Prometheus 指标 | 10+ 预定义指标，/metrics 端点 |
| 审计日志 | 追加式、防篡改，可配置保留期 |
| 共享工作空间 | 跨 Agent 的文件/数据共享 |

---

## 2. 快速开始

### 2.1 安装

```bash
pip install simple-a2a-registry
```

或从源码安装：

```bash
git clone <repo-url>
cd simple-a2a-registry
pip install -e .
```

### 2.2 启动服务器

```bash
a2a-registry
```

启动后日志显示：

```
INFO Simple A2A Registry starting on 0.0.0.0:8321 (data: ~/.simple-a2a-registry) 🔓 auth disabled (dev) | V2: defaults
```

打开浏览器访问 `http://localhost:8321` 即可看到内置 Web Dashboard。

### 2.3 注册第一个 Agent

```bash
curl -s -X POST http://localhost:8321/v1/agents \
  -H "Content-Type: application/json" \
  -d '{
    "name": "My First Agent",
    "description": "只是一个测试 Agent",
    "skills": ["Python", "数据分析"],
    "tags": ["test", "demo"]
  }'
```

返回示例：

```json
{
  "id": "a2a-abc123",
  "name": "My First Agent",
  "status": "alive",
  "skills": ["Python", "数据分析"],
  "tags": ["test", "demo"],
  "created_at": 1718000000
}
```

### 2.4 创建第一个编排任务

```bash
curl -s -X POST http://localhost:8321/v2/tasks \
  -H "Content-Type: application/json" \
  -d '{
    "title": "Hello World 任务",
    "body": "这是我的第一个编排任务",
    "assignee": "my-first-agent",
    "priority": 1
  }'
```

### 2.5 使用 Python SDK

```python
from simple_a2a_registry.client import A2AClient

# 创建客户端（无认证模式）
client = A2AClient(registry_url="http://localhost:8321")

# 健康检查
health = client.health()
print(f"Registry: {health['version']}")

# 注册 Agent
agent_id = client.register_agent(
    name="SDK Agent",
    description="通过 Python SDK 注册",
    skills=["Python"],
)
print(f"Registered: {agent_id}")

# 心跳保活
client.heartbeat(agent_id)

# 列出所有 Agent
agents = client.list_agents()
print(f"Total agents: {agents['total']}")
```

### 2.6 停止服务器

按 `Ctrl+C` — 服务器会优雅关闭：通知所有 WS 连接 Agent、取消进行中任务、关闭数据库。

---

## 3. 架构概览

### 3.1 三层架构

```
┌──────────────────────────────────────────────────────────────┐
│                     HTTP / WebSocket Layer                     │
│                     (aiohttp server)                          │
├──────────────────────────────────────────────────────────────┤
│                                                               │
│  ┌──────────────────────┐  ┌────────────┐  ┌───────────────┐ │
│  │   Agent Discovery    │  │   WS Hub   │  │ Orchestration │ │
│  │   · 注册/注销        │  │  · 长连接   │  │ Engine        │ │
│  │   · 心跳保活         │  │  · 消息路由  │  │ · 任务状态机(8态)│ │
│  │   · 搜索/发现        │  │  · 连接管理  │  │ · DAG 依赖链   │ │
│  └────────┬─────────────┘  └─────┬──────┘  │ · Worker 派发  │ │
│           │                      │         │ · HITL         │ │
│           │                      │         │ · 审计日志     │ │
│           │                      │         └───────┬───────┘ │
│  ┌────────┴──────────────────────┴─────────────────┴────────┐ │
│  │             Store Layer (SQLite / MySQL 双引擎)            │ │
│  │   · RetryEngine: 指数退避重试                             │ │
│  │   · Alembic 迁移管理                                     │ │
│  │   · 5 张编排表 + Agent 表 + OAuth 表 + 审计表            │ │
│  └───────────────────────────────────────────────────────────┘ │
│                                                               │
│  ┌────────────────────────────────────────────────────────┐  │
│  │  Plugin System                                          │  │
│  │  · 生命周期钩子: load/init/before_shutdown              │  │
│  │  · 请求钩子: before_request/after_request               │  │
│  │  · 事件钩子: on_agent_register/task_created 等         │  │
│  └────────────────────────────────────────────────────────┘  │
│                                                               │
│  ┌────────────────────────────────────────────────────────┐  │
│  │  Rate Limiting · Audit · Multi-Tenancy                  │  │
│  └────────────────────────────────────────────────────────┘  │
│                                                               │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │               Auth & Governance                           │  │
│  │   OAuth 2.1 · JWT(RS256/HS256) · Scope 鉴权             │  │
│  │   Security Harness(APE/DTM/PT) · enforce/warn/audit      │  │
│  └──────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────┘
```

### 3.2 核心组件

| 组件 | 所在模块 | 职责 |
|------|---------|------|
| Agent Discovery | `server.py`, `store.py` | Agent 注册、心跳保活、离线清理 |
| WS Hub | `server.py` | WebSocket 长连接管理、消息路由 |
| Orchestration Engine | `orchestration/` | 任务状态机、DAG 依赖、Worker 派发 |
| TaskStore | `orchestration/store.py` | 任务持久化（5 张表） |
| Dispatcher | `orchestration/dispatcher.py` | 后台轮询派发、过期释放、重试 |
| State Machine | `orchestration/state_machine.py` | 8 状态转换校验 |
| Swarm Engine | `orchestration/swarm.py` | 多 Agent 拓扑编排 |
| Auth | `auth.py` | OAuth 2.1 JWT 签发/校验 |
| Plugin System | `plugin.py` | 扩展点管理 |
| Security Harness | `security/` | 安全策略引擎、委托令牌、溯源链 |

### 3.3 任务分发优先级

| 优先级 | 路径 | 说明 |
|--------|------|------|
| **P1** | WebSocket | Agent 通过 WS 连接，任务实时推送 |
| **P1.5** | Subprocess Pool | 持久化子进程 Worker，JSON-Line 协议 |
| **P2** | Blocked | Agent 已知但未在线，等待重连 |
| **P3** | Legacy Worker | 每任务临时生成子进程 |

---

## 4. 安装与部署

### 4.1 环境要求

| 组件 | 版本要求 | 说明 |
|------|---------|------|
| Python | >= 3.10 | 核心运行环境 |
| SQLite | >= 3.38 | 内置，需 JSON 函数支持 |
| pip | >= 21.0 | 依赖管理 |
| OS | Linux / macOS / WSL2 | Windows 需 WSL |

### 4.2 pip 安装（推荐）

```bash
# 安装最新发布版
pip install simple-a2a-registry

# 验证安装
a2a-registry --version
```

### 4.3 开发模式安装

```bash
git clone <repo-url>
cd simple-a2a-registry

# 创建虚拟环境
python3 -m venv .venv
source .venv/bin/activate

# 安装开发模式（包含测试依赖）
pip install -e ".[dev]"

# 启动
a2a-registry
```

### 4.4 生产部署

#### 启用认证

```bash
a2a-registry --auth-enabled true --bootstrap-secret "your-strong-secret"
```

#### 指定持久化路径

```bash
a2a-registry \
  --data-dir /var/lib/a2a-registry \
  --board-path /var/lib/a2a-registry/board.db \
  --port 8321
```

#### 使用 MySQL 数据库

```bash
export A2A_REGISTRY_DATABASE__DRIVER=mysql
export A2A_REGISTRY_DATABASE__MYSQL_DSN=mysql+pymysql://user:password@host:3306/a2a_registry

a2a-registry --auth-enabled true
```

### 4.5 Docker Compose 部署

创建 `docker-compose.yml` 并启动：

```bash
docker compose up -d
```

这将会启动：

- **mysql**: MySQL 8.0 生产数据库
- **registry**: A2A Registry HTTP 服务器（内置自动健康检查）

支持水平扩展：

```bash
docker compose up -d --scale registry=3
```

### 4.6 systemd 服务配置

```ini
[Unit]
Description=Simple A2A Registry
After=network.target

[Service]
Type=simple
User=a2a
ExecStart=/usr/local/bin/a2a-registry \
  --data-dir /var/lib/a2a-registry \
  --auth-enabled true \
  --bootstrap-secret your-secret \
  --log-format json
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

### 4.7 TLS/SSL 配置

```bash
a2a-registry \
  --certfile /etc/ssl/certs/a2a.crt \
  --keyfile /etc/ssl/private/a2a.key \
  --port 8443
```

### 4.8 数据库迁移

```bash
# 运行所有待迁移
alembic upgrade head

# SQLite → MySQL 迁移工具
python scripts/migrate_sqlite_to_mysql.py
```

---

## 5. 配置参考

### 5.1 配置优先级

| 优先级 | 来源 | 示例 |
|--------|------|------|
| 1 | CLI 参数 | `--port 9000 --auth-enabled true` |
| 2 | 环境变量 | `A2A_REGISTRY_SERVER__PORT=9000` |
| 3 | YAML 配置文件 | `~/.simple-a2a-registry/config.yaml` |
| 4 | 代码默认值 | port 8321, auth disabled |

### 5.2 CLI 参数完整说明

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--host` | `0.0.0.0` | 监听地址 |
| `--port` | `8321` | 监听端口 |
| `--data-dir` | `~/.simple-a2a-registry` | 数据目录 |
| `--auth-enabled` | `false` | 启用 OAuth 2.1 认证 |
| `--bootstrap-secret` | 自动生成 | 初始管理员客户端密钥 |
| `--board-path` | `<data-dir>/board.db` | 编排引擎数据库路径 |
| `--dispatcher-enabled` | `true` | 启用后台 Worker 派发器 |
| `--dispatcher-interval` | `5` | 派发器轮询间隔（秒） |
| `--claim-ttl` | `900` | 任务锁 TTL（15 分钟） |
| `--failure-limit` | `3` | 默认重试次数 |
| `--workspaces-root` | `<data-dir>/workspaces` | 工作区根目录 |
| `--log-format` | `text` | 日志格式：json / text |
| `--log-level` | `INFO` | 日志级别 |
| `--log-file` | stderr | 日志文件路径 |
| `--certfile` | — | TLS 证书 |
| `--keyfile` | — | TLS 私钥 |

### 5.3 YAML 完整配置

```yaml
# 文件路径: ~/.simple-a2a-registry/config.yaml

server:
  host: "0.0.0.0"
  port: 8321
  cors_origins: "*"

database:
  driver: sqlite                    # "sqlite" 或 "mysql"
  sqlite_path: "~/.simple-a2a-registry/registry.db"
  mysql_dsn: ""                     # mysql+pymysql://user:pass@host/db
  pool_size: 5
  max_overflow: 10

auth:
  enabled: false                    # 生产环境应设为 true
  bootstrap_secret: ""              # 首次启动的管理员密钥
  rsa_key_size: 2048                # RS256 密钥对生成尺寸
  jwk_ttl: 3600                     # JWK 缓存 TTL（秒）

logging:
  format: text                      # "json" 用于生产
  level: info
  output: stdout

orchestration:
  dispatcher_enabled: true
  dispatcher_interval: 5            # 秒
  claim_ttl: 900                    # 15 分钟
  failure_limit: 3                  # 重试上限
  task_timeout: 300                 # 任务超时（秒）
  workspaces_root: "~/.simple-a2a-registry/workspaces"
  board_path: "~/.simple-a2a-registry/board.db"

monitoring:
  metrics_enabled: true
  metrics_path: "/metrics"

rate_limit:
  enabled: false                    # 建议生产开启
  default_unauthenticated: 60      # 匿名请求/分钟
  default_authenticated: 300       # 已认证请求/分钟
  storage: memory                   # "memory" 或 "mysql"
  whitelist: []                     # 豁免的 client_id 列表

audit:
  retention_days: 90                # 审计日志保留天数

security_harness:
  enabled: false
  mode: warn                        # "enforce" / "warn" / "audit"
  default_delegation_policy: open
  delegation_token_ttl_seconds: 300
  max_delegation_depth: 10

plugins:
  # 第三方插件配置
  # my-plugin:
  #   module: my_package.my_plugin
  #   config:
  #     api_key: "xxx"
```

### 5.4 环境变量参考

使用 `A2A_REGISTRY_` 前缀，双下划线表示嵌套层级：

```bash
# 服务器配置
export A2A_REGISTRY_SERVER__PORT=9000
export A2A_REGISTRY_SERVER__CORS_ORIGINS=https://myapp.com

# 数据库配置
export A2A_REGISTRY_DATABASE__DRIVER=mysql
export A2A_REGISTRY_DATABASE__MYSQL_DSN=mysql+pymysql://user:pass@host:3306/a2a

# 认证配置
export A2A_REGISTRY_AUTH__ENABLED=true
export A2A_REGISTRY_AUTH__BOOTSTRAP_SECRET=my-secret

# 编排配置
export A2A_REGISTRY_ORCHESTRATION__DISPATCHER_INTERVAL=10
export A2A_REGISTRY_ORCHESTRATION__CLAIM_TTL=1800
```

---

## 6. API 使用指南

### 6.1 认证方式

#### 无认证模式（开发环境）

默认 `--auth-enabled false`，所有 API 端点无需认证。

#### OAuth 2.1 模式（生产环境）

启用 `--auth-enabled true` 后，需要在请求头携带 Bearer Token：

```bash
Authorization: Bearer eyJhbG...
```

**获取 Token：**

```bash
curl -s -X POST http://localhost:8321/auth/token \
  -d "grant_type=client_credentials" \
  -d "client_id=simple-a2a-registry" \
  -d "client_secret=auto-generated-or-your-secret"
```

**响应：**

```json
{
  "access_token": "eyJhbG...",
  "token_type": "Bearer",
  "expires_in": 3600,
  "scope": "agent:read agent:register task:read task:write"
}
```

#### Scope 权限速查

| Scope | 权限 | 适用端点 |
|-------|------|---------|
| `agent:read` | 读取 Agent | `GET /v1/agents`, `GET /v1/agents/{id}` |
| `agent:register` | 注册 Agent | `POST /v1/agents` |
| `agent:admin` | 管理 Agent | `DELETE /v1/agents/{id}`, `POST /v1/agents/{id}/toggle` |
| `task:read` | 读取任务 | `GET /v2/tasks`, `GET /v2/tasks/{id}` |
| `task:write` | 操作任务 | `POST /v2/tasks`, 任务 CRUD |
| `registry:admin` | 系统管理 | Admin 客户端管理、WebSocket |
| `user:read` / `user:write` | 用户管理 | 用户 CRUD |

#### 公开端点（永远无需认证）

- `GET /health`
- `GET /.well-known/*`
- `POST /auth/token`
- `POST /auth/register`

### 6.2 Agent Registry API (V1)

#### 注册 Agent

```bash
curl -s -X POST http://localhost:8321/v1/agents \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Coder Agent",
    "description": "AI 编码助手",
    "version": "1.0.0",
    "agent_card": {
      "url": "http://agent-host:9001/.well-known/agent-card.json"
    },
    "skills": ["Python", "JavaScript"],
    "tags": ["coding", "devops"],
    "default_input_modes": ["text/plain"],
    "default_output_modes": ["text/plain"]
  }'
```

#### 搜索 Agent

```bash
# 按关键词搜索
curl "http://localhost:8321/v1/agents?q=coding"

# 按技能筛选
curl "http://localhost:8321/v1/agents?skill=Python"

# 按标签筛选
curl "http://localhost:8321/v1/agents?tag=devops"

# 按状态筛选
curl "http://localhost:8321/v1/agents?status=alive"
```

#### 心跳保活

```bash
curl -s -X POST http://localhost:8321/v1/agents/a2a-xxx/heartbeat
# → {"id":"a2a-xxx","status":"alive","stale_timeout":120}
```

**心跳规则：**

- 每 120 秒未收到心跳 → 标记 `stale`
- 每 300 秒未收到心跳 → 自动垃圾回收

#### 开关 Agent

```bash
# 禁用（标记为 disabled，不再接收任务）
curl -s -X POST http://localhost:8321/v1/agents/a2a-xxx/toggle

# 再次调用 toggle 可重新启用
```

#### 注销 Agent

```bash
curl -s -X DELETE http://localhost:8321/v1/agents/a2a-xxx
```

### 6.3 Kanban Orchestration API (V2)

#### 创建任务

```bash
curl -s -X POST http://localhost:8321/v2/tasks \
  -H "Content-Type: application/json" \
  -d '{
    "title": "实现登录模块",
    "body": "实现 JWT 登录、注册、密码重置",
    "assignee": "coder-agent",
    "priority": 1,
    "tags": ["auth", "backend"]
  }'
```

#### 带依赖的任务（DAG）

```bash
# 创建子任务时指定父任务 ID
curl -s -X POST http://localhost:8321/v2/tasks \
  -H "Content-Type: application/json" \
  -d '{
    "title": "单元测试",
    "body": "为登录模块编写测试",
    "assignee": "tester-agent",
    "parents": ["t_parent_task_id"]
  }'
```

#### 查询任务

```bash
# 列表（支持筛选和排序）
curl "http://localhost:8321/v2/tasks?status=ready&assignee=coder-agent&sort=priority"

# 详情（包含依赖链和运行历史）
curl "http://localhost:8321/v2/tasks/t_xxx"
```

#### Worker 操作流程

**Claim（抢占）：**

```bash
curl -s -X POST http://localhost:8321/v2/tasks/t_xxx/claim \
  -H "Content-Type: application/json" \
  -d '{"worker_id": "coder-1", "pid": 12345}'
# → {"claim_lock": "uuid-xxx", "task": {...}}
```

**心跳（续期锁）：**

```bash
curl -s -X POST http://localhost:8321/v2/tasks/t_xxx/heartbeat \
  -H "Content-Type: application/json" \
  -d '{"claim_lock": "uuid-xxx"}'
```

**完成：**

```bash
curl -s -X POST http://localhost:8321/v2/tasks/t_xxx/complete \
  -H "Content-Type: application/json" \
  -d '{
    "claim_lock": "uuid-xxx",
    "summary": "登录模块实现完成",
    "result": {"files": ["auth.py", "login.py"]}
  }'
```

**失败：**

```bash
curl -s -X POST http://localhost:8321/v2/tasks/t_xxx/fail \
  -H "Content-Type: application/json" \
  -d '{"claim_lock": "uuid-xxx", "error": "超时未响应"}'
```

**阻塞（人工介入）：**

```bash
curl -s -X POST http://localhost:8321/v2/tasks/t_xxx/block \
  -H "Content-Type: application/json" \
  -d '{"claim_lock": "uuid-xxx", "reason": "需要人工确认"}'
```

**添加评论：**

```bash
curl -s -X POST http://localhost:8321/v2/tasks/t_xxx/comment \
  -H "Content-Type: application/json" \
  -d '{"author": "reviewer", "body": "代码审查通过"}'
```

#### 添加/移除依赖

```bash
# 添加父依赖
curl -s -X POST http://localhost:8321/v2/tasks/t_child/depend \
  -H "Content-Type: application/json" \
  -d '{"parent_id": "t_parent"}'

# 移除父依赖
curl -s -X DELETE http://localhost:8321/v2/tasks/t_child/depend/t_parent
```

#### 编排统计

```bash
curl "http://localhost:8321/v2/stats"
# → {"total": 42, "completed": 30, "running": 5, "failed": 3, ...}
```

### 6.4 Swarm API（多 Agent 拓扑）

```bash
# 创建 Swarm
curl -s -X POST http://localhost:8321/v2/swarm \
  -H "Content-Type: application/json" \
  -d '{
    "goal": "研究和实现 OAuth 模块",
    "workers": [
      {"profile": "researcher", "title": "研究 OAuth 协议", "body": "调研主流方案"},
      {"profile": "coder", "title": "实现 OAuth 端点", "body": "编码实现"}
    ],
    "verifier_profile": "reviewer",
    "synthesizer_profile": "writer"
  }'

# 查状态
curl "http://localhost:8321/v2/swarm/t_root_id"

# 写黑版
curl -s -X POST http://localhost:8321/v2/swarm/t_root_id/comment \
  -H "Content-Type: application/json" \
  -d '{"author": "worker-1", "key": "phase1_result", "value": {"protocol": "OAuth 2.1"}}'

# 读黑版
curl "http://localhost:8321/v2/swarm/t_root_id/blackboard"
```

### 6.5 Cron 定时任务 API

```bash
# 创建定时任务
curl -s -X POST http://localhost:8321/v2/cron \
  -H "Content-Type: application/json" \
  -d '{
    "name": "每日健康检查",
    "assignee": "health-checker",
    "cron_expression": "0 9 * * *",
    "task_template": {
      "title": "定时健康检查",
      "body": "检查各服务状态",
      "priority": 1
    }
  }'

# 列出定时任务
curl "http://localhost:8321/v2/cron"

# 启用/禁用
curl -s -X POST http://localhost:8321/v2/cron/cron_xxx/toggle

# 删除
curl -s -X DELETE http://localhost:8321/v2/cron/cron_xxx
```

### 6.6 声明式工作流 API

```bash
# 提交 YAML 工作流
curl -s -X POST http://localhost:8321/v2/workflows \
  -H "Content-Type: application/json" \
  -d '{
    "yaml": "
name: example-workflow
tasks:
  - id: research
    title: 调研阶段
    assignee: researcher
  - id: implement
    title: 实现阶段
    assignee: coder
    depends_on: [research]
  - id: review
    title: 审查阶段
    assignee: reviewer
    depends_on: [implement]
"
  }'

# 查询工作流状态
curl "http://localhost:8321/v2/workflows/wf_xxx"

# 列出工作流所有任务
curl "http://localhost:8321/v2/workflows/wf_xxx/tasks"
```

### 6.7 Agent Memory API

```bash
# 写入内存
curl -s -X POST http://localhost:8321/v2/memory \
  -H "Content-Type: application/json" \
  -d '{
    "agent_id": "a2a-xxx",
    "key": "last_result",
    "value": {"status": "ok", "count": 42},
    "namespace": "personal",
    "ttl": 3600
  }'

# 读取内存
curl "http://localhost:8321/v2/memory?agent_id=a2a-xxx&key=last_result&namespace=personal"

# 按前缀查询
curl "http://localhost:8321/v2/memory/query?agent_id=a2a-xxx&prefix=last_"

# 删除
curl -s -X DELETE "http://localhost:8321/v2/memory?agent_id=a2a-xxx&key=last_result"
```

#### 三级命名空间

| 命名空间 | 可见范围 | 说明 |
|----------|---------|------|
| `personal` | 仅所有者 | 个人上下文，如当前对话状态 |
| `shared` | 同一任务内 | 多 Agent 协作时共享中间结果 |
| `global` | 所有 Agent | 全局知识，如系统配置 |

### 6.8 共享工作空间 API

```bash
# 创建工作空间
curl -s -X POST http://localhost:8321/v2/workspaces \
  -H "Content-Type: application/json" \
  -d '{
    "name": "project-alpha",
    "agent_ids": ["a2a-xxx", "a2a-yyy"]
  }'

# 上传文件
curl -s -X PUT http://localhost:8321/v2/workspaces/ws_xxx/files/readme.md \
  -H "Content-Type: text/markdown" \
  -d '# 项目文档'

# 读取文件
curl "http://localhost:8321/v2/workspaces/ws_xxx/files/readme.md"

# 列出文件
curl "http://localhost:8321/v2/workspaces/ws_xxx/files"

# 删除文件
curl -s -X DELETE "http://localhost:8321/v2/workspaces/ws_xxx/files/old.md"
```

### 6.9 SSE 事件流

通过 Server-Sent Events 实时接收事件：

```bash
curl -N "http://localhost:8321/v2/events"
```

事件类型：

- `task.created` — 新任务创建
- `task.completed` — 任务完成
- `task.failed` — 任务失败
- `task.blocked` — 任务阻塞
- `agent.registered` — Agent 注册

### 6.10 Admin API

```bash
# 创建 OAuth 客户端（需要 registry:admin scope）
curl -s -X POST http://localhost:8321/admin/clients \
  -H "Authorization: Bearer eyJ..." \
  -H "Content-Type: application/json" \
  -d '{"name": "my-app", "scopes": ["task:read", "task:write"]}'

# 列出 OAuth 客户端
curl -s -H "Authorization: Bearer eyJ..." http://localhost:8321/admin/clients

# 查看审计日志
curl -s -H "Authorization: Bearer eyJ..." http://localhost:8321/admin/audit

# 删除 OAuth 客户端
curl -s -X DELETE -H "Authorization: Bearer eyJ..." http://localhost:8321/admin/clients/client_xxx
```

### 6.11 Admin WebSocket

连接到 Admin WebSocket 实时接收任务更新：

```bash
# WebSocket URL
ws://localhost:8321/ws/admin

# 订阅消息格式
{"type": "subscribe", "task_ids": ["t_xxx", "t_yyy"]}

# 接收更新
{"type": "task_update", "task_id": "t_xxx", "status": "completed"}
```

### 6.12 系统端点

```bash
# 健康检查
curl http://localhost:8321/health
# → {"agents_alive": 3, "agents_stale": 0, "version": "1.0.0", ...}

# Registry 自身 Agent Card
curl http://localhost:8321/.well-known/agent-card.json

# Prometheus 指标
curl http://localhost:8321/metrics

# OAuth 元数据
curl http://localhost:8321/.well-known/oauth-authorization-server

# JWT 公钥
curl http://localhost:8321/.well-known/jwks.json
```

---

## 7. CLI 使用指南

### 7.1 全局命令

```bash
a2a-registry --version         # 查看版本
a2a-registry server            # 启动服务器（默认命令）
python -m simple_a2a_registry  # 等价于 a2a-registry
```

### 7.2 服务器管理

```bash
# 开发模式启动
a2a-registry --port 8321 --log-format text --log-level DEBUG

# 生产模式启动
a2a-registry --port 8321 \
  --auth-enabled true \
  --bootstrap-secret "prod-secret" \
  --log-format json \
  --log-file /var/log/a2a-registry/server.log

# 使用 MySQL 并指定编排参数
a2a-registry \
  --board-path /data/board.db \
  --dispatcher-interval 10 \
  --claim-ttl 1800 \
  --failure-limit 5
```

### 7.3 task 子命令

```bash
# 列出任务
a2a-registry task list

# 按状态过滤
a2a-registry task list --status running

# 按指定人过滤
a2a-registry task list --assignee coder-agent

# JSON 格式输出
a2a-registry task list --json

# 查看任务详情
a2a-registry task show t_xxx
```

### 7.4 agent 子命令

```bash
# 列出所有 Agent
a2a-registry agent list

# 查看 Agent 详情
a2a-registry agent show a2a-xxx

# 按状态筛选
a2a-registry agent list --status alive

# 注册 Agent
a2a-registry agent register \
  --name "新Agent" \
  --description "CLI 注册" \
  --skill Python \
  --tag demo

# 发送心跳
a2a-registry agent heartbeat a2a-xxx

# 切换启用/禁用
a2a-registry agent toggle a2a-xxx

# 注销
a2a-registry agent unregister a2a-xxx

# 统计信息
a2a-registry agent stats

# 清理过期 Agent
a2a-registry agent purge-stale
```

### 7.5 history 子命令（审计日志）

```bash
# 查看审计日志
a2a-registry history list

# 按事件类型过滤
a2a-registry history list --event-type task_completed

# 按时间范围
a2a-registry history list --from "2026-01-01" --to "2026-06-01"

# 查看事件详情
a2a-registry history show event_xxx

# JSON 输出
a2a-registry history list --json
```

### 7.6 workflow 子命令

```bash
# 应用声明式工作流
a2a-registry workflow apply examples/sequential-workflow.yaml

# 验证工作流定义（不执行）
a2a-registry workflow validate examples/diamond-workflow.yaml

# 查看工作流
a2a-registry workflow show wf_xxx
```

---

## 8. Agent 开发指南

### 8.1 A2A Agent Card 规范

Agent Card 是 A2A 协议的核心元数据结构，描述 Agent 的能力和接入方式：

```json
{
  "name": "My Agent",
  "description": "A useful AI agent",
  "version": "1.0.0",
  "url": "http://agent-host:9001/.well-known/agent-card.json",
  "agent_card": {
    "skills": [
      {"id": "code-review", "name": "Code Review", "description": "审查代码质量"}
    ],
    "default_input_modes": ["text/plain"],
    "default_output_modes": ["text/plain"],
    "capabilities": ["task/read", "task/write"]
  }
}
```

必填字段：

| 字段 | 类型 | 说明 |
|------|------|------|
| `name` | string | Agent 名称 |
| `description` | string | 简要描述 |
| `default_input_modes` | string[] | 接受的输入格式 |
| `default_output_modes` | string[] | 支持的输出格式 |

### 8.2 最小 Agent 示例

```python
"""simple_a2a_agent.py — 最小 A2A Agent 示例"""
from simple_a2a_registry.client import A2AClient

client = A2AClient(registry_url="http://localhost:8321")

# 1. 注册
agent_id = client.register_agent(
    name="Simple Agent",
    description="最小的 A2A Agent",
    skills=["echo"],
)

# 2. 心跳保活
client.heartbeat(agent_id)

# 3. 设置任务处理函数
def handle_task(task):
    task_id = task["id"]
    query = task.get("query", "")
    client.report_progress(task_id, status="working")
    # 处理任务...
    result = {"text": f"处理完成: {query}"}
    client.report_result(task_id, result)

client.dispatch_handler = handle_task

# 4. 连接 WebSocket
client.connect_websocket(agent_id)

# 保持运行
import time
while True:
    client.heartbeat(agent_id)
    time.sleep(60)
```

### 8.3 完整 A2A Agent（含 OAuth + WS）

参考项目中的 `examples/a2a_coder_agent.py`，这是一个生产级 Agent 示例：

```bash
export OAUTH_CLIENT_ID=client-xxx
export OAUTH_CLIENT_SECRET=secret-xxx
python examples/a2a_coder_agent.py
```

该示例包含：

- OAuth 客户端凭据获取和自动刷新
- WebSocket 长连接（指数退避重连）
- AgentCard 注册和 30 秒心跳
- A2A JSON-RPC over HTTP（端口 9001）
- 通过 Hermes CLI 执行任务

### 8.4 WebSocket 连接协议

Agent 通过 WebSocket 连接接收实时任务推送：

```python
import aiohttp
import json

async def agent_ws_loop():
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(
            "http://localhost:8321/v1/agents/a2a-xxx/ws"
        ) as ws:
            async for msg in ws:
                data = json.loads(msg.data)
                if data["type"] == "task":
                    task_id = data["id"]
                    # 处理任务...
                    await ws.send_json({
                        "type": "task_result",
                        "id": task_id,
                        "result": {"status": "completed", "output": "..."},
                    })
```

### 8.5 HTTP Callback 模式

对于无法维持 WebSocket 长连接的 Agent，可以使用 HTTP 轮询：

```python
# Agent 定期发心跳，服务器缓存待推送任务
# Agent 通过 HTTP GET 检查是否有新任务
client.heartbeat(agent_id)

# 检查待推送任务
pending = client.get_pending_tasks(agent_id)
for task in pending:
    # 处理...
    client.report_result(task["id"], result)
```

### 8.6 任务生命周期（Agent 视角）

```
     ┌────────────┐
     │   READY    │  ← Dispatcher 将任务标记为 READY
     └─────┬──────┘
           │ Worker 调用 POST /claim
           ▼
     ┌────────────┐
     │  RUNNING   │  ← 获得 claim_lock，开始处理
     └─────┬──────┘
           │ 处理中可选: POST /heartbeat 续期锁
           │ 处理中可选: POST /{id}/comment 写入中间结果
           │
     ┌─────┴──────┐
     │  COMPLETED  │  ← POST /complete 完成任务
     └────────────┘

     或失败路径:
     RUNNING → FAILED → (自动重试 到 READY)
     RUNNING → BLOCKED → (人工介入后回到 RUNNING)
```

### 8.7 Subprocess Pool Worker 协议

对于配置了 Subprocess Pool 的持久 Worker，任务通过 stdin 发送 JSON-Line：

```json
{"type": "task", "task_id": "t_xxx", "title": "...", "body": "...",
 "assignee": "coder-agent", "workspace_path": "/tmp/workspaces/t_xxx"}
```

Worker 通过 HTTP API 向 Registry 报告任务状态（claim/complete/fail）。

---

## 9. Kanban 编排深入

### 9.1 完整 8 状态状态机

```
          ┌────────────────────────────────────────────┐
          │                                            │
          v                                            │
    ┌──────────┐    ┌─────────┐    ┌──────────┐       │
    │   TODO   │───→│  READY  │───→│ RUNNING  │       │
    └──────────┘    └─────────┘    └──────────┘       │
                      │    ▲           │    │          │
                      │    │           │    │          │
                      │    │     ┌─────▼────▼──┐      │
                      │    │     │  COMPLETED  │      │
                      │    │     └─────────────┘      │
                      │    │                          │
                      │    │     ┌──────────┐         │
                      │    └─────│  FAILED  │         │
                      │          └──────────┘         │
                      │              │                │
                      │         ┌────▼────┐           │
                      │         │ BLOCKED │           │
                      │         └─────────┘           │
                      │           │    ▲              │
                      │           │    │              │
                      │     ┌─────▼────▼──┐           │
                      │     │  ARCHIVED  │            │
                      │     └────────────┘            │
                      │                               │
                      └───────────────────────────────┘
```

### 9.2 状态说明

| 状态 | 含义 | 进入条件 | 可退出到 |
|------|------|---------|---------|
| `todo` | 等待依赖 | 刚创建，有未完成的父任务 | ready, cancelled |
| `ready` | 可抢占 | 所有父任务完成，已设置 assignee | running, todo, blocked, cancelled |
| `running` | 处理中 | Worker 调用 claim | completed, failed, blocked, cancelled |
| `completed` | 成功完成 | Worker 调用 complete | archived |
| `failed` | 执行失败 | Worker 调用 fail 或超时 | ready（重试）, archived, cancelled |
| `blocked` | 人工介入 | Worker 或系统阻塞 | running, ready, failed, cancelled |
| `cancelled` | 已取消 | 手动取消 | archived |
| `archived` | 已归档 | 清理操作 | 无（终端状态） |

### 9.3 Claim Lock 机制

当 Worker 抢占一个任务时，获得一个独占的 `claim_lock`（UUID）：

- 所有后续状态变更操作都需要携带此锁
- 锁默认 TTL 为 15 分钟（`--claim-ttl` 可配置）
- Worker 可以通过心跳 (`POST /v2/tasks/{id}/heartbeat`) 续期
- 锁过期后 Dispatcher 自动释放，任务标记为 `failed`

### 9.4 DAG 依赖链

- 任务通过 `parents` 字段指定父依赖
- 任务自动保持在 `todo` 状态，直到所有父任务 `completed`
- Dispatcher 自动将 `todo` 升级为 `ready`
- 依赖链支持任意深度的 DAG（有向无环图）

### 9.5 重试与超时

- 任务失败时，若 `consecutive_failures < failure_limit`（默认 3），自动回退到 `ready`
- 每次失败创建一条 `TaskRun` 记录
- 支持超时检测（`task_timeout`，默认 300 秒）
- 超时未完成的任务自动标记为 `failed`

### 9.6 并行组

创建多个无依赖关系且 assignee 不同的任务，Dispatcher 会并行派发：

```bash
# 以下三个任务可同时执行（无相互依赖）
curl -X POST http://localhost:8321/v2/tasks -d '{"title":"Task A","assignee":"worker-1"}'
curl -X POST http://localhost:8321/v2/tasks -d '{"title":"Task B","assignee":"worker-2"}'
curl -X POST http://localhost:8321/v2/tasks -d '{"title":"Task C","assignee":"worker-3"}'
```

### 9.7 进度上报

Worker 在运行过程中可以通过评论上报进度：

```bash
curl -s -X POST http://localhost:8321/v2/tasks/t_xxx/comment \
  -H "Content-Type: application/json" \
  -d '{"author": "coder-1", "body": "进度: 50% - 完成数据库设计"}'
```

### 9.8 交互模式 (interaction_mode)

创建任务时可通过 `interaction_mode` 字段指定交互模式，标识任务在 Agent Runtime Contract 中的治理方式。

**支持的模式：**

| 模式 | 值 | 说明 | 状态机 |
|------|-----|------|--------|
| **TASK** | `"task"` | 异步任务，基于 Claim 的编排任务（默认） | ✅ 经过 Kanban 状态机 |
| **SYNC_CALL** | `"sync_call"` | 同步调用，直接通过 WS 投递，不创建状态机任务 | ❌ 绕过后端状态机 |
| **JOB** | `"job"` | 项目级任务，可分解为子任务 DAG（预留） | ✅ 经过 Kanban 状态机 |

#### 向后兼容

旧 A2A V2 客户端创建请求若未声明 `interaction_mode`，系统**默认按 TASK 处理**（D2 决议）。默认值在 APE 安全检查之前填充，确保旧客户端行为一致。

```bash
# 以下两个等价（v2/tasks 默认 interaction_mode=task）
curl -s -X POST http://localhost:8321/v2/tasks \
  -H "Content-Type: application/json" \
  -d '{"title": "默认交互模式的任务"}'

curl -s -X POST http://localhost:8321/v2/tasks \
  -H "Content-Type: application/json" \
  -d '{"title": "显式指定模式的任务", "interaction_mode": "task"}'
```

#### 适用场景

| 场景 | 推荐模式 | 原因 |
|------|---------|------|
| 常规 Kanban 编排任务 | TASK | 需要状态机、依赖链、重试、HITL |
| 实时 Agent 技能调用 | SYNC_CALL | 低延迟、无状态、不关心任务编排 |
| 多子任务分解 | JOB | 项目级任务，可拆分为子 DAG |

---

## 10. 安全管理

### 10.1 OAuth 2.1 认证

| 特性 | 说明 |
|------|------|
| Grant Type | `client_credentials`（客户端模式） |
| Token 类型 | JWT (JSON Web Token) |
| 签名算法 | HS256（单实例默认）/ RS256（多实例） |
| 过期时间 | 默认 1 小时（可通过 `jwk_ttl` 配置） |
| Scope 鉴权 | 按端点配置的最小 Scope 校验 |

#### 认证流程图

```
Agent / Client                    Registry
     │                               │
     │  POST /auth/token              │
     │  grant_type=client_credentials │
     │  client_id=xxx                │
     │  client_secret=yyy            │
     │                               │
     │←── access_token + expires_in ─│
     │                               │
     │  GET /v1/agents               │
     │  Authorization: Bearer ***  │
     │                               │
     │←── 200 OK [agent list] ───────│
```

#### RS256 模式（多实例）

在多 Registry 实例部署时，需使用 RS256 非对称加密：

```yaml
# config.yaml
auth:
  enabled: true
  rsa_key_size: 2048
  jwk_ttl: 3600
```

- 私钥仅服务端持有
- 公钥通过 `/.well-known/jwks.json` 公开
- Agent 可用公钥离线验证 Token

### 10.2 Security Harness（安全防护框架）

Security Harness 提供三层安全防护：

| 组件 | 全称 | 职责 |
|------|------|------|
| **APE** | Authorization Policy Engine | 授权策略引擎，基于策略的访问控制 |
| **DTM** | Delegated Token Manager | 委托令牌管理，支持 scope 衰减 |
| **PT** | Provenance Tracker | 溯源追踪，记录操作链条 |

#### 三种运行模式

| 模式 | 说明 | 适用场景 |
|------|------|---------|
| `enforce` | 严格模式，拒绝不符合策略的操作 | 生产环境 |
| `warn` | 警告模式，记录但不阻止 | 灰度测试 |
| `audit` | 审计模式，仅记录不干预 | 数据采集 |

#### 配置示例

```yaml
security_harness:
  enabled: true
  mode: enforce                    # 生产环境建议 enforce
  default_delegation_policy: open
  delegation_token_ttl_seconds: 300
  max_delegation_depth: 10
```

### 10.3 速率限制

Token Bucket 算法，支持双后端：

```yaml
rate_limit:
  enabled: true
  default_unauthenticated: 60     # 匿名 60 次/分钟
  default_authenticated: 300      # 认证 300 次/分钟
  storage: mysql                  # memory（单机）或 mysql（集群）
  whitelist: ["my-super-agent"]   # 豁免列表
```

密钥推导优先级：
1. `client_id`（已认证请求）
2. `X-Forwarded-For` / `X-Real-IP` 头
3. TCP 连接 `remote` 地址

### 10.4 审计日志

所有敏感操作写入 `audit_log` 表，追加写入、不可篡改：

| 字段 | 说明 |
|------|------|
| `event_type` | 事件类型（agent_register, token_issue, admin_action...） |
| `actor` | 执行者 |
| `target` | 操作对象 |
| `timestamp` | 时间戳 |
| `success` | 是否成功 |
| `details` | 详细上下文 |

配置保留期（默认 90 天）：

```yaml
audit:
  retention_days: 90
```

### 10.5 Token 类型对比

| 类型 | 适用场景 | 优点 | 缺点 |
|------|---------|------|------|
| HS256 | 单实例部署 | 简单，无需管理密钥对 | 多实例需共享密钥 |
| RS256 | 多实例 / 第三方验证 | 公钥验证，无需共享私钥 | 需要管理密钥对 |

---

## 11. 多租户

### 11.1 租户隔离机制

所有 V2 任务和 Agent 支持租户级隔离：

- **`X-Tenant-ID`** 请求头：客户端身份传播
- **`?tenant=`** 查询参数：API 调用时指定租户
- 不同租户的数据在存储层完全隔离
- 租户信息贯穿 Claim、Dispatch、Audit 全链路

### 11.2 使用示例

```bash
# 创建任务（指定租户）
curl -s -X POST http://localhost:8321/v2/tasks?tenant=team-a \
  -H "Content-Type: application/json" \
  -d '{"title": "团队A的任务", "assignee": "agent-a"}'

# 跨租户查询（仅返回当前租户数据）
curl -s -H "X-Tenant-ID: team-a" http://localhost:8321/v2/tasks

curl -s -H "X-Tenant-ID: team-b" http://localhost:8321/v2/tasks
# 返回不同的结果集
```

### 11.3 JWT 租户声明

Token 中也可以包含租户声明：

```json
{
  "sub": "client-xxx",
  "tenant": "team-a",
  "scopes": ["task:read", "task:write"]
}
```

系统会校验 Token 中的租户与请求中的 `X-Tenant-ID` 是否一致。

---

## 12. 扩展性

### 12.1 插件系统

Plugin 系统支持在三个维度的钩子点扩展功能：

#### 生命周期钩子

| 钩子 | 触发时机 | 用途 |
|------|---------|------|
| `load(config)` | 插件加载时 | 初始化配置 |
| `init(app)` | 应用启动时 | 注册路由、连接资源 |
| `before_shutdown(app)` | 应用关闭前 | 清理资源 |

#### 请求钩子

| 钩子 | 触发时机 | 用途 |
|------|---------|------|
| `before_request(request)` | 每个 HTTP 请求前 | 日志、认证、修改请求 |
| `after_request(request, response)` | 每个 HTTP 请求后 | 日志、修改响应 |

#### 事件钩子

| 钩子 | 说明 |
|------|------|
| `on_agent_register(agent_id, card)` | Agent 注册时 |
| `on_agent_deregister(agent_id)` | Agent 注销时 |
| `on_agent_heartbeat(agent_id)` | Agent 心跳时 |
| `on_task_created(task_id, task)` | 任务创建时 |
| `on_task_completed(task_id, result)` | 任务完成时 |
| `on_token_issued(client_id, token)` | Token 签发时 |
| `on_server_start(app)` | 服务器启动时 |
| `on_server_stop(app)` | 服务器停止时 |

#### 最小插件示例

```python
from simple_a2a_registry.plugin import Plugin

class MyPlugin(Plugin):
    @property
    def name(self) -> str:
        return "my-plugin"

    async def load(self, config: dict):
        print(f"Plugin loaded with config: {config}")

    async def before_request(self, request):
        # 在每个请求前注入自定义逻辑
        return None  # 返回 None 继续执行

    async def on_agent_register(self, agent_id: str, card: dict):
        print(f"Agent registered: {agent_id}")
```

#### 加载方式

**方式一：Entry Points（pyproject.toml）**

```toml
[project.entry-points."simple_a2a_registry.plugins"]
my-plugin = "my_package:MyPlugin"
```

**方式二：Config 文件（config.yaml）**

```yaml
plugins:
  my-plugin:
    module: my_package.my_plugin
    config:
      api_key: "xxx"
      endpoint: "https://api.example.com"
```

### 12.2 Webhook

支持 HTTP 回调通知，HMAC 签名验证：

```bash
# 创建 Webhook 订阅
curl -s -X POST http://localhost:8321/v2/webhooks \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://myapp.com/webhook",
    "events": ["task.completed", "task.failed"],
    "secret": "my-hmac-secret"
  }'
```

Webhook 特性：

- **HMAC-SHA256 签名**：请求头 `X-Signature` 验证消息完整性
- **自动重试**：发送失败自动重试（指数退避，最多 3 次）
- **自动禁用**：连续失败后自动禁用 webhook
- **事件过滤**：按事件类型订阅

### 12.3 事件总线 + SSE

通过 Server-Sent Events 实时消费事件流：

```bash
curl -N "http://localhost:8321/v2/events"
```

事件示例：

```
event: task.completed
data: {"task_id": "t_xxx", "assignee": "coder-agent", "result": {...}}
```

---

## 13. 监控与运维

### 13.1 Prometheus 指标

`GET /metrics` 端点暴露以下指标：

| 指标名称 | 类型 | Labels | 说明 |
|----------|------|--------|------|
| `a2a_registry_requests_total` | Counter | endpoint, method, status | HTTP 请求总数 |
| `a2a_registry_request_duration_seconds` | Histogram | endpoint, method | 请求延迟分布 |
| `a2a_registry_auth_operations_total` | Counter | operation, success | 认证操作计数 |
| `a2a_registry_agents_alive` | Gauge | — | 活跃 Agent 数 |
| `a2a_registry_agents_stale` | Gauge | — | 过期 Agent 数 |
| `a2a_registry_ws_connections` | Gauge | — | WS 连接数 |
| `a2a_registry_admin_ws_connections` | Gauge | — | Admin WS 连接数 |
| `a2a_registry_db_pool_size` | Gauge | — | 数据库连接池大小 |
| `a2a_registry_db_query_duration_seconds` | Histogram | operation | 查询延迟 |
| `a2a_registry_tasks_*` | Gauge | status | 各状态任务计数 |

Prometheus 配置示例：

```yaml
scrape_configs:
  - job_name: 'a2a-registry'
    static_configs:
      - targets: ['localhost:8321']
    metrics_path: '/metrics'
```

### 13.2 健康检查

```bash
curl http://localhost:8321/health
```

响应示例：

```json
{
  "status": "healthy",
  "version": "1.0.0",
  "uptime_seconds": 86400,
  "agents_alive": 5,
  "agents_stale": 0,
  "agents_total": 5,
  "db_driver": "mysql",
  "auth_enabled": true
}
```

### 13.3 结构化日志

日志格式支持两种模式：

**开发模式（text）：**

```
2026-06-15 10:00:00 INFO  [request_id=abc123] POST /v1/agents → 200 45ms
2026-06-15 10:00:01 INFO  [request_id=def456] Agent registered: a2a-xxx
```

**生产模式（json），可被 ELK/Loki 消费：**

```json
{
  "time": "2026-06-15T10:00:00Z",
  "level": "INFO",
  "request_id": "abc123",
  "method": "POST",
  "path": "/v1/agents",
  "status": 200,
  "duration_ms": 45
}
```

配置方式：

```bash
a2a-registry --log-format json --log-file /var/log/a2a-registry/server.log
```

### 13.4 速率限制运维

```bash
# 查看当前限流状态（内存模式）
# 暂未提供 API，可通过 Prometheus 指标监控

# 配置文件热加载方式修改限流配置
# 修改 config.yaml 后重启服务
```

### 13.5 数据库运维

```bash
# SQLite WAL 模式（默认启用，适合开发）
# 查看数据库状态
sqlite3 ~/.simple-a2a-registry/registry.db "PRAGMA wal_checkpoint;"

# MySQL 连接池配置
# pool_size 和 max_overflow 在 config.yaml 中配置
```

**数据库迁移：**

```bash
# Alembic 迁移管理
alembic upgrade head    # 升级到最新版本
alembic downgrade -1    # 回退一个版本
alembic history         # 查看迁移历史
```

### 13.6 Web Dashboard

浏览器打开 `http://localhost:8321`：

- **Agent 列表**：查看状态（alive/stale/disabled）、技能、标签、WS 连接状态
- **Kanban 看板**：Board 视图（按状态分列）和 List 视图（可排序表格）
- **任务详情**：Modal 弹窗展示依赖链、运行历史、事件流、评论线程
- **统计面板**：实时 Agent 数量和编排统计（每 15 秒刷新）

Dashboard 登录（当 auth 启用时）：

```bash
curl -s -X POST http://localhost:8321/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username": "admin", "password": "xxx"}'
# 返回 session cookie，浏览器会自动携带
```

---

## 14. 测试与验证

### 14.1 运行测试

```bash
# 进入项目目录
cd simple-a2a-registry

# 运行所有测试（需要 dev 依赖）
pytest tests/ -v

# 运行特定模块
pytest tests/test_orchestration_api.py -v  # V2 API 测试
pytest tests/test_swarm.py -v               # Swarm 测试
pytest tests/test_auth.py -v                # OAuth 测试
pytest tests/test_store.py -v               # 存储层测试
pytest tests/test_websocket.py -v           # WebSocket 测试
pytest tests/test_tenant_isolation.py -v    # 多租户测试

# 运行慢测试（压力测试）
pytest -m slow -v

# 带覆盖率
pytest tests/ --cov=simple_a2a_registry --cov-report=html
```

### 14.2 验证基本功能

**验证服务运行：**

```bash
curl http://localhost:8321/health
# → {"status": "healthy", "version": "1.0.0", ...}
```

**验证 Agent 注册：**

```bash
# 注册
AGENT_ID=$(curl -s -X POST http://localhost:8321/v1/agents \
  -H "Content-Type: application/json" \
  -d '{"name": "test-agent", "skills": ["test"]}' | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")
echo "Agent ID: $AGENT_ID"

# 列表
curl -s http://localhost:8321/v1/agents | python3 -m json.tool
```

**验证编排引擎：**

```bash
# 创建任务
TASK_ID=$(curl -s -X POST http://localhost:8321/v2/tasks \
  -H "Content-Type: application/json" \
  -d '{"title": "验证任务", "assignee": "test-agent", "priority": 1}' | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")
echo "Task ID: $TASK_ID"

# 查询状态
curl -s "http://localhost:8321/v2/tasks/$TASK_ID" | python3 -m json.tool

# 统计
curl -s http://localhost:8321/v2/stats | python3 -m json.tool
```

**验证 Prometheus 指标：**

```bash
curl -s http://localhost:8321/metrics | grep a2a_registry
```

### 14.3 测试分类速查

| 测试文件 | 覆盖内容 |
|----------|---------|
| `test_store.py` | Agent + OAuth 持久化 |
| `test_models.py` | Agent Card 数据模型 |
| `test_server.py` | HTTP API 集成 |
| `test_auth.py` | OAuth 2.1 流程 |
| `test_orchestration_api.py` | V2 REST API |
| `test_orchestration_store.py` | TaskStore CRUD |
| `test_orchestration_state_machine.py` | 状态转换 |
| `test_orchestration_e2e.py` | 编排端到端 |
| `test_swarm.py` | Swarm 拓扑 |
| `test_dispatcher.py` | 后台派发器 |
| `test_rate_limiter.py` | Token Bucket |
| `test_validation.py` | 输入校验 |
| `test_errors.py` | 错误处理 |
| `test_log.py` | 日志系统 |
| `test_config.py` | 配置加载 |
| `test_cors.py` | CORS 中间件 |
| `test_concurrency.py` | 线程安全 |
| `test_users.py` | 用户管理 |
| `test_tenant_isolation.py` | 多租户隔离 |
| `test_tenant_e2e.py` | 租户端到端 |
| `test_websocket.py` | WebSocket 协议 |
| `test_workspace.py` | 工作区管理 |
| `test_mysql_compat.py` | MySQL 兼容性 |
| `test_tls.py` | TLS/SSL |
| `test_bootstrap_admin.py` | 启动管理客户端 |
| `test_performance_benchmark.py` | 性能基准 |

---

## 15. 故障排除

### 15.1 服务启动失败

```
症状：a2a-registry 启动后立即退出
```

**检查清单：**

| 检查项 | 命令 | 说明 |
|--------|------|------|
| Python 版本 | `python --version` | 需要 >= 3.10 |
| 端口占用 | `lsof -i :8321` | 默认端口 8321 |
| 数据库路径可写 | `ls -la ~/.simple-a2a-registry/` | 检查目录权限 |
| SQLite 版本 | `sqlite3 --version` | 需要 >= 3.38 |

### 15.2 任务一直处于 pending/todo

```
症状：任务创建后长时间不执行
```

**排查步骤：**

1. **检查 Dispatcher 是否启动**
   ```bash
   # 启动日志中搜索 "dispatcher"
   # 或检查进程
   ps aux | grep a2a-registry
   ```

2. **检查 Agent 是否在线**
   ```bash
   curl http://localhost:8321/v1/agents
   # status 应该为 alive
   ```

3. **检查依赖是否完成**
   ```bash
   curl "http://localhost:8321/v2/tasks/${TASK_ID}"
   # 查看 parents 字段是否全部 completed
   ```

4. **检查是否设置了 assignee**
   ```bash
   # 没有 assignee 的任务不会进入 ready 状态
   ```

5. **检查熔断状态**
   ```bash
   curl -s "http://localhost:8321/v2/tasks?status=failed"
   # 查看是否有连续失败触发了熔断
   ```

6. **检查 Dispatcher 是否被禁用**
   ```bash
   # 启动时是否有 --dispatcher-enabled=false 参数
   ```

### 15.3 Agent 连接异常

```
症状：Agent 注册成功但收不到任务
```

**排查步骤：**

1. **检查心跳状态**
   ```bash
   curl "http://localhost:8321/v1/agents/${AGENT_ID}"
   # status 是否为 alive
   ```

2. **检查 WebSocket 连接**
   ```bash
   curl "http://localhost:8321/v1/agents/${AGENT_ID}" | python3 -c "import sys,json; d=json.load(sys.stdin); print('WS connected:', d.get('ws_connected','?'))"
   ```

3. **手动发送心跳**
   ```bash
   curl -s -X POST "http://localhost:8321/v1/agents/${AGENT_ID}/heartbeat"
   ```

4. **检查 Agent 是否被禁用**
   ```bash
   curl "http://localhost:8321/v1/agents/${AGENT_ID}"
   # 检查 disabled 字段
   ```

### 15.4 任务失败

```
症状：任务创建后显示 failed
```

**常见原因：**

| 原因 | 解决方案 |
|------|---------|
| Worker 进程崩溃 | 检查 Worker 日志 |
| 任务超时 | 调整 `--claim-ttl` 或 `--dispatcher-interval` |
| 锁过期 | Worker 需要更频繁地发送心跳续期 |
| Scope 不足 | 检查 Token 是否包含所需 Scope |

### 15.5 认证问题

```
症状：401 Unauthorized 或 403 Forbidden
```

**检查清单：**

- Token 是否过期（`expires_in` 默认 1 小时）
- Token 是否包含所需 Scope（见 [6.1 Scope 权限速查](#scope-权限速查)）
- 是否使用了正确的 Header 格式：`Authorization: Bearer eyJ...`
- 服务端是否已开启认证：`--auth-enabled true`

**获取新 Token：**

```bash
curl -s -X POST http://localhost:8321/auth/token \
  -d "grant_type=client_credentials" \
  -d "client_id=your-client-id" \
  -d "client_secret=your-secret"
```

### 15.6 数据库性能问题

```
症状：响应变慢，高延迟
```

| 场景 | 建议 |
|------|------|
| SQLite 单库 > 100MB | 考虑迁移到 MySQL |
| 日任务量 > 10 万 | 配置定期归档策略 |
| 审计日志增长过快 | 调整 `retention_days` |
| 连接池不够 | 增加 `pool_size` / `max_overflow` |

### 15.7 限流相关问题

```
症状：收到 429 Too Many Requests
```

**解决方法：**

1. 检查是否需要如此高频请求
2. 在 `rate_limit.whitelist` 中添加豁免
3. 增大 `default_unauthenticated` / `default_authenticated` 限制
4. 确保已认证请求携带 Token（已认证用户有更高配额）

---

## 16. FAQ

### Q1: Simple A2A Registry 和 Google A2A 协议的关系？

A: 本系统基于 Google A2A (Agent-to-Agent) 协议的 Agent Card 和任务模型规范，同时扩展了 Kanban 编排引擎、OAuth 2.1 安全、多租户隔离、插件系统和企业级可观测性。

### Q2: 开发环境和生产环境有什么区别？

| 维度 | 开发环境 | 生产环境 |
|------|---------|---------|
| 认证 | 默认关闭 | 必须开启 `--auth-enabled true` |
| 数据库 | SQLite（默认） | MySQL（推荐） |
| 日志 | text 格式 | json 格式 |
| 限流 | 默认关闭 | 建议开启 |

### Q3: 支持哪些数据库？

- **SQLite**：开发环境，单文件，默认启用 WAL 模式
- **MySQL 8.0+**：生产环境，QueuePool 连接池，Alembic 迁移管理
- 支持自动 SQL 方言转换（placeholder, DDL 语句）

### Q4: 多个 Registry 实例如何做高可用？

- 使用 MySQL 作为共享数据库
- 配置 RS256 签名（非对称密钥对）
- 使用反向代理（如 Nginx）做负载均衡
- 参考 `docker-compose.yml` 中的 `--scale registry=3`

### Q5: Agent 必须使用 WebSocket 吗？

不必须。两种模式：

| 模式 | 优点 | 缺点 |
|------|------|------|
| HTTP 心跳 | 简单，无需长连接 | 轮询延迟，不实时 |
| WebSocket | 实时推送，低延迟 | 需要维持长连接 |

建议：对实时性要求高的场景使用 WS，对简单监控任务使用 HTTP 心跳。

### Q6: 如何监控 Registry 的健康状态？

```bash
# 使用健康检查端点
curl http://localhost:8321/health

# 集成 Prometheus + Grafana
# 参考 13.1 节的指标说明

# Docker Compose 已内置 healthcheck
docker compose ps
```

### Q7: 支持哪些 Agent 开发语言？

Registry 的 API 是标准 REST + WebSocket，任何语言都可以接入。官方提供了 Python SDK（`A2AClient`），其他语言的 SDK 可通过 API 规范自行实现。

### Q8: 编排任务的优先级如何工作？

优先级是 Dispatcher 排队的权重依据，数值越大优先级越高（`priority` 字段）。Dispatcher 轮询时优先处理高优先级任务。

### Q9: 如何清理过期数据？

- **过期 Agent**：`a2a-registry agent purge-stale` 或 API 自动清理
- **审计日志**：通过 `audit.retention_days` 配置保留天数
- **已完成任务**：可定期调用 DELETE /v2/tasks/{id} 归档

### Q10: Agent Memory 的数据会丢失吗？

不会，Agent Memory 存储在 SQLite/MySQL 中，持久化保存。通过 `ttl` 字段设置自动过期时间，`ttl=0` 表示永不过期。

### Q11: 什么是 Swarm Blackboard？

Blackboard（黑版）是 Swarm 拓扑中各 Worker 共享中间结果的结构化评论区。Worker 通过 `[swarm:blackboard]` 前缀的评论写入数据，Verifier 和 Synthesizer 可以读取和汇总。

### Q12: 如何备份数据？

```bash
# SQLite
sqlite3 ~/.simple-a2a-registry/registry.db ".backup /backup/registry-$(date +%Y%m%d).db"

# MySQL
mysqldump -u a2a -p a2a_registry > /backup/a2a_registry-$(date +%Y%m%d).sql
```

### Q13: 如何贡献代码或报告问题？

请访问项目的 GitHub 仓库，提交 Issue 或 Pull Request。贡献指南见项目的 `CONTRIBUTING.md`（如有）或 README 中的"Contributing"章节。

---

> **文档版本**: 1.0 | **最后更新**: 2026-06-15
>
> 本文档面向运维人员、AI Agent 开发者和平台管理员。如有疑问或建议，请提交 Issue 或联系项目维护团队。