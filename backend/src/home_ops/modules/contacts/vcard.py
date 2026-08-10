"""vCard in and out (SPEC §4.7).

**Nothing here hand-rolls RFC 6350.** `vobject` does the parsing and the
serialising, for the reason §4.3 gives about RFC 5545: line folding at 75
octets with continuation lines, backslash escaping of `,` `;` and newlines,
grouped properties (`item1.TEL`), parameter values in quotes, vCard 2.1's
quoted-printable and CHARSET, and base64 payloads are precisely where a parser
written in an afternoon goes wrong. Import reads a file somebody else's phone
wrote, so this is untrusted input and a maintained library is worth the
dependency.

Both directions go through the same library, so an export cannot drift from what
the import understands — and `test_vcard.py` round-trips to prove it.

**Reading and writing are separate from storing.** This module returns and
accepts plain dataclasses; it never touches the database or the session. That is
what lets the awkward cases be tested directly: a card with no `FN`, a card with
four numbers, a Windows-1252 file from an old phone, a 2.1 card from a Nokia.

**What is deliberately dropped on import:** photos (§4.7 asks for none and
Phase 7 stores none), and anything else not in the model. A property we cannot
store is skipped rather than stuffed into the notes field, because a notes field
full of `X-ABLABEL` is worse than a note that is empty.

**Exported as vCard 3.0**, not 4.0. 3.0 is what iOS, Android and Outlook all
import without argument; 4.0 is the newer spec and is refused or half-read by
enough of them to matter for a file whose whole purpose is to be opened
somewhere else. Import accepts 2.1, 3.0 and 4.0, because that is what arrives.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# vobject ships no type stubs, so everything it hands back is `Any` to mypy and
# the module is ignored in pyproject. Attribute reads therefore go through the
# small helpers below rather than being trusted inline.
import vobject

#: A cap on what one upload may contain. A contacts export from a phone is
#: hundreds of cards; a hundred thousand is somebody testing what happens.
MAX_CARDS = 5000

#: Field lengths, mirroring models.py. A vCard is somebody else's data and may
#: carry anything at all, so it is truncated to what the columns hold rather
#: than rejected — losing the tail of an address is better than losing the card.
MAX_DISPLAY_NAME = 200
MAX_NAME_PART = 100
MAX_ORG = 200
MAX_TITLE = 200
MAX_URL = 500
MAX_LABEL = 32
MAX_PHONE = 64
MAX_EMAIL = 320
MAX_STREET = 200
MAX_LOCALITY = 100
MAX_REGION = 100
MAX_POSTCODE = 32
MAX_COUNTRY = 100
MAX_ROWS_PER_KIND = 20


class VCardError(ValueError):
    """The upload was not something we could read as vCards at all."""


@dataclass
class ParsedPhone:
    label: str | None
    number: str


@dataclass
class ParsedEmail:
    label: str | None
    address: str


@dataclass
class ParsedAddress:
    label: str | None
    street: str | None = None
    locality: str | None = None
    region: str | None = None
    postcode: str | None = None
    country: str | None = None

    def is_empty(self) -> bool:
        return not any((self.street, self.locality, self.region, self.postcode, self.country))


@dataclass
class ParsedContact:
    """One card, in the shape the service stores. Nothing here is trusted."""

    display_name: str
    given_name: str | None = None
    family_name: str | None = None
    organisation: str | None = None
    job_title: str | None = None
    website: str | None = None
    notes: str = ""
    tags: list[str] = field(default_factory=list)
    phones: list[ParsedPhone] = field(default_factory=list)
    emails: list[ParsedEmail] = field(default_factory=list)
    addresses: list[ParsedAddress] = field(default_factory=list)


@dataclass
class ImportReport:
    contacts: list[ParsedContact] = field(default_factory=list)
    #: Cards present in the file that could not be read as a contact. Reported
    #: so "84 of 90" prompts a question rather than passing unnoticed — the same
    #: lesson the Mealie import taught.
    skipped: int = 0


def _clip(value: str | None, limit: int) -> str | None:
    if value is None:
        return None
    cleaned = " ".join(value.split()) if "\n" not in value else value.strip()
    cleaned = cleaned.strip()
    return cleaned[:limit] or None


def _label_of(prop: object) -> str | None:
    """The human label on a TEL/EMAIL/ADR, from its TYPE parameter.

    vCard writes these as `TYPE=CELL`, `TYPE=WORK,VOICE` or, in 4.0, as
    `TYPE="work,voice"`. `PREF` and `VOICE` are dropped: they say how to rank
    and how to dial, not what somebody would call the number.
    """
    params = getattr(prop, "params", {}) or {}
    values: list[str] = []
    for key in ("TYPE", "type"):
        for raw in params.get(key, []) or []:
            values.extend(part for part in str(raw).replace('"', "").split(",") if part)

    useful = [value for value in values if value.upper() not in {"PREF", "VOICE", "INTERNET"}]
    if not useful:
        return None
    return _clip(", ".join(word.capitalize() for word in useful), MAX_LABEL)


def _part_of(prop: object, part: str) -> str | None:
    """One component of a structured value (an `ADR`'s street, city, and so on).

    vObject builds these as a small object, and an address written by a phone
    routinely leaves half of them empty.
    """
    parts = getattr(prop, "value", None)
    if parts is None:
        return None
    value = getattr(parts, part, "")
    return str(value) if value else None


def _text(prop: object) -> str | None:
    value = getattr(prop, "value", None)
    return _clip(value, 10_000) if isinstance(value, str) else None


def parse(payload: bytes | str) -> ImportReport:
    """Read an uploaded file into contacts.

    Accepts one card or a stream of them, which is what a phone export is.
    """
    text = payload.decode("utf-8", errors="replace") if isinstance(payload, bytes) else payload
    if "BEGIN:VCARD" not in text.upper():
        raise VCardError("That file does not contain any vCards.")

    report = ImportReport()
    try:
        components = vobject.readComponents(text, ignoreUnreadable=True)
        for index, component in enumerate(components):
            if index >= MAX_CARDS:
                break
            contact = _from_component(component)
            if contact is None:
                report.skipped += 1
            else:
                report.contacts.append(contact)
    except Exception as exc:
        # Only fatal if nothing at all was read: a stream where card 40 is
        # malformed should still import the other 39.
        if not report.contacts:
            raise VCardError("That file could not be read as vCards.") from exc

    if not report.contacts and not report.skipped:
        raise VCardError("That file does not contain any vCards.")
    return report


def _from_component(component: object) -> ParsedContact | None:
    name = getattr(component, "name", "")
    if str(name).upper() != "VCARD":
        return None

    def first(attr: str) -> object | None:
        return getattr(component, attr, None)

    def every(attr: str) -> list[object]:
        return list(getattr(component, attr, []) or [])

    given = family = None
    n_value = getattr(first("n"), "value", None)
    if n_value is not None:
        given = _clip(getattr(n_value, "given", "") or None, MAX_NAME_PART)
        family = _clip(getattr(n_value, "family", "") or None, MAX_NAME_PART)

    display = _clip(_text(first("fn")), MAX_DISPLAY_NAME)
    organisation = None
    org_prop = first("org")
    if org_prop is not None:
        value = getattr(org_prop, "value", None)
        if isinstance(value, list):
            organisation = _clip("; ".join(str(part) for part in value if part), MAX_ORG)
        elif isinstance(value, str):
            organisation = _clip(value, MAX_ORG)

    if not display:
        # A card with no FN is common enough from older exporters to be worth
        # rescuing rather than dropping: the name parts, or the organisation,
        # are a better answer than nothing.
        display = _clip(" ".join(part for part in (given, family) if part), MAX_DISPLAY_NAME)
    if not display:
        display = organisation
    if not display:
        return None

    contact = ParsedContact(
        display_name=display,
        given_name=given,
        family_name=family,
        organisation=organisation,
        job_title=_clip(_text(first("title")), MAX_TITLE),
        website=_clip(_text(first("url")), MAX_URL),
        notes=_clip(_text(first("note")), 10_000) or "",
    )

    for prop in every("categories_list"):
        value = getattr(prop, "value", None)
        parts = value if isinstance(value, list) else str(value or "").split(",")
        for part in parts:
            tag = _clip(str(part), 32)
            if tag and tag.lower() not in contact.tags:
                contact.tags.append(tag.lower())

    for prop in every("tel_list")[:MAX_ROWS_PER_KIND]:
        number = _clip(_text(prop), MAX_PHONE)
        if number:
            contact.phones.append(ParsedPhone(label=_label_of(prop), number=number))

    for prop in every("email_list")[:MAX_ROWS_PER_KIND]:
        address = _clip(_text(prop), MAX_EMAIL)
        if address:
            contact.emails.append(ParsedEmail(label=_label_of(prop), address=address))

    for prop in every("adr_list")[:MAX_ROWS_PER_KIND]:
        # `postal`, not `address`: the email loop above binds that name to a
        # string, and reusing it here makes the whole block the wrong type.
        postal = ParsedAddress(
            label=_label_of(prop),
            street=_clip(_part_of(prop, "street"), MAX_STREET),
            locality=_clip(_part_of(prop, "city"), MAX_LOCALITY),
            region=_clip(_part_of(prop, "region"), MAX_REGION),
            postcode=_clip(_part_of(prop, "code"), MAX_POSTCODE),
            country=_clip(_part_of(prop, "country"), MAX_COUNTRY),
        )
        if not postal.is_empty():
            contact.addresses.append(postal)

    return contact


def serialise(contacts: list[ParsedContact]) -> str:
    """Write contacts out as a vCard 3.0 stream.

    One card per contact, concatenated, which is what every phone and mail
    client expects a `.vcf` export to be.
    """
    return "".join(_to_card(contact) for contact in contacts)


def _to_card(contact: ParsedContact) -> str:
    card = vobject.vCard()

    card.add("fn").value = contact.display_name
    # N is required by 3.0 even when a contact is an organisation with no
    # person's name in it, so it is always written — empty parts and all.
    card.add("n").value = vobject.vcard.Name(
        family=contact.family_name or "", given=contact.given_name or ""
    )

    if contact.organisation:
        card.add("org").value = [contact.organisation]
    if contact.job_title:
        card.add("title").value = contact.job_title
    if contact.website:
        card.add("url").value = contact.website
    if contact.notes:
        card.add("note").value = contact.notes
    if contact.tags:
        card.add("categories").value = list(contact.tags)

    for phone in contact.phones:
        prop = card.add("tel")
        prop.value = phone.number
        if phone.label:
            prop.type_param = phone.label

    for email in contact.emails:
        prop = card.add("email")
        prop.value = email.address
        prop.type_param = email.label or "INTERNET"

    for address in contact.addresses:
        prop = card.add("adr")
        prop.value = vobject.vcard.Address(
            street=address.street or "",
            city=address.locality or "",
            region=address.region or "",
            code=address.postcode or "",
            country=address.country or "",
        )
        if address.label:
            prop.type_param = address.label

    return str(card.serialize())
