"""用户登录系统 — 密码认证、会话管理、角色权限。

提供：
- ``UserStore`` — 用户持久化（users 表 CRUD）
- ``SessionManager`` — JWT + HTTP-only cookie 会话
- ``UserAuthMiddleware`` — Web 页面认证中间件
- ``UserHandler`` — 登录/登出/用户管理 API

依赖 bcrypt 进行密码哈希，复用项目的 JWT 签名/验证。
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

import jwt as pyjwt
from aiohttp import web

from simple_a2a_registry.database import DatabaseEngine
from simple_a2a_registry.audit import AuditStore, EventType

logger = logging.getLogger("a2a_registry.users")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SESSION_EXPIRY_SECONDS = 3600         # 1 hour session
SESSION_COOKIE_NAME = "a2a_session"
ROLES = ("admin", "operator", "viewer")
ROLE_HIERARCHY = {
    "admin": 100,
    "operator": 50,
    "viewer": 10,
}

# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------


@dataclass
class UserRecord:
    """A registered dashboard user."""
    username: str
    password_hash: str
    role: str = "viewer"
    created_at: float = 0.0
    updated_at: float = 0.0


# ---------------------------------------------------------------------------
# SQL schema
# ---------------------------------------------------------------------------

_USERS_SCHEMA_SQL = """CREATE TABLE IF NOT EXISTS users (
    username        TEXT PRIMARY KEY,
    password_hash   TEXT NOT NULL,
    role            TEXT NOT NULL DEFAULT 'viewer'
                    CHECK(role IN ('admin', 'operator', 'viewer')),
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL
);"""

_USERS_SCHEMA_MYSQL = """CREATE TABLE IF NOT EXISTS users (
    username        VARCHAR(255) PRIMARY KEY,
    password_hash   VARCHAR(255) NOT NULL,
    role            VARCHAR(50) NOT NULL DEFAULT 'viewer',
    created_at      DOUBLE NOT NULL,
    updated_at      DOUBLE NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;"""


def _maybe_create_users_schema(engine: DatabaseEngine) -> None:
    """Create the users table if it doesn't exist."""
    if engine.driver == "sqlite":
        engine.executescript(_USERS_SCHEMA_SQL)
        engine.commit()
    elif engine.driver == "mysql":
        for stmt in _USERS_SCHEMA_MYSQL.split(";"):
            stripped = stmt.strip()
            if stripped:
                try:
                    engine.execute(stripped)
                except Exception:
                    pass
        engine.commit()


# ---------------------------------------------------------------------------
# UserStore
# ---------------------------------------------------------------------------


class UserStore:
    """User persistence layer — uses the shared DatabaseEngine."""

    def __init__(self, engine: DatabaseEngine) -> None:
        self._engine = engine
        _maybe_create_users_schema(engine)

    # ------------------------------------------------------------------
    # Password helpers
    # ------------------------------------------------------------------

    @staticmethod
    def hash_password(password: str) -> str:
        """Hash a password with bcrypt."""
        import bcrypt
        salt = bcrypt.gensalt(rounds=10)
        return bcrypt.hashpw(password.encode("utf-8"), salt).decode("utf-8")

    @staticmethod
    def verify_password(password: str, password_hash: str) -> bool:
        """Verify a password against its bcrypt hash."""
        import bcrypt
        try:
            return bcrypt.checkpw(
                password.encode("utf-8"),
                password_hash.encode("utf-8"),
            )
        except Exception:
            return False

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def create_user(
        self,
        username: str,
        password: str,
        role: str = "viewer",
    ) -> UserRecord:
        """Create a new user. Raises ValueError if username exists."""
        role = role.lower()
        if role not in ROLES:
            raise ValueError(f"Invalid role '{role}'. Must be one of: {', '.join(ROLES)}")

        # Check duplicate
        existing = self.get_user(username)
        if existing is not None:
            raise ValueError(f"User '{username}' already exists")

        now = time.time()
        pw_hash = self.hash_password(password)
        self._engine.execute(
            "INSERT INTO users (username, password_hash, role, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (username, pw_hash, role, now, now),
        )
        self._engine.commit()
        return UserRecord(username=username, password_hash=pw_hash, role=role,
                          created_at=now, updated_at=now)

    def get_user(self, username: str) -> Optional[UserRecord]:
        """Get a user by username."""
        result = self._engine.execute(
            "SELECT username, password_hash, role, created_at, updated_at "
            "FROM users WHERE username=?",
            (username,),
        )
        row = result.fetchone()
        if row is None:
            return None
        return UserRecord(
            username=row["username"],
            password_hash=row["password_hash"],
            role=row["role"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def list_users(self) -> List[Dict[str, Any]]:
        """List all users (without password hashes)."""
        result = self._engine.execute(
            "SELECT username, role, created_at, updated_at FROM users "
            "ORDER BY created_at ASC",
        )
        return [
            {
                "username": row["username"],
                "role": row["role"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
            for row in result.fetchall()
        ]

    def update_user(
        self,
        username: str,
        password: Optional[str] = None,
        role: Optional[str] = None,
    ) -> bool:
        """Update a user's password and/or role. Returns False if user not found."""
        existing = self.get_user(username)
        if existing is None:
            return False

        now = time.time()
        pw_hash = self.hash_password(password) if password else existing.password_hash
        new_role = role.lower() if role else existing.role
        if new_role not in ROLES:
            raise ValueError(f"Invalid role '{new_role}'")

        self._engine.execute(
            "UPDATE users SET password_hash=?, role=?, updated_at=? WHERE username=?",
            (pw_hash, new_role, now, username),
        )
        self._engine.commit()
        return True

    def delete_user(self, username: str) -> bool:
        """Delete a user. Returns False if not found."""
        self._engine.execute("DELETE FROM users WHERE username=?", (username,))
        self._engine.commit()
        result = self._engine.execute(
            "SELECT changes()" if self._engine.driver == "sqlite"
            else "SELECT ROW_COUNT() AS affected",
        )
        row = result.fetchone()
        # SQLite: changes() returns int; MySQL: ROW_COUNT() returns dict
        if isinstance(row, dict):
            affected = row.get("changes()", 0) or row.get("affected", 0)
        else:
            affected = row[0] if row else 0
        return affected > 0

    def authenticate(self, username: str, password: str) -> Optional[UserRecord]:
        """Authenticate a user with username/password.
        Returns UserRecord on success, None on failure.
        """
        user = self.get_user(username)
        if user is None:
            return None
        if self.verify_password(password, user.password_hash):
            return user
        return None

    # ------------------------------------------------------------------
    # Bootstrap — auto-create default admin on first start
    # ------------------------------------------------------------------

    def bootstrap_admin(self) -> str:
        """Create a default admin account if no users exist.
        Returns the initial password (logged but also returned for the caller).
        """
        users = self.list_users()
        if users:
            return ""  # already bootstrapped

        initial_password = secrets.token_urlsafe(12)
        self.create_user("admin", initial_password, role="admin")
        logger.info(
            "╔══════════════════════════════════════════════════════════╗\n"
            "║  🔐 BOOTSTRAPPED DEFAULT ADMIN ACCOUNT                  ║\n"
            "║                                                          ║\n"
            "║  Username: admin                                        ║\n"
            "║  Password: %-37s  ║\n"
            "║  Role:     admin                                        ║\n"
            "║                                                          ║\n"
            "║  ⚠️  CHANGE THIS PASSWORD IMMEDIATELY AFTER FIRST LOGIN  ║\n"
            "╚══════════════════════════════════════════════════════════╝",
            initial_password,
        )
        return initial_password


# ---------------------------------------------------------------------------
# SessionManager — JWT-based HTTP-only cookie sessions
# ---------------------------------------------------------------------------


class SessionManager:
    """Session management using JWT + HTTP-only cookie.

    Signs session tokens with a per-startup secret key so they are
    not valid across restarts (forces re-login).
    """

    def __init__(self, secret_key: Optional[str] = None) -> None:
        self._secret = secret_key or secrets.token_hex(32)
        self._algorithm = "HS256"
        logger.debug("SessionManager initialised (algorithm=HS256)")

    def create_session(self, username: str, role: str) -> str:
        """Create a signed session JWT."""
        now = int(time.time())
        payload = {
            "sub": username,
            "role": role,
            "iat": now,
            "exp": now + SESSION_EXPIRY_SECONDS,
            "jti": str(uuid.uuid4()),
            "type": "user_session",
        }
        return pyjwt.encode(payload, self._secret, algorithm=self._algorithm)

    def verify_session(self, token: str) -> Optional[Dict[str, Any]]:
        """Verify a session JWT. Returns payload dict or None."""
        try:
            payload = pyjwt.decode(
                token,
                self._secret,
                algorithms=[self._algorithm],
                options={"verify_exp": True},
            )
            if payload.get("type") != "user_session":
                return None
            return payload
        except (pyjwt.ExpiredSignatureError, pyjwt.InvalidTokenError) as e:
            logger.debug("Session token invalid: %s", e)
            return None

    def set_session_cookie(self, response: web.StreamResponse, token: str) -> None:
        """Set the HTTP-only session cookie on the response."""
        response.set_cookie(
            SESSION_COOKIE_NAME,
            token,
            max_age=SESSION_EXPIRY_SECONDS,
            httponly=True,
            samesite="Lax",
            path="/",
            # secure=True in production with HTTPS
        )

    def clear_session_cookie(self, response: web.StreamResponse) -> None:
        """Clear the session cookie (logout)."""
        response.del_cookie(SESSION_COOKIE_NAME, path="/")

    def get_session(self, request: web.Request) -> Optional[Dict[str, Any]]:
        """Extract and verify session from request cookies."""
        token = request.cookies.get(SESSION_COOKIE_NAME, "")
        if not token:
            return None
        return self.verify_session(token)


# ---------------------------------------------------------------------------
# Role check helper
# ---------------------------------------------------------------------------


def role_ge(user_role: str, required_role: str) -> bool:
    """Check if user_role meets or exceeds the required level."""
    return ROLE_HIERARCHY.get(user_role, 0) >= ROLE_HIERARCHY.get(required_role, 0)


def require_role(required_role: str) -> Callable:
    """Decorator that checks the authenticated user has sufficient role.

    Usage::

        @require_role("operator")
        async def handle_foo(request: web.Request) -> web.Response:
            ...

    Returns 403 if the user's role is insufficient.
    The request must have ``user_session`` injected by the session middleware.
    """
    def decorator(handler: Callable) -> Callable:
        async def wrapper(request: web.Request, *args: Any, **kwargs: Any) -> web.StreamResponse:
            session = request.get("user_session")
            if session is None:
                return web.json_response(
                    {"error": "unauthorized", "detail": "Not authenticated"},
                    status=401,
                )
            user_role = session.get("role", "viewer")
            if not role_ge(user_role, required_role):
                return web.json_response(
                    {
                        "error": "forbidden",
                        "detail": f"Role '{user_role}' insufficient; requires '{required_role}'",
                    },
                    status=403,
                )
            return await handler(request, *args, **kwargs)
        return wrapper
    return decorator


# ---------------------------------------------------------------------------
# Session middleware (for web pages)
# ---------------------------------------------------------------------------


def user_session_middleware_factory(
    session_manager: SessionManager,
    *,
    enabled: bool = True,
) -> Callable:
    """Create an aiohttp middleware that validates user session cookies.

    For paths under /api/* (except /api/login, /api/logout):
      - Returns 401 if no valid session (JSON response)

    For web pages (everything else except public paths):
      - Redirects to /login if no valid session
      - Injects ``user_session`` into request for downstream handlers

    Public paths (no auth required):
      - /login, /logout
      - /health, /metrics
      - /.well-known/*
      - /static/login.html
    """

    PUBLIC_PATHS: Set[str] = {
        "/",
        "/health",
        "/metrics",
        "/login",
        "/logout",
        "/api/login",
        "/api/logout",
        "/.well-known/",
        "/auth/",
        "/static/login.html",
        "/favicon.ico",
    }

    def _is_public(path: str) -> bool:
        if path in PUBLIC_PATHS:
            return True
        if path.startswith("/.well-known/"):
            return True
        if path.startswith("/auth/"):
            return True
        return False

    @web.middleware
    async def _session_middleware(
        request: web.Request,
        handler: Any,
    ) -> web.StreamResponse:
        if not enabled:
            # Auth disabled — inject a default admin session
            request["user_session"] = {
                "sub": "admin",
                "role": "admin",
            }
            request["username"] = "admin"
            request["user_role"] = "admin"
            return await handler(request)

        path = request.path

        # Public paths — pass through (no session required)
        if _is_public(path):
            # But still try to decode session for the login page to show state
            session = session_manager.get_session(request)
            if session:
                request["user_session"] = session
                request["username"] = session.get("sub")
                request["user_role"] = session.get("role")
            return await handler(request)

        # API paths — return 401 JSON if no session
        is_api = path.startswith("/api/") or path.startswith("/admin/") or path.startswith("/v1/")

        session = session_manager.get_session(request)
        if session is None:
            if is_api:
                return web.json_response(
                    {"error": "unauthorized", "detail": "Session expired or invalid"},
                    status=401,
                )
            # Web page — redirect to login
            return web.HTTPFound("/login")

        # Inject session info
        request["user_session"] = session
        request["username"] = session.get("sub", "")
        request["user_role"] = session.get("role", "viewer")

        return await handler(request)

    return _session_middleware


# ---------------------------------------------------------------------------
# UserHandler — login, logout, user management API
# ---------------------------------------------------------------------------


class UserHandler:
    """Handlers for login, logout, and user management endpoints."""

    def __init__(
        self,
        user_store: UserStore,
        session_manager: SessionManager,
        *,
        audit_store: Optional[AuditStore] = None,
    ) -> None:
        self.user_store = user_store
        self.session_manager = session_manager
        self.audit_store = audit_store

    # ------------------------------------------------------------------
    # Login / Logout
    # ------------------------------------------------------------------

    async def handle_login_page(self, request: web.Request) -> web.StreamResponse:
        """GET /login — serve the login page.

        If user is already logged in, redirect to dashboard.
        """
        session = self.session_manager.get_session(request)
        if session:
            return web.HTTPFound("/")

        static_dir = Path(__file__).parent / "static"
        login_html_path = static_dir / "login.html"
        if not login_html_path.is_file():
            return web.Response(
                text="<html><body><h1>Login page not found</h1></body></html>",
                content_type="text/html",
                status=500,
            )
        html = login_html_path.read_text(encoding="utf-8")
        return web.Response(
            text=html,
            content_type="text/html",
            charset="utf-8",
        )

    async def handle_login(self, request: web.Request) -> web.Response:
        """POST /api/login — authenticate with username/password.

        Body (JSON)::

            {"username": "admin", "password": "..."}

        On success, sets HTTP-only session cookie and returns::

            {"success": true, "username": "admin", "role": "admin"}
        """
        try:
            body = await request.json()
        except (json.JSONDecodeError, Exception):
            return web.json_response(
                {"error": "invalid_json", "detail": "Invalid JSON body"},
                status=400,
            )

        username = (body.get("username") or "").strip()
        password = body.get("password", "")

        if not username or not password:
            return web.json_response(
                {"error": "validation_error", "detail": "Username and password are required"},
                status=400,
            )

        user = self.user_store.authenticate(username, password)
        if user is None:
            if self.audit_store is not None:
                self.audit_store.log(
                    event_type=EventType.AUTH_FAILURE.value,
                    actor=f"user:{username}",
                    target="/api/login",
                    detail="Invalid credentials",
                    success=False,
                )
            return web.json_response(
                {"error": "invalid_credentials", "detail": "Invalid username or password"},
                status=401,
            )

        # Create session
        token = self.session_manager.create_session(user.username, user.role)
        resp = web.json_response({
            "success": True,
            "username": user.username,
            "role": user.role,
        })
        self.session_manager.set_session_cookie(resp, token)

        if self.audit_store is not None:
            self.audit_store.log(
                event_type="USER_LOGIN",
                actor=f"user:{user.username}",
                target="/api/login",
                detail=f"role={user.role}",
                success=True,
            )

        return resp

    async def handle_logout(self, request: web.Request) -> web.Response:
        """POST /api/logout — clear session cookie."""
        resp = web.json_response({"success": True, "message": "Logged out"})
        self.session_manager.clear_session_cookie(resp)
        return resp

    # ------------------------------------------------------------------
    # User Management (admin only)
    # ------------------------------------------------------------------

    async def handle_list_users(self, request: web.Request) -> web.Response:
        """GET /admin/users — list all users (admin only)."""
        users = self.user_store.list_users()
        return web.json_response(users)

    async def handle_create_user(self, request: web.Request) -> web.Response:
        """POST /admin/users — create a new user (admin only).

        Body (JSON)::

            {
                "username": "operator1",
                "password": "...",
                "role": "operator"
            }
        """
        try:
            body = await request.json()
        except (json.JSONDecodeError, Exception):
            return web.json_response(
                {"error": "invalid_json", "detail": "Invalid JSON body"},
                status=400,
            )

        username = (body.get("username") or "").strip()
        password = body.get("password", "")
        role = body.get("role", "viewer").strip().lower()

        if not username or not password:
            return web.json_response(
                {"error": "validation_error", "detail": "Username and password are required"},
                status=400,
            )
        if len(password) < 6:
            return web.json_response(
                {"error": "validation_error", "detail": "Password must be at least 6 characters"},
                status=400,
            )

        try:
            user = self.user_store.create_user(username, password, role)
        except ValueError as e:
            return web.json_response(
                {"error": "validation_error", "detail": str(e)},
                status=400,
            )

        if self.audit_store is not None:
            self.audit_store.log(
                event_type="USER_CREATE",
                actor=request.get("username", "unknown"),
                target=user.username,
                detail=f"role={user.role}",
                success=True,
            )

        return web.json_response({
            "username": user.username,
            "role": user.role,
            "created_at": user.created_at,
        }, status=201)

    async def handle_update_user(self, request: web.Request) -> web.Response:
        """PUT /admin/users/{username} — update user password/role (admin only)."""
        target_username = request.match_info.get("username", "")

        try:
            body = await request.json()
        except (json.JSONDecodeError, Exception):
            return web.json_response(
                {"error": "invalid_json", "detail": "Invalid JSON body"},
                status=400,
            )

        password = body.get("password")  # Optional
        role = body.get("role")  # Optional

        if not password and not role:
            return web.json_response(
                {"error": "validation_error", "detail": "At least one of 'password' or 'role' is required"},
                status=400,
            )

        try:
            success = self.user_store.update_user(
                target_username,
                password=password,
                role=role,
            )
        except ValueError as e:
            return web.json_response(
                {"error": "validation_error", "detail": str(e)},
                status=400,
            )

        if not success:
            return web.json_response(
                {"error": "not_found", "detail": f"User '{target_username}' not found"},
                status=404,
            )

        if self.audit_store is not None:
            changed = []
            if password:
                changed.append("password")
            if role:
                changed.append(f"role={role}")
            self.audit_store.log(
                event_type="USER_UPDATE",
                actor=request.get("username", "unknown"),
                target=target_username,
                detail=f"updated: {', '.join(changed)}",
                success=True,
            )

        return web.json_response({"success": True, "username": target_username})

    async def handle_delete_user(self, request: web.Request) -> web.Response:
        """DELETE /admin/users/{username} — delete a user (admin only).

        Cannot delete the 'admin' account itself.
        """
        target_username = request.match_info.get("username", "")

        if target_username == "admin":
            return web.json_response(
                {"error": "protected", "detail": "Cannot delete the primary admin account"},
                status=403,
            )

        # Cannot delete yourself
        current_user = request.get("username", "")
        if target_username == current_user:
            return web.json_response(
                {"error": "protected", "detail": "Cannot delete your own account"},
                status=403,
            )

        success = self.user_store.delete_user(target_username)
        if not success:
            return web.json_response(
                {"error": "not_found", "detail": f"User '{target_username}' not found"},
                status=404,
            )

        if self.audit_store is not None:
            self.audit_store.log(
                event_type="USER_DELETE",
                actor=request.get("username", "unknown"),
                target=target_username,
                detail="Deleted by admin",
                success=True,
            )

        return web.json_response({"success": True, "username": target_username})

    async def handle_get_current_user(self, request: web.Request) -> web.Response:
        """GET /api/me — get current user info from session."""
        session = request.get("user_session")
        if not session:
            return web.json_response(
                {"error": "unauthorized", "detail": "Not authenticated"},
                status=401,
            )
        return web.json_response({
            "username": session.get("sub"),
            "role": session.get("role"),
        })