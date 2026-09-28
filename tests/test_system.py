import pytest
from httpx import AsyncClient
from tests.helpers import create_booking, pay_booking


async def test_health_reports_dependencies(client: AsyncClient) -> None:
    response = await client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["checks"]["database"] is True
    assert "timestamp" in body


async def test_openapi_schema_is_served(client: AsyncClient) -> None:
    response = await client.get("/openapi.json")
    assert response.status_code == 200
    schema = response.json()
    assert "bearerAuth" in schema["components"]["securitySchemes"] or "HTTPBearer" in schema[
        "components"
    ]["securitySchemes"]
    for path in ("/auth/login", "/bookings/", "/payments/", "/payments/webhook/"):
        assert path in schema["paths"], path


async def test_unknown_route_returns_structured_404(client: AsyncClient) -> None:
    response = await client.get("/does-not-exist")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"
    assert response.json()["request_id"]


async def test_responses_carry_request_id_header(client: AsyncClient) -> None:
    response = await client.get("/health", headers={"X-Request-ID": "trace-123"})
    assert response.headers["X-Request-ID"] == "trace-123"
    assert "X-Response-Time-ms" in response.headers


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("*", ["*"]),
        ("http://a.test,http://b.test", ["http://a.test", "http://b.test"]),
        (" http://a.test , , http://b.test ", ["http://a.test", "http://b.test"]),
        ("[]", []),
        ("", []),
    ],
)
def test_cors_origins_accepts_a_comma_separated_env_value(
    monkeypatch: pytest.MonkeyPatch, raw: str, expected: list[str]
) -> None:
    # pydantic-settings JSON-decodes list fields unless NoDecode is present, so
    # a bare "*" used to raise SettingsError instead of reaching the validator.
    from app.core.config import Settings

    monkeypatch.setenv("CORS_ORIGINS", raw)
    assert Settings(_env_file=None).cors_origins == expected


def test_cors_origins_defaults_to_denying_every_origin() -> None:
    from app.core.config import Settings

    assert Settings(_env_file=None).cors_origins == []


@pytest.mark.parametrize(
    "origins,expected_credentials",
    [
        (["*"], False),
        (["https://app.eve.health"], True),
    ],
)
def test_cors_middleware_uses_configured_origins(
    monkeypatch: pytest.MonkeyPatch, origins: list[str], expected_credentials: bool
) -> None:
    from app.core.config import settings
    from app.core.handlers import register_middleware
    from fastapi import FastAPI
    from fastapi.middleware.cors import CORSMiddleware

    monkeypatch.setattr(settings, "cors_origins", origins)
    app = FastAPI()
    register_middleware(app)

    cors = [m for m in app.user_middleware if m.cls is CORSMiddleware]
    assert len(cors) == 1
    assert cors[0].kwargs["allow_origins"] == origins
    # The CORS spec forbids credentials alongside "*", so an allowlist is
    # required before allow_credentials may be true.
    assert cors[0].kwargs["allow_credentials"] is expected_credentials


def test_sliding_window_rate_limiter_blocks_after_limit() -> None:
    from app.api.middleware import SlidingWindowRateLimiter

    limiter = SlidingWindowRateLimiter(limit=3, window_seconds=60)

    results = [limiter.allow("client-a") for _ in range(3)]
    assert [allowed for allowed, _, _ in results] == [True, True, True]
    assert results[-1][1] == 0

    allowed, remaining, retry_after = limiter.allow("client-a")
    assert allowed is False
    assert remaining == 0
    assert retry_after > 0

    assert limiter.allow("client-b")[0] is True


def test_sliding_window_rate_limiter_is_per_key() -> None:
    from app.api.middleware import SlidingWindowRateLimiter

    limiter = SlidingWindowRateLimiter(limit=1, window_seconds=60)
    assert limiter.allow("a")[0] is True
    assert limiter.allow("a")[0] is False
    assert limiter.allow("b")[0] is True


def test_sliding_window_rate_limiter_resets() -> None:
    from app.api.middleware import SlidingWindowRateLimiter

    limiter = SlidingWindowRateLimiter(limit=1, window_seconds=60)
    limiter.allow("a")
    assert limiter.allow("a")[0] is False
    limiter.reset()
    assert limiter.allow("a")[0] is True


def test_sliding_window_rate_limiter_forgets_quiet_clients(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A client that stops calling must not be retained forever."""
    from app.api import middleware as middleware_module
    from app.api.middleware import SlidingWindowRateLimiter

    now = [1_000.0]
    monkeypatch.setattr(middleware_module.time, "monotonic", lambda: now[0])

    limiter = SlidingWindowRateLimiter(limit=100, window_seconds=60)
    for index in range(500):
        limiter.allow(f"10.0.0.{index}")
    assert len(limiter._hits) == 500

    # Two windows later every one of those clients is idle and must be gone.
    now[0] += 120
    limiter.allow("10.0.0.0")
    assert len(limiter._hits) == 1


def test_rate_limiter_ignores_forwarded_headers_by_default() -> None:
    """X-Forwarded-For is client-controlled, so it must not key the limiter."""
    from app.api.middleware import RateLimitMiddleware
    from starlette.datastructures import Headers
    from starlette.requests import Request

    def build(forwarded: str | None) -> Request:
        raw_headers = Headers({"x-forwarded-for": forwarded} if forwarded else {})
        return Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/",
                "headers": raw_headers.raw,
                "client": ("10.1.2.3", 1234),
            }
        )

    untrusted = RateLimitMiddleware(app=None, trust_proxy_headers=False)
    assert untrusted._key(build("1.1.1.1")) == untrusted._key(build("2.2.2.2"))

    trusted = RateLimitMiddleware(app=None, trust_proxy_headers=True)
    assert trusted._key(build("1.1.1.1")) != trusted._key(build("2.2.2.2"))


async def test_confirmed_booking_cannot_be_cancelled_by_user(
    client: AsyncClient, auth_headers, catalogue
) -> None:
    booking = await create_booking(client, auth_headers, catalogue)
    await pay_booking(client, auth_headers, booking["id"])

    response = await client.post(f"/bookings/{booking['id']}/cancel", headers=auth_headers)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "booking_not_cancellable"


async def test_booking_delete_is_not_a_cancel_alias(
    client: AsyncClient, auth_headers, catalogue
) -> None:
    """DELETE implies destroying a resource; a booking can only be cancelled.

    Keeping a cancel behind DELETE returned 200 for a request that changed
    nothing, so the route is gone rather than aliased.
    """
    booking = await create_booking(client, auth_headers, catalogue)

    response = await client.delete(f"/bookings/{booking['id']}", headers=auth_headers)
    assert response.status_code == 405
    assert response.json()["error"]["code"] == "method_not_allowed"

    detail = await client.get(f"/bookings/{booking['id']}", headers=auth_headers)
    assert detail.json()["status"] == "PENDING"
