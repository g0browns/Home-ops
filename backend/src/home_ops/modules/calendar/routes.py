"""Calendar endpoints (SPEC §4.3).

The agenda endpoint returns *occurrences*, not events: a recurring event is one
row in the database and many things on a wall planner, and resolving that on the
server means the browser never has to parse an RRULE or reason about daylight
saving to draw a grid.
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from home_ops import audit
from home_ops.audit import AuditAction
from home_ops.dependencies import AuthDep, ClientIpDep, DbDep, require
from home_ops.modules.calendar import expansion, schemas, service
from home_ops.modules.calendar.models import Calendar, CalendarEvent
from home_ops.modules.tasks import service as task_service
from home_ops.policy import Action, Module, Visibility, can_edit_record

router = APIRouter(prefix="/calendar", tags=["calendar"])
calendars_router = APIRouter(prefix="/calendars", tags=["calendar"])


def _as_utc(moment: dt.datetime) -> dt.datetime:
    """Every instant leaves the API in UTC.

    Expanded occurrences arrive in UTC while values read straight from the
    database carry the session timezone, so without this the same endpoint
    returns `...19:00:00+00:00` and `...20:00:00+01:00` for the same moment.
    Both are correct and clients handle both, but one representation is
    easier to read, to log, and to compare in a test.
    """
    return moment.astimezone(dt.UTC)


def _to_occurrence(
    item: service.ExpandedOccurrence, assignees: list[UUID]
) -> schemas.OccurrenceOut:
    event = item.event
    return schemas.OccurrenceOut(
        event_id=event.id,
        calendar_id=event.calendar_id,
        title=event.title,
        description=event.description,
        location=event.location,
        starts_at=_as_utc(item.starts_at),
        ends_at=_as_utc(item.ends_at),
        is_all_day=event.is_all_day,
        tzid=event.tzid,
        original_start=_as_utc(item.original_start),
        is_recurring=event.recurrence_rule is not None or event.series_id is not None,
        is_override=item.is_override,
        recurrence_label=(
            expansion.describe(event.recurrence_rule) if event.recurrence_rule else None
        ),
        recurrence_rule=event.recurrence_rule,
        owner_id=event.owner_id,
        visibility=Visibility(event.visibility),
        assignee_ids=assignees,
    )


def _aware(value: dt.datetime) -> dt.datetime:
    """Treat a naive window bound as UTC.

    `?start=2026-07-03` is a perfectly reasonable thing to send, and pydantic
    parses it into a *naive* datetime. Events are stored timezone-aware, so
    comparing the two raised `can't compare offset-naive and offset-aware
    datetimes` — a 500 on well-formed input, from deep inside the expansion
    where the cause is invisible.

    It never showed because the web app builds its window from `Date` objects
    and always sends an offset. The first client to send a bare date found it
    immediately, which is the usual way a second client earns its keep.

    Assumed UTC rather than rejected: a window bound is a coarse thing, the
    error would help nobody, and every other date in this API is UTC.
    """
    return value if value.tzinfo is not None else value.replace(tzinfo=dt.UTC)


def _load_editable(db: DbDep, auth: AuthDep, event_id: UUID) -> CalendarEvent:
    """404 for what you cannot see, 403 for what you can see but may not change."""
    try:
        event = service.get_event(db, auth.principal, event_id)
    except service.EventNotVisible as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="No such event.") from exc

    if not can_edit_record(
        auth.principal,
        Action.WRITE,
        Module.CALENDAR,
        owner_id=event.owner_id,
        visibility=Visibility(event.visibility),
        assignee_ids=frozenset(service.assignee_ids(db, event.id)),
        deviations=auth.deviations,
    ):
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Not permitted to change this event.")
    return event


# --- calendars ----------------------------------------------------------------


@calendars_router.get(
    "",
    response_model=list[schemas.CalendarOut],
    dependencies=[Depends(require(Action.READ, Module.CALENDAR))],
    summary="List calendars",
)
def list_calendars(db: DbDep) -> list[Calendar]:
    # Touching the default here means a fresh household always has one calendar
    # to put things in, without a data migration to seed it.
    service.default_calendar(db)
    db.commit()
    return service.list_calendars(db)


@calendars_router.post(
    "",
    response_model=schemas.CalendarOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require(Action.WRITE, Module.SETTINGS))],
    summary="Add a calendar",
)
def create_calendar(
    payload: schemas.CalendarIn, auth: AuthDep, db: DbDep, client_ip: ClientIpDep
) -> Calendar:
    """Gated on settings, like task categories: calendars are shared structure."""
    calendar = Calendar(
        name=payload.name,
        # Round-robin when none is chosen, so a new calendar is never the same
        # colour as one that already exists — an uncoloured calendar would fall
        # back to the neutral and be indistinguishable from every other one.
        color_key=payload.color_key or service.next_calendar_hue(db),
        position=payload.position,
    )
    db.add(calendar)
    db.flush()
    audit.record(
        db,
        AuditAction.HOUSEHOLD_SETTING_CHANGED,
        actor_id=auth.user.id,
        actor_label=auth.user.username,
        resource_type="calendar",
        resource_id=str(calendar.id),
        client_ip=client_ip,
        detail={"created": calendar.name},
    )
    db.commit()
    return calendar


@calendars_router.patch(
    "/{calendar_id}",
    response_model=schemas.CalendarOut,
    dependencies=[Depends(require(Action.WRITE, Module.SETTINGS))],
    summary="Rename or recolour a calendar",
)
def update_calendar(
    calendar_id: UUID,
    payload: schemas.CalendarUpdate,
    auth: AuthDep,
    db: DbDep,
    client_ip: ClientIpDep,
) -> Calendar:
    calendar = db.get(Calendar, calendar_id)
    if calendar is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="No such calendar.")

    changed = payload.model_dump(exclude_unset=True)
    for field, value in changed.items():
        if field != "is_default":
            setattr(calendar, field, value)

    if changed.get("is_default"):
        service.make_default(db, calendar)

    db.flush()
    audit.record(
        db,
        AuditAction.HOUSEHOLD_SETTING_CHANGED,
        actor_id=auth.user.id,
        actor_label=auth.user.username,
        resource_type="calendar",
        resource_id=str(calendar.id),
        client_ip=client_ip,
        detail={"updated": calendar.name, "fields": sorted(changed)},
    )
    db.commit()
    return calendar


@calendars_router.delete(
    "/{calendar_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require(Action.WRITE, Module.SETTINGS))],
    summary="Remove a calendar and its events",
)
def delete_calendar(
    calendar_id: UUID, auth: AuthDep, db: DbDep, client_ip: ClientIpDep
) -> Response:
    calendar = db.get(Calendar, calendar_id)
    if calendar is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="No such calendar.")
    if calendar.is_default:
        # Deleting the default would leave new events with nowhere to go.
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail="Make another calendar the default before deleting this one.",
        )

    name = calendar.name
    db.delete(calendar)
    audit.record(
        db,
        AuditAction.HOUSEHOLD_SETTING_CHANGED,
        actor_id=auth.user.id,
        actor_label=auth.user.username,
        resource_type="calendar",
        resource_id=str(calendar_id),
        client_ip=client_ip,
        # Unlike a task category, deleting a calendar takes its events with it.
        detail={"deleted": name, "events_deleted": True},
    )
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- the agenda ---------------------------------------------------------------


@router.get(
    "/agenda",
    response_model=schemas.AgendaOut,
    dependencies=[Depends(require(Action.READ, Module.CALENDAR))],
    summary="Occurrences in a window, with tasks due in it",
)
def agenda(
    auth: AuthDep,
    db: DbDep,
    start: dt.datetime,
    end: dt.datetime,
    calendar_id: UUID | None = None,
    assignee_id: UUID | None = None,
    search: Annotated[str | None, Query(max_length=200)] = None,
    include_tasks: bool = True,
) -> schemas.AgendaOut:
    try:
        found = service.occurrences_in(
            db,
            auth.principal,
            window_start=_aware(start),
            window_end=_aware(end),
            calendar_id=calendar_id,
            assignee_id=assignee_id,
            search=search,
        )
    except service.WindowTooWide as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    assignees = service.assignees_for(db, [item.event.id for item in found])
    occurrences = [_to_occurrence(item, assignees.get(item.event.id, [])) for item in found]

    tasks: list[schemas.AgendaTask] = []
    if include_tasks and auth.can(Action.READ, Module.TASKS):
        # SPEC §4.4: "Tasks with due dates should surface on the calendar."
        # Read through the task service so its visibility scoping applies —
        # a private task must not become visible by being due.
        due = [
            task
            for task in task_service.list_tasks(
                db, auth.principal, task_service.TaskFilters(include_subtasks=True)
            )
            if task.due_at is not None and start <= task.due_at < end
        ]
        task_assignees = task_service.assignees_for(db, [task.id for task in due])
        tasks = [
            schemas.AgendaTask(
                task_id=task.id,
                title=task.title,
                due_at=_as_utc(task.due_at),
                status=task.status,
                priority=task.priority,
                assignee_ids=task_assignees.get(task.id, []),
            )
            for task in due
            if task.due_at is not None
        ]

    return schemas.AgendaOut(occurrences=occurrences, tasks=tasks)


# --- events -------------------------------------------------------------------


@router.post(
    "/events",
    response_model=schemas.OccurrenceOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require(Action.WRITE, Module.CALENDAR))],
    summary="Create an event",
)
def create_event(
    payload: schemas.EventCreate, auth: AuthDep, db: DbDep, client_ip: ClientIpDep
) -> schemas.OccurrenceOut:
    calendar_id = payload.calendar_id or service.default_calendar(db).id
    if db.get(Calendar, calendar_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="No such calendar.")

    try:
        event = service.create_event(
            db,
            auth.principal,
            calendar_id=calendar_id,
            title=payload.title,
            description=payload.description,
            location=payload.location,
            starts_at=payload.starts_at,
            ends_at=payload.ends_at,
            is_all_day=payload.is_all_day,
            tzid=payload.tzid,
            recurrence_rule=payload.recurrence_rule,
            visibility=payload.visibility,
            assignees=list(payload.assignee_ids),
        )
    except (expansion.InvalidRecurrenceRule, expansion.UnknownTimezone) as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    audit.record(
        db,
        AuditAction.EVENT_CREATED,
        actor_id=auth.user.id,
        actor_label=auth.user.username,
        resource_type="calendar_event",
        resource_id=str(event.id),
        client_ip=client_ip,
        detail={"title": event.title, "visibility": event.visibility},
    )
    db.commit()

    return _to_occurrence(
        service.ExpandedOccurrence(
            event=event,
            starts_at=event.starts_at,
            ends_at=event.ends_at,
            original_start=event.starts_at,
            is_override=False,
        ),
        service.assignee_ids(db, event.id),
    )


@router.patch(
    "/events/{event_id}",
    response_model=schemas.OccurrenceOut,
    dependencies=[Depends(require(Action.WRITE, Module.CALENDAR))],
    summary="Edit an event, at the requested scope",
)
def update_event(
    event_id: UUID,
    payload: schemas.EventUpdate,
    auth: AuthDep,
    db: DbDep,
    client_ip: ClientIpDep,
) -> schemas.OccurrenceOut:
    event = _load_editable(db, auth, event_id)

    changes: dict[str, object] = {}
    for field in ("title", "description", "location", "is_all_day", "calendar_id"):
        value = getattr(payload, field)
        if value is not None:
            changes[field] = value
    if payload.visibility is not None:
        changes["visibility"] = payload.visibility.value
    if payload.recurrence_rule is not None:
        try:
            changes["recurrence_rule"] = expansion.validate_rule(payload.recurrence_rule)
        except expansion.InvalidRecurrenceRule as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    try:
        result = service.edit_occurrence(
            db,
            event,
            scope=payload.scope,
            original_start=payload.original_start or event.starts_at,
            changes=changes,
            new_start=payload.starts_at,
            new_end=payload.ends_at,
            assignees=list(payload.assignee_ids) if payload.assignee_ids is not None else None,
        )
    except service.NotRecurring as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    audit.record(
        db,
        AuditAction.EVENT_UPDATED,
        actor_id=auth.user.id,
        actor_label=auth.user.username,
        resource_type="calendar_event",
        resource_id=str(event.id),
        client_ip=client_ip,
        detail={"scope": payload.scope.value, "changed": sorted(changes)},
    )
    db.commit()

    return _to_occurrence(
        service.ExpandedOccurrence(
            event=result,
            starts_at=result.starts_at,
            ends_at=result.ends_at,
            original_start=result.recurrence_id or result.starts_at,
            is_override=result.series_id is not None,
        ),
        service.assignee_ids(db, result.id),
    )


@router.post(
    "/events/{event_id}/delete",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require(Action.WRITE, Module.CALENDAR))],
    summary="Delete an event, at the requested scope",
)
def delete_event(
    event_id: UUID,
    payload: schemas.EventDelete,
    auth: AuthDep,
    db: DbDep,
    client_ip: ClientIpDep,
) -> Response:
    """POST rather than DELETE because it carries a body.

    A scope and an occurrence are not optional here, and DELETE with a body is
    poorly supported by proxies and by fetch.
    """
    event = _load_editable(db, auth, event_id)
    title = event.title

    service.delete_occurrence(
        db,
        event,
        scope=payload.scope,
        original_start=payload.original_start or event.starts_at,
    )
    audit.record(
        db,
        AuditAction.EVENT_DELETED,
        actor_id=auth.user.id,
        actor_label=auth.user.username,
        resource_type="calendar_event",
        resource_id=str(event_id),
        client_ip=client_ip,
        detail={"title": title, "scope": payload.scope.value},
    )
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


__all__ = ["calendars_router", "router"]
