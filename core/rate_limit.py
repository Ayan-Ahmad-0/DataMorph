"""Lightweight in-memory sliding-window HTTP rate limiter for public protection."""

from __future__ import annotations

import os
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Deque, Dict, Tuple

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware


@dataclass(frozen=True)
class RateLimitRule:
    """Rate limit threshold for a route tier."""

    name: str
    max_requests: int
    window_seconds: int


class SlidingWindowRateLimiter:
    """Thread-safe in-memory sliding-window rate limiter keyed by (client_ip, tier)."""

    def __init__(self, max_keys: int = 10_000) -> None:
        self._lock = threading.Lock()
        self._buckets: Dict[Tuple[str, str], Deque[float]] = {}
        self._max_keys = max_keys

    def check(self, client_key: str, rule: RateLimitRule) -> Tuple[bool, int]:
        """Record a hit and return (allowed, retry_after_seconds)."""
        now = time.monotonic()
        cutoff = now - rule.window_seconds
        bucket_key = (client_key, rule.name)

        with self._lock:
            if bucket_key not in self._buckets and len(self._buckets) >= self._max_keys:
                self._evict_stale(cutoff)

            dq = self._buckets.setdefault(bucket_key, deque())
            while dq and dq[0] <= cutoff:
                dq.popleft()

            if len(dq) >= rule.max_requests:
                retry_after = max(1, int(rule.window_seconds - (now - dq[0])) + 1)
                return False, retry_after

            dq.append(now)
            return True, 0

    def _evict_stale(self, cutoff: float) -> None:
        stale_keys = [k for k, dq in self._buckets.items() if not dq or dq[-1] <= cutoff]
        for k in stale_keys[: len(self._buckets) // 2 + 1]:
            self._buckets.pop(k, None)
        if len(self._buckets) >= self._max_keys:
            self._buckets.clear()


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default))))
    except ValueError:
        return default


def _get_client_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For", "").strip()
    if forwarded:
        return forwarded.split(",")[0].strip()
    real_ip = request.headers.get("X-Real-IP", "").strip()
    if real_ip:
        return real_ip
    return request.client.host if request.client else "unknown"


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Apply route-tiered rate limits to uploads, AI calls, connectors, and mutations."""

    def __init__(
        self,
        app,
        limiter: SlidingWindowRateLimiter | None = None,
        upload_limit: int | None = None,
        ai_connector_limit: int | None = None,
        mutation_limit: int | None = None,
        window_seconds: int = 60,
    ) -> None:
        super().__init__(app)
        self.limiter = limiter or SlidingWindowRateLimiter()
        self.upload_rule = RateLimitRule(
            "uploads", upload_limit or _env_int("RATE_LIMIT_UPLOADS_PER_MIN", 60), window_seconds
        )
        self.ai_connector_rule = RateLimitRule(
            "ai_connectors", ai_connector_limit or _env_int("RATE_LIMIT_AI_PER_MIN", 60), window_seconds
        )
        self.mutation_rule = RateLimitRule(
            "mutations", mutation_limit or _env_int("RATE_LIMIT_WRITES_PER_MIN", 240), window_seconds
        )

    def _classify_request(self, method: str, path: str) -> RateLimitRule | None:
        if not path.startswith("/api/"):
            return None
        if method in ("GET", "HEAD", "OPTIONS"):
            return None
        if path in (
            "/api/jobs/upload",
            "/api/query/upload",
            "/api/convert",
            "/api/convert/execute",
            "/api/formats/detect",
        ) or (path.startswith("/api/modeling/sessions/") and path.endswith("/tables")):
            return self.upload_rule
        if (
            path.endswith("/ask")
            or "/ai/" in path
            or path.startswith("/api/connectors/")
        ):
            return self.ai_connector_rule
        return self.mutation_rule

    async def dispatch(self, request: Request, call_next):
        enabled = os.getenv("RATE_LIMIT_ENABLED", "true").strip().lower() not in ("0", "false", "no", "off")
        if not enabled:
            return await call_next(request)

        rule = self._classify_request(request.method, request.url.path)
        if rule is None:
            return await call_next(request)

        client_ip = _get_client_ip(request)
        allowed, retry_after = self.limiter.check(client_ip, rule)
        if not allowed:
            return JSONResponse(
                status_code=429,
                content={"detail": f"Rate limit exceeded for {rule.name}. Please retry in {retry_after}s."},
                headers={"Retry-After": str(retry_after)},
            )

        return await call_next(request)
