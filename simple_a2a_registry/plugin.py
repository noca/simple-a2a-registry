"""Plugin system for Simple A2A Registry — ABC interface + PluginRegistry.

Provides a hook-based plugin architecture that lets third-party code
extend the Registry at well-defined lifecycle points without modifying
core logic.  Plugins are loaded at startup from one or both of:

  - ``pyproject.toml`` entry_points (``simple_a2a_registry.plugins``)
  - ``config.yaml`` ``plugins`` section (explicit path or module name)

Each hook is a no-op by default so plugins implement only what they need.
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import sys
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Type

from aiohttp import web

logger = logging.getLogger("a2a_registry.plugin")

# ---------------------------------------------------------------------------
# Plugin metadata
# ---------------------------------------------------------------------------


@dataclass
class PluginInfo:
    """Runtime metadata for a loaded plugin instance."""

    name: str                          # unique plugin name / identifier
    version: str = "0.1.0"             # plugin version (semver)
    description: str = ""               # one-line description
    module: str = ""                    # Python module path (for diagnostics)
    loaded_at: float = 0.0             # timestamp of load()
    config: Dict[str, Any] = field(default_factory=dict)  # section-specific config


# ---------------------------------------------------------------------------
# Hook registry — typed signature per hook
# ---------------------------------------------------------------------------

# Each hook is a function pointer so the registry can iterate quickly.
# HookType = actual callable signature (enforced at registration time).
# We use ``Any`` in the dict for simplicity since Python doesn't have
# true union types for callable signatures at runtime.

HookFunc = Callable[..., Any]

# ---------------------------------------------------------------------------
# Base plugin class (ABC)
# ---------------------------------------------------------------------------


class Plugin(ABC):
    """Abstract base class for all A2A Registry plugins.

    All hooks have default no-op implementations.  Subclass and override
    only the hooks you need.

    Lifecycle hooks (called in order)::

        load(config)  →  init(app)  →  [serve]  →  before_shutdown(app)

    Request hooks (called per-request)::

        before_request(request)  →  [handler]  →  after_request(request, response)

    Event hooks (called asynchronously when events occur)::

        on_agent_register(agent_id, card)
        on_agent_deregister(agent_id)
        on_agent_heartbeat(agent_id)
        on_task_created(task_id, task_data)
        on_task_completed(task_id, result)
        on_token_issued(client_id, token)
        on_server_start(app)
        on_server_stop(app)
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique plugin name — used for config lookup and diagnostics."""
        ...

    @property
    def version(self) -> str:
        """Plugin version string (default: ``\"0.1.0\"``)."""
        return "0.1.0"

    @property
    def description(self) -> str:
        """One-line description of what this plugin does."""
        return ""

    # ------------------------------------------------------------------
    # Lifecycle hooks
    # ------------------------------------------------------------------

    def load(self, config: Dict[str, Any]) -> None:
        """Called once when the plugin is first loaded.

        *config* is the plugin's own section from the ``plugins`` config
        block (could be empty dict ``{}``).  Use this to validate config,
        set up internal state, connect external services, etc.

        If this raises, the plugin is marked as failed and skipped during
        ``init()``.
        """

    async def init(self, app: web.Application) -> None:
        """Called after the aiohttp ``Application`` is fully configured
        but before the server starts listening.

        Use this to register additional routes, add middleware, start
        background tasks, or attach state to ``app[...]``.
        """

    async def before_shutdown(self, app: web.Application) -> None:
        """Called during graceful shutdown, before connection draining.

        Use this to flush buffers, close external connections, and
        release resources.  The server will wait up to a configurable
        timeout (default 5 s) for all plugins' ``before_shutdown``
        methods to complete.
        """

    # ------------------------------------------------------------------
    # Request hooks (synchronous-style — called from middleware)
    # ------------------------------------------------------------------

    async def before_request(self, request: web.Request) -> Optional[web.Response]:
        """Called **before** every HTTP request.

        Return ``None`` (or omit) to let the request proceed normally.
        Return a ``web.Response`` to short-circuit the request (e.g. for
        rate limiting, IP blocking, request validation).

        Caught exceptions are logged and the request continues — a broken
        plugin should not DOS the server.
        """
        return None

    async def after_request(
        self,
        request: web.Request,
        response: web.StreamResponse,
    ) -> web.StreamResponse:
        """Called **after** every HTTP request.

        May modify or wrap the response (e.g. add headers, inject metrics).

        The returned ``StreamResponse`` replaces the original response.
        Default implementation returns *response* unchanged.
        """
        return response

    # ------------------------------------------------------------------
    # Event hooks (fire-and-forget — called from core handlers)
    # ------------------------------------------------------------------

    async def on_server_start(self, app: web.Application) -> None:
        """Fired after the server starts accepting connections."""

    async def on_server_stop(self, app: web.Application) -> None:
        """Fired when the server begins graceful shutdown."""

    async def on_agent_register(self, agent_id: str, card: Dict[str, Any]) -> None:
        """Fired after an agent is registered."""

    async def on_agent_deregister(self, agent_id: str) -> None:
        """Fired after an agent is unregistered."""

    async def on_agent_heartbeat(self, agent_id: str) -> None:
        """Fired after an agent's heartbeat is processed."""

    async def on_task_created(self, task_id: str, task_data: Dict[str, Any]) -> None:
        """Fired when a new task is created in the in-memory registry."""

    async def on_task_completed(self, task_id: str, result: Optional[Dict[str, Any]]) -> None:
        """Fired when a task completes or fails."""

    async def on_token_issued(self, client_id: str, token: str) -> None:
        """Fired after an OAuth token is issued to a client."""


# ---------------------------------------------------------------------------
# PluginRegistry — loads, manages, and dispatches hooks to plugins
# ---------------------------------------------------------------------------


class PluginRegistry:
    """Central registry of loaded plugins.

    Usage::

        registry = PluginRegistry()
        registry.discover_entry_points()      # from pyproject.toml
        registry.load_config_section({...})   # from config.yaml
        await registry.fire_init(app)

        # Inside a middleware:
        resp = await registry.fire_before_request(request)
        if resp:
            return resp
        response = await handler(request)
        response = await registry.fire_after_request(request, response)
        return response
    """

    def __init__(self) -> None:
        self._plugins: Dict[str, Plugin] = {}
        self._infos: Dict[str, PluginInfo] = {}
        self._failures: Dict[str, str] = {}  # plugin_name → error_message

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def plugins(self) -> Dict[str, Plugin]:
        """Dict of successfully loaded plugins (name → instance)."""
        return dict(self._plugins)

    @property
    def infos(self) -> Dict[str, PluginInfo]:
        """Dict of plugin runtime metadata (name → PluginInfo)."""
        return dict(self._infos)

    @property
    def failures(self) -> Dict[str, str]:
        """Dict of plugins that failed to load (name → error)."""
        return dict(self._failures)

    def is_loaded(self, name: str) -> bool:
        """Check if a plugin is successfully loaded."""
        return name in self._plugins

    def get_plugin(self, name: str) -> Optional[Plugin]:
        """Get a loaded plugin by name, or ``None``."""
        return self._plugins.get(name)

    def get_info(self, name: str) -> Optional[PluginInfo]:
        """Get plugin metadata by name, or ``None``."""
        return self._infos.get(name)

    # ------------------------------------------------------------------
    # Registration (low-level)
    # ------------------------------------------------------------------

    def register(self, plugin: Plugin, config: Optional[Dict[str, Any]] = None) -> None:
        """Register a :class:`Plugin` instance and call its ``load()`` hook.

        Args:
            plugin: Plugin instance.
            config: Optional per-plugin config dict.  If ``None``, an
                empty dict is passed to ``load()``.

        Raises:
            ValueError: If a plugin with the same name is already registered.
        """
        name = plugin.name
        if name in self._plugins:
            raise ValueError(f"Plugin '{name}' is already registered")

        cfg = config or {}
        try:
            plugin.load(cfg)
        except Exception as exc:
            self._failures[name] = str(exc)
            logger.error("Plugin '%s' failed during load(): %s", name, exc)
            return  # don't register

        self._plugins[name] = plugin
        self._infos[name] = PluginInfo(
            name=name,
            version=plugin.version,
            description=plugin.description,
            module=type(plugin).__module__,
            loaded_at=time.time(),
            config=cfg,
        )
        logger.info("Plugin '%s' v%s loaded — %s", name, plugin.version, plugin.description)

    # ------------------------------------------------------------------
    # Discovery — entry_points (setuptools)
    # ------------------------------------------------------------------

    def discover_entry_points(self, group: str = "simple_a2a_registry.plugins") -> int:
        """Discover and load plugins declared in installed packages' entry_points.

        Args:
            group: The entry point group name (default: ``simple_a2a_registry.plugins``).

        Returns:
            Number of plugins loaded (excluding failures).
        """
        try:
            from importlib.metadata import entry_points  # Python ≥3.10
        except ImportError:
            logger.warning("entry_points discovery requires Python ≥3.10")
            return 0

        eps = entry_points(group=group)
        count = 0
        for ep in eps:
            name = ep.name
            try:
                cls: Type[Plugin] = ep.load()
                plugin = cls()
            except Exception as exc:
                self._failures[name] = f"entry_point load failed: {exc}"
                logger.error("Plugin '%s' entry_point load failed: %s", name, exc)
                continue

            try:
                self.register(plugin)
                count += 1
            except ValueError:
                logger.warning("Plugin '%s' skipped (already registered)", name)

        if eps:
            logger.info("Entry-point discovery: loaded %d / %d plugin(s)", count, len(eps))
        return count

    # ------------------------------------------------------------------
    # Discovery — config section
    # ------------------------------------------------------------------

    def load_config_section(self, plugins_config: Dict[str, Any]) -> int:
        """Load plugins declared in the ``plugins`` section of ``config.yaml``.

        Expected format::

            plugins:
              my-plugin:
                module: my_package.my_plugin
                config:
                  key: value

              another-plugin:
                path: /opt/my_registry_plugins/another.py
                config: {}

        Each entry must have **either** ``module`` (dotted Python module
        path) **or** ``path`` (filesystem path to a Python file).  The
        ``config`` sub-dict is passed to ``load()``.

        Args:
            plugins_config: Dict from the ``plugins`` section of config.

        Returns:
            Number of plugins loaded (excluding failures).
        """
        count = 0
        for name, entry in (plugins_config or {}).items():
            if not isinstance(entry, dict):
                logger.warning("Plugin '%s' config entry is not a dict — skipping", name)
                continue

            module_path = entry.get("module", "")
            file_path = entry.get("path", "")
            cfg = entry.get("config", {})

            if not module_path and not file_path:
                logger.warning("Plugin '%s' has neither 'module' nor 'path' — skipping", name)
                continue
            if module_path and file_path:
                logger.warning("Plugin '%s' has both 'module' and 'path' — using 'module'", name)

            try:
                if module_path:
                    mod = importlib.import_module(module_path)
                elif file_path:
                    spec = importlib.util.spec_from_file_location(name, file_path)
                    if spec is None:
                        raise ImportError(f"Cannot load spec from {file_path}")
                    mod = importlib.util.module_from_spec(spec)
                    sys.modules[name] = mod
                    spec.loader.exec_module(mod)  # type: ignore[union-attr]

                # Find plugin class: look for a subclass of Plugin
                plugin_cls: Optional[Type[Plugin]] = None
                for attr_name in dir(mod):
                    attr = getattr(mod, attr_name)
                    if isinstance(attr, type) and issubclass(attr, Plugin) and attr is not Plugin:
                        plugin_cls = attr
                        break

                if plugin_cls is None:
                    raise ValueError(f"No Plugin subclass found in '{module_path or file_path}'")

                plugin = plugin_cls()
            except Exception as exc:
                self._failures[name] = str(exc)
                logger.error("Plugin '%s' failed to load from config: %s", name, exc)
                continue

            try:
                self.register(plugin, cfg)
                count += 1
            except ValueError:
                logger.warning("Plugin '%s' skipped (already registered)", name)

        return count

    # ------------------------------------------------------------------
    # Hook dispatch — lifecycle
    # ------------------------------------------------------------------

    async def fire_init(self, app: web.Application) -> None:
        """Call ``init(app)`` on all registered plugins, in registration order.

        Failures are logged but do not stop subsequent plugins.
        """
        for name, plugin in self._plugins.items():
            try:
                await plugin.init(app)
            except Exception as exc:
                logger.exception("Plugin '%s' init() failed: %s", name, exc)

    async def fire_before_shutdown(self, app: web.Application) -> None:
        """Call ``before_shutdown(app)`` on all registered plugins.

        Each plugin gets a timeout of 5 s.  Slow plugins are logged but
        do not block the shutdown.
        """
        import asyncio

        for name, plugin in self._plugins.items():
            try:
                await asyncio.wait_for(
                    plugin.before_shutdown(app),
                    timeout=5.0,
                )
            except asyncio.TimeoutError:
                logger.warning("Plugin '%s' before_shutdown() timed out after 5s", name)
            except Exception as exc:
                logger.exception("Plugin '%s' before_shutdown() failed: %s", name, exc)

    # ------------------------------------------------------------------
    # Hook dispatch — requests (called from middleware)
    # ------------------------------------------------------------------

    async def fire_before_request(self, request: web.Request) -> Optional[web.Response]:
        """Iterate plugins' ``before_request()`` in registration order.

        Returns the first non-``None`` response (short-circuits remaining
        plugins and the request handler).
        """
        for name, plugin in self._plugins.items():
            try:
                resp = await plugin.before_request(request)
                if resp is not None:
                    return resp
            except Exception as exc:
                logger.exception("Plugin '%s' before_request() error: %s", name, exc)
        return None

    async def fire_after_request(
        self,
        request: web.Request,
        response: web.StreamResponse,
    ) -> web.StreamResponse:
        """Iterate plugins' ``after_request()`` in registration order.

        Each plugin receives the (possibly modified) response from the
        previous plugin.
        """
        current = response
        for name, plugin in self._plugins.items():
            try:
                current = await plugin.after_request(request, current)
            except Exception as exc:
                logger.exception("Plugin '%s' after_request() error: %s", name, exc)
        return current

    # ------------------------------------------------------------------
    # Hook dispatch — events
    # ------------------------------------------------------------------

    async def fire_agent_register(self, agent_id: str, card: Dict[str, Any]) -> None:
        """Fire ``on_agent_register`` to all plugins (fire-and-forget)."""
        for name, plugin in self._plugins.items():
            try:
                await plugin.on_agent_register(agent_id, card)
            except Exception as exc:
                logger.exception("Plugin '%s' on_agent_register() error: %s", name, exc)

    async def fire_agent_deregister(self, agent_id: str) -> None:
        """Fire ``on_agent_deregister`` to all plugins (fire-and-forget)."""
        for name, plugin in self._plugins.items():
            try:
                await plugin.on_agent_deregister(agent_id)
            except Exception as exc:
                logger.exception("Plugin '%s' on_agent_deregister() error: %s", name, exc)

    async def fire_agent_heartbeat(self, agent_id: str) -> None:
        """Fire ``on_agent_heartbeat`` to all plugins (fire-and-forget)."""
        for name, plugin in self._plugins.items():
            try:
                await plugin.on_agent_heartbeat(agent_id)
            except Exception as exc:
                logger.exception("Plugin '%s' on_agent_heartbeat() error: %s", name, exc)

    async def fire_task_created(self, task_id: str, task_data: Dict[str, Any]) -> None:
        """Fire ``on_task_created`` to all plugins (fire-and-forget)."""
        for name, plugin in self._plugins.items():
            try:
                await plugin.on_task_created(task_id, task_data)
            except Exception as exc:
                logger.exception("Plugin '%s' on_task_created() error: %s", name, exc)

    async def fire_task_completed(self, task_id: str, result: Optional[Dict[str, Any]]) -> None:
        """Fire ``on_task_completed`` to all plugins (fire-and-forget)."""
        for name, plugin in self._plugins.items():
            try:
                await plugin.on_task_completed(task_id, result)
            except Exception as exc:
                logger.exception("Plugin '%s' on_task_completed() error: %s", name, exc)

    async def fire_token_issued(self, client_id: str, token: str) -> None:
        """Fire ``on_token_issued`` to all plugins (fire-and-forget)."""
        for name, plugin in self._plugins.items():
            try:
                await plugin.on_token_issued(client_id, token)
            except Exception as exc:
                logger.exception("Plugin '%s' on_token_issued() error: %s", name, exc)

    async def fire_server_start(self, app: web.Application) -> None:
        """Fire ``on_server_start`` to all plugins."""
        for name, plugin in self._plugins.items():
            try:
                await plugin.on_server_start(app)
            except Exception as exc:
                logger.exception("Plugin '%s' on_server_start() error: %s", name, exc)

    async def fire_server_stop(self, app: web.Application) -> None:
        """Fire ``on_server_stop`` to all plugins."""
        for name, plugin in self._plugins.items():
            try:
                await plugin.on_server_stop(app)
            except Exception as exc:
                logger.exception("Plugin '%s' on_server_stop() error: %s", name, exc)

    # ------------------------------------------------------------------
    # Convenience: summary
    # ------------------------------------------------------------------

    def summary(self) -> str:
        """Return a human-readable summary of loaded plugins and failures."""
        lines: List[str] = []
        lines.append(f"Plugins: {len(self._plugins)} loaded, {len(self._failures)} failed")
        if self._plugins:
            lines.append("Loaded:")
            for name, info in sorted(self._infos.items()):
                lines.append(f"  {name} v{info.version} — {info.description}")
        if self._failures:
            lines.append("Failed:")
            for name, err in self._failures.items():
                lines.append(f"  {name}: {err}")
        return "\n".join(lines)

    def __len__(self) -> int:
        return len(self._plugins)

    def __contains__(self, name: str) -> bool:
        return name in self._plugins