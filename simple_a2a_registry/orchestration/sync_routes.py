"""SYNC_CALL 同步直通路由 — POST /v2/sync-call.

§6 SCN-02, AC-02: 不创建状态机任务（INV-3），直接通过 WS Hub 投递到
目标 Agent，同步等待结果（默认 3s 超时），返回前执行出口安全围栏。

关键决策（D1, D5）：
- 默认 3s 超时窗口（NFR-1）
- 不进入状态机（INV-3）
- 携带 security_context 参与溯源但不进状态机

WS 消息协议（参考 docs/ws-task-lifecycle.md §2.1）：
  发送 → {"type": "sync_call", "request_id": "...", "interaction_mode": "sync_call",
           "skill": "...", "input": {...}, "output_contract": {...},
           "security_context": {...}, "tenant_id": "..."}
  接收 ← {"type": "sync_call_response", "request_id": "...",
           "status": "success"|"error", "result": {...} | null, "error": "..."}
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple, TYPE_CHECKING

from aiohttp import web

from simple_a2a_registry.auth import require_scope
from simple_a2a_registry.errors import json_error
from simple_a2a_registry.orchestration.validation import (
    validate_output,
)
from simple_a2a_registry.orchestration.contract import (
    InteractionMode,
    OutputContract,
    SecurityContext,
    TaskEnvelope,
)
from simple_a2a_registry.security.ape import (
    AuthorizationPolicyEngine,
    CallerIdentity,
    CheckpointResult,
)
from simple_a2a_registry.security.events import SecurityEventStore, SecurityEventType
from simple_a2a_registry.security.pt import ProvenanceTracker
from simple_a2a_registry.store import Store as RegistryStore

if TYPE_CHECKING:
    from simple_a2a_registry.registry_handler import WSContext

logger = logging.getLogger("a2a_registry.orchestration.sync_routes")

# ---------------------------------------------------------------------------
# 默认超时时间（NFR-1）
# ---------------------------------------------------------------------------
DEFAULT_SYNC_TIMEOUT_SECONDS: float = 3.0


# ---------------------------------------------------------------------------
# 挂起的同步请求 — 用于 WS 响应关联
# ---------------------------------------------------------------------------

# 请求 ID -> asyncio.Future[Tuple[str, Optional[dict], Optional[str]]]
# 响应的 Future 解析为 (status, result, error)
_pending_requests: Dict[str, asyncio.Future] = {}


def _resolve_pending(request_id: str, status: str,
                     result: Optional[dict] = None,
                     error: Optional[str] = None) -> None:
    """Resolve a pending sync_call request with the agent's response."""
    fut = _pending_requests.pop(request_id, None)
    if fut is not None and not fut.done():
        fut.set_result((status, result, error))


# ---------------------------------------------------------------------------
# 出口安全围栏钩子
# ---------------------------------------------------------------------------

# 签名: async fn(request: web.Request, agent_id: str,
#                request_envelope: TaskEnvelope, response_data: dict) -> Optional[web.Response]
# 当返回非 None 时，该 Response 将替代正常响应返回给调用方
ExitBarrierHook = Callable[
    [web.Request, str, TaskEnvelope, dict],
    Awaitable[Optional[web.Response]],
]

_exit_barriers: list[ExitBarrierHook] = []


def register_exit_barrier(hook: ExitBarrierHook) -> None:
    """Register an exit security barrier hook (出口安全围栏预留钩子).

    All hooks run in order before the sync response is returned.
    If any hook returns a non-None Response, that response is returned
    immediately and subsequent hooks are skipped.
    """
    _exit_barriers.append(hook)


async def _run_exit_barriers(
    request: web.Request,
    agent_id: str,
    envelope: TaskEnvelope,
    response_data: dict,
) -> Optional[web.Response]:
    """Run all exit barriers in order. Returns first blocking response or None."""
    for hook in _exit_barriers:
        try:
            result = await hook(request, agent_id, envelope, response_data)
            if result is not None:
                return result
        except Exception:
            logger.exception("Exit barrier hook failed, continuing")
    return None


# ---------------------------------------------------------------------------
# APE 助手
# ---------------------------------------------------------------------------


def _build_caller_from_request(request: web.Request) -> CallerIdentity:
    """Extract caller identity from auth middleware metadata."""
    return CallerIdentity(
        agent_id=request.get("agent_id", "anonymous"),
        tenant=request.get("tenant", ""),
        scope=request.get("token_scopes", ""),
        token_payload=request.get("token_payload", {}),
    )


async def _run_entry_barriers(
    request: web.Request,
    ape: Optional[AuthorizationPolicyEngine],
    envelope: TaskEnvelope,
) -> Optional[web.Response]:
    """Run entry security barriers (APE). Returns denial response or None."""
    if ape is None:
        return None
    caller = _build_caller_from_request(request)
    checkpoint = await ape.check_task_create(
        caller=caller,
        task_data=envelope.to_dict(),
    )
    if not checkpoint.allowed:
        return json_error(
            403, "security_denied",
            checkpoint.reason,
            request=request,
        )
    return None


# ---------------------------------------------------------------------------
# WS sync_call_response 分发器（由 server.py 的 WS 循环调用）
# ---------------------------------------------------------------------------


async def handle_ws_sync_response(
    ws: web.WebSocketResponse,
    data: Dict[str, Any],
    ctx: WSContext,
) -> None:
    """WS handler for ``sync_call_response`` — resolves pending sync_call.

    Registered via ``create_default_router`` and called by server.py's
    ``_get_ws_handler("sync_call_response")``.

    Expected payload::

        {"type": "sync_call_response", "request_id": "...",
         "status": "success"|"error", "result": {...} | null, "error": "..."}
    """
    request_id = data.get("request_id", "")
    if not request_id:
        logger.warning("sync_call_response without 'request_id' from agent '%s'", ctx.agent_id)
        return

    status = data.get("status", "error")
    result = data.get("result")
    error = data.get("error")

    _resolve_pending(request_id, status, result, error)
    logger.debug("Resolved sync_call %s from agent '%s': status=%s",
                 request_id, ctx.agent_id, status)


# ---------------------------------------------------------------------------
# SyncCallHandler
# ---------------------------------------------------------------------------


class SyncCallHandler:
    """处理同步同步调用（SYNC_CALL），不经过状态机。

    通过 WS Hub 直接投递到目标 Agent 并同步等待结果。
    """

    def __init__(
        self,
        ws_connections: Dict[str, web.WebSocketResponse],
        store: Optional[RegistryStore] = None,
        ape: Optional[AuthorizationPolicyEngine] = None,
        pt: Optional[ProvenanceTracker] = None,
        event_store: Optional[SecurityEventStore] = None,
    ) -> None:
        self._ws_connections = ws_connections
        self._store = store
        self._ape = ape
        self._pt = pt
        self._event_store = event_store

    # ----------------------------------------------------------
    # POST /v2/sync-call
    # ----------------------------------------------------------

    async def handle_sync_call(self, request: web.Request) -> web.Response:
        """POST /v2/sync-call — invoke a skill synchronously via WS Hub.

        请求体::

            {
                "interaction_mode": "sync_call",
                "skill": "...",
                "input": {...},
                "output_contract": {"required_fields": [...]} | None,
                "tenant_id": "...",
                "agent_id": "..."  // 目标 Agent ID
            }

        Returns::

            {
                "status": "success"|"error"|"timeout",
                "result": {...} | null,
                "error": "..." | null,
                "request_id": "req-...",
                "duration_ms": 123
            }
        """
        # ── 解析请求体 ─────────────────────────────────────────────
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return json_error(400, "invalid_json", "Invalid JSON body", request=request)

        agent_id = (body.get("agent_id") or "").strip()
        if not agent_id:
            return json_error(400, "validation_error",
                              "Missing required 'agent_id' field", request=request)

        skill = (body.get("skill") or "").strip()
        if not skill:
            return json_error(400, "validation_error",
                              "Missing required 'skill' field", request=request)

        # ── 构建 TaskEnvelope ──────────────────────────────────────
        envelope = TaskEnvelope(
            task_id=f"sync_{uuid.uuid4().hex[:12]}",
            interaction_mode=InteractionMode.SYNC_CALL,
            skill=skill,
            input_schema=body.get("input_schema"),
            input=body.get("input", {}),
            output_contract=OutputContract.from_dict(
                body.get("output_contract", {})
            ) if isinstance(body.get("output_contract"), dict) else None,
            security_context=SecurityContext.from_dict(
                body.get("security_context", {})
            ) if isinstance(body.get("security_context"), dict) else SecurityContext(
                provenance_chain_id=f"chain_{uuid.uuid4().hex[:16]}",
            ),
            tenant_id=body.get("tenant_id", request.get("tenant", "")),
        )

        # ── 入口安全围栏 — APE checkpoint ──────────────────────────
        barrier_resp = await _run_entry_barriers(request, self._ape, envelope)
        if barrier_resp is not None:
            return barrier_resp

        # ── 查找 Agent WS 连接 ─────────────────────────────────────
        ws = self._ws_connections.get(agent_id)
        if ws is None or ws.closed:
            msg = f"Agent '{agent_id}' is not connected via WebSocket"
            if self._event_store is not None:
                self._event_store.record(
                    event_type=SecurityEventType.AUTHORIZATION_DENIED.value,
                    actor=request.get("agent_id", "anonymous"),
                    target=agent_id,
                    decision="deny",
                    tenant=envelope.tenant_id,
                    reason=msg,
                )
            return json_error(503, "agent_not_connected", msg, request=request)

        # ── 发送 sync_call WS 消息 ─────────────────────────────────
        request_id = f"sync_{uuid.uuid4().hex[:16]}"
        ws_payload = {
            "type": "sync_call",
            "request_id": request_id,
            **envelope.to_dict(),
        }
        # 始终携带 interaction_mode 字符串
        ws_payload["interaction_mode"] = InteractionMode.SYNC_CALL.value

        # 创建 Future 并注册到挂起表
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        _pending_requests[request_id] = fut

        try:
            await ws.send_json(ws_payload)
            logger.info("Sent sync_call %s to agent '%s' (skill=%s)",
                        request_id, agent_id, skill)
        except Exception as e:
            _pending_requests.pop(request_id, None)
            return json_error(502, "dispatch_failed",
                              f"Failed to send sync_call to agent '{agent_id}': {e}",
                              request=request)

        # ── 同步等待结果 ───────────────────────────────────────────
        timeout = body.get("timeout_seconds", DEFAULT_SYNC_TIMEOUT_SECONDS)
        try:
            status, result, error = await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            _pending_requests.pop(request_id, None)
            logger.warning("sync_call %s to agent '%s' timed out after %.1fs",
                           request_id, agent_id, timeout)
            # 记录安全事件
            if self._event_store is not None:
                self._event_store.record(
                    event_type=SecurityEventType.AUTHORIZATION_DENIED.value,
                    actor=request.get("agent_id", "anonymous"),
                    target=agent_id,
                    decision="timeout",
                    tenant=envelope.tenant_id,
                    reason=f"sync_call timed out after {timeout}s",
                )
            return json_error(504, "sync_call_timeout",
                              f"Agent '{agent_id}' did not respond within {timeout}s",
                              request=request)

        # ── OutputContract 校验（T4） ──────────────────────────────
        if (
            status == "success"
            and envelope.output_contract is not None
            and result is not None
        ):
            valid, verr = validate_output(result, envelope.output_contract)
            if not valid:
                logger.warning(
                    "OutputContract validation FAILED for sync_call %s: %s",
                    request_id, verr,
                )
                # 校验失败 → 记录安全事件，返回校验错误
                if self._event_store is not None:
                    self._event_store.record(
                        event_type=SecurityEventType.AUTHORIZATION_DENIED.value,
                        actor=request.get("agent_id", "anonymous"),
                        target=agent_id,
                        decision="deny",
                        tenant=envelope.tenant_id,
                        reason=f"OutputContract validation failed: {verr}",
                    )
                return json_error(
                    502, "output_validation_failed",
                    f"OutputContract validation failed: {verr}",
                    request=request,
                )

        # ── 出口安全围栏 ───────────────────────────────────────────
        exit_barrier_resp = await _run_exit_barriers(
            request, agent_id, envelope,
            {"status": status, "result": result, "error": error},
        )
        if exit_barrier_resp is not None:
            return exit_barrier_resp

        # ── 记录溯源（不创建状态机任务，仅审计，D5） ──────────────
        if self._pt is not None:
            try:
                self._pt.ensure_chain(
                    chain_id=envelope.task_id,
                    origin_agent=request.get("agent_id", "anonymous"),
                    origin_tenant=envelope.tenant_id,
                    root_task_id=envelope.task_id,
                    parent_task_id=None,
                    task_id=envelope.task_id,
                )
            except Exception:
                logger.debug("Failed to record provenance for sync_call %s", request_id)

        # ── 审计日志（SYNC_CALL 不创建状态机任务，仅记录日志） ───
        logger.info(
            "sync_call completed: request_id=%s agent=%s skill=%s status=%s",
            request_id, agent_id, skill, status,
        )

        # ── 记录安全事件 ───────────────────────────────────────────
        if self._event_store is not None:
            decision = "allow" if status == "success" else "deny"
            self._event_store.record(
                event_type=SecurityEventType.AUTHORIZATION_ALLOWED.value,
                actor=request.get("agent_id", "anonymous"),
                target=agent_id,
                decision=decision,
                tenant=envelope.tenant_id,
                reason=f"sync_call {request_id} completed with status={status}",
            )

        # ── 构建响应 ───────────────────────────────────────────────
        response_body: Dict[str, Any] = {
            "status": status,
            "result": result,
            "error": error,
            "request_id": request_id,
        }
        if status == "error":
            return json_error(502, "sync_call_failed",
                              error or "Agent returned error",
                              request=request, extra=response_body)

        return web.json_response(response_body)


# ---------------------------------------------------------------------------
# 路由注册
# ---------------------------------------------------------------------------


def register_sync_routes(
    app: web.Application,
    handler: SyncCallHandler,
) -> None:
    """Register SYNC_CALL routes on *app*.

    Args:
        app:      aiohttp Application.
        handler:  SyncCallHandler instance wired with WS connections and
                  security harness references.
    """
    # POST /v2/sync-call — 同步调用 (task:write)
    app.router.add_post(
        "/v2/sync-call",
        require_scope("task:write")(handler.handle_sync_call),
    )
    logger.info("Registered POST /v2/sync-call")
