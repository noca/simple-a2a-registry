# A2A OpenCode / Claude Code Agent Adapter

## 概述

此适配器将 OpenCode CLI 和 Claude Code CLI 包装为 A2A (Agent-to-Agent) 兼容的 Agent，
使其能够注册到 A2A Registry，接收并处理分布式任务。

## 架构

```
┌─────────────────────────────────────────────────────────────┐
│                    A2A Registry Server                       │
│  (任务调度中心, WebSocket 推送, HTTP API)                    │
└──────────────┬────────────────────────────────────┬─────────┘
               │ WebSocket / HTTP                    │
               ▼                                     ▼
┌──────────────────────────┐       ┌──────────────────────────┐
│  a2a_opencode_agent.py   │       │  其他 A2A Agent           │
│                          │       │  (Hermes, 自定义)         │
│  ┌────────────────────┐  │       └──────────────────────────┘
│  │ HTTP Server (:9002) │  │
│  │ - Agent Card 发现   │  │
│  │ - /tasks/send      │  │
│  │ - /tasks/{id}      │  │
│  └────────┬───────────┘  │
│           │               │
│  ┌────────▼───────────┐  │
│  │ Registry 客户端     │  │
│  │ - 注册 / 心跳      │  │
│  │ - WebSocket 监听    │  │
│  └────────┬───────────┘  │
│           │               │
│  ┌────────▼───────────┐  │
│  │ CLI 后端执行器      │  │
│  │ - opencode run      │  │
│  │ - claude -p         │  │
│  └────────────────────┘  │
└──────────────────────────┘
```

## 快速开始

### 前置条件

1. **Python 3.10+** 和依赖:
   ```bash
   pip install aiohttp
   ```

2. **OpenCode CLI** (v1.x):
   ```bash
   npm install -g @opencode/cli    # 或参考 OpenCode 安装文档
   opencode --version              # 验证安装
   ```

3. **Claude Code CLI** (可选):
   ```bash
   npm install -g @anthropic-ai/claude-code
   claude --version                # 验证安装
   ```

4. **A2A Registry** (本项目中):
   ```bash
   cd /path/to/simple-a2a-registry-v2
   python -m simple_a2a_registry \
       --port 8321 \
       --disable-auth  # 开发环境免认证
   ```

### 运行 OpenCode Agent

```bash
cd /path/to/simple-a2a-registry-v2

# 默认模式 (OpenCode)
python examples/a2a_opencode_agent.py

# 指定 Registry 地址
python examples/a2a_opencode_agent.py \
    --registry http://localhost:8321 \
    --port 9002

# 设置工作目录（CLI 任务默认在此执行）
python examples/a2a_opencode_agent.py --cwd /path/to/project

# 详细日志
python examples/a2a_opencode_agent.py -v
```

### 运行 Claude Code Agent

```bash
python examples/a2a_opencode_agent.py --backend claude
```

### 带 OAuth 认证

```bash
# 方式 1: 环境变量
export OAUTH_CLIENT_ID="my-agent"
export OAUTH_CLIENT_SECRET="secret-xxx"
python examples/a2a_opencode_agent.py --auth

# 方式 2: CLI 参数
python examples/a2a_opencode_agent.py \
    --auth \
    --client-id my-agent \
    --client-secret secret-xxx

# 方式 3: 配置文件 (~/.a2a-cli-agent/auth.json)
echo '{"client_id": "my-agent", "client_secret": "secret-xxx"}' \
    > ~/.a2a-cli-agent/auth.json
python examples/a2a_opencode_agent.py --auth
```

## 完整命令行参数

```
usage: a2a_opencode_agent.py [-h] [--backend {opencode,claude}]
                              [--registry REGISTRY] [--host HOST]
                              [--port PORT] [--cwd CWD] [--timeout TIMEOUT]
                              [--auth] [--client-id CLIENT_ID]
                              [--client-secret CLIENT_SECRET]
                              [--auth-config AUTH_CONFIG]
                              [--agent-config AGENT_CONFIG]
                              [--agent-id AGENT_ID] [-v]

参数:
  --backend {opencode,claude}   CLI 后端: 'opencode'(默认) 或 'claude'
  --registry REGISTRY           Registry 地址 (默认: http://localhost:8321)
  --host HOST                   HTTP 服务监听地址 (默认: 0.0.0.0)
  --port PORT                   HTTP 服务端口 (默认: 9002)
  --cwd CWD                     CLI 任务工作目录
  --timeout TIMEOUT             单个 CLI 任务最大超时秒数 (默认: 600)
  --auth                        启用 OAuth 认证
  --client-id CLIENT_ID         OAuth 客户端 ID
  --client-secret CLIENT_SECRET OAuth 客户端密钥
  --auth-config AUTH_CONFIG     Auth 配置文件路径
  --agent-config AGENT_CONFIG   Agent ID 配置文件路径
  --agent-id AGENT_ID           强制指定 agent ID
  -v, --verbose                 启用 DEBUG 级别日志
```

## 测试集成

### 1. 启动 Registry

```bash
python -m simple_a2a_registry --port 8321 --disable-auth
```

### 2. 启动 OpenCode Agent

```bash
python examples/a2a_opencode_agent.py --registry http://localhost:8321 -v
```

### 3. 通过 Registry 调度任务

```bash
# 方式 A: 通过 Registry API 转发
curl -X POST http://localhost:8321/v1/agents/<agent-id>/dispatch \
  -H "Content-Type: application/json" \
  -d '{"query": "Write a Python function to calculate fibonacci numbers"}'
```

### 4. 直接调用 Agent HTTP 端点

```bash
# 获取 Agent Card
curl http://localhost:9002/.well-known/agent-card.json

# 直接提交任务
curl -X POST http://localhost:9002/tasks/send \
  -H "Content-Type: application/json" \
  -d '{"query": "Write a Python hello world"}'

# 查询任务状态
curl http://localhost:9002/tasks/<task-id>
```

### 5. 使用 SDK 客户端调度

```python
from simple_a2a_registry.client import A2AClient

# 连接 Registry
client = A2AClient(registry_url="http://localhost:8321")

# 查找 OpenCode 代理
agents = client.list_agents(q="OpenCode")
if agents["agents"]:
    agent_id = agents["agents"][0]["id"]
    # 调度任务
    result = client.dispatch_task(
        agent_id=agent_id,
        query="Refactor the main.py into smaller modules",
        session_id="refactor-session-001",
    )
    print(f"Task dispatched: {result['task_id']}")
```

## 支持的交互模式

| 模式 | 说明 | 适用场景 |
|------|------|----------|
| **WebSocket 推送** | Registry 通过 WS 推送任务，Agent 实时处理并回传结果 | 实时任务调度、自动化流程 |
| **HTTP 直连** | 第三方直接调用 Agent 的 `/tasks/send` 端点 | 独立部署、本地调试 |
| **HTTP 轮询** | 通过 `/tasks/{id}` 查询异步任务结果 | 长耗时任务的非阻塞访问 |

## OpenCode vs Claude Code 对比

| 特性 | OpenCode | Claude Code |
|------|----------|-------------|
| 协议 | ACP + JSON 输出 | 文本输出 (--print) |
| JSON 结构化输出 | ✅ (--format json) | ❌ (纯文本) |
| 流式输出 | ✅ | ✅ |
| 安装方式 | npm/brew | npm |
| 模型支持 | 多 provider | Anthropic Claude |
| 任务超时控制 | 内部机制 | --timeout 参数 |
| 镜像源 | 无限制 | 可能需要代理 |

## 高级配置

### 使用不同工作目录

Agent 支持三种层级的工作目录解析（优先级从高到低）：

1. **Task workspace**: WebSocket 推送的 `workspace_path` 字段
2. **CLI `--cwd`** : 命令行参数指定的默认工作目录
3. **当前目录**: 进程当前工作目录

### 持久化 Agent ID

Agent 启动后会将 registry 分配的 agent_id 保存到
`~/.a2a-cli-agent/agent.json`。下次启动时自动复用该 ID，
避免重复注册。

## 故障排查

### 常见问题

**Q: Agent 启动时报 "Backend 'opencode' requires 'opencode' which is not installed"**

A: 请先安装 OpenCode: `npm install -g @opencode/cli`

**Q: Registry 连接被拒绝 (Connection refused)**

A: 确认 Registry 正在运行:
```bash
curl http://localhost:8321/health
```

**Q: 任务提交后状态一直是 "submitted"**

A: 确认 Agent 已通过 WebSocket 连接到 Registry。检查 Agent 日志中
是否看到 "WebSocket connected" 消息。

**Q: Claude Code 执行时显示 API key 错误**

A: 确保已设置 `ANTHROPIC_API_KEY` 环境变量:
```bash
export ANTHROPIC_API_KEY="sk-ant-xxx"
```

## 与 a2a_coder_agent.py 对比

| 特性 | a2a_coder_agent.py (Hermes) | a2a_opencode_agent.py |
|------|---------------------------|----------------------|
| 底层引擎 | Hermes Agent (CLI) | OpenCode / Claude Code |
| 适用场景 | 全栈开发、调试、PR 管理 | 代码生成、重构、Review |
| 模型 | 多模型 (Hermes 配置) | 多模型 (OpenCode 配置) |
| 依赖 | hermes-agent 环境 | opencode 或 claude CLI |
| 启动速度 | 秒级 | 秒级 |
| 结果结构 | Hermes 输出清洗 | JSON 解析 (opencode) |
| 端口 | 9001 | 9002 |