"""Request and response shapes for notes (SPEC §4.5)."""

from __future__ import annotations

import datetime as dt
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

from home_ops.modules.notes.models import MAX_TAG_LENGTH
from home_ops.policy import Visibility

#: Stripped before the length check, so whitespace-only titles are rejected at
#: the boundary rather than tripping a database CHECK as a 500.
Stripped = BeforeValidator(lambda value: value.strip() if isinstance(value, str) else value)

Title = Annotated[str, Stripped, Field(min_length=1, max_length=200)]
#: Generous but bounded. A note is a note, not a document store — §7 rules that
#: out — and an unbounded text field is free work for anyone who finds the API.
Body = Annotated[str, Field(default="", max_length=100_000)]
Tag = Annotated[str, Field(min_length=1, max_length=MAX_TAG_LENGTH)]


class NoteOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    title: str
    #: Markdown source. The client renders it and is responsible for sanitising;
    #: the server never emits HTML.
    body: str
    color_key: str | None
    is_pinned: bool
    #: Manual board order, lowest first. Shared, like pinning.
    position: int
    owner_id: UUID
    visibility: Visibility
    created_at: dt.datetime
    updated_at: dt.datetime
    tags: list[str] = Field(default_factory=list)


class NoteCreate(BaseModel):
    title: Title
    body: Body = ""
    color_key: Annotated[str | None, Field(default=None, max_length=32)] = None
    is_pinned: bool = False
    visibility: Visibility = Visibility.HOUSEHOLD
    tags: list[Tag] = Field(default_factory=list, max_length=20)


class NoteUpdate(BaseModel):
    title: Title | None = None
    body: str | None = Field(default=None, max_length=100_000)
    color_key: Annotated[str | None, Field(default=None, max_length=32)] = None
    is_pinned: bool | None = None
    visibility: Visibility | None = None
    tags: list[Tag] | None = Field(default=None, max_length=20)


class ReorderRequest(BaseModel):
    """The board's new order, front to back.

    Ids the caller cannot see are ignored rather than rejected — see
    `service.reorder_notes` for why refusing loudly would be a disclosure.
    """

    note_ids: list[UUID] = Field(max_length=500)
