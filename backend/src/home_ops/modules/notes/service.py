"""Note operations (SPEC §4.5).

The search function is the one worth reading twice. It applies the same
`scoping.visible()` wrapper as every other read, so full-text search returns
exactly the notes the caller could have found by browsing — no more. Yuvomi's
spec makes the same point about its FTS index; a search that queries the index
directly is the shortest path to handing someone a private note.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from uuid import UUID

from sqlalchemy import Select, delete, func, select, update
from sqlalchemy.orm import Session as DbSession

from home_ops.modules.notes.models import MAX_TAG_LENGTH, SEARCH_CONFIG, Note, NoteTag
from home_ops.policy import Principal, Visibility
from home_ops.scoping import SCOPED_OPTION, visible


class NoteNotVisible(LookupError):
    """The note does not exist, or the caller may not see it — one error for both."""


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def normalize_tag(tag: str) -> str:
    """Tags are lower-cased and trimmed so `Shopping` and `shopping` are one tag."""
    return tag.strip().lower()[:MAX_TAG_LENGTH]


def normalize_tags(tags: list[str]) -> list[str]:
    cleaned = [normalize_tag(tag) for tag in tags]
    # dict.fromkeys de-duplicates while keeping the order the user typed.
    return [tag for tag in dict.fromkeys(cleaned) if tag]


# --- reads --------------------------------------------------------------------


def visible_notes(principal: Principal) -> Select[Any]:
    return visible(select(Note), Note, principal)


def list_notes(
    db: DbSession,
    principal: Principal,
    *,
    search: str | None = None,
    tag: str | None = None,
    author_id: UUID | None = None,
) -> list[Note]:
    """The board. Pinned first, then most recently touched.

    When searching, ordering switches to relevance — `ts_rank_cd` against the
    generated search vector, which weights a hit in the title above one in the
    body.
    """
    stmt = visible_notes(principal)

    if author_id is not None:
        stmt = stmt.where(Note.owner_id == author_id)

    if tag:
        stmt = stmt.where(
            select(NoteTag.note_id)
            .where(NoteTag.note_id == Note.id, NoteTag.tag == normalize_tag(tag))
            .exists()
        )

    query = (search or "").strip()
    if query:
        # websearch_to_tsquery accepts what people actually type — quoted
        # phrases, OR, leading minus — without throwing a syntax error at them,
        # which plainto_tsquery and to_tsquery both do.
        ts_query = func.websearch_to_tsquery(SEARCH_CONFIG, query)
        stmt = stmt.where(Note.search_vector.op("@@")(ts_query)).order_by(
            func.ts_rank_cd(Note.search_vector, ts_query).desc(), Note.updated_at.desc()
        )
    else:
        # Pinned first, then the manual board order, then most recently touched
        # as a stable tiebreak for notes nobody has positioned yet.
        stmt = stmt.order_by(Note.is_pinned.desc(), Note.position, Note.updated_at.desc())

    return list(db.scalars(stmt))


def get_note(db: DbSession, principal: Principal, note_id: UUID) -> Note:
    note: Note | None = db.scalar(visible_notes(principal).where(Note.id == note_id))
    if note is None:
        raise NoteNotVisible(str(note_id))
    return note


def known_tags(db: DbSession, principal: Principal) -> list[str]:
    """Tags in use, for the filter bar.

    Restricted to tags on notes the caller can see. Listing every tag in the
    household would leak the existence and subject of private notes — "divorce
    lawyer" is a disclosure even with the note itself locked away.
    """
    visible_ids = visible_notes(principal).with_only_columns(Note.id).subquery()
    stmt = (
        select(NoteTag.tag)
        .where(NoteTag.note_id.in_(select(visible_ids.c.id)))
        .distinct()
        .order_by(NoteTag.tag)
        # Marked scoped explicitly, and this is the one shape where that is not
        # a shortcut: the subquery above *is* built by visible(), but
        # `.subquery()` drops the statement-level execution option, so the guard
        # sees `notes` in the tree with no marker and cannot tell the difference
        # between this and a genuinely unscoped read. The scoping is real — the
        # tags returned are only those on notes the subquery admits.
        .execution_options(**{SCOPED_OPTION: True})
    )
    return list(db.scalars(stmt))


# --- writes -------------------------------------------------------------------


def create_note(
    db: DbSession,
    principal: Principal,
    *,
    title: str,
    body: str = "",
    color_key: str | None = None,
    is_pinned: bool = False,
    visibility: Visibility = Visibility.HOUSEHOLD,
    tags: list[str] | None = None,
) -> Note:
    note = Note(
        title=title.strip(),
        body=body,
        color_key=color_key,
        is_pinned=is_pinned,
        owner_id=principal.id,
        visibility=visibility.value,
    )
    db.add(note)
    db.flush()

    if tags:
        set_tags(db, note, tags)

    return note


def set_tags(db: DbSession, note: Note, tags: list[str]) -> None:
    """Replace a note's tags wholesale."""
    db.execute(delete(NoteTag).where(NoteTag.note_id == note.id))
    for tag in normalize_tags(tags):
        db.add(NoteTag(note_id=note.id, tag=tag))
    db.flush()


def delete_note(db: DbSession, note: Note) -> None:
    db.delete(note)
    db.flush()


#: Gap between assigned positions. Leaves room to slot a note between two others
#: later without rewriting the whole board.
POSITION_STEP = 10


def reorder_notes(db: DbSession, principal: Principal, note_ids: list[UUID]) -> int:
    """Apply a new board order, and return how many notes moved.

    Only notes the caller can actually see are repositioned — the list is
    intersected with `visible_notes` rather than trusted. Without that, a
    guessed id would let someone shuffle a note they cannot read, which is a
    small leak but a real one: watching the board reflow tells you a private
    note is there.

    Ids the caller cannot see are skipped silently rather than raising, for the
    same reason a missing note returns 404 — refusing loudly would confirm the
    note exists.
    """
    visible_ids = {row for row in db.scalars(visible_notes(principal).with_only_columns(Note.id))}

    moved = 0
    for index, note_id in enumerate(note_ids):
        if note_id not in visible_ids:
            continue
        db.execute(update(Note).where(Note.id == note_id).values(position=index * POSITION_STEP))
        moved += 1

    db.flush()
    return moved
