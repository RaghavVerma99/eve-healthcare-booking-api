import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from app.models.enums import PaymentStatus
from app.schemas.common import ORMModel

SimulateMode = Literal["success", "failure", "insufficient_funds", "gateway_error"]


class PaymentCreate(BaseModel):
    booking_id: uuid.UUID
    simulate: SimulateMode = "success"
    idempotency_key: str | None = Field(default=None, max_length=64)

    @field_validator("idempotency_key")
    @classmethod
    def _validate_key(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned or len(cleaned) > 64:
            raise ValueError("idempotency_key must be 1-64 characters")
        return cleaned


class PaymentRead(ORMModel):
    id: uuid.UUID
    booking_id: uuid.UUID
    amount: Decimal
    currency: str
    status: PaymentStatus
    provider: str
    provider_payment_id: str | None = None
    failure_code: str | None = None
    failure_reason: str | None = None
    created_at: datetime
    updated_at: datetime


class WebhookEventIn(BaseModel):
    event_id: str = Field(min_length=4, max_length=120)
    event_type: Literal[
        "payment.succeeded", "payment.failed", "payment.refunded"
    ]
    created_at: datetime | None = None
    data: dict[str, Any] = Field(default_factory=dict)


class PaymentResponse(BaseModel):
    payment: PaymentRead
    booking_id: uuid.UUID
    booking_status: str
    duplicate: bool = False
    webhook: WebhookEventIn | None = None
    """The event that was delivered for this payment, so the caller can
    replay it against POST /payments/webhook/ and observe idempotency."""


class WebhookAck(BaseModel):
    received: bool = True
    event_id: str
    status: str
    duplicate: bool = False
    booking_status: str | None = None
    payment_id: uuid.UUID | None = None
    """The payment this acknowledgement describes.

    On a repeat delivery this is the payment named in *this* request, not the
    one the first delivery referenced, so a provider that recycles an
    `event_id` is not told its new payment was settled when it was not.
    """


class WebhookEventRead(ORMModel):
    event_id: str
    event_type: str
    source: str
    status: str
    result: str | None = None
    error: str | None = None
    attempts: int
    signature_valid: bool | None = None
    """True when a signature verified, False when one was rejected, null when
    signature checking was not enabled for this delivery."""
    booking_id: uuid.UUID | None = None
    payment_id: uuid.UUID | None = None
    received_at: datetime
    processed_at: datetime | None = None
