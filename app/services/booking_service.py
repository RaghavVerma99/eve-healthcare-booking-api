import secrets
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import Select, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload, selectinload

from app.core.exceptions import (
    AppError,
    ConflictError,
    InvalidStateTransitionError,
    NotFoundError,
    PermissionDeniedError,
    UnprocessableError,
)
from app.db.types import utcnow
from app.models.booking import Booking
from app.models.catalogue import CentreTest, DiagnosticCentre, DiagnosticTest
from app.models.enums import (
    USER_CANCELLABLE_BOOKING_STATUSES,
    BookingStatus,
    can_transition,
)
from app.models.user import User
from app.schemas.booking import BookingCreate

MINIMUM_LEAD_TIME = timedelta(minutes=30)
MAXIMUM_ADVANCE_WINDOW = timedelta(days=90)
_REFERENCE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def _generate_reference() -> str:
    suffix = "".join(secrets.choice(_REFERENCE_ALPHABET) for _ in range(8))
    return f"BK-{suffix}"


def _base_query() -> Select:
    return select(Booking).options(
        selectinload(Booking.payments),
        joinedload(Booking.test),
        joinedload(Booking.centre),
    )


async def _slot_is_taken(
    session: AsyncSession, centre_id: uuid.UUID, appointment_at: datetime
) -> bool:
    """Advisory check, so a clash produces a clear 409 instead of a constraint error.

    It cannot see a concurrent transaction that has not committed, which is why
    `uq_bookings_active_slot` is the real guard.
    """
    clash = await session.scalar(
        select(Booking.id).where(
            Booking.centre_id == centre_id,
            Booking.appointment_at == appointment_at,
            Booking.status.in_([BookingStatus.PENDING, BookingStatus.CONFIRMED]),
        )
    )
    return clash is not None


async def create_booking(
    session: AsyncSession, user: User, payload: BookingCreate
) -> tuple[Booking, bool]:
    if payload.idempotency_key:
        existing = await session.scalar(
            select(Booking).where(Booking.idempotency_key == payload.idempotency_key)
        )
        if existing is not None:
            if existing.user_id != user.id:
                raise PermissionDeniedError(
                    "This idempotency key belongs to another user.",
                    code="idempotency_key_conflict",
                )
            return existing, True

    try:
        test_id = uuid.UUID(str(payload.test_id))
        centre_id = uuid.UUID(str(payload.centre_id))
    except ValueError as exc:
        raise UnprocessableError(
            "test_id and centre_id must be valid UUIDs.", code="invalid_identifier"
        ) from exc

    now = utcnow()
    appointment_at = payload.appointment_at.astimezone(UTC)
    if appointment_at < now + MINIMUM_LEAD_TIME:
        raise UnprocessableError(
            "Appointment must be scheduled at least 30 minutes in the future.",
            code="appointment_too_soon",
        )
    if appointment_at > now + MAXIMUM_ADVANCE_WINDOW:
        raise UnprocessableError(
            "Appointment cannot be scheduled more than 90 days ahead.",
            code="appointment_too_far",
        )

    offering = await session.scalar(
        select(CentreTest).where(
            CentreTest.centre_id == centre_id,
            CentreTest.test_id == test_id,
            CentreTest.is_available.is_(True),
        )
    )
    if offering is None:
        centre = await session.get(DiagnosticCentre, centre_id)
        test = await session.get(DiagnosticTest, test_id)
        if centre is None or not centre.is_active or test is None or not test.is_active:
            raise NotFoundError(
                "Diagnostic centre or test not found.", code="catalogue_not_found"
            )
        raise UnprocessableError(
            "This test is not available at the selected centre.",
            code="test_not_offered_at_centre",
        )

    if await _slot_is_taken(session, centre_id, appointment_at):
        raise ConflictError(
            "The centre already has a booking at that exact appointment time.",
            code="appointment_slot_taken",
        )

    booking = Booking(
        reference=_generate_reference(),
        user_id=user.id,
        test_id=test_id,
        centre_id=centre_id,
        appointment_at=appointment_at,
        amount=offering.price,
        status=BookingStatus.PENDING,
        notes=payload.notes,
        idempotency_key=payload.idempotency_key,
    )
    session.add(booking)
    # The clash read above is advisory: it cannot see a concurrent transaction
    # that has not committed yet, so two requests for the same slot can both
    # pass it. `uq_bookings_active_slot` is the real guard, which means the
    # loser of that race discovers it here rather than silently double-booking.
    # The nested block keeps the failed INSERT from poisoning the transaction.
    try:
        async with session.begin_nested():
            await session.flush()
    except IntegrityError as exc:
        raise _translate_booking_integrity_error(exc) from exc
    return booking, False


def _translate_booking_integrity_error(exc: IntegrityError) -> AppError:
    """Map a losing race or a bad reference to a specific client error.

    The other IntegrityErrors reachable here are the unique booking reference
    and the unique idempotency key, both of which are conflicts rather than
    server faults.
    """
    message = str(exc.orig).lower()
    if "uq_bookings_active_slot" in message or "appointment_at" in message:
        return ConflictError(
            "The centre already has a booking at that exact appointment time.",
            code="appointment_slot_taken",
        )
    if "uq_bookings_idempotency_key" in message or "idempotency_key" in message:
        return ConflictError(
            "A booking with this idempotency key already exists.",
            code="duplicate_idempotency_key",
        )
    if "reference" in message:
        return ConflictError(
            "Could not allocate a unique booking reference. Please retry.",
            code="reference_collision",
        )
    return ConflictError("The booking conflicts with an existing record.")


async def get_booking(
    session: AsyncSession,
    booking_id: uuid.UUID,
    *,
    user: User | None = None,
    for_update: bool = False,
) -> Booking:
    stmt = _base_query()
    if for_update:
        stmt = stmt.with_for_update(of=Booking, skip_locked=False)
    booking = await session.scalar(stmt.where(Booking.id == booking_id))
    if booking is None:
        raise NotFoundError("Booking not found.", code="booking_not_found")
    if user is not None and booking.user_id != user.id and not user.is_admin:
        raise PermissionDeniedError(
            "You cannot access another user's booking.", code="booking_forbidden"
        )
    return booking


async def list_bookings(
    session: AsyncSession,
    user: User,
    *,
    status: BookingStatus | None,
    centre_id: uuid.UUID | None,
    from_date: datetime | None,
    to_date: datetime | None,
    page: int,
    size: int,
) -> tuple[list[Booking], int]:
    filters = []
    if not user.is_admin:
        filters.append(Booking.user_id == user.id)
    if status is not None:
        filters.append(Booking.status == status)
    if centre_id is not None:
        filters.append(Booking.centre_id == centre_id)
    if from_date is not None:
        filters.append(Booking.appointment_at >= from_date.astimezone(UTC))
    if to_date is not None:
        filters.append(Booking.appointment_at <= to_date.astimezone(UTC))

    total = await session.scalar(
        select(func.count()).select_from(Booking).where(*filters)
    )
    result = await session.execute(
        _base_query()
        .where(*filters)
        .order_by(Booking.created_at.desc())
        .offset((page - 1) * size)
        .limit(size)
    )
    return list(result.scalars().unique()), int(total or 0)


def assert_transition(booking: Booking, target: BookingStatus) -> None:
    if booking.status == target:
        raise ConflictError(
            f"Booking is already {target.value}.", code="status_unchanged"
        )
    if not can_transition(booking.status, target):
        raise InvalidStateTransitionError(
            f"Cannot move a booking from {booking.status.value} to {target.value}.",
            details={
                "current_status": booking.status.value,
                "requested_status": target.value,
            },
        )


async def cancel_booking(
    session: AsyncSession, booking: Booking, *, reason: str | None = None
) -> Booking:
    assert_transition(booking, BookingStatus.CANCELLED)
    if booking.status not in USER_CANCELLABLE_BOOKING_STATUSES:
        raise ConflictError(
            f"Bookings in status {booking.status.value} can only be voided by a refund.",
            code="booking_not_cancellable",
        )
    booking.status = BookingStatus.CANCELLED
    booking.cancelled_at = utcnow()
    if reason:
        booking.notes = f"{booking.notes or ''}\n[cancelled] {reason}".strip()
    await session.flush()
    return booking
