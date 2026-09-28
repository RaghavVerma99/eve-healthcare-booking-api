import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query, Response, status

from app.api.deps import CurrentUser, PaginationDep, SessionDep
from app.models.enums import BookingStatus
from app.schemas.booking import BookingCreate, BookingDetail, BookingRead
from app.schemas.common import Page
from app.schemas.payment import PaymentRead
from app.services import booking_service, payment_service

router = APIRouter(prefix="/bookings", tags=["bookings"])


@router.post(
    "/",
    response_model=BookingDetail,
    status_code=status.HTTP_201_CREATED,
    summary="Book a diagnostic test at a centre",
)
async def create_booking(
    payload: BookingCreate, session: SessionDep, user: CurrentUser, response: Response
) -> BookingDetail:
    booking, replayed = await booking_service.create_booking(session, user, payload)
    await session.commit()
    await session.refresh(booking)
    if replayed:
        response.status_code = status.HTTP_200_OK
    return BookingDetail.model_validate(booking)


@router.get("/", response_model=Page[BookingRead], summary="List bookings for the current user")
async def list_bookings(
    session: SessionDep,
    user: CurrentUser,
    pagination: PaginationDep,
    booking_status: Annotated[BookingStatus | None, Query(alias="status")] = None,
    centre_id: uuid.UUID | None = None,
    from_date: datetime | None = None,
    to_date: datetime | None = None,
) -> Page[BookingRead]:
    bookings, total = await booking_service.list_bookings(
        session,
        user,
        status=booking_status,
        centre_id=centre_id,
        from_date=from_date,
        to_date=to_date,
        page=pagination.page,
        size=pagination.size,
    )
    return Page.build(
        [BookingRead.model_validate(booking) for booking in bookings],
        total,
        pagination.page,
        pagination.size,
    )


@router.get("/{booking_id}", response_model=BookingDetail, summary="Fetch one booking")
async def get_booking(
    booking_id: uuid.UUID, session: SessionDep, user: CurrentUser
) -> BookingDetail:
    booking = await booking_service.get_booking(session, booking_id, user=user)
    return BookingDetail.model_validate(booking)


@router.post(
    "/{booking_id}/cancel",
    response_model=BookingDetail,
    summary="Cancel a booking owned by the current user",
)
async def cancel_booking(
    booking_id: uuid.UUID,
    session: SessionDep,
    user: CurrentUser,
    reason: Annotated[str | None, Query(max_length=255)] = None,
) -> BookingDetail:
    booking = await booking_service.get_booking(session, booking_id, user=user, for_update=True)
    await booking_service.cancel_booking(session, booking, reason=reason)
    await session.commit()
    return BookingDetail.model_validate(booking)


@router.get(
    "/{booking_id}/payments",
    response_model=list[PaymentRead],
    summary="List payment attempts for a booking",
)
async def list_booking_payments(
    booking_id: uuid.UUID, session: SessionDep, user: CurrentUser
) -> list[PaymentRead]:
    booking = await booking_service.get_booking(session, booking_id, user=user)
    payments = await payment_service.list_payments_for_booking(session, booking)
    return [PaymentRead.model_validate(payment) for payment in payments]
