import hashlib
import hmac
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import jwt
from passlib.context import CryptContext

from app.core.config import settings
from app.core.exceptions import AuthenticationError

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto", bcrypt__rounds=12)

TokenType = Literal["access", "refresh"]


def hash_password(raw_password: str) -> str:
    return pwd_context.hash(raw_password)


def verify_password(raw_password: str, hashed_password: str) -> bool:
    try:
        return pwd_context.verify(raw_password, hashed_password)
    except ValueError:
        return False


def _create_token(
    subject: str,
    token_type: TokenType,
    expires_delta: timedelta,
    extra_claims: dict[str, Any] | None = None,
) -> str:
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "sub": subject,
        "type": token_type,
        "iat": int(now.timestamp()),
        "nbf": int(now.timestamp()),
        "exp": int((now + expires_delta).timestamp()),
        "jti": uuid.uuid4().hex,
        "iss": "eve-diagnostics",
    }
    if extra_claims:
        payload.update(extra_claims)
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def create_access_token(subject: str, extra_claims: dict[str, Any] | None = None) -> str:
    return _create_token(
        subject,
        "access",
        timedelta(minutes=settings.access_token_expire_minutes),
        extra_claims,
    )


def create_refresh_token(subject: str) -> str:
    return _create_token(
        subject,
        "refresh",
        timedelta(days=settings.refresh_token_expire_days),
    )


def decode_token(token: str, expected_type: TokenType) -> dict[str, Any]:
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret_key,
            algorithms=[settings.jwt_algorithm],
            issuer="eve-diagnostics",
            options={"require": ["exp", "sub", "type"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise AuthenticationError("Token has expired.", code="token_expired") from exc
    except jwt.InvalidTokenError as exc:
        raise AuthenticationError("Invalid authentication token.", code="invalid_token") from exc

    if payload.get("type") != expected_type:
        raise AuthenticationError(
            f"Expected a {expected_type} token.", code="wrong_token_type"
        )
    return payload


def sign_webhook_payload(raw_body: bytes) -> str:
    return hmac.new(
        settings.webhook_signing_secret.encode("utf-8"),
        raw_body,
        hashlib.sha256,
    ).hexdigest()


def verify_webhook_signature(raw_body: bytes, signature: str | None) -> bool:
    if not signature:
        return False
    expected = sign_webhook_payload(raw_body)
    return hmac.compare_digest(expected, signature.strip().lower())
