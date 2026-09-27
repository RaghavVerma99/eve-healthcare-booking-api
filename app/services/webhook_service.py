import uuid
from dataclasses import dataclass

from sqlalchemy import case, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError, UnprocessableError
from app.core.logging_config import get_logger
from app.db.types import utcnow
from app.models.booking import Booking
from app.models.enums import PaymentStatus, WebhookEventType, WebhookProcessingStatus
from app.models.payment import Payment
from app.models.webhook import WebhookEvent
from app.schemas.payment import WebhookEventIn
from app.services.payment_service import apply_payment_outcome

logger = get_logger(__name__)

EVENT_TYPE_TO_PAYMENT_STATUS = {
    WebhookEventType.PAYMENT_SUCCEEDED: PaymentStatus.SUCCESS,
    WebhookEventType.PAYMENT_FAILED: PaymentStatus.FAILED,
    WebhookEventType.PAYMENT_REFUNDED: PaymentStatus.REFUNDED,
}

RETRYABLE_EVENT_STATUSES = frozenset({WebhookProcessingStatus.FAILED})


@dataclass
class WebhookOutcome:
    event_id: str
    duplicate: bool
    status: WebhookProcessingStatus
    booking_status: str | None = None
    payment_id: str | None = None
    result: str | None = None

    @property
    def acknowledged(self) -> bool:
        return self.status in {
            WebhookProcessingStatus.PROCESSED,
            WebhookProcessingStatus.IGNORED,
        }


def _insert_factory(session: AsyncSession):
    dialect = session.get_bind().dialect.name
    return pg_insert if dialect == "postgresql" else sqlite_insert


async def claim_event(session: AsyncSession, event: WebhookEvent) -> bool:
    now = utcnow()
    statement = (
        _insert_factory(session)(WebhookEvent)
        .values(
            event_id=event.event_id,
            event_type=str(event.event_type),
            source=event.source,
            payload=event.payload,
            signature_valid=event.signature_valid,
            status=WebhookProcessingStatus.RECEIVED.value,
            received_at=event.received_at or now,
            created_at=now,
            updated_at=now,
            attempts=1,
        )
        .on_conflict_do_nothing(index_elements=[WebhookEvent.event_id])
        .returning(WebhookEvent.event_id)
    )
    result = await session.execute(statement)
    return result.first() is not None


async def _booking_id_for_reference(session: AsyncSession, reference: str) -> uuid.UUID:
    booking_id = await session.scalar(
        select(Booking.id).where(Booking.reference == str(reference).upper())
    )
    if booking_id is None:
        raise NotFoundError(
            "No booking matches the webhook reference.", code="booking_not_found"
        )
    return booking_id


async def _resolve_payment(session: AsyncSession, data: dict) -> Payment:
    raw_payment_id = data.get("payment_id")
    if raw_payment_id:
        try:
            payment_id = uuid.UUID(str(raw_payment_id))
        except ValueError as exc:
            raise UnprocessableError(
                "payment_id in the webhook payload is not a valid UUID.",
                code="invalid_identifier",
            ) from exc
        payment = await session.get(Payment, payment_id)
        if payment is not None:
            return payment
        raise NotFoundError(
            "No payment matches the webhook payload.", code="payment_not_found"
        )

    provider = str(data.get("provider") or "mockpay")
    provider_payment_id = data.get("provider_payment_id")
    if provider_payment_id:
        payment = await session.scalar(
            select(Payment).where(
                Payment.provider == provider,
                Payment.provider_payment_id == str(provider_payment_id),
            )
        )
        if payment is not None:
            return payment

    if data.get("booking_reference"):
        booking_id = await _booking_id_for_reference(
            session, str(data["booking_reference"])
        )
        payment = await _latest_payment_for_booking(session, booking_id)
        if payment is not None:
            return payment

    raw_booking_id = data.get("booking_id")
    if raw_booking_id:
        try:
            booking_id = uuid.UUID(str(raw_booking_id))
        except ValueError as exc:
            raise UnprocessableError(
                "booking_id in the webhook payload is not a valid UUID.",
                code="invalid_identifier",
            ) from exc
        payment = await _latest_payment_for_booking(session, booking_id)
        if payment is not None:
            return payment

    raise NotFoundError(
        "No payment matches the webhook payload.", code="payment_not_found"
    )


async def _latest_payment_for_booking(
    session: AsyncSession, booking_id: uuid.UUID
) -> Payment | None:
    return await session.scalar(
        select(Payment)
        .where(Payment.booking_id == booking_id)
        .order_by(
            case((Payment.status == PaymentStatus.PENDING, 0), else_=1),
            Payment.created_at.desc(),
        )
        .limit(1)
    )


async def _current_booking_status(
    session: AsyncSession, booking_id: uuid.UUID | None
) -> str | None:
    if booking_id is None:
        return None
    status = await session.scalar(select(Booking.status).where(Booking.id == booking_id))
    return str(status) if status is not None else None


def _stored_outcome(
    event: WebhookEvent, booking_status: str | None, duplicate: bool
) -> WebhookOutcome:
    return WebhookOutcome(
        event_id=event.event_id,
        duplicate=duplicate,
        status=event.status,
        booking_status=booking_status,
        payment_id=str(event.payment_id) if event.payment_id else None,
        result=event.result or "already_received",
    )


async def handle_webhook_event(
    session: AsyncSession, payload: WebhookEventIn, *, signature_valid: bool | None = None
) -> WebhookOutcome:
    try:
        event_type = WebhookEventType(payload.event_type)
    except ValueError as exc:
        raise UnprocessableError(
            f"Unsupported event_type '{payload.event_type}'.",
            code="unsupported_event_type",
        ) from exc

    event = await session.get(WebhookEvent, payload.event_id, with_for_update=True)
    if event is not None and event.status not in RETRYABLE_EVENT_STATUSES:
        booking_status = await _current_booking_status(session, event.booking_id)
        logger.info(
            "webhook_duplicate_ignored",
            extra={"event_id": payload.event_id, "stored_status": str(event.status)},
        )
        return _stored_outcome(event, booking_status, duplicate=True)

    if event is None:
        claimed = await claim_event(
            session,
            WebhookEvent(
                event_id=payload.event_id,
                event_type=event_type,
                source=str(payload.data.get("provider") or "mockpay"),
                payload={
                    "data": payload.data,
                    "event_type": payload.event_type,
                    "created_at": payload.created_at.isoformat() if payload.created_at else None,
                },
                signature_valid=bool(signature_valid),
                received_at=utcnow(),
            ),
        )
        if not claimed:
            stored = await session.get(WebhookEvent, payload.event_id)
            booking_status = (
                await _current_booking_status(session, stored.booking_id) if stored else None
            )
            logger.info("webhook_duplicate_ignored", extra={"event_id": payload.event_id})
            return _stored_outcome(stored, booking_status, duplicate=True)
        event = await session.get(WebhookEvent, payload.event_id)
    else:
        event.attempts += 1
        event.status = WebhookProcessingStatus.RECEIVED
        event.signature_valid = bool(signature_valid)
        event.error = None
        await session.flush()

    if event is None:
        raise NotFoundError("Webhook event could not be persisted.", code="event_not_found")

    if signature_valid is False:
        event.status = WebhookProcessingStatus.FAILED
        event.error = "signature verification failed"
        event.processed_at = utcnow()
        await session.flush()
        logger.warning("webhook_signature_rejected", extra={"event_id": payload.event_id})
        return WebhookOutcome(
            event_id=payload.event_id,
            duplicate=False,
            status=WebhookProcessingStatus.FAILED,
            result="signature_invalid",
        )

    payment = await _resolve_payment(session, payload.data)
    event.payment_id = payment.id
    event.booking_id = payment.booking_id

    booking, result = await apply_payment_outcome(
        session,
        payment,
        target_status=EVENT_TYPE_TO_PAYMENT_STATUS[event_type],
        provider_payment_id=(
            str(payload.data["provider_payment_id"])
            if payload.data.get("provider_payment_id")
            else None
        ),
        failure_code=payload.data.get("failure_code"),
        failure_reason=payload.data.get("failure_reason"),
    )

    event.status = WebhookProcessingStatus.PROCESSED
    event.result = result
    event.processed_at = utcnow()
    await session.flush()

    logger.info(
        "webhook_processed",
        extra={
            "event_id": payload.event_id,
            "event_type": payload.event_type,
            "booking_id": str(booking.id),
            "payment_id": str(payment.id),
            "booking_status": booking.status.value,
            "outcome": result,
        },
    )
    return WebhookOutcome(
        event_id=payload.event_id,
        duplicate=False,
        status=WebhookProcessingStatus.PROCESSED,
        booking_status=booking.status.value,
        payment_id=str(payment.id),
        result=result,
    )


async def get_event(session: AsyncSession, event_id: str) -> WebhookEvent:
    event = await session.get(WebhookEvent, event_id)
    if event is None:
        raise NotFoundError("Webhook event not found.", code="webhook_event_not_found")
    return event
