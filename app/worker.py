import asyncio
import logging
from datetime import timedelta

from celery import Celery
from sqlalchemy import select

from app.core.config import settings
from app.core.logging_config import configure_logging, get_logger
from app.db.session import SessionLocal
from app.db.types import utcnow
from app.models.booking import Booking
from app.models.enums import BookingStatus, WebhookProcessingStatus
from app.schemas.payment import WebhookEventIn

logger = get_logger("eve.worker")

celery_app = Celery(
    "eve_diagnostics",
    broker=settings.redis_url or "memory://",
    backend=settings.redis_url or "cache+memory://",
)
celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_default_retry_delay=10,
    task_time_limit=60,
    # Bookings are never auto-confirmed, so an abandoned PENDING booking would
    # otherwise hold its slot forever. `celery -A app.worker.celery_app beat`
    # runs this schedule; the API is unaffected when beat is not running.
    beat_schedule={
        "expire-stale-pending-bookings": {
            "task": "bookings.expire_stale_pending",
            "schedule": timedelta(minutes=15),
        },
        "purge-expired-refresh-tokens": {
            "task": "auth.purge_expired_refresh_tokens",
            "schedule": timedelta(hours=6),
        },
    },
)


def _run(coro):
    return asyncio.run(coro)


@celery_app.task(
    name="bookings.expire_stale_pending",
    bind=True,
    max_retries=3,
    default_retry_delay=30,
)
def expire_stale_pending_bookings(self) -> dict:
    return _run(_expire_stale_pending())


async def _expire_stale_pending() -> dict:
    cutoff = utcnow() - timedelta(hours=24)
    async with SessionLocal() as session:
        # skip_locked keeps this sweep off rows a payment request is currently
        # holding, so expiring a booking can never race a payment confirming it.
        result = await session.execute(
            select(Booking)
            .where(
                Booking.status == BookingStatus.PENDING,
                Booking.appointment_at < cutoff,
            )
            .with_for_update(skip_locked=True)
        )
        bookings = list(result.scalars())
        for booking in bookings:
            booking.status = BookingStatus.CANCELLED
            booking.cancelled_at = utcnow()
        await session.commit()
    logger.info("expired_stale_bookings", extra={"count": len(bookings)})
    return {"expired": len(bookings)}


@celery_app.task(name="auth.purge_expired_refresh_tokens")
def purge_expired_refresh_tokens() -> dict:
    from app.services.auth_service import purge_expired_refresh_tokens as purge

    return _run(_purge_refresh_tokens(purge))


async def _purge_refresh_tokens(purge) -> dict:
    async with SessionLocal() as session:
        purged = await purge(session)
        await session.commit()
    logger.info("purged_refresh_tokens", extra={"count": purged})
    return {"purged": purged}


@celery_app.task(
    name="payments.replay_webhook",
    bind=True,
    max_retries=5,
    default_retry_delay=15,
)
def replay_webhook(self, event_id: str) -> dict:
    return _run(_replay_webhook(event_id))


async def _replay_webhook(event_id: str) -> dict:
    from app.models.webhook import WebhookEvent
    from app.services.webhook_service import handle_webhook_event

    async with SessionLocal() as session:
        event = await session.get(WebhookEvent, event_id, with_for_update=True)
        if event is None:
            return {"status": "unknown_event", "event_id": event_id}

        if event.status == WebhookProcessingStatus.PROCESSED:
            return {"status": "already_processed", "event_id": event_id}

        payload = WebhookEventIn.model_validate(
            {
                "event_id": event.event_id,
                "event_type": str(event.event_type),
                "data": (event.payload or {}).get("data", {}),
            }
        )
        outcome = await handle_webhook_event(
            session, payload, signature_valid=event.signature_valid
        )
        await session.commit()
    return {
        "status": str(outcome.status),
        "event_id": event_id,
        "booking_status": outcome.booking_status,
    }


def enqueue_replay(event_id: str, *, queue: str = "payments") -> str:
    """Queue a replay of a stored event. Raises if the broker is unreachable."""
    task = replay_webhook.apply_async(kwargs={"event_id": event_id}, queue=queue)
    return str(task.id)


if __name__ == "__main__":
    configure_logging()
    logging.getLogger("celery").setLevel(settings.log_level.upper())
    celery_app.start()
