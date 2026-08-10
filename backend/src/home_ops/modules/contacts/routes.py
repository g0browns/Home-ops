"""Contact directory endpoints (SPEC §4.7).

**No CardDAV.** Settled with the owner on 2026-07-31: vCard import and export
only, for now. CardDAV is the same machinery CalDAV needs — an outbound queue on
the record, at-least-once deletes, and the prune guard that stops an empty
remote listing wiping the local rows — and Phase 4b, which was to build that
first, is still deferred. Building it here would mean writing that engine twice
or writing it in the wrong module. Nothing in this phase forecloses it.

**Export is the scoped list, serialised.** It is the endpoint most likely to
become an accidental bypass: a file of everything is exactly what somebody would
reach for. It goes through the same `list_contacts` the directory draws from,
with the same search and tag filters, so it can never return a contact the
screen would not.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, Query, Response, UploadFile, status

from home_ops import audit
from home_ops.audit import AuditAction
from home_ops.dependencies import AuthDep, ClientIpDep, DbDep, require
from home_ops.modules.contacts import schemas, service, vcard
from home_ops.modules.contacts.models import Contact
from home_ops.policy import Action, Module, Visibility, can_edit_record

router = APIRouter(prefix="/contacts", tags=["contacts"])

#: A vCard file is text. This is generous for a household's whole address book
#: and small enough that a stray upload cannot fill the disk.
MAX_IMPORT_BYTES = 5 * 1024 * 1024


def _summary(contact: Contact) -> schemas.ContactSummary:
    return schemas.ContactSummary(
        id=contact.id,
        display_name=contact.display_name,
        organisation=contact.organisation,
        job_title=contact.job_title,
        owner_id=contact.owner_id,
        visibility=Visibility(contact.visibility),
        tags=[tag.tag for tag in contact.tags],
        phones=[schemas.PhoneOut(label=row.label, number=row.number) for row in contact.phones],
        emails=[schemas.EmailOut(label=row.label, address=row.address) for row in contact.emails],
    )


def _detail(contact: Contact) -> schemas.ContactDetail:
    return schemas.ContactDetail(
        **_summary(contact).model_dump(),
        given_name=contact.given_name,
        family_name=contact.family_name,
        website=contact.website,
        notes=contact.notes,
        addresses=[
            schemas.AddressOut(
                label=row.label,
                street=row.street,
                locality=row.locality,
                region=row.region,
                postcode=row.postcode,
                country=row.country,
            )
            for row in contact.addresses
        ],
    )


def _parsed_from(
    payload: schemas.ContactCreate | schemas.ContactUpdate, base: Contact | None = None
) -> vcard.ParsedContact:
    """The editor's shape, in the one the service writes.

    One conversion, shared by create and update, so the two cannot disagree
    about what a blank field means.
    """

    def pick(field: str, fallback: object) -> object:
        value = getattr(payload, field, None)
        return fallback if value is None else value

    return vcard.ParsedContact(
        display_name=str(pick("display_name", base.display_name if base else "")),
        given_name=payload.given_name
        if payload.given_name is not None
        else (base.given_name if base else None),
        family_name=payload.family_name
        if payload.family_name is not None
        else (base.family_name if base else None),
        organisation=payload.organisation
        if payload.organisation is not None
        else (base.organisation if base else None),
        job_title=payload.job_title
        if payload.job_title is not None
        else (base.job_title if base else None),
        website=payload.website
        if payload.website is not None
        else (base.website if base else None),
        notes=str(pick("notes", base.notes if base else "")),
        tags=list(payload.tags)
        if payload.tags is not None
        else ([tag.tag for tag in base.tags] if base else []),
        phones=[vcard.ParsedPhone(label=row.label, number=row.number) for row in payload.phones]
        if payload.phones is not None
        else (
            [vcard.ParsedPhone(label=r.label, number=r.number) for r in base.phones] if base else []
        ),
        emails=[vcard.ParsedEmail(label=row.label, address=row.address) for row in payload.emails]
        if payload.emails is not None
        else (
            [vcard.ParsedEmail(label=r.label, address=r.address) for r in base.emails]
            if base
            else []
        ),
        addresses=[
            vcard.ParsedAddress(
                label=row.label,
                street=row.street,
                locality=row.locality,
                region=row.region,
                postcode=row.postcode,
                country=row.country,
            )
            for row in payload.addresses
        ]
        if payload.addresses is not None
        else (
            [
                vcard.ParsedAddress(
                    label=r.label,
                    street=r.street,
                    locality=r.locality,
                    region=r.region,
                    postcode=r.postcode,
                    country=r.country,
                )
                for r in base.addresses
            ]
            if base
            else []
        ),
    )


def _require_contact(db: DbDep, auth: AuthDep, contact_id: UUID) -> Contact:
    try:
        return service.get_contact(db, auth.principal, contact_id)
    except service.ContactNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="No such contact.") from exc


def _require_editable(auth: AuthDep, contact: Contact) -> None:
    if not can_edit_record(
        auth.principal,
        Action.WRITE,
        Module.CONTACTS,
        owner_id=contact.owner_id,
        visibility=Visibility(contact.visibility),
        deviations=auth.deviations,
    ):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, detail="Not permitted to change this contact."
        )


# --- reading ------------------------------------------------------------------


@router.get(
    "",
    response_model=list[schemas.ContactSummary],
    dependencies=[Depends(require(Action.READ, Module.CONTACTS))],
    summary="The directory",
)
def list_contacts(
    db: DbDep,
    auth: AuthDep,
    search: Annotated[str | None, Query(max_length=100)] = None,
    tag: Annotated[str | None, Query(max_length=32)] = None,
) -> list[schemas.ContactSummary]:
    contacts = service.list_contacts(db, auth.principal, search=search, tag=tag)
    return [_summary(contact) for contact in contacts]


@router.get(
    "/tags",
    response_model=list[str],
    dependencies=[Depends(require(Action.READ, Module.CONTACTS))],
    summary="Tags in use",
)
def list_tags(db: DbDep, auth: AuthDep) -> list[str]:
    return service.list_tags(db, auth.principal)


@router.get(
    "/export",
    dependencies=[Depends(require(Action.READ, Module.CONTACTS))],
    summary="Everything you can see, as a vCard file",
    response_class=Response,
)
def export_contacts(
    db: DbDep,
    auth: AuthDep,
    client_ip: ClientIpDep,
    search: Annotated[str | None, Query(max_length=100)] = None,
    tag: Annotated[str | None, Query(max_length=32)] = None,
) -> Response:
    body = service.export_vcards(db, auth.principal, search=search, tag=tag)

    # §4.1 requires exports to be audited. This one is a file of names and
    # numbers leaving the app, which is the definition of an event worth a row.
    audit.record(
        db,
        AuditAction.DATA_EXPORTED,
        actor_id=auth.user.id,
        actor_label=auth.user.username,
        resource_type="contacts",
        client_ip=client_ip,
        detail={"cards": body.count("BEGIN:VCARD"), "filtered": bool(search or tag)},
    )
    db.commit()

    return Response(
        content=body,
        media_type="text/vcard; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="contacts.vcf"'},
    )


@router.get(
    "/{contact_id}",
    response_model=schemas.ContactDetail,
    dependencies=[Depends(require(Action.READ, Module.CONTACTS))],
    summary="One contact",
)
def get_contact(contact_id: UUID, db: DbDep, auth: AuthDep) -> schemas.ContactDetail:
    return _detail(_require_contact(db, auth, contact_id))


# --- writing ------------------------------------------------------------------


@router.post(
    "",
    response_model=schemas.ContactDetail,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require(Action.WRITE, Module.CONTACTS))],
    summary="Add a contact",
)
def create_contact(
    payload: schemas.ContactCreate, db: DbDep, auth: AuthDep, client_ip: ClientIpDep
) -> schemas.ContactDetail:
    contact = service.create_contact(
        db, auth.principal, _parsed_from(payload), visibility=payload.visibility
    )
    audit.record(
        db,
        AuditAction.CONTACT_CREATED,
        actor_id=auth.user.id,
        actor_label=auth.user.username,
        resource_type="contact",
        resource_id=str(contact.id),
        client_ip=client_ip,
        # The name and who may see it. Never the numbers: an audit log that
        # records a phone number has copied the record into a second place
        # with different permissions.
        detail={"display_name": contact.display_name, "visibility": contact.visibility},
    )
    db.commit()
    return _detail(service.get_contact(db, auth.principal, contact.id))


@router.patch(
    "/{contact_id}",
    response_model=schemas.ContactDetail,
    dependencies=[Depends(require(Action.WRITE, Module.CONTACTS))],
    summary="Change a contact",
)
def update_contact(
    contact_id: UUID,
    payload: schemas.ContactUpdate,
    db: DbDep,
    auth: AuthDep,
    client_ip: ClientIpDep,
) -> schemas.ContactDetail:
    contact = _require_contact(db, auth, contact_id)
    _require_editable(auth, contact)

    if payload.visibility is not None:
        contact.visibility = payload.visibility.value
    service.apply_details(db, contact, _parsed_from(payload, contact))

    audit.record(
        db,
        AuditAction.CONTACT_UPDATED,
        actor_id=auth.user.id,
        actor_label=auth.user.username,
        resource_type="contact",
        resource_id=str(contact.id),
        client_ip=client_ip,
        detail={
            "changed": sorted(payload.model_dump(exclude_unset=True)),
            "visibility": contact.visibility,
        },
    )
    db.commit()
    return _detail(service.get_contact(db, auth.principal, contact_id))


@router.delete(
    "/{contact_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require(Action.WRITE, Module.CONTACTS))],
    summary="Remove a contact",
)
def delete_contact(contact_id: UUID, db: DbDep, auth: AuthDep, client_ip: ClientIpDep) -> Response:
    contact = _require_contact(db, auth, contact_id)
    _require_editable(auth, contact)

    audit.record(
        db,
        AuditAction.CONTACT_DELETED,
        actor_id=auth.user.id,
        actor_label=auth.user.username,
        resource_type="contact",
        resource_id=str(contact.id),
        client_ip=client_ip,
        detail={"display_name": contact.display_name},
    )
    service.delete_contact(db, contact)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/import",
    response_model=schemas.ImportResult,
    dependencies=[Depends(require(Action.WRITE, Module.CONTACTS))],
    summary="Read a vCard file into the directory",
)
async def import_contacts(
    db: DbDep,
    auth: AuthDep,
    client_ip: ClientIpDep,
    file: Annotated[UploadFile, File()],
    preview: Annotated[bool, Query()] = True,
    on_conflict: Annotated[str, Query(pattern="^(skip|replace)$")] = "skip",
    visibility: Annotated[Visibility, Query()] = Visibility.HOUSEHOLD,
) -> schemas.ImportResult:
    payload = await file.read(MAX_IMPORT_BYTES + 1)
    if len(payload) > MAX_IMPORT_BYTES:
        raise HTTPException(
            status.HTTP_413_CONTENT_TOO_LARGE,
            detail=f"A vCard file may not be larger than {MAX_IMPORT_BYTES // (1024 * 1024)} MB.",
        )

    try:
        outcome = service.import_vcards(
            db,
            auth.principal,
            payload,
            dry_run=preview,
            on_conflict=on_conflict,
            visibility=visibility,
        )
    except vcard.VCardError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    if preview:
        # Nothing was written, so nothing is committed and nothing is audited:
        # reading a file somebody uploaded to themselves is not an event.
        db.rollback()
    else:
        audit.record(
            db,
            AuditAction.CONTACT_IMPORTED,
            actor_id=auth.user.id,
            actor_label=auth.user.username,
            resource_type="contacts",
            client_ip=client_ip,
            detail={
                "found": outcome.found,
                "imported": outcome.imported,
                "replaced": outcome.replaced,
                "on_conflict": on_conflict,
            },
        )
        db.commit()

    return schemas.ImportResult(
        preview=preview,
        found=outcome.found,
        imported=outcome.imported,
        replaced=outcome.replaced,
        skipped_existing=outcome.skipped_existing,
        unreadable=outcome.unreadable,
        conflict_count=outcome.conflict_count,
        conflicts=outcome.conflicts,
    )
