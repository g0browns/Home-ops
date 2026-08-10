"""Request and response shapes for shopping (SPEC §4.12)."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, BeforeValidator, Field, model_validator

from home_ops.modules.kitchen.models import MAX_INGREDIENT_NAME
from home_ops.modules.kitchen.units import is_unit
from home_ops.modules.shopping.models import MAX_LIST_NAME
from home_ops.policy import Visibility

Stripped = BeforeValidator(lambda value: value.strip() if isinstance(value, str) else value)

# NOTE: a length constraint has to sit on the `str` side of a union, never on
# `str | None` — pydantic tries to measure the length of None and raises a
# TypeError, which is a 500 rather than a 422, the moment a client sends an
# explicit `null`. The whole project learned this from an editor that could not
# save anything while its test suite was green, because every test omitted the
# field and the browser sends null.
ListName = Annotated[str, Stripped, Field(min_length=1, max_length=MAX_LIST_NAME)]
IngredientName = Annotated[str, Stripped, Field(min_length=1, max_length=MAX_INGREDIENT_NAME)]
Section = Annotated[str, Stripped, Field(max_length=64)]
ItemTitle = Annotated[str, Stripped, Field(min_length=1, max_length=200)]
ItemNote = Annotated[str, Stripped, Field(max_length=200)]


def _known_unit(value: str | None) -> str | None:
    if value is None or value == "":
        return None
    if not is_unit(value):
        raise ValueError(f"{value!r} is not a unit we know.")
    return value


UnitKey = Annotated[str | None, BeforeValidator(_known_unit), Field(default=None)]


class ShoppingItemOut(BaseModel):
    id: UUID
    list_id: UUID
    ingredient_id: UUID | None
    #: The ingredient's name, or the text somebody typed.
    name: str
    #: Where in the shop it lives: the ingredient's aisle, or the line's own.
    #: One field, because the client should not have to know which kind of line
    #: it is holding in order to group it.
    section: str | None
    quantity: Decimal | None
    unit: str | None
    note: str | None
    is_generated: bool
    #: The quantities behind this line could not be added — 200 g and 2 cups of
    #: the same flour. §4.6: "or flag that it can't".
    is_uncombined: bool
    #: True when somebody typed their own amount over the plan's. Shown, so a
    #: line that does not change when the plan does is not a mystery.
    quantity_overridden: bool = False
    is_checked: bool
    #: Who put it in the trolley, so two people in different aisles can tell.
    checked_by_id: UUID | None
    position: int


class ShoppingListOut(BaseModel):
    id: UUID
    name: str
    visibility: Visibility
    #: Members this list is shared with. Meaningful only while visibility is
    #: `assignees`; kept regardless, so flipping back does not lose them.
    shared_with: list[UUID] = Field(default_factory=list)
    is_meal_plan_target: bool
    position: int
    owner_id: UUID
    items: list[ShoppingItemOut] = Field(default_factory=list)


class ShoppingListIn(BaseModel):
    #: Optional client-generated primary key, for a client that queues writes
    #: while offline. Supplying it makes creation **idempotent**: a retry of a
    #: request whose response was lost returns the existing row instead of
    #: adding a second one. That is the ordinary outcome on a supermarket
    #: connection, and it is the one write in this module that was not already
    #: safe to repeat. Omitted, the database generates one exactly as before.
    id: UUID | None = None
    name: ListName
    visibility: Visibility = Visibility.HOUSEHOLD
    shared_with: Annotated[list[UUID], Field(max_length=50)] = Field(default_factory=list)
    is_meal_plan_target: bool = False


class ShoppingListPatch(BaseModel):
    """Every field optional; only what is supplied changes."""

    name: ListName | None = None
    visibility: Visibility | None = None
    shared_with: Annotated[list[UUID], Field(max_length=50)] | None = None
    is_meal_plan_target: bool | None = None


class ShoppingListOrder(BaseModel):
    order: Annotated[list[UUID], Field(max_length=100)]


class ShoppingItemIn(BaseModel):
    """A line somebody typed.

    An ingredient name resolves through the shared kitchen vocabulary and
    inherits its aisle; a bare title is for the things that are not ingredients
    at all, which on a hardware list is all of them.
    """

    #: Optional client-generated primary key, for a client that queues writes
    #: while offline. Supplying it makes creation **idempotent**: a retry of a
    #: request whose response was lost returns the existing row instead of
    #: adding a second one. That is the ordinary outcome on a supermarket
    #: connection, and it is the one write in this module that was not already
    #: safe to repeat. Omitted, the database generates one exactly as before.
    id: UUID | None = None
    ingredient_name: IngredientName | None = None
    title: ItemTitle | None = None
    quantity: Annotated[Decimal, Field(gt=0, le=Decimal("100000"))] | None = None
    unit: UnitKey = None
    note: ItemNote | None = None
    section: Section | None = None

    @model_validator(mode="after")
    def _names_something(self) -> ShoppingItemIn:
        if not (self.ingredient_name or "").strip() and not (self.title or "").strip():
            raise ValueError("A shopping list line needs a name.")
        return self


class ShoppingItemPatch(BaseModel):
    is_checked: bool | None = None
    #: How much to buy. Sending a number on a *generated* line overrides what
    #: the meal plan worked out and survives the next build; sending `null`
    #: gives the line back to the plan's arithmetic.
    quantity: Annotated[Decimal, Field(gt=0, le=Decimal("100000"))] | None = None
    unit: UnitKey = None
    note: ItemNote | None = None
    #: Where in the shop it lives. On an ingredient this writes the ingredient's
    #: aisle, so it is remembered on every future list.
    section: Section | None = None
    #: Send this line to another list. §4.12's transfer, and the reason an item
    #: PATCH exists at all rather than only a tick endpoint.
    list_id: UUID | None = None


class ShoppingGenerateIn(BaseModel):
    start: dt.date
    end: dt.date
    #: Which list to fill. Absent means the one marked as the meal-plan list,
    #: which is the ordinary case and the reason the flag exists.
    list_id: UUID | None = None


class ShoppingGenerateResult(BaseModel):
    list_id: UUID
    #: Planned meals whose recipe this caller may not open. A count, so the list
    #: is honest about being incomplete without disclosing what is missing.
    hidden_meals: int = 0
    #: Planned meals that are free text and have no ingredients to add. Named,
    #: so a shopper knows they were not forgotten.
    text_meals: list[str] = Field(default_factory=list)
    #: How many lines could not be combined into a single quantity.
    uncombined: int = 0
    #: Wanted, but already sitting on another list because somebody moved it
    #: there. Not an error — it is the transfer working.
    kept_on_other_lists: int = 0


class ShoppingClearResult(BaseModel):
    removed: int
