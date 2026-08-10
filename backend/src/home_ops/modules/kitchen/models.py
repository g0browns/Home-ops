"""Recipes (SPEC §4.6, phase 5a).

Two shapes here are load-bearing for work that has not been built yet, and both
would be painful to retrofit:

**Ingredients are a normalised table, not a string per row.** §4.6's shopping
list has to aggregate duplicates across a week of meals. "Plain flour", "plain
flour" and "Plain Flour " are one thing to a shopper and three things to a
`GROUP BY` on free text. Referencing a shared row makes the aggregate exact
rather than fuzzy, and it gives the aisle a place to live — §4.6 wants the list
grouped by aisle, and an aisle is a property of the ingredient, not of the
recipe that happens to mention it.

**Quantities are `Numeric`, not float.** They get multiplied by a scaling factor
and then summed into a shopping list somebody is holding in a supermarket, and
0.30000000000000004 kg of anything is indefensible.

Search follows the notes module exactly: a generated `tsvector` column on the
recipes table itself, so a search *is* a select on `recipes` and the scoping
guard applies to it like any other read. A separate index table would be a
second read path to forget to scope.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    Computed,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from home_ops.db import Base
from home_ops.scoping import OwnedVisibleMixin

SEARCH_CONFIG = "english"

MAX_TAG_LENGTH = 32
MAX_INGREDIENT_NAME = 120


class Ingredient(Base):
    """A thing you can buy. Shared across every recipe that mentions it.

    Household vocabulary, like task categories and calendars — which is why it
    carries no owner and no visibility. Knowing that "flour" exists tells you
    nothing about whose recipe uses it.
    """

    __tablename__ = "ingredients"

    id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    name: Mapped[str] = mapped_column(String(MAX_INGREDIENT_NAME), nullable=False)
    #: Where it lives in a shop. Nullable until somebody says; §4.6 groups the
    #: shopping list by this in 5d.
    aisle: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        # Case-insensitively unique: "Flour" and "flour" must not become two
        # rows, or the aggregate this table exists for stops working.
        Index("uq_ingredients_name", func.lower(name), unique=True),
        CheckConstraint("length(trim(name)) > 0", name="name_not_blank"),
        Index("ix_ingredients_aisle", "aisle"),
    )


class RecipeTag(Base):
    """One tag on one recipe. Same shape as note_tags, and for the same reason."""

    __tablename__ = "recipe_tags"

    recipe_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("recipes.id", ondelete="CASCADE"), primary_key=True
    )
    tag: Mapped[str] = mapped_column(String(MAX_TAG_LENGTH), primary_key=True)

    __table_args__ = (
        CheckConstraint("tag = lower(tag)", name="tag_is_lowercase"),
        CheckConstraint("length(trim(tag)) > 0", name="tag_not_blank"),
        Index("ix_recipe_tags_tag", "tag"),
    )


class RecipeIngredient(Base):
    """One line of the ingredient list."""

    __tablename__ = "recipe_ingredients"

    id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    recipe_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("recipes.id", ondelete="CASCADE"), nullable=False
    )
    #: Ingredients are not deleted out from under a recipe: RESTRICT rather than
    #: CASCADE, so tidying the vocabulary can never silently empty a recipe.
    ingredient_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("ingredients.id", ondelete="RESTRICT"), nullable=False
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))

    #: Null for "salt to taste" — an ingredient with no measurement is normal,
    #: and scaling has to leave it alone rather than turn it into zero.
    quantity: Mapped[Decimal | None] = mapped_column(Numeric(12, 4), nullable=True)
    #: A key from units.py, or null for a bare count ("2 eggs").
    unit: Mapped[str | None] = mapped_column(String(16), nullable=True)
    #: "finely chopped", "at room temperature".
    note: Mapped[str | None] = mapped_column(String(200), nullable=True)

    ingredient: Mapped[Ingredient] = relationship(lazy="joined")

    __table_args__ = (
        CheckConstraint("quantity IS NULL OR quantity > 0", name="quantity_positive"),
        Index("ix_recipe_ingredients_recipe_id", "recipe_id", "position"),
        Index("ix_recipe_ingredients_ingredient_id", "ingredient_id"),
    )


class RecipeStep(Base):
    """One instruction, in order."""

    __tablename__ = "recipe_steps"

    id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    recipe_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("recipes.id", ondelete="CASCADE"), nullable=False
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    text_body: Mapped[str] = mapped_column("body", Text, nullable=False)

    __table_args__ = (
        CheckConstraint("length(trim(body)) > 0", name="body_not_blank"),
        Index("ix_recipe_steps_recipe_id", "recipe_id", "position"),
    )


class Recipe(OwnedVisibleMixin, Base):
    """A recipe. Carries owner_id, visibility and created_at from the mixin."""

    __tablename__ = "recipes"

    id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )

    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))

    #: What the quantities below are written for. Scaling is relative to this,
    #: so it must never be zero — hence the CHECK rather than a hopeful default.
    servings: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("4"))
    prep_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cook_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)

    #: Where it came from, when it was imported. Phase 5b fills this in; it is
    #: here now so the import lands without a migration.
    source_url: Mapped[str | None] = mapped_column(String(2000), nullable=True)

    #: The stored image's identity, not its path. A path in the database would
    #: mean the layout on disk could never change, and a user-supplied component
    #: in one would be a traversal waiting to happen — see storage.py, which is
    #: the only place that turns this into a filename.
    image_key: Mapped[str | None] = mapped_column(String(64), nullable=True)

    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    search_vector: Mapped[str] = mapped_column(
        TSVECTOR,
        Computed(
            f"setweight(to_tsvector('{SEARCH_CONFIG}', coalesce(title, '')), 'A') || "
            f"setweight(to_tsvector('{SEARCH_CONFIG}', coalesce(description, '')), 'B')",
            persisted=True,
        ),
        nullable=False,
    )

    ingredients: Mapped[list[RecipeIngredient]] = relationship(
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="selectin",
        order_by="RecipeIngredient.position",
    )
    steps: Mapped[list[RecipeStep]] = relationship(
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="selectin",
        order_by="RecipeStep.position",
    )
    tags: Mapped[list[RecipeTag]] = relationship(
        cascade="all, delete-orphan", passive_deletes=True, lazy="selectin"
    )

    __table_args__ = (
        CheckConstraint("length(trim(title)) > 0", name="title_not_blank"),
        CheckConstraint("servings > 0", name="servings_positive"),
        CheckConstraint(
            "prep_minutes IS NULL OR prep_minutes >= 0", name="prep_minutes_not_negative"
        ),
        CheckConstraint(
            "cook_minutes IS NULL OR cook_minutes >= 0", name="cook_minutes_not_negative"
        ),
        Index("ix_recipes_search_vector", "search_vector", postgresql_using="gin"),
        Index("ix_recipes_owner_id", "owner_id"),
        Index("ix_recipes_title", "title"),
    )
