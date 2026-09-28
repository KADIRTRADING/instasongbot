"""SQLAlchemy ORM models. See ARCHITECTURE.md §5 for the schema rationale."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, BigInteger, Boolean, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, new_uuid, utcnow_column

# Cross-dialect JSON: real JSONB on Postgres (production), plain JSON text on
# SQLite (tests / aiosqlite) since SQLite has no native JSONB type. Using this
# variant type means the exact same models.py works, unmodified, against both.
JSONVariant = JSON().with_variant(JSONB, "postgresql")


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)  # Telegram user id
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    first_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    language_code: Mapped[str] = mapped_column(String(8), nullable=False, default="uz")
    is_banned: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_admin_cached: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = utcnow_column()
    last_seen_at: Mapped[datetime] = utcnow_column(onupdate=True)

    jobs: Mapped[list["Job"]] = relationship(back_populates="user", cascade="all, delete-orphan")

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<User id={self.id} lang={self.language_code}>"


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    job_type: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    platform: Mapped[str | None] = mapped_column(String(32), nullable=True)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_meta: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant, nullable=True)
    created_at: Mapped[datetime] = utcnow_column()
    updated_at: Mapped[datetime] = utcnow_column(onupdate=True)

    user: Mapped[User] = relationship(back_populates="jobs")

    __table_args__ = (
        Index("ix_jobs_user_id", "user_id"),
        Index("ix_jobs_type_status", "job_type", "status"),
        Index("ix_jobs_created_at", "created_at"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Job id={self.id} type={self.job_type} status={self.status}>"


class CaptionTemplate(Base):
    """Admin-editable caption template, one row per media type."""

    __tablename__ = "caption_templates"

    media_type: Mapped[str] = mapped_column(String(16), primary_key=True)  # video/audio/image
    template: Mapped[str] = mapped_column(Text, nullable=False)
    updated_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    updated_at: Mapped[datetime] = utcnow_column(onupdate=True)


class CaptionButton(Base):
    """Admin-managed inline buttons attached under delivered media captions."""

    __tablename__ = "caption_buttons"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # NULL media_type => button appears under every media type's caption.
    media_type: Mapped[str | None] = mapped_column(String(16), nullable=True)
    label: Mapped[str] = mapped_column(String(64), nullable=False)
    url: Mapped[str] = mapped_column(String(512), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    __table_args__ = (Index("ix_caption_buttons_media_type", "media_type"),)


class PlatformSetting(Base):
    """Per-platform enable/disable toggle and optional file-size override."""

    __tablename__ = "platform_settings"

    platform: Mapped[str] = mapped_column(String(32), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    max_file_size_mb: Mapped[int | None] = mapped_column(Integer, nullable=True)
    updated_at: Mapped[datetime] = utcnow_column(onupdate=True)


class BotSetting(Base):
    """Generic runtime-tunable key/value store (rate limits, global file cap, etc.)."""

    __tablename__ = "bot_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[Any] = mapped_column(JSONVariant, nullable=False)
    updated_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    updated_at: Mapped[datetime] = utcnow_column(onupdate=True)


class Broadcast(Base):
    """Admin broadcast/announcement job, tracked so progress is observable."""

    __tablename__ = "broadcasts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    admin_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    message_text: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    total_users: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sent_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = utcnow_column()
    completed_at: Mapped[datetime | None] = mapped_column(nullable=True)
