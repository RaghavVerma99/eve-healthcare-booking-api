from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi

from app.api.router import api_router
from app.core.config import settings
from app.core.handlers import configure_app
from app.core.logging_config import get_logger

logger = get_logger("eve.app")

DESCRIPTION = """
Backend service for diagnostic test bookings with a simulated payment provider.

* JWT authentication with rotating refresh tokens
* Diagnostic centre / test catalogue with per-centre pricing
* Booking lifecycle with an explicit state machine
* Idempotent payment webhooks (safe to replay and safe under concurrency)
"""


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(
        "application_startup",
        extra={"environment": settings.environment, "version": settings.app_version},
    )
    yield
    from app.db.session import dispose_engine
    from app.services.cache import get_cache

    await get_cache().close()
    await dispose_engine()
    logger.info("application_shutdown")


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description=DESCRIPTION,
    lifespan=lifespan,
    debug=settings.debug,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
    contact={"name": "EVE Healthcare", "email": "engineering@eve.health"},
    license_info={"name": "MIT"},
)

configure_app(app)
app.include_router(api_router)


def custom_openapi() -> dict:
    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
        tags=[
            {"name": "auth", "description": "Signup, login and token lifecycle."},
            {"name": "catalogue", "description": "Diagnostic centres and tests."},
            {"name": "bookings", "description": "Appointment booking lifecycle."},
            {"name": "payments", "description": "Simulated payments and webhooks."},
            {"name": "system", "description": "Health and diagnostics."},
        ],
    )
    components = schema.setdefault("components", {}).setdefault("securitySchemes", {})
    components["HTTPBearer"] = {
        "type": "http",
        "scheme": "bearer",
        "bearerFormat": "JWT",
        "description": "Paste the access_token returned by /auth/login.",
    }
    app.openapi_schema = schema
    return schema


app.openapi = custom_openapi
