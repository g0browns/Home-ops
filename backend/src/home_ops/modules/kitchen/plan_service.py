"""Meal plan operations (SPEC §4.6, phase 5c).

The function worth reading is `visible_entries`, and the reason is §4.2.

A meal plan entry is **shared** — one household, one dinner — but it can point at
a recipe that is **private**. Returning the entry with its recipe's title
attached would hand somebody the name of a record they are not allowed to open,
which is precisely the bypass §4.2 says must not exist. Equally, hiding the entry
would put a hole in a shared plan and make Tuesday look free when it is not.

So the entry is always returned and the *recipe* is resolved through the scoped
query. Someone who cannot see the recipe sees that the slot is taken, by whom,
and nothing else. Nobody bypasses record visibility, including here, and the
plan stays honest about what is booked.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from home_ops.modules.kitchen.models import Recipe
from home_ops.modules.kitchen.plan_models import POSITION_STEP, MealPlanEntry, MealSlot
from home_ops.modules.kitchen.service import visible_recipes
from home_ops.policy import Principal
from home_ops.scoping import SCOPED_OPTION

#: The widest window the planner will answer for. A planner asks for a week; a
#: caller asking for a decade is a mistake or a probe, and either way the answer
#: is no. Same reasoning as the calendar's 400-day cap.
MAX_WINDOW = dt.timedelta(days=120)


class WindowTooWide(ValueError):
    pass


class EntryNotFound(LookupError):
    pass


@dataclass
class ResolvedEntry:
    """One entry, with the recipe attached only if the caller may see it."""

    entry: MealPlanEntry
    recipe: Recipe | None
    #: True when the entry points at a recipe this caller cannot see. The slot
    #: is occupied and they are told so; the title is not theirs to read.
    hidden_recipe: bool


def week_start(day: dt.date, week_starts_on: str = "monday") -> dt.date:
    """The first day of the week containing `day`.

    Mirrors `frontend/src/lib/dates.ts`, because the planner and the calendar
    have to agree about where a week begins — the household setting drives both.
    """
    index = {"monday": 0, "sunday": 6, "saturday": 5}.get(week_starts_on, 0)
    return day - dt.timedelta(days=(day.weekday() - index) % 7)


def list_entries(
    db: DbSession, principal: Principal, *, start: dt.date, end: dt.date
) -> list[ResolvedEntry]:
    """Every entry in a window, with recipes resolved per the caller's sight.

    Two queries rather than a join, and deliberately: the entries are shared, so
    they are read unscoped; the recipes are not, so they go through
    `visible_recipes`. Joining them would make one query that is half-scoped,
    which is the shape of bug this separation exists to prevent.
    """
    if end < start:
        raise ValueError("The window ends before it starts.")
    if end - start > MAX_WINDOW:
        raise WindowTooWide(f"A meal plan window may not exceed {MAX_WINDOW.days} days.")

    stmt = (
        select(MealPlanEntry)
        .where(MealPlanEntry.plan_date >= start, MealPlanEntry.plan_date <= end)
        .order_by(MealPlanEntry.plan_date, MealPlanEntry.position, MealPlanEntry.created_at)
        # Exempt with a reason, as the guard requires: `meal_plan_entries` is not
        # a visibility-bearing table. The plan is the household's — see
        # plan_models.py — and the recipes it points at are scoped separately
        # below, which is the part that actually carries a visibility.
        .execution_options(**{SCOPED_OPTION: True})
    )
    entries = list(db.scalars(stmt))

    wanted = {entry.recipe_id for entry in entries if entry.recipe_id}
    allowed: dict[UUID, Recipe] = {}
    if wanted:
        visible = db.scalars(visible_recipes(principal).where(Recipe.id.in_(wanted)))
        allowed = {recipe.id: recipe for recipe in visible}

    resolved: list[ResolvedEntry] = []
    for entry in entries:
        recipe = allowed.get(entry.recipe_id) if entry.recipe_id else None
        resolved.append(
            ResolvedEntry(
                entry=entry,
                recipe=recipe,
                hidden_recipe=bool(entry.recipe_id) and recipe is None,
            )
        )
    return resolved


def get_entry(db: DbSession, entry_id: UUID) -> MealPlanEntry:
    entry = db.get(MealPlanEntry, entry_id)
    if entry is None:
        raise EntryNotFound(str(entry_id))
    return entry


def _next_position(db: DbSession, plan_date: dt.date, slot: MealSlot) -> int:
    stmt = (
        select(MealPlanEntry.position)
        .where(MealPlanEntry.plan_date == plan_date, MealPlanEntry.slot == slot.value)
        .order_by(MealPlanEntry.position.desc())
        .limit(1)
        .execution_options(**{SCOPED_OPTION: True})
    )
    highest = db.scalar(stmt)
    return (highest or 0) + POSITION_STEP


def add_entry(
    db: DbSession,
    principal: Principal,
    *,
    plan_date: dt.date,
    slot: MealSlot,
    recipe_id: UUID | None = None,
    title: str | None = None,
    note: str | None = None,
) -> MealPlanEntry:
    entry = MealPlanEntry(
        plan_date=plan_date,
        slot=slot.value,
        position=_next_position(db, plan_date, slot),
        recipe_id=recipe_id,
        title=(title or "").strip() or None,
        note=(note or "").strip() or None,
        owner_id=principal.id,
    )
    db.add(entry)
    db.flush()
    return entry


def move_entry(
    db: DbSession,
    entry: MealPlanEntry,
    *,
    plan_date: dt.date,
    slot: MealSlot,
    before_id: UUID | None = None,
) -> MealPlanEntry:
    """Move an entry to a day and slot, optionally ahead of another entry.

    Positions are recomputed for the destination rather than nudged, because a
    drag can land anywhere and gap arithmetic that mostly works is worse than
    renumbering seven items.
    """
    entry.plan_date = plan_date
    entry.slot = slot.value
    db.flush()

    stmt = (
        select(MealPlanEntry)
        .where(MealPlanEntry.plan_date == plan_date, MealPlanEntry.slot == slot.value)
        .order_by(MealPlanEntry.position, MealPlanEntry.created_at)
        .execution_options(**{SCOPED_OPTION: True})
    )
    siblings = [row for row in db.scalars(stmt) if row.id != entry.id]

    if before_id is None:
        siblings.append(entry)
    else:
        index = next((i for i, row in enumerate(siblings) if row.id == before_id), len(siblings))
        siblings.insert(index, entry)

    for position, row in enumerate(siblings):
        row.position = position * POSITION_STEP

    db.flush()
    return entry


def remove_entry(db: DbSession, entry: MealPlanEntry) -> None:
    db.delete(entry)
    db.flush()
