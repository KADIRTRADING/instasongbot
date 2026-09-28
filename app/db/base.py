"""SQLAlchemy declarative base and shared column helpers."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def utcnow_column(*, onupdate: bool = False) -> Mapped[datetime]:
    """A timezone-aware timestamp column defaulting to DB-side now()."""
    if onupdate:
        return mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


def new_uuid() -> str:
    return str(uuid.uuid4())
