import uuid
from datetime import datetime
from typing import ClassVar

from sqlalchemy import MetaData, Uuid, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.db.types import TZDateTime

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    def __repr__(self) -> str:
        identifier = getattr(self, "id", None) or getattr(self, "event_id", None)
        return f"<{type(self).__name__} {identifier}>"


class TimestampMixin:
    __mapper_args__: ClassVar[dict[str, bool]] = {"eager_defaults": True}

    created_at: Mapped[datetime] = mapped_column(
        TZDateTime, server_default=func.now(), nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        TZDateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )


class UUIDPrimaryKeyMixin:
    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)


__all__ = ["Base", "TimestampMixin", "UUIDPrimaryKeyMixin"]
