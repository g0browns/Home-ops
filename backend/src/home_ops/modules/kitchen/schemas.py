"""Request and response shapes for recipes (SPEC §4.6)."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Annotated
from uuid import UUID

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from home_ops.modules.kitchen.models import MAX_INGREDIENT_NAME, MAX_TAG_LENGTH
from home_ops.modules.kitchen.plan_models import MealSlot
from home_ops.modules.kitchen.units import is_unit
from home_ops.policy import Visibility

Stripped = BeforeValidator(lambda value: value.strip() if isinstance(value, str) else value)

Title = Annotated[str, Stripped, Field(min_length=1, max_length=200)]
# NOTE on optional fields below: a length constraint has to sit on the `str`
# side of the union, not on `str | None`. Written as
# `Annotated[str | None, Field(max_length=...)]` pydantic tries to measure the
# length of None and raises a TypeError — a 500, not a 422 — the moment a client
# sends an explicit `null` rather than omitting the field. Omitting it never
# runs the validator, which is why this survived a suite that only ever left
# these out.
IngredientName = Annotated[str, Stripped, Field(min_length=1, max_length=MAX_INGREDIENT_NAME)]
Description = Annotated[str, Stripped, Field(max_length=10_000)]
SourceUrl = Annotated[str, Stripped, Field(max_length=2000)]
IngredientNote = Annotated[str, Stripped, Field(max_length=200)]
StepBody = Annotated[str, Stripped, Field(min_length=1, max_length=4000)]
Tag = Annotated[str, Stripped, Field(min_length=1, max_length=MAX_TAG_LENGTH)]


def _known_unit(value: str | None) -> str | None:
    """Units are keys from units.py, not free text.

    Validated here rather than only in the database so the caller gets a 422
    naming the field. A unit outside the vocabulary would make §4.6's shopping
    list aggregation silently incapable of combining that row with anything.
    """
    if value is None or value == "":
        return None
    if not is_unit(value):
        raise ValueError(f"{value!r} is not a unit we know.")
    return value


UnitKey = Annotated[str | None, BeforeValidator(_known_unit), Field(default=None)]


class IngredientOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    aisle: str | None


class RecipeIngredientIn(BaseModel):
    #: The ingredient by name. Resolved to a shared row, created if new — a cook
    #: typing an ingredient list should not have to manage a vocabulary first.
    name: IngredientName
    quantity: Annotated[Decimal, Field(gt=0, le=Decimal("100000"))] | None = None
    unit: UnitKey = None
    note: IngredientNote | None = None


class RecipeIngredientOut(BaseModel):
    id: UUID
    ingredient_id: UUID
    name: str
    aisle: str | None
    quantity: Decimal | None
    unit: str | None
    note: str | None
    position: int


class RecipeStepOut(BaseModel):
    id: UUID
    position: int
    body: str


class RecipeSummary(BaseModel):
    """What a list needs. Deliberately without steps or ingredients."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    title: str
    description: str
    servings: int
    prep_minutes: int | None
    cook_minutes: int | None
    image_key: str | None
    owner_id: UUID
    visibility: Visibility
    tags: list[str] = Field(default_factory=list)


class RecipeDetail(RecipeSummary):
    source_url: str | None
    ingredients: list[RecipeIngredientOut] = Field(default_factory=list)
    steps: list[RecipeStepOut] = Field(default_factory=list)


class RecipeCreate(BaseModel):
    title: Title
    description: Description = ""
    servings: int = Field(default=4, ge=1, le=1000)
    prep_minutes: Annotated[int, Field(ge=0, le=100_000)] | None = None
    cook_minutes: Annotated[int, Field(ge=0, le=100_000)] | None = None
    source_url: SourceUrl | None = None
    visibility: Visibility = Visibility.HOUSEHOLD
    tags: Annotated[list[Tag], Field(max_length=20)] = Field(default_factory=list)
    ingredients: Annotated[list[RecipeIngredientIn], Field(max_length=200)] = Field(
        default_factory=list
    )
    steps: Annotated[list[StepBody], Field(max_length=200)] = Field(default_factory=list)

    @field_validator("source_url")
    @classmethod
    def _http_only(cls, value: str | None) -> str | None:
        # A recipe's source is a link somebody will click. `javascript:` and
        # `data:` are not sources, they are payloads.
        if value and not value.startswith(("http://", "https://")):
            raise ValueError("A source URL must start with http:// or https://")
        return value


class RecipeUpdate(BaseModel):
    """Every field optional; only what is supplied changes.

    `ingredients` and `steps` are wholesale replacements when present, because a
    per-row patch protocol for an ordered list is far more surface than a
    household recipe editor needs. Absent means "leave them alone", which is why
    the route reads `exclude_unset` rather than these defaults.
    """

    title: Title | None = None
    description: Description | None = None
    servings: Annotated[int, Field(ge=1, le=1000)] | None = None
    prep_minutes: Annotated[int, Field(ge=0, le=100_000)] | None = None
    cook_minutes: Annotated[int, Field(ge=0, le=100_000)] | None = None
    source_url: SourceUrl | None = None
    visibility: Visibility | None = None
    tags: Annotated[list[Tag], Field(max_length=20)] | None = None
    ingredients: Annotated[list[RecipeIngredientIn], Field(max_length=200)] | None = None
    steps: Annotated[list[StepBody], Field(max_length=200)] | None = None

    @field_validator("source_url")
    @classmethod
    def _http_only(cls, value: str | None) -> str | None:
        if value and not value.startswith(("http://", "https://")):
            raise ValueError("A source URL must start with http:// or https://")
        return value


class UnitOut(BaseModel):
    key: str
    singular: str
    plural: str
    dimension: str


# --- import (SPEC §4.6) -------------------------------------------------------


class ImportRequest(BaseModel):
    url: Annotated[str, Stripped, Field(min_length=1, max_length=2000)]


class ImportedIngredient(BaseModel):
    """One parsed line, alongside the text it came from.

    `raw` travels with it deliberately. §4.6 requires the cook to correct the
    parse before saving, and correcting it is guesswork unless the original line
    is on screen next to the fields.
    """

    raw: str
    name: str
    quantity: Decimal | None
    unit: str | None
    note: str | None
    #: False when nothing measurable was found, so the UI can point at the rows
    #: worth a second look rather than making the cook check all of them.
    confident: bool


class ImportedRecipe(BaseModel):
    """A draft. Nothing here has been saved, and none of it is trusted."""

    title: str
    description: str
    servings: int | None
    prep_minutes: int | None
    cook_minutes: int | None
    source_url: str
    tags: list[str]
    ingredients: list[ImportedIngredient]
    steps: list[str]
    #: Which reader produced this — json-ld, microdata — so a bug report names
    #: the path taken.
    extracted_by: str
    #: The page's picture, if it published one. Carried on the draft rather
    #: than fetched with it: downloading somebody else's photograph is a second
    #: request to a second address, and it should wait until the cook has
    #: decided to keep the recipe. It is a public URL off a public page, and the
    #: server re-validates it before fetching regardless of what comes back.
    image_url: str | None = None
    #: Set when the draft is saved: the recipe it became.
    recipe_id: UUID | None = None


class MealieImportResult(BaseModel):
    """What an import did, or what it would do when previewing."""

    #: True when nothing was written — the preview.
    preview: bool
    #: Recipes found in the archive that we could read.
    found: int
    imported: int
    replaced: int
    skipped_existing: int
    #: Files in the archive that were not recipes we recognised. Reported so
    #: "84 of 90" prompts a question rather than passing unnoticed.
    skipped_unreadable: int
    #: Pictures present in the archive. A property of the file, which is what
    #: the "found N recipes, M with pictures" line is describing.
    found_with_images: int = 0
    #: Pictures actually stored. Different from the above whenever recipes are
    #: skipped, and different again when one will not decode — which is why the
    #: two are not one number.
    with_images: int
    #: How many titles already exist. Carried separately from the list below,
    #: which is truncated for display — reading a count off a truncated list is
    #: how "55 already exist" became "50", understating a clash.
    conflict_count: int = 0
    #: The first few of those titles, so the preview can name them rather than
    #: only count them.
    conflicts: list[str] = Field(default_factory=list)


# --- the meal plan (SPEC §4.6, phase 5c) --------------------------------------


class MealPlanEntryOut(BaseModel):
    id: UUID
    plan_date: dt.date
    slot: str
    position: int
    #: Null when the entry is free text, and also when it points at a recipe the
    #: caller may not see — `hidden_recipe` distinguishes those two.
    recipe_id: UUID | None
    #: The recipe's title, or the entry's own text. Never a title the caller is
    #: not entitled to read.
    title: str
    note: str | None
    image_key: str | None
    owner_id: UUID
    #: True when a recipe is planned here that this caller cannot open. The slot
    #: is visibly taken; what is in it is not disclosed.
    hidden_recipe: bool = False


class MealPlanEntryIn(BaseModel):
    plan_date: dt.date
    slot: MealSlot
    recipe_id: UUID | None = None
    title: Annotated[str, Stripped, Field(max_length=200)] | None = None
    note: Annotated[str, Stripped, Field(max_length=200)] | None = None

    @model_validator(mode="after")
    def _names_something(self) -> MealPlanEntryIn:
        # Mirrors the database CHECK, so the caller gets a 422 naming the
        # problem rather than a 500 from a constraint violation.
        if self.recipe_id is None and not (self.title or "").strip():
            raise ValueError("A plan entry needs either a recipe or a name.")
        return self


class MealPlanMove(BaseModel):
    """Where a dragged entry landed."""

    plan_date: dt.date
    slot: MealSlot
    #: Drop it ahead of this entry. Absent means the end of the slot.
    before_id: UUID | None = None
