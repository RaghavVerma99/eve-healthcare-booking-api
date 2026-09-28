import time
import uuid
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.config import settings
from app.core.exceptions import AppError
from app.core.logging_config import configure_logging, get_logger

logger = get_logger("eve.request")


def _error_response(
    request: Request,
    *,
    status_code: int,
    code: str,
    message: str,
    details: Any = None,
) -> JSONResponse:
    request_id = getattr(request.state, "request_id", None)
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {"code": code, "message": message, "details": details},
            "request_id": request_id,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z",
        },
    )


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        logger.warning(
            "domain_error",
            extra={
                "code": exc.code,
                "status_code": exc.status_code,
                "path": request.url.path,
                "method": request.method,
                "detail": exc.message,
            },
        )
        return _error_response(
            request,
            status_code=exc.status_code,
            code=exc.code,
            message=exc.message,
            details=exc.details,
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return _error_response(
            request,
            status_code=422,
            code="validation_error",
            message="Request payload failed validation.",
            details=_safe_errors(exc.errors()),
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_exception(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        codes = {401: "unauthorized", 403: "forbidden", 404: "not_found", 405: "method_not_allowed"}
        return _error_response(
            request,
            status_code=exc.status_code,
            code=codes.get(exc.status_code, "http_error"),
            message=str(exc.detail),
        )

    @app.exception_handler(HTTPException)
    async def handle_fastapi_http_exception(
        request: Request, exc: HTTPException
    ) -> JSONResponse:
        return await handle_http_exception(request, exc)

    @app.exception_handler(Exception)
    async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        logger.exception(
            "unhandled_exception",
            extra={"path": request.url.path, "method": request.method},
        )
        return _error_response(
            request,
            status_code=500,
            code="internal_error",
            message="An unexpected error occurred.",
        )


def _json_safe(value: Any) -> Any:
    """Coerce a value out of pydantic's error dict into something json.dumps accepts.

    `exc.errors()` carries the offending `input` verbatim, and for a body that
    failed to parse that input is the raw request body as `bytes`. Returning it
    unchanged made json.dumps raise inside the error handler, so the handler
    itself failed: a malformed request got a 500 and a stack trace instead of
    the 422 it should have, and the client learned nothing about what was wrong.
    """
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    if isinstance(value, bytes | bytearray):
        # Deliberately not echoed back. A body that fails to parse can be a
        # password, and a 422 is not the place to reflect it.
        return f"<{len(value)} bytes of unparseable body>"
    if isinstance(value, list | tuple):
        return [_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    return f"<{type(value).__name__}>"


def _safe_errors(errors: list[dict]) -> list[dict]:
    cleaned = []
    for error in errors:
        item = {key: value for key, value in error.items() if key != "ctx"}
        item["loc"] = [str(part) for part in error.get("loc", ())]
        ctx = error.get("ctx")
        if isinstance(ctx, dict) and "error" in ctx:
            item["msg"] = str(ctx["error"])
        cleaned.append(_json_safe(item))
    return cleaned


def register_middleware(app: FastAPI) -> None:
    from app.api.middleware import RateLimitMiddleware

    app.add_middleware(RateLimitMiddleware)
    # The CORS spec forbids credentials alongside a wildcard origin, so
    # credentials are enabled only for an explicit allowlist.
    allow_credentials = "*" not in settings.cors_origins
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=allow_credentials,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        request.state.request_id = request_id
        started = time.perf_counter()
        response = await call_next(request)
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Response-Time-ms"] = str(duration_ms)
        logger.info(
            "request_completed",
            extra={
                "request_id": request_id,
                "method": request.method,
                "path": request.url.path,
                "status_code": response.status_code,
                "duration_ms": duration_ms,
                "client": request.client.host if request.client else None,
            },
        )
        return response


def configure_app(app: FastAPI) -> None:
    configure_logging()
    register_middleware(app)
    register_exception_handlers(app)
