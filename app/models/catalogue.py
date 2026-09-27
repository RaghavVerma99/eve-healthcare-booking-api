import uuid
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class DiagnosticCentre(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "diagnostic_centres"

    name: Mapped[str] = mapped_column(String(150), nullable=False, index=True)
    address: Mapped[str] = mapped_column(Text, nullable=False)
    city: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    state: Mapped[str | None] = mapped_column(String(80), nullable=True)
    postal_code: Mapped[str | None] = mapped_column(String(20), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(20), nullable=True)
    latitude: Mapped[Decimal | None] = mapped_column(Numeric(9, 6), nullable=True)
    longitude: Mapped[Decimal | None] = mapped_column(Numeric(9, 6), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    offerings: Mapped[list["CentreTest"]] = relationship(
        back_populates="centre", cascade="all, delete-orphan", lazy="selectin"
    )

    __table_args__ = (
        CheckConstraint(
            "latitude IS NULL OR (latitude >= -90 AND latitude <= 90)",
            name="latitude_range",
        ),
        CheckConstraint(
            "longitude IS NULL OR (longitude >= -180 AND longitude <= 180)",
            name="longitude_range",
        ),
        Index("ix_diagnostic_centres_city_active", "city", "is_active"),
    )


class DiagnosticTest(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "diagnostic_tests"

    code: Mapped[str] = mapped_column(String(40), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(150), nullable=False, index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_minutes: Mapped[int] = mapped_column(nullable=False, default=30)
    base_price: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    fasting_required: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    offerings: Mapped[list["CentreTest"]] = relationship(
        back_populates="test", cascade="all, delete-orphan", lazy="selectin"
    )

    __table_args__ = (
        CheckConstraint("duration_minutes > 0", name="duration_positive"),
        CheckConstraint("base_price >= 0", name="base_price_non_negative"),
    )


class CentreTest(TimestampMixin, Base):
    __tablename__ = "centre_tests"

    centre_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("diagnostic_centres.id", ondelete="CASCADE"),
        primary_key=True,
    )
    test_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("diagnostic_tests.id", ondelete="CASCADE"),
        primary_key=True,
    )
    price: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    is_available: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    centre: Mapped[DiagnosticCentre] = relationship(back_populates="offerings")
    test: Mapped[DiagnosticTest] = relationship(back_populates="offerings")

    # (centre_id, test_id) is the composite primary key, which already
    # guarantees one offering per centre/test pair.
    __table_args__ = (
        CheckConstraint("price >= 0", name="price_non_negative"),
        Index("ix_centre_tests_test_available", "test_id", "is_available"),
    )
