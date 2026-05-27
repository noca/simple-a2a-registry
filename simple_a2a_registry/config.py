"""YAML-based configuration system for Simple A2A Registry.

Priority (highest → lowest):
  1. CLI arguments
  2. Environment variables  (A2A_REGISTRY_ prefix, __ for nesting)
  3. YAML config file       (~/.simple-a2a-registry/config.yaml)
  4. Code defaults

Sensitive fields (auth.bootstrap_secret, database.mysql_dsn) are
auto-masked with ``***`` when printing the config summary at startup.
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass, field, fields, asdict
from pathlib import Path
from typing import Any, Dict, Optional, Set

import yaml

logger = logging.getLogger(__name__)


def _log(lvl: str, msg: str, *a: Any) -> None:
    """Log a message via the standard library logging."""
    log_fn = getattr(logger, lvl, None)
    if log_fn:
        if a:
            log_fn(msg % a)
        else:
            log_fn(msg)
    else:
        print(f"[{lvl.upper()}] {msg % a if a else msg}", file=sys.stderr)

# ---------------------------------------------------------------------------
# Nested data-classes mirroring the YAML sections
# ---------------------------------------------------------------------------


@dataclass
class ServerConfig:
    host: str = "0.0.0.0"
    port: int = 8321
    cors_origins: str = "*"


@dataclass
class DatabaseConfig:
    driver: str = "sqlite"  # sqlite | mysql
    sqlite_path: str = "~/.simple-a2a-registry/registry.db"
    mysql_dsn: str = ""
    pool_size: int = 10
    max_overflow: int = 20


@dataclass
class AuthConfig:
    enabled: bool = False
    bootstrap_secret: str = ""
    rsa_key_size: int = 2048
    jwk_ttl: int = 3600


@dataclass
class LoggingConfig:
    format: str = "text"  # json | text
    level: str = "info"
    output: str = "stdout"


@dataclass
class OrchestrationConfig:
    dispatcher_enabled: bool = True
    dispatcher_interval: int = 5
    claim_ttl: int = 900
    failure_limit: int = 3
    workspaces_root: str = "~/.simple-a2a-registry/workspaces"
    board_path: str = "~/.simple-a2a-registry/board.db"


@dataclass
class MonitoringConfig:
    metrics_enabled: bool = True
    metrics_path: str = "/metrics"


@dataclass
class RateLimitConfig:
    enabled: bool = False
    default_unauthenticated: int = 60
    default_authenticated: int = 300
    storage: str = "memory"
    whitelist: list = field(default_factory=list)


@dataclass
class AuditConfig:
    retention_days: int = 90


@dataclass
class Config:
    server: ServerConfig = field(default_factory=ServerConfig)
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    auth: AuthConfig = field(default_factory=AuthConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    orchestration: OrchestrationConfig = field(default_factory=OrchestrationConfig)
    monitoring: MonitoringConfig = field(default_factory=MonitoringConfig)
    rate_limit: RateLimitConfig = field(default_factory=RateLimitConfig)
    audit: AuditConfig = field(default_factory=AuditConfig)


# ---------------------------------------------------------------------------
# Sensitive field paths (dotted, e.g. "auth.bootstrap_secret")
# ---------------------------------------------------------------------------

SECRET_FIELDS: Set[str] = {
    "auth.bootstrap_secret",
    "database.mysql_dsn",
}


# ---------------------------------------------------------------------------
# Loading helpers
# ---------------------------------------------------------------------------


def _resolve_path(p: str) -> str:
    """Expand ``~`` / ``$HOME`` and return a real filesystem path."""
    return str(Path(p).expanduser().resolve())


def _env_var_to_path(key: str) -> list[str]:
    """Convert ``A2A_REGISTRY_DATABASE__DRIVER`` → ``['database', 'driver']``."""
    stripped = key[len("A2A_REGISTRY_"):]
    return stripped.lower().split("__")


def _get_nested(cfg: Config, path: list[str]) -> Any:
    """Traverse a Config dataclass tree by dotted path segments."""
    obj: Any = cfg
    for segment in path:
        try:
            obj = getattr(obj, segment)
        except AttributeError:
            return None
    return obj


def _set_nested(cfg: Config, path: list[str], value: Any) -> bool:
    """Set a value on a Config dataclass tree by dotted path segments.

    Coerces the value to match the target field's declared type.
    Returns ``True`` on success, ``False`` if the path is invalid.
    """
    obj: Any = cfg
    for i, segment in enumerate(path[:-1]):
        try:
            obj = getattr(obj, segment)
        except AttributeError:
            return False

    field_name = path[-1]
    target_field = next(
        (f for f in fields(obj) if f.name == field_name), None
    )
    if target_field is None:
        return False

    typed_val = _coerce(value, target_field.type)
    setattr(obj, field_name, typed_val)
    return True


def _coerce(value: Any, target_type: Any) -> Any:
    """Coerce a raw string/config value to the target type."""
    # dataclass fields may have type as str (forward ref) or actual type
    if isinstance(target_type, str):
        target_type = {"bool": bool, "int": int, "str": str, "float": float}.get(target_type, str)
    if target_type is bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.lower() in ("1", "true", "yes", "on")
        return bool(value)
    if target_type is int:
        return int(value)
    if target_type is str:
        return str(value)
    return value


def _dict_to_config(d: Dict[str, Any], cfg: Config) -> Config:
    """Merge a flat-or-nested dict into an existing Config instance.

    Only known sections/fields are accepted; unknown keys are silently ignored.
    """
    section_map = {
        "server": cfg.server,
        "database": cfg.database,
        "auth": cfg.auth,
        "logging": cfg.logging,
        "orchestration": cfg.orchestration,
        "monitoring": cfg.monitoring,
        "rate_limit": cfg.rate_limit,
        "audit": cfg.audit,
    }

    for section_name, section_data in d.items():
        if section_name not in section_map or not isinstance(section_data, dict):
            continue
        section_obj = section_map[section_name]
        section_fields = {f.name for f in fields(section_obj)}
        for key, value in section_data.items():
            if key in section_fields:
                setattr(section_obj, key, _coerce(value, type(getattr(section_obj, key))))
    return cfg


def _apply_env_overrides(cfg: Config) -> Config:
    """Override config values from ``A2A_REGISTRY_*`` environment variables.

    Double underscore ``__`` separates nesting levels, e.g.::

        A2A_REGISTRY_DATABASE__DRIVER=mysql
        A2A_REGISTRY_AUTH__ENABLED=true
        A2A_REGISTRY_SERVER__PORT=9000
    """
    prefix = "A2A_REGISTRY_"
    for env_key, env_val in sorted(os.environ.items()):
        if not env_key.startswith(prefix):
            continue
        path = _env_var_to_path(env_key)
        if len(path) < 2:
            continue  # need at least section + field
        _set_nested(cfg, path, env_val)
    return cfg


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def load_config(config_path: Optional[str] = None) -> Config:
    """Load configuration from file + env, merged over code defaults.

    Resolution order:
      1. Code defaults (already baked into ``Config()``).
      2. YAML config file — if *config_path* is given, tries that path;
         otherwise auto-checks ``~/.simple-a2a-registry/config.yaml``.
      3. Environment variables (``A2A_REGISTRY_*``).

    Args:
        config_path: Explicit config file path.  ``None`` = auto-detect.

    Returns:
        Populated :class:`Config` instance.
    """
    cfg = Config()

    # --- Step 1: YAML config file ---
    resolved_path: Optional[str] = None
    if config_path is not None:
        resolved_path = _resolve_path(config_path)
    else:
        auto = _resolve_path("~/.simple-a2a-registry/config.yaml")
        if os.path.isfile(auto):
            resolved_path = auto

    if resolved_path and os.path.isfile(resolved_path):
        try:
            with open(resolved_path, "r") as f:
                raw: Dict[str, Any] = yaml.safe_load(f) or {}
            _dict_to_config(raw, cfg)
            _log("info", "Loaded config from %s", resolved_path)
        except Exception as exc:
            _log("warning", "Failed to load config from %s: %s", resolved_path, exc)

    # --- Step 2: Environment variable overrides ---
    _apply_env_overrides(cfg)

    return cfg


def config_summary(cfg: Config) -> str:
    """Return a human-readable YAML-ish summary of the active configuration.

    Sensitive fields (``SECRET_FIELDS``) are masked with ``***``.
    """
    d = asdict(cfg)

    def _mask(obj: Any, prefix: str = "") -> Any:
        if isinstance(obj, dict):
            result: Dict[str, Any] = {}
            for k, v in obj.items():
                dotted = f"{prefix}.{k}" if prefix else k
                if dotted in SECRET_FIELDS:
                    result[k] = "***" if v else ""
                else:
                    result[k] = _mask(v, dotted)
            return result
        return obj

    masked = _mask(d)
    return yaml.dump(masked, default_flow_style=False, sort_keys=False).strip()