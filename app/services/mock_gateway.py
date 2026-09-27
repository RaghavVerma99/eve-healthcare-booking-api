import hashlib
import uuid
from dataclasses import dataclass
from decimal import Decimal

from app.models.enums import PaymentStatus, WebhookEventType
from app.schemas.payment import SimulateMode

PROVIDER_NAME = "mockpay"

_DECLINE_REASONS: dict[str, tuple[str, str]] = {
    "failure": ("payment_declined", "The payment was declined by the issuer."),
    "insufficient_funds": (
        "insufficient_funds",
        "The payment method has insufficient funds.",
    ),
    "gateway_error": ("gateway_error", "The payment gateway is temporarily unavailable."),
}


@dataclass(frozen=True)
class GatewayChargeResult:
    provider_payment_id: str
    status: PaymentStatus
    event_type: WebhookEventType
    event_id: str
    failure_code: str | None = None
    failure_reason: str | None = None

    def as_event_payload(self, booking_reference: str, booking_id: str, amount: Decimal) -> dict:
        return {
            "provider": PROVIDER_NAME,
            "provider_payment_id": self.provider_payment_id,
            "booking_id": booking_id,
            "booking_reference": booking_reference,
            "amount": str(amount),
            "currency": "INR",
            "status": self.status.value,
            "failure_code": self.failure_code,
            "failure_reason": self.failure_reason,
        }


class MockPaymentGateway:
    async def charge(
        self,
        *,
        booking_id: str,
        booking_reference: str,
        amount: Decimal,
        mode: SimulateMode = "success",
    ) -> GatewayChargeResult:
        seed = f"{booking_id}:{mode}:{uuid.uuid4().hex}"
        digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
        provider_payment_id = f"mpay_{digest[:24]}"
        event_id = f"evt_{digest[24:56]}"

        if mode == "success":
            return GatewayChargeResult(
                provider_payment_id=provider_payment_id,
                status=PaymentStatus.SUCCESS,
                event_type=WebhookEventType.PAYMENT_SUCCEEDED,
                event_id=event_id,
            )

        code, reason = _DECLINE_REASONS.get(mode, _DECLINE_REASONS["failure"])
        return GatewayChargeResult(
            provider_payment_id=provider_payment_id,
            status=PaymentStatus.FAILED,
            event_type=WebhookEventType.PAYMENT_FAILED,
            event_id=event_id,
            failure_code=code,
            failure_reason=reason,
        )


mock_gateway = MockPaymentGateway()
