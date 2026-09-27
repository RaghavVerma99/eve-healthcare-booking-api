import uuid
from typing import Annotated

from fastapi import APIRouter, Query, status

from app.api.deps import AdminUser, PaginationDep, SessionDep
from app.schemas.catalogue import (
    CentreCreate,
    CentreRead,
    CentreTestListUpdate,
    CentreTestRead,
    CentreUpdate,
    TestCreate,
    TestRead,
    TestUpdate,
)
from app.schemas.common import MessageResponse, Page
from app.services import catalogue_service
from app.services.cache import (
    CACHE_PREFIX_CENTRES,
    CACHE_PREFIX_TESTS,
    cache_get,
    cache_invalidate,
    cache_set,
)

router = APIRouter(tags=["catalogue"])


@router.get(
    "/centres/",
    response_model=Page[CentreRead],
    summary="List active diagnostic centres",
)
async def list_centres(
    session: SessionDep,
    pagination: PaginationDep,
    city: Annotated[str | None, Query(max_length=80)] = None,
    search: Annotated[str | None, Query(max_length=120)] = None,
) -> Page[CentreRead]:
    cache_key = (
        f"{CACHE_PREFIX_CENTRES}:list:{city or 'all'}:{search or 'all'}"
        f":{pagination.page}:{pagination.size}"
    )
    cached = await cache_get(cache_key)
    if cached is not None:
        return Page[CentreRead].model_validate(cached)

    centres, total = await catalogue_service.list_centres(
        session, city=city, search=search, page=pagination.page, size=pagination.size
    )
    page = Page.build(
        [CentreRead.model_validate(centre) for centre in centres],
        total,
        pagination.page,
        pagination.size,
    )
    await cache_set(cache_key, page.model_dump(mode="json"))
    return page


@router.post(
    "/centres/",
    response_model=CentreRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a diagnostic centre (admin)",
)
async def create_centre(payload: CentreCreate, session: SessionDep, admin: AdminUser) -> CentreRead:
    centre = await catalogue_service.create_centre(session, payload)
    await session.commit()
    await cache_invalidate(CACHE_PREFIX_CENTRES)
    await cache_invalidate(CACHE_PREFIX_TESTS)
    return CentreRead.model_validate(centre)


@router.get("/centres/{centre_id}", response_model=CentreRead, summary="Fetch one centre")
async def get_centre(centre_id: uuid.UUID, session: SessionDep) -> CentreRead:
    cache_key = f"{CACHE_PREFIX_CENTRES}:{centre_id}"
    cached = await cache_get(cache_key)
    if cached is not None:
        return CentreRead.model_validate(cached)
    centre = await catalogue_service.get_centre(session, centre_id)
    schema = CentreRead.model_validate(centre)
    await cache_set(cache_key, schema.model_dump(mode="json"))
    return schema


@router.patch("/centres/{centre_id}", response_model=CentreRead, summary="Update a centre (admin)")
async def update_centre(
    centre_id: uuid.UUID, payload: CentreUpdate, session: SessionDep, admin: AdminUser
) -> CentreRead:
    centre = await catalogue_service.update_centre(session, centre_id, payload)
    await session.commit()
    await cache_invalidate(CACHE_PREFIX_CENTRES)
    await cache_invalidate(CACHE_PREFIX_TESTS)
    return CentreRead.model_validate(centre)


@router.get(
    "/centres/{centre_id}/tests",
    response_model=Page[CentreTestRead],
    summary="List the tests offered at a centre with centre-specific pricing",
)
async def list_centre_tests(
    centre_id: uuid.UUID,
    session: SessionDep,
    pagination: PaginationDep,
    available_only: Annotated[bool, Query(description="Hide tests marked unavailable")] = True,
) -> Page[CentreTestRead]:
    offerings, total = await catalogue_service.list_centre_tests(
        session,
        centre_id,
        available_only=available_only,
        page=pagination.page,
        size=pagination.size,
    )
    items = [catalogue_service.offering_to_schema(offering, test) for offering, test in offerings]
    return Page.build(items, total, pagination.page, pagination.size)


@router.put(
    "/centres/{centre_id}/tests",
    response_model=list[CentreTestRead],
    summary="Create or update the tests a centre offers (admin)",
)
async def upsert_centre_tests(
    centre_id: uuid.UUID,
    payload: CentreTestListUpdate,
    session: SessionDep,
    admin: AdminUser,
) -> list[CentreTestRead]:
    await catalogue_service.replace_centre_offerings(session, centre_id, payload)
    await session.commit()
    await cache_invalidate(CACHE_PREFIX_CENTRES)
    await cache_invalidate(CACHE_PREFIX_TESTS)
    offerings, _ = await catalogue_service.list_centre_tests(
        session, centre_id, available_only=False, page=1, size=200
    )
    return [catalogue_service.offering_to_schema(offering, test) for offering, test in offerings]


@router.delete(
    "/centres/{centre_id}/tests/{test_id}",
    response_model=MessageResponse,
    summary="Stop offering a test at a centre (admin)",
)
async def remove_centre_test(
    centre_id: uuid.UUID, test_id: uuid.UUID, session: SessionDep, admin: AdminUser
) -> MessageResponse:
    await catalogue_service.remove_offering(session, centre_id, test_id)
    await session.commit()
    await cache_invalidate(CACHE_PREFIX_CENTRES)
    await cache_invalidate(CACHE_PREFIX_TESTS)
    return MessageResponse(message="Test removed from the centre.")


@router.get(
    "/tests/", response_model=Page[TestRead], summary="Search the diagnostic test catalogue"
)
async def list_tests(
    session: SessionDep,
    pagination: PaginationDep,
    search: Annotated[str | None, Query(max_length=120)] = None,
) -> Page[TestRead]:
    cache_key = f"{CACHE_PREFIX_TESTS}:list:{search or 'all'}:{pagination.page}:{pagination.size}"
    cached = await cache_get(cache_key)
    if cached is not None:
        return Page[TestRead].model_validate(cached)

    tests, total = await catalogue_service.list_tests(
        session, search=search, page=pagination.page, size=pagination.size
    )
    page = Page.build(
        [TestRead.model_validate(test) for test in tests],
        total,
        pagination.page,
        pagination.size,
    )
    await cache_set(cache_key, page.model_dump(mode="json"))
    return page


@router.get("/tests/{test_id}", response_model=TestRead, summary="Fetch one diagnostic test")
async def get_test(test_id: uuid.UUID, session: SessionDep) -> TestRead:
    cache_key = f"{CACHE_PREFIX_TESTS}:{test_id}"
    cached = await cache_get(cache_key)
    if cached is not None:
        return TestRead.model_validate(cached)
    test = await catalogue_service.get_test(session, test_id)
    schema = TestRead.model_validate(test)
    await cache_set(cache_key, schema.model_dump(mode="json"))
    return schema


@router.post(
    "/tests/",
    response_model=TestRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a diagnostic test (admin)",
)
async def create_test(payload: TestCreate, session: SessionDep, admin: AdminUser) -> TestRead:
    test = await catalogue_service.create_test(session, payload)
    await session.commit()
    await cache_invalidate(CACHE_PREFIX_CENTRES)
    await cache_invalidate(CACHE_PREFIX_TESTS)
    return TestRead.model_validate(test)


@router.patch(
    "/tests/{test_id}", response_model=TestRead, summary="Update a diagnostic test (admin)"
)
async def update_test(
    test_id: uuid.UUID, payload: TestUpdate, session: SessionDep, admin: AdminUser
) -> TestRead:
    test = await catalogue_service.update_test(session, test_id, payload)
    await session.commit()
    await cache_invalidate(CACHE_PREFIX_CENTRES)
    await cache_invalidate(CACHE_PREFIX_TESTS)
    return TestRead.model_validate(test)
