import uuid
from typing import Annotated

from fastapi import APIRouter, Header, Request, Response, status

from app.api.deps import AdminUser, CurrentUser, SessionDep
from app.core.config import settings
from app.core.exceptions import AuthenticationError
from app.core.logging_config import get_logger
from app.core.security import verify_webhook_signature
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
    outcome = await handle_webhook_event(session, event)
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
