import time
from collections import defaultdict, deque

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

from app.core.config import settings
from app.core.logging_config import get_logger

logger = get_logger("eve.ratelimit")


class SlidingWindowRateLimiter:
    """In-process sliding-window counter, keyed by client.

    This is per-replica state, which is a deliberate trade-off: it needs no
    round trip to Redis on the hot path, and the README documents that a
    multi-replica deployment needs a shared store instead.
    """

    def __init__(self, limit: int, window_seconds: int) -> None:
        self.limit = limit
        self.window = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._swept_at = 0.0

    def allow(self, key: str) -> tuple[bool, int, int]:
        now = time.monotonic()
        self._sweep(now)
        bucket = self._hits[key]
        cutoff = now - self.window
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if len(bucket) >= self.limit:
            retry_after = max(1, int(self.window - (now - bucket[0])) + 1)
            return False, 0, retry_after
        bucket.append(now)
        remaining = self.limit - len(bucket)
        return True, remaining, 0

    def _sweep(self, now: float) -> None:
        """Drop clients that have gone quiet, at most once per window.

        Without this the key space only ever grows: a request from a new IP adds
        an entry that is never revisited, so a long-lived process accumulates one
        deque per client it has ever seen.
        """
        if now - self._swept_at < self.window:
            return
        self._swept_at = now
        cutoff = now - self.window
        stale = [key for key, bucket in self._hits.items() if not bucket or bucket[-1] < cutoff]
        for key in stale:
            del self._hits[key]

    def reset(self) -> None:
        self._hits.clear()
        self._swept_at = 0.0


class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(
        self,
        app,
        limit: int | None = None,
        window: int | None = None,
        trust_proxy_headers: bool | None = None,
    ) -> None:
        super().__init__(app)
        self.limiter = SlidingWindowRateLimiter(
            limit or settings.rate_limit_requests,
            window or settings.rate_limit_window_seconds,
        )
        self.enabled = (limit or settings.rate_limit_requests) > 0
        if trust_proxy_headers is None:
            trust_proxy_headers = settings.rate_limit_trust_proxy_headers
        self.trust_proxy_headers = trust_proxy_headers

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if not self.enabled or request.url.path in {"/health", "/metrics"}:
            return await call_next(request)

        key = self._key(request)
        allowed, remaining, retry_after = self.limiter.allow(key)
        if not allowed:
            logger.warning("rate_limit_exceeded", extra={"client": key, "path": request.url.path})
            return JSONResponse(
                status_code=429,
                content={
                    "error": {
                        "code": "rate_limit_exceeded",
                        "message": "Too many requests. Please retry later.",
                    }
                },
                headers={
                    "Retry-After": str(retry_after),
                    "X-RateLimit-Limit": str(self.limiter.limit),
                    "X-RateLimit-Remaining": "0",
                },
            )

        response = await call_next(request)
        response.headers["X-RateLimit-Limit"] = str(self.limiter.limit)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        return response

    def _key(self, request: Request) -> str:
        if self.trust_proxy_headers:
            forwarded = request.headers.get("x-forwarded-for")
            if forwarded:
                return f"ip:{forwarded.split(',')[0].strip()}"
        host = request.client.host if request.client else "unknown"
        return f"ip:{host}"
