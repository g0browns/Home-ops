"""Shopping lists and their lines (SPEC §4.12).

Shopping left the Kitchen on 2026-07-31, and the reason is in the shape of these
two tables. A household buys things that are not groceries, from places that are
not one shop, on days that are not one day — so there are **many lists**, and a
list is the thing that carries visibility.

**The list is visibility-bearing; the item is not.** `ShoppingList` mixes in
`OwnedVisibleMixin`, so the scoping guard refuses to serve it unscoped and
§4.2's three states — private, shared with named members, household — come from
the engine that already has no admin bypass. An item inherits its list's
visibility and carries none of its own: two places to say who may see something
is two places to disagree, and the one that gets forgotten is the leak.

That has a consequence worth stating, because the guard cannot state it for us:
`shopping_items` is **not** registered with the guard, so an unscoped SELECT of
it will not raise. Every read of an item goes through `service.visible_items`,
which scopes by the set of lists the caller may see. `test_shopping.py` asserts
the outcome rather than trusting the discipline.

**A line is either an ingredient or a piece of text**, as it was in 5d, and that
is precisely what makes this more than a grocery list: "bin bags", "a light
bulb", "something for Dad" are first-class rather than second.

**Ticking is per line and idempotent**, which is what lets two people shop at
once without locking — two shoppers ticking different lines never touch the same
row, and two ticking the *same* line agree about the result. Unchanged from 5d,
because it was right.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ColumnElement,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    and_,
    func,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from home_ops.db import Base
from home_ops.modules.kitchen.models import Ingredient
from home_ops.policy import Principal
from home_ops.scoping import OwnedVisibleMixin

#: Gap between positions, so a line can be slotted between two others without
#: renumbering. Same trick as the notes board and the meal plan.
POSITION_STEP = 10

MAX_LIST_NAME = 60
#: Enough for a household, few enough that the screen stays a screen.
MAX_LISTS = 30


class ShoppingListShare(Base):
    """One member a list is shared with.

    This is what makes `Visibility.ASSIGNEES` mean "shared with these people" for
    a list, in exactly the way `TaskAssignment` does for a task. A join table
    rather than a column, from the start.
    """

    __tablename__ = "shopping_list_shares"

    list_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("shopping_lists.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    shared_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (Index("ix_shopping_list_shares_user_id", "user_id"),)


class ShoppingList(OwnedVisibleMixin, Base):
    """One list. Carries `owner_id`, `visibility` and `created_at` from the mixin."""

    __tablename__ = "shopping_lists"

    id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    name: Mapped[str] = mapped_column(String(MAX_LIST_NAME), nullable=False)

    #: The list "Build from this week" fills. **At most one row may have this
    #: set**, and that is enforced by a partial unique index on a constant
    #: expression rather than by the service — a rule the database states cannot
    #: be got wrong by a second code path.
    is_meal_plan_target: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )

    position: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    items: Mapped[list[ShoppingItem]] = relationship(
        back_populates="list", cascade="all, delete-orphan", passive_deletes=True
    )
    shares: Mapped[list[ShoppingListShare]] = relationship(
        cascade="all, delete-orphan", passive_deletes=True, lazy="selectin"
    )

    @classmethod
    def assignee_clause(cls, principal: Principal) -> ColumnElement[bool]:
        """Makes "shared with named members" mean something for a list.

        Without this override the mixin's default treats `assignees` visibility
        as private, which is the safe direction to fail but not the useful one.
        """
        return (
            select(ShoppingListShare.list_id)
            .where(
                and_(
                    ShoppingListShare.list_id == cls.id,
                    ShoppingListShare.user_id == principal.id,
                )
            )
            .exists()
        )

    __table_args__ = (
        CheckConstraint("length(trim(name)) > 0", name="name_not_blank"),
        Index("ix_shopping_lists_position", "position"),
        # At most one meal-plan target, said once, by the database.
        Index(
            "uq_shopping_lists_meal_plan_target",
            text("(is_meal_plan_target)"),
            unique=True,
            postgresql_where=text("is_meal_plan_target"),
        ),
    )


class ShoppingItem(Base):
    """One line on one list.

    No visibility of its own — see the module docstring. `owner_id` records who
    added it, not who may read it.
    """

    __tablename__ = "shopping_items"

    id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )

    #: CASCADE: deleting a list takes its lines with it. A line that outlived its
    #: list would be a row nobody could reach and nobody could scope.
    list_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("shopping_lists.id", ondelete="CASCADE"), nullable=False
    )

    #: RESTRICT, not CASCADE: tidying the ingredient vocabulary must not quietly
    #: remove something from a list somebody is holding in a shop.
    ingredient_id: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("ingredients.id", ondelete="RESTRICT"), nullable=True
    )
    #: For a line that is not an ingredient — "bin bags", "a light bulb".
    title: Mapped[str | None] = mapped_column(String(200), nullable=True)

    #: Decimal, not float: these are totals somebody reads in a shop, and the
    #: aggregation that produces them is exact for the same reason.
    quantity: Mapped[Decimal | None] = mapped_column(Numeric(12, 4), nullable=True)
    unit: Mapped[str | None] = mapped_column(String(16), nullable=True)
    note: Mapped[str | None] = mapped_column(String(200), nullable=True)

    #: Where in the shop this line lives, when the line is not an ingredient. An
    #: ingredient takes its section from `ingredients.aisle` instead, so setting
    #: it once is remembered for every future list; the resolved value is worked
    #: out at read time rather than copied, or renaming an aisle would leave
    #: yesterday's spelling on today's list.
    section: Mapped[str | None] = mapped_column(String(64), nullable=True)

    #: True when this line came from adding up the meal plan rather than from
    #: somebody typing it.
    is_generated: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    #: Somebody has typed their own amount over the one the plan worked out —
    #: "the recipe needs two, buy four". Kept as a flag rather than by turning
    #: the line manual, because a manual line with the same ingredient would be
    #: *added to* by the next build rather than replaced, and the shopper would
    #: end up with two rows for onions. Instead `generate()` carries the
    #: override across a rebuild, exactly as it carries a tick across.
    quantity_overridden: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    #: Set when the quantities behind this line could not be added — 200 g and
    #: 2 cups of the same flour. §4.6: "or flag that it can't".
    is_uncombined: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )

    is_checked: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    checked_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: Who put it in the trolley. Two people in different aisles want to see
    #: that the other has already got the milk.
    checked_by_id: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    position: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    owner_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    list: Mapped[ShoppingList] = relationship(back_populates="items")
    ingredient: Mapped[Ingredient | None] = relationship(lazy="joined")

    __table_args__ = (
        CheckConstraint(
            "ingredient_id IS NOT NULL OR (title IS NOT NULL AND length(trim(title)) > 0)",
            name="item_names_something",
        ),
        CheckConstraint("quantity IS NULL OR quantity > 0", name="quantity_positive"),
        Index("ix_shopping_items_list_id", "list_id"),
        Index("ix_shopping_items_position", "position"),
        Index("ix_shopping_items_ingredient_id", "ingredient_id"),
    )
