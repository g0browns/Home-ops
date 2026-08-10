"""The weekly meal plan (SPEC §4.6, phase 5c).

One table, and two decisions in it worth stating.

**The plan is shared, not owned.** Unlike a task or a note, a meal plan entry has
no `visibility`: there is one household and one dinner, and everybody needs to
know Tuesday is taken. `owner_id` is recorded for the audit trail, not to decide
who may see it. That is deliberately *unlike* the rest of the app, so it is said
here rather than left to be inferred from a missing column.

That leaves one real leak to close, and the route closes it: an entry may point
at a **private** recipe, and the entry being shared must not make that recipe's
title shared. See `plan_service.visible_entries`.

**An entry is either a recipe or a piece of text, and both are first-class.**
§4.6 asks that "a planned meal can be saved back as a recipe", which only means
anything if you can write "Mum's lasagne" into Thursday before any such recipe
exists. `recipe_id` and `title` are therefore both nullable, with a CHECK that at
least one is present — an entry that is neither is not a plan, it is a blank.
"""

from __future__ import annotations

import datetime as dt
from enum import StrEnum
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from home_ops.db import Base
from home_ops.modules.kitchen.models import Recipe


class MealSlot(StrEnum):
    """When in the day. The same four Mealie uses, on purpose.

    Their vocabulary maps onto ours one-for-one, so a meal plan imported from a
    Mealie backup lands without translation. Worth keeping even though this
    household's own data uses only `dinner` and `side`: the planner hides slots
    nobody uses rather than making them impossible.
    """

    BREAKFAST = "breakfast"
    LUNCH = "lunch"
    DINNER = "dinner"
    SIDE = "side"


#: The order slots read in a day. Not alphabetical, and not the enum's order
#: either — `side` sits after `dinner` because that is where a side goes.
SLOT_ORDER: tuple[MealSlot, ...] = (
    MealSlot.BREAKFAST,
    MealSlot.LUNCH,
    MealSlot.DINNER,
    MealSlot.SIDE,
)

#: Gap between positions within a slot, so an entry can be dropped between two
#: others without renumbering the day. Same trick as the notes board.
POSITION_STEP = 10


class MealPlanEntry(Base):
    """One thing planned for one day."""

    __tablename__ = "meal_plan_entries"

    id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )

    #: A calendar date, not a timestamp. A meal belongs to a day in the
    #: household's own reckoning; giving it an instant would drag the whole
    #: timezone question (SPEC §4.3) into a thing that does not have one.
    plan_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    slot: Mapped[str] = mapped_column(String(16), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))

    #: CASCADE, not SET NULL: deleting a recipe should take it off the plan.
    #: SET NULL would leave an entry pointing at nothing, occupying a Tuesday
    #: with no way to say what it was.
    recipe_id: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("recipes.id", ondelete="CASCADE"), nullable=True
    )
    #: For an entry that is not a recipe yet — "Leftovers", "Mum's lasagne".
    title: Mapped[str | None] = mapped_column(String(200), nullable=True)
    note: Mapped[str | None] = mapped_column(String(200), nullable=True)

    #: Who put it there. For the audit trail; it decides nothing about who sees
    #: it, because the plan is the household's.
    owner_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    recipe: Mapped[Recipe | None] = relationship(lazy="joined")

    __table_args__ = (
        CheckConstraint(
            "slot IN ('breakfast', 'lunch', 'dinner', 'side')",
            name="slot_is_known",
        ),
        # An entry that names neither a recipe nor a dish is a blank, and a
        # blank in a planner is a bug somebody will have to explain later.
        CheckConstraint(
            "recipe_id IS NOT NULL OR (title IS NOT NULL AND length(trim(title)) > 0)",
            name="entry_names_something",
        ),
        # The planner always reads a date window; this is the index for it.
        Index("ix_meal_plan_entries_date", "plan_date", "slot", "position"),
        Index("ix_meal_plan_entries_recipe_id", "recipe_id"),
    )
