"""Calendar operations, including the three edit scopes (SPEC §4.3).

Editing one occurrence of a series is the part §4.3 singles out as hard, and the
difficulty is not the arithmetic — it is that each scope is a genuinely
different operation on the data:

``this``
    Detach the occurrence into its own event, pointed back at the master. The
    master is untouched, so later edits to the series still reach every
    occurrence except this one.

``this_and_following``
    Truncate the master to end just before this occurrence, then start a new
    series here carrying the edits. Two series where there was one — which is
    what "and following" means, and why a COUNT has to be split between them.

``all``
    Edit the master in place. Existing detached instances survive, because
    someone moved those deliberately.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import Select, delete, select
from sqlalchemy.orm import Session as DbSession

from home_ops.modules.calendar import expansion
from home_ops.modules.calendar.models import (
    Calendar,
    CalendarEvent,
    EditScope,
    EventAssignment,
    EventException,
)
from home_ops.policy import Principal, Visibility
from home_ops.scoping import visible

#: Refuse to expand an unbounded span. A year at a time is generous for a wall
#: planner and keeps one careless request from expanding a decade of dailies.
MAX_WINDOW = dt.timedelta(days=400)


class EventNotVisible(LookupError):
    """The event does not exist, or the caller may not see it — one error for both."""


class WindowTooWide(ValueError):
    pass


class NotRecurring(ValueError):
    """A per-occurrence scope was asked for on an event that does not recur."""


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


# --- calendars ----------------------------------------------------------------

# Calendar hues, in assignment order. KEYS, not colours: the hex values live in
# the frontend's tokens.css and differ between light and dark.
#
# A separate palette from MEMBER_HUES, and deliberately so. A calendar's hue
# fills a whole event block while a member's hue edges a row, so an event filled
# in a member's colour would swallow that member's own initials mark. Two
# palettes makes that impossible rather than merely unlikely.
#
# Must stay in step with CALENDAR_HUES in frontend/src/lib/calendars.ts and with
# the Literal in this package's schemas.py — tests/test_calendars.py asserts all
# three agree.
CALENDAR_HUES: tuple[str, ...] = ("violet", "slate", "graphite", "brick", "rust", "moss")


def next_calendar_hue(db: DbSession) -> str:
    """The least-used hue, ties broken by the order above.

    Round-robin rather than random, for the same reason members get one: a
    household with three calendars should see three visibly different colours
    rather than roll the same one twice.
    """
    counts = dict.fromkeys(CALENDAR_HUES, 0)
    for (colour,) in db.execute(select(Calendar.color_key)):
        if colour in counts:
            counts[colour] += 1
    return min(CALENDAR_HUES, key=lambda hue: (counts[hue], CALENDAR_HUES.index(hue)))


def make_default(db: DbSession, calendar: Calendar) -> None:
    """Promote one calendar, demoting whichever held the flag.

    Both halves in one transaction: a partial unique index allows exactly one
    default, so setting the new flag before clearing the old one violates it.
    """
    for other in db.scalars(select(Calendar).where(Calendar.is_default.is_(True))):
        other.is_default = False
    db.flush()
    calendar.is_default = True
    db.flush()


def list_calendars(db: DbSession) -> list[Calendar]:
    """Household vocabulary, like task categories — not visibility-scoped."""
    return list(db.scalars(select(Calendar).order_by(Calendar.position, Calendar.name)))


def default_calendar(db: DbSession) -> Calendar:
    """The calendar new events land in, created on first use.

    Auto-created rather than seeded by a migration: a migration that inserts
    rows is a data migration, and this is a detail the application owns.
    """
    existing = db.scalar(select(Calendar).where(Calendar.is_default.is_(True)))
    if existing is not None:
        return existing

    first = db.scalar(select(Calendar).order_by(Calendar.position, Calendar.name))
    if first is not None:
        first.is_default = True
        db.flush()
        return first

    created = Calendar(name="Household", is_default=True, color_key=CALENDAR_HUES[0])
    db.add(created)
    db.flush()
    return created


# --- reads --------------------------------------------------------------------


def visible_events(principal: Principal) -> Select[Any]:
    return visible(select(CalendarEvent), CalendarEvent, principal)


def get_event(db: DbSession, principal: Principal, event_id: UUID) -> CalendarEvent:
    event: CalendarEvent | None = db.scalar(
        visible_events(principal).where(CalendarEvent.id == event_id)
    )
    if event is None:
        raise EventNotVisible(str(event_id))
    return event


def assignee_ids(db: DbSession, event_id: UUID) -> list[UUID]:
    return list(
        db.scalars(select(EventAssignment.user_id).where(EventAssignment.event_id == event_id))
    )


def assignees_for(db: DbSession, event_ids: list[UUID]) -> dict[UUID, list[UUID]]:
    if not event_ids:
        return {}
    rows = db.execute(
        select(EventAssignment.event_id, EventAssignment.user_id).where(
            EventAssignment.event_id.in_(event_ids)
        )
    )
    grouped: dict[UUID, list[UUID]] = {event_id: [] for event_id in event_ids}
    for event_id, user_id in rows:
        grouped[event_id].append(user_id)
    return grouped


@dataclass(frozen=True)
class ExpandedOccurrence:
    event: CalendarEvent
    starts_at: dt.datetime
    ends_at: dt.datetime
    #: The RECURRENCE-ID this occurrence answers to, for editing one instance.
    original_start: dt.datetime
    #: True when this came from a detached instance rather than the series.
    is_override: bool


def occurrences_in(
    db: DbSession,
    principal: Principal,
    *,
    window_start: dt.datetime,
    window_end: dt.datetime,
    calendar_id: UUID | None = None,
    assignee_id: UUID | None = None,
    search: str | None = None,
) -> list[ExpandedOccurrence]:
    """Every occurrence overlapping the window, from visible events only.

    Filters are applied to the *events* before expansion, so a filtered view
    costs no more than an unfiltered one.
    """
    span = window_end - window_start
    if span <= dt.timedelta(0) or span > MAX_WINDOW:
        raise WindowTooWide(f"Ask for at most {MAX_WINDOW.days} days at a time.")

    stmt = visible_events(principal)
    if calendar_id is not None:
        stmt = stmt.where(CalendarEvent.calendar_id == calendar_id)
    if assignee_id is not None:
        stmt = stmt.where(
            select(EventAssignment.event_id)
            .where(
                EventAssignment.event_id == CalendarEvent.id,
                EventAssignment.user_id == assignee_id,
            )
            .exists()
        )
    if search and search.strip():
        pattern = f"%{search.strip()}%"
        stmt = stmt.where(
            CalendarEvent.title.ilike(pattern)
            | CalendarEvent.description.ilike(pattern)
            | CalendarEvent.location.ilike(pattern)
        )

    events = list(db.scalars(stmt))
    masters = [event for event in events if event.series_id is None]
    overrides = [event for event in events if event.series_id is not None]

    # An override may be visible while its master is not, and vice versa. Group
    # by series so each master knows which of its occurrences are spoken for.
    overridden_by_series: dict[UUID, set[dt.datetime]] = {}
    for override in overrides:
        if override.series_id is not None and override.recurrence_id is not None:
            overridden_by_series.setdefault(override.series_id, set()).add(override.recurrence_id)

    found: list[ExpandedOccurrence] = []

    for master in masters:
        exclusions = frozenset(exception.occurrence_start for exception in master.exceptions)
        for occurrence in expansion.expand(
            starts_at=master.starts_at,
            ends_at=master.ends_at,
            rule=master.recurrence_rule,
            tzid=master.tzid,
            window_start=window_start,
            window_end=window_end,
            exclusions=exclusions,
            overridden=frozenset(overridden_by_series.get(master.id, set())),
        ):
            found.append(
                ExpandedOccurrence(
                    event=master,
                    starts_at=occurrence.starts_at,
                    ends_at=occurrence.ends_at,
                    original_start=occurrence.original_start,
                    is_override=False,
                )
            )

    for override in overrides:
        if override.starts_at < window_end and override.ends_at > window_start:
            found.append(
                ExpandedOccurrence(
                    event=override,
                    starts_at=override.starts_at,
                    ends_at=override.ends_at,
                    original_start=override.recurrence_id or override.starts_at,
                    is_override=True,
                )
            )

    found.sort(key=lambda item: (item.starts_at, item.event.title))
    return found


# --- writes -------------------------------------------------------------------


def create_event(
    db: DbSession,
    principal: Principal,
    *,
    calendar_id: UUID,
    title: str,
    starts_at: dt.datetime,
    ends_at: dt.datetime,
    tzid: str,
    description: str | None = None,
    location: str | None = None,
    is_all_day: bool = False,
    recurrence_rule: str | None = None,
    visibility: Visibility = Visibility.HOUSEHOLD,
    assignees: list[UUID] | None = None,
) -> CalendarEvent:
    expansion.zone_for(tzid)  # rejects an offset or an unknown zone
    rule = expansion.validate_rule(recurrence_rule) if recurrence_rule else None

    event = CalendarEvent(
        calendar_id=calendar_id,
        title=title.strip(),
        description=description,
        location=location,
        starts_at=starts_at,
        ends_at=ends_at,
        is_all_day=is_all_day,
        tzid=tzid,
        recurrence_rule=rule,
        owner_id=principal.id,
        visibility=visibility.value,
    )
    db.add(event)
    db.flush()

    if assignees:
        set_assignees(db, event, assignees)
    return event


def set_assignees(db: DbSession, event: CalendarEvent, user_ids: list[UUID]) -> None:
    db.execute(delete(EventAssignment).where(EventAssignment.event_id == event.id))
    for user_id in dict.fromkeys(user_ids):
        db.add(EventAssignment(event_id=event.id, user_id=user_id))
    db.flush()


def _copy_for_detach(
    source: CalendarEvent,
    *,
    starts_at: dt.datetime,
    ends_at: dt.datetime,
    changes: dict[str, Any],
) -> CalendarEvent:
    return CalendarEvent(
        calendar_id=changes.get("calendar_id", source.calendar_id),
        title=changes.get("title", source.title),
        description=changes.get("description", source.description),
        location=changes.get("location", source.location),
        starts_at=starts_at,
        ends_at=ends_at,
        is_all_day=changes.get("is_all_day", source.is_all_day),
        tzid=changes.get("tzid", source.tzid),
        owner_id=source.owner_id,
        visibility=changes.get("visibility", source.visibility),
    )


def edit_occurrence(
    db: DbSession,
    master: CalendarEvent,
    *,
    scope: EditScope,
    original_start: dt.datetime,
    changes: dict[str, Any],
    new_start: dt.datetime | None = None,
    new_end: dt.datetime | None = None,
    assignees: list[UUID] | None = None,
) -> CalendarEvent:
    """Apply `changes` at the requested scope and return the event that now owns them."""
    if scope is not EditScope.ALL and not master.recurrence_rule:
        raise NotRecurring("This event does not repeat, so there is nothing to split.")

    duration = master.ends_at - master.starts_at
    starts_at = new_start or original_start
    ends_at = new_end or (starts_at + duration)

    if scope is EditScope.ALL:
        for field, value in changes.items():
            setattr(master, field, value)
        if new_start is not None:
            master.starts_at = new_start
            master.ends_at = new_end or (new_start + duration)
        if assignees is not None:
            set_assignees(db, master, assignees)
        db.flush()
        return master

    if scope is EditScope.THIS:
        detached = _copy_for_detach(master, starts_at=starts_at, ends_at=ends_at, changes=changes)
        detached.series_id = master.id
        detached.recurrence_id = original_start
        db.add(detached)
        db.flush()
        set_assignees(
            db, detached, assignees if assignees is not None else assignee_ids(db, master.id)
        )
        return detached

    # this_and_following: truncate the master, then start a new series here.
    rule = master.recurrence_rule or ""
    count = expansion.rule_count(rule)

    if count is not None:
        # Split the COUNT between the two halves. Left as-is, the truncated
        # series would keep the full count and the new one would start it again,
        # so the pair would run roughly twice as long as the original.
        taken = expansion.occurrences_before(
            starts_at=master.starts_at, rule=rule, tzid=master.tzid, split_at=original_start
        )
        master.recurrence_rule = expansion.with_count(rule, taken)
        remaining = max(count - taken, 1)
        following_rule = expansion.with_count(rule, remaining)
    else:
        master.recurrence_rule = expansion.with_until(
            rule, original_start - dt.timedelta(seconds=1)
        )
        following_rule = rule

    following = _copy_for_detach(master, starts_at=starts_at, ends_at=ends_at, changes=changes)
    following.recurrence_rule = following_rule
    db.add(following)
    db.flush()

    # Exclusions at or after the split belong to the new series.
    for exception in list(master.exceptions):
        if exception.occurrence_start >= original_start:
            db.add(
                EventException(event_id=following.id, occurrence_start=exception.occurrence_start)
            )
            db.delete(exception)

    set_assignees(
        db, following, assignees if assignees is not None else assignee_ids(db, master.id)
    )
    db.flush()
    return following


def delete_occurrence(
    db: DbSession, master: CalendarEvent, *, scope: EditScope, original_start: dt.datetime
) -> None:
    """Remove one occurrence, this-and-following, or the whole series."""
    if scope is EditScope.ALL or not master.recurrence_rule:
        db.delete(master)
        db.flush()
        return

    if scope is EditScope.THIS:
        # An override for this occurrence is now meaningless; drop it too.
        db.execute(
            delete(CalendarEvent).where(
                CalendarEvent.series_id == master.id,
                CalendarEvent.recurrence_id == original_start,
            )
        )
        db.add(EventException(event_id=master.id, occurrence_start=original_start))
        db.flush()
        return

    rule = master.recurrence_rule
    count = expansion.rule_count(rule)
    if count is not None:
        taken = expansion.occurrences_before(
            starts_at=master.starts_at, rule=rule, tzid=master.tzid, split_at=original_start
        )
        master.recurrence_rule = expansion.with_count(rule, taken)
    else:
        master.recurrence_rule = expansion.with_until(
            rule, original_start - dt.timedelta(seconds=1)
        )

    # Detached instances after the split have nothing left to override.
    db.execute(
        delete(CalendarEvent).where(
            CalendarEvent.series_id == master.id,
            CalendarEvent.recurrence_id >= original_start,
        )
    )
    db.flush()


def delete_event(db: DbSession, event: CalendarEvent) -> None:
    db.delete(event)
    db.flush()
