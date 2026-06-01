"""Admin WebSocket Hub — real-time task updates for the Admin SPA.

Provides a WebSocket endpoint (``/ws/admin``) that the a2a-admin frontend
connects to receive live task updates.  Supports selective subscription
per task ID or full (all tasks) subscription.

Protocol
--------
Client → Server (JSON)::

    {"type": "subscribe",       "task_ids": ["t_xxx", "t_yyy"]}
    {"type": "subscribe_all"}
    {"type": "ping"}

Server → Client (JSON)::

    {"type": "task_update", "event": "created|updated|deleted|status_changed|comment_added",
     "task": {...}}
    {"type": "task_list",   "tasks": [...]}
    {"type": "pong"}
    {"type": "error",       "detail": "..."}
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Dict, List, Optional, Set

from aiohttp import web, WSMsgType

from simple_a2a_registry.auth import verify_token, ISSUER
from simple_a2a_registry.metrics import update_admin_ws_connections

logger = logging.getLogger("a2a_registry.ws_admin")

# Heartbeat interval — server sends ping every 30 s;
# client must respond with pong within 10 s or the connection is dropped.
HEARTBEAT_INTERVAL = 30
PONG_TIMEOUT = 10


class AdminWSHub:
    """Manages Admin UI WebSocket connections and subscription-based broadcasting.

    Attributes:
        _connections:       session_id → WebSocketResponse
        _subscriptions:     session_id → set of task_ids (empty set = subscribe all)
        _heartbeat_tasks:   session_id → asyncio.Task for the heartbeat coroutine
    """

    def __init__(self) -> None:
        self._connections: Dict[str, web.WebSocketResponse] = {}
        self._subscriptions: Dict[str, Set[str]] = {}
        self._heartbeat_tasks: Dict[str, asyncio.Task[None]] = {}
        self._auth_public_key: str = ""
        self._auth_algorithm: str = "HS256"
        self._auth_enabled: bool = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def active_connections(self) -> int:
        """Number of currently active Admin WS connections."""
        return len(self._connections)

    async def register(self, request: web.Request) -> web.WebSocketResponse:
        """Accept a WebSocket upgrade, authenticate, and start the listen loop.

        Authentication:
        - When ``_auth_enabled`` is ``True``, the query parameter ``?token=xxx``
          is validated via :func:`verify_token`.
        - A token with ``registry:admin`` scope bypasses tenant checks.
        - When auth is disabled, any connection is accepted.

        Returns:
            The :class:`aiohttp.web.WebSocketResponse` after the connection
            closes (the listen loop has already run).
        """
        # --- Authentication ---
        if self._auth_enabled:
            token = request.query.get("token", "")
            if not token:
                return web.json_response(
                    {"error": "unauthorized",
                     "detail": "Admin WebSocket upgrade requires ?token= query parameter"},
                    status=401,
                )

            payload = verify_token(
                token,
                public_key=self._auth_public_key,
                algorithm=self._auth_algorithm,
                issuer=ISSUER,
            )
            if payload is None:
                return web.json_response(
                    {"error": "invalid_token",
                     "detail": "Token expired or invalid"},
                    status=401,
                )

            scopes = payload.get("scope", "").split()
            if "registry:admin" not in scopes:
                return web.json_response(
                    {"error": "forbidden",
                     "detail": "Admin WebSocket requires registry:admin scope"},
                    status=403,
                )

        # --- WebSocket upgrade ---
        ws = web.WebSocketResponse(max_msg_size=0)
        await ws.prepare(request)

        session_id = _make_session_id(request)
        self._connections[session_id] = ws
        self._subscriptions[session_id] = set()  # empty = subscribe all
        update_admin_ws_connections(len(self._connections))
        logger.info(
            "Admin WS connected: session=%s (%d active)",
            session_id, len(self._connections),
        )

        # Start heartbeat task
        self._heartbeat_tasks[session_id] = asyncio.create_task(
            self._heartbeat_loop(session_id, ws)
        )

        # --- Message listen loop ---
        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                    except json.JSONDecodeError:
                        await self._send(ws, {"type": "error", "detail": "Invalid JSON"})
                        continue

                    msg_type = data.get("type", "")

                    if msg_type == "ping":
                        await self._send(ws, {"type": "pong"})

                    elif msg_type == "subscribe":
                        task_ids = data.get("task_ids")
                        await self.subscribe(session_id, task_ids)

                    elif msg_type == "subscribe_all":
                        self._subscriptions[session_id] = set()
                        logger.debug("Admin session '%s' subscribed to all tasks", session_id)

                    else:
                        logger.debug("Unknown admin WS message from %s: %s", session_id, msg_type)

                elif msg.type == WSMsgType.ERROR:
                    logger.error("Admin WS error for session '%s': %s",
                                 session_id, ws.exception())
                    break

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error("Admin WS handler error for session '%s': %s", session_id, e)
        finally:
            self.disconnect(session_id)

        return ws

    async def subscribe(self, session_id: str,
                        task_ids: Optional[List[str]]) -> None:
        """Update the subscription for *session_id*.

        Args:
            session_id: The client's session identifier.
            task_ids:   List of task IDs to subscribe to, or ``None`` to
                        subscribe to all tasks (same as ``subscribe_all``).
        """
        if session_id not in self._connections:
            logger.warning("subscribe() called for unknown session '%s'", session_id)
            return

        if task_ids is None:
            self._subscriptions[session_id] = set()  # all tasks
            logger.debug("Admin session '%s' subscribed to all tasks", session_id)
        else:
            self._subscriptions[session_id] = set(task_ids)
            logger.debug(
                "Admin session '%s' subscribed to %d task(s)",
                session_id, len(task_ids),
            )

    async def broadcast_task_update(self, task_id: str, event: str,
                                     data: dict) -> None:
        """Push a task update to every subscribed client.

        Args:
            task_id: The kanban task ID.
            event:   One of ``created``, ``updated``, ``deleted``,
                     ``status_changed``, ``comment_added``.
            data:    Full task object (serialisable dict).
        """
        msg = {
            "type": "task_update",
            "event": event,
            "task": data,
        }
        await self._broadcast(task_id, msg)

    async def broadcast_task_list(self, session_id: str) -> None:
        """Send the current full task list to a single client (reconnect sync).

        Args:
            session_id: The reconnecting client's session ID.
        """
        ws = self._connections.get(session_id)
        if ws is None or ws.closed:
            return
        # The caller is responsible for passing the task list data.
        # This method exists as a protocol-level hook — the actual data
        # is supplied by the orchestration layer at call time.
        # To use it:
        #     await hub.broadcast_task_list(session_id)
        #     tasks = task_store.list_tasks(...)
        #     await hub._send(ws, {"type": "task_list", "tasks": tasks})
        logger.debug("broadcast_task_list requested for session '%s'", session_id)

    def broadcast_to_all(self, msg: dict) -> None:
        """Send a JSON message to every connected session, regardless of subscription.

        Args:
            msg: The JSON-serialisable message to send.

        Note:
            This is a fire-and-forget method — does not await send_json.
            Use for agent-registered / agent-removed events that all
            Admin UI clients should see.
        """
        for session_id, ws in list(self._connections.items()):
            if ws.closed:
                continue
            try:
                asyncio.ensure_future(self._send(ws, msg))
            except Exception:
                logger.debug("Failed to schedule broadcast_to_all for '%s'", session_id)

    def disconnect(self, session_id: str) -> None:
        """Clean up a disconnected session.

        Removes the connection, subscription, and heartbeat task.
        """
        if session_id not in self._connections:
            return

        ws = self._connections.pop(session_id, None)
        self._subscriptions.pop(session_id, None)

        # Cancel heartbeat task
        hb_task = self._heartbeat_tasks.pop(session_id, None)
        if hb_task and not hb_task.done():
            hb_task.cancel()

        # Close the socket if still open
        if ws and not ws.closed:
            try:
                # Schedule close in the event loop (may be called from
                # exception handlers where await is not safe)
                asyncio.ensure_future(ws.close())
            except Exception:
                pass

        update_admin_ws_connections(len(self._connections))
        logger.info("Admin WS disconnected: session=%s (%d active)",
                     session_id, len(self._connections))

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _heartbeat_loop(self, session_id: str,
                               ws: web.WebSocketResponse) -> None:
        """Periodic server-side ping — drop connection if no pong received.

        Sends a ``ping`` frame every 30 seconds.  If the client's pong
        doesn't arrive within 10 seconds the connection is closed.
        """
        try:
            while not ws.closed:
                await asyncio.sleep(HEARTBEAT_INTERVAL)
                if ws.closed:
                    break
                try:
                    await ws.send_json({"type": "ping"})
                    # Wait for the client's pong (the listen loop handles it)
                    pong_waiter = asyncio.create_task(
                        self._wait_for_pong(session_id)
                    )
                    done, _ = await asyncio.wait(
                        [pong_waiter], timeout=PONG_TIMEOUT,
                    )
                    if not done:
                        logger.warning(
                            "Admin session '%s' heartbeat timeout — closing",
                            session_id,
                        )
                        self.disconnect(session_id)
                        break
                except ConnectionResetError:
                    logger.debug("Admin session '%s' connection reset during heartbeat",
                                 session_id)
                    self.disconnect(session_id)
                    break
                except Exception:
                    logger.exception("Admin heartbeat error for '%s'", session_id)
                    break
        except asyncio.CancelledError:
            pass

    async def _wait_for_pong(self, session_id: str) -> None:
        """Wait until the next message from the client is a ``pong``.

        This is a coroutine that continuously reads from the WS until
        we see a ``pong`` or the connection drops.  The enclosing
        ``_heartbeat_loop`` times it out with ``asyncio.wait``.
        """
        ws = self._connections.get(session_id)
        if ws is None or ws.closed:
            return
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                except json.JSONDecodeError:
                    continue
                if data.get("type") == "pong":
                    return
            elif msg.type in (WSMsgType.CLOSED, WSMsgType.ERROR):
                return
        return

    async def _broadcast(self, task_id: str, msg: dict) -> None:
        """Send *msg* to every session that is subscribed to *task_id*.

        Args:
            task_id: The kanban task ID to check subscriptions against.
            msg:     The JSON-serialisable message to send.
        """
        if not self._connections:
            return

        stale: list[str] = []
        for session_id, ws in list(self._connections.items()):
            if ws.closed:
                stale.append(session_id)
                continue

            # Empty subscription set = subscribe all
            subs = self._subscriptions.get(session_id, set())
            if subs and task_id not in subs:
                continue  # not interested in this task

            try:
                await ws.send_json(msg)
            except (ConnectionResetError, ConnectionAbortedError):
                stale.append(session_id)
            except Exception as e:
                logger.warning("Error sending to admin session '%s': %s",
                               session_id, e)
                stale.append(session_id)

        # Clean up stale connections
        for sid in stale:
            self.disconnect(sid)

    async def _send(self, ws: web.WebSocketResponse, msg: dict) -> None:
        """Send a JSON message to a single WebSocket, catching errors."""
        try:
            await ws.send_json(msg)
        except (ConnectionResetError, ConnectionAbortedError):
            pass
        except Exception as e:
            logger.warning("Error sending admin WS message: %s", e)

    # ------------------------------------------------------------------
    # Graceful shutdown
    # ------------------------------------------------------------------

    async def close_all(self) -> None:
        """Close all active Admin WebSocket connections.

        Called during server shutdown to notify all connected clients.
        """
        if not self._connections:
            return

        logger.info("Closing %d admin WebSocket connection(s)…", len(self._connections))
        close_tasks = []
        for session_id, ws in list(self._connections.items()):
            if ws and not ws.closed:
                close_tasks.append(
                    ws.send_json({"type": "close", "reason": "registry_shutdown"})
                )
        if close_tasks:
            done, _ = await asyncio.wait(close_tasks, timeout=2.0)
            logger.info(
                "  Notified %d/%d admin WS clients (%d timed out)",
                len(done), len(close_tasks), len(close_tasks) - len(done),
            )

        # Disconnect all sessions
        for session_id in list(self._connections.keys()):
            self.disconnect(session_id)


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _make_session_id(request: web.Request) -> str:
    """Generate a unique session ID for an Admin WS connection.

    Uses ``X-Request-ID`` header when available, otherwise falls back
    to a timestamp + random suffix.
    """
    rid = request.headers.get("X-Request-ID", "")
    if rid:
        return f"admin-ws-{rid[:16]}"
    return f"admin-ws-{int(time.time() * 1000)}"
