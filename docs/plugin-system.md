# Simple A2A Registry — 插件系统设计

> 面向可扩展性的插件架构，允许第三方模块在 Registry 核心定义的生命周期点注入自定义行为。
>
> 对应 Kanban: P3-A
>
> **实施状态：**
> - ✅ 插件接口 (plugin.py) — 已实现 (ABC + PluginRegistry)
> - ✅ 配置声明 (config.py PluginConfig) — 已实现
> - ⬜ 入口点集成 (pyproject.toml) — 待实现
> - ⬜ 中间件集成 (server.py create_app) — 待实现
> - ⬜ 示例插件实现 — 待实现

---

## 目录

1. [设计目标](#1-设计目标)
2. [架构概览](#2-架构概览)
3. [插件生命周期](#3-插件生命周期)
4. [钩子点清单](#4-钩子点清单)
5. [插件接口规范](#5-插件接口规范)
6. [插件加载方式](#6-插件加载方式)
7. [插件隔离原则](#7-插件隔离原则)
8. [插件配置](#8-插件配置)
9. [插件注册表](#9-插件注册表)
10. [与现有架构的集成](#10-与现有架构的集成)
11. [示例插件](#11-示例插件)
12. [约束与风险](#12-约束与风险)

---

## 1. 设计目标

| 目标 | 说明 |
|------|------|
| **非侵入** | 插件的添加/移除不应修改核心代码 |
| **关注点分离** | 每个插件独立处理自己的领域逻辑 |
| **失败隔离** | 一个插件的崩溃不应影响其他插件或核心 |
| **配置驱动** | 通过 config.yaml 控制插件的启用和参数 |
| **渐进复杂** | 简单插件只需实现一两个钩子；复杂插件可深度集成 |
| **发现友好** | 通过 setuptools entry_points 自动发现安装的插件 |

### 1.1 非功能性要求

| 维度 | 目标 | 验证方式 |
|------|------|---------|
| 性能 | 空钩子路径 < 0.1ms（无插件时零开销） | pytest-benchmark |
| 内存 | 每个插件实例 ~2KB 基础开销 | tracemalloc |
| 健壮性 | 单插件异常不会传播到其他插件或核心 | 异常被 `try/except` 吞没并记录 |
| 可观测 | 插件加载/失败/钩子异常均有结构化日志 | 日志级别 INFO/ERROR |

---

## 2. 架构概览

```
┌─────────────────────────────────────────────────────┐
│                   aiohttp Application                │
│  ┌─────────────────────────────────────────┐        │
│  │            Middleware Stack              │        │
│  │  CORS → request_id → error → auth →    │        │
│  │  metrics → **plugin_middleware**        │        │
│  └─────────────────────────────────────────┘        │
│                                                      │
│  ┌─────────────────────────────────────────┐        │
│  │           PluginRegistry                 │        │
│  │  ┌──────┐ ┌──────┐ ┌──────┐ ┌──────┐  │        │
│  │  │ P1   │ │ P2   │ │ P3   │ │ P4   │  │        │
│  │  └──────┘ └──────┘ └──────┘ └──────┘  │        │
│  └─────────────────────────────────────────┘        │
│                                                      │
│  ┌─────────────────────────────────────────┐        │
│  │         Core Handlers                    │        │
│  │  RegistryHandler  AuthHandler  Admin     │        │
│  └─────────────────────────────────────────┘        │
└─────────────────────────────────────────────────────┘
```

### 2.1 核心概念

- **Plugin** — 实现 `Plugin` 抽象基类的 Python 类。定义一组可选的生命周期/请求/事件钩子。
- **PluginRegistry** — 加载、管理和分发钩子调用的中心注册表。负责迭代所有已加载插件并有序调用各钩子。
- **PluginInfo** — 插件的运行时元数据（名称、版本、描述、加载时间、配置）。
- **Hook** — Plugin 基类中定义的命名方法。按注册顺序依次执行。

---

## 3. 插件生命周期

插件经历以下状态序列：

```
 [配置加载]
     │
     ▼
   LOAD  ←── load(config) ── 解析配置、初始化内部状态
     │
     ▼
   INIT  ←── init(app)   ── 注入路由/中间件/后台任务
     │
     ▼
 ┌──┬──────────────────────────────────────────┐
 │  │          运行态（请求/事件驱动）        │
 │  │  ┌─ before_request ─► handler ─► after  │
 │  │  └─ on_agent_register / on_heartbeat ...│
 │  └──────────────────────────────────────────┘
     │
     ▼
 SHUTDOWN ←── before_shutdown(app) ── 清理/刷缓冲
```

### 3.1 各阶段说明

| 阶段 | 调用时机 | 典型用途 | 失败行为 |
|------|----------|----------|---------|
| `load(config)` | 配置解析后、服务启动前 | 验证参数、建立外部连接 | 插件标记为失败，不注册 |
| `init(app)` | 路由注册后、监听前 | 加路由/中间件/启动后台任务 | 日志警告，插件继续可用 |
| `before_request(request)` | 每次 HTTP 请求前 | 限流、鉴权增强、请求变换 | 异常记录，请求继续 |
| `after_request(request, response)` | 每次 HTTP 请求后 | 响应注入、计时、审计日志 | 异常记录，原始响应返回 |
| `before_shutdown(app)` | 优雅关闭阶段 | 刷新缓冲、关闭连接 | 超时 5s 后跳过 |

---

## 4. 钩子点清单

### 4.1 生命周期钩子

| 钩子 | 签名 | 触发点 |
|------|------|--------|
| `load` | `(config: Dict) -> None` | 插件首次加载时 |
| `init` | `(app: Application) -> None` | aiohttp 应用完全配置后 |
| `before_shutdown` | `(app: Application) -> None` | 服务器优雅关闭阶段 |

### 4.2 请求钩子

| 钩子 | 签名 | 语义 |
|------|------|------|
| `before_request` | `(request: Request) -> Optional[Response]` | 返回 `None` 继续处理；返回 `Response` 短路请求 |
| `after_request` | `(request: Request, response: StreamResponse) -> StreamResponse` | 可修改响应内容/头 |

### 4.3 事件钩子

| 钩子 | 签名 | 触发时机 |
|------|------|---------|
| `on_server_start` | `(app: Application) -> None` | 服务器开始接受连接后 |
| `on_server_stop` | `(app: Application) -> None` | 服务器开始关闭时 |
| `on_agent_register` | `(agent_id: str, card: Dict) -> None` | 代理注册成功 |
| `on_agent_deregister` | `(agent_id: str) -> None` | 代理注销 |
| `on_agent_heartbeat` | `(agent_id: str) -> None` | 代理心跳处理完成 |
| `on_task_created` | `(task_id: str, task_data: Dict) -> None` | 内存中创建新任务 |
| `on_task_completed` | `(task_id: str, result: Optional[Dict]) -> None` | 任务完成或失败 |
| `on_token_issued` | `(client_id: str, token: str) -> None` | OAuth 令牌签发 |

---

## 5. 插件接口规范

### 5.1 Plugin 抽象基类

所有插件必须实现 `name` 属性，可选实现任何钩子方法。未实现的钩子自动使用基类的空操作。

```python
from abc import ABC, abstractmethod
from aiohttp import web
from typing import Any, Dict, Optional

class Plugin(ABC):
    @property
    @abstractmethod
    def name(self) -> str: ...

    @property
    def version(self) -> str: return "0.1.0"

    @property
    def description(self) -> str: return ""

    # Lifecycle
    def load(self, config: Dict[str, Any]) -> None: ...
    async def init(self, app: web.Application) -> None: ...
    async def before_shutdown(self, app: web.Application) -> None: ...

    # Request hooks
    async def before_request(self, request: web.Request) -> Optional[web.Response]: ...
    async def after_request(self, request: web.Request, response: web.StreamResponse) -> web.StreamResponse: ...

    # Event hooks
    async def on_server_start(self, app: web.Application) -> None: ...
    async def on_server_stop(self, app: web.Application) -> None: ...
    async def on_agent_register(self, agent_id: str, card: Dict[str, Any]) -> None: ...
    async def on_agent_deregister(self, agent_id: str) -> None: ...
    async def on_agent_heartbeat(self, agent_id: str) -> None: ...
    async def on_task_created(self, task_id: str, task_data: Dict[str, Any]) -> None: ...
    async def on_task_completed(self, task_id: str, result: Optional[Dict[str, Any]]) -> None: ...
    async def on_token_issued(self, client_id: str, token: str) -> None: ...
```

### 5.2 设计决策

| 决策 | 选项 | 选择 | 理由 |
|------|------|------|------|
| 基类方式 | ABC vs Protocol | **ABC** | 更明确的继承关系；便于 IDE 补全；支持默认实现 |
| 钩子行为 | 抽象 vs 可选 | **可选（空默认实现）** | 插件只需重写自己需要的钩子 |
| 签名 | 同步 vs 异步 | **异步（async def）** | 与 aiohttp 生态一致；支持数据库/网络操作 |
| 异常处理 | 传播 vs 吞没 | **吞没并记录** | 单个插件的错误不应影响其他插件或核心 |

### 5.3 与 Python Protocol 的兼容性

虽然基类选择了 ABC，但 `PluginProtocol` 可用于类型标注场景（如接受插件的函数参数）：

```python
from typing import Protocol, runtime_checkable

@runtime_checkable
class PluginProtocol(Protocol):
    @property
    def name(self) -> str: ...
```

---

## 6. 插件加载方式

插件支持**两种互补**的加载机制，按以下优先级执行：

### 6.1 入口点发现（entry_points）

通过 `pyproject.toml` 的 `[project.entry-points."simple_a2a_registry.plugins"]` 声明：

```toml
[project.entry-points."simple_a2a_registry.plugins"]
my-plugin = "my_package.plugins:MyPlugin"
```

运行时通过 `importlib.metadata.entry_points(group="simple_a2a_registry.plugins")` 自动发现。

**优点：**
- 零配置激活 —— 安装包即自动加载
- 适合独立分发的第三方插件

### 6.2 配置文件声明（config.yaml）

通过 `config.yaml` 的 `plugins.enabled` 节明确加载：

```yaml
plugins:
  enabled:
    my-plugin:
      module: my_package.plugins     # Python module path
      config:
        key: value

    file-plugin:
      path: /opt/plugins/custom.py   # 或文件系统路径
      config: {}
```

**优点：**
- 明确的启用/禁用控制
- 支持文件系统路径加载（无需安装）
- 可传递 per-plugin 配置参数

### 6.3 加载顺序

1. importlib entry_points 发现并加载
2. config.yaml 解析并加载（可覆盖 entry_points 的同名插件**
3. 同一名称第二次注册时抛出 `ValueError`

> **注意：** config.yaml 中声明同名的插件会**失败**（注册冲突）。设计上禁止同名覆盖以防止混淆。

### 6.4 失败处理

| 失败类型 | 处理方式 | 日志级别 |
|----------|----------|---------|
| `load()` 抛异常 | 插件不注册，记录到 `failures` 字典 | ERROR |
| `init()` 抛异常 | 日志记录，插件继续可用 | ERROR |
| 钩子函数抛异常 | 日志记录，当前钩子跳过 | ERROR (with stack trace) |
| before_shutdown 超时 | 超时 5s 后跳过，继续执行其他插件 | WARNING |

---

## 7. 插件隔离原则

### 7.1 命名空间隔离

每个插件实例拥有独立的属性空间 —— 不需要担心实例变量冲突：

```python
class MyPlugin(Plugin):
    def load(self, config):
        self._client = create_client(config.get("endpoint"))
        self._rate = config.get("rpm", 60)

    # _client 和 _rate 仅属于此实例
```

### 7.2 依赖注入

插件不应直接 import 核心模块的私有实现。可通过 `app[...]` 字典获取核心服务：

```python
async def init(self, app):
    store = app["store"]          # 注册表 Store
    task_store = app["task_store"] # Kanban TaskStore
    config = app["config"]         # 全局配置
```

### 7.3 隔离规则

| 维度 | 策略 |
|------|------|
| 状态 | 每个插件独立实例，无共享可变状态 |
| 异常 | `PluginRegistry` 在调用点捕获所有异常 |
| 超时 | `before_shutdown` 有 5s 超时保护 |
| 资源 | 建议插件在 `load()` 中预分配资源，`before_shutdown()` 中释放 |
| 文件 | 插件应使用自己的临时目录或 `app["store"]` 的数据路径 |

### 7.4 禁止的行为

- 插件不得修改 `plugin.py`、`config.py` 等核心模块
- 插件不得直接替换 aiohttp 应用的路由表（应通过 `app.router.add_*()` 添加）
- 插件不得捕获和抑制 `asyncio.CancelledError`

---

## 8. 插件配置

### 8.1 config.yaml 结构

```yaml
plugins:
  enabled:
    # 简单启用 — 使用默认配置
    logging-plugin: true

    # 带自定义配置
    rate-limit-plugin:
      module: simple_a2a_registry_examples.plugins.rate_limiter
      config:
        rpm: 100
        burst: 20
        storage: memory

    # 文件路径加载
    custom-audit:
      path: /opt/a2a-plugins/audit_logger.py
      config:
        output: /var/log/a2a/audit.log
```

### 8.2 PluginConfig 数据类

```python
@dataclass
class PluginConfig:
    enabled: Dict[str, Any] = field(default_factory=dict)
```

`enabled` 字典的每个键是插件名称，值可以是：
- `True` — 简单启用，使用 entry_points 发现
- `{"module": "...", "config": {...}}` — 指定 module 加载
- `{"path": "...", "config": {...}}` — 指定文件路径加载

### 8.3 环境变量覆盖

遵循现有配置模式，可通过 `A2A_REGISTRY_PLUGINS__ENABLED__<NAME>__CONFIG__KEY` 覆盖：

```bash
export A2A_REGISTRY_PLUGINS__ENABLED__RATE_LIMIT__CONFIG__RPM=200
```

---

## 9. 插件注册表

`PluginRegistry` 是插件的管理中心，定义在 `simple_a2a_registry/plugin.py`。

### 9.1 核心 API

| 方法 | 说明 |
|------|------|
| `register(plugin, config)` | 注册插件实例并调用 `load()` |
| `discover_entry_points(group)` | 从 setuptools entry_points 发现并加载 |
| `load_config_section(plugins_config)` | 从 config.yaml 节加载插件 |
| `fire_init(app)` | 迭代调用所有插件的 `init()` |
| `fire_before_shutdown(app)` | 迭代调用所有插件的 `before_shutdown()` |
| `fire_before_request(request)` | 返回第一个非 None 的响应（短路） |
| `fire_after_request(request, response)` | 链式调用返回最终响应 |
| `fire_agent_register(agent_id, card)` | 事件分发（fire-and-forget） |
| `summary()` | 返回人类可读的加载摘要 |

### 9.2 属性

| 属性 | 类型 | 说明 |
|------|------|------|
| `plugins` | `Dict[str, Plugin]` | 成功加载的插件 |
| `infos` | `Dict[str, PluginInfo]` | 插件运行时元数据 |
| `failures` | `Dict[str, str]` | 加载失败记录 |
| `is_loaded(name)` | `bool` | 快速检查 |
| `get_plugin(name)` | `Optional[Plugin]` | 按名称获取 |

### 9.3 PluginInfo 结构

```python
@dataclass
class PluginInfo:
    name: str
    version: str = "0.1.0"
    description: str = ""
    module: str = ""
    loaded_at: float = 0.0
    config: Dict[str, Any] = field(default_factory=dict)
```

---

## 10. 与现有架构的集成

### 10.1 启动流程集成

`create_app()` 中插件的加载序列：

```
create_app()
  │
  ├─ 1. 创建 Store、Handler、AuthHandler、Dispatcher 等核心组件
  ├─ 2. 注册路由（静态 + v1 + v2 + auth + admin）
  ├─ 3. 创建 PluginRegistry 实例
  ├─ 4. registry.discover_entry_points()   ← 入口点发现
  ├─ 5. registry.load_config_section()     ← 配置声明加载
  ├─ 6. 插入 plugin_middleware 到中间件栈
  ├─ 7. 在 on_startup 中调用 registry.fire_init(app)
  ├─ 8. 在 on_startup 中调用 registry.fire_server_start(app)
  └─ 9. 在 on_cleanup 中调用 registry.fire_before_shutdown(app)
```

### 10.2 中间件集成

```python
async def plugin_middleware_factory(registry: PluginRegistry):
    @web.middleware
    async def _middleware(request, handler):
        # Before — 允许插件短路请求
        resp = await registry.fire_before_request(request)
        if resp is not None:
            return resp

        # 正常处理
        try:
            response = await handler(request)
        except web.HTTPException as exc:
            response = exc

        # After — 允许插件修改响应
        return await registry.fire_after_request(request, response)

    return _middleware
```

### 10.3 事件钩子的调用点

核心 handler 的改动量极小 —— 在关键事件后增加一行调用即可。

**示例：注册事件**（`server.py` `handle_register` 末尾）：

```python
# 原有代码
response = {"message": "Agent registered successfully", "id": agent_id, "card": card}
result = web.json_response(response, status=201)

# 新增加的事件分发
registry = app.get("plugin_registry")
if registry:
    await registry.fire_agent_register(agent_id, card)

return result
```

### 10.4 集成 Checklist

| 改动点 | 文件 | 改动量 | 风险 |
|--------|------|--------|------|
| 添加 `PluginConfig` | config.py | +15 行 | 低 — 纯数据类 |
| 添加 `plugin.py` | 新文件 | ~500 行 | 低 — 独立模块 |
| 创建 Registry + 发现 | server.py create_app | +10 行 | 低 |
| 插入中间件 | server.py create_app | +3 行 | 低 — 中间件模式已建立 |
| 事件钩子调用点 | server.py handlers | ~8 处各+3行 | 低 — 简单追加 |

---

## 11. 示例插件

### 11.1 logging-plugin — 增强请求日志

```python
class EnhancedLoggingPlugin(Plugin):
    name = "enhanced-logging"
    version = "1.0.0"
    description = "Log every request with payload size and latency"

    async def after_request(self, request, response):
        duration = time.monotonic() - getattr(request, "_start", time.monotonic())
        size = len(response.body) if hasattr(response, "body") and response.body else 0
        logger.info(
            "[plugin] %s %s → %d (%d bytes, %.2fms)",
            request.method, request.path, response.status, size, duration * 1000,
        )
        return response
```

### 11.2 rate-limit-plugin — 限流

```python
class RateLimitPlugin(Plugin):
    name = "rate-limit"
    version = "1.0.0"
    description = "Token-bucket rate limiting per client ID"

    def load(self, config):
        self.rpm = config.get("rpm", 60)
        self.buckets: Dict[str, float] = {}
        self._lock = asyncio.Lock()

    async def before_request(self, request):
        client_id = request.headers.get("X-Client-ID", request.remote or "anonymous")
        async with self._lock:
            now = time.monotonic()
            last = self.buckets.get(client_id, 0)
            if now - last < 60.0 / self.rpm:
                return web.json_response(
                    {"error": "rate_limited"},
                    status=429,
                    headers={"Retry-After": str(int(60 / self.rpm))},
                )
            self.buckets[client_id] = now
        return None
```

### 11.3 metrics-plugin — 自定义指标

```python
class CustomMetricsPlugin(Plugin):
    name = "custom-metrics"
    version = "1.0.0"
    description = "Expose additional Prometheus metrics"

    async def init(self, app):
        from prometheus_client import Gauge, generate_latest, CONTENT_TYPE_LATEST

        self._agent_registration_gauge = Gauge(
            "a2a_registry_agent_registrations_total", "Total agent registrations",
        )

        # 注册额外 /metrics 端点（但与现有不冲突）
        app.router.add_get("/metrics/custom", self._handle_custom_metrics)

    async def on_agent_register(self, agent_id, card):
        self._agent_registration_gauge.inc()

    async def _handle_custom_metrics(self, request):
        return web.Response(
            body=generate_latest(),
            headers={"Content-Type": CONTENT_TYPE_LATEST},
        )
```

### 11.4 插件结构（独立包发布）

```
my-a2a-plugin/
├── pyproject.toml           # entry-points 声明
├── src/
│   └── my_plugin/
│       ├── __init__.py
│       └── plugin.py        # Plugin 子类
└── tests/
    └── test_plugin.py
```

`pyproject.toml` 关键部分：

```toml
[project.entry-points."simple_a2a_registry.plugins"]
my-plugin = "my_plugin.plugin:MyPlugin"
```

---

## 12. 约束与风险

### 12.1 已知风险

| 风险 | 影响 | 缓解措施 |
|------|------|---------|
| 插件 `before_request` 返回短路径响应 | 绕过正常鉴权和业务逻辑 | 文档明确警告；日志记录短路径 |
| 插件 `init` 注册重叠路由 | 路由冲突 500 | aiohttp 的 `add_route` 在冲突时抛异常；捕获记录 |
| 插件 `before_shutdown` 阻塞 | 服务器关闭延迟 | 5s 超时保护 |
| 插件泄漏内存 | 生产稳定性 | 建议插件在 `load()` 预分配，`before_shutdown()` 释放 |
| entry-point 名称冲突 | 同包名不同插件的冲突 | `register()` 直接抛 `ValueError`，名称必须唯一 |
| 插件吞没 CancelledError | asyncio 关闭卡死 | 文档明确禁止；review 检查点 |

### 12.2 安全约束

- 插件运行在**与核心相同的进程**中 —— 安装第三方插件前需审查代码
- 文件路径加载（`path:`）应限制在白名单目录
- 插件 `before_request` 的短路响应不会触发核心的鉴权中间件（因为插件中间件在 auth 之后）
- `load()` 和 `init()` 阶段的异常**不会**阻止服务器启动（仅记录日志）

### 12.3 性能预算

| 场景 | 延迟预算 |
|------|---------|
| 无插件时中间件跳过 | 零开销（空 `FireBeforeRequest` 只遍历空字典） |
| 1 个插件的 `after_request` | < 0.5ms |
| 5 个插件全部钩子空转 | < 1ms |
| 5 个插件的 `before_shutdown` 全部空转 | < 50ms |

### 12.4 未来扩展方向

| 方向 | 说明 | 优先级 |
|------|------|--------|
| 插件排序 | 支持 `priority` 字段控制执行顺序 | ⭐⭐ |
| 热加载 | 运行时添加/移除插件（需 `app.reload` 机制） | ⭐⭐⭐ |
| 沙箱执行 | `subprocess`/`container` 隔离非可信插件 | ⭐⭐⭐ |
| 声明式健康检查 | 插件可声明自己的健康端点，自动聚合到 `/health` | ⭐ |
| 插件市场 | 中央索引 + 一键安装 | 远期 |
| 异步 `load()` | 支持 `await plugin.load()` 用于异步初始化连接 | ⭐⭐ |

---

## 附录 A：文件清单

| 文件 | 路径 | 状态 |
|------|------|------|
| 插件接口 + 注册表 | `simple_a2a_registry/plugin.py` | ✅ 已实现 |
| 插件配置声明 | `simple_a2a_registry/config.py` (PluginConfig) | ✅ 已实现 (追加) |
| 插件系统设计文档 | `docs/plugin-system.md` | ✅ 当前文档 |
| 入口点声明 | `pyproject.toml` | ⬜ 待集成 |

## 附录 B：与 Hermes Kanban 插件系统的相似性

本插件系统与 Hermes Agent 的技能系统（skills）在设计上有诸多共通之处：

| 维度 | Hermes Skills | A2A Registry Plugins |
|------|---------------|----------------------|
| 加载方式 | `config.yaml` 声明 | entry_points + config.yaml |
| 生命周期 | load → （执行） → cleanup | load → init → run → before_shutdown |
| 钩子机制 | 独立 skill 文件 | Plugin ABC 方法 |
| 隔离 | 每个 skill 独立文件 | 每个 plugin 独立实例 |

这种设计相似性不是偶然的 —— 两者都遵循"在核心架构上通过明确定义的扩展点注入行为"这一模式。A2A Registry 的插件系统是这一模式在 HTTP Agent 注册表领域的特化实现。