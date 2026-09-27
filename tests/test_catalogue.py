import uuid

import pytest
from httpx import AsyncClient


async def test_list_centres_is_paginated(client: AsyncClient, catalogue) -> None:
    response = await client.get("/centres/", params={"page": 1, "size": 1})
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    assert body["page"] == 1
    assert body["size"] == 1
    assert body["pages"] == 2
    assert body["has_next"] is True
    assert len(body["items"]) == 1


async def test_list_centres_filters_by_city(client: AsyncClient, catalogue) -> None:
    response = await client.get("/centres/", params={"city": "bengaluru"})
    assert response.json()["total"] == 2

    empty = await client.get("/centres/", params={"city": "Mumbai"})
    assert empty.json()["total"] == 0
    assert empty.json()["items"] == []


async def test_list_centres_searches_by_name(client: AsyncClient, catalogue) -> None:
    response = await client.get("/centres/", params={"search": "airport"})
    assert response.json()["total"] == 1
    assert response.json()["items"][0]["name"] == "EVE Airport Diagnostics"


async def test_get_centre_returns_detail(client: AsyncClient, catalogue) -> None:
    response = await client.get(f"/centres/{catalogue['centre'].id}")
    assert response.status_code == 200
    body = response.json()
    assert body["city"] == "Bengaluru"
    assert body["latitude"] == "12.971599"


async def test_get_centre_unknown_id_returns_404(client: AsyncClient, catalogue) -> None:
    response = await client.get(f"/centres/{uuid.uuid4()}")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "centre_not_found"


async def test_get_centre_with_malformed_id_returns_422(client: AsyncClient) -> None:
    response = await client.get("/centres/not-a-uuid")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


async def test_centre_tests_expose_centre_specific_price(client: AsyncClient, catalogue) -> None:
    response = await client.get(f"/centres/{catalogue['centre'].id}/tests")
    assert response.status_code == 200
    items = {item["code"]: item for item in response.json()["items"]}
    assert items["CBC"]["price"] == "500.00"
    assert items["LIPID"]["price"] == "950.00"
    assert items["CBC"]["fasting_required"] is False
    assert items["LIPID"]["fasting_required"] is True


async def test_list_tests_search_by_code(client: AsyncClient, catalogue) -> None:
    response = await client.get("/tests/", params={"search": "lipid"})
    assert response.json()["total"] == 1
    assert response.json()["items"][0]["code"] == "LIPID"


async def test_create_centre_requires_admin(client: AsyncClient, auth_headers) -> None:
    payload = {"name": "New Centre", "address": "1 Test Street", "city": "Pune"}
    assert (await client.post("/centres/", json=payload, headers=auth_headers)).status_code == 403

    anonymous = await client.post("/centres/", json=payload)
    assert anonymous.status_code == 401


async def test_admin_can_create_centre(client: AsyncClient, admin_headers) -> None:
    response = await client.post(
        "/centres/",
        json={"name": "Pune Diagnostics", "address": "9 FC Road", "city": "Pune", "state": "MH"},
        headers=admin_headers,
    )
    assert response.status_code == 201, response.text
    assert response.json()["city"] == "Pune"


async def test_create_centre_rejects_invalid_coordinates(
    client: AsyncClient, admin_headers
) -> None:
    response = await client.post(
        "/centres/",
        json={"name": "Bad Coords", "address": "1 Street", "city": "Pune", "latitude": 120},
        headers=admin_headers,
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


async def test_create_test_rejects_duplicate_code(
    client: AsyncClient, admin_headers, catalogue
) -> None:
    response = await client.post(
        "/tests/",
        json={"code": "CBC", "name": "Duplicate CBC", "base_price": 100},
        headers=admin_headers,
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "test_code_exists"


async def test_upsert_centre_offerings_requires_admin(
    client: AsyncClient, auth_headers, catalogue
) -> None:
    payload = {"offerings": [{"test_id": str(catalogue["cbc"].id), "price": 600}]}
    response = await client.put(
        f"/centres/{catalogue['centre'].id}/tests", json=payload, headers=auth_headers
    )
    assert response.status_code == 403


async def test_admin_can_add_offering_to_another_centre(
    client: AsyncClient, admin_headers, catalogue
) -> None:
    response = await client.put(
        f"/centres/{catalogue['other_centre'].id}/tests",
        json={"offerings": [{"test_id": str(catalogue["cbc"].id), "price": 550}]},
        headers=admin_headers,
    )
    assert response.status_code == 200, response.text
    assert response.json()[0]["price"] == "550.00"

    listing = await client.get(f"/centres/{catalogue['other_centre'].id}/tests")
    assert listing.json()["total"] == 1


async def test_upsert_offerings_rejects_unknown_test(
    client: AsyncClient, admin_headers, catalogue
) -> None:
    response = await client.put(
        f"/centres/{catalogue['centre'].id}/tests",
        json={"offerings": [{"test_id": str(uuid.uuid4()), "price": 100}]},
        headers=admin_headers,
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_test_reference"


async def test_remove_offering(client: AsyncClient, admin_headers, catalogue) -> None:
    response = await client.delete(
        f"/centres/{catalogue['centre'].id}/tests/{catalogue['lipid'].id}", headers=admin_headers
    )
    assert response.status_code == 200
    listing = await client.get(f"/centres/{catalogue['centre'].id}/tests")
    assert listing.json()["total"] == 1


async def test_remove_missing_offering_returns_404(
    client: AsyncClient, admin_headers, catalogue
) -> None:
    response = await client.delete(
        f"/centres/{catalogue['other_centre'].id}/tests/{catalogue['cbc'].id}",
        headers=admin_headers,
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "offering_not_found"


async def test_catalogue_cache_serves_repeat_reads(
    client: AsyncClient, catalogue, session
) -> None:
    from app.services.cache import MemoryCache, set_cache

    set_cache(MemoryCache())
    try:
        first = await client.get(f"/centres/{catalogue['centre'].id}")
        catalogue["centre"].name = "Renamed Diagnostics"
        await session.commit()
        second = await client.get(f"/centres/{catalogue['centre'].id}")
        assert second.json()["name"] == first.json()["name"] == "EVE City Diagnostics"
    finally:
        from app.services.cache import NullCache

        set_cache(NullCache())


@pytest.mark.parametrize("path", ["/centres/", "/tests/"])
async def test_pagination_bounds_are_enforced(client: AsyncClient, catalogue, path: str) -> None:
    assert (await client.get(path, params={"size": 0})).status_code == 422
    assert (await client.get(path, params={"size": 500})).status_code == 422
    assert (await client.get(path, params={"page": 0})).status_code == 422


async def test_centre_update_invalidates_cached_reads(
    client: AsyncClient, admin_headers, catalogue
) -> None:
    from app.services.cache import MemoryCache, NullCache, set_cache

    set_cache(MemoryCache())
    try:
        before = await client.get(f"/centres/{catalogue['centre'].id}")
        assert before.status_code == 200

        updated = await client.patch(
            f"/centres/{catalogue['centre'].id}",
            json={"name": "EVE Renamed Diagnostics"},
            headers=admin_headers,
        )
        assert updated.status_code == 200, updated.text

        after = await client.get(f"/centres/{catalogue['centre'].id}")
        assert after.json()["name"] == "EVE Renamed Diagnostics"
    finally:
        set_cache(NullCache())


async def test_create_test_invalidates_cached_centre_offerings(
    client: AsyncClient, admin_headers, catalogue
) -> None:
    from app.services.cache import MemoryCache, NullCache, set_cache

    set_cache(MemoryCache())
    try:
        before = await client.get(f"/centres/{catalogue['centre'].id}/tests")
        assert before.status_code == 200
        baseline = before.json()["total"]

        created = await client.post(
            "/tests/",
            json={
                "code": "VITD",
                "name": "Vitamin D",
                "base_price": 900,
                "duration_minutes": 60,
            },
            headers=admin_headers,
        )
        assert created.status_code == 201, created.text

        after = await client.get(f"/centres/{catalogue['centre'].id}/tests")
        assert after.json()["total"] == baseline

        attached = await client.put(
            f"/centres/{catalogue['centre'].id}/tests",
            json={
                "offerings": [
                    {"test_id": created.json()["id"], "price": 950, "is_available": True}
                ]
            },
            headers=admin_headers,
        )
        assert attached.status_code == 200, attached.text

        refreshed = await client.get(f"/centres/{catalogue['centre'].id}/tests")
        assert refreshed.json()["total"] == baseline + 1
    finally:
        set_cache(NullCache())
