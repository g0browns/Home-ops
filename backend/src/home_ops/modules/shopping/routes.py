"""Shopping endpoints (SPEC §4.12).

**Polling with an ETag, settled in 5d and unchanged by the move.** `GET` returns
every list the caller may see *with its items*, and answers `304` to a matching
`If-None-Match`. One request rather than one per list: the client draws all the
lists at once, so a poll that had to ask per list would multiply by the number
of lists somebody made, and a 304 per list is not a saving.

It behaves identically on all three access paths (§2.1), where a websocket has
to be tested over the tunnel *and* over plain HTTP twice more; and the conflict
§4.6 named — two people shopping at once — is prevented by the *shape of the
writes*, not by the transport. Every mutation here touches one row.

**Generating is gated on `shopping` write *and* `kitchen` read.** Building a
list reads the meal plan and recipe ingredients, which is a Kitchen read by a
longer route. Without that second check, a member with no Kitchen access could
obtain recipe contents through this module — the same shape of bypass §4.2
forbids, arriving through a door nobody thought to lock.
"""

from __future__ import annotations

import hashlib
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from home_ops import audit
from home_ops.audit import AuditAction
from home_ops.dependencies import AuthContext, AuthDep, ClientIpDep, DbDep, require
from home_ops.modules.shopping import schemas, service
from home_ops.modules.shopping.models import ShoppingItem, ShoppingList
from home_ops.policy import Action, Module

router = APIRouter(prefix="/shopping-lists", tags=["shopping"])


def _item_out(item: ShoppingItem) -> schemas.ShoppingItemOut:
    return schemas.ShoppingItemOut(
        id=item.id,
        list_id=item.list_id,
        ingredient_id=item.ingredient_id,
        name=item.ingredient.name if item.ingredient else (item.title or ""),
        section=service.section_of(item),
        quantity=item.quantity,
        unit=item.unit,
        note=item.note,
        is_generated=item.is_generated,
        is_uncombined=item.is_uncombined,
        quantity_overridden=item.quantity_overridden,
        is_checked=item.is_checked,
        checked_by_id=item.checked_by_id,
        position=item.position,
    )


def _list_out(shopping_list: ShoppingList, items: list[ShoppingItem]) -> schemas.ShoppingListOut:
    from home_ops.policy import Visibility

    return schemas.ShoppingListOut(
        id=shopping_list.id,
        name=shopping_list.name,
        visibility=Visibility(shopping_list.visibility),
        shared_with=[share.user_id for share in shopping_list.shares],
        is_meal_plan_target=shopping_list.is_meal_plan_target,
        position=shopping_list.position,
        owner_id=shopping_list.owner_id,
        items=[_item_out(item) for item in items],
    )


def _etag(payload: list[schemas.ShoppingListOut]) -> str:
    """A tag over what a client would actually render.

    Built from the fields the response carries rather than from a row count or a
    `max(updated_at)`: a tick that flips back inside one second must still change
    the tag, and a count would not notice a swap or a line moving between lists.
    """
    digest = hashlib.sha256()
    for shopping_list in payload:
        digest.update(
            f"L{shopping_list.id}:{shopping_list.name}:{shopping_list.visibility}:"
            f"{shopping_list.is_meal_plan_target}:{shopping_list.position}:"
            f"{sorted(str(user) for user in shopping_list.shared_with)}".encode()
        )
        for item in shopping_list.items:
            digest.update(
                f"I{item.id}:{item.list_id}:{item.name}:{item.quantity}:{item.unit}:"
                f"{item.section}:{item.note}:{item.is_checked}:{item.checked_by_id}:"
                f"{item.position}:{item.is_uncombined}".encode()
            )
    return f'W/"{digest.hexdigest()[:32]}"'


def _visible_payload(db: DbDep, auth: AuthContext) -> list[schemas.ShoppingListOut]:
    lists = service.visible_lists(db, auth.principal)
    items = service.visible_items(db, auth.principal)
    by_list: dict[UUID, list[ShoppingItem]] = {shopping.id: [] for shopping in lists}
    for item in items:
        by_list.setdefault(item.list_id, []).append(item)
    return [_list_out(shopping, by_list.get(shopping.id, [])) for shopping in lists]


# --- lists --------------------------------------------------------------------


@router.get(
    "",
    response_model=list[schemas.ShoppingListOut],
    dependencies=[Depends(require(Action.READ, Module.SHOPPING))],
    summary="Every list you can see, with its lines",
)
def list_lists(
    db: DbDep, auth: AuthDep, request: Request, response: Response
) -> list[schemas.ShoppingListOut] | Response:
    payload = _visible_payload(db, auth)
    tag = _etag(payload)
    response.headers["ETag"] = tag
    response.headers["Cache-Control"] = "no-cache"

    if request.headers.get("if-none-match") == tag:
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers={"ETag": tag})
    return payload


@router.post(
    "",
    response_model=schemas.ShoppingListOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require(Action.WRITE, Module.SHOPPING))],
    summary="Start a list",
)
def create_list(
    payload: schemas.ShoppingListIn, db: DbDep, auth: AuthDep, client_ip: ClientIpDep
) -> schemas.ShoppingListOut:
    try:
        created = service.create_list(
            db,
            auth.principal,
            item_id=payload.id,
            name=payload.name,
            visibility=payload.visibility,
            is_meal_plan_target=payload.is_meal_plan_target,
            shared_with=payload.shared_with,
        )
    except service.TooManyLists as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except service.IdAlreadyTaken as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="That id is already in use.") from exc

    audit.record(
        db,
        AuditAction.SHOPPING_LIST_CREATED,
        actor_id=auth.user.id,
        actor_label=auth.user.username,
        resource_type="shopping_list",
        resource_id=str(created.id),
        client_ip=client_ip,
        # The name and who can see it, never the contents.
        detail={"name": created.name, "visibility": created.visibility},
    )
    db.commit()
    return _list_out(created, [])


@router.patch(
    "/{list_id}",
    response_model=schemas.ShoppingListOut,
    dependencies=[Depends(require(Action.WRITE, Module.SHOPPING))],
    summary="Rename a list, or change who can see it",
)
def update_list(
    list_id: UUID,
    payload: schemas.ShoppingListPatch,
    db: DbDep,
    auth: AuthDep,
    client_ip: ClientIpDep,
) -> schemas.ShoppingListOut:
    shopping_list = _require_list(db, auth, list_id)
    fields = payload.model_dump(exclude_unset=True)

    service.update_list(
        db,
        shopping_list,
        name=payload.name if "name" in fields else None,
        visibility=payload.visibility if "visibility" in fields else None,
        is_meal_plan_target=(
            payload.is_meal_plan_target if "is_meal_plan_target" in fields else None
        ),
        shared_with=payload.shared_with if "shared_with" in fields else None,
    )

    audit.record(
        db,
        AuditAction.SHOPPING_LIST_UPDATED,
        actor_id=auth.user.id,
        actor_label=auth.user.username,
        resource_type="shopping_list",
        resource_id=str(shopping_list.id),
        client_ip=client_ip,
        detail={"changed": sorted(fields), "visibility": shopping_list.visibility},
    )
    db.commit()
    return _list_out(shopping_list, list(shopping_list.items))


@router.put(
    "/order",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require(Action.WRITE, Module.SHOPPING))],
    summary="The order the lists are drawn in",
)
def reorder(payload: schemas.ShoppingListOrder, db: DbDep, auth: AuthDep) -> Response:
    service.reorder_lists(db, auth.principal, payload.order)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete(
    "/{list_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require(Action.WRITE, Module.SHOPPING))],
    summary="Delete a list and everything on it",
)
def delete_list(list_id: UUID, db: DbDep, auth: AuthDep, client_ip: ClientIpDep) -> Response:
    shopping_list = _require_list(db, auth, list_id)
    audit.record(
        db,
        AuditAction.SHOPPING_LIST_DELETED,
        actor_id=auth.user.id,
        actor_label=auth.user.username,
        resource_type="shopping_list",
        resource_id=str(shopping_list.id),
        client_ip=client_ip,
        detail={"name": shopping_list.name, "lines": len(shopping_list.items)},
    )
    service.delete_list(db, shopping_list)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- items --------------------------------------------------------------------


@router.post(
    "/{list_id}/items",
    response_model=schemas.ShoppingItemOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require(Action.WRITE, Module.SHOPPING))],
    summary="Add something by hand",
)
def add_item(
    list_id: UUID, payload: schemas.ShoppingItemIn, db: DbDep, auth: AuthDep
) -> schemas.ShoppingItemOut:
    shopping_list = _require_list(db, auth, list_id)
    try:
        item = service.add_manual(
            db,
            auth.principal,
            shopping_list,
            item_id=payload.id,
            title=payload.title,
            ingredient_name=payload.ingredient_name,
            quantity=payload.quantity,
            unit=payload.unit,
            note=payload.note,
            section=payload.section,
        )
    except service.IdAlreadyTaken as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="That id is already in use.") from exc
    db.commit()
    return _item_out(service.get_item(db, auth.principal, item.id))


@router.patch(
    "/items/{item_id}",
    response_model=schemas.ShoppingItemOut,
    dependencies=[Depends(require(Action.WRITE, Module.SHOPPING))],
    summary="Tick a line, note it, place it in the shop, or move it to another list",
)
def update_item(
    item_id: UUID, payload: schemas.ShoppingItemPatch, db: DbDep, auth: AuthDep
) -> schemas.ShoppingItemOut:
    item = _require_item(db, auth, item_id)
    fields = payload.model_dump(exclude_unset=True)

    if payload.is_checked is not None:
        service.set_checked(db, auth.principal, item, checked=payload.is_checked)
    if "quantity" in fields or "unit" in fields:
        # Both together: an amount without its unit is a different amount, and
        # a client changing one has decided about the other by omission.
        service.set_quantity(db, item, payload.quantity, payload.unit)
    if "note" in fields:
        item.note = (payload.note or "").strip() or None
        db.flush()
    if "section" in fields:
        service.set_section(db, item, payload.section)
    if payload.list_id is not None:
        # The transfer. Both ends are resolved through the caller's own scope,
        # so a line cannot be posted into a list they cannot see and cannot be
        # dragged out of one they cannot see either.
        service.move_item(db, item, _require_list(db, auth, payload.list_id))

    db.commit()
    return _item_out(service.get_item(db, auth.principal, item_id))


@router.delete(
    "/items/{item_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require(Action.WRITE, Module.SHOPPING))],
    summary="Take a line off the list",
)
def remove_item(item_id: UUID, db: DbDep, auth: AuthDep) -> Response:
    service.remove_item(db, _require_item(db, auth, item_id))
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{list_id}/clear-checked",
    response_model=schemas.ShoppingClearResult,
    dependencies=[Depends(require(Action.WRITE, Module.SHOPPING))],
    summary="Remove everything already in the trolley",
)
def clear_checked(list_id: UUID, db: DbDep, auth: AuthDep) -> schemas.ShoppingClearResult:
    removed = service.clear_checked(db, _require_list(db, auth, list_id))
    db.commit()
    return schemas.ShoppingClearResult(removed=removed)


@router.get(
    "/sections",
    response_model=list[str],
    dependencies=[Depends(require(Action.READ, Module.SHOPPING))],
    summary="Sections already in use",
)
def list_sections(db: DbDep, auth: AuthDep) -> list[str]:
    return service.known_sections(db, auth.principal)


# --- generating from the meal plan --------------------------------------------


@router.post(
    "/generate",
    response_model=schemas.ShoppingGenerateResult,
    dependencies=[Depends(require(Action.WRITE, Module.SHOPPING))],
    summary="Build a list from the meal plan",
)
def generate(
    payload: schemas.ShoppingGenerateIn, db: DbDep, auth: AuthDep, client_ip: ClientIpDep
) -> schemas.ShoppingGenerateResult:
    # Shopping write is not enough. This reads the plan and the ingredients
    # behind planned recipes, so it is a Kitchen read as well, and it says so
    # rather than letting the Shopping module become a side door into recipes.
    if not auth.can(Action.READ, Module.KITCHEN):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail="Building a list from the meal plan needs access to the Kitchen.",
        )

    try:
        target = (
            _require_list(db, auth, payload.list_id)
            if payload.list_id
            else service.meal_plan_target(db, auth.principal)
        )
    except service.NoMealPlanTarget as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    try:
        basket = service.generate(db, auth.principal, target, start=payload.start, end=payload.end)
    except service.SpanTooWide as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    audit.record(
        db,
        AuditAction.RECIPE_UPDATED,
        actor_id=auth.user.id,
        actor_label=auth.user.username,
        resource_type="shopping_list",
        resource_id=str(target.id),
        client_ip=client_ip,
        # The window and the size, never what is on the list.
        detail={"from": str(payload.start), "to": str(payload.end), "lines": len(basket.lines)},
    )
    db.commit()

    return schemas.ShoppingGenerateResult(
        list_id=target.id,
        hidden_meals=basket.hidden_meals,
        text_meals=basket.text_meals[:20],
        uncombined=sum(1 for line in basket.lines if line.uncombined),
        kept_on_other_lists=basket.kept_on_other_lists,
    )


# --- shared lookups -----------------------------------------------------------


def _require_list(db: DbDep, auth: AuthContext, list_id: UUID) -> ShoppingList:
    """A list the caller may see, or a 404.

    404 and not 403: "you may not see this" confirms the list exists, which is
    exactly what per-list visibility is there to withhold.
    """
    try:
        return service.get_list(db, auth.principal, list_id)
    except service.ListNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="No such list.") from exc


def _require_item(db: DbDep, auth: AuthContext, item_id: UUID) -> ShoppingItem:
    try:
        return service.get_item(db, auth.principal, item_id)
    except service.ItemNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="No such item.") from exc
