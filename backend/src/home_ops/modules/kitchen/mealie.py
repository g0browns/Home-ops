"""Reading a Mealie export (SPEC §4.6).

§4.6 says to prefer Mealie's export over its database, "the export is a stable
contract, the schema is not", and the approach was settled with the owner on
2026-07-30: **a ZIP uploaded through the UI**. No credentials stored, no Mealie
instance that has to stay reachable, and it works from a backup of a Mealie
already switched off.

**A ZIP from another application is hostile input**, whatever its provenance —
it may have travelled through a backup, a cloud drive and a USB stick before it
got here. Two classic attacks, both closed below:

* **Zip slip.** An entry named `../../etc/cron.d/x` makes naive extraction write
  outside the target directory. Nothing here extracts to disk at all: entries
  are read into memory by name, and names are validated before that. Images go
  through `storage.py`, which generates its own filenames.
* **Zip bombs.** A few hundred kilobytes can decompress to gigabytes. Entries
  are refused on declared size, on *actual* size while reading, and on
  compression ratio — the declared size in a zip header is attacker-controlled,
  so checking only that would be checking the label on the tin.

Everything here is pure: bytes in, parsed recipes out. No database, no
filesystem, so every rule above is testable against a zip built in a test.

**Mealie's shape is read tolerantly on purpose.** Its exports have changed
between versions and this project cannot pin itself to one: rather than assert a
layout, it walks every JSON in the archive and treats anything carrying a name
and an ingredient list as a recipe. A file it does not recognise is skipped and
counted, not fatal.

**Two archive shapes, and they are not the same thing.**

*A recipe export* — Mealie's "export recipes" — gives one JSON per recipe. That
is the stable contract §4.6 asks us to prefer, and `_from_documents` reads it.

*A backup* gives `database.json`: the whole database, table by table, joined by
id. `_from_backup` reads that, and it is worth being honest that doing so is
exactly what §4.6 cautions against — "the export is a stable contract, the schema
is not". It is supported because a backup is what people actually have, and
because refusing the file somebody is holding is not a design principle. The
mitigation is that every field is looked up by name with a fallback, an
unrecognised table is ignored rather than fatal, and a Mealie version that
renames something loses that field rather than failing the import.

**A backup carries far more than recipes**, including `data/.secret`,
`data/.session_secret`, a users table with password hashes, and API tokens. This
module reads *only* the recipe tables, by name, and constructs only
`MealieRecipe` objects — nothing else can leave it. `test_mealie_import.py`
asserts that with a backup carrying all of them.
"""

from __future__ import annotations

import json
import zipfile
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from io import BytesIO
from posixpath import dirname, normpath
from typing import Any, Final

from home_ops.modules.kitchen.ingredient_text import ParsedIngredient, parse_ingredient
from home_ops.modules.kitchen.units import is_unit

#: Total decompressed bytes we will read out of one archive.
MAX_TOTAL_BYTES: Final[int] = 250 * 1024 * 1024
#: Largest single entry. A recipe JSON is kilobytes; a photo is under a few MB.
MAX_ENTRY_BYTES: Final[int] = 20 * 1024 * 1024
#: Refuse an entry that expands by more than this. Text compresses ~5-10x;
#: 200x is not a recipe.
MAX_RATIO: Final[int] = 200
MAX_ENTRIES: Final[int] = 20_000
MAX_RECIPES: Final[int] = 5_000

IMAGE_SUFFIXES: Final[tuple[str, ...]] = (".webp", ".jpg", ".jpeg", ".png")

#: Mealie's own thumbnails. Importing them would store a tiny image as the
#: recipe's picture when a full-size one is sitting beside it.
THUMBNAIL_MARKERS: Final[tuple[str, ...]] = ("min-original", "tiny-original", "thumb")


class BadArchive(ValueError):
    """The upload is not a readable, safe ZIP. The message reaches the user."""


@dataclass
class MealieIngredient:
    name: str
    quantity: Decimal | None = None
    unit: str | None = None
    note: str | None = None
    confident: bool = True


@dataclass
class MealieRecipe:
    title: str
    description: str = ""
    servings: int | None = None
    prep_minutes: int | None = None
    cook_minutes: int | None = None
    source_url: str | None = None
    tags: list[str] = field(default_factory=list)
    ingredients: list[MealieIngredient] = field(default_factory=list)
    steps: list[str] = field(default_factory=list)
    #: Raw bytes of the recipe's picture, if the archive carried one. Rendered
    #: by the ordinary image pipeline, never stored as they arrived.
    image: bytes | None = None


@dataclass
class MealieArchive:
    recipes: list[MealieRecipe]
    #: JSON files that did not look like recipes. Reported rather than hidden,
    #: so "84 of 90" prompts a question instead of passing unnoticed.
    skipped: int = 0


# --- safety -------------------------------------------------------------------


def _safe_name(name: str) -> bool:
    """Is this entry name one we are willing to look at?

    Nothing is extracted to disk, so this is defence in depth rather than the
    only thing standing between the archive and the filesystem. It stays because
    a name like `../../x` in an archive is a statement of intent, and an archive
    that contains one should be refused rather than partially read.
    """
    if not name or name.endswith("/"):
        return False
    if name.startswith(("/", "\\")) or ":" in name[:3]:
        return False
    if "\\" in name:
        return False
    normalised = normpath(name)
    return not (normalised.startswith("..") or normalised.startswith("/"))


def _read_entry(archive: zipfile.ZipFile, info: zipfile.ZipInfo, budget: list[int]) -> bytes:
    """Read one entry, refusing it on size or ratio.

    `info.file_size` is a number the archive claims about itself, so it is
    checked *and* the read is capped independently. Reading one byte past the
    cap is what distinguishes a truthful header from a lying one.
    """
    if info.file_size > MAX_ENTRY_BYTES:
        raise BadArchive("That archive contains a file far larger than a recipe or a photo.")

    with archive.open(info) as handle:
        data = handle.read(MAX_ENTRY_BYTES + 1)

    if len(data) > MAX_ENTRY_BYTES:
        raise BadArchive("That archive contains a file far larger than a recipe or a photo.")
    if info.compress_size > 0 and len(data) / info.compress_size > MAX_RATIO:
        raise BadArchive("That archive expands to far more than it claims. Refusing to read it.")

    budget[0] -= len(data)
    if budget[0] < 0:
        raise BadArchive("That archive is too large to import.")
    return data


# --- reading Mealie's shapes --------------------------------------------------


def _text(value: Any, limit: int = 10_000) -> str:
    if isinstance(value, str):
        return " ".join(value.split())[:limit]
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, dict):
        return _text(value.get("name") or value.get("text") or value.get("title"), limit)
    return ""


def _pick(node: dict[str, Any], *keys: str) -> Any:
    """First present key. Mealie mixes camelCase and snake_case across versions."""
    for key in keys:
        if key in node and node[key] not in (None, ""):
            return node[key]
    return None


def _minutes(value: Any) -> int | None:
    from home_ops.modules.kitchen.scrape import parse_duration

    return parse_duration(_text(value) or None)


def _servings(value: Any) -> int | None:
    from home_ops.modules.kitchen.scrape import _first_int

    return _first_int(value)


def _quantity(value: Any) -> Decimal | None:
    if value in (None, "", 0):
        return None
    try:
        quantity = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return quantity if quantity > 0 else None


def _ingredient(node: Any) -> MealieIngredient | None:
    """One ingredient, from either shape Mealie writes.

    Structured (`food`, `unit`, `quantity`) when the user enabled its ingredient
    parser, and a single `display`/`note` string when they did not. The second
    goes through this project's own parser, so an unparsed Mealie library still
    arrives as structured rows rather than as text.
    """
    if isinstance(node, str):
        return _from_free_text(node)
    if not isinstance(node, dict):
        return None

    # A section heading in Mealie's ingredient list, not an ingredient.
    if node.get("disableAmount") is None and not any(
        key in node for key in ("food", "note", "display", "title", "quantity", "unit")
    ):
        return None

    food = _text(_pick(node, "food", "ingredient"))
    if not food:
        # No structured food: fall back to whatever text is there.
        text = _text(_pick(node, "display", "note", "originalText", "original_text", "title"))
        return _from_free_text(text) if text else None

    unit = _text(_pick(node, "unit"))
    unit_key = _unit_key(unit)
    note = _text(_pick(node, "note")) or None
    if unit and unit_key is None:
        # Mealie allows arbitrary unit names. One we do not have a key for is
        # kept as a note rather than dropped, so the cook can see it.
        note = ", ".join(part for part in (unit, note) if part) or None

    return MealieIngredient(
        name=food,
        quantity=_quantity(_pick(node, "quantity")),
        unit=unit_key,
        note=note,
        confident=True,
    )


def _from_free_text(text: str) -> MealieIngredient | None:
    cleaned = _text(text)
    if not cleaned:
        return None
    parsed: ParsedIngredient = parse_ingredient(cleaned)
    return MealieIngredient(
        name=parsed.name,
        quantity=parsed.quantity,
        unit=parsed.unit,
        note=parsed.note,
        confident=parsed.confident,
    )


def _unit_key(unit: str) -> str | None:
    """Mealie's unit name to one of ours, or None."""
    from home_ops.modules.kitchen.ingredient_text import UNIT_WORDS

    cleaned = unit.strip().strip(".").lower()
    if not cleaned:
        return None
    if is_unit(cleaned):
        return cleaned
    return UNIT_WORDS.get(cleaned)


def _steps(value: Any) -> list[str]:
    steps: list[str] = []
    for item in value if isinstance(value, list) else [value]:
        text = _text(item) if not isinstance(item, dict) else _text(_pick(item, "text", "title"))
        if text:
            steps.append(text)
    return steps[:200]


def _tags(node: dict[str, Any]) -> list[str]:
    found: list[str] = []
    for key in ("tags", "recipeCategory", "recipe_category", "categories"):
        for item in node.get(key) or []:
            text = _text(item)
            if text:
                found.append(text.lower()[:32])
    # dict.fromkeys de-duplicates while keeping order.
    return list(dict.fromkeys(found))[:20]


#: Tables read out of a backup's database.json. Everything else in that file —
#: users, tokens, notifiers, the group's settings — is deliberately never
#: touched. An import brings recipes across, not an identity system.
BACKUP_TABLES: Final[tuple[str, ...]] = (
    "recipes",
    "recipe_instructions",
    "recipes_ingredients",
    "ingredient_units",
    "ingredient_foods",
    "tags",
    "recipes_to_tags",
    "categories",
    "recipes_to_categories",
)


def looks_like_backup(node: Any) -> bool:
    return isinstance(node, dict) and isinstance(node.get("recipes"), list)


def _rows(node: dict[str, Any], table: str) -> list[dict[str, Any]]:
    value = node.get(table)
    return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []


def _by_id(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(row["id"]): row for row in rows if row.get("id") is not None}


def _group(rows: list[dict[str, Any]], key: str) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        recipe_id = row.get(key)
        if recipe_id is not None:
            grouped.setdefault(str(recipe_id), []).append(row)
    return grouped


def _from_backup(node: dict[str, Any]) -> list[tuple[str, MealieRecipe]]:
    """Reassemble recipes from a backup's relational tables.

    Returns `(mealie_recipe_id, recipe)` so images — which a backup files under
    `data/recipes/<id>/` — can be matched by id rather than by guessing at the
    directory layout.
    """
    units = _by_id(_rows(node, "ingredient_units"))
    foods = _by_id(_rows(node, "ingredient_foods"))
    tags = _by_id(_rows(node, "tags"))
    categories = _by_id(_rows(node, "categories"))

    instructions = _group(_rows(node, "recipe_instructions"), "recipe_id")
    ingredients = _group(_rows(node, "recipes_ingredients"), "recipe_id")
    tag_links = _group(_rows(node, "recipes_to_tags"), "recipe_id")
    category_links = _group(_rows(node, "recipes_to_categories"), "recipe_id")

    built: list[tuple[str, MealieRecipe]] = []
    for row in _rows(node, "recipes"):
        title = _text(_pick(row, "name"), 200)
        if not title:
            continue
        recipe_id = str(row.get("id") or "")

        labels: list[str] = []
        for link in tag_links.get(recipe_id, []):
            tag = tags.get(str(link.get("tag_id")))
            if tag:
                labels.append(_text(tag.get("name")).lower()[:32])
        for link in category_links.get(recipe_id, []):
            category = categories.get(str(link.get("category_id")))
            if category:
                labels.append(_text(category.get("name")).lower()[:32])

        steps = [
            _text(step.get("text") or step.get("title"))
            for step in sorted(
                instructions.get(recipe_id, []), key=lambda s: s.get("position") or 0
            )
        ]

        rows: list[MealieIngredient] = []
        for line in sorted(ingredients.get(recipe_id, []), key=lambda i: i.get("position") or 0):
            parsed = _backup_ingredient(line, units, foods)
            if parsed is not None:
                rows.append(parsed)

        built.append(
            (
                recipe_id,
                MealieRecipe(
                    title=title,
                    description=_text(_pick(row, "description")),
                    # recipe_servings is a number; recipe_yield is prose like
                    # "Serves 4". Prefer the number, fall back to reading the prose.
                    servings=_servings(_pick(row, "recipe_servings", "recipe_yield")),
                    prep_minutes=_minutes(_pick(row, "prep_time")),
                    cook_minutes=_minutes(_pick(row, "perform_time", "cook_time")),
                    source_url=_source_url(_pick(row, "org_url")),
                    tags=list(dict.fromkeys(label for label in labels if label))[:20],
                    ingredients=rows[:200],
                    steps=[step for step in steps if step][:200],
                ),
            )
        )
    return built


def _backup_ingredient(
    line: dict[str, Any],
    units: dict[str, dict[str, Any]],
    foods: dict[str, dict[str, Any]],
) -> MealieIngredient | None:
    """One row of `recipes_ingredients`.

    Mealie stores this two ways depending on whether its ingredient parser was
    switched on. With a `food_id` the row is structured; without one the whole
    line sits in `original_text` or `note`, and goes through this project's own
    parser so the import still lands structured rows.
    """
    food = foods.get(str(line.get("food_id")))
    name = _text(food.get("name")) if food else ""

    if not name:
        text = _text(_pick(line, "original_text", "note", "display"))
        # A row carrying only a `title` is a section heading in Mealie's
        # ingredient list ("For the sauce"), not an ingredient.
        return _from_free_text(text) if text else None

    unit = units.get(str(line.get("unit_id")))
    unit_name = _text(unit.get("name") or unit.get("abbreviation")) if unit else ""
    unit_key = _unit_key(unit_name)
    note = _text(_pick(line, "note")) or None
    if unit_name and unit_key is None:
        note = ", ".join(part for part in (unit_name, note) if part) or None

    return MealieIngredient(
        name=name,
        quantity=_quantity(line.get("quantity")),
        unit=unit_key,
        note=note,
        confident=True,
    )


def looks_like_recipe(node: Any) -> bool:
    if not isinstance(node, dict):
        return False
    has_name = bool(_text(_pick(node, "name", "title")))
    has_parts = any(
        key in node
        for key in (
            "recipeIngredient",
            "recipe_ingredient",
            "recipeInstructions",
            "recipe_instructions",
        )
    )
    return has_name and has_parts


def _recipe(node: dict[str, Any]) -> MealieRecipe:
    ingredients = [
        row
        for row in (
            _ingredient(item)
            for item in (_pick(node, "recipeIngredient", "recipe_ingredient") or [])
        )
        if row is not None
    ]
    return MealieRecipe(
        title=_text(_pick(node, "name", "title"), 200),
        description=_text(_pick(node, "description")),
        servings=_servings(_pick(node, "recipeServings", "recipeYield", "recipe_yield", "yield")),
        prep_minutes=_minutes(_pick(node, "prepTime", "prep_time")),
        # Mealie calls the cooking time performTime; older versions cookTime.
        cook_minutes=_minutes(_pick(node, "performTime", "perform_time", "cookTime", "cook_time")),
        source_url=_source_url(_pick(node, "orgURL", "org_url", "sourceUrl", "source_url")),
        tags=_tags(node),
        ingredients=ingredients[:200],
        steps=_steps(_pick(node, "recipeInstructions", "recipe_instructions") or []),
    )


def _source_url(value: Any) -> str | None:
    url = _text(value, 2000)
    return url if url.lower().startswith(("http://", "https://")) else None


# --- the entry point ----------------------------------------------------------


def read(data: bytes) -> MealieArchive:
    """Parse an uploaded Mealie export.

    Raises `BadArchive`, which the route turns into a 422 carrying the message.
    """
    try:
        archive = zipfile.ZipFile(BytesIO(data))
    except (zipfile.BadZipFile, OSError) as exc:
        raise BadArchive("That file is not a ZIP archive we can read.") from exc

    with archive:
        infos = archive.infolist()
        if len(infos) > MAX_ENTRIES:
            raise BadArchive("That archive contains far too many files.")

        for info in infos:
            if not info.is_dir() and not _safe_name(info.filename):
                # An entry name like `../../etc/passwd` is a statement of intent.
                raise BadArchive("That archive contains an unsafe file path. Refusing to read it.")

        budget = [MAX_TOTAL_BYTES]
        recipes: list[MealieRecipe] = []
        skipped = 0

        # Directory -> the best image in it, so a recipe export can find its
        # picture without assuming Mealie's layout.
        images: dict[str, tuple[str, bytes]] = {}
        # Recipe id -> its picture, for a backup, where images live under
        # `data/recipes/<id>/images/` a long way from database.json.
        by_recipe_id: dict[str, bytes] = {}

        json_entries = [i for i in infos if not i.is_dir() and i.filename.lower().endswith(".json")]
        image_entries = [
            i for i in infos if not i.is_dir() and i.filename.lower().endswith(IMAGE_SUFFIXES)
        ]

        for info in image_entries:
            name = info.filename.lower()
            if any(marker in name for marker in THUMBNAIL_MARKERS):
                continue
            payload = _read_entry(archive, info, budget)
            folder = _recipe_folder(info.filename)
            existing = images.get(folder)
            # Biggest wins: Mealie writes several sizes side by side.
            if existing is None or len(payload) > len(existing[1]):
                images[folder] = (info.filename, payload)

            recipe_id = _recipe_id_from_path(info.filename)
            if recipe_id:
                key = _id_key(recipe_id)
                if len(payload) > len(by_recipe_id.get(key, b"")):
                    by_recipe_id[key] = payload

        for info in json_entries:
            payload = _read_entry(archive, info, budget)
            try:
                parsed = json.loads(payload.decode("utf-8", errors="replace"))
            except (ValueError, RecursionError):
                skipped += 1
                continue

            if looks_like_backup(parsed):
                # A backup: recipes come out of database.json's tables, and its
                # images are filed under data/recipes/<recipe id>/ rather than
                # beside the JSON.
                for mealie_id, recipe in _from_backup(parsed):
                    if len(recipes) >= MAX_RECIPES:
                        raise BadArchive("That archive contains more recipes than we will import.")
                    picture = by_recipe_id.get(_id_key(mealie_id))
                    if picture:
                        recipe.image = picture
                    recipes.append(recipe)
                continue

            nodes = parsed if isinstance(parsed, list) else [parsed]
            found_here = False
            for node in nodes:
                if not looks_like_recipe(node):
                    continue
                if len(recipes) >= MAX_RECIPES:
                    raise BadArchive("That archive contains more recipes than we will import.")
                recipe = _recipe(node)
                if not recipe.title:
                    continue
                # Named apart from the backup branch's `picture`: one holds
                # (name, bytes) and the other bare bytes, and sharing a name
                # gave them one inferred type between them.
                beside = images.get(_recipe_folder(info.filename))
                if beside:
                    recipe.image = beside[1]
                recipes.append(recipe)
                found_here = True
            if not found_here:
                skipped += 1

        if not recipes:
            raise BadArchive(
                "No recipes found in that archive. Export from Mealie with "
                "'Recipes' included and upload the .zip it gives you."
            )

        return MealieArchive(recipes=recipes, skipped=skipped)


def _id_key(value: str) -> str:
    """A recipe id in a form both halves of a backup agree on.

    `database.json` stores ids unhyphenated — `e3ed6be0205d4d728b791707f66be64a`
    — while the image directories use the canonical hyphenated form of the same
    UUID. Comparing them as written matches nothing at all, which is how an
    import of 55 recipes arrives with 55 pictures left behind and no error to
    say so. Real data found this; a fixture with matching ids never would.
    """
    return value.replace("-", "").strip().lower()


def _recipe_id_from_path(path: str) -> str | None:
    """The recipe id out of `data/recipes/<id>/images/original.webp`.

    A backup keys its images by id, so this is how a picture finds its recipe
    when the JSON that describes it is a single file at the root.
    """
    parts = [part for part in path.split("/") if part]
    for index, part in enumerate(parts[:-1]):
        if part.lower() == "recipes" and index + 1 < len(parts):
            return parts[index + 1]
    return None


def _recipe_folder(path: str) -> str:
    """The directory a recipe's files share.

    Mealie nests images one level deeper (`<slug>/images/original.webp`) than the
    JSON (`<slug>/recipe.json`), so an `images/` tail is stripped to make the two
    meet.
    """
    folder = dirname(path)
    if folder.rsplit("/", 1)[-1].lower() in ("images", "image", "assets"):
        folder = dirname(folder)
    return folder
