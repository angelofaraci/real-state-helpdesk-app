"""Shared SQLAlchemy declarative base and common mixins."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Declarative base shared by every ORM model."""


class UUIDPrimaryKeyMixin:
    """Mixin providing a UUID primary key with an app-side default.

    IDs are generated in Python via `uuid.uuid4` (NOT Postgres'
    `gen_random_uuid()`), so a new entity's ID is known before it is
    flushed to the database.
    """

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )


class TimestampMixin:
    """Mixin providing a `created_at` column, defaulted server-side to now()."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
