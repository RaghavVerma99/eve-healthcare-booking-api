import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from tests.helpers import create_booking, future_appointment


async def test_create_booking_snapshots_price(client: AsyncClient, auth_headers, catalogue) -> None:
    booking = await create_booking(client, auth_headers, catalogue)
    assert booking["status"] == "PENDING"
    assert booking["amount"] == "500.00"
    assert booking["reference"].startswith("BK-")
    assert booking["test"]["code"] == "CBC"
    assert booking["centre"]["id"] == str(catalogue["centre"].id)
    assert booking["payments"] == []


async def test_create_booking_requires_authentication(client: AsyncClient, catalogue) -> None:
    response = await client.post(
        "/bookings/",
        json={
            "test_id": str(catalogue["cbc"].id),
            "centre_id": str(catalogue["centre"].id),
            "appointment_at": future_appointment(),
        },
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "missing_credentials"


async def test_create_booking_rejects_test_not_offered_at_centre(
    client: AsyncClient, auth_headers, catalogue
) -> None:
    response = await client.post(
        "/bookings/",
        json={
            "test_id": str(catalogue["cbc"].id),
            "centre_id": str(catalogue["other_centre"].id),
            "appointment_at": future_appointment(),
        },
        headers=auth_headers,
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "test_not_offered_at_centre"


async def test_create_booking_rejects_unknown_ids(
    client: AsyncClient, auth_headers, catalogue
) -> None:
    response = await client.post(
        "/bookings/",
        json={
            "test_id": str(uuid.uuid4()),
            "centre_id": str(catalogue["centre"].id),
            "appointment_at": future_appointment(),
        },
        headers=auth_headers,
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "catalogue_not_found"


async def test_create_booking_rejects_non_uuid_ids(client: AsyncClient, auth_headers) -> None:
    response = await client.post(
        "/bookings/",
        json={"test_id": "abc", "centre_id": "def", "appointment_at": future_appointment()},
        headers=auth_headers,
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


@pytest.mark.parametrize(
    "offset_hours,expected_code",
    [
        (-5, "appointment_too_soon"),
        (0, "appointment_too_soon"),
        (24 * 200, "appointment_too_far"),
    ],
)
async def test_create_booking_validates_appointment_window(
    client: AsyncClient, auth_headers, catalogue, offset_hours: int, expected_code: str
) -> None:
    appointment = datetime.now(UTC) + timedelta(hours=offset_hours)
    response = await client.post(
        "/bookings/",
        json={
            "test_id": str(catalogue["cbc"].id),
            "centre_id": str(catalogue["centre"].id),
            "appointment_at": appointment.isoformat(),
        },
        headers=auth_headers,
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == expected_code


async def test_create_booking_rejects_naive_datetime(
    client: AsyncClient, auth_headers, catalogue
) -> None:
    naive = datetime.now() + timedelta(days=2)
    response = await client.post(
        "/bookings/",
        json={
            "test_id": str(catalogue["cbc"].id),
            "centre_id": str(catalogue["centre"].id),
            "appointment_at": naive.isoformat(),
        },
        headers=auth_headers,
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


async def test_double_booking_same_slot_conflicts(
    client: AsyncClient, auth_headers, catalogue
) -> None:
    slot = future_appointment(72)
    await create_booking(client, auth_headers, catalogue, appointment_at=slot)
    response = await client.post(
        "/bookings/",
        json={
            "test_id": str(catalogue["cbc"].id),
            "centre_id": str(catalogue["centre"].id),
            "appointment_at": slot,
        },
        headers=auth_headers,
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "appointment_slot_taken"


async def test_booking_idempotency_key_returns_same_booking(
    client: AsyncClient, auth_headers, catalogue
) -> None:
    payload = {
        "test_id": str(catalogue["cbc"].id),
        "centre_id": str(catalogue["centre"].id),
        "appointment_at": future_appointment(),
        "idempotency_key": "client-req-1",
    }
    first = await client.post("/bookings/", json=payload, headers=auth_headers)
    assert first.status_code == 201

    replay = await client.post("/bookings/", json=payload, headers=auth_headers)
    assert replay.status_code == 200, replay.text
    assert replay.json()["id"] == first.json()["id"]

    listing = await client.get("/bookings/", headers=auth_headers)
    assert listing.json()["total"] == 1


async def test_booking_idempotency_key_cannot_be_reused_by_another_user(
    client: AsyncClient, auth_headers, other_auth_headers, catalogue
) -> None:
    payload = {
        "test_id": str(catalogue["cbc"].id),
        "centre_id": str(catalogue["centre"].id),
        "appointment_at": future_appointment(),
        "idempotency_key": "shared-key",
    }
    assert (await client.post("/bookings/", json=payload, headers=auth_headers)).status_code == 201
    response = await client.post("/bookings/", json=payload, headers=other_auth_headers)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "idempotency_key_conflict"


async def test_get_booking_hides_other_users_booking(
    client: AsyncClient, auth_headers, other_auth_headers, catalogue
) -> None:
    booking = await create_booking(client, auth_headers, catalogue)
    response = await client.get(f"/bookings/{booking['id']}", headers=other_auth_headers)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "booking_forbidden"


async def test_get_unknown_booking_returns_404(client: AsyncClient, auth_headers) -> None:
    response = await client.get(f"/bookings/{uuid.uuid4()}", headers=auth_headers)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "booking_not_found"


async def test_list_bookings_is_scoped_to_owner(
    client: AsyncClient, auth_headers, other_auth_headers, catalogue
) -> None:
    await create_booking(client, auth_headers, catalogue)
    await create_booking(client, other_auth_headers, catalogue)

    mine = await client.get("/bookings/", headers=auth_headers)
    theirs = await client.get("/bookings/", headers=other_auth_headers)
    assert mine.json()["total"] == 1
    assert theirs.json()["total"] == 1
    assert mine.json()["items"][0]["id"] != theirs.json()["items"][0]["id"]


async def test_list_bookings_filters_by_status(
    client: AsyncClient, auth_headers, catalogue
) -> None:
    await create_booking(client, auth_headers, catalogue)
    response = await client.get("/bookings/", params={"status": "CANCELLED"}, headers=auth_headers)
    assert response.json()["total"] == 0

    response = await client.get("/bookings/", params={"status": "PENDING"}, headers=auth_headers)
    assert response.json()["total"] == 1


async def test_list_bookings_rejects_invalid_status_filter(
    client: AsyncClient, auth_headers, catalogue
) -> None:
    response = await client.get(
        "/bookings/", params={"status": "NOT_A_STATUS"}, headers=auth_headers
    )
    assert response.status_code == 422


async def test_cancel_booking(client: AsyncClient, auth_headers, catalogue) -> None:
    booking = await create_booking(client, auth_headers, catalogue)
    response = await client.post(
        f"/bookings/{booking['id']}/cancel", headers=auth_headers
    )
    assert response.status_code == 200
    assert response.json()["status"] == "CANCELLED"
    assert response.json()["cancelled_at"] is not None


async def test_cancel_is_not_allowed_twice(client: AsyncClient, auth_headers, catalogue) -> None:
    booking = await create_booking(client, auth_headers, catalogue)
    await client.post(f"/bookings/{booking['id']}/cancel", headers=auth_headers)
    response = await client.post(f"/bookings/{booking['id']}/cancel", headers=auth_headers)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "status_unchanged"


async def test_cancel_other_users_booking_forbidden(
    client: AsyncClient, auth_headers, other_auth_headers, catalogue
) -> None:
    booking = await create_booking(client, auth_headers, catalogue)
    response = await client.post(
        f"/bookings/{booking['id']}/cancel", headers=other_auth_headers
    )
    assert response.status_code == 403


async def test_admin_can_read_any_booking(
    client: AsyncClient, auth_headers, admin_headers, catalogue
) -> None:
    booking = await create_booking(client, auth_headers, catalogue)
    detail = await client.get(f"/bookings/{booking['id']}", headers=admin_headers)
    assert detail.status_code == 200
    listing = await client.get("/bookings/", headers=admin_headers)
    assert listing.json()["total"] == 1


async def test_delete_booking_cancels(client: AsyncClient, auth_headers, catalogue) -> None:
    booking = await create_booking(client, auth_headers, catalogue)
    response = await client.delete(f"/bookings/{booking['id']}", headers=auth_headers)
    assert response.status_code == 200
    detail = await client.get(f"/bookings/{booking['id']}", headers=auth_headers)
    assert detail.json()["status"] == "CANCELLED"
