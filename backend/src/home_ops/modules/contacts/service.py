"""Contact directory operations (SPEC §4.7).

**Every read goes through `visible_contacts`.** `contacts` is visibility-bearing,
so the scoping guard would catch a forgotten scope on the parent — but the child
tables are not, and a query that joined `contact_phones` directly would sail
past it. They are only ever reached through a scoped contact, which is why this
module has no function that starts from a phone number.

**Search is a filter on the scoped query, never a query of its own.** A separate
search path is the classic way a private record leaks: the record is hidden from
the list and then handed back by the thing that looks for it. `notes/models.py`
argues the point at length; a directory is where somebody would actually try it.

**An import is a bulk write, so it previews first**, exactly as the Mealie import
does and for the same reason: the same code path runs both, with `dry_run`,
because a preview that disagrees with the import is worse than no preview.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import Select, delete, func, or_, select
from sqlalchemy.orm import Session as DbSession

from home_ops.modules.contacts import vcard
from home_ops.modules.contacts.models import (
    MAX_ROWS_PER_KIND,
    SEARCH_CONFIG,
    Contact,
    ContactAddress,
    ContactEmail,
    ContactPhone,
    ContactTag,
)
from home_ops.policy import Principal, Visibility
from home_ops.scoping import SCOPED_OPTION, visible


class ContactNotFound(LookupError):
    pass


def visible_contacts(principal: Principal) -> Select[Any]:
    return visible(select(Contact), Contact, principal)


def list_contacts(
    db: DbSession,
    principal: Principal,
    *,
    search: str | None = None,
    tag: str | None = None,
    limit: int = 500,
) -> list[Contact]:
    """The directory, in name order.

    Ordered by name rather than by relevance even when searching: a directory is
    scanned, and a household's whole address book fits on a screen or two.
    """
    stmt = visible_contacts(principal)

    query = (search or "").strip()
    if query:
        # Full-text over the name, organisation, title and notes, *plus* a plain
        # match on the numbers and addresses. Somebody looking up an unknown
        # caller has the number and nothing else, and `to_tsvector` stems words,
        # not digits.
        pattern = f"%{query}%"
        stmt = stmt.where(
            or_(
                Contact.search_vector.op("@@")(func.websearch_to_tsquery(SEARCH_CONFIG, query)),
                Contact.display_name.ilike(pattern),
                select(ContactPhone.id)
                .where(
                    ContactPhone.contact_id == Contact.id,
                    # Punctuation-insensitive: "5551234567" finds
                    # "(555) 123-4567" only if the brackets stop mattering.
                    func.translate(ContactPhone.number, " ()-.", "").ilike(
                        f"%{func_translate_literal(query)}%"
                    ),
                )
                .exists(),
                select(ContactEmail.id)
                .where(ContactEmail.contact_id == Contact.id, ContactEmail.address.ilike(pattern))
                .exists(),
            )
        )

    wanted_tag = (tag or "").strip().lower()
    if wanted_tag:
        stmt = stmt.where(
            select(ContactTag.contact_id)
            .where(ContactTag.contact_id == Contact.id, ContactTag.tag == wanted_tag)
            .exists()
        )

    stmt = stmt.order_by(func.lower(Contact.display_name)).limit(limit)
    return list(db.scalars(stmt).unique())


def func_translate_literal(value: str) -> str:
    """The same punctuation stripped from a search term as from a stored number.

    Kept beside the query it serves rather than inlined, because the two have to
    agree: strip on one side only and "(555) 123-4567" matches nothing.
    """
    return value.translate(str.maketrans("", "", " ()-."))


def get_contact(db: DbSession, principal: Principal, contact_id: UUID) -> Contact:
    """One contact, or `ContactNotFound`.

    A contact the caller may not see is reported as missing rather than
    forbidden: "you may not see this" confirms that it exists, which for a
    directory is most of what somebody wanted to know.
    """
    found: Contact | None = (
        db.scalars(visible_contacts(principal).where(Contact.id == contact_id))
        .unique()
        .one_or_none()
    )
    if found is None:
        raise ContactNotFound(str(contact_id))
    return found


def list_tags(db: DbSession, principal: Principal) -> list[str]:
    """Tags in use, on contacts this caller can see.

    Scoped, unlike the kitchen's ingredient list. An ingredient name discloses
    nothing; "cardiologist" sitting on somebody's private contact does.
    """
    contact_ids = [contact.id for contact in db.scalars(visible_contacts(principal)).unique()]
    if not contact_ids:
        return []
    stmt = (
        select(ContactTag.tag)
        .where(ContactTag.contact_id.in_(contact_ids))
        .distinct()
        .order_by(ContactTag.tag)
        # Scoped by the contact ids above; the tag table carries no visibility
        # of its own to scope by.
        .execution_options(**{SCOPED_OPTION: True})
    )
    return list(db.scalars(stmt))


# --- writes -------------------------------------------------------------------


def create_contact(
    db: DbSession,
    principal: Principal,
    parsed: vcard.ParsedContact,
    *,
    visibility: Visibility = Visibility.HOUSEHOLD,
) -> Contact:
    contact = Contact(
        display_name=parsed.display_name.strip(),
        owner_id=principal.id,
        visibility=visibility.value,
    )
    db.add(contact)
    db.flush()
    apply_details(db, contact, parsed)
    return contact


def apply_details(db: DbSession, contact: Contact, parsed: vcard.ParsedContact) -> Contact:
    """Write the scalar fields and replace every child collection.

    Wholesale replacement rather than a per-row patch protocol, the same choice
    the recipe editor makes: an ordered list of three phone numbers is not worth
    a diff format, and "the rows I sent are the rows there are" is a rule with
    no edge cases.
    """
    contact.display_name = parsed.display_name.strip()
    contact.given_name = parsed.given_name
    contact.family_name = parsed.family_name
    contact.organisation = parsed.organisation
    contact.job_title = parsed.job_title
    contact.website = parsed.website
    # NOT NULL with a '' default in the database, so None must not be written
    # through — the same trap the recipe route documents.
    contact.notes = parsed.notes or ""

    set_tags(db, contact, parsed.tags)
    set_phones(db, contact, parsed.phones)
    set_emails(db, contact, parsed.emails)
    set_addresses(db, contact, parsed.addresses)
    db.flush()
    return contact


def set_tags(db: DbSession, contact: Contact, tags: list[str]) -> None:
    db.execute(delete(ContactTag).where(ContactTag.contact_id == contact.id))
    seen: set[str] = set()
    for raw in tags:
        tag = raw.strip().lower()
        if tag and tag not in seen:
            seen.add(tag)
            db.add(ContactTag(contact_id=contact.id, tag=tag))
    db.flush()
    # The collection was emptied by a bulk DELETE the session knows nothing
    # about, so it has to be re-read rather than trusted.
    db.expire(contact, ["tags"])


def set_phones(db: DbSession, contact: Contact, phones: list[vcard.ParsedPhone]) -> None:
    db.execute(delete(ContactPhone).where(ContactPhone.contact_id == contact.id))
    for position, phone in enumerate(phones[:MAX_ROWS_PER_KIND]):
        number = phone.number.strip()
        if number:
            db.add(
                ContactPhone(
                    contact_id=contact.id,
                    label=(phone.label or "").strip() or None,
                    number=number,
                    position=position,
                )
            )
    db.flush()
    db.expire(contact, ["phones"])


def set_emails(db: DbSession, contact: Contact, emails: list[vcard.ParsedEmail]) -> None:
    db.execute(delete(ContactEmail).where(ContactEmail.contact_id == contact.id))
    for position, email in enumerate(emails[:MAX_ROWS_PER_KIND]):
        address = email.address.strip()
        if address:
            db.add(
                ContactEmail(
                    contact_id=contact.id,
                    label=(email.label or "").strip() or None,
                    address=address,
                    position=position,
                )
            )
    db.flush()
    db.expire(contact, ["emails"])


def set_addresses(db: DbSession, contact: Contact, addresses: list[vcard.ParsedAddress]) -> None:
    db.execute(delete(ContactAddress).where(ContactAddress.contact_id == contact.id))
    position = 0
    for address in addresses[:MAX_ROWS_PER_KIND]:
        if address.is_empty():
            continue
        db.add(
            ContactAddress(
                contact_id=contact.id,
                label=(address.label or "").strip() or None,
                street=address.street,
                locality=address.locality,
                region=address.region,
                postcode=address.postcode,
                country=address.country,
                position=position,
            )
        )
        position += 1
    db.flush()
    db.expire(contact, ["addresses"])


def delete_contact(db: DbSession, contact: Contact) -> None:
    db.delete(contact)
    db.flush()


# --- vCard --------------------------------------------------------------------


def to_parsed(contact: Contact) -> vcard.ParsedContact:
    """A stored contact in the shape the vCard writer takes."""
    return vcard.ParsedContact(
        display_name=contact.display_name,
        given_name=contact.given_name,
        family_name=contact.family_name,
        organisation=contact.organisation,
        job_title=contact.job_title,
        website=contact.website,
        notes=contact.notes,
        tags=[tag.tag for tag in contact.tags],
        phones=[vcard.ParsedPhone(label=row.label, number=row.number) for row in contact.phones],
        emails=[vcard.ParsedEmail(label=row.label, address=row.address) for row in contact.emails],
        addresses=[
            vcard.ParsedAddress(
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


def export_vcards(
    db: DbSession, principal: Principal, *, search: str | None = None, tag: str | None = None
) -> str:
    """Everything this caller can see, as a vCard stream.

    Through the same scoped list the directory uses, so an export cannot become
    the way to read a contact the screen would not show.
    """
    contacts = list_contacts(db, principal, search=search, tag=tag, limit=5000)
    return vcard.serialise([to_parsed(contact) for contact in contacts])


class ImportOutcome:
    """What an import did, or would do."""

    def __init__(self) -> None:
        self.found = 0
        self.imported = 0
        self.replaced = 0
        self.skipped_existing = 0
        self.unreadable = 0
        self.conflict_count = 0
        self.conflicts: list[str] = []


def import_vcards(
    db: DbSession,
    principal: Principal,
    payload: bytes,
    *,
    dry_run: bool = True,
    on_conflict: str = "skip",
    visibility: Visibility = Visibility.HOUSEHOLD,
) -> ImportOutcome:
    """Read a vCard file into the directory.

    `dry_run` runs every step except the writes, so the preview and the import
    cannot disagree — the same arrangement the Mealie import uses.

    A clash is a contact this caller can already see with the same name. Matching
    on the *visible* set matters: matching on every row would tell somebody
    that a private contact called "Dr Weaver" exists by refusing to import
    theirs.
    """
    report = vcard.parse(payload)
    outcome = ImportOutcome()
    outcome.found = len(report.contacts)
    outcome.unreadable = report.skipped

    existing = {
        contact.display_name.strip().lower(): contact
        for contact in db.scalars(visible_contacts(principal)).unique()
    }

    for parsed in report.contacts:
        key = parsed.display_name.strip().lower()
        clash = existing.get(key)

        if clash is not None:
            outcome.conflict_count += 1
            if len(outcome.conflicts) < 20:
                # Truncated for display, and counted separately above. Reading a
                # count off a truncated list is how the Mealie import once said
                # "50 already exist" when it was 55.
                outcome.conflicts.append(parsed.display_name)

            if on_conflict == "skip":
                outcome.skipped_existing += 1
                continue

            outcome.replaced += 1
            if not dry_run:
                apply_details(db, clash, parsed)
            continue

        outcome.imported += 1
        if not dry_run:
            created = create_contact(db, principal, parsed, visibility=visibility)
            existing[key] = created

    return outcome
