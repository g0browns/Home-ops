"""Shopping list operations (SPEC §4.12).

Three rules carry this module, and each one exists because getting it wrong is
worse than not having the feature.

**Every read of an item is scoped by the lists the caller may see.** The item
table is not visibility-bearing, so the scoping guard will not catch a forgotten
scope here the way it does for a list. `visible_items()` is therefore the only
way items are read, and it takes the list scope from `visible_lists()` rather
than re-deriving it.

**Generating reads the Kitchen, so it is gated on the Kitchen too.** The route
requires `shopping` write *and* `kitchen` read. Producing a list from recipes is
a kitchen read by a longer route, and somebody with no Kitchen access must not
obtain recipe contents through this module. Inside that, recipes still go
through `visible_recipes`, so record visibility applies as well: a planned
recipe the caller may not open contributes nothing and is reported as a count.

**Generation never touches a line on another list.** An ingredient already
sitting on any visible list as a plan-generated line is skipped, so an item
dragged to "Sam's Club" is not recreated on the meal-plan list next week.
Without that, moving something to the shop you actually buy it from would
silently duplicate it, and the transfer feature would be useless.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session as DbSession

from home_ops.modules.kitchen.models import Ingredient, Recipe, RecipeIngredient
from home_ops.modules.kitchen.plan_models import MealPlanEntry
from home_ops.modules.kitchen.service import resolve_ingredient, visible_recipes
from home_ops.modules.shopping import aggregate
from home_ops.modules.shopping.models import (
    MAX_LISTS,
    POSITION_STEP,
    ShoppingItem,
    ShoppingList,
    ShoppingListShare,
)
from home_ops.policy import Principal, Visibility
from home_ops.scoping import SCOPED_OPTION, visible

#: The widest span a list may be generated from. A shopping list is a week, or a
#: fortnight if somebody is organised. A year is a mistake.
MAX_SPAN = dt.timedelta(days=60)

#: The list the migration creates for everything that existed before there were
#: lists, and the one a household starts with.
DEFAULT_LIST_NAME = "Groceries"


class SpanTooWide(ValueError):
    pass


class ItemNotFound(LookupError):
    pass


class ListNotFound(LookupError):
    pass


class NoMealPlanTarget(LookupError):
    pass


class IdAlreadyTaken(ValueError):
    """A client-supplied id names a row that exists and the caller cannot see.

    Essentially unreachable: v4 UUIDs do not collide, so the only way here is a
    deliberate probe, and all it learns is that some id is taken. It exists so
    that probe gets a 409 rather than an IntegrityError and a 500.
    """


class TooManyLists(ValueError):
    pass


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


# --- lists --------------------------------------------------------------------


def visible_lists(db: DbSession, principal: Principal) -> list[ShoppingList]:
    """Every list this caller may see, in walking order.

    Through `visible()`, so private lists stay private and a list shared with
    named members reaches exactly those members. There is no admin branch here
    and there must never be one (§4.2).
    """
    stmt = visible(select(ShoppingList), ShoppingList, principal).order_by(
        ShoppingList.position, ShoppingList.created_at
    )
    return list(db.scalars(stmt).unique())


def get_list(db: DbSession, principal: Principal, list_id: UUID) -> ShoppingList:
    """One list, or `ListNotFound`.

    A list the caller may not see is reported as missing rather than forbidden.
    "You may not see this" confirms it exists, which is the disclosure the
    visibility rule is there to prevent.
    """
    stmt = visible(select(ShoppingList), ShoppingList, principal).where(ShoppingList.id == list_id)
    found: ShoppingList | None = db.scalars(stmt).unique().one_or_none()
    if found is None:
        raise ListNotFound(str(list_id))
    return found


def meal_plan_target(db: DbSession, principal: Principal) -> ShoppingList:
    """The list "Build from this week" fills.

    Scoped like any other read: if the flagged list is one this caller may not
    see, they have no meal-plan target, rather than a pointer to somebody
    else's list.
    """
    for candidate in visible_lists(db, principal):
        if candidate.is_meal_plan_target:
            return candidate
    raise NoMealPlanTarget("No list is set as the meal-plan list.")


def create_list(
    db: DbSession,
    principal: Principal,
    *,
    name: str,
    visibility: Visibility = Visibility.HOUSEHOLD,
    is_meal_plan_target: bool = False,
    shared_with: list[UUID] | None = None,
    item_id: UUID | None = None,
) -> ShoppingList:
    if item_id is not None:
        # Looked up **through the scoped query**, not by primary key. An
        # unscoped `db.get` here would hand back a list the caller cannot see
        # if they happened to name its id — turning a retry-safety mechanism
        # into the disclosure §4.2 exists to prevent. The scoping guard catches
        # this, which is the whole reason it exists.
        try:
            # An offline client retrying a request whose response was lost. The
            # row is already what it asked for, so hand it back rather than
            # making a second one. Returned unchanged: a retry is a repeat of
            # the original intent, not a later edit.
            return get_list(db, principal, item_id)
        except ListNotFound:
            # Not visible to this caller — but it may still exist. Counted
            # rather than selected: an aggregate exposes no row, which is the
            # exemption `scoping.py` names for exactly this shape.
            taken = db.scalar(
                select(func.count())
                .select_from(ShoppingList)
                .where(ShoppingList.id == item_id)
                .execution_options(**{SCOPED_OPTION: True})
            )
            if taken:
                raise IdAlreadyTaken(str(item_id)) from None

    total = db.scalar(
        select(func.count()).select_from(ShoppingList).execution_options(**{SCOPED_OPTION: True})
    )
    # Counted across the household rather than per caller: the cap is about the
    # table, and a count of rows discloses no row.
    if (total or 0) >= MAX_LISTS:
        raise TooManyLists(f"A household may keep {MAX_LISTS} lists.")

    if is_meal_plan_target:
        _clear_meal_plan_target(db)

    created = ShoppingList(
        # See `add_manual`: set only when supplied, never as an explicit None.
        **({"id": item_id} if item_id is not None else {}),
        name=name.strip(),
        visibility=visibility.value,
        is_meal_plan_target=is_meal_plan_target,
        position=_next_list_position(db),
        owner_id=principal.id,
    )
    db.add(created)
    db.flush()
    set_shares(db, created, shared_with or [])
    return created


def update_list(
    db: DbSession,
    shopping_list: ShoppingList,
    *,
    name: str | None = None,
    visibility: Visibility | None = None,
    is_meal_plan_target: bool | None = None,
    shared_with: list[UUID] | None = None,
) -> ShoppingList:
    if name is not None:
        shopping_list.name = name.strip()
    if visibility is not None:
        shopping_list.visibility = visibility.value
    if is_meal_plan_target is not None:
        if is_meal_plan_target:
            # Cleared first, then set: the partial unique index would otherwise
            # reject the second target before the first was released.
            _clear_meal_plan_target(db)
        shopping_list.is_meal_plan_target = is_meal_plan_target
    if shared_with is not None:
        set_shares(db, shopping_list, shared_with)
    db.flush()
    return shopping_list


def set_shares(db: DbSession, shopping_list: ShoppingList, user_ids: list[UUID]) -> None:
    """Who a list is shared with, replacing whatever was there.

    Shares are kept even while the list is `private` or `household`, so flipping
    visibility back to "shared with named members" does not silently lose them.
    They mean nothing until the visibility says they do.
    """
    db.execute(delete(ShoppingListShare).where(ShoppingListShare.list_id == shopping_list.id))
    for user_id in dict.fromkeys(user_ids):
        db.add(ShoppingListShare(list_id=shopping_list.id, user_id=user_id))
    db.flush()
    db.expire(shopping_list, ["shares"])


def delete_list(db: DbSession, shopping_list: ShoppingList) -> None:
    db.delete(shopping_list)
    db.flush()


def reorder_lists(db: DbSession, principal: Principal, order: list[UUID]) -> None:
    """The order the lists are drawn in, shared across everybody who sees them.

    Only lists the caller can see are moved; anything they cannot see keeps its
    position, so reordering cannot be used to probe for lists that exist.
    """
    by_id = {item.id: item for item in visible_lists(db, principal)}
    position = 0
    for list_id in order:
        found = by_id.get(list_id)
        if found is None:
            continue
        found.position = position
        position += POSITION_STEP
    db.flush()


def _clear_meal_plan_target(db: DbSession) -> None:
    """Unset the flag wherever it is, including on a list this caller cannot see.

    Unscoped deliberately: there is exactly one target for the household, and
    handing it over means taking it from whoever had it. This writes a flag and
    returns no rows, so it discloses nothing.
    """
    for current in db.scalars(
        select(ShoppingList)
        .where(ShoppingList.is_meal_plan_target.is_(True))
        .execution_options(**{SCOPED_OPTION: True})
    ):
        current.is_meal_plan_target = False
    db.flush()


def _next_list_position(db: DbSession) -> int:
    stmt = (
        select(ShoppingList.position)
        .order_by(ShoppingList.position.desc())
        .limit(1)
        .execution_options(**{SCOPED_OPTION: True})
    )
    return (db.scalar(stmt) or 0) + POSITION_STEP


# --- items --------------------------------------------------------------------


def visible_items(db: DbSession, principal: Principal) -> list[ShoppingItem]:
    """Every line on every list this caller may see.

    **The only way items are read.** `shopping_items` carries no visibility of
    its own and so is not registered with the scoping guard; the scope comes
    from the lists instead. Adding a second read path that does not come through
    here is how a private list's contents escape it.
    """
    list_ids = [item.id for item in visible_lists(db, principal)]
    if not list_ids:
        return []

    stmt = (
        select(ShoppingItem)
        .where(ShoppingItem.list_id.in_(list_ids))
        .order_by(ShoppingItem.is_checked, ShoppingItem.position, ShoppingItem.created_at)
        # Scoped by list above. The guard does not cover this table, which is
        # exactly why every caller comes through this function.
        #
        # `populate_existing` makes this an actual re-read. The session is built
        # with `expire_on_commit=False`, so without it the identity map hands
        # back whatever is already in memory — and a PATCH that answers with
        # its own input reports `500` where every later read says `500.0000`,
        # because the value has not been through a `Numeric(12,4)` column yet.
        .execution_options(populate_existing=True, **{SCOPED_OPTION: True})
    )
    return list(db.scalars(stmt).unique())


def get_item(db: DbSession, principal: Principal, item_id: UUID) -> ShoppingItem:
    """One line, and only if its list is one the caller may see."""
    for item in visible_items(db, principal):
        if item.id == item_id:
            return item
    raise ItemNotFound(str(item_id))


def _next_item_position(db: DbSession, list_id: UUID) -> int:
    stmt = (
        select(ShoppingItem.position)
        .where(ShoppingItem.list_id == list_id)
        .order_by(ShoppingItem.position.desc())
        .limit(1)
        .execution_options(**{SCOPED_OPTION: True})
    )
    return (db.scalar(stmt) or 0) + POSITION_STEP


def add_manual(
    db: DbSession,
    principal: Principal,
    shopping_list: ShoppingList,
    *,
    title: str | None = None,
    ingredient_name: str | None = None,
    quantity: Decimal | None = None,
    unit: str | None = None,
    note: str | None = None,
    section: str | None = None,
    item_id: UUID | None = None,
) -> ShoppingItem:
    """A line somebody typed.

    An ingredient name resolves through the shared kitchen vocabulary so a
    manual "flour" lands on the same row as a generated one and inherits its
    aisle. A bare title is for everything that is not an ingredient — which, on
    a hardware list, is all of it.
    """
    if item_id is not None:
        try:
            # See `create_list`: a retried offline write, not a new line. Scoped
            # too — `get_item` only returns lines on a list the caller may see.
            return get_item(db, principal, item_id)
        except ItemNotFound:
            # `shopping_items` carries no visibility of its own, so this needs
            # no exemption — but the row may belong to a list this caller
            # cannot see, and reusing its id must not be allowed to work.
            if db.get(ShoppingItem, item_id) is not None:
                raise IdAlreadyTaken(str(item_id)) from None

    ingredient = resolve_ingredient(db, ingredient_name) if ingredient_name else None
    item = ShoppingItem(
        # Only set when supplied. Passing `id=None` explicitly is not the same as
        # leaving it unset: the column's `gen_random_uuid()` server default is
        # what fills it otherwise, and an explicit None invites SQLAlchemy to
        # send a NULL primary key instead.
        **({"id": item_id} if item_id is not None else {}),
        list_id=shopping_list.id,
        ingredient_id=ingredient.id if ingredient else None,
        title=None if ingredient else (title or "").strip() or None,
        quantity=quantity,
        unit=unit,
        note=(note or "").strip() or None,
        section=(section or "").strip() or None,
        is_generated=False,
        position=_next_item_position(db, shopping_list.id),
        owner_id=principal.id,
    )
    db.add(item)
    db.flush()
    return item


def set_checked(
    db: DbSession, principal: Principal, item: ShoppingItem, *, checked: bool
) -> ShoppingItem:
    """Tick or untick one line.

    Idempotent and per line, which is what lets two people shop at once without
    locking: different lines never touch the same row, and the same line twice
    agrees about the answer.
    """
    item.is_checked = checked
    item.checked_at = utcnow() if checked else None
    item.checked_by_id = principal.id if checked else None
    db.flush()
    return item


def move_item(db: DbSession, item: ShoppingItem, target: ShoppingList) -> ShoppingItem:
    """Send a line to another list.

    The line keeps `is_generated`, and that is deliberate rather than an
    oversight: it is what tells the next generation that this ingredient is
    already accounted for somewhere, so dragging Chicken to "Sam's Club" does
    not put Chicken back on the groceries list next week.
    """
    if item.list_id == target.id:
        return item
    item.list_id = target.id
    item.position = _next_item_position(db, target.id)
    db.flush()
    return item


def set_quantity(
    db: DbSession, item: ShoppingItem, quantity: Decimal | None, unit: str | None
) -> ShoppingItem:
    """Type your own amount over the one the plan worked out.

    "The recipes need two onions, buy four" is the case this exists for, and the
    point of it is that it has to *survive the next build* — otherwise the
    spare onions vanish on Sunday and nobody knows why. On a generated line the
    override is flagged, and `generate()` carries it across a rebuild exactly as
    it carries a tick across.

    Passing `None` hands the line back to the plan's arithmetic.
    """
    item.quantity = quantity
    item.unit = unit
    # Only a generated line can be "overridden": on a manual line the quantity
    # is simply the quantity, and there is nothing to be overriding.
    item.quantity_overridden = item.is_generated and quantity is not None
    db.flush()
    return item


def remove_item(db: DbSession, item: ShoppingItem) -> None:
    db.delete(item)
    db.flush()


def clear_checked(db: DbSession, shopping_list: ShoppingList) -> int:
    """Everything in the trolley, off this list. Returns how many went."""
    items = [item for item in shopping_list.items if item.is_checked]
    for item in items:
        db.delete(item)
    db.flush()
    return len(items)


def set_section(db: DbSession, item: ShoppingItem, section: str | None) -> ShoppingItem:
    """Where in the shop this line lives.

    On an ingredient it writes the *ingredient's* aisle, so saying it once is
    remembered on every future list; on a text line it writes the line's own
    section, because there is nothing shared to remember it on. One control, one
    meaning — "where in the shop is this".
    """
    value = (section or "").strip() or None
    if item.ingredient is not None:
        item.ingredient.aisle = value
    else:
        item.section = value
    db.flush()
    return item


def section_of(item: ShoppingItem) -> str | None:
    """The resolved section: the ingredient's aisle, or the line's own.

    Resolved at read time rather than copied at write time, or renaming an aisle
    would leave yesterday's spelling on today's list.
    """
    if item.ingredient is not None:
        return item.ingredient.aisle
    return item.section


def known_sections(db: DbSession, principal: Principal) -> list[str]:
    """Every section anybody has named, so the next one is offered not retyped.

    Ingredient aisles are household vocabulary and disclose nothing about whose
    list uses them; the sections on *items* are read only from lists this caller
    can see, because a section typed on a private list is a word off that list.
    """
    aisles = {
        aisle
        for aisle in db.scalars(
            select(Ingredient.aisle).where(Ingredient.aisle.is_not(None)).distinct()
        )
        if aisle
    }
    sections = {item.section for item in visible_items(db, principal) if item.section}
    return sorted(aisles | sections, key=str.lower)


# --- generating from the meal plan --------------------------------------------


def _wanted_from_plan(
    db: DbSession, principal: Principal, *, start: dt.date, end: dt.date
) -> tuple[list[aggregate.Wanted], int, list[str]]:
    """Every ingredient line behind the plan, and what could not be read."""
    entries = list(
        db.scalars(
            select(MealPlanEntry)
            .where(MealPlanEntry.plan_date >= start, MealPlanEntry.plan_date <= end)
            # The plan is shared and carries no visibility — see plan_models.
            .execution_options(**{SCOPED_OPTION: True})
        )
    )

    recipe_ids = {entry.recipe_id for entry in entries if entry.recipe_id}
    text_meals = [entry.title for entry in entries if entry.title and not entry.recipe_id]

    allowed: set[UUID] = set()
    if recipe_ids:
        allowed = {
            recipe.id
            for recipe in db.scalars(visible_recipes(principal).where(Recipe.id.in_(recipe_ids)))
        }
    hidden = len([entry for entry in entries if entry.recipe_id and entry.recipe_id not in allowed])

    wanted: list[aggregate.Wanted] = []
    if allowed:
        rows = db.execute(
            select(RecipeIngredient, Ingredient)
            .join(Ingredient, Ingredient.id == RecipeIngredient.ingredient_id)
            .where(RecipeIngredient.recipe_id.in_(allowed))
            .execution_options(**{SCOPED_OPTION: True})
        ).all()

        # A recipe planned twice in the week should be bought for twice.
        times_planned: dict[UUID, int] = {}
        for entry in entries:
            if entry.recipe_id in allowed:
                times_planned[entry.recipe_id] = times_planned.get(entry.recipe_id, 0) + 1

        for line, ingredient in rows:
            for _ in range(times_planned.get(line.recipe_id, 0)):
                wanted.append(
                    aggregate.Wanted(
                        ingredient_id=ingredient.id,
                        name=ingredient.name,
                        aisle=ingredient.aisle,
                        quantity=line.quantity,
                        unit=line.unit,
                    )
                )

    return wanted, hidden, text_meals


def generate(
    db: DbSession,
    principal: Principal,
    target: ShoppingList,
    *,
    start: dt.date,
    end: dt.date,
) -> aggregate.Basket:
    """Rebuild the generated part of one list from the plan.

    Three things are left alone, and each is a promise the button makes:

    * **Manual lines on the target**, so pressing it twice is safe.
    * **Every other list**, entirely — including generated lines that were
      dragged there. An ingredient already sitting on another visible list as a
      generated line is skipped rather than recreated here.
    * **Ticks**, carried across where the same ingredient is still wanted, since
      losing one mid-shop is worse than a stale one.
    """
    if end < start:
        raise ValueError("The range ends before it starts.")
    if end - start > MAX_SPAN:
        raise SpanTooWide(f"A shopping list may not span more than {MAX_SPAN.days} days.")

    wanted, hidden, text_meals = _wanted_from_plan(db, principal, start=start, end=end)
    lines = aggregate.aggregate(wanted)

    everything = visible_items(db, principal)
    previously_checked = {
        item.ingredient_id
        for item in everything
        if item.list_id == target.id
        and item.is_generated
        and item.is_checked
        and item.ingredient_id
    }
    # Amounts somebody typed over the plan's. Carried across a rebuild for the
    # same reason the ticks are: losing "buy four, not two" every Sunday would
    # make the edit pointless.
    overridden = {
        item.ingredient_id: (item.quantity, item.unit)
        for item in everything
        if item.list_id == target.id
        and item.is_generated
        and item.quantity_overridden
        and item.ingredient_id
    }
    # Claimed elsewhere: a generated line somebody moved to another list. It
    # stays where they put it and is not recreated here.
    claimed_elsewhere = {
        item.ingredient_id
        for item in everything
        if item.list_id != target.id and item.is_generated and item.ingredient_id
    }

    db.execute(
        delete(ShoppingItem).where(
            ShoppingItem.list_id == target.id, ShoppingItem.is_generated.is_(True)
        )
    )
    db.flush()

    position = _next_item_position(db, target.id)
    kept_elsewhere = 0
    for line in lines:
        if line.ingredient_id in claimed_elsewhere:
            kept_elsewhere += 1
            continue
        was_checked = line.ingredient_id in previously_checked
        override = overridden.get(line.ingredient_id)
        db.add(
            ShoppingItem(
                list_id=target.id,
                ingredient_id=line.ingredient_id,
                quantity=override[0] if override else line.quantity,
                unit=override[1] if override else line.unit,
                quantity_overridden=override is not None,
                is_generated=True,
                is_uncombined=line.uncombined,
                is_checked=was_checked,
                checked_at=utcnow() if was_checked else None,
                checked_by_id=principal.id if was_checked else None,
                position=position,
                owner_id=principal.id,
            )
        )
        position += POSITION_STEP
    db.flush()

    return aggregate.Basket(
        lines=lines,
        hidden_meals=hidden,
        text_meals=text_meals,
        kept_on_other_lists=kept_elsewhere,
    )
