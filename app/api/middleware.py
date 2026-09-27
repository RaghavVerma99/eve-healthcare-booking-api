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
    def __init__(self, limit: int, window_seconds: int) -> None:
        self.limit = limit
        self.window = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str) -> tuple[bool, int, int]:
        now = time.monotonic()
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

    def reset(self) -> None:
        self._hits.clear()


class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, limit: int | None = None, window: int | None = None) -> None:
        super().__init__(app)
        self.limiter = SlidingWindowRateLimiter(
            limit or settings.rate_limit_requests,
            window or settings.rate_limit_window_seconds,
        )
        self.enabled = (limit or settings.rate_limit_requests) > 0

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
                headers={"Retry-After": str(retry_after)},
            )

        response = await call_next(request)
        response.headers["X-RateLimit-Limit"] = str(self.limiter.limit)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        return response

    @staticmethod
    def _key(request: Request) -> str:
        identity = getattr(request.state, "user_id", None)
        if identity:
            return f"user:{identity}"
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return f"ip:{forwarded.split(',')[0].strip()}"
        return f"ip:{request.client.host if request.client else 'unknown'}"
