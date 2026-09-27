import uuid
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Numeric,
    String,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy import (
    Enum as SAEnum,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import PaymentStatus


class Payment(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "payments"

    booking_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("bookings.id", ondelete="RESTRICT"), nullable=False
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="INR", nullable=False)
    status: Mapped[PaymentStatus] = mapped_column(
        SAEnum(
            PaymentStatus,
            name="payment_status",
            native_enum=False,
            length=20,
            validate_strings=True,
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        default=PaymentStatus.PENDING,
        nullable=False,
    )
    provider: Mapped[str] = mapped_column(String(30), default="mockpay", nullable=False)
    provider_payment_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    failure_code: Mapped[str | None] = mapped_column(String(50), nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(64), nullable=True)

    booking: Mapped["Booking"] = relationship(back_populates="payments")  # noqa: F821

    __table_args__ = (
        UniqueConstraint("provider", "provider_payment_id", name="uq_payment_provider_ref"),
        UniqueConstraint("idempotency_key", name="uq_payments_idempotency_key"),
        CheckConstraint("amount >= 0", name="amount_non_negative"),
        Index("ix_payments_booking_created", "booking_id", "created_at"),
        Index(
            "uq_payments_single_success_per_booking",
            "booking_id",
            unique=True,
            postgresql_where=text("status = 'SUCCESS'"),
        ).ddl_if(dialect="postgresql"),
    )
