"""Note endpoints (SPEC §4.5).

Search, tag listing and the board all run through the same visibility scoping.
That is the property worth protecting here: a note you cannot browse to is also
one you cannot find by searching for a word inside it.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from home_ops import audit
from home_ops.audit import AuditAction
from home_ops.dependencies import AuthDep, ClientIpDep, DbDep, require
from home_ops.modules.notes import schemas, service
from home_ops.modules.notes.models import Note
from home_ops.policy import Action, Module, Visibility, can_edit_record

router = APIRouter(prefix="/notes", tags=["notes"])


def _to_out(note: Note) -> schemas.NoteOut:
    """Build the response field by field rather than from the ORM object.

    `model_validate` would try to coerce the `NoteTag` rows into the `list[str]`
    the schema declares and fail before any override could fix it. Listing the
    fields explicitly also makes the response an allowlist, so a column added to
    `Note` later cannot leak through it by accident.
    """
    return schemas.NoteOut(
        id=note.id,
        title=note.title,
        body=note.body,
        color_key=note.color_key,
        is_pinned=note.is_pinned,
        position=note.position,
        owner_id=note.owner_id,
        visibility=Visibility(note.visibility),
        created_at=note.created_at,
        updated_at=note.updated_at,
        tags=sorted(tag.tag for tag in note.tags),
    )


def _load_editable(db: DbDep, auth: AuthDep, note_id: UUID) -> Note:
    """404 for what you cannot see, 403 for what you can see but may not change.

    That order matters: a 403 on an invisible note would confirm it exists.
    """
    try:
        note = service.get_note(db, auth.principal, note_id)
    except service.NoteNotVisible as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="No such note.") from exc

    if not can_edit_record(
        auth.principal,
        Action.WRITE,
        Module.NOTES,
        owner_id=note.owner_id,
        visibility=Visibility(note.visibility),
        deviations=auth.deviations,
    ):
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Not permitted to change this note.")
    return note


@router.get(
    "",
    response_model=list[schemas.NoteOut],
    dependencies=[Depends(require(Action.READ, Module.NOTES))],
    summary="List or search notes",
)
def list_notes(
    auth: AuthDep,
    db: DbDep,
    search: Annotated[str | None, Query(max_length=200)] = None,
    tag: Annotated[str | None, Query(max_length=32)] = None,
    author_id: UUID | None = None,
) -> list[schemas.NoteOut]:
    notes = service.list_notes(db, auth.principal, search=search, tag=tag, author_id=author_id)
    return [_to_out(note) for note in notes]


@router.get(
    "/tags",
    response_model=list[str],
    dependencies=[Depends(require(Action.READ, Module.NOTES))],
    summary="Tags in use on notes you can see",
)
def list_tags(auth: AuthDep, db: DbDep) -> list[str]:
    return service.known_tags(db, auth.principal)


@router.post(
    "",
    response_model=schemas.NoteOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require(Action.WRITE, Module.NOTES))],
    summary="Create a note",
)
def create_note(
    payload: schemas.NoteCreate, auth: AuthDep, db: DbDep, client_ip: ClientIpDep
) -> schemas.NoteOut:
    note = service.create_note(
        db,
        auth.principal,
        title=payload.title,
        body=payload.body,
        color_key=payload.color_key,
        is_pinned=payload.is_pinned,
        visibility=payload.visibility,
        tags=list(payload.tags),
    )
    audit.record(
        db,
        AuditAction.NOTE_CREATED,
        actor_id=auth.user.id,
        actor_label=auth.user.username,
        resource_type="note",
        resource_id=str(note.id),
        client_ip=client_ip,
        # The title, never the body: a note's contents are the private part.
        detail={"title": note.title, "visibility": note.visibility},
    )
    db.commit()
    # Re-read through the scoped path rather than db.refresh(): refresh issues an
    # unscoped SELECT, which the visibility guard rightly rejects. Going back
    # through get_note also picks up the tags written above.
    return _to_out(service.get_note(db, auth.principal, note.id))


@router.put(
    "/order",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require(Action.WRITE, Module.NOTES))],
    summary="Set the board order",
)
def reorder(
    payload: schemas.ReorderRequest, auth: AuthDep, db: DbDep, client_ip: ClientIpDep
) -> Response:
    """Declared before /{note_id} so "order" is not read as a note id."""
    moved = service.reorder_notes(db, auth.principal, list(payload.note_ids))
    audit.record(
        db,
        AuditAction.NOTE_UPDATED,
        actor_id=auth.user.id,
        actor_label=auth.user.username,
        resource_type="note",
        client_ip=client_ip,
        detail={"reordered": moved},
    )
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/{note_id}",
    response_model=schemas.NoteOut,
    dependencies=[Depends(require(Action.READ, Module.NOTES))],
    summary="Read one note",
)
def read_note(note_id: UUID, auth: AuthDep, db: DbDep) -> schemas.NoteOut:
    try:
        return _to_out(service.get_note(db, auth.principal, note_id))
    except service.NoteNotVisible as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="No such note.") from exc


@router.patch(
    "/{note_id}",
    response_model=schemas.NoteOut,
    dependencies=[Depends(require(Action.WRITE, Module.NOTES))],
    summary="Update a note",
)
def update_note(
    note_id: UUID,
    payload: schemas.NoteUpdate,
    auth: AuthDep,
    db: DbDep,
    client_ip: ClientIpDep,
) -> schemas.NoteOut:
    note = _load_editable(db, auth, note_id)
    changes: dict[str, object] = {}

    if payload.title is not None:
        note.title = payload.title.strip()
        changes["title"] = note.title
    if payload.body is not None:
        note.body = payload.body
        # The fact of an edit, not its content.
        changes["body_changed"] = True
    if payload.color_key is not None:
        note.color_key = payload.color_key
    if payload.is_pinned is not None:
        note.is_pinned = payload.is_pinned
        changes["is_pinned"] = note.is_pinned
    if payload.visibility is not None:
        note.visibility = payload.visibility.value
        changes["visibility"] = note.visibility
    if payload.tags is not None:
        service.set_tags(db, note, list(payload.tags))
        changes["tags"] = service.normalize_tags(list(payload.tags))

    db.flush()
    audit.record(
        db,
        AuditAction.NOTE_UPDATED,
        actor_id=auth.user.id,
        actor_label=auth.user.username,
        resource_type="note",
        resource_id=str(note.id),
        client_ip=client_ip,
        detail=changes,
    )
    db.commit()
    # Re-read through the scoped path rather than db.refresh(): refresh issues an
    # unscoped SELECT, which the visibility guard rightly rejects. Going back
    # through get_note also picks up the tags written above.
    return _to_out(service.get_note(db, auth.principal, note.id))


@router.delete(
    "/{note_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require(Action.WRITE, Module.NOTES))],
    summary="Delete a note",
)
def delete_note(note_id: UUID, auth: AuthDep, db: DbDep, client_ip: ClientIpDep) -> Response:
    note = _load_editable(db, auth, note_id)
    title = note.title
    service.delete_note(db, note)

    audit.record(
        db,
        AuditAction.NOTE_DELETED,
        actor_id=auth.user.id,
        actor_label=auth.user.username,
        resource_type="note",
        resource_id=str(note_id),
        client_ip=client_ip,
        detail={"title": title},
    )
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
