# Simple A2A Registry — 企业级架构设计

> 面向生产部署的架构扩展方案：MySQL迁移、配置系统、结构化日志、全局错误模型、Prometheus监控、CORS
>
> 对应 GitHub Issues: P1-A
>
> **实施状态：**
> - ✅ **配置系统 (config.py)** — 已实现 (YAML dataclass + A2A_REGISTRY_* 环境变量 + CLI 三优先级)
> - ✅ **结构化日志 (log.py)** — 已实现 (JSON/Text 双模式 + request_id contextvars 中间件 + key event logging)
> - ✅ **CORS 中间件 (server.py)** — 已实现 (`_cors_middleware_factory` + `cors_origins` 参数)
> - ✅ **全局错误模型 (errors.py)** — 已实现 (APIError dataclass + 异常类体系 + error_middleware + 超时控制)
> - ✅ **优雅关闭** — 已实现 (SIGTERM/SIGINT → 6 阶段清理: WS关闭→TaskStore→DB→Dispatcher→cleanup→flsuh logs)
> - ✅ **DB 断线重试** — 已实现 (Store._retry_operation: 指数退避, 3 次重试)
> - ✅ **请求级超时控制** — 已实现 (timeout_middleware, 默认 30s)
> - ✅ **启动前置检查** — 已实现 (DB连接/端口可用/RSA密钥生成)
> - ⬜ **Prometheus 指标 (metrics.py)** — 待实现
> - ⬜ **MySQL 迁移 (db/engine.py + alembic)** — 待实现
> - ⬜ **配置验证、动态日志级别 API** — 待实现
>
> 本文档作为完整的设计参考，包含所有方案的对比与决策记录。
> 灰色标记的章节表示已通过代码实现，实施团队可直接跳转到待实现章节。

---

## 目录

1. [MySQL 迁移方案](#1-mysql-迁移方案)
2. [配置系统设计](#2-配置系统设计)
3. [结构化日志](#3-结构化日志)
4. [全局错误模型](#4-全局错误模型)
5. [Prometheus 指标命名](#5-prometheus-指标命名)
6. [CORS 设计](#6-cors-设计)
7. [实施路线图](#7-实施路线图)

---

## 1. MySQL 迁移方案

### 1.1 技术选型

| 组件 | 选型 | 理由 |
|------|------|------|
| ORM | **SQLAlchemy 2.0+** (async模式) | 生态成熟，支持异步/同步双引擎，Alembic迁移原生集成 |
| 异步驱动 | **aiomysql** | 纯异步Python MySQL驱动，与aiohttp生态天然匹配 |
| 迁移工具 | **Alembic** | SQLAlchemy官方迁移工具，自动生成迁移脚本 |
| 连接池 | SQLAlchemy内置池 (QueuePool) | 自带最小/最大连接数、超时、回收策略 |

**备选方案对比：**

| 方案 | ORM | 异步支持 | 迁移 | 复杂度 | 推荐 |
|------|-----|---------|------|--------|------|
| SQLAlchemy + aiomysql | ✅ 完整ORM | ✅ async/await | ✅ Alembic | 中 | **首选** |
| asyncmy (纯Python) | 需配合SQLAlchemy | ✅ | ✅ Alembic | 低 | 备选，社区较小 |
| MySQL Connector/Python | 原生SQL | ❌ 同步 | 手动 | 低 | 不推荐 |
| Tortoise-ORM | ✅ ORM | ✅ | aerich | 低 | 不兼容现有Store模式 |

> **决策：** SQLAlchemy 2.0 async + aiomysql。理由：SQLAlchemy的Engine抽象层允许运行时切换SQLite↔MySQL，无需修改业务代码；Alembic是业界标准迁移方案。

### 1.2 数据库连接池配置

```python
# config.yaml — database 区
database:
  # --- 生产配置 (MySQL) ---
  driver: mysql+aiomysql      # 生产使用
  host: 127.0.0.1
  port: 3306
  user: a2a_registry
  password: "${DB_PASSWORD}"   # 环境变量注入
  database: a2a_registry
  charset: utf8mb4

  pool:
    min_size: 5                # 最小连接数（连接池预热）
    max_size: 20               # 最大连接数
    overflow: 10               # 超出 max_size 的额外应急连接
    pool_timeout: 30           # 等待连接超时(秒)
    pool_recycle: 3600         # 连接回收周期(秒)，防MySQL wait_timeout断开
    echo: false                # SQL日志（仅调试用）

  # --- 开发配置 (SQLite) ---
  # driver: sqlite+aiosqlite
  # path: ~/.simple-a2a-registry/registry.db
```

**连接池参数设计说明：**

| 参数 | 建议值 | 说明 |
|------|--------|------|
| `pool_size` | 5~20 | 按并发WS连接数预估：每个Agent一条WS，20连接≈20 Agent并发 |
| `max_overflow` | 10 | 突发流量时临时额外分配，不增加常驻连接 |
| `pool_timeout` | 30s | 等待连接的超时，超过则抛出 `TimeoutError` |
| `pool_recycle` | 3600s | 避免MySQL默认 `wait_timeout=28800` 导致的断连 |
| `pool_pre_ping` | true | 每次借用连接前发送 `SELECT 1` 验证连接活性 |

### 1.3 表结构设计

所有表保持与当前 SQLite 方案相同的逻辑结构，仅作 MySQL 适配。

#### 1.3.1 Registry 表 (原SQLite → MySQL)

```sql
-- 替代 store.py 中的 agents 表
CREATE TABLE agents (
    id              VARCHAR(64) PRIMARY KEY,
    card_json       JSON NOT NULL,                          -- MySQL 原生 JSON 类型
    heartbeat_at    DOUBLE NOT NULL DEFAULT 0,
    registered_at   VARCHAR(32) NOT NULL,
    created_at      DOUBLE NOT NULL,
    INDEX idx_agents_heartbeat (heartbeat_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
```

| 变更 | SQLite 原类型 | MySQL 目标类型 | 原因 |
|------|-------------|---------------|------|
| `card_json` | TEXT | JSON | MySQL JSON列支持部分更新(`JSON_SET`)、索引虚拟列 |
| 所有 TEXT | TEXT | VARCHAR(x) | 定长字段使用 VARCHAR，提高查询效率 |
| 引擎 | — | InnoDB | 支持事务、行级锁、外键 |
| 字符集 | — | utf8mb4 | 支持emoji和扩展BMP字符 |

#### 1.3.2 OAuth 表

```sql
-- 替代 store.py 中的 oauth_clients 表
CREATE TABLE oauth_clients (
    client_id           VARCHAR(64) PRIMARY KEY,
    client_secret_hash  VARCHAR(64) NOT NULL,
    allowed_scopes      VARCHAR(512) NOT NULL,
    agent_card_id       VARCHAR(64) NOT NULL DEFAULT '',
    created_at          DOUBLE NOT NULL,
    description         VARCHAR(256) NOT NULL DEFAULT ''
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 替代 store.py 中的 oauth_tokens 表
CREATE TABLE oauth_tokens (
    jti         VARCHAR(64) PRIMARY KEY,
    client_id   VARCHAR(64) NOT NULL,
    scope       VARCHAR(256) NOT NULL,
    expires_at  DOUBLE NOT NULL,
    INDEX idx_oauth_tokens_client_id (client_id),
    INDEX idx_oauth_tokens_expires (expires_at),
    FOREIGN KEY (client_id) REFERENCES oauth_clients(client_id)
        ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 替代 store.py 中的 auth_codes 表
CREATE TABLE auth_codes (
    code                    VARCHAR(64) PRIMARY KEY,
    client_id               VARCHAR(64) NOT NULL,
    code_challenge          VARCHAR(128) NOT NULL,
    code_challenge_method   VARCHAR(16) NOT NULL,
    redirect_uri            VARCHAR(512) NOT NULL,
    scope                   VARCHAR(256) NOT NULL,
    created_at              DOUBLE NOT NULL,
    INDEX idx_auth_codes_created (created_at),
    FOREIGN KEY (client_id) REFERENCES oauth_clients(client_id)
        ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
```

#### 1.3.3 Orchestration 表 (board.db)

当前 `TaskStore` 管理的 5 张表 (`tasks`, `task_links`, `task_runs`, `task_comments`, `task_events`) 也需迁移到 MySQL。关键变更：

| 表 | SQLite 特殊类型 | MySQL 代替 | 说明 |
|----|---------------|-----------|------|
| `tasks` | `task_body TEXT` | `task_body JSON` | MySQL JSON 支持索引 |
| `task_links` | 无显式FK | `FOREIGN KEY ... ON DELETE CASCADE` | 外键约束（InnoDB支持） |
| `task_events` | `event_payload TEXT` | `event_payload JSON` | 事件负载使用JSON列 |

### 1.4 兼容性层：SQLite ↔ MySQL 双引擎

核心设计原则：**开发环境用 SQLite，生产环境用 MySQL，代码层零改动**。

```python
# 抽象存储引擎 — 统一接口
class DatabaseEngine:
    """数据库引擎抽象层，运行时切换 SQLite / MySQL。"""

    @staticmethod
    def create(config: dict) -> "DatabaseEngine":
        driver = config.get("driver", "sqlite+aiosqlite")
        if "sqlite" in driver:
            return SQLiteEngine(config)
        elif "mysql" in driver:
            return MySQLEngine(config)
        raise ValueError(f"Unsupported driver: {driver}")

    async def connect(self): ...
    async def execute(self, sql: str, params: tuple = ()): ...
    async def fetchone(self, sql: str, params: tuple = ()): ...
    async def fetchall(self, sql: str, params: tuple = ()): ...
    async def close(self): ...
```

**实现策略对比：**

| 方案 | 实现方式 | 优点 | 缺点 |
|------|---------|------|------|
| **方案A：抽象引擎层** ★推荐 | 封装 DatabaseEngine 统一接口 | 对现有 Store 改动最小，清晰 | 额外抽象层 |
| 方案B：SQLAlchemy ORM | 直接用 ORM 重写 Store | ORM 自带多方言支持 | 改动大，现有 Store 需全部重写 |
| 方案C：运行时 SQL 改写 | 自动替换 SQLite 特有语法 | 零改动 | 脆弱，容易遗漏 |

> **决策：方案A**。选择抽象引擎层而非直接上 ORM，原因：当前 Store 使用原生 SQLite API（`sqlite3` + `threading.RLock`），业务逻辑与 SQL 耦合。抽象引擎层可逐步迁移，先实现 MySQL 兼容接口，再按需引入 SQLAlchemy ORM。

```python
# 伪代码 — Store 如何切换
class Store:
    def __init__(self, config: dict):
        self._engine = DatabaseEngine.create(config)

    async def get_agent(self, agent_id: str):
        row = await self._engine.fetchone(
            "SELECT card_json, heartbeat_at FROM agents WHERE id=?",
            (agent_id,),
        )
        # ... 后续逻辑不变
```

### 1.5 Alembic Schema Migration 策略

```
项目结构：
simple_a2a_registry/
  alembic/
    env.py          — Alembic 环境配置（支持 SQLite + MySQL 双目标）
    versions/       — 迁移脚本目录
      0001_initial_schema.py
      0002_add_agent_tags.py
  alembic.ini       — Alembic 配置文件
```

**Alembic 双引擎配置：**

```python
# alembic/env.py — 核心配置
from sqlalchemy import engine_from_config, pool
from alembic import context

# 运行时通过环境变量选择目标数据库
DATABASE_URL = os.getenv(
    "ALEMBIC_DATABASE_URL",
    "sqlite:///~/.simple-a2a-registry/registry.db",
)

def run_migrations_online():
    connectable = engine_from_config(
        config.get_section(config.config_ini_section),
        prefix="sqlalchemy.",
        url=DATABASE_URL,
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,     # 检测列类型变更
            compare_server_default=True,  # 检测默认值变更
        )
        with context.begin_transaction():
            context.run_migrations()
```

**迁移工作流：**

```bash
# 1. 自动生成迁移脚本
alembic revision --autogenerate -m "add agent_tags column"

# 2. 预览 SQL（不执行）
alembic upgrade head --sql

# 3. 应用到开发环境 (SQLite)
ALEMBIC_DATABASE_URL="sqlite:///dev.db" alembic upgrade head

# 4. 应用到生产环境 (MySQL)
ALEMBIC_DATABASE_URL="mysql+aiomysql://user:pass@host/db" alembic upgrade head
```

### 1.6 数据迁移工具

从现有 SQLite (`registry.db` + `board.db`) → MySQL：

```python
# scripts/migrate_to_mysql.py
"""
增量迁移脚本：SQLite → MySQL
策略：读取 SQLite 数据，逐表写入 MySQL，
      使用事务批处理 + 断点续传。

用法：
    python scripts/migrate_to_mysql.py \
        --sqlite ~/.simple-a2a-registry/registry.db \
        --mysql mysql+aiomysql://user:pass@localhost:3306/a2a_registry
"""
```

**迁移策略：**

| 阶段 | 操作 | 风险等级 |
|------|------|---------|
| Phase 1: Schema 对齐 | Alembic upgrade head 创建 MySQL 表结构 | 低 |
| Phase 2: 数据批量导入 | 读取 SQLite → batch INSERT MySQL | 中 |
| Phase 3: 双写过渡 | 应用层同时写 SQLite + MySQL，验证一致性 | 中 |
| Phase 4: 切换 | 改配置指向 MySQL，保留 SQLite 只读备份 | 低 |
| Phase 5: 清理 | 下线 SQLite，移除双写逻辑 | 低 |

**建议使用 `pgloader` 类似工具或自定义 Python 脚本**，以批处理方式迁移（每批 500 行），避免长事务锁表。

### 1.7 依赖变更

```toml
# pyproject.toml 新增依赖
dependencies = [
    "aiohttp>=3.9,<4",
    "aiohttp-cors>=0.7",
    "pyjwt>=2.8,<3",
    # ↓ 新增
    "sqlalchemy[asyncio]>=2.0,<3",
    "aiomysql>=0.2,<1",
    "alembic>=1.13,<2",
    "pyyaml>=6.0,<7",
    "prometheus-client>=0.20,<1",
]

[project.optional-dependencies]
dev = [
    "aiosqlite>=0.20,<1",     # 开发环境 SQLite 异步支持
]
```

---

## 2. 配置系统设计

### 2.1 三优先级：CLI > ENV > config.yaml

```
优先级 高 → 低
┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│  CLI 参数    │ ──▶ │  环境变量    │ ──▶ │  config.yaml │
│  final       │     │  override   │     │  defaults    │
│  --port 8888 │     │  A2A_PORT=  │     │  port: 8321  │
└─────────────┘     └─────────────┘     └─────────────┘
```

**优先级规则：**
1. 运行时 `--port 8888` 覆盖所有 → **最高**
2. 环境变量 `A2A_PORT=9999` 覆盖 config.yaml → **中**
3. config.yaml 中的值作为默认 → **基准**
4. 代码中的硬编码默认值 → **回退**
5. **无自动合并**——每个级别完整覆盖对应键，不逐字段 merge

### 2.2 配置结构

```yaml
# ~/.simple-a2a-registry/config.yaml

# ============================================================
# Simple A2A Registry — 配置文件
# 优先级: CLI 参数 > 环境变量 > 此文件
# 环境变量命名: A2A_<SECTION>_<KEY> (如 A2A_SERVER_PORT=8888)
# ============================================================

server:
  host: "0.0.0.0"              # 监听地址 | ENV: A2A_SERVER_HOST
  port: 8321                    # 监听端口 | ENV: A2A_SERVER_PORT
  base_url: "http://localhost:8321"  # 外部可访问 URL | ENV: A2A_SERVER_BASE_URL

database:
  # 生产环境 (MySQL)
  driver: "mysql+aiomysql"
  host: "127.0.0.1"
  port: 3306
  user: "a2a_registry"
  password: "${DB_PASSWORD}"    # 从环境变量读取
  database: "a2a_registry"
  charset: "utf8mb4"

  # 连接池
  pool:
    min_size: 5
    max_size: 20
    overflow: 10
    pool_timeout: 30
    pool_recycle: 3600
    pool_pre_ping: true

  # 开发环境覆盖 (通过环境变量 A2A_DATABASE_DRIVER=sqlite+aiosqlite)
  # dev:
  #   driver: "sqlite+aiosqlite"
  #   path: "~/.simple-a2a-registry/registry.db"

auth:
  enabled: false                # 启用 OAuth 2.1 认证 | ENV: A2A_AUTH_ENABLED
  algorithm: "RS256"            # RS256 生产 / HS256 开发
  token_expiry: 3600            # Token 过期时间(秒)
  bootstrap_secret: "${A2A_BOOTSTRAP_SECRET}"  # 管理账户密钥

logging:
  level: "INFO"                 # DEBUG / INFO / WARN / ERROR
  format: "json"                # json | text
  output: "stderr"              # stderr | stdout | filepath
  file: "~/.simple-a2a-registry/server.log"  # 文件路径（output=file 时生效）
  tracing: true                 # 启用 request_id + trace_id

orchestration:
  board_path: "~/.simple-a2a-registry/board.db"
  dispatcher:
    enabled: true
    poll_interval: 5            # 轮询间隔(秒)
    claim_ttl: 900              # 认领锁 TTL(秒)
    failure_limit: 3            # 默认重试次数
  workspaces_root: "~/.simple-a2a-registry/workspaces"

monitoring:
  metrics_enabled: true         # 启用 Prometheus /metrics 端点
  metrics_port: 8322            # 独立指标端口（0=使用主端口）
  # 如果为 0，指标端点挂载在 server 同一端口 /metrics

cors:
  enabled: true
  # 开发环境: "*"
  # 生产环境: 白名单
  allowed_origins:
    - "http://localhost:3000"
    - "https://dashboard.example.com"
  allow_credentials: true
  max_age: 3600                 # 预检请求缓存(秒)
```

### 2.3 配置加载实现

```python
# simple_a2a_registry/config.py
"""
三优先级配置加载器。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional

import yaml


def _env_to_key(env_name: str) -> tuple:
    """A2A_SERVER_PORT → ('server', 'port')"""
    parts = env_name.lower().replace("a2a_", "", 1).split("_")
    return tuple(parts)


def _deep_set(d: dict, keys: tuple, value: Any) -> None:
    for k in keys[:-1]:
        d = d.setdefault(k, {})
    d[keys[-1]] = value


def load_config(config_path: Optional[str] = None) -> Dict[str, Any]:
    """Load config with CLI > ENV > YAML priority.

    1. 读取 YAML 文件获取基准配置
    2. 扫描 A2A_* 环境变量覆盖对应键
    3. CLI 参数在调用方覆盖（此函数不处理 argparse）
    """
    # Step 1: 基准配置
    defaults = _default_config()
    config = dict(defaults)

    if config_path:
        path = Path(config_path).expanduser()
        if path.exists():
            with open(path, "r") as f:
                yml = yaml.safe_load(f) or {}
            _deep_merge(config, yml)

    # Step 2: 环境变量覆盖（扫描 A2A_*）
    for env_name, env_value in os.environ.items():
        if env_name.startswith("A2A_"):
            keys = _env_to_key(env_name)
            # 类型推断：数字、布尔、原样
            typed_value = _infer_type(env_value)
            _deep_set(config, keys, typed_value)

    return config


def _infer_type(value: str):
    """环境变量值类型推断"""
    if value.lower() in ("true", "yes", "1"):
        return True
    if value.lower() in ("false", "no", "0"):
        return False
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        pass
    return value


def _deep_merge(base: dict, override: dict) -> None:
    """递归合并字典（覆盖而非追加）"""
    for key, value in override.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value


def _default_config() -> Dict[str, Any]:
    """返回与代码硬编码默认值一致的基准配置"""
    return {
        "server": {"host": "0.0.0.0", "port": 8321},
        "database": {
            "driver": "sqlite+aiosqlite",
            "path": "~/.simple-a2a-registry/registry.db",
        },
        "auth": {"enabled": False},
        "logging": {"level": "INFO", "format": "text", "tracing": False},
        "orchestration": {
            "dispatcher": {
                "enabled": True,
                "poll_interval": 5,
                "claim_ttl": 900,
                "failure_limit": 3,
            },
            "workspaces_root": None,
        },
        "monitoring": {"metrics_enabled": False, "metrics_port": 0},
        "cors": {"enabled": False, "allowed_origins": [], "allow_credentials": False, "max_age": 3600},
    }
```

### 2.4 配置与 CLI 集成

```python
# 在 cli.py 中新增 --config 参数
parser.add_argument(
    "--config",
    default="~/.simple-a2a-registry/config.yaml",
    help="Config file path (default: ~/.simple-a2a-registry/config.yaml)",
)

def main(argv=None):
    args = parser.parse_args(argv)
    config = load_config(args.config)

    # CLI 参数覆盖 config 中的对应值
    # 注意：argparse 的默认值不会覆盖——只有当用户显式传参时才覆盖
    if args.host != "0.0.0.0":
        config["server"]["host"] = args.host
    if args.port != 8321:
        config["server"]["port"] = args.port
    # ... 其他参数同理

    # 将 config 传递给 create_app
    app = create_app(config=config, ...)
```

### 2.5 配置验证

```python
# 启动时验证必填项
def validate_config(config: dict) -> list[str]:
    errors = []

    # 数据库配置校验
    db = config.get("database", {})
    if "sqlite" not in db.get("driver", ""):
        if not db.get("host"):
            errors.append("database.host is required for MySQL driver")
        if not db.get("password"):
            errors.append("database.password is required for MySQL driver")

    # Auth 配置校验
    if config.get("auth", {}).get("enabled"):
        if not config.get("auth", {}).get("bootstrap_secret"):
            errors.append("auth.bootstrap_secret is required when auth is enabled")

    # CORS 校验
    if config.get("cors", {}).get("enabled"):
        origins = config.get("cors", {}).get("allowed_origins", [])
        if not origins:
            errors.append("cors.allowed_origins must not be empty when cors is enabled")

    return errors
```

---

## 3. 结构化日志

### 3.1 设计目标

| 需求 | 方案 | 优先级 |
|------|------|--------|
| 机器可解析 | JSON 格式输出，直接送 ELK/Loki | P0 |
| 人可读 | text 格式（带颜色可选），开发友好 | P0 |
| 请求追踪 | 每个请求分配 `request_id`，链路透传 `trace_id` | P0 |
| 日志级别可配置 | YAML/ENV/CLI 均可指定 | P0 |
| 动态调整级别 | 运行时 HTTP 端点调整日志级别（无需重启） | P1 |
| 敏感信息脱敏 | 自动遮蔽密码、Token、Secret | P1 |

### 3.2 JSON 日志格式

```json
{
  "timestamp": "2026-05-26T10:30:00.123Z",
  "level": "INFO",
  "logger": "a2a_registry.server",
  "message": "Agent 'agent-abc' registered successfully",
  "request_id": "req-abc123",
  "trace_id": "trace-xyz789",
  "agent_id": "agent-abc",
  "duration_ms": 42.5,
  "extra": {}
}
```

### 3.3 实现方案：JSON + Text 双模式

```python
# simple_a2a_registry/logging_config.py
"""
结构化日志配置 — JSON / Text 双模式。

用法：
    from simple_a2a_registry.logging_config import setup_logging
    setup_logging(config)  # 根据 config 设置日志
"""

import json
import logging
import sys
import time
import uuid
from typing import Any, Dict, Optional


class JSONFormatter(logging.Formatter):
    """JSON 日志格式化器 — 输出结构化 JSON 行。"""

    def format(self, record: logging.LogRecord) -> str:
        log_entry: Dict[str, Any] = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S.%fZ"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if hasattr(record, "request_id"):
            log_entry["request_id"] = record.request_id
        if hasattr(record, "trace_id"):
            log_entry["trace_id"] = record.trace_id
        if hasattr(record, "extra"):
            log_entry.update(record.extra)
        if record.exc_info and record.exc_info[0]:
            log_entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(log_entry, ensure_ascii=False, default=str)


class TextFormatter(logging.Formatter):
    """人类可读文本格式化器 — 带基本信息。"""

    # ISO8601-like format without microsecond noise
    FORMAT = "%(asctime)s [%(levelname)-5s] %(name)s: %(message)s"

    def __init__(self):
        super().__init__(
            self.FORMAT,
            datefmt="%H:%M:%S",
        )
```

### 3.4 请求追踪：request_id + trace_id

```python
# simple_a2a_registry/tracing.py
"""请求追踪中间件 — 为每个请求分配 request_id + trace_id。"""

import uuid
from aiohttp import web


TRACE_ID_HEADER = "X-Trace-Id"
REQUEST_ID_ATTR = "request_id"


@web.middleware
async def tracing_middleware(request: web.Request, handler) -> web.StreamResponse:
    """为每个请求注入 request_id，传递 trace_id。

    如果客户端在 X-Trace-Id 头中提供了 trace_id，则沿用（链路透传）；
    否则自动生成新的 trace_id。
    """
    request_id = str(uuid.uuid4())[:8]
    trace_id = request.headers.get(TRACE_ID_HEADER, str(uuid.uuid4()))

    request[REQUEST_ID_ATTR] = request_id
    request["trace_id"] = trace_id

    # 在日志记录器上设置上下文
    old_factory = logging.getLogRecordFactory()

    def record_factory(*args, **kwargs):
        record = old_factory(*args, **kwargs)
        record.request_id = request_id
        record.trace_id = trace_id
        return record

    logging.setLogRecordFactory(record_factory)

    start = time.monotonic()
    try:
        response = await handler(request)
        return response
    finally:
        duration_ms = (time.monotonic() - start) * 1000
        response.headers["X-Request-Id"] = request_id
        if "trace_id" in request:
            response.headers[TRACE_ID_HEADER] = request["trace_id"]
        # 恢复日志工厂
        logging.setLogRecordFactory(old_factory)
```

### 3.5 动态日志级别调整

```python
# 在 AdminHandler 或独立端点中实现
async def handle_set_log_level(request: web.Request) -> web.Response:
    """POST /admin/log-level — 运行时调整日志级别（无需重启）。

    Body: {"level": "DEBUG"}
    """
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "invalid_json"}, status=400)

    level_name = body.get("level", "").upper()
    level = getattr(logging, level_name, None)
    if level is None:
        return web.json_response(
            {"error": "invalid_level", "detail": f"Unknown level: {level_name}"},
            status=400,
        )

    logging.getLogger("a2a_registry").setLevel(level)
    logger.info("Log level changed to %s by admin request (request_id=%s)",
                level_name, request.get("request_id", ""))

    return web.json_response({
        "message": f"Log level changed to {level_name}",
        "previous_level": logging.getLevelName(logger.level),
    })


async def handle_get_log_level(request: web.Request) -> web.Response:
    """GET /admin/log-level — 查看当前日志级别。"""
    return web.json_response({
        "level": logging.getLevelName(logging.getLogger("a2a_registry").level),
    })
```

### 3.6 Logger 命名规范

```python
# 统一命名空间
# 所有 logger 使用 a2a_registry 前缀
logger = logging.getLogger("a2a_registry.server")    # server.py
logger = logging.getLogger("a2a_registry.store")      # store.py
logger = logging.getLogger("a2a_registry.auth")       # auth.py
logger = logging.getLogger("a2a_registry.cli")        # cli.py
logger = logging.getLogger("a2a_registry.config")     # config.py (新建)
```

### 3.7 请求日志采样示例

```json
{
  "timestamp": "2026-05-26T10:30:00.123Z",
  "level": "INFO",
  "logger": "a2a_registry.server",
  "message": "Handled request",
  "request_id": "req-a1b2c3",
  "trace_id": "trace-x1y2z3",
  "method": "POST",
  "path": "/v1/agents",
  "status": 201,
  "duration_ms": 42.5,
  "agent_id": "agent-abc"
}
```

---

## 4. 全局错误模型

### 4.1 统一响应格式

**成功响应：**
```json
{
  "data": { ... },
  "request_id": "req-abc123"
}
```

**错误响应：** (当前 `_json_error` 的增强版本)
```json
{
  "error": "validation_error",
  "detail": "Agent requires a 'name'",
  "request_id": "req-abc123",
  "timestamp": "2026-05-26T10:30:00.123Z"
}
```

### 4.2 错误分类体系

| 分类 | 错误码 | HTTP 状态码 | 说明 | 当前代码位置 |
|------|--------|------------|------|-------------|
| **Validation** | `validation_error` | 400 | 请求体校验失败 | `server.py L228` |
| | `invalid_json` | 400 | JSON 解析失败 | `server.py L223` |
| **Auth** | `unauthorized` | 401 | 缺少或无效的认证凭据 | `server.py L336` |
| | `invalid_token` | 401 | Token 过期或无效 | `server.py L347` |
| | `forbidden` | 403 | 权限不足（scope 不匹配） | `server.py L376` |
| | `invalid_scope` | 400 | 请求了未知 scope | `server.py L771` |
| **Not Found** | `agent_not_found` | 404 | Agent 不存在 | `server.py L186` |
| | `task_not_found` | 404 | 任务不存在 | `server.py L588` |
| | `not_found` | 404 | 通用资源不存在 | `server.py L832` |
| **Conflict** | `agent_exists` | 409 | Agent 名称已存在 | `server.py L235` |
| | `conflict` | 409 | 通用冲突 | — |
| **Stale** | `agent_stale` | 410 | Agent 已过期 | `server.py L287` |
| **Rate Limit** | `too_many_requests` | 429 | 请求频率超限 | — |
| **Internal** | `internal_error` | 500 | 未预期异常 | `server.py L696` |
| | `dispatch_failed` | 502 | 分发到 Agent 失败 | `server.py L572` |
| | `agent_not_connected` | 503 | Agent 未通过 WS 连接 | `server.py L528` |
| | `agent_not_routable` | 400 | Agent 无可用 URL | `server.py L654` |
| **Protected** | `protected` | 403 | 试图删除受保护资源 | `server.py L826` |

### 4.3 现有 _json_error → 增强方案

**当前实现** (server.py L56-60):
```python
def _json_error(status: int, error_code: str, detail: str) -> web.Response:
    return web.json_response(
        {"error": error_code, "detail": detail},
        status=status,
    )
```

**增强方案：**
```python
# simple_a2a_registry/errors.py
"""全局错误模型 — 统一错误响应格式。"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from aiohttp import web


@dataclass
class APIError:
    """统一错误响应体。"""
    error: str                          # 错误码
    detail: str                         # 人类可读描述
    request_id: str = ""                # 请求追踪 ID
    timestamp: str = field(default_factory=lambda:
        datetime.now(timezone.utc).isoformat())
    extra: Optional[Dict[str, Any]] = None  # 可选补充信息

    def to_response(self, status: int) -> web.Response:
        body: Dict[str, Any] = {
            "error": self.error,
            "detail": self.detail,
            "request_id": self.request_id,
            "timestamp": self.timestamp,
        }
        if self.extra:
            body["extra"] = self.extra
        return web.json_response(body, status=status)


# 便捷工厂函数
def json_error(
    status: int,
    error_code: str,
    detail: str,
    request: Optional[web.Request] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> web.Response:
    err = APIError(
        error=error_code,
        detail=detail,
        request_id=request.get("request_id", "") if request else "",
        extra=extra,
    )
    return err.to_response(status)
```

### 4.4 异常 → HTTP 映射

```python
# simple_a2a_registry/errors.py 中的异常类

class A2ARegistryError(Exception):
    """所有 Registry 异常的基类。"""
    def __init__(self, error_code: str, detail: str,
                 status: int = 500,
                 extra: Optional[Dict] = None):
        self.error_code = error_code
        self.detail = detail
        self.status = status
        self.extra = extra
        super().__init__(detail)


class ValidationError(A2ARegistryError):
    def __init__(self, detail: str, extra: Optional[Dict] = None):
        super().__init__("validation_error", detail, 400, extra)


class NotFoundError(A2ARegistryError):
    def __init__(self, resource_type: str, resource_id: str):
        super().__init__(
            f"{resource_type}_not_found",
            f"{resource_type.capitalize()} '{resource_id}' not found",
            404,
        )


class ConflictError(A2ARegistryError):
    def __init__(self, detail: str):
        super().__init__("conflict", detail, 409)


class AuthError(A2ARegistryError):
    def __init__(self, error_code: str, detail: str, status: int = 401):
        super().__init__(error_code, detail, status)
```

**增强的 error_middleware：**
```python
@web.middleware
async def error_middleware(request: web.Request, handler) -> web.StreamResponse:
    try:
        response = await handler(request)
        return response
    except A2ARegistryError as e:
        return json_error(
            e.status, e.error_code, e.detail,
            request=request, extra=e.extra,
        )
    except web.HTTPException as exc:
        return json_error(
            exc.status,
            _status_to_error_code(exc.status),
            exc.reason or str(exc),
            request=request,
        )
    except json.JSONDecodeError:
        return json_error(400, "invalid_json", "Invalid JSON body", request=request)
    except Exception:
        logger.exception("Unhandled error handling %s %s",
                         request.method, request.path)
        return json_error(500, "internal_error", "Internal server error",
                         request=request)
```

### 4.5 向后兼容

- 新的错误格式增加了 `request_id` 和 `timestamp` 字段
- 所有现有客户端收到的响应仍包含 `error` 和 `detail`
- 新增字段是**附加的**，不破坏已有解析逻辑
- 过渡期可加 `X-API-Version: 1` 头让客户端自由选择格式

---

## 5. Prometheus 指标命名

### 5.1 命名规范

| 规则 | 示例 | 说明 |
|------|------|------|
| 前缀 | `a2a_registry_` | 全局唯一前缀，防命名冲突 |
| 命名风格 | snake_case | Prometheus/PromQL 常规 |
| 单位后缀 | `_seconds` `_total` `_bytes` | 遵循 Prometheus 命名最佳实践 |
| 标签(labels) | `endpoint`, `method`, `status`, `agent_id` | 用于多维聚合 |

### 5.2 指标清单

#### Counter（计数器：只增不减）

| 指标名 | 标签 | 说明 | 代码位置 |
|--------|------|------|---------|
| `a2a_registry_requests_total` | `method`, `endpoint`, `status` | 总请求计数 | 中间件 |
| `a2a_registry_requests_by_endpoint` | `endpoint`, `method` | 按端点拆分的请求量 | 中间件 |
| `a2a_registry_agent_operations_total` | `operation` (register/unregister/heartbeat/ws_connect/ws_disconnect) | Agent 操作计数 | handler |
| `a2a_registry_tasks_dispatched_total` | `agent_id` | 分发任务计数 | handle_dispatch |
| `a2a_registry_tasks_completed_total` | `status` (completed/failed) | 任务完成/失败计数 | WS task_result |
| `a2a_registry_auth_requests_total` | `grant_type`, `status` | 认证请求计数 | auth handler |
| `a2a_registry_cors_preflight_total` | `origin` | CORS 预检请求计数 | CORS handler |

#### Gauge（仪表盘：可增可减）

| 指标名 | 标签 | 说明 | 代码位置 |
|--------|------|------|---------|
| `a2a_registry_agents_alive` | — | 当前活跃 Agent 数量 | 定时采集 |
| `a2a_registry_agents_stale` | — | 当前过期 Agent 数量 | 定时采集 |
| `a2a_registry_ws_connections` | — | 当前 WebSocket 连接数 | RegistryHandler |
| `a2a_registry_db_pool_size` | — | 数据库连接池当前大小 | 连接池回调 |
| `a2a_registry_db_pool_available` | — | 数据库连接池可用连接 | 连接池回调 |
| `a2a_registry_tasks_pending` | `status` (todo/ready/running/blocked) | 各种状态的待办任务数 | 定时采集 |
| `a2a_registry_uptime_seconds` | — | 服务启动时间 | 启动时记录 |
| `a2a_registry_dispatcher_queue_depth` | — | Dispatcher 待处理队列深度 | dispatcher |

#### Histogram（直方图：延迟分布）

| 指标名 | 标签 | Buckets | 说明 |
|--------|------|---------|------|
| `a2a_registry_request_duration_seconds` | `method`, `endpoint` | [0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10] | 请求处理延迟 |
| `a2a_registry_db_query_duration_seconds` | `operation` | [0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1] | 数据库查询延迟 |
| `a2a_registry_ws_message_duration_seconds` | `msg_type` | [0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1] | WS 消息处理延迟 |
| `a2a_registry_task_duration_seconds` | `status` | [1, 5, 10, 30, 60, 120, 300, 600] | 任务端到端耗时 |

### 5.3 实现方案

```python
# simple_a2a_registry/metrics.py
"""Prometheus 指标定义与暴露。"""

from prometheus_client import Counter, Gauge, Histogram
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST
from aiohttp import web


# === Counters ===
requests_total = Counter(
    "a2a_registry_requests_total",
    "Total HTTP requests",
    ["method", "endpoint", "status"],
)
requests_by_endpoint = Counter(
    "a2a_registry_requests_by_endpoint",
    "Requests by endpoint",
    ["endpoint", "method"],
)
agent_operations = Counter(
    "a2a_registry_agent_operations_total",
    "Agent lifecycle operations",
    ["operation"],  # register, unregister, heartbeat, ws_connect, ws_disconnect
)
tasks_dispatched = Counter(
    "a2a_registry_tasks_dispatched_total",
    "Tasks dispatched to agents",
    ["agent_id"],
)
tasks_completed = Counter(
    "a2a_registry_tasks_completed_total",
    "Tasks completed",
    ["status"],  # completed, failed
)

# === Gauges ===
agents_alive = Gauge("a2a_registry_agents_alive", "Currently alive agents")
agents_stale = Gauge("a2a_registry_agents_stale", "Currently stale agents")
ws_connections = Gauge("a2a_registry_ws_connections", "Active WebSocket connections")
db_pool_size = Gauge("a2a_registry_db_pool_size", "Database pool total connections")
db_pool_available = Gauge("a2a_registry_db_pool_available", "Database pool available connections")
tasks_pending = Gauge(
    "a2a_registry_tasks_pending",
    "Pending tasks by status",
    ["status"],
)
uptime_seconds = Gauge("a2a_registry_uptime_seconds", "Server uptime in seconds")

# === Histograms ===
request_duration = Histogram(
    "a2a_registry_request_duration_seconds",
    "HTTP request duration in seconds",
    ["method", "endpoint"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
)
db_query_duration = Histogram(
    "a2a_registry_db_query_duration_seconds",
    "Database query duration in seconds",
    ["operation"],
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1),
)


# === Metrics Middleware ===
@web.middleware
async def metrics_middleware(request: web.Request, handler) -> web.StreamResponse:
    """记录请求计数和延迟。"""
    method = request.method
    # 提取路由模式（匹配 aiohttp resource）而非实际路径
    route = request.match_info.route
    endpoint = route.resource.canonical if route and route.resource else request.path

    with request_duration.labels(method=method, endpoint=endpoint).time():
        response = await handler(request)

    requests_total.labels(
        method=method, endpoint=endpoint, status=response.status,
    ).inc()
    requests_by_endpoint.labels(endpoint=endpoint, method=method).inc()

    return response


# === Metrics Endpoint ===
async def handle_metrics(request: web.Request) -> web.Response:
    """GET /metrics — Prometheus 抓取端点。"""
    data = generate_latest()
    return web.Response(
        body=data,
        content_type=CONTENT_TYPE_LATEST,
    )
```

### 5.4 指标采集点

| 采集点 | 触发时机 | 指标 |
|--------|---------|------|
| 请求中间件 | 每个 HTTP 请求 | `requests_total`, `request_duration_seconds` |
| 注册/注销回调 | 每个 Agent 操作 | `agent_operations_total` |
| WS 连接/断开 | 每次 WS 建立/关闭 | `ws_connections`, `agent_operations_total` |
| 任务分发 | 每次 `POST .../dispatch` | `tasks_dispatched_total` |
| 任务完成 | WS 收到 `task_result` | `tasks_completed_total` |
| 定时采集 | 每 15 秒（Proemtheus scrape_interval） | `agents_alive`, `agents_stale`, `tasks_pending` |
| 数据库连接池事件 | 连接创建/释放 | `db_pool_size`, `db_pool_available` |

### 5.5 独立 Metrics 端口

当 `monitoring.metrics_port > 0` 时，启动第二个 aiohttp server 只暴露 `/metrics`：

```python
async def start_metrics_server(config: dict) -> None:
    """在独立端口启动 Prometheus 抓取端点。"""
    port = config["monitoring"]["metrics_port"]
    if port <= 0:
        return  # 使用主端口，/metrics 已注册在主 app

    app = web.Application()
    app.router.add_get("/metrics", handle_metrics)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info("Prometheus metrics endpoint on :%d/metrics", port)
```

**为什么可能需要独立端口：**
- 避免外部通过 `/metrics` 暴露内部细节
- 指标端口可绑定内部网络接口（`127.0.0.1:8322`），不对外暴露
- 与主服务的认证策略解耦

### 5.6 推荐 Grafana Dashboard 面板

| 面板 | 指标 | 图表类型 |
|------|------|---------|
| 请求 QPS | `rate(a2a_registry_requests_total[5m])` | 折线图 |
| P99 延迟 | `histogram_quantile(0.99, ...)` | 热力图 |
| 活跃 Agent | `a2a_registry_agents_alive` | 仪表盘 |
| WS 连接数 | `a2a_registry_ws_connections` | 折线图 |
| 任务吞吐 | `rate(a2a_registry_tasks_completed_total[5m])` | 折线图 |
| 数据库延迟 | `a2a_registry_db_query_duration_seconds` | 热力图 |

---

## 6. CORS 设计

### 6.1 设计目标

| 需求 | 方案 | 优先级 |
|------|------|--------|
| 浏览器端 Dashboard 访问 | 允许跨域请求 | P0 |
| 开发环境无限制 | `Access-Control-Allow-Origin: *` | P0 |
| 生产环境白名单 | 配置允许的来源列表 | P0 |
| 安全凭证 | 支持 `credentials: include` | P0 |
| 预检缓存 | `Access-Control-Max-Age` 减少 OPTIONS 请求 | P1 |

### 6.2 实现方案

当前项目中 `pyproject.toml` 已声明依赖 `aiohttp-cors>=0.7`，但 `server.py` 中未调用。

**使用 aiohttp-cors 库（推荐）：**

```python
# 在 create_app() 中添加 CORS 配置
import aiohttp_cors

def setup_cors(app: web.Application, config: dict) -> None:
    """根据配置设置 CORS。"""
    cors_config = config.get("cors", {})
    if not cors_config.get("enabled", False):
        return

    origins = cors_config.get("allowed_origins", [])
    if not origins:
        return

    # aiohttp-cors 配置
    cors = aiohttp_cors.setup(app, defaults={
        origin: aiohttp_cors.ResourceOptions(
            allow_credentials=cors_config.get("allow_credentials", False),
            expose_headers="*",
            allow_headers="*",
            max_age=cors_config.get("max_age", 3600),
        )
        for origin in origins
    })

    # 为所有已注册路由启用 CORS
    for route in app.router.routes():
        cors.add(route)
```

**不依赖 aiohttp-cors 的手动方案（轻量备选）：**

```python
# simple_a2a_registry/cors.py
"""CORS 中间件 — 支持开发/生产双模式。"""

from aiohttp import web


@web.middleware
async def cors_middleware(request: web.Request, handler) -> web.StreamResponse:
    """CORS 中间件 — 处理跨域请求和预检。"""

    # 处理预检请求
    if request.method == "OPTIONS":
        response = web.Response()
        _set_cors_headers(response, request, request.app["cors_config"])
        return response

    response = await handler(request)
    _set_cors_headers(response, request, request.app["cors_config"])
    return response


def _set_cors_headers(
    response: web.StreamResponse,
    request: web.Request,
    config: dict,
) -> None:
    """设置 CORS 响应头。"""
    origin = request.headers.get("Origin", "")

    if not config.get("enabled", False):
        return

    allowed = config.get("allowed_origins", [])
    allow_credentials = config.get("allow_credentials", True)
    max_age = config.get("max_age", 3600)

    # 开发模式：允许所有来源
    if not allowed or allowed == ["*"]:
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, X-Trace-Id"
        return

    # 生产模式：白名单校验
    if origin in allowed:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, X-Trace-Id"

        if allow_credentials:
            response.headers["Access-Control-Allow-Credentials"] = "true"

        if max_age > 0:
            response.headers["Access-Control-Max-Age"] = str(max_age)
```

### 6.3 配置示例

```yaml
# 开发环境 — 允许所有来源
cors:
  enabled: true
  allowed_origins:
    - "*"
  allow_credentials: false
  max_age: 3600

# 生产环境 — 白名单
cors:
  enabled: true
  allowed_origins:
    - "https://dashboard.example.com"
    - "https://admin.internal.example.com"
  allow_credentials: true
  max_age: 3600
```

### 6.4 安全注意事项

| 注意点 | 说明 |
|--------|------|
| `Access-Control-Allow-Origin: *` 不能与 `credentials: include` 同时使用 | 浏览器规范限制。白名单模式需指定具体 origin |
| 预检请求无状态 | OPTIONS 请求不应修改服务端状态 |
| CORS 不替代认证 | CORS 是浏览器安全机制，不能替代 Token 认证。恶意客户端仍可跳过浏览器直接 HTTP 调用 |
| 错误响应也应带 CORS 头 | 确保 400/401/500 等错误响应也能被浏览器端 JS 读取到错误信息 |

---

## 7. 实施路线图

### Phase 1: 基础框架 (P0，1-2天)

| 任务 | 文件 | 依赖 |
|------|------|------|
| 创建 `config.py` 配置加载器 | `simple_a2a_registry/config.py` | 无 |
| 创建 `errors.py` 统一错误模型 | `simple_a2a_registry/errors.py` | 无 |
| 在 server.py 中替换 `_json_error` 为 `errors.py` | `simple_a2a_registry/server.py` | errors.py 完成 |
| 创建 `logging_config.py` 结构化日志 | `simple_a2a_registry/logging_config.py` | 无 |
| 更新 error_middleware 使用新的错误类 | `simple_a2a_registry/server.py` | errors.py 完成 |
| 更新 `pyproject.toml` 新增依赖 | `pyproject.toml` | 无 |

### Phase 2: CORS + Metrics (P0，1天)

| 任务 | 文件 | 依赖 |
|------|------|------|
| 创建 `cors.py` CORS 中间件 | `simple_a2a_registry/cors.py` | config.py 完成 |
| 创建 `metrics.py` Prometheus 指标定义 | `simple_a2a_registry/metrics.py` | 无 |
| 在 create_app 中注册中间件 | `simple_a2a_registry/server.py` | cors.py, metrics.py 完成 |
| 更新 CLI 参数支持 `--config` | `simple_a2a_registry/cli.py` | config.py 完成 |

### Phase 3: MySQL 迁移 (P1，2-3天)

| 任务 | 文件 | 依赖 |
|------|------|------|
| 添加 SQLAlchemy + aiomysql 依赖 | `pyproject.toml` | 无 |
| 创建 `DatabaseEngine` 抽象层 | `simple_a2a_registry/db/engine.py` | 无 |
| 创建 `MySQLStore` 实现 | `simple_a2a_registry/db/mysql_store.py` | engine.py 完成 |
| 初始化 Alembic 迁移环境 | `alembic/` | 无 |
| 首版迁移脚本：所有表结构 | `alembic/versions/0001_*.py` | Alebmic 初始化完成 |
| 数据迁移脚本 | `scripts/migrate_to_mysql.py` | MySQLStore 完成 |
| 更新 Store 构造方法支持配置驱动 | `simple_a2a_registry/store.py` | config.py, engine.py 完成 |

### Phase 4: 完善 (P1-P2，1-2天)

| 任务 | 文件 | 依赖 |
|------|------|------|
| 请求追踪中间件 `tracing.py` | `simple_a2a_registry/tracing.py` | logging_config.py 完成 |
| 动态日志级别 API | `simple_a2a_registry/server.py` | logging_config.py 完成 |
| 配置验证启动检查 | `simple_a2a_registry/config.py` | config.py 完成 |
| 单元测试覆盖 | `tests/` | 各模块完成 |
| 更新 `docs/architecture.md` 反映新架构 | `docs/architecture.md` | 全部完成 |

---

## 附录：更新计划 — 已存在的文件修改

### server.py 中的 TODO 标记（对应实现位置）

```python
# === TODO: P1-A Config System ===
# 位置: cli.py main() — 添加 --config 参数
# 位置: create_app() — 接收 config dict 替代散参数

# === TODO: P1-A Error Model ===
# 位置: server.py L56-60 — 替换 _json_error 为 errors.json_error
# 位置: server.py L670-715 — 替换 _error_middleware 为增强版本
# 位置: server.py L699-715 — 替换 _status_to_error_code 为 errors 模块

# === TODO: P1-A Structured Logging ===
# 位置: server.py L39 — logger 命名保持不变
# 位置: server.py L974-999 — create_app 中 setup_logging(config)
# 位置: server.py L1187-1217 — 启动/停止钩子中调用 tracing 中间件

# === TODO: P1-A CORS ===
# 位置: server.py L1056 — 在 app 创建后调用 setup_cors(app, config)
# 位置: server.py L974-999 — create_app 接收 cors_config

# === TODO: P1-A Metrics ===
# 位置: server.py — 新增 /metrics 路由
# 位置: server.py — 新增 metrics_middleware
# 位置: server.py L1187-1217 — 启动钩子中可选启动 metrics 独立端口

# === TODO: P1-A MySQL Migration ===
# 位置: store.py L127-161 — Store.__init__ 支持配置驱动引擎选择
# 位置: store.py L169-185 — _tx 上下文管理器改为 engine.fetch*/execute
# 位置: 新增 simple_a2a_registry/db/ — 抽象引擎层 + MySQL 实现
# 位置: cli.py L39-42 — data_dir 参数将逐步迁移到 config.database.path
```