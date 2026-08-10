"""Tasks, subtasks, assignments and categories (SPEC §4.4).

The first tables to carry per-item visibility, so this is where the Phase 1
permission layer stops being theoretical: `Task` mixes in `OwnedVisibleMixin`,
which registers it with the scoping guard — any read that forgets
`scoping.visible()` now raises instead of quietly returning another member's
private chores.

**Recurrence** follows the model chosen for §4.4: one open instance at a time,
the next generated on completion rather than on schedule. Instances of a series
share a `recurrence_group_id`, and a partial unique index makes "only one open"
a database guarantee rather than a promise the service layer has to keep.
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


class TaskPriority(StrEnum):
    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    URGENT = "urgent"


class TaskStatus(StrEnum):
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    ARCHIVED = "archived"


#: Statuses that count as "still on someone's plate". Used by the one-open-
#: instance index and by the default list filter.
LIVE_STATUSES: tuple[str, ...] = (TaskStatus.OPEN.value, TaskStatus.IN_PROGRESS.value)

#: SPEC §4.4 wants subtasks, not an outline tool. Yuvomi caps at the same depth
#: and it keeps the kanban board legible.
MAX_SUBTASK_DEPTH = 2


def _in_values(column: str, enum_cls: type[StrEnum]) -> str:
    rendered = ", ".join(f"'{member.value}'" for member in enum_cls)
    return f"{column} IN ({rendered})"


class TaskCategory(Base):
    """A household-wide label for tasks. Managed in Settings."""

    __tablename__ = "task_categories"

    id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    # A palette key, never a hex — same rule as member hues, and for the same
    # reason: the colour differs between themes.
    color_key: Mapped[str | None] = mapped_column(String(32), nullable=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        # Case-insensitive uniqueness: "Chores" and "chores" are one category.
        Index("uq_task_categories_name", func.lower(name), unique=True),
        CheckConstraint("length(trim(name)) > 0", name="name_not_blank"),
    )


class TaskAssignment(Base):
    """Who a task is assigned to (SPEC §4.4 — multi-member assignment).

    A join table from the start rather than a single `assigned_to` column.
    Yuvomi still carries the legacy column beside this one; starting here avoids
    inheriting that.
    """

    __tablename__ = "task_assignments"

    task_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("tasks.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    assigned_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (Index("ix_task_assignments_user_id", "user_id"),)


class Task(OwnedVisibleMixin, Base):
    """A task. Carries `owner_id`, `visibility` and `created_at` from the mixin."""

    __tablename__ = "tasks"

    id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )

    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    category_id: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("task_categories.id", ondelete="SET NULL"),
        nullable=True,
    )

    priority: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text(f"'{TaskPriority.NONE.value}'")
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text(f"'{TaskStatus.OPEN.value}'")
    )

    # Timestamptz plus an all-day flag rather than a bare date: SPEC §4.4 says
    # tasks with due dates surface on the calendar, and the calendar needs to
    # know whether 09:00 was meant or merely implied.
    due_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    due_is_all_day: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )

    parent_task_id: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("tasks.id", ondelete="CASCADE"), nullable=True
    )

    # RFC 5545 RRULE. Parsed with dateutil rather than hand-rolled, per the same
    # instruction SPEC §4.3 gives for the calendar.
    recurrence_rule: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Shared by every instance of one series, so history survives and the
    #: partial unique index below can hold "one open instance at a time".
    recurrence_group_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    #: When the series began, carried unchanged to every successor.
    #:
    #: Load-bearing for COUNT and UNTIL. Re-anchoring DTSTART to each instance's
    #: own due date restarts the count every time, so `FREQ=DAILY;COUNT=2` would
    #: recur forever. The rule has to be evaluated against the start of the
    #: series, not the start of the latest instance.
    recurrence_start_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    completed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_by_user_id: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    #: Manual ordering within a kanban column.
    position: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))

    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    assignments: Mapped[list[TaskAssignment]] = relationship(
        cascade="all, delete-orphan", passive_deletes=True
    )
    subtasks: Mapped[list[Task]] = relationship(cascade="all, delete-orphan", passive_deletes=True)

    @classmethod
    def assignee_clause(cls, principal: Principal) -> ColumnElement[bool]:
        """Makes `assignees` visibility mean something for tasks.

        Without this override the mixin's default treats "visible to assignees"
        as private, which is the safe direction to fail but not the useful one.
        """
        return (
            select(TaskAssignment.task_id)
            .where(
                and_(
                    TaskAssignment.task_id == cls.id,
                    TaskAssignment.user_id == principal.id,
                )
            )
            .exists()
        )

    __table_args__ = (
        CheckConstraint(_in_values("priority", TaskPriority), name="priority_is_known"),
        CheckConstraint(_in_values("status", TaskStatus), name="status_is_known"),
        CheckConstraint("length(trim(title)) > 0", name="title_not_blank"),
        CheckConstraint("parent_task_id IS NULL OR parent_task_id <> id", name="no_self_parent"),
        # An open task cannot also be completed.
        CheckConstraint(
            "status <> 'open' OR completed_at IS NULL", name="open_tasks_are_not_completed"
        ),
        # A recurring task must belong to a series, or its history has no thread.
        CheckConstraint(
            "recurrence_rule IS NULL OR recurrence_group_id IS NOT NULL",
            name="recurring_tasks_have_a_group",
        ),
        # The chosen recurrence model, enforced by the database rather than by
        # the service layer remembering: at most one live instance per series.
        Index(
            "uq_tasks_one_live_instance_per_series",
            "recurrence_group_id",
            unique=True,
            postgresql_where=text(
                "recurrence_group_id IS NOT NULL AND status IN ('open', 'in_progress')"
            ),
        ),
        Index("ix_tasks_status_due_at", "status", "due_at"),
        Index("ix_tasks_owner_id", "owner_id"),
        Index("ix_tasks_category_id", "category_id"),
        Index("ix_tasks_parent_task_id", "parent_task_id"),
        Index("ix_tasks_recurrence_group_id", "recurrence_group_id"),
    )
