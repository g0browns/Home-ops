"""Request and response shapes for tasks (SPEC §4.4)."""

from __future__ import annotations

import datetime as dt
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

from home_ops.modules.tasks.models import TaskPriority, TaskStatus
from home_ops.policy import Visibility

#: Strip *before* the length check, so a title of pure whitespace is rejected
#: at the boundary rather than reaching the database and tripping a CHECK
#: constraint as a 500.
Stripped = BeforeValidator(lambda value: value.strip() if isinstance(value, str) else value)

Title = Annotated[str, Stripped, Field(min_length=1, max_length=200)]
CategoryName = Annotated[str, Stripped, Field(min_length=1, max_length=64)]
ColorKey = Annotated[str | None, Field(default=None, max_length=32)]


class CategoryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    color_key: str | None
    position: int


class CategoryIn(BaseModel):
    name: CategoryName
    color_key: ColorKey = None
    position: int = Field(default=0, ge=0, le=999)


class TaskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    title: str
    description: str | None
    category_id: UUID | None
    priority: TaskPriority
    status: TaskStatus
    due_at: dt.datetime | None
    due_is_all_day: bool
    parent_task_id: UUID | None
    recurrence_rule: str | None
    recurrence_group_id: UUID | None
    #: Present so the UI can label a series without parsing an RRULE itself.
    recurrence_label: str | None = None
    completed_at: dt.datetime | None
    completed_by_user_id: UUID | None
    owner_id: UUID
    visibility: Visibility
    position: int
    created_at: dt.datetime
    assignee_ids: list[UUID] = Field(default_factory=list)


class TaskCreate(BaseModel):
    title: Title
    description: str | None = Field(default=None, max_length=10_000)
    category_id: UUID | None = None
    priority: TaskPriority = TaskPriority.NONE
    due_at: dt.datetime | None = None
    due_is_all_day: bool = True
    parent_task_id: UUID | None = None
    #: Household by default. A chore nobody can see is not a chore — the strict
    #: default belongs to health data (SPEC §4.8), not to the bins.
    visibility: Visibility = Visibility.HOUSEHOLD
    recurrence_rule: str | None = Field(default=None, max_length=500)
    assignee_ids: list[UUID] = Field(default_factory=list, max_length=20)


class TaskUpdate(BaseModel):
    """Every field optional; only what is supplied changes."""

    title: Title | None = None
    description: str | None = Field(default=None, max_length=10_000)
    category_id: UUID | None = None
    priority: TaskPriority | None = None
    status: TaskStatus | None = None
    due_at: dt.datetime | None = None
    due_is_all_day: bool | None = None
    visibility: Visibility | None = None
    recurrence_rule: str | None = Field(default=None, max_length=500)
    position: int | None = Field(default=None, ge=0)
    assignee_ids: list[UUID] | None = Field(default=None, max_length=20)


class CompletionOut(BaseModel):
    """What completing a task produced.

    `successor` is the next instance of a recurring series — the visible
    consequence of the "one open instance at a time" model, so the UI can say
    "next one due Tuesday" instead of leaving the user to wonder.
    """

    completed: TaskOut
    successor: TaskOut | None = None
