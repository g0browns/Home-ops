"""Request and response shapes for the calendar (SPEC §4.3)."""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator

from home_ops.modules.calendar.models import EditScope
from home_ops.policy import Visibility

Stripped = BeforeValidator(lambda value: value.strip() if isinstance(value, str) else value)

Title = Annotated[str, Stripped, Field(min_length=1, max_length=200)]
CalendarName = Annotated[str, Stripped, Field(min_length=1, max_length=64)]
#: An IANA zone name. Rejected at the service layer if unknown; never an offset.
Tzid = Annotated[str, Field(min_length=1, max_length=64)]

# A palette key, validated at the boundary against the same tuple the service
# uses. Its own palette, separate from the member hues: a calendar's colour
# fills a whole event block while a member's edges a row, and an event filled in
# a member's hue would swallow that member's own mark. See
# frontend/src/lib/calendars.ts.
CalendarHue = Literal["violet", "slate", "graphite", "brick", "rust", "moss"]
ColorKey = Annotated[CalendarHue | None, Field(default=None)]


class CalendarOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    color_key: str | None
    is_default: bool
    position: int


class CalendarIn(BaseModel):
    name: CalendarName
    color_key: ColorKey = None
    position: int = Field(default=0, ge=0, le=999)


class CalendarUpdate(BaseModel):
    """Every field optional; only what is supplied changes.

    `exclude_unset` is what distinguishes "leave the colour alone" from "clear
    it", so the route must not read these off a plain default.
    """

    name: CalendarName | None = None
    color_key: ColorKey = None
    position: int | None = Field(default=None, ge=0, le=999)
    is_default: bool | None = None


class EventBase(BaseModel):
    title: Title
    description: str | None = Field(default=None, max_length=10_000)
    location: Annotated[str | None, Field(default=None, max_length=255)] = None
    starts_at: dt.datetime
    ends_at: dt.datetime
    is_all_day: bool = False
    tzid: Tzid = "UTC"
    recurrence_rule: str | None = Field(default=None, max_length=500)
    visibility: Visibility = Visibility.HOUSEHOLD
    assignee_ids: list[UUID] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def _ends_after_it_starts(self) -> EventBase:
        # Checked here as well as by the database CHECK so the caller gets a 422
        # naming the problem rather than a 500 from a constraint violation.
        if self.ends_at < self.starts_at:
            raise ValueError("An event cannot end before it starts.")
        return self


class EventCreate(EventBase):
    calendar_id: UUID | None = None


class OccurrenceOut(BaseModel):
    """One occurrence, resolved. What the calendar grid actually draws."""

    event_id: UUID
    calendar_id: UUID
    title: str
    description: str | None
    location: str | None
    starts_at: dt.datetime
    ends_at: dt.datetime
    is_all_day: bool
    tzid: str
    #: Which occurrence this is (RECURRENCE-ID). Send it back to edit one instance.
    original_start: dt.datetime
    #: True when the event repeats — the UI asks for a scope before editing.
    is_recurring: bool
    #: True when this occurrence has already been detached from its series.
    is_override: bool
    recurrence_label: str | None
    #: The rule itself, so a client that cannot reach the server can still show
    #: an editor with the current pattern in it. `recurrence_label` beside it is
    #: the same thing in words, for reading rather than editing.
    #:
    #: Added for the offline phone client (SPEC-APP.md): everything else it
    #: needs to change a series — `original_start` and the edit scope — was
    #: already here, so this one field was the whole gap.
    recurrence_rule: str | None
    owner_id: UUID
    visibility: Visibility
    assignee_ids: list[UUID] = Field(default_factory=list)


class EventUpdate(BaseModel):
    """An edit, and how far it reaches.

    `scope` is the whole point of §4.3: the same payload means three different
    operations depending on whether it applies to one occurrence, this one and
    everything after it, or the entire series.
    """

    scope: EditScope = EditScope.ALL
    #: Required for a per-occurrence scope: which occurrence you were looking at.
    original_start: dt.datetime | None = None

    title: Title | None = None
    description: str | None = Field(default=None, max_length=10_000)
    location: Annotated[str | None, Field(default=None, max_length=255)] = None
    starts_at: dt.datetime | None = None
    ends_at: dt.datetime | None = None
    is_all_day: bool | None = None
    calendar_id: UUID | None = None
    visibility: Visibility | None = None
    recurrence_rule: str | None = Field(default=None, max_length=500)
    assignee_ids: list[UUID] | None = Field(default=None, max_length=20)

    @model_validator(mode="after")
    def _per_occurrence_scopes_need_an_occurrence(self) -> EventUpdate:
        if self.scope is not EditScope.ALL and self.original_start is None:
            raise ValueError(
                "original_start is required when editing one occurrence or this and following."
            )
        return self


class EventDelete(BaseModel):
    scope: EditScope = EditScope.ALL
    original_start: dt.datetime | None = None

    @model_validator(mode="after")
    def _per_occurrence_scopes_need_an_occurrence(self) -> EventDelete:
        if self.scope is not EditScope.ALL and self.original_start is None:
            raise ValueError(
                "original_start is required when deleting one occurrence or this and following."
            )
        return self


class AgendaTask(BaseModel):
    """A task with a due date, shown on the calendar (SPEC §4.4).

    Deliberately a distinct shape rather than pretending to be an event: it is a
    deadline, not an appointment, and the UI should be able to tell.
    """

    task_id: UUID
    title: str
    due_at: dt.datetime
    status: str
    priority: str
    assignee_ids: list[UUID] = Field(default_factory=list)


class AgendaOut(BaseModel):
    occurrences: list[OccurrenceOut]
    tasks: list[AgendaTask] = Field(default_factory=list)
