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
        if "cache-control" not in resp.headers:
            if request.url.path.startswith("/api/"):
                resp.headers["Cache-Control"] = "no-store"     # account data never sits in a cache
            else:
                # Pages and scripts: browsers must re-check with the server (cheap, via ETag),
                # so a new deploy shows up on the next page load instead of an old cached copy.
                resp.headers["Cache-Control"] = "no-cache"
        return resp


class SlidingWindow:
    """Allow `limit` events per `window` seconds per key."""

    def __init__(self, limit: int, window: float):
        self.limit, self.window = limit, window
        self.hits: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        self._prune(now)
        q = self.hits[key]
        while q and now - q[0] > self.window:
            q.popleft()
        if len(q) >= self.limit:
            return False
        q.append(now)
        return True

    def _prune(self, now: float) -> None:
        # Drop keys with no recent hits, so a flood of made-up emails or IPs can't fill memory.
        if len(self.hits) > 10_000:
            for k in [k for k, q in self.hits.items() if not q or now - q[-1] > self.window]:
                del self.hits[k]


class LoginLockout:
    """Lock an email after `max_failures` wrong passwords within `window` seconds, for `lock_s`.

    Keyed on the email (not the IP), so spreading guesses over many IPs doesn't help.
    A successful login clears the count. The same 429 is returned for unknown emails, so a
    lockout doesn't reveal whether an account exists."""

    def __init__(self, max_failures: int = 10, window: float = 900.0, lock_s: float = 900.0):
        self.max_failures, self.window, self.lock_s = max_failures, window, lock_s
        self.failures: dict[str, deque[float]] = defaultdict(deque)
        self.locked_until: dict[str, float] = {}

    def is_locked(self, key: str) -> bool:
        until = self.locked_until.get(key)
        if until is None:
            return False
        if time.monotonic() >= until:
            del self.locked_until[key]
            self.failures.pop(key, None)
            return False
        return True

    def failed(self, key: str) -> None:
        now = time.monotonic()
        if len(self.failures) > 10_000:           # bound memory under a spray of random emails
            for k in [k for k, q in self.failures.items() if not q or now - q[-1] > self.window]:
                del self.failures[k]
        q = self.failures[key]
        while q and now - q[0] > self.window:
            q.popleft()
        q.append(now)
        if len(q) >= self.max_failures:
            self.locked_until[key] = now + self.lock_s

    def succeeded(self, key: str) -> None:
        self.failures.pop(key, None)


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
