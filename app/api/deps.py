import uuid
from typing import Annotated

from fastapi import Depends, Query
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AuthenticationError, PermissionDeniedError
from app.core.security import decode_token
from app.db.session import get_session
from app.models.user import User
from app.services import auth_service

bearer_scheme = HTTPBearer(auto_error=False, description="JWT access token")

SessionDep = Annotated[AsyncSession, Depends(get_session)]


async def get_current_user(
    session: SessionDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> User:
    if credentials is None or not credentials.credentials:
        raise AuthenticationError(
            "Authorization header with a bearer token is required.",
            code="missing_credentials",
        )
    claims = decode_token(credentials.credentials, "access")
    try:
        user_id = uuid.UUID(claims["sub"])
    except (KeyError, ValueError) as exc:
        raise AuthenticationError("Malformed token subject.", code="invalid_token") from exc

    user = await auth_service.get_user_by_id(session, user_id)
    if user is None:
        raise AuthenticationError("User no longer exists.", code="user_not_found")
    if not user.is_active:
        raise AuthenticationError("This account is deactivated.", code="account_disabled")
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


async def get_current_admin(user: CurrentUser) -> User:
    if not user.is_admin:
        raise PermissionDeniedError(
            "This action requires an administrator account.", code="admin_required"
        )
    return user


AdminUser = Annotated[User, Depends(get_current_admin)]


class Pagination:
    def __init__(
        self,
        page: Annotated[int, Query(ge=1, description="1-based page number")] = 1,
        size: Annotated[int, Query(ge=1, le=100, description="Items per page")] = 20,
    ) -> None:
        self.page = page
        self.size = size

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.size


PaginationDep = Annotated[Pagination, Depends(Pagination)]
