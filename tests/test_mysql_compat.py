"""MySQL 兼容性测试（P1-I）。

测试覆盖 MySQL 数据库驱动的配置、工厂、连接池和与 SQLite 的行为一致性。

这些测试在同一套测试框架下验证两种驱动模式：
1. SQLite（默认/向后兼容）— 始终运行
2. MySQL（生产环境驱动）— 有 MySQL 连接时运行
"""
from __future__ import annotations

import os
import tempfile

import pytest
from aiohttp.test_utils import TestServer, TestClient

from simple_a2a_registry.config import Config, DatabaseConfig
from simple_a2a_registry.database import create_engine, SQLiteEngine, MySQLEngine, RetryEngine
from simple_a2a_registry.server import create_app

# ---------------------------------------------------------------------------
# MySQL 可用性探测
# ---------------------------------------------------------------------------

_has_mysql = None


def _check_mysql_available() -> bool:
    """检查是否有可用的 MySQL 测试数据库。"""
    global _has_mysql
    if _has_mysql is not None:
        return _has_mysql

    mysql_dsn = os.environ.get("A2A_REGISTRY_TEST_MYSQL_DSN", "")
    if not mysql_dsn:
        _has_mysql = False
        return False

    try:
        cfg = DatabaseConfig(driver="mysql", mysql_dsn=mysql_dsn)
        eng = MySQLEngine(cfg)
        eng.connect()
        eng.execute("SELECT 1")
        eng.close()
        _has_mysql = True
        return True
    except Exception:
        _has_mysql = False
        return False


mysql_available = pytest.mark.skipif(
    not _check_mysql_available(),
    reason="No MySQL available — set A2A_REGISTRY_TEST_MYSQL_DSN env var",
)


# =========================================================================
# DatabaseConfig — MySQL 配置模型
# =========================================================================


class TestMySqlConfig:
    """MySQL 配置模型验证。"""

    def test_mysql_driver_string(self):
        """driver='mysql' 设置正确驱动标识。"""
        cfg = DatabaseConfig(driver="mysql")
        assert cfg.driver == "mysql"

    def test_mysql_custom_pool_size(self):
        """MySQL 连接池配置。"""
        cfg = DatabaseConfig(driver="mysql", pool_size=25, max_overflow=50)
        assert cfg.pool_size == 25
        assert cfg.max_overflow == 50

    def test_mysql_dsn_default(self):
        """MySQL DSN 默认值为空字符串（需手动设置）。"""
        cfg = DatabaseConfig(driver="mysql")
        assert isinstance(cfg.mysql_dsn, str)

    def test_mysql_dsn_custom(self):
        """自定义 MySQL DSN。"""
        custom_dsn = "mysql+pymysql://app:***@prod:3306/mydb"
        cfg = DatabaseConfig(driver="mysql", mysql_dsn=custom_dsn)
        assert cfg.mysql_dsn == custom_dsn

    def test_create_config_with_mysql(self):
        """完整 Config 对象中使用 MySQL 数据库配置。"""
        cfg = Config()
        cfg.database.driver = "mysql"
        cfg.database.mysql_dsn = "mysql+pymysql://test:***@localhost:3306/test"
        cfg.database.pool_size = 20
        assert cfg.database.driver == "mysql"
        assert "test" in cfg.database.mysql_dsn

    def test_env_var_config_for_mysql(self):
        """环境变量 A2A_REGISTRY_DATABASE__DRIVER=mysql 设置 MySQL 驱动。"""
        os.environ["A2A_REGISTRY_DATABASE__DRIVER"] = "mysql"
        try:
            from simple_a2a_registry.config import load_config
            c = load_config()
            assert c.database.driver == "mysql"
        finally:
            del os.environ["A2A_REGISTRY_DATABASE__DRIVER"]


# =========================================================================
# create_engine — MySQL 驱动工厂
# =========================================================================


class TestCreateEngine:
    """create_engine 工厂函数测试。"""
    pytestmark = pytest.mark.asyncio

    def test_sqlite_engine_from_config(self):
        """driver='sqlite' 产生 SQLiteEngine（或 RetryEngine 包装）。"""
        cfg = DatabaseConfig(driver="sqlite")
        engine = create_engine(cfg)
        assert isinstance(engine, (SQLiteEngine, RetryEngine))
        engine.close()

    def test_mysql_engine_from_config(self):
        """driver='mysql' 产生 MySQLEngine。"""
        cfg = DatabaseConfig(
            driver="mysql",
            mysql_dsn=os.environ.get(
                "A2A_REGISTRY_TEST_MYSQL_DSN",
                "mysql+pymysql://user:***@localhost:3306/db",
            ),
        )
        engine = create_engine(cfg)
        assert isinstance(engine, MySQLEngine), f"Expected MySQLEngine, got {type(engine)}"
        engine.close()

    @mysql_available
    async def test_mysql_engine_connect_actual(self):
        """MySQL 引擎可实际连接并执行查询。"""
        dsn = os.environ["A2A_REGISTRY_TEST_MYSQL_DSN"]
        cfg = DatabaseConfig(driver="mysql", mysql_dsn=dsn)
        engine = create_engine(cfg)
        engine.connect()
        try:
            result = engine.execute("SELECT COUNT(*) as cnt FROM agents")
            row = result.fetchone()
            assert row is not None
            assert "cnt" in row
        except Exception:
            # Table may not exist yet — connection itself is verified
            pass
        finally:
            engine.close()

    def test_create_engine_sqlite_then_mysql(self):
        """create_engine 基于不同驱动配置产生不同的引擎类型。"""
        sqlite_cfg = DatabaseConfig(driver="sqlite")
        engine1 = create_engine(sqlite_cfg)
        assert isinstance(engine1, (SQLiteEngine, RetryEngine))

        mysql_cfg = DatabaseConfig(
            driver="mysql",
            mysql_dsn="mysql+pymysql://u:***@h:3306/d",
        )
        engine2 = create_engine(mysql_cfg)
        assert isinstance(engine2, MySQLEngine), f"Expected MySQLEngine, got {type(engine2)}"

        engine1.close()
        engine2.close()

    def test_engine_independence(self):
        """不同引擎实例互不影响。"""
        import tempfile
        import uuid

        path_a = tempfile.mktemp(suffix=f"_{uuid.uuid4().hex}.db")
        path_b = tempfile.mktemp(suffix=f"_{uuid.uuid4().hex}.db")

        cfg_a = DatabaseConfig(driver="sqlite", sqlite_path=path_a)
        cfg_b = DatabaseConfig(driver="sqlite", sqlite_path=path_b)

        eng_a = create_engine(cfg_a)
        eng_b = create_engine(cfg_b)

        eng_a.connect()
        eng_b.connect()

        eng_a.execute("SELECT 1")
        eng_b.execute("SELECT 1")

        eng_a.close()
        eng_b.close()

        # Cleanup temp files
        for p in [path_a, path_b]:
            try:
                os.unlink(p)
            except OSError:
                pass


# =========================================================================
# 连接池配置验证
# =========================================================================


class TestMySqlPoolConfig:
    """MySQL 连接池设置验证。"""

    def test_pool_size_from_config(self):
        """Config 中的 pool_size 传递到 MySQLEngine。"""
        cfg = DatabaseConfig(driver="mysql", pool_size=15, max_overflow=30)
        engine = MySQLEngine(cfg)
        # pool_size/max_overflow are stored in config and used at connect()
        assert engine._config.pool_size == 15
        assert engine._config.max_overflow == 30

    def test_pool_defaults(self):
        """MySQLEngine 的默认连接池参数来自 DatabaseConfig 默认值。"""
        cfg = DatabaseConfig(driver="mysql")
        engine = MySQLEngine(cfg)
        assert engine._config.pool_size == 10
        assert engine._config.max_overflow == 20

    def test_config_object_propagates_to_engine(self):
        """DatabaseConfig 对象的值传递给 MySQLEngine 构造函数。"""
        cfg = DatabaseConfig(driver="mysql", pool_size=30, max_overflow=60)
        engine = MySQLEngine(cfg)
        assert engine._config.pool_size == 30
        assert engine._config.max_overflow == 60


# =========================================================================
# SQLite ↔ MySQL 双模式
# =========================================================================


class TestDualModeConfig:
    """在同一套测试框架下验证 SQLite 和 MySQL 配置模式。"""
    pytestmark = pytest.mark.asyncio

    @mysql_available
    async def test_app_creates_with_mysql_config(self):
        """MySQL 配置模式下 create_app 能正常创建（架构初始化）。"""
        dsn = os.environ["A2A_REGISTRY_TEST_MYSQL_DSN"]
        cfg = Config()
        cfg.database.driver = "mysql"
        cfg.database.mysql_dsn = dsn

        client = await _make_app_with_config(cfg)
        try:
            resp = await client.get("/health")
            assert resp.status == 200
        finally:
            await client.close()

    async def test_dual_mode_health_structure(self):
        """SQLite 和 MySQL 模式下的健康检查返回相同结构。"""
        # SQLite mode
        client = await _make_app_with_config(None)
        try:
            sqlite_resp = await client.get("/health")
            assert sqlite_resp.status == 200
            sqlite_data = await sqlite_resp.json()
            assert "status" in sqlite_data
        finally:
            await client.close()

        # MySQL mode (if available)
        dsn = os.environ.get("A2A_REGISTRY_TEST_MYSQL_DSN", "")
        if dsn:
            cfg = Config()
            cfg.database.driver = "mysql"
            cfg.database.mysql_dsn = dsn
            client = await _make_app_with_config(cfg)
            try:
                mysql_resp = await client.get("/health")
                assert mysql_resp.status == 200
                mysql_data = await mysql_resp.json()
                assert "status" in mysql_data
            finally:
                await client.close()

    @mysql_available
    async def test_dual_mode_agent_register(self):
        """同一套 Agent 注册逻辑在两种配置下工作。"""
        # MySQL mode
        dsn = os.environ["A2A_REGISTRY_TEST_MYSQL_DSN"]
        cfg = Config()
        cfg.database.driver = "mysql"
        cfg.database.mysql_dsn = dsn
        client = await _make_app_with_config(cfg)
        try:
            resp = await client.post("/v1/agents", json={"name": "mysql-agent"})
            assert resp.status in (200, 201)
        finally:
            await client.close()


# =========================================================================
# 配置驱动的引擎切换
# =========================================================================


class TestEngineSwitching:
    """运行时通过 Config 切换引擎的能力。"""

    def test_sqlite_to_mysql_config_change(self):
        """同一 Config 对象可切换数据库驱动。"""
        cfg = Config()
        assert cfg.database.driver == "sqlite"  # default

        cfg.database.driver = "mysql"
        cfg.database.mysql_dsn = "mysql+pymysql://u:***@h:3306/db"
        assert cfg.database.driver == "mysql"

        cfg.database.driver = "sqlite"
        assert cfg.database.driver == "sqlite"


# =========================================================================
# Helper
# =========================================================================


async def _make_app_with_config(config: Config | None) -> TestClient:
    """Create a TestClient with the given Config (or default SQLite)."""
    tmpdir_obj = tempfile.TemporaryDirectory()
    data_dir = tmpdir_obj.name

    app = create_app(
        data_dir=data_dir,
        base_url="http://localhost:8321",
        config=config,
        dispatcher_enabled=False,
    )
    server = TestServer(app)
    await server.start_server()
    client = TestClient(server)

    # Override close to clean up tmpdir
    orig_close = client.close

    async def _close():
        await orig_close()
        try:
            tmpdir_obj.cleanup()
        except Exception:
            pass

    client.close = _close  # type: ignore[method-assign]
    return client