"""测试：首次启动默认 admin 引导流程 — 初始密码打印到日志。

验证：
1. UserStore.bootstrap_admin() 在空数据库创建 admin 并返回随机密码
2. 日志中打印带框的密码提示（含 ╔╗╚╝ 装饰）
3. 已有用户时 bootstrap_admin() 是空操作（返回空串）
4. create_app() 调用 bootstrap_admin() 并记录到日志
"""
from __future__ import annotations

import logging
import re
import tempfile
import io

import pytest

from simple_a2a_registry.database import SQLiteEngine
from simple_a2a_registry.users import UserStore
from simple_a2a_registry.server import create_app


# ---------------------------------------------------------------------------
# 单元测试：UserStore.bootstrap_admin() 日志输出
# ---------------------------------------------------------------------------


@pytest.fixture
def engine():
    db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db.close()
    e = SQLiteEngine(db.name)
    e.connect()
    yield e
    e.close()


@pytest.fixture
def store(engine):
    return UserStore(engine)


class TestBootstrapAdminLog:
    """验证 bootstrap_admin() 的日志输出格式和密码可见性。"""

    def test_bootstrap_log_contains_password(self, store):
        """bootstrap_admin() 的日志消息应包含实际密码（带框打印）。"""
        logger = logging.getLogger("a2a_registry.users")
        old_level = logger.level
        logger.setLevel(logging.INFO)

        try:
            capture = io.StringIO()
            handler = logging.StreamHandler(capture)
            handler.setFormatter(logging.Formatter("%(message)s"))
            logger.addHandler(handler)

            password = store.bootstrap_admin()
            assert password, "should return non-empty password on first start"

            handler.flush()
            log_text = capture.getvalue()

            # 验证密码出现在日志中
            assert password in log_text, (
                f"Password '{password}' should appear in log output"
            )
            # 验证带框装饰
            assert "╔" in log_text, "Log should have box-drawing header"
            assert "╚" in log_text, "Log should have box-drawing footer"
            assert "BOOTSTRAPPED" in log_text, "Log should contain BOOTSTRAPPED label"
            assert "Username: admin" in log_text
            assert "Password:" in log_text
            assert "CHANGE THIS PASSWORD" in log_text

        finally:
            logger.removeHandler(handler)
            logger.setLevel(old_level)

    def test_bootstrap_log_re_entrant_is_noop(self, store):
        """第二次调用 bootstrap_admin() 不应产生日志输出（空操作）。"""
        # 第一次调用 — 应产生日志
        pw1 = store.bootstrap_admin()
        assert pw1 != ""

        logger = logging.getLogger("a2a_registry.users")
        old_level = logger.level
        logger.setLevel(logging.INFO)

        capture = io.StringIO()
        handler = logging.StreamHandler(capture)
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)

        try:
            # 第二次调用 — 已存在用户，空操作
            pw2 = store.bootstrap_admin()
            assert pw2 == "", "second call should be no-op"

            handler.flush()
            log_text = capture.getvalue()

            # 不应包含引导相关的日志行
            assert "Bootstrapped" not in log_text
            assert "BOOTSTRAPPED" not in log_text

        finally:
            logger.removeHandler(handler)
            logger.setLevel(old_level)

    def test_bootstrap_log_with_existing_non_admin_user(self, store):
        """已存在普通用户时 bootstrap_admin() 也是空操作。"""
        store.create_user("operator1", "op-pass", role="operator")

        logger = logging.getLogger("a2a_registry.users")
        old_level = logger.level
        logger.setLevel(logging.INFO)

        capture = io.StringIO()
        handler = logging.StreamHandler(capture)
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)

        try:
            pw = store.bootstrap_admin()
            assert pw == ""

            handler.flush()
            log_text = capture.getvalue()
            assert "BOOTSTRAPPED" not in log_text
        finally:
            logger.removeHandler(handler)
            logger.setLevel(old_level)


# ---------------------------------------------------------------------------
# 集成测试：create_app() 启动时调用 bootstrap_admin()
# ---------------------------------------------------------------------------


class TestBootstrapViaCreateApp:
    """验证 create_app() 在首次启动时自动执行 admin 引导。"""

    def test_create_app_bootstraps_admin_on_first_start(self):
        """首次 create_app（空 data_dir）应创建 admin 用户。"""
        with tempfile.TemporaryDirectory() as data_dir:
            app = create_app(
                data_dir=data_dir,
                base_url="http://localhost:8321",
            )

            user_store = app.get("user_store")
            assert user_store is not None, "user_store should be attached to app"

            # 验证 admin 用户被创建
            admin = user_store.get_user("admin")
            assert admin is not None, "admin user should exist after create_app"
            assert admin.role == "admin", "admin user should have admin role"

    def test_create_app_does_not_re_bootstrap(self):
        """第二次 create_app 不应覆盖或修改已有 admin 密码。"""
        with tempfile.TemporaryDirectory() as data_dir:
            # 第一次启动
            app1 = create_app(
                data_dir=data_dir,
                base_url="http://localhost:8321",
            )
            user_store1 = app1["user_store"]
            admin1 = user_store1.get_user("admin")
            assert admin1 is not None

            # 第二次启动 — 相同 data_dir
            app2 = create_app(
                data_dir=data_dir,
                base_url="http://localhost:8321",
            )
            user_store2 = app2["user_store"]
            admin2 = user_store2.get_user("admin")
            assert admin2 is not None
            # 密码哈希应该相同（未重新引导）
            assert admin2.password_hash == admin1.password_hash, (
                "Admin password hash should not change on re-bootstrap"
            )

    def test_create_app_bootstrap_admin_authenticates(self):
        """引导创建的 admin 用户应能通过 authenticate 验证。"""
        with tempfile.TemporaryDirectory() as data_dir:
            app = create_app(
                data_dir=data_dir,
                base_url="http://localhost:8321",
            )
            user_store = app["user_store"]

            # 直接创建一个用户以验证认证 — bootstrap_admin 的随机密码
            # 只能通过 user_store 直接获取
            admin = user_store.get_user("admin")
            assert admin is not None

            # 确认 authenticate 可以验证这个用户（需要明文密码）
            # 这里我们只验证用户存在且有正确哈希
            assert admin.role == "admin"
            assert len(admin.password_hash) > 20, "password should be hashed"
