"""单元测试：YAML 配置系统（P1-D）。

测试覆盖：
- 代码默认值
- YAML 配置文件加载
- 环境变量覆写（A2A_REGISTRY_*）
- 缺失配置项回落默认值
- 敏感字段掩码
- 优先级：CLI > env > file > default
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest
import yaml

from simple_a2a_registry.config import (
    Config,
    ServerConfig,
    DatabaseConfig,
    AuthConfig,
    LoggingConfig,
    OrchestrationConfig,
    MonitoringConfig,
    load_config,
    config_summary,
    SECRET_FIELDS,
    _dict_to_config,
    _apply_env_overrides,
    _coerce,
    _get_nested,
    _set_nested,
)


# =========================================================================
# Dataclass model defaults
# =========================================================================


class TestCodeDefaults:
    """所有配置项在没有文件/env 时使用代码默认值。"""

    def test_server_defaults(self):
        c = ServerConfig()
        assert c.host == "0.0.0.0"
        assert c.port == 8321
        assert c.cors_origins == "*"

    def test_database_defaults(self):
        c = DatabaseConfig()
        assert c.driver == "sqlite"
        assert "registry.db" in c.sqlite_path
        assert c.pool_size == 10
        assert c.max_overflow == 20

    def test_auth_defaults(self):
        c = AuthConfig()
        assert c.enabled is False
        assert c.bootstrap_secret == ""
        assert c.rsa_key_size == 2048
        assert c.jwk_ttl == 3600

    def test_logging_defaults(self):
        c = LoggingConfig()
        assert c.format == "text"
        assert c.level == "info"
        assert c.output == "stdout"

    def test_orchestration_defaults(self):
        c = OrchestrationConfig()
        assert c.dispatcher_enabled is True
        assert c.dispatcher_interval == 5
        assert c.claim_ttl == 900
        assert c.failure_limit == 3
        assert "workspaces" in c.workspaces_root
        assert "board.db" in c.board_path

    def test_monitoring_defaults(self):
        c = MonitoringConfig()
        assert c.metrics_enabled is True
        assert c.metrics_path == "/metrics"

    def test_top_level_defaults(self):
        c = Config()
        assert c.server.port == 8321
        assert c.database.driver == "sqlite"
        assert c.auth.enabled is False
        assert c.logging.level == "info"
        assert c.orchestration.dispatcher_interval == 5
        assert c.monitoring.metrics_path == "/metrics"


# =========================================================================
# YAML file loading
# =========================================================================


class TestYamlLoading:
    """从 YAML 文件加载配置项。"""

    def test_load_full_config(self):
        """加载完整的 YAML 配置，所有 section 生效。"""
        data = {
            "server": {"host": "127.0.0.1", "port": 9000},
            "database": {"driver": "mysql", "pool_size": 20},
            "auth": {"enabled": True, "rsa_key_size": 4096},
            "logging": {"format": "json", "level": "debug"},
            "orchestration": {"dispatcher_interval": 10, "failure_limit": 5},
            "monitoring": {"metrics_path": "/metrics"},
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.dump(data, f)
            path = f.name

        cfg = load_config(config_path=path)
        assert cfg.server.host == "127.0.0.1"
        assert cfg.server.port == 9000
        assert cfg.database.driver == "mysql"
        assert cfg.database.pool_size == 20
        assert cfg.auth.enabled is True
        assert cfg.auth.rsa_key_size == 4096
        assert cfg.logging.format == "json"
        assert cfg.logging.level == "debug"
        assert cfg.orchestration.dispatcher_interval == 10
        assert cfg.orchestration.failure_limit == 5
        assert cfg.monitoring.metrics_path == "/metrics"

        os.unlink(path)

    def test_partial_yaml_uses_defaults(self):
        """YAML 只设定部分字段时，其余使用代码默认值。"""
        data = {"server": {"port": 8888}}
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.dump(data, f)
            path = f.name

        cfg = load_config(config_path=path)
        assert cfg.server.port == 8888  # from YAML
        assert cfg.server.host == "0.0.0.0"  # from default
        assert cfg.database.driver == "sqlite"  # from default

        os.unlink(path)

    def test_unknown_yaml_keys_ignored(self):
        """YAML 中的未知 section/key 被静默忽略。"""
        data = {"server": {"host": "10.0.0.1"}, "nonexistent": {"foo": "bar"}}
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.dump(data, f)
            path = f.name

        cfg = load_config(config_path=path)
        assert cfg.server.host == "10.0.0.1"
        # Should not crash; nonexistent section ignored

        os.unlink(path)

    def test_missing_config_file_fallback(self):
        """指定不存在的配置文件路径不会报错，回退到默认值。"""
        cfg = load_config(config_path="/tmp/nonexistent_config_xyz.yaml")
        assert cfg.server.port == 8321

    def test_empty_config_file(self):
        """空 YAML 文件使用全默认值。"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("")
            path = f.name

        cfg = load_config(config_path=path)
        assert cfg.server.port == 8321
        assert cfg.database.driver == "sqlite"

        os.unlink(path)


# =========================================================================
# Environment variable overrides
# =========================================================================


class TestEnvOverrides:
    """A2A_REGISTRY_* 环境变量覆写配置。"""

    def _set_env(self, **kwargs):
        for k, v in kwargs.items():
            os.environ[k] = v

    def _clean_env(self):
        for k in list(os.environ.keys()):
            if k.startswith("A2A_REGISTRY_"):
                del os.environ[k]

    def test_simple_env_override(self):
        self._set_env(A2A_REGISTRY_SERVER__PORT="7777")
        cfg = load_config()
        assert cfg.server.port == 7777
        self._clean_env()

    def test_env_nested_path(self):
        self._set_env(A2A_REGISTRY_DATABASE__DRIVER="mysql")
        self._set_env(A2A_REGISTRY_AUTH__ENABLED="true")
        cfg = load_config()
        assert cfg.database.driver == "mysql"
        assert cfg.auth.enabled is True
        self._clean_env()

    def test_env_overrides_yaml(self):
        """环境变量优先级高于 YAML 文件。"""
        data = {"server": {"port": 8000}}
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.dump(data, f)
            path = f.name

        self._set_env(A2A_REGISTRY_SERVER__PORT="9999")
        cfg = load_config(config_path=path)
        assert cfg.server.port == 9999  # env wins over YAML

        os.unlink(path)
        self._clean_env()

    def test_env_bool_coercion(self):
        self._set_env(
            A2A_REGISTRY_AUTH__ENABLED="false",
            A2A_REGISTRY_ORCHESTRATION__DISPATCHER_ENABLED="true",
        )
        cfg = load_config()
        assert cfg.auth.enabled is False
        assert cfg.orchestration.dispatcher_enabled is True
        self._clean_env()

    def test_env_int_coercion(self):
        self._set_env(
            A2A_REGISTRY_SERVER__PORT="9000",
            A2A_REGISTRY_DATABASE__POOL_SIZE="25",
        )
        cfg = load_config()
        assert cfg.server.port == 9000
        assert cfg.database.pool_size == 25
        self._clean_env()

    def test_env_no_prefix_ignored(self):
        """不匹配 A2A_REGISTRY_ 前缀的 env var 被忽略。"""
        os.environ["PATH"] = "/usr/bin"  # should not crash
        cfg = load_config()
        assert cfg.server.port == 8321


# =========================================================================
# Secret field masking
# =========================================================================


class TestSecretMasking:
    """启动时配置输出中的敏感字段掩码。"""

    def test_secret_fields_set(self):
        assert "auth.bootstrap_secret" in SECRET_FIELDS
        assert "database.mysql_dsn" in SECRET_FIELDS

    def test_masks_bootstrap_secret(self):
        cfg = Config()
        cfg.auth.bootstrap_secret = "my-secret-key"
        summary = config_summary(cfg)
        assert "my-secret-key" not in summary
        assert "***" in summary

    def test_masks_mysql_dsn(self):
        cfg = Config()
        cfg.database.mysql_dsn = "mysql://user:pass@localhost:3306/db"
        summary = config_summary(cfg)
        assert "user:pass" not in summary
        assert "***" in summary

    def test_empty_secret_shows_empty(self):
        """空的敏感字段显示为空字符串而非 ***。"""
        cfg = Config()
        cfg.auth.bootstrap_secret = ""
        summary = config_summary(cfg)
        # When value is empty/falsy, should show "" not "***"
        assert "bootstrap_secret:" in summary

    def test_non_secret_fields_visible(self):
        """非敏感字段正常显示。"""
        cfg = Config()
        summary = config_summary(cfg)
        assert "host: 0.0.0.0" in summary or "host: '0.0.0.0'" in summary
        assert "port: 8321" in summary


# =========================================================================
# _coerce type conversion
# =========================================================================


class TestCoerce:
    def test_str(self):
        assert _coerce(123, str) == "123"

    def test_int(self):
        assert _coerce("42", int) == 42

    def test_bool_true_strings(self):
        assert _coerce("true", bool) is True
        assert _coerce("1", bool) is True
        assert _coerce("yes", bool) is True
        assert _coerce("on", bool) is True

    def test_bool_false_strings(self):
        assert _coerce("false", bool) is False
        assert _coerce("0", bool) is False
        assert _coerce("no", bool) is False
        assert _coerce("off", bool) is False

    def test_bool_passthrough(self):
        assert _coerce(True, bool) is True
        assert _coerce(False, bool) is False


# =========================================================================
# Priority chain
# =========================================================================


class TestPriority:
    """验证优先级链：CLI(测试用) > env > file > default。"""

    def test_env_over_file(self):
        """env > file"""
        data = {"server": {"port": 5000}}
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.dump(data, f)
            path = f.name

        os.environ["A2A_REGISTRY_SERVER__PORT"] = "6000"
        cfg = load_config(config_path=path)
        assert cfg.server.port == 6000

        os.unlink(path)
        del os.environ["A2A_REGISTRY_SERVER__PORT"]

    def test_file_over_default(self):
        """file > default"""
        data = {"server": {"port": 7000}}
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.dump(data, f)
            path = f.name

        cfg = load_config(config_path=path)
        assert cfg.server.port == 7000  # overrides code default 8321

        os.unlink(path)

    def test_default_fallback(self):
        """没有文件/env 时使用代码默认值。"""
        cfg = load_config()
        assert cfg.server.port == 8321
        assert cfg.server.host == "0.0.0.0"


# =========================================================================
# YAML edge cases
# =========================================================================


class TestYamlEdgeCases:
    """YAML 加载的边界情况。"""

    def test_yaml_null_values(self):
        """YAML 中 null/none 字段被转换为字符串 "None" 而不是默认值。"""
        data = {"server": {"host": None, "port": 8000}}
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.dump(data, f)
            path = f.name

        cfg = load_config(config_path=path)
        assert cfg.server.port == 8000
        # None → _coerce(None, str) → "None"
        assert cfg.server.host == "None"

        os.unlink(path)

    def test_yaml_empty_section(self):
        """YAML 中空 section 不产生配置变更。"""
        data = {"server": {}}
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.dump(data, f)
            path = f.name

        cfg = load_config(config_path=path)
        assert cfg.server.port == 8321
        assert cfg.server.host == "0.0.0.0"

        os.unlink(path)

    def test_yaml_deeply_nested_unknown(self):
        """YAML 深层未知路径被静默忽略。"""
        data = {"server": {"unknown_deep": {"a": {"b": 1}}}}
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.dump(data, f)
            path = f.name

        cfg = load_config(config_path=path)
        assert cfg.server.port == 8321  # no crash

        os.unlink(path)

    def test_yaml_zero_values(self):
        """YAML 中 0 / false 作为显式值。"""
        data = {
            "server": {"port": 0},
            "orchestration": {"dispatcher_enabled": False},
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.dump(data, f)
            path = f.name

        cfg = load_config(config_path=path)
        assert cfg.server.port == 0
        assert cfg.orchestration.dispatcher_enabled is False

        os.unlink(path)

    def test_yaml_negative_values(self):
        """YAML 中负数/负整数被接受。"""
        data = {"server": {"port": -1}}
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.dump(data, f)
            path = f.name

        cfg = load_config(config_path=path)
        assert cfg.server.port == -1

        os.unlink(path)

    def test_yaml_bool_values(self):
        """YAML 布尔值的各种表示。"""
        data = {
            "auth": {"enabled": True},
            "orchestration": {"dispatcher_enabled": False},
            "monitoring": {"metrics_enabled": True},
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.dump(data, f)
            path = f.name

        cfg = load_config(config_path=path)
        assert cfg.auth.enabled is True
        assert cfg.orchestration.dispatcher_enabled is False
        assert cfg.monitoring.metrics_enabled is True

        os.unlink(path)


# =========================================================================
# Config dict conversion
# =========================================================================


class TestDictToConfig:
    """_dict_to_config 辅助函数。"""

    def test_full_dict(self):
        d = {
            "server": {"host": "10.0.0.1", "port": 9000},
            "database": {"driver": "mysql"},
            "auth": {"enabled": True},
            "logging": {"level": "debug"},
            "orchestration": {"dispatcher_interval": 10},
            "monitoring": {"metrics_path": "/custom_metrics"},
        }
        cfg = _dict_to_config(d, Config())
        assert cfg.server.host == "10.0.0.1"
        assert cfg.server.port == 9000
        assert cfg.database.driver == "mysql"
        assert cfg.auth.enabled is True
        assert cfg.logging.level == "debug"
        assert cfg.orchestration.dispatcher_interval == 10
        assert cfg.monitoring.metrics_path == "/custom_metrics"

    def test_empty_dict(self):
        cfg = _dict_to_config({}, Config())
        assert cfg.server.port == 8321  # defaults preserved
        assert cfg.database.driver == "sqlite"

    def test_partial_dict(self):
        cfg = _dict_to_config({"server": {"port": 7777}}, Config())
        assert cfg.server.port == 7777
        assert cfg.server.host == "0.0.0.0"

    def test_unknown_section_ignored(self):
        cfg = _dict_to_config({"imaginary": {"key": "val"}}, Config())
        assert cfg.server.port == 8321  # no crash


# =========================================================================
# _get_nested / _set_nested helpers
# =========================================================================


class TestNestedHelpers:
    """配置系统内部辅助函数。"""

    def test_get_nested_simple(self):
        c = Config()
        c.server.port = 9000
        assert _get_nested(c, ["server", "port"]) == 9000

    def test_get_nested_section(self):
        c = Config()
        assert _get_nested(c, ["server"]) is c.server

    def test_get_nested_missing(self):
        c = Config()
        assert _get_nested(c, ["server", "nonexistent"]) is None

    def test_set_nested_simple(self):
        c = Config()
        assert _set_nested(c, ["server", "port"], 9000) is True
        assert c.server.port == 9000

    def test_set_nested_overwrite(self):
        c = Config()
        c.server.port = 5000
        assert _set_nested(c, ["server", "port"], 9999) is True
        assert c.server.port == 9999

    def test_set_nested_invalid_path(self):
        c = Config()
        assert _set_nested(c, ["imaginary", "field"], "val") is False


# =========================================================================
# Coerce edge cases
# =========================================================================


class TestCoerceEdgeCases:
    """_coerce 边界情况。"""

    def test_int_via_float_string_raises(self):
        with pytest.raises(ValueError):
            _coerce("3.14", int)

    def test_bool_mixed_case(self):
        assert _coerce("True", bool) is True
        assert _coerce("FALSE", bool) is False

    def test_bool_weird_string(self):
        assert _coerce("maybe", bool) is False  # unparseable → False

    def test_str_already_str(self):
        assert _coerce("hello", str) == "hello"

    def test_none_to_str(self):
        assert _coerce(None, str) == "None"

    def test_none_to_int_raises(self):
        with pytest.raises((TypeError, ValueError)):
            _coerce(None, int)