from enum import StrEnum


class BookingStatus(StrEnum):
    PENDING = "PENDING"
    CONFIRMED = "CONFIRMED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class PaymentStatus(StrEnum):
    PENDING = "PENDING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    REFUNDED = "REFUNDED"


class WebhookEventType(StrEnum):
    PAYMENT_SUCCEEDED = "payment.succeeded"
    PAYMENT_FAILED = "payment.failed"
    PAYMENT_REFUNDED = "payment.refunded"


class WebhookProcessingStatus(StrEnum):
    RECEIVED = "RECEIVED"
    PROCESSED = "PROCESSED"
    IGNORED = "IGNORED"
    FAILED = "FAILED"


BOOKING_TRANSITIONS: dict[BookingStatus, frozenset[BookingStatus]] = {
    BookingStatus.PENDING: frozenset(
        {BookingStatus.CONFIRMED, BookingStatus.FAILED, BookingStatus.CANCELLED}
    ),
    BookingStatus.FAILED: frozenset(
        {BookingStatus.PENDING, BookingStatus.CONFIRMED, BookingStatus.CANCELLED}
    ),
    BookingStatus.CONFIRMED: frozenset({BookingStatus.CANCELLED}),
    BookingStatus.CANCELLED: frozenset(),
}

TERMINAL_BOOKING_STATUSES = frozenset(
    status for status, targets in BOOKING_TRANSITIONS.items() if not targets
)

USER_CANCELLABLE_BOOKING_STATUSES = frozenset({BookingStatus.PENDING, BookingStatus.FAILED})

PAYABLE_BOOKING_STATUSES = frozenset({BookingStatus.PENDING, BookingStatus.FAILED})

PAYMENT_TRANSITIONS: dict[PaymentStatus, frozenset[PaymentStatus]] = {
    PaymentStatus.PENDING: frozenset({PaymentStatus.SUCCESS, PaymentStatus.FAILED}),
    PaymentStatus.FAILED: frozenset({PaymentStatus.PENDING, PaymentStatus.SUCCESS}),
    PaymentStatus.SUCCESS: frozenset({PaymentStatus.REFUNDED}),
    PaymentStatus.REFUNDED: frozenset(),
}


def can_transition(current: BookingStatus, target: BookingStatus) -> bool:
    return target in BOOKING_TRANSITIONS[current]


def can_payment_transition(current: PaymentStatus, target: PaymentStatus) -> bool:
    return target in PAYMENT_TRANSITIONS[current]
