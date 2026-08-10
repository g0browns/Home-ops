"""Recipe endpoints (SPEC §4.6, phase 5a).

The image endpoints are the ones to read carefully.

**Images are served by this application, not by nginx.** A recipe carries a
visibility, and a file served straight off the volume by the proxy would have
none — a private recipe's photograph would be readable by anyone who guessed or
was given the URL, which is exactly the leak §4.2 exists to prevent. Serving
through here means the same `get_recipe` call, and therefore the same scoping,
guards the picture as guards the text.

The keys are unguessable, but that is a mitigation, not a control. Access
control is a check, not an absence of hints.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, Query, Response, UploadFile, status

from home_ops import audit
from home_ops.audit import AuditAction
from home_ops.config import get_settings
from home_ops.dependencies import AuthDep, ClientIpDep, DbDep, SettingsDep, require
from home_ops.modules.kitchen import (
    images,
    ingredient_text,
    mealie,
    schemas,
    scrape,
    service,
    storage,
    urlfetch,
)
from home_ops.modules.kitchen.models import Recipe
from home_ops.modules.kitchen.units import UNITS
from home_ops.policy import Action, Module, Visibility, can_edit_record

router = APIRouter(prefix="/recipes", tags=["kitchen"])
ingredients_router = APIRouter(prefix="/ingredients", tags=["kitchen"])
units_router = APIRouter(prefix="/kitchen", tags=["kitchen"])

#: How long a browser may keep an image. The key changes whenever the picture
#: does, so a stored copy can never be stale — it can only be for a key nothing
#: points at any more. `private` keeps it out of shared caches, which matters
#: because these are behind a visibility check.
IMAGE_CACHE_CONTROL = "private, max-age=604800"


def _summary(recipe: Recipe) -> schemas.RecipeSummary:
    return schemas.RecipeSummary(
        id=recipe.id,
        title=recipe.title,
        description=recipe.description,
        servings=recipe.servings,
        prep_minutes=recipe.prep_minutes,
        cook_minutes=recipe.cook_minutes,
        image_key=recipe.image_key,
        owner_id=recipe.owner_id,
        visibility=Visibility(recipe.visibility),
        tags=sorted(tag.tag for tag in recipe.tags),
    )


def _detail(recipe: Recipe) -> schemas.RecipeDetail:
    """Built field by field, so a column added to `Recipe` later cannot leak
    through this response by accident. Same reasoning as the notes module."""
    return schemas.RecipeDetail(
        **_summary(recipe).model_dump(),
        source_url=recipe.source_url,
        ingredients=[
            schemas.RecipeIngredientOut(
                id=row.id,
                ingredient_id=row.ingredient_id,
                name=row.ingredient.name,
                aisle=row.ingredient.aisle,
                quantity=row.quantity,
                unit=row.unit,
                note=row.note,
                position=row.position,
            )
            for row in recipe.ingredients
        ],
        steps=[
            schemas.RecipeStepOut(id=step.id, position=step.position, body=step.text_body)
            for step in recipe.steps
        ],
    )


#: The plan routes return a full recipe when one is created from an entry, and
#: building that response twice is how two shapes of the same thing drift apart.
detail_of = _detail


def _load_visible(db: DbDep, auth: AuthDep, recipe_id: UUID) -> Recipe:
    try:
        return service.get_recipe(db, auth.principal, recipe_id)
    except service.RecipeNotVisible as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="No such recipe.") from exc


def _load_editable(db: DbDep, auth: AuthDep, recipe_id: UUID) -> Recipe:
    """404 for what you cannot see, 403 for what you can see but may not change.

    That order matters: a 403 on an invisible recipe would confirm it exists.
    """
    recipe = _load_visible(db, auth, recipe_id)
    if not can_edit_record(
        auth.principal,
        Action.WRITE,
        Module.KITCHEN,
        owner_id=recipe.owner_id,
        visibility=Visibility(recipe.visibility),
        deviations=auth.deviations,
    ):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, detail="Not permitted to change this recipe."
        )
    return recipe


# --- vocabulary ---------------------------------------------------------------


@units_router.get(
    "/units",
    response_model=list[schemas.UnitOut],
    dependencies=[Depends(require(Action.READ, Module.KITCHEN))],
    summary="The measurement vocabulary",
)
def list_units() -> list[schemas.UnitOut]:
    """Served so the frontend list can be checked against this one in a test.

    The UI carries its own copy for offline rendering; `test_units.py` asserts
    they agree, the same arrangement the colour palettes use.
    """
    return [
        schemas.UnitOut(
            key=unit.key, singular=unit.singular, plural=unit.plural, dimension=unit.dimension.value
        )
        for unit in UNITS
    ]


@ingredients_router.get(
    "",
    response_model=list[schemas.IngredientOut],
    dependencies=[Depends(require(Action.READ, Module.KITCHEN))],
    summary="Ingredient autocomplete",
)
def list_ingredients(
    db: DbDep,
    search: Annotated[str | None, Query(max_length=100)] = None,
) -> list[schemas.IngredientOut]:
    rows = service.list_ingredients(db, search=search)
    return [schemas.IngredientOut.model_validate(row) for row in rows]


# --- recipes ------------------------------------------------------------------


@router.get(
    "",
    response_model=list[schemas.RecipeSummary],
    dependencies=[Depends(require(Action.READ, Module.KITCHEN))],
    summary="List or search recipes",
)
def list_recipes(
    db: DbDep,
    auth: AuthDep,
    search: Annotated[str | None, Query(max_length=200)] = None,
    tag: Annotated[str | None, Query(max_length=32)] = None,
    author_id: UUID | None = None,
) -> list[schemas.RecipeSummary]:
    recipes = service.list_recipes(db, auth.principal, search=search, tag=tag, author_id=author_id)
    return [_summary(recipe) for recipe in recipes]


@router.get(
    "/tags",
    response_model=list[str],
    dependencies=[Depends(require(Action.READ, Module.KITCHEN))],
    summary="Tags in use on recipes you can see",
)
def list_tags(db: DbDep, auth: AuthDep) -> list[str]:
    return service.known_tags(db, auth.principal)


@router.get(
    "/{recipe_id}",
    response_model=schemas.RecipeDetail,
    dependencies=[Depends(require(Action.READ, Module.KITCHEN))],
    summary="One recipe, with its ingredients and steps",
)
def get_recipe(recipe_id: UUID, db: DbDep, auth: AuthDep) -> schemas.RecipeDetail:
    return _detail(_load_visible(db, auth, recipe_id))


@router.post(
    "",
    response_model=schemas.RecipeDetail,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require(Action.WRITE, Module.KITCHEN))],
    summary="Add a recipe",
)
def create_recipe(
    payload: schemas.RecipeCreate, db: DbDep, auth: AuthDep, client_ip: ClientIpDep
) -> schemas.RecipeDetail:
    recipe = service.create_recipe(
        db,
        auth.principal,
        title=payload.title,
        description=payload.description,
        servings=payload.servings,
        prep_minutes=payload.prep_minutes,
        cook_minutes=payload.cook_minutes,
        source_url=payload.source_url,
        visibility=payload.visibility,
        tags=payload.tags,
        ingredients=payload.ingredients,
        steps=payload.steps,
    )
    audit.record(
        db,
        AuditAction.RECIPE_CREATED,
        actor_id=auth.user.id,
        actor_label=auth.user.username,
        resource_type="recipe",
        resource_id=str(recipe.id),
        client_ip=client_ip,
        detail={"title": recipe.title, "visibility": recipe.visibility},
    )
    db.commit()
    # Re-read through the scoped path rather than db.refresh(): refresh issues an
    # unscoped SELECT, which the visibility guard rightly rejects. Going back
    # through get_recipe also picks up the rows written above.
    return _detail(service.get_recipe(db, auth.principal, recipe.id))


@router.patch(
    "/{recipe_id}",
    response_model=schemas.RecipeDetail,
    dependencies=[Depends(require(Action.WRITE, Module.KITCHEN))],
    summary="Change a recipe",
)
def update_recipe(
    recipe_id: UUID,
    payload: schemas.RecipeUpdate,
    db: DbDep,
    auth: AuthDep,
    client_ip: ClientIpDep,
) -> schemas.RecipeDetail:
    recipe = _load_editable(db, auth, recipe_id)
    # `exclude_unset` is what separates "leave the ingredients alone" from
    # "replace them with nothing".
    changed = payload.model_dump(exclude_unset=True)

    # Which columns a null may actually clear. Getting this wrong is a 500, not
    # a 422: `description` is NOT NULL with a '' default, so writing None to it
    # violates the constraint at flush time, well past where a validation error
    # would have been useful. Splitting the fields by nullability keeps that
    # decision visible instead of implied by a loop.
    NULLABLE = ("prep_minutes", "cook_minutes", "source_url")
    REQUIRED = ("title", "servings")

    for field in NULLABLE:
        if field in changed:
            setattr(recipe, field, changed[field])

    for field in REQUIRED:
        if field in changed:
            if changed[field] is None:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail=f"{field} cannot be cleared.",
                )
            setattr(recipe, field, changed[field])

    if "description" in changed:
        # A recipe with no description has an empty one, not a null one.
        recipe.description = changed["description"] or ""

    if "visibility" in changed and payload.visibility is not None:
        recipe.visibility = payload.visibility.value

    if payload.tags is not None:
        service.set_tags(db, recipe, payload.tags)
    if payload.ingredients is not None:
        service.set_ingredients(db, recipe, payload.ingredients)
    if payload.steps is not None:
        service.set_steps(db, recipe, payload.steps)

    db.flush()
    audit.record(
        db,
        AuditAction.RECIPE_UPDATED,
        actor_id=auth.user.id,
        actor_label=auth.user.username,
        resource_type="recipe",
        resource_id=str(recipe.id),
        client_ip=client_ip,
        detail={"fields": sorted(changed)},
    )
    db.commit()
    # Re-read through the scoped path rather than db.refresh(): refresh issues an
    # unscoped SELECT, which the visibility guard rightly rejects. Going back
    # through get_recipe also picks up the rows written above.
    return _detail(service.get_recipe(db, auth.principal, recipe.id))


@router.delete(
    "/{recipe_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require(Action.WRITE, Module.KITCHEN))],
    summary="Remove a recipe and its image",
)
def delete_recipe(recipe_id: UUID, db: DbDep, auth: AuthDep, client_ip: ClientIpDep) -> Response:
    recipe = _load_editable(db, auth, recipe_id)
    title = recipe.title
    service.delete_recipe(db, recipe)
    audit.record(
        db,
        AuditAction.RECIPE_DELETED,
        actor_id=auth.user.id,
        actor_label=auth.user.username,
        resource_type="recipe",
        resource_id=str(recipe_id),
        client_ip=client_ip,
        detail={"title": title},
    )
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- images -------------------------------------------------------------------


@router.get(
    "/{recipe_id}/image",
    dependencies=[Depends(require(Action.READ, Module.KITCHEN))],
    summary="A recipe's picture",
    response_class=Response,
)
def get_image(
    recipe_id: UUID,
    db: DbDep,
    auth: AuthDep,
    thumb: bool = False,
) -> Response:
    # The visibility check, which is the reason this is not served statically.
    recipe = _load_visible(db, auth, recipe_id)
    if not recipe.image_key:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="That recipe has no picture.")

    data = storage.read(recipe.image_key, thumb=thumb)
    if data is None:
        # The row points at a file that is not there — a database-only restore
        # is the likely cause. 404 rather than 500: the recipe is fine, the
        # picture is missing.
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="That picture is missing.")

    return Response(
        content=data,
        media_type=images.OUTPUT_MEDIA_TYPE,
        headers={
            "Cache-Control": IMAGE_CACHE_CONTROL,
            # Everything served here was re-encoded by us and is a WebP. Saying
            # so, and forbidding sniffing, closes the gap where a browser
            # decides some other content type looks more plausible.
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": "inline",
        },
    )


@router.put(
    "/{recipe_id}/image",
    response_model=schemas.RecipeDetail,
    dependencies=[Depends(require(Action.WRITE, Module.KITCHEN))],
    summary="Set a recipe's picture",
)
async def put_image(
    recipe_id: UUID,
    db: DbDep,
    auth: AuthDep,
    client_ip: ClientIpDep,
    settings: SettingsDep,
    file: Annotated[UploadFile, File()],
) -> schemas.RecipeDetail:
    recipe = _load_editable(db, auth, recipe_id)
    # Through the dependency, not the process-wide cache: every other route
    # resolves settings this way, and it is what lets a test configure one.
    limit = settings.upload_max_bytes

    # Read one byte past the limit so an oversized upload is rejected on size
    # rather than after a full read into memory.
    data = await file.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(
            status.HTTP_413_CONTENT_TOO_LARGE,
            detail=f"That image is larger than {limit // (1024 * 1024)} MB.",
        )
    if not data:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail="That file is empty.")

    try:
        rendered = images.render(data)
    except (images.NotAnImage, images.ImageTooLarge) as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    service.set_image(db, recipe, rendered.full, rendered.thumb)
    audit.record(
        db,
        AuditAction.RECIPE_UPDATED,
        actor_id=auth.user.id,
        actor_label=auth.user.username,
        resource_type="recipe",
        resource_id=str(recipe.id),
        client_ip=client_ip,
        # No filename: it is attacker-controlled text that would land in a log
        # somebody reads. The dimensions are what is actually useful.
        detail={"image": f"{rendered.width}x{rendered.height}"},
    )
    db.commit()
    # Re-read through the scoped path rather than db.refresh(): refresh issues an
    # unscoped SELECT, which the visibility guard rightly rejects. Going back
    # through get_recipe also picks up the rows written above.
    return _detail(service.get_recipe(db, auth.principal, recipe.id))


@router.delete(
    "/{recipe_id}/image",
    response_model=schemas.RecipeDetail,
    dependencies=[Depends(require(Action.WRITE, Module.KITCHEN))],
    summary="Remove a recipe's picture",
)
def delete_image(
    recipe_id: UUID, db: DbDep, auth: AuthDep, client_ip: ClientIpDep
) -> schemas.RecipeDetail:
    recipe = _load_editable(db, auth, recipe_id)
    service.clear_image(db, recipe)
    audit.record(
        db,
        AuditAction.RECIPE_UPDATED,
        actor_id=auth.user.id,
        actor_label=auth.user.username,
        resource_type="recipe",
        resource_id=str(recipe.id),
        client_ip=client_ip,
        detail={"image": "removed"},
    )
    db.commit()
    # Re-read through the scoped path rather than db.refresh(): refresh issues an
    # unscoped SELECT, which the visibility guard rightly rejects. Going back
    # through get_recipe also picks up the rows written above.
    return _detail(service.get_recipe(db, auth.principal, recipe.id))


# --- import (SPEC §4.6) -------------------------------------------------------


@router.post(
    "/import",
    response_model=schemas.ImportedRecipe,
    dependencies=[Depends(require(Action.WRITE, Module.KITCHEN))],
    summary="Read a recipe from a web page, without saving it",
)
def import_from_url(
    payload: schemas.ImportRequest, auth: AuthDep, client_ip: ClientIpDep, db: DbDep
) -> schemas.ImportedRecipe:
    """Fetch, parse, and hand back a **draft**.

    Nothing is saved here, and that is §4.6's requirement rather than an
    implementation convenience: "let me correct the parse in the UI before
    saving". The draft goes to the browser, the cook fixes whatever the parser
    got wrong, and the ordinary create endpoint stores the result — so an
    imported recipe goes through exactly the same validation as a typed one.

    Gated on kitchen **write** even though it writes nothing. It makes the
    server open a connection to an address the caller chose, which is a
    capability, not a read.
    """
    try:
        fetched = urlfetch.fetch(payload.url)
    except urlfetch.UnsafeUrl as exc:
        # 400 rather than 422: the address is well-formed, we are declining to
        # go there. The message says which, because "invalid URL" would send
        # somebody hunting for a typo that is not there.
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except urlfetch.FetchFailed as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    try:
        found = scrape.scrape(urlfetch.decode(fetched), url=fetched.url)
    except scrape.NoRecipeFound as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                "That page does not publish its recipe in a form we can read. "
                "You can still add it by hand."
            ),
        ) from exc

    audit.record(
        db,
        AuditAction.RECIPE_CREATED,
        actor_id=auth.user.id,
        actor_label=auth.user.username,
        resource_type="recipe_import",
        client_ip=client_ip,
        # The address is the point of the record: this is the one endpoint that
        # makes the server talk to somewhere the user chose.
        detail={"url": fetched.url, "extracted_by": found.source},
    )
    db.commit()

    parsed = ingredient_text.parse_ingredients(found.ingredients)
    return schemas.ImportedRecipe(
        title=found.title,
        description=found.description,
        servings=found.servings,
        prep_minutes=found.prep_minutes,
        cook_minutes=found.cook_minutes,
        source_url=fetched.url,
        tags=found.tags,
        ingredients=[
            schemas.ImportedIngredient(
                raw=raw,
                name=row.name,
                quantity=row.quantity,
                unit=row.unit,
                note=row.note,
                confident=row.confident,
            )
            for raw, row in zip(found.ingredients, parsed, strict=False)
        ],
        steps=found.steps,
        extracted_by=found.source,
        image_url=found.image_url,
    )


@router.post(
    "/{recipe_id}/image/from-url",
    response_model=schemas.RecipeDetail,
    dependencies=[Depends(require(Action.WRITE, Module.KITCHEN))],
    summary="Store a picture from the page a recipe was imported from",
)
def image_from_url(
    recipe_id: UUID,
    payload: schemas.ImportRequest,
    db: DbDep,
    auth: AuthDep,
    client_ip: ClientIpDep,
) -> schemas.RecipeDetail:
    """Fetch an image by URL and run it through the ordinary pipeline.

    Separate from the draft on purpose. Downloading somebody's photograph is a
    second network request to a second address, and it should only happen once
    the cook has decided to keep the recipe — not while they are still looking
    at a preview they might discard.

    The bytes go through `images.render` exactly like an upload, so a page
    serving a script with an image's content type gets the same refusal.
    """
    recipe = _load_editable(db, auth, recipe_id)
    try:
        fetched = urlfetch.fetch(payload.url, max_bytes=get_settings().upload_max_bytes)
    except urlfetch.UnsafeUrl as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except urlfetch.FetchFailed as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    try:
        rendered = images.render(fetched.body)
    except (images.NotAnImage, images.ImageTooLarge) as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    service.set_image(db, recipe, rendered.full, rendered.thumb)
    audit.record(
        db,
        AuditAction.RECIPE_UPDATED,
        actor_id=auth.user.id,
        actor_label=auth.user.username,
        resource_type="recipe",
        resource_id=str(recipe.id),
        client_ip=client_ip,
        detail={"image": f"{rendered.width}x{rendered.height}", "from": fetched.url},
    )
    db.commit()
    return _detail(service.get_recipe(db, auth.principal, recipe.id))


@router.post(
    "/import/mealie",
    response_model=schemas.MealieImportResult,
    dependencies=[Depends(require(Action.WRITE, Module.KITCHEN))],
    summary="Import a Mealie ZIP export",
)
async def import_mealie(
    db: DbDep,
    auth: AuthDep,
    client_ip: ClientIpDep,
    settings: SettingsDep,
    file: Annotated[UploadFile, File()],
    preview: bool = True,
    on_conflict: service.ConflictMode = service.ConflictMode.SKIP,
) -> schemas.MealieImportResult:
    """Read a Mealie export and import it.

    Two passes over the same code path. `preview=true` reports what *would*
    happen and writes nothing; `preview=false` does it. They share
    `service.import_mealie` rather than deriving the numbers twice, because a
    preview that disagrees with the import is worse than no preview at all.

    The whole import is one transaction. A failure halfway through imports
    nothing, which matters more than it sounds: a half-migrated library is worse
    than an unmigrated one, because you cannot tell by looking which half
    arrived.
    """
    # Archives are bigger than photographs, so the cap is its own — but there is
    # still a cap, and it is enforced by reading one byte past it.
    limit = max(settings.upload_max_bytes, mealie.MAX_TOTAL_BYTES // 4)
    data = await file.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(
            status.HTTP_413_CONTENT_TOO_LARGE,
            detail=f"That archive is larger than {limit // (1024 * 1024)} MB.",
        )
    if not data:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail="That file is empty.")

    try:
        archive = mealie.read(data)
    except mealie.BadArchive as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    outcome = service.import_mealie(
        db,
        auth.principal,
        archive,
        on_conflict=on_conflict,
        dry_run=preview,
        render_image=images.render,
    )

    if not preview:
        audit.record(
            db,
            AuditAction.RECIPE_CREATED,
            actor_id=auth.user.id,
            actor_label=auth.user.username,
            resource_type="mealie_import",
            client_ip=client_ip,
            detail={
                "imported": outcome.imported,
                "replaced": outcome.replaced,
                "skipped": outcome.skipped_existing,
            },
        )
        db.commit()
    else:
        # Nothing was written, but `existing_titles` opened a transaction.
        db.rollback()

    return schemas.MealieImportResult(
        preview=preview,
        found=len(archive.recipes),
        imported=outcome.imported,
        replaced=outcome.replaced,
        skipped_existing=outcome.skipped_existing,
        skipped_unreadable=outcome.skipped_unreadable,
        found_with_images=sum(1 for recipe in archive.recipes if recipe.image),
        with_images=outcome.with_images,
        conflict_count=len(outcome.conflicts),
        # Truncated for display only. The count above is the real one.
        conflicts=sorted(outcome.conflicts)[:8],
    )
