import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy import (
    Enum as SAEnum,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.types import TZDateTime
from app.models.enums import BookingStatus


class Booking(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "bookings"

    reference: Mapped[str] = mapped_column(
        String(24), unique=True, nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    test_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("diagnostic_tests.id", ondelete="RESTRICT"),
        nullable=False,
    )
    centre_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("diagnostic_centres.id", ondelete="RESTRICT"),
        nullable=False,
    )
    appointment_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    status: Mapped[BookingStatus] = mapped_column(
        SAEnum(
            BookingStatus,
            name="booking_status",
            native_enum=False,
            length=20,
            validate_strings=True,
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        default=BookingStatus.PENDING,
        nullable=False,
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(64), nullable=True)

    user: Mapped["User"] = relationship(back_populates="bookings", lazy="joined")  # noqa: F821
    test: Mapped["DiagnosticTest"] = relationship(lazy="joined")  # noqa: F821
    centre: Mapped["DiagnosticCentre"] = relationship(lazy="joined")  # noqa: F821
    payments: Mapped[list["Payment"]] = relationship(  # noqa: F821
        back_populates="booking", lazy="selectin", order_by="Payment.created_at"
    )

    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_bookings_idempotency_key"),
        CheckConstraint("amount >= 0", name="amount_non_negative"),
        Index("ix_bookings_user_created", "user_id", "created_at"),
        Index("ix_bookings_user_status", "user_id", "status"),
        Index("ix_bookings_centre_appointment", "centre_id", "appointment_at"),
        # The application-level "is this slot taken?" check is a plain SELECT, so
        # two concurrent requests can both pass it and both insert. This partial
        # index is the authoritative guard: at most one live booking per centre
        # per appointment slot. Cancelled and failed bookings release the slot,
        # which is why the predicate restricts the index to live statuses.
        Index(
            "uq_bookings_active_slot",
            "centre_id",
            "appointment_at",
            unique=True,
            postgresql_where=text("status IN ('PENDING', 'CONFIRMED')"),
            sqlite_where=text("status IN ('PENDING', 'CONFIRMED')"),
        ),
    )
