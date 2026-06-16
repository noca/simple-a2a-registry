"""A2A Registry Python SDK — synchronous and asynchronous client.

Provides the :class:`A2AClient` class that encapsulates all interactions
between an Agent and the A2A Registry server:

- **OAuth 2.1** token management (auto-acquire, auto-refresh 30 s before expiry)
- **Agent lifecycle**: register, deregister, heartbeat
- **WebSocket** persistent connection with automatic reconnect and exponential backoff
- **Task dispatch handling**: callback for incoming pushed tasks
- **Task reporting**: report_result() / report_progress() via WebSocket
- **Task polling**: poll task status and results via HTTP
- **Dual-mode**: synchronous (``requests``) and asynchronous (``aiohttp``)

Usage:
    .. code-block:: python

        from simple_a2a_registry.client import A2AClient

        client = A2AClient(
            registry_url="http://localhost:8321",
            client_id="my-agent",
            client_secret="secret-xxx",
        )
        agent_id = client.register_agent(
            name="My Agent",
            description="A useful agent",
            agent_card={...},
        )
        client.heartbeat(agent_id)

        # Set up dispatch callback
        def on_task(task: dict) -> None:
            print(f"Received task: {task['id']}")
            client.report_result(task["id"], {"text": "done"})

        client.dispatch_handler = on_task
        client.connect_websocket(agent_id)

        # Poll
        result = client.task_poll(task_id)

    Async mode:
    .. code-block:: python

        async with A2AClient(...) as client:
            agent_id = await client.async_register_agent(...)
            await client.async_connect_websocket(agent_id)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Optional,
    TypeVar,
    Union,
)

logger = logging.getLogger("a2a_registry.client")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TOKEN_REFRESH_MARGIN = 30  # seconds before expiry to refresh token
DEFAULT_TIMEOUT = 30  # seconds for HTTP requests
WS_PING_INTERVAL = 30  # seconds between WebSocket ping messages
WS_RECONNECT_BASE = 1.0  # initial backoff delay in seconds
WS_RECONNECT_MAX = 60.0  # maximum backoff delay in seconds
WS_RECONNECT_FACTOR = 2.0  # exponential backoff multiplier

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

DispatchCallback = Callable[[Dict[str, Any]], Any]
"""Callback type for incoming WS-dispatched tasks.

Receives a single dict containing the task payload, e.g.::

    {
        "type": "task",
        "id": "uuid-...",
        "query": "...",
        "sessionId": "..."
    }
"""

_T = TypeVar("_T")


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class A2AClientError(Exception):
    """Base exception for A2A client errors."""


class RegistryError(A2AClientError):
    """The Registry returned a non-success HTTP status.

    Attributes:
        status:   HTTP status code.
        body:     Parsed response body (dict or raw text).
        error:    Error code from the Registry, if available.
        detail:   Human-readable detail from the Registry, if available.
    """

    def __init__(
        self,
        status: int,
        body: Any,
        error: str = "",
        detail: str = "",
    ) -> None:
        self.status = status
        self.body = body
        self.error = error
        self.detail = detail
        msg = detail or error or f"HTTP {status}"
        super().__init__(msg)


class AuthError(RegistryError):
    """Authentication or authorisation failure (401/403)."""


class NotFoundError(RegistryError):
    """Resource not found (404)."""


class ConnectionError(A2AClientError):
    """WebSocket or HTTP connection failure."""


# ---------------------------------------------------------------------------
# OAuth Token Management
# ---------------------------------------------------------------------------


@dataclass
class OAuthToken:
    """An OAuth 2.1 access token with expiry tracking.

    Attributes:
        access_token:  The JWT access token string.
        token_type:    Token type, typically ``"Bearer"``.
        expires_in:    Lifetime in seconds from issuance.
        scope:         Space-separated scope string, if returned.
        acquired_at:   Unix timestamp when this token was acquired.
    """

    access_token: str
    token_type: str = "Bearer"
    expires_in: int = 3600
    scope: str = ""
    acquired_at: float = field(default_factory=time.time)

    @property
    def is_expired(self) -> bool:
        """Check if the token is expired or within the grace margin."""
        return time.time() >= self.acquired_at + self.expires_in - TOKEN_REFRESH_MARGIN


# ---------------------------------------------------------------------------
# A2AClient — Core SDK
# ---------------------------------------------------------------------------


class A2AClient:
    """Client SDK for the A2A Registry.

    Encapsulates all interactions between an Agent and the Registry,
    supporting both synchronous (``requests``) and asynchronous
    (``aiohttp``) modes.

    **Auth Modes:**

    - **OAuth 2.1** (default when ``client_id`` and ``client_secret`` are
      provided): tokens are auto-acquired via the ``client_credentials`` grant
      and auto-refreshed 30 seconds before expiry.
    - **No auth**: pass ``auth_enabled=False`` to skip authentication entirely
      (useful for development or when the Registry runs without ``--auth-enabled``).

    Args:
        registry_url:   Base URL of the A2A Registry, e.g. ``http://localhost:8321``.
        client_id:      OAuth client ID. Required when ``auth_enabled=True``.
        client_secret:  OAuth client secret. Required when ``auth_enabled=True``.
        auth_enabled:   Whether to use OAuth authentication. Defaults to
                        ``True`` if both ``client_id`` and ``client_secret``
                        are provided, ``False`` otherwise.
        scope:          Space-separated scope string for token requests.
                        Default: ``"task:read task:write agent:read agent:register"``.
        timeout:        Default HTTP request timeout in seconds.
        verify_ssl:     Whether to verify SSL certificates. Default ``True``.
        user_agent:     Custom User-Agent header value.

    Attributes:
        dispatch_handler: Callback invoked when a task is received via
                          WebSocket. Set this to a callable that accepts a
                          single dict argument before calling
                          :meth:`connect_websocket`.
    """

    def __init__(
        self,
        registry_url: str = "http://localhost:8321",
        client_id: str = "",
        client_secret: str = "",
        *,
        tenant: str = "",
        auth_enabled: Optional[bool] = None,
        scope: str = "task:read task:write agent:read agent:register",
        timeout: int = DEFAULT_TIMEOUT,
        verify_ssl: bool = True,
        user_agent: str = "a2a-client-sdk/1.0",
    ) -> None:
        self._base_url = registry_url.rstrip("/")
        self._client_id = client_id
        self._client_secret = client_secret
        self._tenant = tenant
        self._timeout = timeout
        self._verify_ssl = verify_ssl
        self._user_agent = user_agent
        self._scope = scope

        # Resolve auth mode
        if auth_enabled is None:
            auth_enabled = bool(client_id and client_secret)
        self._auth_enabled = auth_enabled

        # OAuth token cache
        self._token: Optional[OAuthToken] = None

        # WebSocket state
        self._ws_session: Any = None  # aiohttp ClientSession
        self._ws_connection: Any = None  # aiohttp ClientWebSocketResponse
        self._ws_agent_id: str = ""
        self._ws_task: Optional[asyncio.Task] = None  # background reconnect loop
        self._ws_event_loop: Optional[asyncio.AbstractEventLoop] = None

        # Callbacks
        self.dispatch_handler: Optional[DispatchCallback] = None
        """Callback for incoming WS-dispatched tasks. Signature: ``fn(task_dict)``."""

        # Async HTTP session (lazy)
        self._async_session: Any = None

        # Sync session (lazy)
        self._sync_session: Any = None

    # ------------------------------------------------------------------
    # Tenant selection
    # ------------------------------------------------------------------

    @property
    def tenant(self) -> str:
        """Currently selected tenant for multi-tenant isolation.

        When set to a non-empty string, all subsequent API calls will
        automatically include ``?tenant=<value>`` query parameter so the
        Registry filters by this tenant.

        Returns:
            The currently selected tenant, or ``""`` if none is selected.
        """
        return self._tenant

    @tenant.setter
    def tenant(self, value: str) -> None:
        """Select a tenant for subsequent API calls."""
        self._tenant = value

    def _tenant_params(self) -> Dict[str, str]:
        """Build query-param dict with ``tenant`` when a tenant is selected.

        Use this in ``params=`` argument of requests/aiohttp calls to
        automatically attach ``?tenant=xxx`` to the URL.

        Returns:
            ``{"tenant": value}`` if a tenant is selected, else ``{}``.
        """
        if self._tenant:
            return {"tenant": self._tenant}
        return {}

    def _tenant_header(self) -> Dict[str, str]:
        """Build ``X-Tenant-ID`` header dict when a tenant is selected.

        Use this alongside ``headers=`` to propagate tenant identity on
        endpoints that read the ``X-Tenant-ID`` header.

        Returns:
            ``{"X-Tenant-ID": value}`` if a tenant is selected, else ``{}``.
        """
        if self._tenant:
            return {"X-Tenant-ID": self._tenant}
        return {}

    # ------------------------------------------------------------------
    # Context manager (async)
    # ------------------------------------------------------------------

    async def __aenter__(self) -> A2AClient:
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()

    async def close(self) -> None:
        """Close all open connections (WebSocket, async HTTP session)."""
        # Disconnect WebSocket
        if self._ws_connection is not None:
            try:
                if not self._ws_connection.closed:
                    await self._ws_connection.close()
            except Exception:
                pass
            self._ws_connection = None

        # Cancel background reconnect task
        if self._ws_task is not None and not self._ws_task.done():
            self._ws_task.cancel()
            try:
                await self._ws_task
            except (asyncio.CancelledError, Exception):
                pass
            self._ws_task = None

        # Close aiohttp session
        if self._async_session is not None and not self._async_session.closed:
            await self._async_session.close()
            self._async_session = None

        # Close requests session
        if self._sync_session is not None:
            self._sync_session.close()
            self._sync_session = None

    # ------------------------------------------------------------------
    # Internal: async HTTP session
    # ------------------------------------------------------------------

    async def _get_async_session(self) -> Any:
        """Get or create an aiohttp ClientSession."""
        if self._async_session is None or self._async_session.closed:
            import aiohttp

            self._async_session = aiohttp.ClientSession(
                headers={"User-Agent": self._user_agent},
                timeout=aiohttp.ClientTimeout(total=self._timeout),
            )
        return self._async_session

    def _get_sync_session(self) -> Any:
        """Get or create a requests.Session."""
        if self._sync_session is None:
            import requests

            self._sync_session = requests.Session()
            self._sync_session.headers.update({"User-Agent": self._user_agent})
        return self._sync_session

    # ------------------------------------------------------------------
    # OAuth Token Management
    # ------------------------------------------------------------------

    def _ensure_token(self) -> str:
        """Obtain or refresh an OAuth token (sync).

        Uses the ``client_credentials`` grant to acquire a token from the
        Registry's ``/auth/token`` endpoint.  Caches the token and
        auto-refreshes shortly before expiry (``TOKEN_REFRESH_MARGIN`` seconds).

        Returns:
            The Bearer token string, or empty string if auth is disabled.

        Raises:
            A2AClientError: If token acquisition fails and auth is required.
        """
        if not self._auth_enabled:
            return ""

        if self._token is not None and not self._token.is_expired:
            return self._token.access_token

        # Acquire a fresh token
        payload = {
            "grant_type": "client_credentials",
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "scope": self._scope,
        }

        import requests

        try:
            resp = requests.post(
                f"{self._base_url}/auth/token",
                data=payload,
                timeout=self._timeout,
                verify=self._verify_ssl,
            )
        except requests.RequestException as e:
            raise ConnectionError(f"Failed to connect to Registry at {self._base_url}: {e}") from e

        if resp.status_code != 200:
            _raise_for_status(resp, "Token acquisition failed")

        data = resp.json()
        self._token = OAuthToken(
            access_token=data["access_token"],
            token_type=data.get("token_type", "Bearer"),
            expires_in=data.get("expires_in", 3600),
            scope=data.get("scope", self._scope),
        )
        logger.debug(
            "Obtained OAuth token (expires in %ss, scope=%s)",
            data.get("expires_in", 3600),
            self._scope,
        )
        return self._token.access_token

    async def _async_ensure_token(self) -> str:
        """Obtain or refresh an OAuth token (async).

        Same semantics as :meth:`_ensure_token` but uses ``aiohttp``.
        """
        if not self._auth_enabled:
            return ""

        if self._token is not None and not self._token.is_expired:
            return self._token.access_token

        session = await self._get_async_session()

        payload = {
            "grant_type": "client_credentials",
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "scope": self._scope,
        }

        try:
            async with session.post(
                f"{self._base_url}/auth/token",
                data=payload,
            ) as resp:
                if resp.status != 200:
                    body = await _async_read_body(resp)
                    _raise_from_resp(resp, body, "Token acquisition failed")

                data = await resp.json()
        except asyncio.TimeoutError as e:
            raise ConnectionError(
                f"Token request timed out after {self._timeout}s"
            ) from e

        self._token = OAuthToken(
            access_token=data["access_token"],
            token_type=data.get("token_type", "Bearer"),
            expires_in=data.get("expires_in", 3600),
            scope=data.get("scope", self._scope),
        )
        logger.debug(
            "Obtained OAuth token (expires in %ss, scope=%s)",
            data.get("expires_in", 3600),
            self._scope,
        )
        return self._token.access_token

    def _auth_header(self) -> Dict[str, str]:
        """Build the ``Authorization`` header dict.

        Returns:
            ``{"Authorization": "Bearer <token>"}`` if auth is enabled,
            otherwise an empty dict (making callers transparently skip auth
            via header merging).  Also includes ``X-Tenant-ID`` when a
            tenant is selected.
        """
        result: Dict[str, str] = {}
        if self._auth_enabled:
            token = self._ensure_token()
            result["Authorization"] = f"Bearer {token}"
        result.update(self._tenant_header())
        return result

    async def _async_auth_header(self) -> Dict[str, str]:
        """Async version of :meth:`_auth_header`."""
        result: Dict[str, str] = {}
        if self._auth_enabled:
            token = await self._async_ensure_token()
            result["Authorization"] = f"Bearer {token}"
        result.update(self._tenant_header())
        return result

    # ------------------------------------------------------------------
    # Agent Lifecycle
    # ------------------------------------------------------------------

    def register_agent(
        self,
        name: str = "",
        description: str = "",
        *,
        agent_card: Optional[Dict[str, Any]] = None,
        url: str = "",
        skills: Optional[List[Dict[str, Any]]] = None,
        provider: Optional[Dict[str, str]] = None,
        security_schemes: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> str:
        """Register this agent with the A2A Registry.

        Accepts either a full ``agent_card`` dict (v1.0 format), or builds
        one from individual parameters.  When ``agent_card`` is provided,
        the ``name`` and ``description`` parameters are optional.

        Args:
            name:              Agent display name (required when not using
                               ``agent_card``).
            description:       Human-readable description.
            agent_card:        Full v1.0 Agent Card dict. When provided,
                               the individual parameters are ignored.
            url:               Agent's endpoint URL (converted to
                               ``supported_interfaces``).
            skills:            List of skill dicts (e.g.
                               ``[{"id": "...", "name": "...", "description": "..."}]``).
            provider:          Provider metadata dict with ``organization`` and ``url``.
            security_schemes:  Security scheme definitions dict.

        Returns:
            The agent ID assigned by the Registry.

        Raises:
            RegistryError:  On HTTP error responses.
            ConnectionError: On connection failures.
        """
        import requests

        if agent_card is not None:
            payload = agent_card
        else:
            if not name:
                raise ValueError("'name' is required when not using 'agent_card'")
            payload = self._build_card(
                name=name,
                description=description,
                url=url,
                skills=skills,
                provider=provider,
                security_schemes=security_schemes,
            )

        headers = {"Content-Type": "application/json"}
        headers.update(self._auth_header())

        session = self._get_sync_session()
        try:
            resp = session.post(
                f"{self._base_url}/v1/agents",
                json=payload,
                headers=headers,
                timeout=self._timeout,
                verify=self._verify_ssl,
            )
        except requests.RequestException as e:
            raise ConnectionError(f"Failed to connect to Registry: {e}") from e

        if resp.status_code in (200, 201):
            data = resp.json()
            agent_id = data["id"]
            logger.info("Registered agent '%s' as '%s'", name, agent_id)
            return agent_id
        elif resp.status_code == 409:
            # Agent already exists — look up by name
            return self._find_agent_by_name(name)
        else:
            _raise_for_status(resp, "Agent registration failed")
            return ""  # unreachable

    async def async_register_agent(
        self,
        name: str = "",
        description: str = "",
        *,
        agent_card: Optional[Dict[str, Any]] = None,
        url: str = "",
        skills: Optional[List[Dict[str, Any]]] = None,
        provider: Optional[Dict[str, str]] = None,
        security_schemes: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> str:
        """Async version of :meth:`register_agent`."""
        if agent_card is not None:
            payload = agent_card
        else:
            if not name:
                raise ValueError("'name' is required when not using 'agent_card'")
            payload = self._build_card(
                name=name,
                description=description,
                url=url,
                skills=skills,
                provider=provider,
                security_schemes=security_schemes,
            )

        headers = {"Content-Type": "application/json"}
        auth_headers = await self._async_auth_header()
        headers.update(auth_headers)

        session = await self._get_async_session()

        try:
            async with session.post(
                f"{self._base_url}/v1/agents",
                json=payload,
                headers=headers,
            ) as resp:
                if resp.status in (200, 201):
                    data = await resp.json()
                    agent_id = data["id"]
                    logger.info("Registered agent '%s' as '%s'", name, agent_id)
                    return agent_id
                elif resp.status == 409:
                    return await self._async_find_agent_by_name(name)
                else:
                    body = await _async_read_body(resp)
                    _raise_from_resp(resp, body, "Agent registration failed")
        except asyncio.TimeoutError as e:
            raise ConnectionError(
                f"Agent registration timed out after {self._timeout}s"
            ) from e

        return ""  # unreachable

    def deregister_agent(self, agent_id: str) -> Dict[str, Any]:
        """Deregister an agent from the Registry.

        Args:
            agent_id:  The agent ID to remove.

        Returns:
            The response dict from the Registry.

        Raises:
            NotFoundError: If the agent does not exist.
            RegistryError: On other HTTP errors.
        """
        import requests

        session = self._get_sync_session()
        try:
            resp = session.delete(
                f"{self._base_url}/v1/agents/{agent_id}",
                headers=self._auth_header(),
                timeout=self._timeout,
                verify=self._verify_ssl,
            )
        except requests.RequestException as e:
            raise ConnectionError(f"Failed to connect to Registry: {e}") from e

        if resp.status_code == 200:
            data = resp.json()
            logger.info("Deregistered agent '%s'", agent_id)
            return data
        else:
            _raise_for_status(resp, "Agent deregistration failed")
            return {}  # unreachable

    async def async_deregister_agent(self, agent_id: str) -> Dict[str, Any]:
        """Async version of :meth:`deregister_agent`."""
        session = await self._get_async_session()
        try:
            async with session.delete(
                f"{self._base_url}/v1/agents/{agent_id}",
                headers=await self._async_auth_header(),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    logger.info("Deregistered agent '%s'", agent_id)
                    return data
                else:
                    body = await _async_read_body(resp)
                    _raise_from_resp(resp, body, "Agent deregistration failed")
        except asyncio.TimeoutError as e:
            raise ConnectionError(
                f"Agent deregistration timed out after {self._timeout}s"
            ) from e
        return {}  # unreachable

    def heartbeat(self, agent_id: str) -> Dict[str, Any]:
        """Send a heartbeat to keep an agent alive.

        Args:
            agent_id:  The agent ID to heartbeat.

        Returns:
            Response dict with ``status``, ``last_heartbeat``, ``expires_at``.

        Raises:
            NotFoundError: If the agent does not exist or is stale.
            RegistryError: On other HTTP errors.
        """
        import requests

        session = self._get_sync_session()
        try:
            resp = session.post(
                f"{self._base_url}/v1/agents/{agent_id}/heartbeat",
                headers=self._auth_header(),
                timeout=self._timeout,
                verify=self._verify_ssl,
            )
        except requests.RequestException as e:
            raise ConnectionError(f"Failed to connect to Registry: {e}") from e

        if resp.status_code in (200, 203):
            data = resp.json()
            logger.debug("Heartbeat for agent '%s': %s", agent_id, data.get("status"))
            return data
        else:
            _raise_for_status(resp, "Heartbeat failed")
            return {}  # unreachable

    async def async_heartbeat(self, agent_id: str) -> Dict[str, Any]:
        """Async version of :meth:`heartbeat`."""
        session = await self._get_async_session()
        try:
            async with session.post(
                f"{self._base_url}/v1/agents/{agent_id}/heartbeat",
                headers=await self._async_auth_header(),
            ) as resp:
                if resp.status in (200, 203):
                    data = await resp.json()
                    logger.debug(
                        "Heartbeat for agent '%s': %s",
                        agent_id,
                        data.get("status"),
                    )
                    return data
                else:
                    body = await _async_read_body(resp)
                    # Token expired/invalid — force refresh and retry once
                    if resp.status in (401, 403):
                        self._token = None
                        logger.info("Heartbeat 401 — refreshing token and retrying")
                        async with session.post(
                            f"{self._base_url}/v1/agents/{agent_id}/heartbeat",
                            headers=await self._async_auth_header(),
                        ) as retry_resp:
                            if retry_resp.status in (200, 203):
                                data = await retry_resp.json()
                                return data
                            body = await _async_read_body(retry_resp)
                            _raise_from_resp(retry_resp, body, "Heartbeat failed")
                    _raise_from_resp(resp, body, "Heartbeat failed")
        except asyncio.TimeoutError as e:
            raise ConnectionError(
                f"Heartbeat timed out after {self._timeout}s"
            ) from e
        return {}  # unreachable

    # ------------------------------------------------------------------
    # Agent Search / Lookup
    # ------------------------------------------------------------------

    def _find_agent_by_name(self, name: str) -> str:
        """Search for an agent by name (sync). Used on 409 conflict."""
        import requests

        import urllib.parse

        query = urllib.parse.quote(name)
        session = self._get_sync_session()
        try:
            resp = session.get(
                f"{self._base_url}/v1/agents?q={query}",
                headers=self._auth_header(),
                timeout=self._timeout,
                verify=self._verify_ssl,
            )
        except requests.RequestException as e:
            raise ConnectionError(f"Failed to search agents: {e}") from e

        if resp.status_code != 200:
            _raise_for_status(resp, "Agent search failed")

        data = resp.json()
        agents = data.get("agents", [])
        if agents:
            agent_id = agents[0].get("id", "")
            logger.info("Found existing agent '%s' as '%s'", name, agent_id)
            return agent_id
        raise RegistryError(404, f"Agent '{name}' not found after 409", error="agent_not_found")

    async def _async_find_agent_by_name(self, name: str) -> str:
        """Search for an agent by name (async). Used on 409 conflict."""
        import urllib.parse

        query = urllib.parse.quote(name)
        session = await self._get_async_session()
        try:
            async with session.get(
                f"{self._base_url}/v1/agents?q={query}",
                headers=await self._async_auth_header(),
            ) as resp:
                if resp.status != 200:
                    body = await _async_read_body(resp)
                    _raise_from_resp(resp, body, "Agent search failed")

                data = await resp.json()
                agents = data.get("agents", [])
                if agents:
                    agent_id = agents[0].get("id", "")
                    logger.info("Found existing agent '%s' as '%s'", name, agent_id)
                    return agent_id
                raise RegistryError(
                    404,
                    f"Agent '{name}' not found after 409",
                    error="agent_not_found",
                )
        except asyncio.TimeoutError as e:
            raise ConnectionError(
                f"Agent search timed out after {self._timeout}s"
            ) from e

    def list_agents(
        self,
        *,
        skill: str = "",
        tag: str = "",
        q: str = "",
        limit: int = 50,
        offset: int = 0,
    ) -> Dict[str, Any]:
        """List and search agents.

        Args:
            skill:  Filter by skill ID (partial match).
            tag:    Filter by tag (partial match).
            q:      Full-text search query.
            limit:  Maximum results (default 50, max 200).
            offset: Pagination offset (default 0).

        Returns:
            Dict with ``total``, ``limit``, ``offset``, and ``agents`` list.
        """
        import requests

        params: Dict[str, Any] = {"limit": limit, "offset": offset}
        if skill:
            params["skill"] = skill
        if tag:
            params["tag"] = tag
        if q:
            params["q"] = q

        session = self._get_sync_session()
        try:
            resp = session.get(
                f"{self._base_url}/v1/agents",
                params=params,
                headers=self._auth_header(),
                timeout=self._timeout,
                verify=self._verify_ssl,
            )
        except requests.RequestException as e:
            raise ConnectionError(f"Failed to list agents: {e}") from e

        if resp.status_code != 200:
            _raise_for_status(resp, "List agents failed")
        return resp.json()

    async def async_list_agents(
        self,
        *,
        skill: str = "",
        tag: str = "",
        q: str = "",
        limit: int = 50,
        offset: int = 0,
    ) -> Dict[str, Any]:
        """Async version of :meth:`list_agents`."""
        params: Dict[str, Any] = {"limit": limit, "offset": offset}
        if skill:
            params["skill"] = skill
        if tag:
            params["tag"] = tag
        if q:
            params["q"] = q

        session = await self._get_async_session()
        try:
            async with session.get(
                f"{self._base_url}/v1/agents",
                params=params,
                headers=await self._async_auth_header(),
            ) as resp:
                if resp.status != 200:
                    body = await _async_read_body(resp)
                    _raise_from_resp(resp, body, "List agents failed")
                return await resp.json()
        except asyncio.TimeoutError as e:
            raise ConnectionError(
                f"List agents timed out after {self._timeout}s"
            ) from e

    def get_agent(self, agent_id: str) -> Dict[str, Any]:
        """Get an agent's full details and Agent Card.

        Args:
            agent_id:  The agent ID to look up.

        Returns:
            The agent's Agent Card dict.

        Raises:
            NotFoundError: If the agent does not exist.
        """
        import requests

        session = self._get_sync_session()
        try:
            resp = session.get(
                f"{self._base_url}/v1/agents/{agent_id}",
                headers=self._auth_header(),
                timeout=self._timeout,
                verify=self._verify_ssl,
            )
        except requests.RequestException as e:
            raise ConnectionError(f"Failed to get agent: {e}") from e

        if resp.status_code == 200:
            return resp.json()
        _raise_for_status(resp, "Get agent failed")
        return {}  # unreachable

    async def async_get_agent(self, agent_id: str) -> Dict[str, Any]:
        """Async version of :meth:`get_agent`."""
        session = await self._get_async_session()
        try:
            async with session.get(
                f"{self._base_url}/v1/agents/{agent_id}",
                headers=await self._async_auth_header(),
            ) as resp:
                if resp.status == 200:
                    return await resp.json()
                body = await _async_read_body(resp)
                _raise_from_resp(resp, body, "Get agent failed")
        except asyncio.TimeoutError as e:
            raise ConnectionError(f"Get agent timed out after {self._timeout}s") from e
        return {}  # unreachable

    # ------------------------------------------------------------------
    # Task Polling
    # ------------------------------------------------------------------

    def task_poll(
        self,
        task_id: str,
        *,
        timeout: float = 300.0,
        poll_interval: float = 1.0,
    ) -> Dict[str, Any]:
        """Poll a task's status and result synchronously.

        Blocks until the task reaches a terminal state
        (``completed``, ``failed``, ``canceled``) or *timeout* elapses.

        Args:
            task_id:        The task ID to poll.
            timeout:        Maximum time to poll in seconds (default 300).
            poll_interval:  Seconds between polls (default 1.0).

        Returns:
            The full task dict, or a dict with ``state: "timeout"`` if the
            polling timed out.
        """
        import requests

        import urllib.parse

        session = self._get_sync_session()
        deadline = time.time() + timeout

        while time.time() < deadline:
            try:
                resp = session.get(
                    f"{self._base_url}/v1/tasks/{task_id}",
                    params=self._tenant_params() or None,
                    headers=self._auth_header(),
                    timeout=self._timeout,
                    verify=self._verify_ssl,
                )
            except requests.RequestException as e:
                raise ConnectionError(f"Task poll failed: {e}") from e

            if resp.status_code == 200:
                task = resp.json()
                state = task.get("state", "")
                if state in ("completed", "failed", "canceled"):
                    return task
            elif resp.status_code == 404:
                # Task might not exist yet; sleep and retry
                pass
            else:
                _raise_for_status(resp, "Task poll failed")

            time.sleep(poll_interval)

        return {
            "id": task_id,
            "state": "timeout",
            "detail": f"Polling timed out after {timeout}s",
        }

    async def async_task_poll(
        self,
        task_id: str,
        *,
        timeout: float = 300.0,
        poll_interval: float = 1.0,
    ) -> Dict[str, Any]:
        """Async version of :meth:`task_poll`."""
        session = await self._get_async_session()
        deadline = time.time() + timeout

        while time.time() < deadline:
            try:
                async with session.get(
                    f"{self._base_url}/v1/tasks/{task_id}",
                    params=self._tenant_params() or None,
                    headers=await self._async_auth_header(),
                ) as resp:
                    if resp.status == 200:
                        task = await resp.json()
                        state = task.get("state", "")
                        if state in ("completed", "failed", "canceled"):
                            return task
                    elif resp.status == 404:
                        pass
                    else:
                        body = await _async_read_body(resp)
                        _raise_from_resp(resp, body, "Task poll failed")
            except asyncio.TimeoutError as e:
                raise ConnectionError(
                    f"Task poll timed out after {self._timeout}s"
                ) from e

            await asyncio.sleep(poll_interval)

        return {
            "id": task_id,
            "state": "timeout",
            "detail": f"Polling timed out after {timeout}s",
        }

    # ------------------------------------------------------------------
    # Task Dispatch (HTTP)
    # ------------------------------------------------------------------

    def dispatch_task(
        self,
        agent_id: str,
        query: str,
        *,
        session_id: str = "",
    ) -> Dict[str, Any]:
        """Submit a task to an agent via the Registry.

        The Registry forwards the task to the agent via WebSocket if
        connected.

        Args:
            agent_id:    Target agent ID.
            query:       Task description or instruction text.
            session_id:  Optional session identifier.

        Returns:
            Response dict with ``task_id``, ``agent_id``, ``state``.

        Raises:
            RegistryError: On HTTP errors (503 if agent not connected).
        """
        import requests

        payload: Dict[str, Any] = {"query": query}
        if session_id:
            payload["sessionId"] = session_id

        session = self._get_sync_session()
        try:
            resp = session.post(
                f"{self._base_url}/v1/agents/{agent_id}/dispatch",
                json=payload,
                headers=self._auth_header(),
                timeout=self._timeout,
                verify=self._verify_ssl,
            )
        except requests.RequestException as e:
            raise ConnectionError(f"Failed to dispatch task: {e}") from e

        if resp.status_code == 202:
            return resp.json()
        _raise_for_status(resp, "Task dispatch failed")
        return {}  # unreachable

    async def async_dispatch_task(
        self,
        agent_id: str,
        query: str,
        *,
        session_id: str = "",
    ) -> Dict[str, Any]:
        """Async version of :meth:`dispatch_task`."""
        payload: Dict[str, Any] = {"query": query}
        if session_id:
            payload["sessionId"] = session_id

        session = await self._get_async_session()
        try:
            async with session.post(
                f"{self._base_url}/v1/agents/{agent_id}/dispatch",
                json=payload,
                headers=await self._async_auth_header(),
            ) as resp:
                if resp.status == 202:
                    return await resp.json()
                body = await _async_read_body(resp)
                _raise_from_resp(resp, body, "Task dispatch failed")
        except asyncio.TimeoutError as e:
            raise ConnectionError(
                f"Task dispatch timed out after {self._timeout}s"
            ) from e
        return {}  # unreachable

    def task_list(
        self,
        *,
        agent_id: str = "",
        state: str = "",
        limit: int = 50,
        offset: int = 0,
    ) -> Dict[str, Any]:
        """List tasks with optional filters.

        Args:
            agent_id:  Filter by agent ID (partial match).
            state:     Filter by state (e.g. ``"dispatched"``, ``"completed"``).
            limit:     Max results (default 50, max 200).
            offset:    Pagination offset (default 0).

        Returns:
            Dict with ``total``, ``limit``, ``offset``, and ``tasks`` list.
        """
        import requests

        params: Dict[str, Any] = {"limit": limit, "offset": offset}
        if agent_id:
            params["agent_id"] = agent_id
        if state:
            params["state"] = state

        session = self._get_sync_session()
        try:
            resp = session.get(
                f"{self._base_url}/v1/tasks",
                params=params,
                headers=self._auth_header(),
                timeout=self._timeout,
                verify=self._verify_ssl,
            )
        except requests.RequestException as e:
            raise ConnectionError(f"Failed to list tasks: {e}") from e

        if resp.status_code == 200:
            return resp.json()
        _raise_for_status(resp, "List tasks failed")
        return {}  # unreachable

    async def async_task_list(
        self,
        *,
        agent_id: str = "",
        state: str = "",
        limit: int = 50,
        offset: int = 0,
    ) -> Dict[str, Any]:
        """Async version of :meth:`task_list`."""
        params: Dict[str, Any] = {"limit": limit, "offset": offset}
        if agent_id:
            params["agent_id"] = agent_id
        if state:
            params["state"] = state

        session = await self._get_async_session()
        try:
            async with session.get(
                f"{self._base_url}/v1/tasks",
                params=params,
                headers=await self._async_auth_header(),
            ) as resp:
                if resp.status == 200:
                    return await resp.json()
                body = await _async_read_body(resp)
                _raise_from_resp(resp, body, "List tasks failed")
        except asyncio.TimeoutError as e:
            raise ConnectionError(
                f"List tasks timed out after {self._timeout}s"
            ) from e
        return {}  # unreachable

    # ------------------------------------------------------------------
    # WebSocket — Persistent Connection
    # ------------------------------------------------------------------

    def connect_websocket(self, agent_id: str) -> None:
        """Connect to the Registry via WebSocket (blocking event loop runner).

        This is a **blocking** method that runs the asyncio event loop for
        the WebSocket connection.  It keeps running until the connection is
        terminated or the agent is deregistered.

        The WebSocket implements:
        - **Ping/heartbeat**: sends ``{"type": "ping"}`` every 30 s.
        - **Task dispatch**: incoming ``{"type": "task", ...}`` messages
          trigger the ``dispatch_handler`` callback.
        - **Auto-reconnect**: on disconnect, waits with exponential backoff
          (1 s → 2 s → 4 s → ... → 60 s max) and reconnects.
        - **Re-registration**: if reconnection fails with 404, re-registers
          the agent automatically.

        Args:
            agent_id:  The agent ID to connect as.

        Raises:
            ConnectionError: If the initial WebSocket upgrade fails.
        """
        import asyncio

        self._ws_agent_id = agent_id
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._ws_reconnect_loop())
        except KeyboardInterrupt:
            pass
        finally:
            loop.close()

    async def async_connect_websocket(self, agent_id: str) -> None:
        """Connect via WebSocket in the current event loop (non-blocking).

        This starts a background asyncio task that manages the WebSocket
        connection with auto-reconnect.  The task runs until the connection
        is terminated or :meth:`close` is called.

        Args:
            agent_id:  The agent ID to connect as.
        """
        self._ws_agent_id = agent_id
        self._ws_event_loop = asyncio.get_event_loop()
        self._ws_task = asyncio.create_task(self._ws_reconnect_loop())
        # Yield control briefly so the creator can catch cancellation
        await asyncio.sleep(0)

    async def _ws_reconnect_loop(self) -> None:
        """Background loop: connect, listen for messages, reconnect on error.

        Implements exponential backoff and automatic re-registration on 404.
        """
        delay = WS_RECONNECT_BASE
        agent_id = self._ws_agent_id

        while True:
            try:
                await self._ws_connect_single(agent_id)
                # Connected successfully → reset backoff on next disconnect
                delay = WS_RECONNECT_BASE
            except asyncio.CancelledError:
                logger.info("WebSocket reconnect loop cancelled for '%s'", agent_id)
                break
            except NotFoundError:
                # Agent doesn't exist — try re-registering with stored name
                logger.warning(
                    "Agent '%s' not found on reconnect — attempting re-registration",
                    agent_id,
                )
                try:
                    new_id = await self.async_register_agent(
                        name=agent_id,
                        description=f"Auto-re-registered agent: {agent_id}",
                    )
                    self._ws_agent_id = new_id
                    agent_id = new_id
                    delay = WS_RECONNECT_BASE
                except Exception as e:
                    logger.error(
                        "Re-registration failed for '%s': %s — retrying in %ss",
                        agent_id,
                        e,
                        delay,
                    )
                    await asyncio.sleep(delay)
                    delay = min(delay * WS_RECONNECT_FACTOR, WS_RECONNECT_MAX)
            except Exception as e:
                logger.warning(
                    "WebSocket error for '%s': %s — reconnecting in %ss",
                    agent_id,
                    e,
                    delay,
                )
                await asyncio.sleep(delay)
                delay = min(delay * WS_RECONNECT_FACTOR, WS_RECONNECT_MAX)

    async def _ws_connect_single(self, agent_id: str) -> None:
        """Establish a single WebSocket connection and listen for messages.

        Sends periodic ``ping`` messages.  Incoming ``task`` messages
        trigger ``dispatch_handler``.

        Args:
            agent_id:  The agent ID to connect as.
        """
        import aiohttp

        session = await self._get_async_session()
        ws_url = (
            f"{self._base_url.replace('http://', 'ws://').replace('https://', 'wss://')}"
            f"/v1/agents/{agent_id}/ws"
        )

        # Build query parameters for auth
        params: Dict[str, str] = {}
        if self._auth_enabled:
            token = await self._async_ensure_token()
            if token:
                params["token"] = token

        logger.info("Connecting WebSocket for agent '%s'", agent_id)
        try:
            ws = await session.ws_connect(
                ws_url,
                params=params if params else None,
                heartbeat=WS_PING_INTERVAL,
            )
        except aiohttp.ClientResponseError as e:
            if e.status == 404:
                raise NotFoundError(
                    404, f"Agent '{agent_id}' not found on WebSocket connect",
                    error="agent_not_found",
                ) from e
            if e.status in (401, 403):
                # Force token refresh: server may have rotated its JWT key
                self._token = None
                raise AuthError(
                    e.status, "WebSocket auth failed — invalid token",
                    error="unauthorized",
                ) from e
            raise ConnectionError(
                f"WebSocket connect failed (HTTP {e.status}): {e.message}"
            ) from e
        except aiohttp.ClientConnectorError as e:
            raise ConnectionError(
                f"WebSocket connect failed — cannot reach {ws_url}: {e}"
            ) from e

        self._ws_connection = ws
        logger.info(
            "WebSocket connected for agent '%s' (%d active)",
            agent_id,
            len(session._ws_connections) if hasattr(session, "_ws_connections") else 0,
        )

        # Send a heartbeat to mark alive
        try:
            await self.async_heartbeat(agent_id)
        except Exception:
            pass

        # Listen for messages
        try:
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                    except json.JSONDecodeError:
                        logger.warning("Invalid JSON from WS: %s", msg.data[:200])
                        continue

                    msg_type = data.get("type", "")

                    if msg_type == "pong":
                        logger.debug("Received pong")
                        continue

                    # Recognise both legacy flat format (type:"task") and
                    # TaskEnvelope format (has task_id + interaction_mode).
                    # The envelope format is sent by all three dispatch paths
                    # (V1 HTTP, V2 Dispatcher, reconnection pending dispatch).
                    is_envelope = bool(data.get("task_id")) and "interaction_mode" in data
                    if msg_type == "task" or is_envelope:
                        task_payload: Dict[str, Any] = dict(data)
                        task_label = data.get("task_id") or data.get("id", "?")
                        if self.dispatch_handler:
                            logger.info(
                                "Received WS task '%s' for agent '%s'%s",
                                task_label,
                                agent_id,
                                f" (mode={data.get('interaction_mode','')})" if is_envelope else "",
                            )
                            try:
                                result = self.dispatch_handler(task_payload)
                                # If the handler returned a coroutine, await it
                                if asyncio.iscoroutine(result):
                                    await result
                            except Exception as e:
                                logger.exception(
                                    "Dispatch handler failed for task '%s': %s",
                                    task_label,
                                    e,
                                )
                        else:
                            logger.debug(
                                "No dispatch_handler set; dropping task '%s'",
                                task_label,
                            )

                    elif msg_type == "error":
                        logger.warning("WS error from Registry: %s", data.get("detail", ""))

                    elif msg_type == "close":
                        logger.info("Registry requested WS close for '%s'", agent_id)
                        break

                    else:
                        logger.debug("Unknown WS message type: %s", msg_type)

                elif msg.type == aiohttp.WSMsgType.ERROR:
                    logger.error(
                        "WS connection error for '%s': %s",
                        agent_id,
                        ws.exception(),
                    )
                    break

                elif msg.type == aiohttp.WSMsgType.CLOSED:
                    logger.info("WS connection closed for '%s'", agent_id)
                    break

        except asyncio.CancelledError:
            logger.debug("WS listen cancelled for '%s'", agent_id)
            raise
        except Exception as e:
            logger.error("WS listen error for '%s': %s", agent_id, e)
            raise
        finally:
            self._ws_connection = None
            if not ws.closed:
                await ws.close()
            logger.info("WebSocket disconnected for agent '%s'", agent_id)
            # Let reconnect loop handle it
            raise ConnectionError(f"WebSocket disconnected for '{agent_id}'")

    # ------------------------------------------------------------------
    # Task Reporting (via WebSocket)
    # ------------------------------------------------------------------

    async def _send_ws_json(self, msg: Dict[str, Any]) -> bool:
        """Send a JSON message via WebSocket.

        Returns ``True`` on success, ``False`` if not connected.
        """
        ws = self._ws_connection
        if ws is None or ws.closed:
            logger.debug("WS not connected, dropping message: %s", msg.get("type"))
            return False
        try:
            await ws.send_json(msg)
            return True
        except Exception as e:
            logger.warning("WS send failed: %s", e)
            return False

    def report_result(
        self,
        task_id: str,
        result: Dict[str, Any],
        *,
        error: Optional[str] = None,
    ) -> bool:
        """Report a task result via WebSocket (from sync context).

        Sends ``{"type": "task_result", "id": task_id, "status": "completed",
        "result": result, "error": error}``.

        Args:
            task_id:  The task ID to report.
            result:   Result payload (e.g. ``{"text": "done"}``).
            error:    Optional error message (if the task failed).

        Returns:
            ``True`` if the message was sent, ``False`` if the WebSocket is
            not connected.
        """
        if self._ws_event_loop is None or self._ws_event_loop.is_closed():
            logger.warning("No event loop for WS reporting")
            return False

        msg: Dict[str, Any] = {
            "type": "task_result",
            "id": task_id,
            "status": "failed" if error else "completed",
            "result": result,
        }
        if error:
            msg["error"] = error

        # Schedule the send on the WS event loop
        future = asyncio.run_coroutine_threadsafe(
            self._send_ws_json(msg),
            self._ws_event_loop,
        )
        try:
            future.result(timeout=5)
            return True
        except Exception as e:
            logger.warning("report_result failed: %s", e)
            return False

    async def async_report_result(
        self,
        task_id: str,
        result: Dict[str, Any],
        *,
        error: Optional[str] = None,
    ) -> bool:
        """Async: report a task result via WebSocket.

        Args:
            task_id:  The task ID to report.
            result:   Result payload.
            error:    Optional error message.

        Returns:
            ``True`` if sent, ``False`` if not connected.
        """
        msg: Dict[str, Any] = {
            "type": "task_result",
            "id": task_id,
            "status": "failed" if error else "completed",
            "result": result,
        }
        if error:
            msg["error"] = error
        return await self._send_ws_json(msg)

    def report_progress(
        self,
        task_id: str,
        *,
        status: str = "working",
    ) -> bool:
        """Report a task progress update via WebSocket (from sync context).

        Sends ``{"type": "task_progress", "id": task_id, "status": status}``.

        Args:
            task_id:  The task ID to update.
            status:   Status string (default ``"working"``).

        Returns:
            ``True`` if the message was sent, ``False`` if not connected.
        """
        if self._ws_event_loop is None or self._ws_event_loop.is_closed():
            logger.warning("No event loop for WS reporting")
            return False

        msg = {
            "type": "task_progress",
            "id": task_id,
            "status": status,
        }

        future = asyncio.run_coroutine_threadsafe(
            self._send_ws_json(msg),
            self._ws_event_loop,
        )
        try:
            future.result(timeout=5)
            return True
        except Exception as e:
            logger.warning("report_progress failed: %s", e)
            return False

    async def async_report_progress(
        self,
        task_id: str,
        *,
        status: str = "working",
    ) -> bool:
        """Async: report a task progress update via WebSocket.

        Args:
            task_id:  The task ID to update.
            status:   Status string (default ``"working"``).

        Returns:
            ``True`` if sent, ``False`` if not connected.
        """
        msg = {
            "type": "task_progress",
            "id": task_id,
            "status": status,
        }
        return await self._send_ws_json(msg)

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    def health(self) -> Dict[str, Any]:
        """Check the Registry's health.

        Returns:
            Health status dict with ``status``, ``version``, ``uptime_seconds``.
        """
        import requests

        session = self._get_sync_session()
        try:
            resp = session.get(
                f"{self._base_url}/health",
                timeout=self._timeout,
                verify=self._verify_ssl,
            )
        except requests.RequestException as e:
            raise ConnectionError(f"Health check failed: {e}") from e

        if resp.status_code == 200:
            return resp.json()
        _raise_for_status(resp, "Health check failed")
        return {}  # unreachable

    async def async_health(self) -> Dict[str, Any]:
        """Async version of :meth:`health`."""
        session = await self._get_async_session()
        try:
            async with session.get(f"{self._base_url}/health") as resp:
                if resp.status == 200:
                    return await resp.json()
                body = await _async_read_body(resp)
                _raise_from_resp(resp, body, "Health check failed")
        except asyncio.TimeoutError as e:
            raise ConnectionError(f"Health check timed out after {self._timeout}s") from e
        return {}  # unreachable

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_card(
        name: str,
        description: str = "",
        url: str = "",
        skills: Optional[List[Dict[str, Any]]] = None,
        provider: Optional[Dict[str, str]] = None,
        security_schemes: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Build a v1.0 Agent Card dict from individual parameters.

        Returns a dict suitable for the ``POST /v1/agents`` endpoint.
        """
        card: Dict[str, Any] = {
            "name": name,
            "description": description,
            "version": "1.0.0",
            "default_input_modes": ["text/plain"],
            "default_output_modes": ["text/plain"],
            "capabilities": {
                "streaming": False,
                "push_notifications": True,
            },
        }

        if url:
            card["supported_interfaces"] = [
                {
                    "url": url,
                    "protocol_binding": "JSONRPC",
                    "protocol_version": "1.0",
                },
            ]

        if skills:
            card["skills"] = skills

        if provider:
            card["provider"] = provider

        if security_schemes:
            card["security_schemes"] = security_schemes

        return card


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _raise_for_status(resp: Any, context: str) -> None:
    """Parse an error response and raise the appropriate exception."""
    try:
        body = resp.json()
    except Exception:
        body = resp.text

    _raise_from_resp(resp, body, context)


def _raise_from_resp(resp: Any, body: Any, context: str) -> None:
    """Raise the appropriate RegistryError subclass from an HTTP response."""
    status = resp.status

    error_code = ""
    detail = ""
    if isinstance(body, dict):
        error_code = body.get("error", "")
        detail = body.get("detail", body.get("message", ""))

    msg = detail or error_code or f"{context}: HTTP {status}"

    if status in (401, 403):
        raise AuthError(status, body, error=error_code, detail=detail)
    if status == 404:
        raise NotFoundError(status, body, error=error_code, detail=detail)
    if status == 409:
        raise RegistryError(status, body, error=error_code or "conflict", detail=detail)

    raise RegistryError(status, body, error=error_code, detail=detail)


async def _async_read_body(resp: Any) -> Any:
    """Read an aiohttp response body as JSON or text."""
    try:
        return await resp.json()
    except Exception:
        return await resp.text()