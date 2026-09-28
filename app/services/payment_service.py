import re
import uuid
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import (
    AppError,
    ConflictError,
    InvalidStateTransitionError,
    NotFoundError,
    PermissionDeniedError,
    UnprocessableError,
)
from app.core.logging_config import get_logger
from app.db.types import utcnow
from app.models.booking import Booking
from app.models.enums import (
    PAYABLE_BOOKING_STATUSES,
    BookingStatus,
    PaymentStatus,
    can_payment_transition,
)
from app.models.payment import Payment
from app.models.user import User
from app.schemas.payment import PaymentCreate
from app.services.booking_service import assert_transition
from app.services.mock_gateway import PROVIDER_NAME

logger = get_logger(__name__)

PAYMENT_STATUS_TO_BOOKING_STATUS = {
    PaymentStatus.SUCCESS: BookingStatus.CONFIRMED,
    PaymentStatus.FAILED: BookingStatus.FAILED,
    PaymentStatus.REFUNDED: BookingStatus.CANCELLED,
}


async def get_payment(session: AsyncSession, payment_id: uuid.UUID) -> Payment:
    payment = await session.get(Payment, payment_id)
    if payment is None:
        raise NotFoundError("Payment not found.", code="payment_not_found")
    return payment


async def list_payments_for_booking(
    session: AsyncSession, booking: Booking
) -> list[Payment]:
    result = await session.execute(
        select(Payment)
        .where(Payment.booking_id == booking.id)
        .order_by(Payment.created_at.desc())
    )
    return list(result.scalars())


async def _lock_booking(session: AsyncSession, booking_id: uuid.UUID) -> Booking:
    # `of=Booking` is required: Booking eager-joins user/test/centre, and
    # PostgreSQL refuses FOR UPDATE on the nullable side of an outer join.
    # Locking only the booking row also avoids blocking unrelated bookings
    # that share the same centre or user.
    booking = await session.scalar(
        select(Booking).where(Booking.id == booking_id).with_for_update(of=Booking)
    )
    if booking is None:
        raise NotFoundError("Booking not found.", code="booking_not_found")
    return booking


def _ensure_payable(booking: Booking) -> None:
    if booking.status not in PAYABLE_BOOKING_STATUSES:
        raise InvalidStateTransitionError(
            f"Bookings in status {booking.status.value} cannot be paid.",
            details={
                "current_status": booking.status.value,
                "payable_statuses": sorted(s.value for s in PAYABLE_BOOKING_STATUSES),
            },
        )


async def apply_payment_outcome(
    session: AsyncSession,
    payment: Payment,
    *,
    target_status: PaymentStatus,
    provider_payment_id: str | None = None,
    failure_code: str | None = None,
    failure_reason: str | None = None,
    claimed_amount: Any = None,
    claimed_currency: str | None = None,
) -> tuple[Booking, str]:
    # Already-settled payments short-circuit first. Recording the provider
    # reference before this check let a second, differently-identified event
    # silently overwrite the reference the first one stored.
    if payment.status == target_status:
        booking = await _lock_booking(session, payment.booking_id)
        return booking, "payment_already_applied"

    _assert_amount_matches(payment, claimed_amount, claimed_currency)

    if not can_payment_transition(payment.status, target_status):
        raise InvalidStateTransitionError(
            f"A payment in status {payment.status.value} cannot move to {target_status.value}.",
            code="payment_state_conflict",
            details={
                "current_status": payment.status.value,
                "requested_status": target_status.value,
            },
        )

    if provider_payment_id:
        duplicate = await session.scalar(
            select(Payment.id).where(
                Payment.provider == payment.provider,
                Payment.provider_payment_id == provider_payment_id,
                Payment.id != payment.id,
            )
        )
        if duplicate is not None:
            raise ConflictError(
                "This provider payment reference is already linked to another payment.",
                code="duplicate_provider_payment_id",
            )
        payment.provider_payment_id = provider_payment_id

    payment.status = target_status
    payment.failure_code = failure_code
    payment.failure_reason = failure_reason

    booking = await _lock_booking(session, payment.booking_id)
    target_booking_status = PAYMENT_STATUS_TO_BOOKING_STATUS[target_status]

    try:
        assert_transition(booking, target_booking_status)
    except InvalidStateTransitionError:
        logger.warning(
            "booking_transition_skipped",
            extra={
                "booking_id": str(booking.id),
                "booking_status": booking.status.value,
                "requested_status": target_booking_status.value,
                "payment_id": str(payment.id),
                "payment_status": target_status.value,
            },
        )
        await session.flush()
        return booking, "booking_state_preserved"

    booking.status = target_booking_status
    if target_booking_status == BookingStatus.CANCELLED:
        booking.cancelled_at = utcnow()
    await session.flush()
    return booking, "applied"


def _assert_amount_matches(
    payment: Payment, claimed_amount: Any, claimed_currency: str | None
) -> None:
    """Reject a settlement for a different amount or currency than we charged.

    A provider is the only party allowed to settle a payment, but it still has
    to agree with the invoice. Trusting the amount on the event would let a
    malformed or tampered delivery confirm an arbitrary figure, so the amount is
    checked whenever the provider bothers to send one.
    """
    if claimed_amount is not None:
        try:
            amount = Decimal(str(claimed_amount))
        except (InvalidOperation, ValueError, TypeError) as exc:
            raise UnprocessableError(
                "amount in the webhook payload is not a valid decimal.",
                code="invalid_amount",
            ) from exc
        if amount != payment.amount:
            raise UnprocessableError(
                "The settled amount does not match the payment amount.",
                code="amount_mismatch",
                details={
                    "payment_id": str(payment.id),
                    "expected_amount": str(payment.amount),
                    "claimed_amount": str(amount),
                },
            )
    if claimed_currency and claimed_currency.upper() != payment.currency.upper():
        raise UnprocessableError(
            "The settled currency does not match the payment currency.",
            code="currency_mismatch",
            details={
                "payment_id": str(payment.id),
                "expected_currency": payment.currency,
                "claimed_currency": claimed_currency,
            },
        )


async def initiate_payment(
    session: AsyncSession, user: User, payload: PaymentCreate
) -> tuple[Payment, Booking, bool]:
    if payload.idempotency_key:
        existing = await session.scalar(
            select(Payment).where(Payment.idempotency_key == payload.idempotency_key)
        )
        if existing is not None:
            if existing.booking_id is not None:
                booking = await _lock_booking(session, existing.booking_id)
                if booking.user_id != user.id and not user.is_admin:
                    raise PermissionDeniedError(
                        "This idempotency key belongs to another user.",
                        code="idempotency_key_conflict",
                    )
                return existing, booking, True
            raise ConflictError(
                "A payment with this idempotency key is already in progress.",
                code="duplicate_payment_request",
            )

    try:
        booking_id = uuid.UUID(str(payload.booking_id))
    except ValueError as exc:
        raise UnprocessableError(
            "booking_id must be a valid UUID.", code="invalid_identifier"
        ) from exc

    booking = await _lock_booking(session, booking_id)
    if booking.user_id != user.id and not user.is_admin:
        raise PermissionDeniedError(
            "You cannot pay for another user's booking.", code="booking_forbidden"
        )

    already_paid = await session.scalar(
        select(Payment.id).where(
            Payment.booking_id == booking.id, Payment.status == PaymentStatus.SUCCESS
        )
    )
    if already_paid is not None:
        raise ConflictError(
            "This booking already has a successful payment.", code="payment_already_successful"
        )

    _ensure_payable(booking)

    payment = Payment(
        booking_id=booking.id,
        amount=booking.amount,
        currency="INR",
        status=PaymentStatus.PENDING,
        provider=PROVIDER_NAME,
        idempotency_key=payload.idempotency_key,
    )
    session.add(payment)
    try:
        async with session.begin_nested():
            await session.flush()
    except IntegrityError as exc:
        raise _translate_integrity_error(exc) from exc

    return payment, booking, False


def _translate_integrity_error(exc: IntegrityError) -> AppError:
    constraint = _failed_constraint(exc)
    if constraint and "single_success" in constraint:
        return ConflictError(
            "This booking already has a successful payment.",
            code="payment_already_successful",
        )
    return ConflictError(
        "A payment with this idempotency key already exists.",
        code="duplicate_payment_request",
    )


def _failed_constraint(exc: IntegrityError) -> str:
    orig = exc.orig
    diag = getattr(orig, "diag", None)
    name = getattr(diag, "constraint_name", None)
    if name:
        return str(name)
    message = str(orig)
    for pattern in (
        r"constraint\s+\"?([\w-]+)\"?",
        r"constraint failed:\s*([\w-]+)",
        r"index\s+\"([\w-]+)\"",
    ):
        match = re.search(pattern, message, re.IGNORECASE)
        if match:
            return match.group(1)
    return ""
