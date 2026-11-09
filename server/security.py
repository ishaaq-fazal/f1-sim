"""Security middleware: response headers, per-IP rate limiting, origin checks."""
from __future__ import annotations

import time
from collections import defaultdict, deque

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

CSP = "; ".join([
    "default-src 'self'",
    "script-src 'self'",
    "style-src 'self'",
    "connect-src 'self'",
    "img-src 'self' data:",
    "object-src 'none'",
    "base-uri 'none'",
    "frame-ancestors 'none'",
    "form-action 'self'",
])


class SecurityHeaders(BaseHTTPMiddleware):
    def __init__(self, app, hsts: bool = False):
        super().__init__(app)
        self.hsts = hsts

    async def dispatch(self, request: Request, call_next):
        resp = await call_next(request)
        if self.hsts:
            resp.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        resp.headers["Content-Security-Policy"] = CSP
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        return resp


class SlidingWindow:
    """Allow `limit` events per `window` seconds per key."""

    def __init__(self, limit: int, window: float):
        self.limit, self.window = limit, window
        self.hits: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        q = self.hits[key]
        while q and now - q[0] > self.window:
            q.popleft()
        if len(q) >= self.limit:
            return False
        q.append(now)
        return True


class RateLimit(BaseHTTPMiddleware):
    def __init__(self, app, per_minute: int):
        super().__init__(app)
        self.window = SlidingWindow(per_minute, 60.0)

    async def dispatch(self, request: Request, call_next):
        ip = request.client.host if request.client else "unknown"
        if not self.window.allow(ip):
            return JSONResponse({"detail": "Too many requests"}, status_code=429)
        return await call_next(request)


def origin_allowed(origin: str | None, allowed: list[str]) -> bool:
    return origin is not None and origin in allowed


class SameOriginWrites(BaseHTTPMiddleware):
    """CSRF defence in depth (cookies are also SameSite=Strict): state-changing requests
    must come from an allowed Origin and carry JSON."""

    def __init__(self, app, allowed: list[str]):
        super().__init__(app)
        self.allowed = allowed

    async def dispatch(self, request: Request, call_next):
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            origin = request.headers.get("origin")
            if origin is not None and origin not in self.allowed:
                return JSONResponse({"detail": "Bad origin"}, status_code=403)
            ctype = request.headers.get("content-type", "")
            if request.headers.get("content-length", "0") != "0" and not ctype.startswith("application/json"):
                return JSONResponse({"detail": "JSON only"}, status_code=415)
        return await call_next(request)
