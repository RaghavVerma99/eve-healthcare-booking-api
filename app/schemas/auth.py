import uuid
from datetime import datetime

from pydantic import BaseModel, EmailStr, Field, field_validator

from app.schemas.common import ORMModel


class UserCreate(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    full_name: str = Field(min_length=2, max_length=150)
    phone: str | None = Field(default=None, max_length=20)

    @field_validator("password")
    @classmethod
    def _password_strength(cls, value: str) -> str:
        if not any(char.isalpha() for char in value):
            raise ValueError("password must contain at least one letter")
        if not any(char.isdigit() for char in value):
            raise ValueError("password must contain at least one digit")
        return value

    @field_validator("full_name")
    @classmethod
    def _strip_name(cls, value: str) -> str:
        cleaned = " ".join(value.split())
        if not cleaned:
            raise ValueError("full_name must not be blank")
        return cleaned


class UserLogin(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class UserRead(ORMModel):
    id: uuid.UUID
    email: EmailStr
    full_name: str
    phone: str | None = None
    is_active: bool
    is_admin: bool
    created_at: datetime


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int


class AuthResponse(BaseModel):
    user: UserRead
    tokens: TokenPair


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=10)
