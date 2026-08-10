"""Recipe operations (SPEC §4.6).

Search goes through `scoping.visible()` like every other read, for the reason
spelled out in the notes service: a search that queries the index directly is
the shortest path to handing someone a private record.

The interesting piece here is `resolve_ingredient`. Ingredients are a shared,
case-insensitively unique vocabulary, but a cook types free text. Every typed
name is matched against that vocabulary and only creates a row when genuinely
new — which is what keeps "Plain Flour" and "plain flour" from becoming two
things the shopping list can never combine.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import Select, delete, func, select
from sqlalchemy.orm import Session as DbSession

from home_ops.modules.kitchen import storage
from home_ops.modules.kitchen.models import (
    MAX_TAG_LENGTH,
    SEARCH_CONFIG,
    Ingredient,
    Recipe,
    RecipeIngredient,
    RecipeStep,
    RecipeTag,
)
from home_ops.modules.kitchen.schemas import RecipeIngredientIn
from home_ops.policy import Principal, Visibility
from home_ops.scoping import SCOPED_OPTION, visible


class RecipeNotVisible(LookupError):
    """The recipe does not exist, or the caller may not see it — one error for both."""


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def normalize_tag(tag: str) -> str:
    return tag.strip().lower()[:MAX_TAG_LENGTH]


def normalize_tags(tags: list[str]) -> list[str]:
    cleaned = [normalize_tag(tag) for tag in tags]
    return [tag for tag in dict.fromkeys(cleaned) if tag]


# --- the ingredient vocabulary ------------------------------------------------


def resolve_ingredient(db: DbSession, name: str) -> Ingredient:
    """Find or create the shared ingredient row for a typed name.

    Matched on `lower(name)` to agree with the unique index, so the lookup and
    the constraint can never disagree about whether something already exists.
    """
    cleaned = " ".join(name.split())
    existing = db.scalar(select(Ingredient).where(func.lower(Ingredient.name) == cleaned.lower()))
    if existing is not None:
        return existing

    created = Ingredient(name=cleaned)
    db.add(created)
    db.flush()
    return created


def list_ingredients(
    db: DbSession, *, search: str | None = None, limit: int = 25
) -> list[Ingredient]:
    """Autocomplete for the editor.

    Household vocabulary, so it is not visibility-scoped — the same call the
    task-category and calendar lists make. An ingredient name discloses nothing
    about whose recipe uses it.
    """
    stmt = select(Ingredient).order_by(Ingredient.name).limit(limit)
    query = (search or "").strip()
    if query:
        stmt = stmt.where(Ingredient.name.ilike(f"%{query}%"))
    return list(db.scalars(stmt))


# --- reads --------------------------------------------------------------------


def visible_recipes(principal: Principal) -> Select[Any]:
    return visible(select(Recipe), Recipe, principal)


def list_recipes(
    db: DbSession,
    principal: Principal,
    *,
    search: str | None = None,
    tag: str | None = None,
    author_id: UUID | None = None,
) -> list[Recipe]:
    stmt = visible_recipes(principal)

    if author_id is not None:
        stmt = stmt.where(Recipe.owner_id == author_id)

    if tag:
        stmt = stmt.where(
            select(RecipeTag.recipe_id)
            .where(RecipeTag.recipe_id == Recipe.id, RecipeTag.tag == normalize_tag(tag))
            .exists()
        )

    query = (search or "").strip()
    if query:
        ts_query = func.websearch_to_tsquery(SEARCH_CONFIG, query)
        # Title matches also come back for a partial word the tsvector would
        # miss — "chick" should find "chicken" while somebody is still typing.
        stmt = stmt.where(
            Recipe.search_vector.op("@@")(ts_query) | Recipe.title.ilike(f"%{query}%")
        ).order_by(func.ts_rank_cd(Recipe.search_vector, ts_query).desc(), Recipe.title)
    else:
        stmt = stmt.order_by(Recipe.title)

    return list(db.scalars(stmt))


def get_recipe(db: DbSession, principal: Principal, recipe_id: UUID) -> Recipe:
    recipe: Recipe | None = db.scalar(visible_recipes(principal).where(Recipe.id == recipe_id))
    if recipe is None:
        raise RecipeNotVisible(str(recipe_id))
    return recipe


def known_tags(db: DbSession, principal: Principal) -> list[str]:
    """Tags in use, restricted to recipes the caller can see.

    Same reasoning as the notes board: listing every tag in the household would
    disclose the subject of records that are otherwise hidden.
    """
    visible_ids = visible_recipes(principal).with_only_columns(Recipe.id).subquery()
    stmt = (
        select(RecipeTag.tag)
        .where(RecipeTag.recipe_id.in_(select(visible_ids.c.id)))
        .distinct()
        .order_by(RecipeTag.tag)
        # See the identical note in the notes service: `.subquery()` drops the
        # statement-level execution option, so the guard cannot see that the
        # scoping it is looking for is right there in the subquery.
        .execution_options(**{SCOPED_OPTION: True})
    )
    return list(db.scalars(stmt))


# --- writes -------------------------------------------------------------------


def set_tags(db: DbSession, recipe: Recipe, tags: list[str]) -> None:
    db.execute(delete(RecipeTag).where(RecipeTag.recipe_id == recipe.id))
    for tag in normalize_tags(tags):
        db.add(RecipeTag(recipe_id=recipe.id, tag=tag))
    db.flush()
    # A bulk DELETE does not reach into an already-loaded collection, and the
    # session does not expire on commit, so without this the response would be
    # built from the list as it was before the replacement.
    db.expire(recipe, ["tags"])


def set_ingredients(db: DbSession, recipe: Recipe, rows: list[RecipeIngredientIn]) -> None:
    """Replace the ingredient list wholesale, renumbering positions from zero."""
    db.execute(delete(RecipeIngredient).where(RecipeIngredient.recipe_id == recipe.id))
    for position, row in enumerate(rows):
        ingredient = resolve_ingredient(db, row.name)
        db.add(
            RecipeIngredient(
                recipe_id=recipe.id,
                ingredient_id=ingredient.id,
                position=position,
                quantity=row.quantity,
                unit=row.unit,
                note=row.note,
            )
        )
    db.flush()
    db.expire(recipe, ["ingredients"])


def set_steps(db: DbSession, recipe: Recipe, bodies: list[str]) -> None:
    db.execute(delete(RecipeStep).where(RecipeStep.recipe_id == recipe.id))
    for position, body in enumerate(bodies):
        db.add(RecipeStep(recipe_id=recipe.id, position=position, text_body=body))
    db.flush()
    db.expire(recipe, ["steps"])


def create_recipe(
    db: DbSession,
    principal: Principal,
    *,
    title: str,
    description: str = "",
    servings: int = 4,
    prep_minutes: int | None = None,
    cook_minutes: int | None = None,
    source_url: str | None = None,
    visibility: Visibility = Visibility.HOUSEHOLD,
    tags: list[str] | None = None,
    ingredients: list[RecipeIngredientIn] | None = None,
    steps: list[str] | None = None,
) -> Recipe:
    recipe = Recipe(
        title=title.strip(),
        description=description,
        servings=servings,
        prep_minutes=prep_minutes,
        cook_minutes=cook_minutes,
        source_url=source_url,
        owner_id=principal.id,
        visibility=visibility.value,
    )
    db.add(recipe)
    db.flush()

    if tags:
        set_tags(db, recipe, tags)
    if ingredients:
        set_ingredients(db, recipe, ingredients)
    if steps:
        set_steps(db, recipe, steps)

    return recipe


def delete_recipe(db: DbSession, recipe: Recipe) -> None:
    """Remove the recipe and, if it had one, its image files.

    The files go first and the row second. The other order can leave a row
    pointing at nothing if the delete fails halfway; this order can leave files
    with no row, which `orphaned_image_keys` finds and nothing depends on.
    """
    if recipe.image_key:
        storage.delete(recipe.image_key)
    db.delete(recipe)
    db.flush()


def set_image(db: DbSession, recipe: Recipe, full: bytes, thumb: bytes) -> str:
    """Store a rendered image against a recipe, replacing any previous one."""
    previous = recipe.image_key
    key = storage.new_key()
    storage.write(key, full, thumb)
    recipe.image_key = key
    db.flush()

    # Only after the new one is safely written and recorded.
    if previous:
        storage.delete(previous)
    return key


def clear_image(db: DbSession, recipe: Recipe) -> None:
    if recipe.image_key:
        storage.delete(recipe.image_key)
        recipe.image_key = None
        db.flush()


def image_keys_in_use(db: DbSession) -> set[str]:
    """Every key a recipe row still points at.

    Not visibility-scoped, and deliberately: this answers "which files are
    referenced by anything at all", which is a housekeeping question about the
    filesystem, not a question about whose recipes exist. It returns keys only —
    no titles, no owners, nothing that could disclose a private recipe.
    """
    stmt = (
        select(Recipe.image_key)
        .where(Recipe.image_key.is_not(None))
        .execution_options(**{SCOPED_OPTION: True})
    )
    return {key for key in db.scalars(stmt) if key}


# --- importing a Mealie library (SPEC §4.6) -----------------------------------


class ConflictMode(StrEnum):
    """What to do with a recipe whose title a household recipe already uses."""

    SKIP = "skip"
    REPLACE = "replace"


@dataclass
class ImportOutcome:
    imported: int = 0
    replaced: int = 0
    skipped_existing: int = 0
    skipped_unreadable: int = 0
    with_images: int = 0
    #: Titles that clash, so the preview can name them rather than just count.
    conflicts: list[str] = field(default_factory=list)


def existing_titles(db: DbSession, principal: Principal, titles: list[str]) -> dict[str, Recipe]:
    """Recipes the caller can see whose titles match, keyed by lowercased title.

    Scoped like every other read. That has a consequence worth stating: a clash
    with somebody else's *private* recipe is invisible here, so the import
    creates a second recipe of the same name. That is the right outcome —
    reporting the clash would disclose that the private one exists, and §4.2 is
    clear that nothing bypasses record visibility, not even a convenience.
    """
    if not titles:
        return {}
    wanted = {title.strip().lower() for title in titles if title.strip()}
    stmt = visible_recipes(principal).where(func.lower(Recipe.title).in_(wanted))
    return {recipe.title.strip().lower(): recipe for recipe in db.scalars(stmt)}


def import_mealie(
    db: DbSession,
    principal: Principal,
    archive: Any,
    *,
    on_conflict: ConflictMode = ConflictMode.SKIP,
    dry_run: bool = False,
    render_image: Any = None,
) -> ImportOutcome:
    """Import a parsed Mealie archive.

    The caller commits. Everything below happens in one transaction, so a
    failure halfway through imports nothing rather than leaving a library half
    migrated — which is much worse than importing nothing, because you cannot
    tell by looking which half arrived.

    `dry_run` walks the same path and reports what *would* happen without
    writing, which is what the preview shows. It deliberately shares this code
    rather than re-deriving the counts: a preview that disagrees with the import
    is worse than no preview.
    """
    outcome = ImportOutcome(skipped_unreadable=archive.skipped)
    existing = existing_titles(db, principal, [recipe.title for recipe in archive.recipes])

    for incoming in archive.recipes:
        clash = existing.get(incoming.title.strip().lower())
        if clash is not None:
            outcome.conflicts.append(incoming.title)
            if on_conflict is ConflictMode.SKIP:
                outcome.skipped_existing += 1
                continue

        if dry_run:
            if clash is not None:
                outcome.replaced += 1
            else:
                outcome.imported += 1
            if incoming.image:
                outcome.with_images += 1
            continue

        if clash is not None:
            # Replace means replace: the old recipe goes, taking its image files
            # with it, rather than being edited into something half-old.
            delete_recipe(db, clash)
            outcome.replaced += 1
        else:
            outcome.imported += 1

        recipe = create_recipe(
            db,
            principal,
            title=incoming.title,
            description=incoming.description,
            servings=incoming.servings or 4,
            prep_minutes=incoming.prep_minutes,
            cook_minutes=incoming.cook_minutes,
            source_url=incoming.source_url,
            tags=incoming.tags,
            ingredients=[
                RecipeIngredientIn(
                    name=row.name,
                    quantity=row.quantity,
                    unit=row.unit,
                    note=row.note,
                )
                for row in incoming.ingredients
                if row.name
            ],
            steps=incoming.steps,
        )

        if incoming.image and render_image is not None:
            # Through the ordinary pipeline: an "image" in somebody else's
            # archive gets exactly the same scrutiny as one somebody uploads.
            # A picture that will not decode loses the picture, not the recipe.
            try:
                rendered = render_image(incoming.image)
            except ValueError:
                rendered = None
            if rendered is not None:
                set_image(db, recipe, rendered.full, rendered.thumb)
                outcome.with_images += 1

    return outcome
