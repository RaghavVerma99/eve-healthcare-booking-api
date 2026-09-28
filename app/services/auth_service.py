import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import AuthenticationError, ConflictError
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    verify_password,
)
from app.db.types import utcnow
from app.models.user import RefreshToken, User
from app.schemas.auth import UserCreate


async def get_user_by_email(session: AsyncSession, email: str) -> User | None:
    result = await session.execute(select(User).where(func.lower(User.email) == email.lower()))
    return result.scalar_one_or_none()


async def get_user_by_id(session: AsyncSession, user_id: uuid.UUID) -> User | None:
    return await session.get(User, user_id)


async def create_user(
    session: AsyncSession, payload: UserCreate, *, is_admin: bool = False
) -> User:
    existing = await get_user_by_email(session, str(payload.email))
    if existing is not None:
        raise ConflictError(
            "A user with this email already exists.", code="email_already_registered"
        )

    user = User(
        email=str(payload.email).lower(),
        full_name=payload.full_name,
        phone=payload.phone,
        hashed_password=hash_password(payload.password),
        is_admin=is_admin,
    )
    session.add(user)
    await session.flush()
    return user


def authenticate(session_user: User | None, email: str, password: str) -> User:
    if session_user is None or not verify_password(password, session_user.hashed_password):
        raise AuthenticationError("Incorrect email or password.", code="invalid_credentials")
    if not session_user.is_active:
        raise AuthenticationError("This account is deactivated.", code="account_disabled")
    return session_user


async def issue_token_pair(
    session: AsyncSession, user: User, *, user_agent: str | None = None
) -> tuple[str, str, int]:
    access_token = create_access_token(
        str(user.id), extra_claims={"email": user.email, "is_admin": user.is_admin}
    )
    refresh_token = create_refresh_token(str(user.id))
    claims = decode_token(refresh_token, "refresh")

    session.add(
        RefreshToken(
            user_id=user.id,
            jti=claims["jti"],
            family_id=claims["jti"],
            expires_at=datetime.fromtimestamp(claims["exp"], tz=UTC),
            user_agent=(user_agent or "")[:300] or None,
        )
    )
    user.last_login_at = utcnow()
    await session.flush()
    return (
        access_token,
        refresh_token,
        settings.access_token_expire_minutes * 60,
    )


async def _revoke_token_family(session: AsyncSession, family_id: str) -> int:
    """Revoke every still-live token in a rotation chain.

    Commits before returning. The caller raises an AuthenticationError to reject
    the request, and the request-scoped session rolls back on any exception, so
    a deferred commit would silently discard the revocation and leave the
    attacker holding a working token. The security action must not depend on the
    request succeeding.
    """
    result = await session.execute(
        select(RefreshToken)
        .where(RefreshToken.family_id == family_id, RefreshToken.revoked_at.is_(None))
        .with_for_update(of=RefreshToken)
    )
    tokens = list(result.scalars())
    for token in tokens:
        token.revoked_at = utcnow()
    await session.commit()
    return len(tokens)


async def rotate_refresh_token(
    session: AsyncSession, refresh_token: str, *, user_agent: str | None = None
) -> tuple[User, str, str, int]:
    claims = decode_token(refresh_token, "refresh")
    # `of=RefreshToken` keeps FOR UPDATE off the eager-joined user row, which
    # PostgreSQL rejects ("cannot be applied to the nullable side of an outer
    # join") and which would otherwise needlessly serialise logins per user.
    stored = await session.scalar(
        select(RefreshToken)
        .where(RefreshToken.jti == claims["jti"])
        .with_for_update(of=RefreshToken)
    )
    if stored is None:
        raise AuthenticationError("Refresh token is not recognised.", code="unknown_refresh_token")
    if stored.revoked_at is not None:
        # A rotated-past token coming back means either a replayed client or a
        # stolen token racing the legitimate user; both mean the chain is no
        # longer trustworthy, so burn the whole family. A token revoked by an
        # explicit logout has no successor and is left alone, because logging
        # out and retrying is not a compromise.
        if stored.replaced_by_jti is not None:
            await _revoke_token_family(session, stored.family_id)
            raise AuthenticationError(
                "This token was already rotated. All sessions in its chain have been revoked; "
                "sign in again.",
                code="refresh_token_reuse_detected",
            )
        raise AuthenticationError(
            "Refresh token has already been used or revoked.", code="refresh_token_revoked"
        )
    if stored.expires_at <= utcnow():
        raise AuthenticationError("Refresh token has expired.", code="token_expired")

    user = await session.get(User, stored.user_id)
    if user is None or not user.is_active:
        raise AuthenticationError("Account is unavailable.", code="account_disabled")

    stored.revoked_at = utcnow()
    access_token = create_access_token(
        str(user.id), extra_claims={"email": user.email, "is_admin": user.is_admin}
    )
    new_refresh = create_refresh_token(str(user.id))
    new_claims = decode_token(new_refresh, "refresh")
    stored.replaced_by_jti = new_claims["jti"]
    session.add(
        RefreshToken(
            user_id=user.id,
            jti=new_claims["jti"],
            family_id=stored.family_id,
            expires_at=datetime.fromtimestamp(new_claims["exp"], tz=UTC),
            user_agent=(user_agent or "")[:300] or None,
        )
    )
    await session.flush()
    return user, access_token, new_refresh, settings.access_token_expire_minutes * 60


async def revoke_refresh_token(session: AsyncSession, refresh_token: str) -> None:
    try:
        claims = decode_token(refresh_token, "refresh")
    except AuthenticationError:
        return
    stored = await session.scalar(
        select(RefreshToken)
        .where(RefreshToken.jti == claims["jti"])
        .with_for_update(of=RefreshToken)
    )
    if stored is not None and stored.revoked_at is None:
        stored.revoked_at = utcnow()


async def purge_expired_refresh_tokens(session: AsyncSession) -> int:
    cutoff = utcnow() - timedelta(days=1)
    result = await session.execute(
        select(RefreshToken).where(RefreshToken.expires_at < cutoff)
    )
    tokens = list(result.scalars())
    for token in tokens:
        await session.delete(token)
    return len(tokens)
