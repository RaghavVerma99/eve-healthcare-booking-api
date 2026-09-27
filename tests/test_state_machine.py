import pytest
from app.models.enums import (
    BOOKING_TRANSITIONS,
    PAYABLE_BOOKING_STATUSES,
    PAYMENT_TRANSITIONS,
    TERMINAL_BOOKING_STATUSES,
    USER_CANCELLABLE_BOOKING_STATUSES,
    BookingStatus,
    PaymentStatus,
    can_payment_transition,
    can_transition,
)


@pytest.mark.parametrize(
    "current,target,allowed",
    [
        (BookingStatus.PENDING, BookingStatus.CONFIRMED, True),
        (BookingStatus.PENDING, BookingStatus.FAILED, True),
        (BookingStatus.PENDING, BookingStatus.CANCELLED, True),
        (BookingStatus.FAILED, BookingStatus.CONFIRMED, True),
        (BookingStatus.FAILED, BookingStatus.CANCELLED, True),
        (BookingStatus.CONFIRMED, BookingStatus.CANCELLED, True),
        (BookingStatus.CONFIRMED, BookingStatus.FAILED, False),
        (BookingStatus.CONFIRMED, BookingStatus.PENDING, False),
        (BookingStatus.CANCELLED, BookingStatus.CONFIRMED, False),
        (BookingStatus.CANCELLED, BookingStatus.PENDING, False),
        (BookingStatus.CANCELLED, BookingStatus.FAILED, False),
    ],
)
def test_booking_transition_rules(
    current: BookingStatus, target: BookingStatus, allowed: bool
) -> None:
    assert can_transition(current, target) is allowed


@pytest.mark.parametrize(
    "current,target,allowed",
    [
        (PaymentStatus.PENDING, PaymentStatus.SUCCESS, True),
        (PaymentStatus.PENDING, PaymentStatus.FAILED, True),
        (PaymentStatus.FAILED, PaymentStatus.SUCCESS, True),
        (PaymentStatus.SUCCESS, PaymentStatus.REFUNDED, True),
        (PaymentStatus.SUCCESS, PaymentStatus.FAILED, False),
        (PaymentStatus.SUCCESS, PaymentStatus.PENDING, False),
        (PaymentStatus.REFUNDED, PaymentStatus.SUCCESS, False),
    ],
)
def test_payment_transition_rules(
    current: PaymentStatus, target: PaymentStatus, allowed: bool
) -> None:
    assert can_payment_transition(current, target) is allowed


def test_every_status_has_a_transition_entry() -> None:
    assert set(BOOKING_TRANSITIONS) == set(BookingStatus)
    assert set(PAYMENT_TRANSITIONS) == set(PaymentStatus)


def test_cancelled_is_the_only_terminal_booking_status() -> None:
    assert frozenset({BookingStatus.CANCELLED}) == TERMINAL_BOOKING_STATUSES


def test_user_cancellable_statuses_exclude_confirmed() -> None:
    assert BookingStatus.CONFIRMED not in USER_CANCELLABLE_BOOKING_STATUSES
    assert frozenset({BookingStatus.PENDING, BookingStatus.FAILED}) == (
        USER_CANCELLABLE_BOOKING_STATUSES
    )


def test_payable_statuses_match_cancellable_statuses() -> None:
    assert USER_CANCELLABLE_BOOKING_STATUSES == PAYABLE_BOOKING_STATUSES
