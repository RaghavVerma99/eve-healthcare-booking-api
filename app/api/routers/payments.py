import uuid
from typing import Annotated

from fastapi import APIRouter, Header, Request, Response, status

from app.api.deps import AdminUser, CurrentUser, SessionDep
from app.core.config import settings
from app.core.exceptions import AuthenticationError, ServiceUnavailableError
from app.core.logging_config import get_logger
from app.core.security import verify_webhook_signature
from app.schemas.common import MessageResponse
from app.schemas.payment import (
    PaymentCreate,
    PaymentRead,
    PaymentResponse,
    WebhookAck,
    WebhookEventIn,
    WebhookEventRead,
)
from app.services import booking_service, payment_service, webhook_service
from app.services.mock_gateway import mock_gateway
from app.services.webhook_service import handle_webhook_event
from app.worker import enqueue_replay

logger = get_logger(__name__)
router = APIRouter(tags=["payments"])


@router.post(
    "/payments/",
    response_model=PaymentResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a simulated payment for a booking",
)
async def create_payment(
    payload: PaymentCreate, session: SessionDep, user: CurrentUser, response: Response
) -> PaymentResponse:
    payment, booking, replayed = await payment_service.initiate_payment(session, user, payload)
    if replayed:
        await session.commit()
        response.status_code = status.HTTP_200_OK
        return PaymentResponse(
            payment=PaymentRead.model_validate(payment),
            booking_id=str(booking.id),
            booking_status=str(booking.status),
            duplicate=True,
        )

    charge = await mock_gateway.charge(
        booking_id=str(booking.id),
        booking_reference=booking.reference,
        amount=booking.amount,
        mode=payload.simulate,
    )
    data = charge.as_event_payload(booking.reference, str(booking.id), booking.amount)
    data["payment_id"] = str(payment.id)
    event = WebhookEventIn(
        event_id=charge.event_id,
        event_type=charge.event_type.value,
        data=data,
    )
    # `signature_valid=True` records the truth here: the event came from the
    # in-process mock gateway, which is the trusted source. Recording `None` as
    # False would look like a failed verification to the admin inspector and
    # would make the replay task reject the event forever.
    outcome = await handle_webhook_event(session, event, signature_valid=True)
    await session.commit()

    logger.info(
        "payment_processed",
        extra={
            "payment_id": str(payment.id),
            "booking_id": str(booking.id),
            "booking_status": str(booking.status),
            "payment_status": str(payment.status),
            "simulate_mode": payload.simulate,
            "event_id": charge.event_id,
            "webhook_outcome": outcome.result,
        },
    )
    return PaymentResponse(
        payment=PaymentRead.model_validate(payment),
        booking_id=str(booking.id),
        booking_status=str(booking.status),
        webhook=event,
    )


@router.post(
    "/payments/webhook/",
    response_model=WebhookAck,
    summary="Receive a payment status update from the simulated provider (idempotent)",
)
async def payment_webhook(
    request: Request,
    payload: WebhookEventIn,
    session: SessionDep,
    response: Response,
    x_signature: Annotated[str | None, Header(alias="X-Signature")] = None,
) -> WebhookAck:
    signature_valid: bool | None = None
    if settings.webhook_require_signature:
        raw_body = await request.body()
        signature_valid = verify_webhook_signature(raw_body, x_signature)
        if not signature_valid:
            logger.warning("webhook_rejected", extra={"event_id": payload.event_id})
            raise AuthenticationError(
                "Invalid webhook signature.", code="invalid_webhook_signature"
            )

    outcome = await handle_webhook_event(session, payload, signature_valid=signature_valid)
    await session.commit()
    if outcome.duplicate:
        response.status_code = status.HTTP_200_OK
    return WebhookAck(
        received=True,
        event_id=outcome.event_id,
        status=str(outcome.status),
        duplicate=outcome.duplicate,
        booking_status=outcome.booking_status,
        payment_id=outcome.payment_id,
    )


@router.get(
    "/payments/{payment_id}", response_model=PaymentRead, summary="Fetch one payment (owner only)"
)
async def get_payment(payment_id: uuid.UUID, session: SessionDep, user: CurrentUser) -> PaymentRead:
    payment = await payment_service.get_payment(session, payment_id)
    await booking_service.get_booking(session, payment.booking_id, user=user)
    return PaymentRead.model_validate(payment)


@router.get(
    "/payments/webhook/{event_id}",
    response_model=WebhookEventRead,
    summary="Inspect a stored webhook event (admin)",
)
async def get_webhook_event(
    event_id: str, session: SessionDep, admin: AdminUser
) -> WebhookEventRead:
    event = await webhook_service.get_event(session, event_id)
    return WebhookEventRead.model_validate(event)


@router.post(
    "/payments/webhook/{event_id}/replay",
    response_model=MessageResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Re-queue a stored event for processing (admin)",
)
async def replay_webhook_event(
    event_id: str, session: SessionDep, admin: AdminUser
) -> MessageResponse:
    """Hand a stored event back to the worker.

    The processor is idempotent, so replaying a settled event is a no-op. This
    exists for the case the receiver could not finish on its own: an event
    parked in FAILED once the underlying cause is fixed.
    """
    await webhook_service.get_event(session, event_id)
    try:
        task_id = enqueue_replay(event_id)
    except Exception as exc:
        logger.error(
            "webhook_replay_enqueue_failed",
            extra={"event_id": event_id, "error": str(exc), "error_type": type(exc).__name__},
        )
        raise ServiceUnavailableError(
            "The task queue is unavailable. Start the worker, or replay the event directly.",
            code="queue_unavailable",
        ) from exc
    return MessageResponse(message=f"Replay queued as task {task_id}.")
