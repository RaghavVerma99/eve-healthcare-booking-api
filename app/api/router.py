from fastapi import APIRouter

from app.api.routers import auth, bookings, catalogue, payments, system

api_router = APIRouter()
api_router.include_router(system.router)
api_router.include_router(auth.router)
api_router.include_router(catalogue.router)
api_router.include_router(bookings.router)
api_router.include_router(payments.router)

__all__ = ["api_router"]
