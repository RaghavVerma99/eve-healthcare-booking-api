from typing import Annotated

from fastapi import APIRouter, Depends, Request, status
from fastapi.security import OAuth2PasswordRequestForm

from app.api.deps import CurrentUser, SessionDep
from app.core.config import settings
from app.core.logging_config import get_logger
from app.schemas.auth import (
    AuthResponse,
    RefreshRequest,
    TokenPair,
    UserCreate,
    UserLogin,
    UserRead,
)
from app.schemas.common import MessageResponse
from app.services import auth_service

logger = get_logger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])


def _auth_response(user, access_token: str, refresh_token: str, expires_in: int) -> AuthResponse:
    return AuthResponse(
        user=UserRead.model_validate(user),
        tokens=TokenPair(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_in=expires_in,
        ),
    )


@router.post(
    "/signup",
    response_model=AuthResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register a new user and return an initial token pair",
)
async def signup(
    payload: UserCreate, session: SessionDep, request: Request
) -> AuthResponse:
    user = await auth_service.create_user(session, payload)
    access_token, refresh_token, expires_in = await auth_service.issue_token_pair(
        session, user, user_agent=request.headers.get("user-agent")
    )
    await session.commit()
    logger.info("user_signed_up", extra={"user_id": str(user.id), "email": user.email})
    return _auth_response(user, access_token, refresh_token, expires_in)


@router.post("/login", response_model=AuthResponse, summary="Exchange credentials for a token pair")
async def login(payload: UserLogin, session: SessionDep, request: Request) -> AuthResponse:
    candidate = await auth_service.get_user_by_email(session, str(payload.email))
    user = auth_service.authenticate(candidate, str(payload.email), payload.password)
    access_token, refresh_token, expires_in = await auth_service.issue_token_pair(
        session, user, user_agent=request.headers.get("user-agent")
    )
    await session.commit()
    logger.info("user_logged_in", extra={"user_id": str(user.id)})
    return _auth_response(user, access_token, refresh_token, expires_in)


@router.post(
    "/token",
    response_model=TokenPair,
    summary="OAuth2 password flow (form encoded) for tooling compatibility",
)
async def login_form(
    form: Annotated[OAuth2PasswordRequestForm, Depends()],
    session: SessionDep,
    request: Request,
) -> TokenPair:
    candidate = await auth_service.get_user_by_email(session, form.username)
    user = auth_service.authenticate(candidate, form.username, form.password)
    access_token, refresh_token, expires_in = await auth_service.issue_token_pair(
        session, user, user_agent=request.headers.get("user-agent")
    )
    await session.commit()
    return TokenPair(
        access_token=access_token, refresh_token=refresh_token, expires_in=expires_in
    )


@router.post("/refresh", response_model=TokenPair, summary="Rotate a refresh token")
async def refresh(payload: RefreshRequest, session: SessionDep, request: Request) -> TokenPair:
    _, access_token, refresh_token, expires_in = await auth_service.rotate_refresh_token(
        session, payload.refresh_token, user_agent=request.headers.get("user-agent")
    )
    await session.commit()
    return TokenPair(
        access_token=access_token, refresh_token=refresh_token, expires_in=expires_in
    )


@router.post("/logout", response_model=MessageResponse, summary="Revoke a refresh token")
async def logout(payload: RefreshRequest, session: SessionDep) -> MessageResponse:
    await auth_service.revoke_refresh_token(session, payload.refresh_token)
    await session.commit()
    return MessageResponse(message="Refresh token revoked.")


@router.get("/me", response_model=UserRead, summary="Return the authenticated user")
async def me(user: CurrentUser) -> UserRead:
    return UserRead.model_validate(user)


@router.get("/config", response_model=dict, summary="Expose non-sensitive runtime settings")
async def public_config() -> dict:
    return {
        "access_token_expire_minutes": settings.access_token_expire_minutes,
        "refresh_token_expire_days": settings.refresh_token_expire_days,
        "webhook_signature_required": settings.webhook_require_signature,
        "environments": ["development", "test", "staging", "production"],
    }
