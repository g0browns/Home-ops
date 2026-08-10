"""Request and response shapes for the contact directory (SPEC §4.7)."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, BeforeValidator, Field, field_validator

from home_ops.modules.contacts.models import MAX_LABEL, MAX_ROWS_PER_KIND, MAX_TAG_LENGTH
from home_ops.policy import Visibility

Stripped = BeforeValidator(lambda value: value.strip() if isinstance(value, str) else value)

# NOTE: every length constraint below sits on the `str` side of its union, never
# on `str | None`. Written the other way pydantic tries to measure the length of
# None and raises a TypeError — a 500, not a 422 — the moment a client sends an
# explicit `null`. Omitting a field never runs the validator, which is why an
# editor that could not save anything once shipped with a green suite.
DisplayName = Annotated[str, Stripped, Field(min_length=1, max_length=200)]
NamePart = Annotated[str, Stripped, Field(max_length=100)]
Organisation = Annotated[str, Stripped, Field(max_length=200)]
JobTitle = Annotated[str, Stripped, Field(max_length=200)]
Website = Annotated[str, Stripped, Field(max_length=500)]
Notes = Annotated[str, Stripped, Field(max_length=10_000)]
Label = Annotated[str, Stripped, Field(max_length=MAX_LABEL)]
Tag = Annotated[str, Stripped, Field(min_length=1, max_length=MAX_TAG_LENGTH)]
PhoneNumber = Annotated[str, Stripped, Field(min_length=1, max_length=64)]
EmailAddress = Annotated[str, Stripped, Field(min_length=1, max_length=320)]


class PhoneIn(BaseModel):
    label: Label | None = None
    number: PhoneNumber


class PhoneOut(BaseModel):
    label: str | None
    number: str


class EmailIn(BaseModel):
    label: Label | None = None
    address: EmailAddress


class EmailOut(BaseModel):
    label: str | None
    address: str


class AddressIn(BaseModel):
    label: Label | None = None
    street: Annotated[str, Stripped, Field(max_length=200)] | None = None
    locality: Annotated[str, Stripped, Field(max_length=100)] | None = None
    region: Annotated[str, Stripped, Field(max_length=100)] | None = None
    postcode: Annotated[str, Stripped, Field(max_length=32)] | None = None
    country: Annotated[str, Stripped, Field(max_length=100)] | None = None


class AddressOut(BaseModel):
    label: str | None
    street: str | None
    locality: str | None
    region: str | None
    postcode: str | None
    country: str | None


class ContactSummary(BaseModel):
    """What the directory list needs. Phones and emails ride along because a
    directory that makes you open a record to see the number is a directory you
    stop using."""

    id: UUID
    display_name: str
    organisation: str | None
    job_title: str | None
    owner_id: UUID
    visibility: Visibility
    tags: list[str] = Field(default_factory=list)
    phones: list[PhoneOut] = Field(default_factory=list)
    emails: list[EmailOut] = Field(default_factory=list)


class ContactDetail(ContactSummary):
    given_name: str | None
    family_name: str | None
    website: str | None
    notes: str
    addresses: list[AddressOut] = Field(default_factory=list)


class ContactCreate(BaseModel):
    display_name: DisplayName
    given_name: NamePart | None = None
    family_name: NamePart | None = None
    organisation: Organisation | None = None
    job_title: JobTitle | None = None
    website: Website | None = None
    notes: Notes = ""
    visibility: Visibility = Visibility.HOUSEHOLD
    tags: Annotated[list[Tag], Field(max_length=20)] = Field(default_factory=list)
    phones: Annotated[list[PhoneIn], Field(max_length=MAX_ROWS_PER_KIND)] = Field(
        default_factory=list
    )
    emails: Annotated[list[EmailIn], Field(max_length=MAX_ROWS_PER_KIND)] = Field(
        default_factory=list
    )
    addresses: Annotated[list[AddressIn], Field(max_length=MAX_ROWS_PER_KIND)] = Field(
        default_factory=list
    )

    @field_validator("website")
    @classmethod
    def _http_only(cls, value: str | None) -> str | None:
        # A website is a link somebody will click. `javascript:` and `data:` are
        # not websites, they are payloads. Same rule as a recipe's source URL.
        if value and not value.startswith(("http://", "https://")):
            raise ValueError("A website must start with http:// or https://")
        return value


class ContactUpdate(BaseModel):
    """Every field optional; only what is supplied changes.

    The child collections are wholesale replacements when present. Absent means
    "leave them alone", which is why the route reads `exclude_unset` rather than
    these defaults.
    """

    display_name: DisplayName | None = None
    given_name: NamePart | None = None
    family_name: NamePart | None = None
    organisation: Organisation | None = None
    job_title: JobTitle | None = None
    website: Website | None = None
    notes: Notes | None = None
    visibility: Visibility | None = None
    tags: Annotated[list[Tag], Field(max_length=20)] | None = None
    phones: Annotated[list[PhoneIn], Field(max_length=MAX_ROWS_PER_KIND)] | None = None
    emails: Annotated[list[EmailIn], Field(max_length=MAX_ROWS_PER_KIND)] | None = None
    addresses: Annotated[list[AddressIn], Field(max_length=MAX_ROWS_PER_KIND)] | None = None

    @field_validator("website")
    @classmethod
    def _http_only(cls, value: str | None) -> str | None:
        if value and not value.startswith(("http://", "https://")):
            raise ValueError("A website must start with http:// or https://")
        return value


class ImportResult(BaseModel):
    """What an import did, or what it would do when previewing."""

    #: True when nothing was written — the preview.
    preview: bool
    #: Cards in the file we could read as a contact.
    found: int
    imported: int
    replaced: int
    skipped_existing: int
    #: Cards present that were not contacts we could read. Reported so "84 of
    #: 90" prompts a question rather than passing unnoticed.
    unreadable: int
    #: How many names already exist. Carried separately from the list below,
    #: which is truncated for display — reading a count off a truncated list is
    #: how the Mealie import once understated a clash.
    conflict_count: int
    conflicts: list[str] = Field(default_factory=list)
