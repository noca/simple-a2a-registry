"""全局错误模型 — 统一错误响应格式、异常类体系和错误处理中间件。

提供：
- ``APIError`` dataclass — 统一错误响应体 {error, detail, request_id, timestamp, extra}
- ``json_error()`` — 便捷工厂函数，从 request 对象注入 request_id
- 异常类体系：A2ARegistryError → ValidationError / NotFoundError / ConflictError / AuthError / RateLimitError
- ``error_middleware`` — aiohttp 中间件，自动捕获异常转为统一错误格式
- ``_status_to_error_code()`` — HTTP 状态码 → 错误码映射

用法：
    from simple_a2a_registry.errors import json_error, error_middleware

    # 在 handler 中直接返回错误
    return json_error(400, "validation_error", "Agent requires a 'name'", request=request)

    # 或者抛异常让中间件捕获
    raise ValidationError("Agent requires a 'name'")
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from aiohttp import web

logger = logging.getLogger("a2a_registry.errors")

# ---------------------------------------------------------------------------
# HTTP 状态码 → 错误码映射
# ---------------------------------------------------------------------------

_STATUS_TO_ERROR_CODE: Dict[int, str] = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    410: "gone",
    422: "unprocessable_entity",
    429: "too_many_requests",
    500: "internal_error",
    502: "bad_gateway",
    503: "service_unavailable",
}


def status_to_error_code(status: int) -> str:
    """Map an HTTP status code to a canonical error code string."""
    return _STATUS_TO_ERROR_CODE.get(status, f"http_{status}")


# ---------------------------------------------------------------------------
# 统一错误响应体
# ---------------------------------------------------------------------------


@dataclass
class APIError:
    """统一错误响应体。

    所有 API 端点返回的错误遵循此格式。

    Attributes:
        error:        规范错误码（如 ``validation_error``, ``agent_not_found``）
        detail:       人类可读的错误描述
        request_id:   请求追踪 ID，由传入的 request 对象自动提取
        timestamp:    ISO-8601 格式的时间戳
        extra:        可选的补充信息（如字段级别校验错误）
    """

    error: str
    detail: str
    request_id: str = ""
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    extra: Optional[Dict[str, Any]] = None

    def to_response(self, status: int) -> web.Response:
        """转换为 aiohttp JSON 响应。"""
        body: Dict[str, Any] = {
            "error": self.error,
            "detail": self.detail,
            "request_id": self.request_id,
            "timestamp": self.timestamp,
        }
        if self.extra:
            body["extra"] = self.extra
        return web.json_response(body, status=status)


# ---------------------------------------------------------------------------
# 便捷工厂函数
# ---------------------------------------------------------------------------


def json_error(
    status: int,
    error_code: str,
    detail: str,
    request: Optional[web.Request] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> web.Response:
    """创建统一格式的错误响应。

    Args:
        status:     HTTP 状态码（如 400, 404, 500）
        error_code: 规范错误码（如 ``validation_error``, ``agent_not_found``）
        detail:     人类可读的错误描述
        request:    可选的 aiohttp Request 对象；提供时会自动注入 request_id
        extra:      可选的补充信息

    Returns:
        格式化的 aiohttp JSON 响应。
    """
    request_id = _extract_request_id(request) if request else ""
    err = APIError(
        error=error_code,
        detail=detail,
        request_id=request_id,
        extra=extra,
    )
    return err.to_response(status)


def _extract_request_id(request: web.Request) -> str:
    """从 request 对象中提取 request_id。

    兼容两种模式：
    1. log.py 的 request_id_middleware 将 request_id 设置在 contextvars 中
    2. 架构文档中的 tracing_middleware 将 request_id 设置在 request 字典中
    """
    rid = request.get("request_id", "")
    if not rid:
        rid = request.headers.get("X-Request-Id", "")
    return rid


# ---------------------------------------------------------------------------
# 异常类体系
# ---------------------------------------------------------------------------


class A2ARegistryError(Exception):
    """所有 Registry 异常的基类。

    Attributes:
        error_code: 规范错误码（如 ``validation_error``, ``agent_not_found``）
        detail:     人类可读的错误描述
        status:     HTTP 状态码
        extra:      可选的补充信息
    """

    def __init__(
        self,
        error_code: str,
        detail: str,
        status: int = 500,
        extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.error_code = error_code
        self.detail = detail
        self.status = status
        self.extra = extra
        super().__init__(detail)


class ValidationError(A2ARegistryError):
    """请求体校验失败 — 400"""

    def __init__(self, detail: str, extra: Optional[Dict[str, Any]] = None) -> None:
        super().__init__("validation_error", detail, 400, extra)


class InvalidJSONError(A2ARegistryError):
    """JSON 解析失败 — 400"""

    def __init__(self, detail: str = "Invalid JSON body") -> None:
        super().__init__("invalid_json", detail, 400)


class NotFoundError(A2ARegistryError):
    """资源不存在 — 404"""

    def __init__(self, resource_type: str, resource_id: str) -> None:
        detail = f"{resource_type.capitalize()} '{resource_id}' not found"
        super().__init__(f"{resource_type}_not_found", detail, 404)


class ConflictError(A2ARegistryError):
    """资源冲突 — 409"""

    def __init__(self, detail: str) -> None:
        super().__init__("conflict", detail, 409)


class AuthError(A2ARegistryError):
    """认证/授权失败 — 401/403"""

    def __init__(self, error_code: str, detail: str, status: int = 401) -> None:
        super().__init__(error_code, detail, status)


class RateLimitError(A2ARegistryError):
    """请求频率超限 — 429"""

    def __init__(self, detail: str = "Too many requests", retry_after: int = 60) -> None:
        super().__init__("too_many_requests", detail, 429, extra={"retry_after": retry_after})


class ProtectedResourceError(A2ARegistryError):
    """试图删除受保护资源 — 403"""

    def __init__(self, detail: str = "Cannot delete a protected resource") -> None:
        super().__init__("protected", detail, 403)


# ---------------------------------------------------------------------------
# 错误处理中间件
# ---------------------------------------------------------------------------


@web.middleware
async def error_middleware(
    request: web.Request, handler: Any,
) -> web.StreamResponse:
    """全局统一错误处理中间件。

    捕获所有未处理异常，返回统一格式的错误响应：:

        {
            "error": "validation_error",
            "detail": "Agent requires a 'name'",
            "request_id": "req-abc123",
            "timestamp": "2026-05-26T10:30:00.123Z"
        }

    处理优先级：
    1. A2ARegistryError — 自定义异常，直接使用其 status/error_code
    2. web.HTTPException — aiohttp 原生 HTTP 异常（如 404, 405）
    3. JSONDecodeError — JSON 解析失败
    4. 其余未处理异常 — 500 internal_error
    """
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
            status_to_error_code(exc.status),
            exc.reason or str(exc),
            request=request,
        )
    except json.JSONDecodeError:
        return json_error(400, "invalid_json", "Invalid JSON body", request=request)
    except Exception:
        logger.exception(
            "Unhandled error handling %s %s", request.method, request.path,
        )
        return json_error(500, "internal_error", "Internal server error", request=request)


# ---------------------------------------------------------------------------
# 请求超时中间件
# ---------------------------------------------------------------------------


@web.middleware
async def timeout_middleware(
    request: web.Request, handler: Any,
) -> web.StreamResponse:
    """请求级超时控制中间件。

    默认超时 30 秒，可通过 request 的 ``timeout_seconds`` 属性自定义。
    超时后返回 503 错误。WebSocket 升级请求被自动跳过（长连）。

    用法：:

        # 自定义超时（某个特定端点）
        request["timeout_seconds"] = 60
    """
    # WebSocket upgrade requests are long-lived — skip timeout wrapping
    if request.headers.get("Upgrade", "").lower() == "websocket":
        return await handler(request)

    timeout = request.get("timeout_seconds", 30)
    try:
        response = await asyncio.wait_for(handler(request), timeout=timeout)
        return response
    except asyncio.TimeoutError:
        return json_error(
            503, "request_timeout",
            f"Request exceeded timeout of {timeout}s",
            request=request,
        )