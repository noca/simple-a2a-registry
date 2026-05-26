# Simple A2A Registry — 架构设计

## 概述

Simple A2A Registry 是一个轻量级的 Agent 注册与发现服务，基于 Google A2A (Agent-to-Agent) 协议。Agent 通过 API 注册，依赖心跳（HTTP 或 WebSocket）保活。

除基础注册/发现功能外，Registry 还支持 **WebSocket 持久连接** 和 **任务分发**，使客户端能够通过 Registry 向 Agent 提交任务并获取结果。

## 组件

```
simple_a2a_registry/
  cli.py          — argparse CLI 入口
  server.py       — aiohttp REST API + WebSocket + 任务分发
  store.py        — 持久化注册状态（JSON 文件）+ 心跳管理
  models.py       — A2A Agent Card 数据模型（无 Pydantic 依赖）
  static/         — Web Dashboard（HTML+JS）
tests/
  test_store.py   — 存储层单元测试
  test_models.py  — 数据模型单元测试
  test_server.py  — HTTP API 集成测试（含 WS/dispatch/task）
```

## 数据流

### 经典模式：HTTP 注册 + 心跳轮询

```
         ┌───────────────┐
         │  External Agent│
         └──┬────────────┘
            │ POST /v1/agents + heartbeat every 2min
            ▼
    ┌───────────────┐         ┌──────────────┐
    │  A2A Registry  │────────▶ registry.json │
    │  (aiohttp)     │         └──────────────┘
    └────────────────┘
```

### 新增模式：WebSocket Hub-and-Spoke + 任务分发

```
         ┌─────────────┐
         │   Client    │
         │  (调用方)    │
         └──────┬──────┘
                │ POST /v1/agents/{id}/dispatch
                ▼
    ┌───────────────────────────────────┐
    │          A2A Registry             │
    │                                   │
    │  ┌──────────────────────────┐     │
    │  │  Task Store (内存)        │     │
    │  │  task_id → {state,result}│     │
    │  └──────────────────────────┘     │
    │                                   │
    │  ┌──────────────────────────┐     │
    │  │  WS Hub (连接管理器)      │     │
    │  │  agent_id ↦ WebSocket    │     │
    │  └──────────────────────────┘     │
    └──────┬────────────────────┬───────┘
           │ WS: {"type":"task"}│        │ Client 轮询
           ▼                    │ GET /v1/tasks/{id}
    ┌──────────────┐            ▼
    │  Agent A     │    ┌──────────────┐
    │ (WS 长连接)   │    │  Client      │
    │              │    │ (轮询结果)    │
    └──────────────┘    └──────────────┘

注意：Agent 也可主动通过 WS 推送结果
      (task_result / task_progress)
```

### 完整工作流：客户端 → Registry → Agent

```
Client                  Registry                Agent (WS 连接)
  │                        │                        │
  │  POST /dispatch        │                        │
  │  ─────────────────────→│                        │
  │  202 {task_id}         │                        │
  │  ←─────────────────────│                        │
  │                        │  WS: {"type":"task"}   │
  │                        │  ─────────────────────→│
  │                        │                        │  处理中
  │                        │  WS: {"type":"task_    │
  │                        │       progress"}       │
  │                        │  ←─────────────────────│
  │                        │                        │  ...处理完成
  │                        │  WS: {"type":"task_    │
  │                        │       result"}          │
  │                        │  ←─────────────────────│
  │  GET /v1/tasks/{id}    │                        │
  │  ─────────────────────→│                        │
  │  200 {state, result}   │                        │
  │  ←─────────────────────│                        │
```

## 关键组件说明

### server.py — RegistryHandler

| 属性/方法 | 说明 |
|-----------|------|
| `_ws_connections` | `Dict[str, WebSocketResponse]` — 管理所有 Agent 的 WS 连接 |
| `_tasks` | `Dict[str, Dict]` — 内存中的任务存储（task_id → 任务状态/结果） |
| `handle_ws()` | WebSocket 端点处理器，支持 ping/pong、task_result、task_progress、close |
| `handle_dispatch()` | 任务分发入口，通过 WS 向 Agent 推送任务 |
| `handle_get_task()` | 任务状态查询 |
| `_cleanup_task()` | 后台任务：每 60 秒清理过期 Agent 和断开的 WS 连接 |

### store.py — A2ARegistryStore

| 机制 | 数值 | 说明 |
|------|------|------|
| `HEARTBEAT_TIMEOUT` | 120 秒 | 超过此时间无心跳的 Agent 标记为 stale |
| `HEARTBEAT_PURGE` | 300 秒 | 超过此时间的 stale Agent 被彻底清除 |
| 持久化 | JSON 文件 | 原子写入（tmp + replace），保存在 `~/.simple-a2a-registry/registry.json` |

### Task Store（内存任务存储）

- 位于 `RegistryHandler._tasks`，纯内存结构
- 任务通过 `handle_dispatch()` 创建，状态流转：`dispatched → forwarded → working → completed/failed`
- Agent 也可以通过 WS 主动推送 `task_result`/`task_progress` 消息来创建或更新任务
- **当前无自动过期机制** — 长时间运行需注意内存增长

### WebSocket Hub

- Hub-and-Spoke 拓扑：Registry 作为中心 Hub，每个 Agent 作为 Spoke 建立一条 WS 连接
- 同一 Agent 的第二个 WS 连接会替换第一个（发送 "replaced" 关闭旧连接）
- 后台清理任务定期清除已关闭的 WS 连接

## 设计决策

| 决策 | 选择 | 原因 |
|------|------|------|
| 数据模型 | Pydantic-free dataclass | 零依赖，仅需 `aiohttp` |
| 持久化 | JSON 原子写入 | 简单可靠，适合单实例场景 |
| 心跳模型 | 120s 超时 / 300s 清理 | 平衡网络波动容忍度和资源回收时效 |
| 任务存储 | 内存 | 适合轻量级、临时性任务追踪 |
| 认证 | OAuth 2.1 (JWT) | A2A v1.0 规范要求 SecurityScheme 集成 |
| 任务分发 | WebSocket 推送 | 低延迟、双向通信，优于 HTTP 轮询 |

---

## Agent Card v1.0 数据模型变更

### 变更概览

Agent Card 数据结构从 v0.x 对齐至 A2A v1.0 protobuf 规范（[a2a.proto](https://github.com/a2aproject/A2A/blob/main/specification/a2a.proto)），字段映射如下：

| v0.x 字段 | v1.0 字段 | 变更说明 |
|-----------|-----------|----------|
| `id` | — | 移除，Agent 通过 URL 标识 |
| `name` | `name` (REQUIRED) | 保留 |
| `description` | `description` (REQUIRED) | 保留 |
| `url` | `supported_interfaces` (REQUIRED) | 替换为列表，支持多个接口 |
| `version` | `version` (REQUIRED) | 保留 |
| `capabilities.skills` | `skills` (REQUIRED) | 提升为顶级字段 |
| `capabilities` | `capabilities` (REQUIRED) | 重构为 `streaming` / `pushNotifications` / `extensions` |
| `provider` | `provider` | 字段对齐，保留 `organization` |
| `authentication` | `security_schemes` + `security_requirements` | 完全重写 |
| `notification` | — | 移除（通过 `capabilities.pushNotifications` 表达） |
| `tags` | — | 移除 |
| `metadata` | — | 移除 |
| — | `documentation_url` | 新增 |
| — | `default_input_modes` (REQUIRED) | 新增，如 `["text/plain"]` |
| — | `default_output_modes` (REQUIRED) | 新增，如 `["text/plain"]` |
| — | `signatures` | 新增，AgentCard JWS 签名 |
| — | `icon_url` | 新增 |

### 安全模型（新增）

Agent Card 新增 `security_schemes` 和 `security_requirements` 字段，支持五种安全方案：

| 方案 | 类型标识 | 说明 |
|------|---------|------|
| API Key | `apiKey` | 静态 API Key |
| HTTP Auth | `http` | Basic / Digest / Bearer |
| OAuth 2.1 | `oauth2` | OAuth 2.1 认证（本 Registry 主推方案） |
| OpenID Connect | `openIdConnect` | OpenID Connect 认证 |
| Mutual TLS | `mutualTls` | 双向 TLS 客户端证书 |

OAuth 2.1 方案支持下述 flow：

- `authorization_code` + PKCE — 面向用户 Agent
- `client_credentials` — 面向服务间通信
- `device_code` — 面向无用户交互设备

> 注意：OAuth 2.1 移除了 Implicit 和 Resource Owner Password Credentials Grant。protobuf 中保留为 `deprecated = true` 以便向后兼容解析。

---

## OAuth 2.1 认证架构

### 架构概览

```
┌─────────────────────────────────────────────────────────────┐
│                     A2A Registry                             │
│                                                             │
│  ┌──────────────────┐    ┌──────────────────────────────┐  │
│  │ AuthMiddleware    │    │ AuthHandler                  │  │
│  │ ───────────────── │    │ ─────────────────────       │  │
│  │ 拦截受保护端点      │    │ POST /auth/token            │  │
│  │ Bearer token 校验  │    │ POST /auth/register         │  │
│  │ Scope 鉴权        │    │ GET /.well-known/oauth-*    │  │
│  │ request['agent_id]│    │ GET /.well-known/jwks.json  │  │
│  └────────┬─────────┘    └─────────────┬────────────────┘  │
│           │                            │                    │
│           ▼                            ▼                    │
│  ┌──────────────────────────────────────────────────────┐  │
│  │                   AuthStore                           │  │
│  │  ┌──────────────┐  ┌──────────────┐  ┌────────────┐ │  │
│  │  │ clients 表    │  │ tokens 表    │  │ 密钥存储    │ │  │
│  │  │ (client_id)   │  │ (jti/exp)   │  │ (RS256)    │ │  │
│  │  └──────────────┘  └──────────────┘  └────────────┘ │  │
│  └──────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
```

### 认证中间件（AuthMiddleware）

```
Request → AuthMiddleware
  ├── /auth/* → 跳过认证（Token 端点公开）
  ├── /.well-known/* → 跳过认证（Discovery 端点公开）
  ├── /health → 跳过认证
  ├── Authorization: Bearer *** → 验证 JWT
  │     ├── 签名验证（RS256/HS256）
  │     ├── 过期时间校验
  │     ├── Scope 检查（装饰器级别）
  │     └── 注入 request['agent_id']
  ├── 无 Token → 401 Unauthorized + WWW-Authenticate
  └── Token 无效/过期 → 401 + error 描述
```

### Token 结构（JWT）

```json
{
  "iss": "simple-a2a-registry",
  "sub": "agent-1",
  "aud": ["simple-a2a-registry", "agent-2"],
  "exp": 1712345678,
  "iat": 1712342078,
  "scope": "task:read task:write",
  "jti": "unique-token-id"
}
```

### 密钥管理

| 阶段 | 算法 | 说明 |
|------|------|------|
| 开发/测试 | HS256 (HMAC) | 对称密钥，启动时自动生成 |
| 生产 | RS256 (RSA) | 非对称密钥对，私钥签名 / 公钥验证 |

RS256 模式下，JWKS 端点位于 `/.well-known/jwks.json`，供客户端验证 Token。

---

## 安全集成流程

### Agent 注册 → Token → 认证请求

```
1. Agent 启动
   │
   ├── 生成 AgentCard（含 security_schemes OAuth2 定义）
   │
   ▼
2. POST /v1/agents 注册
   │
   ├── Registry 解析 AgentCard.security_schemes
   ├── 自动创建 OAuth client
   └── 返回 agent_id + client_id + client_secret
   │
   ▼
3. POST /auth/token (client_credentials)
   │
   ├── Registry 签发 JWT access_token
   └── 返回 Bearer token（有效期 1 小时）
   │
   ▼
4. GET /.well-known/agent-card.json
   │
   ├── 获取其他 Agent 的 AgentCard
   └── 了解对方的 security_schemes（公开端点，无需认证）
   │
   ▼
5. 调用受保护 API
   │
   ├── Authorization: Bearer <token>
   ├── Registry 中间件验证 JWT + scope
   └── 返回请求数据
```

### Scopes 设计

| Scope | 描述 | 适用端点 |
|-------|------|---------|
| `task:read` | 读取任务列表和详情 | V1/V2 任务查询 |
| `task:write` | 创建和修改任务 | 任务操作端点 |
| `agent:read` | 读取 Agent 列表和详情 | Agent 查询 |
| `agent:register` | 注册新 Agent | POST /v1/agents |
| `agent:admin` | 管理 Agent（删除/禁用） | Agent 管理 |
| `registry:admin` | Registry 管理操作 | 管理端点 |

### 配置开关

```bash
# 开发模式（无认证）
a2a-registry
# 或显式关闭认证
a2a-registry --auth-enabled false

# 生产模式（OAuth 2.1 认证）
a2a-registry --auth-enabled true
```

`--auth-enabled` 控制整个认证中间件的开关。关闭时所有端点行为与旧版一致（无需 Token），开启后受保护端点需要 Bearer Token。

### 向下兼容

1. 认证关闭（`--auth-enabled false` 默认）时，所有端点行为不变
2. 认证开启后，受保护端点返回 `401` 而非旧版的 200/201
3. `/health`、`/.well-known/`、`/auth/` 端点始终公开