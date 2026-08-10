"""Calendars, events, exclusions and assignments (SPEC §4.3).

The recurrence model follows RFC 5545 rather than inventing one, because
CalDAV sync in the next sub-phase has to speak it anyway and a private model
would only need translating later:

* A **master** event carries the RRULE.
* A **detached instance** overrides one occurrence. It points at its master via
  `series_id` and identifies which occurrence via `recurrence_id` — the
  RECURRENCE-ID, the occurrence's originally-scheduled start.
* An **exclusion** row deletes one occurrence. The EXDATE.

`tzid` is an IANA zone name, never an offset. An offset cannot express daylight
saving, so "every Monday at 09:00" stored as +01:00 silently becomes 08:00 for
half the year — see expansion.py, where this is load-bearing.
"""

from __future__ import annotations

import datetime as dt
from enum import StrEnum
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    and_,
    func,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql.elements import ColumnElement

from home_ops.db import Base
from home_ops.policy import Principal
from home_ops.scoping import OwnedVisibleMixin


class EditScope(StrEnum):
    """What an edit or deletion applies to (SPEC §4.3)."""

    THIS = "this"
    THIS_AND_FOLLOWING = "this_and_following"
    ALL = "all"


class Calendar(Base):
    """A named calendar. Household-wide, like task categories."""

    __tablename__ = "calendars"

    id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    #: A palette key, never a hex — same rule as member hues.
    color_key: Mapped[str | None] = mapped_column(String(32), nullable=True)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    position: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index("uq_calendars_name", func.lower(name), unique=True),
        CheckConstraint("length(trim(name)) > 0", name="name_not_blank"),
        # Exactly one default, enforced rather than assumed: two defaults means
        # new events land somewhere unpredictable.
        Index(
            "uq_calendars_single_default",
            "is_default",
            unique=True,
            postgresql_where=text("is_default"),
        ),
    )


class EventAssignment(Base):
    """Who an event is assigned to. SPEC §4.3 shows assignees as avatars."""

    __tablename__ = "calendar_event_assignments"

    event_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("calendar_events.id", ondelete="CASCADE"),
        primary_key=True,
    )
    user_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )

    __table_args__ = (Index("ix_calendar_event_assignments_user_id", "user_id"),)


class EventException(Base):
    """One deleted occurrence of a series. The EXDATE.

    A row rather than a serialised list on the event, so an exclusion can be
    added or removed with one statement and indexed for expansion.
    """

    __tablename__ = "calendar_event_exceptions"

    id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    event_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("calendar_events.id", ondelete="CASCADE"),
        nullable=False,
    )
    #: The occurrence's originally-scheduled start, in UTC.
    occurrence_start: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("event_id", "occurrence_start", name="uq_event_exception_occurrence"),
    )


class CalendarEvent(OwnedVisibleMixin, Base):
    """An event. Carries owner_id, visibility and created_at from the mixin."""

    __tablename__ = "calendar_events"

    id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    calendar_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("calendars.id", ondelete="CASCADE"), nullable=False
    )

    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    location: Mapped[str | None] = mapped_column(String(255), nullable=True)

    starts_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    is_all_day: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    #: IANA zone name. Never an offset — see the module docstring.
    tzid: Mapped[str] = mapped_column(String(64), nullable=False, server_default=text("'UTC'"))

    recurrence_rule: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: Set on a detached instance: the master it overrides.
    series_id: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("calendar_events.id", ondelete="CASCADE"),
        nullable=True,
    )
    #: Set on a detached instance: which occurrence it replaces (RECURRENCE-ID),
    #: as that occurrence's originally-scheduled start.
    recurrence_id: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    assignments: Mapped[list[EventAssignment]] = relationship(
        cascade="all, delete-orphan", passive_deletes=True
    )
    exceptions: Mapped[list[EventException]] = relationship(
        cascade="all, delete-orphan", passive_deletes=True, lazy="selectin"
    )

    @classmethod
    def assignee_clause(cls, principal: Principal) -> ColumnElement[bool]:
        """Makes "visible to assignees" mean something for events."""
        return (
            select(EventAssignment.event_id)
            .where(
                and_(
                    EventAssignment.event_id == cls.id,
                    EventAssignment.user_id == principal.id,
                )
            )
            .exists()
        )

    __table_args__ = (
        CheckConstraint("length(trim(title)) > 0", name="title_not_blank"),
        CheckConstraint("ends_at >= starts_at", name="ends_after_it_starts"),
        # A detached instance needs both halves of its identity, or it cannot be
        # matched to the occurrence it overrides.
        CheckConstraint(
            "(series_id IS NULL) = (recurrence_id IS NULL)",
            name="detached_instances_are_fully_identified",
        ),
        # A detached instance overrides one occurrence; it does not recur itself.
        CheckConstraint(
            "series_id IS NULL OR recurrence_rule IS NULL",
            name="detached_instances_do_not_recur",
        ),
        UniqueConstraint("series_id", "recurrence_id", name="uq_event_override_occurrence"),
        Index("ix_calendar_events_window", "starts_at", "ends_at"),
        Index("ix_calendar_events_calendar_id", "calendar_id"),
        Index("ix_calendar_events_owner_id", "owner_id"),
        Index("ix_calendar_events_series_id", "series_id"),
    )
