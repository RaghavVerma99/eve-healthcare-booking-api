import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    ForeignKey,
    Index,
    String,
    Uuid,
)
from sqlalchemy import (
    Enum as SAEnum,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin
from app.db.types import JSONType, TZDateTime
from app.models.enums import WebhookEventType, WebhookProcessingStatus


class WebhookEvent(TimestampMixin, Base):
    __tablename__ = "webhook_events"

    event_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    event_type: Mapped[WebhookEventType | str] = mapped_column(String(60), nullable=False)
    source: Mapped[str] = mapped_column(String(30), default="mockpay", nullable=False)
    payment_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("payments.id", ondelete="SET NULL"), nullable=True
    )
    booking_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("bookings.id", ondelete="SET NULL"), nullable=True
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False)
    signature_valid: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    status: Mapped[WebhookProcessingStatus] = mapped_column(
        SAEnum(
            WebhookProcessingStatus,
            name="webhook_processing_status",
            native_enum=False,
            length=20,
            validate_strings=True,
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        default=WebhookProcessingStatus.RECEIVED,
        nullable=False,
    )
    result: Mapped[str | None] = mapped_column(String(120), nullable=True)
    error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    received_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    processed_at: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)
    attempts: Mapped[int] = mapped_column(default=1, nullable=False)

    __table_args__ = (
        Index("ix_webhook_events_status_received", "status", "received_at"),
        Index("ix_webhook_events_booking", "booking_id"),
    )
