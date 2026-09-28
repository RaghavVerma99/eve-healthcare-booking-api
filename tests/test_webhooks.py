import asyncio
import uuid

from app.core.security import sign_webhook_payload
from httpx import AsyncClient
from tests.helpers import create_booking, pay_booking, send_webhook


async def test_webhook_success_event_confirms_booking(
    client: AsyncClient, auth_headers, catalogue
) -> None:
    booking = await create_booking(client, auth_headers, catalogue)
    payment = (await pay_booking(client, auth_headers, booking["id"], simulate="failure"))["body"][
        "payment"
    ]
    assert payment["status"] == "FAILED"

    result = await send_webhook(
        client,
        event_id="evt_success_1",
        event_type="payment.succeeded",
        data={
            "provider": "mockpay",
            "payment_id": payment["id"],
            "provider_payment_id": "mpay_retry_001",
            "booking_id": booking["id"],
            "status": "SUCCESS",
        },
    )
    assert result["status_code"] == 200, result["response"].text
    assert result["body"]["duplicate"] is False
    assert result["body"]["booking_status"] == "CONFIRMED"

    detail = await client.get(f"/bookings/{booking['id']}", headers=auth_headers)
    assert detail.json()["status"] == "CONFIRMED"
    assert detail.json()["payments"][0]["provider_payment_id"] == "mpay_retry_001"


async def test_replayed_webhook_is_idempotent(client: AsyncClient, auth_headers, catalogue) -> None:
    booking = await create_booking(client, auth_headers, catalogue)
    payment = (await pay_booking(client, auth_headers, booking["id"], simulate="failure"))["body"][
        "payment"
    ]
    payload = {
        "provider": "mockpay",
        "payment_id": payment["id"],
        "provider_payment_id": "mpay_replay_001",
        "booking_id": booking["id"],
    }

    first = await send_webhook(
        client, event_id="evt_replay_1", event_type="payment.succeeded", data=payload
    )
    assert first["body"]["duplicate"] is False
    assert first["body"]["booking_status"] == "CONFIRMED"

    for _ in range(4):
        replay = await send_webhook(
            client, event_id="evt_replay_1", event_type="payment.succeeded", data=payload
        )
        assert replay["status_code"] == 200
        assert replay["body"]["duplicate"] is True
        assert replay["body"]["booking_status"] == "CONFIRMED"

    payments = await client.get(f"/bookings/{booking['id']}/payments", headers=auth_headers)
    assert len(payments.json()) == 1
    assert payments.json()[0]["status"] == "SUCCESS"

    detail = await client.get(f"/bookings/{booking['id']}", headers=auth_headers)
    assert detail.json()["status"] == "CONFIRMED"
    assert detail.json()["updated_at"]


async def test_replayed_webhook_does_not_duplicate_booking_state(
    client: AsyncClient, auth_headers, catalogue
) -> None:
    booking = await create_booking(client, auth_headers, catalogue)
    await pay_booking(client, auth_headers, booking["id"])
    initial = await client.get(f"/bookings/{booking['id']}", headers=auth_headers)

    await send_webhook(
        client,
        event_id="evt_after_pay",
        event_type="payment.succeeded",
        data={"payment_id": initial.json()["payments"][0]["id"]},
    )
    after = await client.get(f"/bookings/{booking['id']}", headers=auth_headers)
    assert after.json()["status"] == "CONFIRMED"
    assert len(after.json()["payments"]) == 1
    assert after.json()["updated_at"] == initial.json()["updated_at"]


async def test_concurrent_duplicate_webhooks_are_processed_once(
    client: AsyncClient, auth_headers, catalogue, concurrent_client: AsyncClient
) -> None:
    """Five simultaneous deliveries of one event must apply exactly once."""
    booking = await create_booking(client, auth_headers, catalogue)
    payment = (await pay_booking(client, auth_headers, booking["id"], simulate="failure"))["body"][
        "payment"
    ]
    payload = {
        "provider": "mockpay",
        "payment_id": payment["id"],
        "provider_payment_id": "mpay_race_001",
    }

    results = await asyncio.gather(
        *[
            send_webhook(
                concurrent_client,
                event_id="evt_race_1",
                event_type="payment.succeeded",
                data=payload,
            )
            for _ in range(5)
        ]
    )
    assert all(result["status_code"] == 200 for result in results)
    assert sum(1 for result in results if not result["body"]["duplicate"]) == 1

    payments = await client.get(f"/bookings/{booking['id']}/payments", headers=auth_headers)
    assert len(payments.json()) == 1
    assert payments.json()[0]["status"] == "SUCCESS"


async def test_different_event_ids_for_same_payment_do_not_double_apply(
    client: AsyncClient, auth_headers, catalogue
) -> None:
    booking = await create_booking(client, auth_headers, catalogue)
    payment = (await pay_booking(client, auth_headers, booking["id"], simulate="failure"))["body"][
        "payment"
    ]
    payload = {"payment_id": payment["id"], "provider_payment_id": "mpay_multi_event"}

    first = await send_webhook(
        client, event_id="evt_multi_a", event_type="payment.succeeded", data=payload
    )
    second = await send_webhook(
        client, event_id="evt_multi_b", event_type="payment.succeeded", data=payload
    )
    assert first["body"]["duplicate"] is False
    assert second["body"]["duplicate"] is False
    assert second["body"]["booking_status"] == "CONFIRMED"

    detail = await client.get(f"/bookings/{booking['id']}", headers=auth_headers)
    assert len(detail.json()["payments"]) == 1
    assert detail.json()["status"] == "CONFIRMED"


async def test_refund_event_cancels_confirmed_booking(
    client: AsyncClient, auth_headers, catalogue
) -> None:
    booking = await create_booking(client, auth_headers, catalogue)
    payment = (await pay_booking(client, auth_headers, booking["id"]))["body"]["payment"]

    result = await send_webhook(
        client,
        event_id="evt_refund_1",
        event_type="payment.refunded",
        data={"payment_id": payment["id"]},
    )
    assert result["body"]["booking_status"] == "CANCELLED"

    detail = await client.get(f"/bookings/{booking['id']}", headers=auth_headers)
    assert detail.json()["status"] == "CANCELLED"
    assert detail.json()["payments"][0]["status"] == "REFUNDED"


async def test_late_success_event_cannot_reopen_cancelled_booking(
    client: AsyncClient, auth_headers, catalogue
) -> None:
    booking = await create_booking(client, auth_headers, catalogue)
    payment = (await pay_booking(client, auth_headers, booking["id"], simulate="failure"))["body"][
        "payment"
    ]
    await client.post(f"/bookings/{booking['id']}/cancel", headers=auth_headers)

    result = await send_webhook(
        client,
        event_id="evt_late_success",
        event_type="payment.succeeded",
        data={"payment_id": payment["id"]},
    )
    assert result["status_code"] == 200
    assert result["body"]["booking_status"] == "CANCELLED"

    detail = await client.get(f"/bookings/{booking['id']}", headers=auth_headers)
    assert detail.json()["status"] == "CANCELLED"


async def test_webhook_for_unknown_payment_returns_404(client: AsyncClient) -> None:
    result = await send_webhook(
        client,
        event_id="evt_unknown",
        event_type="payment.succeeded",
        data={"payment_id": str(uuid.uuid4())},
    )
    assert result["status_code"] == 404
    assert result["body"]["error"]["code"] == "payment_not_found"


async def test_webhook_with_unknown_event_type_returns_422(client: AsyncClient) -> None:
    result = await send_webhook(
        client,
        event_id="evt_bad_type",
        event_type="payment.exploded",
        data={},
    )
    assert result["status_code"] == 422
    assert result["body"]["error"]["code"] == "validation_error"


async def test_webhook_with_short_event_id_returns_422(client: AsyncClient) -> None:
    result = await send_webhook(client, event_id="ab", event_type="payment.succeeded", data={})
    assert result["status_code"] == 422


async def test_webhook_with_malformed_ids_returns_422(client: AsyncClient) -> None:
    result = await send_webhook(
        client,
        event_id="evt_malformed",
        event_type="payment.succeeded",
        data={"payment_id": "not-a-uuid"},
    )
    assert result["status_code"] == 422
    assert result["body"]["error"]["code"] == "invalid_identifier"


async def test_webhook_resolves_payment_by_booking_reference(
    client: AsyncClient, auth_headers, catalogue
) -> None:
    booking = await create_booking(client, auth_headers, catalogue)
    await pay_booking(client, auth_headers, booking["id"], simulate="failure")

    result = await send_webhook(
        client,
        event_id="evt_by_reference",
        event_type="payment.succeeded",
        data={"booking_reference": booking["reference"], "provider": "mockpay"},
    )
    assert result["body"]["booking_status"] == "CONFIRMED"


async def test_webhook_with_invalid_signature_is_rejected_when_enforced(
    client: AsyncClient, session, auth_headers, catalogue, monkeypatch
) -> None:
    from app.core.config import settings

    booking = await create_booking(client, auth_headers, catalogue)
    payment = (await pay_booking(client, auth_headers, booking["id"], simulate="failure"))["body"][
        "payment"
    ]
    monkeypatch.setattr(settings, "webhook_require_signature", True)

    rejected = await send_webhook(
        client,
        event_id="evt_bad_signature",
        event_type="payment.succeeded",
        data={"payment_id": payment["id"]},
        signature="deadbeef",
    )
    assert rejected["status_code"] == 401
    assert rejected["body"]["error"]["code"] == "invalid_webhook_signature"

    body = {
        "event_id": "evt_good_signature",
        "event_type": "payment.succeeded",
        "data": {"payment_id": payment["id"]},
    }
    import json

    raw = json.dumps(body).encode()
    accepted = await client.post(
        "/payments/webhook/",
        content=raw,
        headers={
            "Content-Type": "application/json",
            "X-Signature": sign_webhook_payload(raw),
        },
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["booking_status"] == "CONFIRMED"


async def test_webhook_retry_after_failed_event_is_processed(
    client: AsyncClient, auth_headers, catalogue, monkeypatch
) -> None:
    from app.core.config import settings

    booking = await create_booking(client, auth_headers, catalogue)
    payment = (await pay_booking(client, auth_headers, booking["id"], simulate="failure"))["body"][
        "payment"
    ]
    monkeypatch.setattr(settings, "webhook_require_signature", True)

    first = await send_webhook(
        client,
        event_id="evt_retryable",
        event_type="payment.succeeded",
        data={"payment_id": payment["id"]},
    )
    assert first["status_code"] == 401

    monkeypatch.setattr(settings, "webhook_require_signature", False)
    second = await send_webhook(
        client,
        event_id="evt_retryable",
        event_type="payment.succeeded",
        data={"payment_id": payment["id"]},
    )
    assert second["status_code"] == 200
    assert second["body"]["booking_status"] == "CONFIRMED"
    assert second["body"]["duplicate"] is False


async def test_admin_can_inspect_stored_event(
    client: AsyncClient, auth_headers, admin_headers, catalogue
) -> None:
    booking = await create_booking(client, auth_headers, catalogue)
    payment = (await pay_booking(client, auth_headers, booking["id"]))["body"]["payment"]

    result = await send_webhook(
        client,
        event_id="evt_inspect",
        event_type="payment.succeeded",
        data={"payment_id": payment["id"]},
    )
    assert result["status_code"] == 200

    forbidden = await client.get("/payments/webhook/evt_inspect", headers=auth_headers)
    assert forbidden.status_code == 403

    allowed = await client.get("/payments/webhook/evt_inspect", headers=admin_headers)
    assert allowed.status_code == 200
    assert allowed.json()["event_id"] == "evt_inspect"
    assert allowed.json()["attempts"] == 1
    assert allowed.json()["payment_id"] == payment["id"]
    # This delivery arrived while signature checking was disabled, which is not
    # the same as failing verification and must not be recorded as such.
    assert allowed.json()["signature_valid"] is None


async def test_rejected_signature_is_recorded_as_invalid(
    client: AsyncClient, auth_headers, admin_headers, catalogue, monkeypatch
) -> None:
    from app.core.config import settings

    monkeypatch.setattr(settings, "webhook_require_signature", True)
    booking = await create_booking(client, auth_headers, catalogue)
    payment = (await pay_booking(client, auth_headers, booking["id"], simulate="failure"))["body"][
        "payment"
    ]
    rejected = await send_webhook(
        client,
        event_id="evt_recorded_reject",
        event_type="payment.succeeded",
        data={"payment_id": payment["id"]},
        signature="not-the-right-digest",
    )
    assert rejected["status_code"] == 401


async def test_signature_may_carry_an_algorithm_prefix(
    client: AsyncClient, auth_headers, catalogue, monkeypatch
) -> None:
    """Providers send `sha256=<hex>`; a bare `<hex>` has to keep working too."""
    import json

    from app.core.config import settings
    from app.core.security import sign_webhook_payload

    booking = await create_booking(client, auth_headers, catalogue)
    payment = (await pay_booking(client, auth_headers, booking["id"], simulate="failure"))["body"][
        "payment"
    ]
    monkeypatch.setattr(settings, "webhook_require_signature", True)

    forms = ["bare", "prefixed", "uppercase"]
    for form in forms:
        body = {
            "event_id": f"evt_sigform_{form}",
            "event_type": "payment.succeeded",
            "data": {"payment_id": payment["id"]},
        }
        raw = json.dumps(body).encode()
        digest = sign_webhook_payload(raw)
        header = {
            "bare": digest,
            "prefixed": f"sha256={digest}",
            "uppercase": f"SHA256={digest.upper()}",
        }[form]
        response = await client.post(
            "/payments/webhook/",
            content=raw,
            headers={"Content-Type": "application/json", "X-Signature": header},
        )
        assert response.status_code == 200, (form, response.text)


async def test_settlement_for_a_different_amount_is_rejected(
    client: AsyncClient, auth_headers, catalogue
) -> None:
    """A provider still has to agree with the invoice it is settling."""
    booking = await create_booking(client, auth_headers, catalogue)
    paid = await pay_booking(client, auth_headers, booking["id"], simulate="failure")
    payment = paid["body"]["payment"]

    result = await send_webhook(
        client,
        event_id="evt_wrong_amount",
        event_type="payment.succeeded",
        data={"payment_id": payment["id"], "amount": "0.01", "currency": "INR"},
    )
    assert result["status_code"] == 422
    assert result["body"]["error"]["code"] == "amount_mismatch"

    detail = await client.get(f"/bookings/{booking['id']}", headers=auth_headers)
    assert detail.json()["status"] == "FAILED"
    assert detail.json()["payments"][0]["status"] == "FAILED"


async def test_settlement_in_a_foreign_currency_is_rejected(
    client: AsyncClient, auth_headers, catalogue
) -> None:
    booking = await create_booking(client, auth_headers, catalogue)
    paid = await pay_booking(client, auth_headers, booking["id"], simulate="failure")
    payment = paid["body"]["payment"]

    result = await send_webhook(
        client,
        event_id="evt_wrong_currency",
        event_type="payment.succeeded",
        data={
            "payment_id": payment["id"],
            "amount": payment["amount"],
            "currency": "USD",
        },
    )
    assert result["status_code"] == 422
    assert result["body"]["error"]["code"] == "currency_mismatch"


async def test_matching_amount_is_accepted(
    client: AsyncClient, auth_headers, catalogue
) -> None:
    """The check must not reject the normal case the gateway itself emits."""
    booking = await create_booking(client, auth_headers, catalogue)
    paid = await pay_booking(client, auth_headers, booking["id"], simulate="failure")
    payment = paid["body"]["payment"]

    result = await send_webhook(
        client,
        event_id="evt_matching_amount",
        event_type="payment.succeeded",
        data={
            "payment_id": payment["id"],
            "amount": payment["amount"],
            "currency": payment["currency"],
        },
    )
    assert result["status_code"] == 200
    assert result["body"]["booking_status"] == "CONFIRMED"


async def test_non_numeric_amount_is_rejected(
    client: AsyncClient, auth_headers, catalogue
) -> None:
    booking = await create_booking(client, auth_headers, catalogue)
    payment = (await pay_booking(client, auth_headers, booking["id"], simulate="failure"))["body"][
        "payment"
    ]

    result = await send_webhook(
        client,
        event_id="evt_nan_amount",
        event_type="payment.succeeded",
        data={"payment_id": payment["id"], "amount": "not-a-number"},
    )
    assert result["status_code"] == 422
    assert result["body"]["error"]["code"] == "invalid_amount"


async def test_settled_provider_reference_is_not_overwritten(
    client: AsyncClient, auth_headers, catalogue
) -> None:
    """A second event must not rewrite the reference the first one recorded."""
    booking = await create_booking(client, auth_headers, catalogue)
    payment = (await pay_booking(client, auth_headers, booking["id"]))["body"]["payment"]
    original = payment["provider_payment_id"]
    assert original

    result = await send_webhook(
        client,
        event_id="evt_ref_smuggle",
        event_type="payment.succeeded",
        data={"payment_id": payment["id"], "provider_payment_id": "mpay_smuggled"},
    )
    assert result["status_code"] == 200

    stored = await client.get(f"/payments/{payment['id']}", headers=auth_headers)
    assert stored.json()["provider_payment_id"] == original


async def test_retry_after_failure_may_record_a_new_provider_reference(
    client: AsyncClient, auth_headers, catalogue
) -> None:
    """An unsettled payment still accepts the reference from its retry."""
    booking = await create_booking(client, auth_headers, catalogue)
    payment = (await pay_booking(client, auth_headers, booking["id"], simulate="failure"))["body"][
        "payment"
    ]

    result = await send_webhook(
        client,
        event_id="evt_retry_ref",
        event_type="payment.succeeded",
        data={"payment_id": payment["id"], "provider_payment_id": "mpay_retry_ref"},
    )
    assert result["body"]["booking_status"] == "CONFIRMED"

    stored = await client.get(f"/payments/{payment['id']}", headers=auth_headers)
    assert stored.json()["provider_payment_id"] == "mpay_retry_ref"


async def test_recycled_event_id_is_acked_against_the_requested_payment(
    client: AsyncClient, auth_headers, catalogue
) -> None:
    """A duplicate ack must describe the payment the caller just asked about.

    Reporting the first event's booking made a provider that reused an
    `event_id` believe its second payment had been settled when it had not.
    """
    first = await create_booking(client, auth_headers, catalogue)
    second = await create_booking(client, auth_headers, catalogue)
    pay_a = (await pay_booking(client, auth_headers, first["id"], simulate="failure"))["body"][
        "payment"
    ]
    pay_b = (await pay_booking(client, auth_headers, second["id"], simulate="failure"))["body"][
        "payment"
    ]

    original = await send_webhook(
        client,
        event_id="evt_recycled",
        event_type="payment.succeeded",
        data={"payment_id": pay_a["id"]},
    )
    assert original["body"]["booking_status"] == "CONFIRMED"

    replay = await send_webhook(
        client,
        event_id="evt_recycled",
        event_type="payment.succeeded",
        data={"payment_id": pay_b["id"]},
    )
    assert replay["status_code"] == 200
    assert replay["body"]["duplicate"] is True
    assert replay["body"]["payment_id"] == pay_b["id"]
    assert replay["body"]["booking_status"] == "FAILED"

    detail = await client.get(f"/bookings/{second['id']}", headers=auth_headers)
    assert detail.json()["status"] == "FAILED"


async def test_replay_of_an_unresolvable_duplicate_still_acks(
    client: AsyncClient, auth_headers, catalogue
) -> None:
    """A repeat delivery with an unusable body must not turn into a 404."""
    booking = await create_booking(client, auth_headers, catalogue)
    payment = (await pay_booking(client, auth_headers, booking["id"]))["body"]["payment"]
    await send_webhook(
        client,
        event_id="evt_dup_unusable",
        event_type="payment.succeeded",
        data={"payment_id": payment["id"]},
    )

    replay = await send_webhook(
        client,
        event_id="evt_dup_unusable",
        event_type="payment.succeeded",
        data={"payment_id": "not-a-uuid"},
    )
    assert replay["status_code"] == 200
    assert replay["body"]["duplicate"] is True
    assert replay["body"]["booking_status"] == "CONFIRMED"


async def test_replay_endpoint_is_admin_only_and_reports_queue_failure(
    client: AsyncClient, auth_headers, admin_headers, catalogue, monkeypatch
) -> None:
    booking = await create_booking(client, auth_headers, catalogue)
    payment = (await pay_booking(client, auth_headers, booking["id"]))["body"]["payment"]
    await send_webhook(
        client,
        event_id="evt_replayable",
        event_type="payment.succeeded",
        data={"payment_id": payment["id"]},
    )

    assert (
        await client.post("/payments/webhook/evt_replayable/replay", headers=auth_headers)
    ).status_code == 403

    import app.api.routers.payments as payments_router

    def boom(event_id: str) -> str:
        raise ConnectionError("broker down")

    monkeypatch.setattr(payments_router, "enqueue_replay", boom)
    unavailable = await client.post(
        "/payments/webhook/evt_replayable/replay", headers=admin_headers
    )
    assert unavailable.status_code == 503
    assert unavailable.json()["error"]["code"] == "queue_unavailable"

    monkeypatch.setattr(payments_router, "enqueue_replay", lambda event_id: "task-123")
    queued = await client.post("/payments/webhook/evt_replayable/replay", headers=admin_headers)
    assert queued.status_code == 202
    assert "task-123" in queued.json()["message"]

    missing = await client.post("/payments/webhook/evt_absent/replay", headers=admin_headers)
    assert missing.status_code == 404
