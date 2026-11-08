"""HTTP security middleware: API-key authentication, rate limiting, upload size gate.

All middleware is pure ASGI so streaming responses (SSE) are passed through
without buffering. Rate-limit state is process-local; run a single worker or
move the counters to a shared store if the backend is scaled out.
"""

from __future__ import annotations

import logging
import secrets
import threading
import time
from collections import deque
from typing import Deque, Dict, Tuple

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.core.config import settings

logger = logging.getLogger(__name__)

_AUTH_EXEMPT_PATHS = frozenset(
    {"/health", "/openapi.json", "/docs", "/redoc", "/docs/oauth2-redirect"}
)
_CHAT_PATHS = frozenset(
    {"/api/chat", "/api/chat/stream", "/extract_customs", "/api/extract_customs"}
)
_UPLOAD_PATH = "/api/upload"
# Multipart boundaries and form headers are not part of the file itself.
_MULTIPART_OVERHEAD_BYTES = 64 * 1024


def _normalized_path(scope: Scope) -> str:
    path = str(scope.get("path") or "/")
    if len(path) > 1:
        path = path.rstrip("/") or "/"
    return path


def _client_ip(scope: Scope) -> str:
    if settings.TRUST_PROXY_HEADERS:
        forwarded = Headers(scope=scope).get("x-forwarded-for")
        if forwarded:
            first = forwarded.split(",", 1)[0].strip()
            if first:
                return first
    client = scope.get("client")
    return client[0] if client else "unknown"


def _provided_api_key(headers: Headers) -> str:
    header_value = headers.get(settings.API_KEY_HEADER)
    if header_value:
        return header_value.strip()
    authorization = headers.get("authorization", "")
    scheme, _, credentials = authorization.partition(" ")
    if scheme.lower() == "bearer" and credentials.strip():
        return credentials.strip()
    return ""


def _keys_match(provided: str, expected: str) -> bool:
    try:
        return secrets.compare_digest(
            provided.encode("utf-8"), expected.encode("utf-8")
        )
    except (TypeError, UnicodeError):
        return False


class ApiKeyAuthMiddleware:
    """Require the configured API key on every non-exempt HTTP request."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not settings.API_KEY:
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "")
        path = _normalized_path(scope)
        if method == "OPTIONS" or path in _AUTH_EXEMPT_PATHS:
            await self.app(scope, receive, send)
            return

        provided = _provided_api_key(Headers(scope=scope))
        if provided and _keys_match(provided, settings.API_KEY):
            await self.app(scope, receive, send)
            return

        logger.warning(
            "Rejected unauthenticated request: %s %s from %s", method, path, _client_ip(scope)
        )
        response = JSONResponse(
            {"detail": "缺少或无效的 API Key"},
            status_code=401,
            headers={
                "WWW-Authenticate": f'ApiKey header="{settings.API_KEY_HEADER}"',
            },
        )
        await response(scope, receive, send)


class RateLimitMiddleware:
    """Sliding-window request limit per client IP and route category."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self._hits: Dict[Tuple[str, str], Deque[float]] = {}
        self._lock = threading.Lock()
        self._last_cleanup = time.monotonic()

    def _cleanup_locked(self, now: float) -> None:
        if now - self._last_cleanup >= 300:
            self._last_cleanup = now
            cutoff = now - settings.RATE_LIMIT_WINDOW_SECONDS
            stale_keys = [
                key
                for key, hits in self._hits.items()
                if not hits or hits[-1] <= cutoff
            ]
            for key in stale_keys:
                self._hits.pop(key, None)
        if len(self._hits) > settings.RATE_LIMIT_MAX_TRACKED_CLIENTS:
            # Bounded memory under address-spoofing floods; counters reset rather
            # than growing without limit.
            logger.warning("Rate-limit client table exceeded its cap; clearing counters")
            self._hits.clear()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not settings.RATE_LIMIT_ENABLED:
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "")
        path = _normalized_path(scope)
        if method == "OPTIONS" or path in _AUTH_EXEMPT_PATHS:
            await self.app(scope, receive, send)
            return

        bucket, limit = _limit_for_path(path)
        client = _client_ip(scope)
        now = time.monotonic()
        window = settings.RATE_LIMIT_WINDOW_SECONDS
        retry_after = 0

        with self._lock:
            self._cleanup_locked(now)
            key = (client, bucket)
            hits = self._hits.get(key)
            if hits is None:
                hits = deque()
                self._hits[key] = hits
            cutoff = now - window
            while hits and hits[0] <= cutoff:
                hits.popleft()
            if len(hits) >= limit:
                retry_after = max(1, int(hits[0] + window - now) + 1)
            else:
                hits.append(now)

        if retry_after:
            response = JSONResponse(
                {"detail": f"请求过于频繁，请 {retry_after} 秒后重试"},
                status_code=429,
                headers={
                    "Retry-After": str(retry_after),
                    "X-RateLimit-Limit": str(limit),
                    "X-RateLimit-Remaining": "0",
                },
            )
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)


class UploadSizeLimitMiddleware:
    """Reject oversized uploads before the multipart body is parsed and spooled."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] == "http"
            and scope.get("method") == "POST"
            and _normalized_path(scope) == _UPLOAD_PATH
        ):
            raw_length = Headers(scope=scope).get("content-length")
            if not raw_length:
                # A chunked upload would bypass the byte check below; require a
                # declared length so an oversized body is rejected before spooling.
                response = JSONResponse(
                    {"detail": "上传请求必须提供 Content-Length"},
                    status_code=411,
                    headers={"Connection": "close"},
                )
                await response(scope, receive, send)
                return
            try:
                content_length = int(raw_length)
            except ValueError:
                content_length = None
            if content_length is None or content_length < 0:
                response = JSONResponse(
                    {"detail": "Content-Length 无效"},
                    status_code=400,
                    headers={"Connection": "close"},
                )
                await response(scope, receive, send)
                return
            if content_length > settings.max_upload_bytes + _MULTIPART_OVERHEAD_BYTES:
                response = JSONResponse(
                    {"detail": f"文件大小超过 {settings.MAX_UPLOAD_MB} MB 限制"},
                    status_code=413,
                    headers={"Connection": "close"},
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def _limit_for_path(path: str) -> Tuple[str, int]:
    if path == _UPLOAD_PATH:
        return "upload", settings.RATE_LIMIT_UPLOAD_REQUESTS
    if path in _CHAT_PATHS:
        return "chat", settings.RATE_LIMIT_CHAT_REQUESTS
    return "default", settings.RATE_LIMIT_DEFAULT_REQUESTS
