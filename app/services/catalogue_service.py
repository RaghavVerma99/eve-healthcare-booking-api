import uuid
from decimal import Decimal

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError, NotFoundError, UnprocessableError
from app.models.catalogue import CentreTest, DiagnosticCentre, DiagnosticTest
from app.schemas.catalogue import (
    CentreCreate,
    CentreTestListUpdate,
    CentreTestRead,
    CentreUpdate,
    TestCreate,
    TestUpdate,
)


async def list_centres(
    session: AsyncSession,
    *,
    city: str | None,
    search: str | None,
    page: int,
    size: int,
) -> tuple[list[DiagnosticCentre], int]:
    filters = [DiagnosticCentre.is_active.is_(True)]
    if city:
        filters.append(func.lower(DiagnosticCentre.city) == city.lower())
    if search:
        pattern = f"%{search.lower()}%"
        filters.append(
            or_(
                func.lower(DiagnosticCentre.name).like(pattern),
                func.lower(DiagnosticCentre.address).like(pattern),
            )
        )

    total = await session.scalar(
        select(func.count()).select_from(DiagnosticCentre).where(*filters)
    )
    result = await session.execute(
        select(DiagnosticCentre)
        .where(*filters)
        .order_by(DiagnosticCentre.name)
        .offset((page - 1) * size)
        .limit(size)
    )
    return list(result.scalars().unique()), int(total or 0)


async def get_centre(
    session: AsyncSession, centre_id: uuid.UUID, *, include_inactive: bool = False
) -> DiagnosticCentre:
    centre = await session.get(DiagnosticCentre, centre_id)
    if centre is None or (not include_inactive and not centre.is_active):
        raise NotFoundError("Diagnostic centre not found.", code="centre_not_found")
    return centre


async def create_centre(session: AsyncSession, payload: CentreCreate) -> DiagnosticCentre:
    centre = DiagnosticCentre(**payload.model_dump())
    session.add(centre)
    await session.flush()
    return centre


async def update_centre(
    session: AsyncSession, centre_id: uuid.UUID, payload: CentreUpdate
) -> DiagnosticCentre:
    centre = await get_centre(session, centre_id, include_inactive=True)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(centre, field, value)
    await session.flush()
    return centre


async def list_tests(
    session: AsyncSession, *, search: str | None, page: int, size: int
) -> tuple[list[DiagnosticTest], int]:
    filters = [DiagnosticTest.is_active.is_(True)]
    if search:
        pattern = f"%{search.lower()}%"
        filters.append(
            or_(
                func.lower(DiagnosticTest.name).like(pattern),
                func.lower(DiagnosticTest.code).like(pattern),
            )
        )
    total = await session.scalar(select(func.count()).select_from(DiagnosticTest).where(*filters))
    result = await session.execute(
        select(DiagnosticTest)
        .where(*filters)
        .order_by(DiagnosticTest.name)
        .offset((page - 1) * size)
        .limit(size)
    )
    return list(result.scalars().unique()), int(total or 0)


async def get_test(session: AsyncSession, test_id: uuid.UUID) -> DiagnosticTest:
    test = await session.get(DiagnosticTest, test_id)
    if test is None or not test.is_active:
        raise NotFoundError("Diagnostic test not found.", code="test_not_found")
    return test


async def create_test(session: AsyncSession, payload: TestCreate) -> DiagnosticTest:
    existing = await session.scalar(
        select(DiagnosticTest).where(DiagnosticTest.code == payload.code)
    )
    if existing is not None:
        raise ConflictError(
            f"Test code '{payload.code}' already exists.", code="test_code_exists"
        )
    test = DiagnosticTest(**payload.model_dump())
    session.add(test)
    await session.flush()
    return test


async def update_test(
    session: AsyncSession, test_id: uuid.UUID, payload: TestUpdate
) -> DiagnosticTest:
    test = await get_test(session, test_id)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(test, field, value)
    await session.flush()
    return test


async def list_centre_tests(
    session: AsyncSession,
    centre_id: uuid.UUID,
    *,
    available_only: bool = True,
    page: int = 1,
    size: int = 50,
) -> tuple[list[tuple[CentreTest, DiagnosticTest]], int]:
    await get_centre(session, centre_id)
    filters = [CentreTest.centre_id == centre_id]
    if available_only:
        filters.append(CentreTest.is_available.is_(True))
        filters.append(DiagnosticTest.is_active.is_(True))

    total = await session.scalar(
        select(func.count())
        .select_from(CentreTest)
        .join(DiagnosticTest, DiagnosticTest.id == CentreTest.test_id)
        .where(*filters)
    )
    result = await session.execute(
        select(CentreTest, DiagnosticTest)
        .join(DiagnosticTest, DiagnosticTest.id == CentreTest.test_id)
        .where(*filters)
        .order_by(DiagnosticTest.name)
        .offset((page - 1) * size)
        .limit(size)
    )
    return [(row[0], row[1]) for row in result.all()], int(total or 0)


async def replace_centre_offerings(
    session: AsyncSession, centre_id: uuid.UUID, payload: CentreTestListUpdate
) -> list[CentreTest]:
    await get_centre(session, centre_id, include_inactive=True)
    requested_ids = {item.test_id for item in payload.offerings}
    existing = {
        offering.test_id: offering
        for offering in (await session.execute(
            select(CentreTest).where(CentreTest.centre_id == centre_id)
        )).scalars()
    }

    for test_id in requested_ids:
        try:
            await get_test(session, uuid.UUID(str(test_id)))
        except (ValueError, NotFoundError) as exc:
            raise UnprocessableError(
                f"Test '{test_id}' does not exist or is inactive.", code="invalid_test_reference"
            ) from exc

    for offering in payload.offerings:
        record = existing.get(uuid.UUID(str(offering.test_id)))
        price = Decimal(str(offering.price))
        if record is None:
            session.add(
                CentreTest(
                    centre_id=centre_id,
                    test_id=uuid.UUID(str(offering.test_id)),
                    price=price,
                    is_available=offering.is_available,
                )
            )
        else:
            record.price = price
            record.is_available = offering.is_available

    await session.flush()
    return list(
        (
            await session.execute(
                select(CentreTest).where(CentreTest.centre_id == centre_id)
            )
        ).scalars()
    )


async def remove_offering(
    session: AsyncSession, centre_id: uuid.UUID, test_id: uuid.UUID
) -> None:
    await get_centre(session, centre_id, include_inactive=True)
    await get_test(session, test_id)
    offering = await session.get(CentreTest, {"centre_id": centre_id, "test_id": test_id})
    if offering is None:
        raise NotFoundError(
            "This test is not offered at the selected centre.",
            code="offering_not_found",
        )
    await session.delete(offering)
    await session.flush()


def offering_to_schema(offering: CentreTest, test: DiagnosticTest) -> CentreTestRead:
    return CentreTestRead(
        test_id=str(test.id),
        code=test.code,
        name=test.name,
        description=test.description,
        duration_minutes=test.duration_minutes,
        fasting_required=test.fasting_required,
        price=offering.price,
        is_available=offering.is_available,
    )

