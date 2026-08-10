"""Household and per-user settings (SPEC §4.9).

Kept "cleanly separated" as the spec asks: two tables, not one table with a
nullable `user_id`. A nullable discriminator invites a query that forgets to
filter on it and quietly returns another member's preferences.

Values are `JSONB` so a setting can be a boolean, a string, or a small object
without a migration per setting. The *meaning* of each key, its default, and the
permission needed to change it live in `registry.py` — a value in this table
with no registry entry is not a setting, it is junk, and is rejected on write.
"""

from __future__ import annotations

import datetime as dt
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from home_ops.db import Base


class HouseholdSetting(Base):
    """One shared setting for the whole household."""

    __tablename__ = "household_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict[str, object] | list[object] | str | int | float | bool | None] = (
        mapped_column(JSONB, nullable=False)
    )

    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    # SET NULL rather than CASCADE: removing a member must not delete the
    # household's configuration.
    updated_by_user_id: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class UserSetting(Base):
    """One preference belonging to one member.

    Always writable by its owner regardless of role — a read-only member still
    picks their own theme.
    """

    __tablename__ = "user_settings"

    user_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict[str, object] | list[object] | str | int | float | bool | None] = (
        mapped_column(JSONB, nullable=False)
    )

    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
