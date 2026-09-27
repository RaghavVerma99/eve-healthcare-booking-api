import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field, field_validator

from app.models.enums import BookingStatus
from app.schemas.common import ORMModel
from app.schemas.payment import PaymentRead


class BookingCreate(BaseModel):
    test_id: uuid.UUID
    centre_id: uuid.UUID
    appointment_at: datetime
    notes: str | None = Field(default=None, max_length=1000)
    idempotency_key: str | None = Field(default=None, max_length=64)

    @field_validator("appointment_at")
    @classmethod
    def _appointment_must_be_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("appointment_at must include a timezone offset")
        return value

    @field_validator("idempotency_key")
    @classmethod
    def _validate_key(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned or len(cleaned) > 64:
            raise ValueError("idempotency_key must be 1-64 characters")
        return cleaned


class CentreSummary(ORMModel):
    id: uuid.UUID
    name: str
    address: str
    city: str


class TestSummary(ORMModel):
    id: uuid.UUID
    code: str
    name: str
    duration_minutes: int
    fasting_required: bool


class BookingRead(ORMModel):
    id: uuid.UUID
    reference: str
    user_id: uuid.UUID
    test_id: uuid.UUID
    centre_id: uuid.UUID
    appointment_at: datetime
    amount: Decimal
    status: BookingStatus
    notes: str | None = None
    cancelled_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    test: TestSummary | None = None
    centre: CentreSummary | None = None


class BookingDetail(BookingRead):
    payments: list[PaymentRead] = Field(default_factory=list)


class BookingStatusChange(BaseModel):
    status: BookingStatus
    reason: str | None = Field(default=None, max_length=255)
