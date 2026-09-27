from datetime import UTC, datetime

from fastapi import APIRouter
from sqlalchemy import text

from app.api.deps import SessionDep
from app.core.config import settings
from app.services.cache import get_cache

router = APIRouter(tags=["system"])


@router.get("/health", summary="Liveness and dependency health check")
async def health(session: SessionDep) -> dict:
    database_ok = True
    try:
        await session.execute(text("SELECT 1"))
    except Exception:
        database_ok = False

    cache_ok = await get_cache().ping()
    healthy = database_ok
    return {
        "status": "ok" if healthy else "degraded",
        "environment": settings.environment,
        "version": settings.app_version,
        "checks": {"database": database_ok, "cache": cache_ok},
        "timestamp": datetime.now(UTC).isoformat(),
    }
