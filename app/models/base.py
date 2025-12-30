from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(default=utc_now)


class SoftDeleteMixin:
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    deleted_by: Mapped[int | None] = mapped_column(default=None)
