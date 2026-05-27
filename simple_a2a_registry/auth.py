"""OAuth 2.1 authentication — JWT tokens, client registry, and auth middleware.

Provides:
- ``create_token()`` / ``verify_token()`` — JWT sign/verify (RS256 primary, HS256 fallback)
- ``AuthStore`` — re-exported from ``store.py`` for backward compatibility
- ``AuthHandler`` — aiohttp handlers for ``/auth/*`` token and registration endpoints
- ``AuthMiddleware`` — aiohttp middleware that validates Bearer tokens
- ``require_scope`` — view-level decorator for granular scope enforcement

The persistent layer (``Store`` / ``AuthStore``) now lives in ``store.py``.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any, Callable, Dict, List, Optional, Set

import jwt
from aiohttp import web

# Re-export Store as AuthStore for backward compatibility.
# The unified ``Store`` class in ``store.py`` now provides both registry
# and auth persistence, but code that imports ``AuthStore`` from ``auth``
# still works.
from simple_a2a_registry.store import Store as AuthStore  # noqa: F401
from simple_a2a_registry.store import ClientRecord, TokenRecord  # noqa: F401
from simple_a2a_registry.store import SCOPES, AUTH_CODE_EXPIRY_SECONDS
from simple_a2a_registry.audit import AuditStore, EventType
from simple_a2a_registry.metrics import record_auth_operation

logger = logging.getLogger("a2a_registry.auth")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TOKEN_EXPIRY_SECONDS = 3600  # 1 hour

ISSUER = "simple-a2a-registry"


# ---------------------------------------------------------------------------
# Key management
# ---------------------------------------------------------------------------


def _generate_rsa_keypair() -> tuple[str, str]:
    """Generate an RSA-256 key pair using PyJWT's built-in key generation.

    Returns:
        ``(private_key_pem, public_key_pem)``
    """
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
    )
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")

    public_key = private_key.public_key()
    public_pem = public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("utf-8")

    return private_pem, public_pem


# ---------------------------------------------------------------------------
# JWT helpers
# ---------------------------------------------------------------------------


def create_token(
    sub: str,
    *,
    private_key: str,
    algorithm: str = "RS256",
    audience: Optional[List[str]] = None,
    scope: Optional[str] = None,
    expiry: int = TOKEN_EXPIRY_SECONDS,
    issuer: str = ISSUER,
) -> str:
    """Create a signed JWT access token.

    Args:
        sub: Subject (client_id / agent_id).
        private_key: PEM-encoded private key (RS256) or shared secret (HS256).
        algorithm: JWT signing algorithm (``RS256`` or ``HS256``).
        audience: List of intended audiences.
        scope: Space-separated scope string.
        expiry: Token lifetime in seconds.
        issuer: Token issuer claim.

    Returns:
        Encoded JWT string.
    """
    now = int(time.time())
    payload: Dict[str, Any] = {
        "iss": issuer,
        "sub": sub,
        "iat": now,
        "exp": now + expiry,
        "jti": str(uuid.uuid4()),
    }
    if audience:
        payload["aud"] = audience
    if scope:
        payload["scope"] = scope

    if algorithm == "RS256":
        return _rsa_sign_jwt(payload, private_key)
    else:
        return jwt.encode(payload, private_key, algorithm=algorithm)


def verify_token(
    token: str,
    *,
    public_key: str,
    algorithm: str = "RS256",
    audience: Optional[List[str]] = None,
    issuer: str = ISSUER,
) -> Optional[Dict[str, Any]]:
    """Verify and decode a JWT access token.

    Args:
        token: Encoded JWT string.
        public_key: PEM-encoded public key (RS256) or shared secret (HS256).
        algorithm: Expected signing algorithm.
        audience: Expected audience(s) — verified if provided.
        issuer: Expected issuer.

    Returns:
        Decoded payload dict, or ``None`` if verification fails.
    """
    if algorithm == "RS256":
        return _rsa_verify_jwt(token, public_key, audience, issuer)
    try:
        payload = jwt.decode(
            token,
            public_key,
            algorithms=[algorithm],
            audience=audience,
            issuer=issuer,
            options={"verify_exp": True},
        )
        return payload
    except jwt.ExpiredSignatureError:
        logger.warning("Token expired")
        return None
    except jwt.InvalidAudienceError:
        logger.warning("Token audience mismatch")
        return None
    except jwt.InvalidIssuerError:
        logger.warning("Token issuer mismatch")
        return None
    except jwt.InvalidTokenError as e:
        logger.warning("Invalid token: %s", e)
        return None


# ---------------------------------------------------------------------------
# RSA JWT sign/verify (cryptography-backed, avoids pyjwt + cryptography 46.x bug)
# ---------------------------------------------------------------------------


def _rsa_sign_jwt(payload: Dict[str, Any], private_key_pem: str) -> str:
    """Sign a JWT payload using RSA-SHA256 and return the encoded token.

    Uses cryptography directly instead of pyjwt to work around a
    compatibility issue between PyJWT 2.12.x and cryptography >= 46
    where RSAPrivateKey.verify() was removed.
    """
    from cryptography.hazmat.primitives import serialization, hashes
    from cryptography.hazmat.primitives.asymmetric import padding as asym_padding

    header = {"alg": "RS256", "typ": "JWT"}

    # Encode header and payload as base64url
    def _b64encode(data: bytes) -> str:
        import base64
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")

    header_b64 = _b64encode(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    payload_b64 = _b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))

    # Load private key
    private_key = serialization.load_pem_private_key(
        private_key_pem.encode("utf-8"),
        password=None,
    )

    # Sign
    message = f"{header_b64}.{payload_b64}".encode("utf-8")
    signature = private_key.sign(message, asym_padding.PKCS1v15(), hashes.SHA256())
    sig_b64 = _b64encode(signature)

    return f"{header_b64}.{payload_b64}.{sig_b64}"


def _rsa_verify_jwt(
    token: str,
    public_key_pem: str,
    audience: Optional[List[str]] = None,
    issuer: str = ISSUER,
) -> Optional[Dict[str, Any]]:
    """Verify and decode an RSA-SHA256 JWT token."""
    from cryptography.hazmat.primitives import serialization, hashes
    from cryptography.hazmat.primitives.asymmetric import padding as asym_padding

    import base64

    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        header_b64, payload_b64, sig_b64 = parts

        # Decode header (validate alg)
        def _b64decode(data: str) -> bytes:
            padding_len = 4 - len(data) % 4
            if padding_len != 4:
                data += "=" * padding_len
            return base64.urlsafe_b64decode(data)

        header = json.loads(_b64decode(header_b64))
        if header.get("alg") != "RS256":
            logger.warning("Unexpected JWT algorithm: %s", header.get("alg"))
            return None

        # Decode payload
        payload = json.loads(_b64decode(payload_b64))

        # Verify expiration
        now = time.time()
        exp = payload.get("exp")
        if exp and now > exp:
            logger.warning("Token expired")
            return None

        # Verify issuer
        if payload.get("iss") != issuer:
            logger.warning("Token issuer mismatch")
            return None

        # Verify audience
        if audience is not None:
            token_aud = payload.get("aud", [])
            if isinstance(token_aud, str):
                token_aud = [token_aud]
            if not any(a in token_aud for a in audience):
                logger.warning("Token audience mismatch")
                return None

        # Verify signature
        public_key = serialization.load_pem_public_key(
            public_key_pem.encode("utf-8"),
        )
        message = f"{header_b64}.{payload_b64}".encode("utf-8")
        signature = _b64decode(sig_b64)

        public_key.verify(signature, message, asym_padding.PKCS1v15(), hashes.SHA256())

        return payload

    except (json.JSONDecodeError, ValueError, TypeError) as e:
        logger.warning("Invalid JWT: %s", e)
        return None
    except Exception as e:
        # Catch cryptography InvalidSignature and any other edge cases
        logger.warning("JWT verification failed: %s", e)
        return None


# ---------------------------------------------------------------------------
# AuthMiddleware — aiohttp middleware
# ---------------------------------------------------------------------------


def _auth_middleware_factory(
    auth_store: AuthStore,
    *,
    enabled: bool,
    public_key: str,
    algorithm: str = "RS256",
    issuer: str = ISSUER,
    audit_store: Optional[AuditStore] = None,
) -> Callable:
    """Create an aiohttp auth middleware.

    The middleware skips authentication for:
    - ``/auth/*`` — token endpoint is public
    - ``/.well-known/*`` — discovery endpoints are public
    - ``/health`` — health check is public
    - ``/login``, ``/api/login``, ``/api/logout`` — user login is public
    - ``/api/me`` — current user info
    - ``/favicon.ico``, ``/static/login.html`` — static assets
    - ``/`` — dashboard is public

    When ``enabled=False``, the middleware is a no-op pass-through.

    Args:
        audit_store: Optional :class:`AuditStore` for logging AUTH_FAILURE events.
    """

    @web.middleware
    async def auth_middleware(
        request: web.Request, handler: Any,
    ) -> web.StreamResponse:
        if not enabled:
            # Auth disabled — grant all scopes so require_scope passes through
            request["agent_id"] = "anonymous"
            request["token_scopes"] = " ".join(SCOPES.keys())
            request["token_payload"] = {}
            return await handler(request)

        path = request.path

        # Public paths — skip authentication
        if (
            path.startswith("/auth/")
            or path.startswith("/.well-known/")
            or path == "/health"
            or path == "/"
            or path == "/metrics"
            or path == "/login"
            or path == "/api/login"
            or path == "/api/logout"
            or path == "/api/me"
            or path == "/favicon.ico"
            or path == "/static/login.html"
            # WebSocket upgrade — token passed via ?token=xxx query param
            or path.endswith("/ws")
            # JWKS endpoint — public key distribution
            or path == "/.well-known/jwks.json"
        ):
            return await handler(request)

        # Bearer token validation
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            record_auth_operation("token_validate", success=False)
            if audit_store is not None:
                audit_store.log(
                    event_type=EventType.AUTH_FAILURE.value,
                    actor=request.remote or "unknown",
                    target=request.path,
                    detail="Missing or invalid Authorization header",
                    success=False,
                )
            return web.json_response(
                {"error": "unauthorized", "detail": "Missing or invalid Authorization header"},
                status=401,
                headers={"WWW-Authenticate": 'Bearer realm="simple-a2a-registry"'},
            )

        token = auth_header[7:]  # strip "Bearer "
        payload = verify_token(
            token,
            public_key=public_key,
            algorithm=algorithm,
            issuer=issuer,
        )
        if payload is None:
            record_auth_operation("token_validate", success=False)
            if audit_store is not None:
                audit_store.log(
                    event_type=EventType.AUTH_FAILURE.value,
                    actor=request.remote or "unknown",
                    target=request.path,
                    detail="Token expired or invalid",
                    success=False,
                )
            return web.json_response(
                {"error": "invalid_token", "detail": "Token expired or invalid"},
                status=401,
                headers={"WWW-Authenticate": 'Bearer realm="simple-a2a-registry", error="invalid_token"'},
            )

        # Inject agent_id into request for downstream handlers
        request["agent_id"] = payload.get("sub", "")
        request["token_scopes"] = payload.get("scope", "")
        request["token_payload"] = payload

        record_auth_operation("token_validate", success=True)
        return await handler(request)

    return auth_middleware


# ---------------------------------------------------------------------------
# require_scope — view-level decorator
# ---------------------------------------------------------------------------


def require_scope(*required_scopes: str) -> Callable:
    """Decorator that checks the authenticated request has the required scopes.

    Usage::

        @require_scope("task:write")
        async def handle_create_task(request: web.Request) -> web.Response:
            ...

    Returns 403 if the token doesn't have the required scopes.
    """
    def decorator(handler: Callable) -> Callable:
        async def wrapper(request: web.Request, *args: Any, **kwargs: Any) -> web.StreamResponse:
            # Auth middleware injects token_scopes; if not present, check
            # was not performed — require auth
            token_scopes = request.get("token_scopes", "")
            token_scopes_set: Set[str] = set(token_scopes.split()) if token_scopes else set()
            missing = [s for s in required_scopes if s not in token_scopes_set]
            if missing:
                return web.json_response(
                    {
                        "error": "insufficient_scope",
                        "detail": f"Missing required scope(s): {', '.join(missing)}",
                    },
                    status=403,
                    headers={"WWW-Authenticate": f'Bearer realm="simple-a2a-registry", error="insufficient_scope", scope="{" ".join(required_scopes)}"'},
                )
            return await handler(request, *args, **kwargs)
        return wrapper
    return decorator


# ---------------------------------------------------------------------------
# Token endpoint handlers
# ---------------------------------------------------------------------------


class AuthHandler:
    """Handlers for ``/auth/*`` endpoints."""

    def __init__(
        self,
        auth_store: AuthStore,
        *,
        private_key: str,
        algorithm: str = "RS256",
        base_url: str = "http://localhost:8321",
        audit_store: Optional[AuditStore] = None,
    ) -> None:
        self.auth_store = auth_store
        self.private_key = private_key
        self.algorithm = algorithm
        self.base_url = base_url.rstrip("/")
        self.audit_store = audit_store

    async def handle_register(self, request: web.Request) -> web.Response:
        """POST /auth/register — register a new OAuth 2.1 client.

        Body (JSON)::

            {
                "agent_card_id": "...",
                "allowed_scopes": ["task:read", "task:write"],
                "description": "My A2A Agent"
            }

        Returns::

            {
                "client_id": "client-abc123",
                "client_secret": "secret-xyz...",
                "allowed_scopes": ["task:read", "task:write"]
            }
        """
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response(
                {"error": "invalid_json", "detail": "Invalid JSON body"},
                status=400,
            )

        agent_card_id = body.get("agent_card_id", "")
        allowed_scopes = body.get("allowed_scopes")
        description = body.get("description", "")

        # Validate scopes
        if allowed_scopes is not None:
            valid_scopes = set(SCOPES.keys())
            for s in allowed_scopes:
                if s not in valid_scopes:
                    return web.json_response(
                        {"error": "invalid_scope", "detail": f"Unknown scope: {s}"},
                        status=400,
                    )
        result = self.auth_store.register_client(
            agent_card_id=agent_card_id,
            allowed_scopes=allowed_scopes,
            description=description,
        )

        if self.audit_store is not None:
            self.audit_store.log(
                event_type=EventType.CLIENT_CREATE.value,
                actor=request.remote or "unknown",
                target=result.get("client_id", "unknown"),
                detail=f"agent_card_id={agent_card_id} scopes={allowed_scopes or list(SCOPES.keys())}",
                success=True,
            )

        return web.json_response(
            {
                "client_id": result["client_id"],
                "client_secret": result["client_secret"],
                "allowed_scopes": allowed_scopes or list(SCOPES.keys()),
            },
            status=201,
        )

    async def handle_token(self, request: web.Request) -> web.Response:
        """POST /auth/token — issue an access token.

        Supports:
        - ``grant_type=client_credentials``
        - ``grant_type=authorization_code`` (with PKCE)

        Request (form-encoded)::

            grant_type=client_credentials
            &client_id=client-abc123
            &client_secret=secret-xyz...
            &scope=task:read+task:write

        Or::

            grant_type=authorization_code
            &client_id=client-abc123
            &client_secret=secret-xyz...
            &code=abc...
            &code_verifier=xyz...
            &redirect_uri=https://agent.example/callback
        """
        try:
            data = await request.post()
        except Exception:
            return web.json_response(
                {"error": "invalid_request", "detail": "Could not parse form data"},
                status=400,
            )

        grant_type = str(data.get("grant_type", ""))
        if grant_type not in ("client_credentials", "authorization_code"):
            return web.json_response(
                {"error": "unsupported_grant_type",
                 "detail": f"Grant type '{grant_type}' not supported. "
                           f"Supported: client_credentials, authorization_code"},
                status=400,
            )

        client_id = str(data.get("client_id", ""))
        client_secret = str(data.get("client_secret", ""))

        # Validate client credentials
        if not self.auth_store.verify_client_secret(client_id, client_secret):
            return web.json_response(
                {"error": "invalid_client", "detail": "Invalid client credentials"},
                status=401,
            )

        if grant_type == "client_credentials":
            return await self._handle_client_credentials(client_id, data)
        else:
            return await self._handle_authorization_code(client_id, data)

    async def _handle_client_credentials(
        self, client_id: str, data: Any,
    ) -> web.Response:
        scope = str(data.get("scope", ""))
        if scope and not self.auth_store.client_allowed_scopes(client_id, scope):
            return web.json_response(
                {"error": "invalid_scope",
                 "detail": "Requested scope(s) not allowed for this client"},
                status=400,
            )
        if not scope:
            # Default to all allowed scopes
            client = self.auth_store.get_client(client_id)
            if client:
                scope = " ".join(client.allowed_scopes)

        token = create_token(
            client_id,
            private_key=self.private_key,
            algorithm=self.algorithm,
            scope=scope,
        )

        # Decode to get jti for storage
        payload = jwt.decode(
            token, options={"verify_signature": False},
        )
        self.auth_store.record_token(payload)

        if self.audit_store is not None:
            self.audit_store.log(
                event_type=EventType.TOKEN_ISSUE.value,
                actor=client_id,
                target=scope or "all",
                detail=f"grant_type=client_credentials jti={payload.get('jti', '')}",
                success=True,
            )

        return web.json_response(
            {
                "access_token": token,
                "token_type": "Bearer",
                "expires_in": TOKEN_EXPIRY_SECONDS,
                "scope": scope,
            },
            status=200,
        )

    async def _handle_authorization_code(
        self, client_id: str, data: Any,
    ) -> web.Response:
        code = str(data.get("code", ""))
        code_verifier = str(data.get("code_verifier", ""))
        redirect_uri = str(data.get("redirect_uri", ""))

        if not code or not code_verifier:
            return web.json_response(
                {"error": "invalid_request",
                 "detail": "authorization_code grant requires 'code' and 'code_verifier'"},
                status=400,
            )

        auth_data = self.auth_store.consume_auth_code(code, code_verifier)
        if auth_data is None:
            return web.json_response(
                {"error": "invalid_grant", "detail": "Invalid or expired authorization code"},
                status=400,
            )

        if auth_data["redirect_uri"] != redirect_uri:
            return web.json_response(
                {"error": "invalid_grant", "detail": "redirect_uri mismatch"},
                status=400,
            )

        scope = auth_data.get("scope", "")
        token = create_token(
            client_id,
            private_key=self.private_key,
            algorithm=self.algorithm,
            scope=scope,
        )

        payload = jwt.decode(token, options={"verify_signature": False})
        self.auth_store.record_token(payload)

        if self.audit_store is not None:
            self.audit_store.log(
                event_type=EventType.TOKEN_ISSUE.value,
                actor=client_id,
                target=scope or "all",
                detail=f"grant_type=authorization_code jti={payload.get('jti', '')}",
                success=True,
            )

        return web.json_response(
            {
                "access_token": token,
                "token_type": "Bearer",
                "expires_in": TOKEN_EXPIRY_SECONDS,
                "scope": scope,
            },
            status=200,
        )

    async def handle_well_known_oauth(self, request: web.Request) -> web.Response:
        """GET /.well-known/oauth-authorization-server — RFC 8414 metadata."""
        issuer_url = f"{self.base_url}"
        return web.json_response(
            {
                "issuer": issuer_url,
                "authorization_endpoint": f"{self.base_url}/auth/authorize",
                "token_endpoint": f"{self.base_url}/auth/token",
                "registration_endpoint": f"{self.base_url}/auth/register",
                "jwks_uri": f"{self.base_url}/.well-known/jwks.json",
                "response_types_supported": ["code"],
                "grant_types_supported": [
                    "client_credentials",
                    "authorization_code",
                ],
                "code_challenge_methods_supported": ["S256", "plain"],
                "token_endpoint_auth_methods_supported": [
                    "client_secret_basic",
                    "client_secret_post",
                ],
                "scopes_supported": list(SCOPES.keys()),
                "claims_supported": [
                    "iss", "sub", "aud", "exp", "iat", "scope", "jti",
                ],
            },
            headers={"Cache-Control": "public, max-age=86400"},
        )


# ---------------------------------------------------------------------------
# JWKS endpoint
# ---------------------------------------------------------------------------


def make_jwks_endpoint(public_key_pem: str) -> Callable:
    """Create a handler for ``/.well-known/jwks.json``.

    Returns the public RSA key in JWKS format so clients can verify
    token signatures locally.
    """
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.backends import default_backend
    import base64

    public_key = serialization.load_pem_public_key(
        public_key_pem.encode("utf-8"),
        backend=default_backend(),
    )
    if not isinstance(public_key, rsa.RSAPublicKey):
        raise TypeError("Expected RSA public key")

    pub_numbers = public_key.public_numbers()

    def _b64url(n: int) -> str:
        byte_length = (n.bit_length() + 7) // 8
        return base64.urlsafe_b64encode(
            n.to_bytes(byte_length, byteorder="big")
        ).rstrip(b"=").decode("ascii")

    jwks = {
        "keys": [
            {
                "kty": "RSA",
                "use": "sig",
                "alg": "RS256",
                "n": _b64url(pub_numbers.n),
                "e": _b64url(pub_numbers.e),
            }
        ]
    }

    async def jwks_handler(request: web.Request) -> web.Response:
        return web.json_response(
            jwks,
            headers={"Cache-Control": "public, max-age=86400"},
        )

    return jwks_handler
