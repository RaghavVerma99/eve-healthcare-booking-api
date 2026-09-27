import uuid

import pytest
from httpx import AsyncClient
from tests.helpers import create_booking, pay_booking


async def test_successful_payment_confirms_booking(
    client: AsyncClient, auth_headers, catalogue
) -> None:
    booking = await create_booking(client, auth_headers, catalogue)
    result = await pay_booking(client, auth_headers, booking["id"])

    assert result["status_code"] == 201, result["response"].text
    body = result["body"]
    assert body["payment"]["status"] == "SUCCESS"
    assert body["payment"]["amount"] == "500.00"
    assert body["payment"]["provider_payment_id"].startswith("mpay_")
    assert body["booking_status"] == "CONFIRMED"

    detail = await client.get(f"/bookings/{booking['id']}", headers=auth_headers)
    assert detail.json()["status"] == "CONFIRMED"
    assert len(detail.json()["payments"]) == 1


async def test_failed_payment_marks_booking_failed(
    client: AsyncClient, auth_headers, catalogue
) -> None:
    booking = await create_booking(client, auth_headers, catalogue)
    result = await pay_booking(client, auth_headers, booking["id"], simulate="failure")

    body = result["body"]
    assert body["payment"]["status"] == "FAILED"
    assert body["payment"]["failure_code"] == "payment_declined"
    assert body["booking_status"] == "FAILED"


@pytest.mark.parametrize(
    "mode,expected_code",
    [
        ("insufficient_funds", "insufficient_funds"),
        ("gateway_error", "gateway_error"),
    ],
)
async def test_payment_failure_modes(
    client: AsyncClient, auth_headers, catalogue, mode: str, expected_code: str
) -> None:
    booking = await create_booking(client, auth_headers, catalogue)
    result = await pay_booking(client, auth_headers, booking["id"], simulate=mode)
    assert result["body"]["payment"]["failure_code"] == expected_code
    assert result["body"]["booking_status"] == "FAILED"


async def test_invalid_simulate_mode_rejected(client: AsyncClient, auth_headers, catalogue) -> None:
    booking = await create_booking(client, auth_headers, catalogue)
    response = await client.post(
        "/payments/",
        json={"booking_id": booking["id"], "simulate": "definitely-not-a-mode"},
        headers=auth_headers,
    )
    assert response.status_code == 422


async def test_payment_requires_authentication(
    client: AsyncClient, catalogue, auth_headers
) -> None:
    booking = await create_booking(client, auth_headers, catalogue)
    response = await client.post("/payments/", json={"booking_id": booking["id"]})
    assert response.status_code == 401


async def test_payment_for_unknown_booking_returns_404(client: AsyncClient, auth_headers) -> None:
    response = await client.post(
        "/payments/", json={"booking_id": str(uuid.uuid4())}, headers=auth_headers
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "booking_not_found"


async def test_payment_for_malformed_booking_id_returns_422(
    client: AsyncClient, auth_headers
) -> None:
    response = await client.post(
        "/payments/", json={"booking_id": "not-a-uuid"}, headers=auth_headers
    )
    assert response.status_code == 422


async def test_cannot_pay_for_another_users_booking(
    client: AsyncClient, auth_headers, other_auth_headers, catalogue
) -> None:
    booking = await create_booking(client, auth_headers, catalogue)
    result = await pay_booking(client, other_auth_headers, booking["id"])
    assert result["status_code"] == 403
    assert result["body"]["error"]["code"] == "booking_forbidden"


async def test_cannot_pay_twice_for_confirmed_booking(
    client: AsyncClient, auth_headers, catalogue
) -> None:
    booking = await create_booking(client, auth_headers, catalogue)
    await pay_booking(client, auth_headers, booking["id"])

    result = await pay_booking(client, auth_headers, booking["id"])
    assert result["status_code"] == 409
    assert result["body"]["error"]["code"] == "payment_already_successful"


async def test_cannot_pay_for_cancelled_booking(
    client: AsyncClient, auth_headers, catalogue
) -> None:
    booking = await create_booking(client, auth_headers, catalogue)
    await client.post(f"/bookings/{booking['id']}/cancel", headers=auth_headers)

    result = await pay_booking(client, auth_headers, booking["id"])
    assert result["status_code"] == 409
    assert result["body"]["error"]["code"] == "invalid_state_transition"


async def test_retry_payment_after_failure(client: AsyncClient, auth_headers, catalogue) -> None:
    booking = await create_booking(client, auth_headers, catalogue)
    await pay_booking(client, auth_headers, booking["id"], simulate="failure")

    retry = await pay_booking(client, auth_headers, booking["id"], simulate="success")
    assert retry["status_code"] == 201
    assert retry["body"]["payment"]["status"] == "SUCCESS"
    assert retry["body"]["booking_status"] == "CONFIRMED"

    payments = await client.get(f"/bookings/{booking['id']}/payments", headers=auth_headers)
    assert len(payments.json()) == 2
    statuses = sorted(payment["status"] for payment in payments.json())
    assert statuses == ["FAILED", "SUCCESS"]


async def test_payment_idempotency_key_returns_same_payment(
    client: AsyncClient, auth_headers, catalogue
) -> None:
    booking = await create_booking(client, auth_headers, catalogue)
    first = await pay_booking(client, auth_headers, booking["id"], idempotency_key="pay-1")
    assert first["status_code"] == 201

    replay = await pay_booking(client, auth_headers, booking["id"], idempotency_key="pay-1")
    assert replay["status_code"] == 200
    assert replay["body"]["duplicate"] is True
    assert replay["body"]["payment"]["id"] == first["body"]["payment"]["id"]

    payments = await client.get(f"/bookings/{booking['id']}/payments", headers=auth_headers)
    assert len(payments.json()) == 1


async def test_get_payment_enforces_ownership(
    client: AsyncClient, auth_headers, other_auth_headers, catalogue
) -> None:
    booking = await create_booking(client, auth_headers, catalogue)
    payment = (await pay_booking(client, auth_headers, booking["id"]))["body"]["payment"]

    ok = await client.get(f"/payments/{payment['id']}", headers=auth_headers)
    assert ok.status_code == 200
    assert ok.json()["status"] == "SUCCESS"

    forbidden = await client.get(f"/payments/{payment['id']}", headers=other_auth_headers)
    assert forbidden.status_code == 403
    assert forbidden.json()["error"]["code"] == "booking_forbidden"

    missing = await client.get(f"/payments/{uuid.uuid4()}", headers=auth_headers)
    assert missing.status_code == 404


async def test_payment_response_echoes_replayable_webhook_event(
    client: AsyncClient, auth_headers, catalogue
) -> None:
    booking = await create_booking(client, auth_headers, catalogue)
    result = await pay_booking(client, auth_headers, booking["id"])
    body = result["body"]

    event = body["webhook"]
    assert event["event_id"].startswith("evt_")
    assert event["event_type"] == "payment.succeeded"
    assert event["data"]["payment_id"] == body["payment"]["id"]

    replay = await client.post("/payments/webhook/", json=event)
    assert replay.status_code == 200
    assert replay.json()["duplicate"] is True
    assert replay.json()["booking_status"] == "CONFIRMED"
