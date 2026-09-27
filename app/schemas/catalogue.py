import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field, field_validator

from app.schemas.common import ORMModel


class CentreTestRead(ORMModel):
    test_id: uuid.UUID
    code: str
    name: str
    description: str | None = None
    duration_minutes: int
    fasting_required: bool
    price: Decimal
    is_available: bool


class CentreRead(ORMModel):
    id: uuid.UUID
    name: str
    address: str
    city: str
    state: str | None = None
    postal_code: str | None = None
    phone: str | None = None
    latitude: Decimal | None = None
    longitude: Decimal | None = None
    is_active: bool
    tests_count: int | None = None
    created_at: datetime


class CentreCreate(BaseModel):
    name: str = Field(min_length=2, max_length=150)
    address: str = Field(min_length=5, max_length=500)
    city: str = Field(min_length=2, max_length=80)
    state: str | None = Field(default=None, max_length=80)
    postal_code: str | None = Field(default=None, max_length=20)
    phone: str | None = Field(default=None, max_length=20)
    latitude: Decimal | None = Field(default=None, ge=-90, le=90)
    longitude: Decimal | None = Field(default=None, ge=-180, le=180)


class CentreUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=150)
    address: str | None = Field(default=None, min_length=5, max_length=500)
    city: str | None = Field(default=None, min_length=2, max_length=80)
    state: str | None = Field(default=None, max_length=80)
    postal_code: str | None = Field(default=None, max_length=20)
    phone: str | None = Field(default=None, max_length=20)
    latitude: Decimal | None = Field(default=None, ge=-90, le=90)
    longitude: Decimal | None = Field(default=None, ge=-180, le=180)
    is_active: bool | None = None


class TestRead(ORMModel):
    id: uuid.UUID
    code: str
    name: str
    description: str | None = None
    duration_minutes: int
    base_price: Decimal
    fasting_required: bool
    is_active: bool
    created_at: datetime


class TestCreate(BaseModel):
    code: str = Field(min_length=2, max_length=40)
    name: str = Field(min_length=2, max_length=150)
    description: str | None = Field(default=None, max_length=2000)
    duration_minutes: int = Field(default=30, gt=0, le=1440)
    base_price: Decimal = Field(ge=0, decimal_places=2, max_digits=10)
    fasting_required: bool = False

    @field_validator("code")
    @classmethod
    def _upper_code(cls, value: str) -> str:
        return value.strip().upper()


class TestUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=150)
    description: str | None = Field(default=None, max_length=2000)
    duration_minutes: int | None = Field(default=None, gt=0, le=1440)
    base_price: Decimal | None = Field(default=None, ge=0, decimal_places=2, max_digits=10)
    fasting_required: bool | None = None
    is_active: bool | None = None


class CentreTestUpsert(BaseModel):
    test_id: uuid.UUID
    price: Decimal = Field(ge=0, decimal_places=2, max_digits=10)
    is_available: bool = True


class CentreTestListUpdate(BaseModel):
    offerings: list[CentreTestUpsert] = Field(min_length=1, max_length=200)
