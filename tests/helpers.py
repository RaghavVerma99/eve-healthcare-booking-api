from datetime import UTC, datetime, timedelta

from httpx import AsyncClient


def future_appointment(hours: int = 48) -> str:
    return (datetime.now(UTC) + timedelta(hours=hours)).isoformat()


async def create_booking(
    client: AsyncClient,
    headers: dict[str, str],
    catalogue: dict,
    *,
    centre_id: str | None = None,
    test_id: str | None = None,
    appointment_at: str | None = None,
    **extra,
) -> dict:
    payload = {
        "test_id": test_id or str(catalogue["cbc"].id),
        "centre_id": centre_id or str(catalogue["centre"].id),
        "appointment_at": appointment_at or future_appointment(),
        **extra,
    }
    response = await client.post("/bookings/", json=payload, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


async def pay_booking(
    client: AsyncClient,
    headers: dict[str, str],
    booking_id: str,
    simulate: str = "success",
    **extra,
) -> dict:
    response = await client.post(
        "/payments/",
        json={"booking_id": booking_id, "simulate": simulate, **extra},
        headers=headers,
    )
    return {"status_code": response.status_code, "body": response.json(), "response": response}


async def send_webhook(
    client: AsyncClient,
    *,
    event_id: str,
    event_type: str,
    data: dict,
    signature: str | None = None,
) -> dict:
    headers = {"X-Signature": signature} if signature else {}
    response = await client.post(
        "/payments/webhook/",
        json={"event_id": event_id, "event_type": event_type, "data": data},
        headers=headers,
    )
    return {"status_code": response.status_code, "body": response.json(), "response": response}
