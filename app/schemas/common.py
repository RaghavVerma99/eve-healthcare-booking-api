from datetime import UTC, datetime
from typing import Annotated, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")

PageSize = Annotated[int, Field(ge=1, le=100)]
PageNumber = Annotated[int, Field(ge=1)]


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    page: int
    size: int
    pages: int
    has_next: bool

    @classmethod
    def build(cls, items: list[T], total: int, page: int, size: int) -> "Page[T]":
        pages = (total + size - 1) // size if size else 0
        return cls(
            items=items,
            total=total,
            page=page,
            size=size,
            pages=pages,
            has_next=page < pages,
        )


class ErrorDetail(BaseModel):
    code: str
    message: str
    details: dict | list | None = None


class ErrorResponse(BaseModel):
    error: ErrorDetail
    request_id: str | None = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))


class MessageResponse(BaseModel):
    message: str
