"""Meal plan endpoints (SPEC §4.6, phase 5c).

Gated on `kitchen`, like recipes. A limited member — a kid — has kitchen write,
so they can put something on Thursday; that is the point of a shared planner.

`_to_out` is where §4.2 is honoured: an entry whose recipe the caller cannot see
comes back with the slot occupied and the title withheld. See
`plan_service.visible_entries` for why that is the shape rather than hiding the
entry or disclosing the name.
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from home_ops import audit
from home_ops.audit import AuditAction
from home_ops.dependencies import AuthDep, ClientIpDep, DbDep, require
from home_ops.modules.kitchen import plan_service, schemas, service
from home_ops.modules.kitchen import routes as kitchen_routes
from home_ops.modules.kitchen.plan_service import ResolvedEntry
from home_ops.policy import Action, Module

router = APIRouter(prefix="/meal-plan", tags=["kitchen"])


def _to_out(resolved: ResolvedEntry) -> schemas.MealPlanEntryOut:
    entry = resolved.entry

    if resolved.recipe is not None:
        title = resolved.recipe.title
        image_key = resolved.recipe.image_key
    elif resolved.hidden_recipe:
        # The slot is taken and the caller is told so. The recipe's title is not
        # theirs to read, and neither is its picture.
        title = "Something planned"
        image_key = None
    else:
        title = entry.title or ""
        image_key = None

    return schemas.MealPlanEntryOut(
        id=entry.id,
        plan_date=entry.plan_date,
        slot=entry.slot,
        position=entry.position,
        # Withheld along with the title: an id is enough to try fetching it.
        recipe_id=entry.recipe_id if resolved.recipe is not None else None,
        title=title,
        note=entry.note,
        image_key=image_key,
        owner_id=entry.owner_id,
        hidden_recipe=resolved.hidden_recipe,
    )


@router.get(
    "",
    response_model=list[schemas.MealPlanEntryOut],
    dependencies=[Depends(require(Action.READ, Module.KITCHEN))],
    summary="The plan for a window of days",
)
def list_plan(
    db: DbDep,
    auth: AuthDep,
    start: Annotated[dt.date, Query()],
    end: Annotated[dt.date, Query()],
) -> list[schemas.MealPlanEntryOut]:
    try:
        entries = plan_service.list_entries(db, auth.principal, start=start, end=end)
    except plan_service.WindowTooWide as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return [_to_out(entry) for entry in entries]


@router.post(
    "",
    response_model=schemas.MealPlanEntryOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require(Action.WRITE, Module.KITCHEN))],
    summary="Put something on the plan",
)
def add_entry(
    payload: schemas.MealPlanEntryIn, db: DbDep, auth: AuthDep, client_ip: ClientIpDep
) -> schemas.MealPlanEntryOut:
    if payload.recipe_id is not None:
        # Planning a recipe you cannot see would be a way to ask whether it
        # exists. 404 for the same reason every other route uses one.
        try:
            service.get_recipe(db, auth.principal, payload.recipe_id)
        except service.RecipeNotVisible as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="No such recipe.") from exc

    entry = plan_service.add_entry(
        db,
        auth.principal,
        plan_date=payload.plan_date,
        slot=payload.slot,
        recipe_id=payload.recipe_id,
        title=payload.title,
        note=payload.note,
    )
    audit.record(
        db,
        AuditAction.RECIPE_UPDATED,
        actor_id=auth.user.id,
        actor_label=auth.user.username,
        resource_type="meal_plan_entry",
        resource_id=str(entry.id),
        client_ip=client_ip,
        # The date and slot, never the dish: a private recipe's title must not
        # reach the audit log either, where §4.8's readers can see it.
        detail={"date": str(entry.plan_date), "slot": entry.slot},
    )
    db.commit()
    return _to_out(_reread(db, auth, entry.id))


@router.patch(
    "/{entry_id}",
    response_model=schemas.MealPlanEntryOut,
    dependencies=[Depends(require(Action.WRITE, Module.KITCHEN))],
    summary="Move an entry to another day or slot",
)
def move_entry(
    entry_id: UUID,
    payload: schemas.MealPlanMove,
    db: DbDep,
    auth: AuthDep,
    client_ip: ClientIpDep,
) -> schemas.MealPlanEntryOut:
    try:
        entry = plan_service.get_entry(db, entry_id)
    except plan_service.EntryNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="No such plan entry.") from exc

    plan_service.move_entry(
        db, entry, plan_date=payload.plan_date, slot=payload.slot, before_id=payload.before_id
    )
    audit.record(
        db,
        AuditAction.RECIPE_UPDATED,
        actor_id=auth.user.id,
        actor_label=auth.user.username,
        resource_type="meal_plan_entry",
        resource_id=str(entry.id),
        client_ip=client_ip,
        detail={"moved_to": str(entry.plan_date), "slot": entry.slot},
    )
    db.commit()
    return _to_out(_reread(db, auth, entry_id))


@router.delete(
    "/{entry_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require(Action.WRITE, Module.KITCHEN))],
    summary="Take something off the plan",
)
def remove_entry(entry_id: UUID, db: DbDep, auth: AuthDep, client_ip: ClientIpDep) -> Response:
    try:
        entry = plan_service.get_entry(db, entry_id)
    except plan_service.EntryNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="No such plan entry.") from exc

    when, slot = entry.plan_date, entry.slot
    plan_service.remove_entry(db, entry)
    audit.record(
        db,
        AuditAction.RECIPE_UPDATED,
        actor_id=auth.user.id,
        actor_label=auth.user.username,
        resource_type="meal_plan_entry",
        resource_id=str(entry_id),
        client_ip=client_ip,
        detail={"removed_from": str(when), "slot": slot},
    )
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _reread(db: DbDep, auth: AuthDep, entry_id: UUID) -> ResolvedEntry:
    """Read one entry back through the resolving path.

    Building the response from the object in hand would attach the recipe
    regardless of whether the caller may see it — the one thing this module
    exists to avoid.
    """
    entry = plan_service.get_entry(db, entry_id)
    resolved = plan_service.list_entries(
        db, auth.principal, start=entry.plan_date, end=entry.plan_date
    )
    for candidate in resolved:
        if candidate.entry.id == entry_id:
            return candidate
    return ResolvedEntry(entry=entry, recipe=None, hidden_recipe=bool(entry.recipe_id))


@router.post(
    "/{entry_id}/save-as-recipe",
    response_model=schemas.RecipeDetail,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require(Action.WRITE, Module.KITCHEN))],
    summary="Turn a planned meal into a recipe",
)
def save_as_recipe(
    entry_id: UUID, db: DbDep, auth: AuthDep, client_ip: ClientIpDep
) -> schemas.RecipeDetail:
    """§4.6: "a planned meal can be saved back as a recipe".

    The case this exists for: somebody types "Mum's lasagne" into Thursday
    because that is how planning actually happens, and later wants it on the
    shelf. The entry keeps its place in the week and gains a link to the new
    recipe, so the plan does not shuffle underneath somebody mid-thought.

    An entry that already points at a recipe is refused rather than quietly
    duplicating it — the useful answer there is "it already is one".
    """
    try:
        entry = plan_service.get_entry(db, entry_id)
    except plan_service.EntryNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="No such plan entry.") from exc

    if entry.recipe_id is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="That plan entry is already a recipe.")

    title = (entry.title or "").strip()
    if not title:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, detail="That entry has no name to use."
        )

    recipe = service.create_recipe(
        db,
        auth.principal,
        title=title,
        description=entry.note or "",
    )
    # The entry keeps its day, slot and position; only what it points at
    # changes. `title` is cleared so the recipe becomes the single source of the
    # name — two copies would drift the first time one is renamed.
    entry.recipe_id = recipe.id
    entry.title = None
    db.flush()

    audit.record(
        db,
        AuditAction.RECIPE_CREATED,
        actor_id=auth.user.id,
        actor_label=auth.user.username,
        resource_type="recipe",
        resource_id=str(recipe.id),
        client_ip=client_ip,
        detail={"title": recipe.title, "from": "meal_plan_entry"},
    )
    db.commit()
    return kitchen_routes.detail_of(service.get_recipe(db, auth.principal, recipe.id))
