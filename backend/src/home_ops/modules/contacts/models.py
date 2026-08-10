"""The contact directory (SPEC §4.7).

Names, phones, emails, addresses, notes and tags, with per-contact visibility.

**A contact is one row plus four child tables**, not one row with repeated
columns. §4.7 says "phones, emails, addresses" in the plural and a household
directory means it: a plumber has a mobile and a landline, a school has an
office address and a website. Two columns and a "phone2" is the shape that runs
out on the third one.

**Only `contacts` carries visibility**; the children inherit it, exactly as
shopping items inherit their list's. That means the children are **not**
registered with the scoping guard, so an unscoped SELECT of `contact_phones`
will not raise — they are only ever loaded through a scoped `contacts` query,
and `service.py` is the one place that happens.

**Tags are free text, settled with the owner on 2026-07-31.** Task categories
and calendars are household vocabulary behind `settings`; these are not. The
trade was made knowingly: "Plumber" and "plumbers" can both exist. What keeps
that survivable is the same treatment the notes board uses — stored lowercase,
with a CHECK that says so, and the tags already in use offered whenever one is
typed.

**Search is a column on `contacts`, not a separate index.** A search is then an
ordinary SELECT on a visibility-bearing table, so the scoping guard applies to
it and it cannot be bypassed by accident. The same reasoning is written out at
length in `notes/models.py`, and it matters more here: a directory is exactly
the kind of thing somebody would search for a name they should not be able to
find.
"""

from __future__ import annotations

import datetime as dt
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    Computed,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from home_ops.db import Base
from home_ops.scoping import OwnedVisibleMixin

#: Postgres text-search configuration, matching the notes board.
SEARCH_CONFIG = "english"

MAX_TAG_LENGTH = 32
MAX_LABEL = 32
#: Enough for a household directory; small enough that a paste cannot become a
#: denial of service.
MAX_ROWS_PER_KIND = 20


class ContactTag(Base):
    """One tag on one contact — plumber, doctor, school, family.

    A plain string per row rather than a tags table with a join, for the reason
    the notes board gives: a household has tens of tags, not thousands, and this
    keeps "rename a tag" a single UPDATE.
    """

    __tablename__ = "contact_tags"

    contact_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("contacts.id", ondelete="CASCADE"), primary_key=True
    )
    tag: Mapped[str] = mapped_column(String(MAX_TAG_LENGTH), primary_key=True)

    __table_args__ = (
        CheckConstraint("tag = lower(tag)", name="tag_is_lowercase"),
        CheckConstraint("length(trim(tag)) > 0", name="tag_not_blank"),
        Index("ix_contact_tags_tag", "tag"),
    )


class ContactPhone(Base):
    """One number. `label` is free text — "mobile", "out of hours", "reception"."""

    __tablename__ = "contact_phones"

    id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    contact_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False
    )
    label: Mapped[str | None] = mapped_column(String(MAX_LABEL), nullable=True)
    #: Stored exactly as written. A household directory holds "(555) 123-4567",
    #: "555-0142" and "x2214"; normalising them to E.164 would need a country
    #: assumption and would mangle the third.
    number: Mapped[str] = mapped_column(String(64), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))

    __table_args__ = (
        CheckConstraint("length(trim(number)) > 0", name="number_not_blank"),
        Index("ix_contact_phones_contact_id", "contact_id"),
    )


class ContactEmail(Base):
    __tablename__ = "contact_emails"

    id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    contact_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False
    )
    label: Mapped[str | None] = mapped_column(String(MAX_LABEL), nullable=True)
    address: Mapped[str] = mapped_column(String(320), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))

    __table_args__ = (
        CheckConstraint("length(trim(address)) > 0", name="address_not_blank"),
        Index("ix_contact_emails_contact_id", "contact_id"),
    )


class ContactAddress(Base):
    """A postal address, kept in parts rather than as one blob.

    The parts are vCard's own (`ADR`), so import and export do not have to guess
    where the city ends: `locality` is the city, `region` the state, `postcode`
    the ZIP. The UI reads them back as Street Address, City, State and Zip — the
    column names stay the RFC's so the import mapping stays obvious.

    They are all optional because half the addresses in a household directory
    are half-written, and one that refuses "Lincoln Elementary, Springfield" is
    a directory nobody fills in.
    """

    __tablename__ = "contact_addresses"

    id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    contact_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False
    )
    label: Mapped[str | None] = mapped_column(String(MAX_LABEL), nullable=True)
    street: Mapped[str | None] = mapped_column(String(200), nullable=True)
    locality: Mapped[str | None] = mapped_column(String(100), nullable=True)
    region: Mapped[str | None] = mapped_column(String(100), nullable=True)
    postcode: Mapped[str | None] = mapped_column(String(32), nullable=True)
    country: Mapped[str | None] = mapped_column(String(100), nullable=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))

    __table_args__ = (Index("ix_contact_addresses_contact_id", "contact_id"),)


class Contact(OwnedVisibleMixin, Base):
    """One contact. Carries `owner_id`, `visibility` and `created_at` from the mixin."""

    __tablename__ = "contacts"

    id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )

    #: What the directory shows and sorts by. vCard calls it FN and requires it.
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    #: The parts, kept alongside so a vCard round-trips without losing its `N`.
    #: Optional, because "Springfield Plumbing" has no first name and never will.
    given_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    family_name: Mapped[str | None] = mapped_column(String(100), nullable=True)

    organisation: Mapped[str | None] = mapped_column(String(200), nullable=True)
    job_title: Mapped[str | None] = mapped_column(String(200), nullable=True)
    website: Mapped[str | None] = mapped_column(String(500), nullable=True)
    notes: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))

    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    #: Generated by Postgres, so it can never drift from the row. The name is
    #: weighted above the organisation and both above the notes: looking
    #: somebody up by name is the common case, and a word buried in a note is a
    #: weaker signal than the same word in the name.
    search_vector: Mapped[str] = mapped_column(
        TSVECTOR,
        Computed(
            f"setweight(to_tsvector('{SEARCH_CONFIG}', coalesce(display_name, '')), 'A') || "
            f"setweight(to_tsvector('{SEARCH_CONFIG}', coalesce(organisation, '')), 'B') || "
            f"setweight(to_tsvector('{SEARCH_CONFIG}', coalesce(job_title, '')), 'C') || "
            f"setweight(to_tsvector('{SEARCH_CONFIG}', coalesce(notes, '')), 'D')",
            persisted=True,
        ),
        nullable=False,
    )

    tags: Mapped[list[ContactTag]] = relationship(
        cascade="all, delete-orphan", passive_deletes=True, lazy="selectin"
    )
    phones: Mapped[list[ContactPhone]] = relationship(
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="selectin",
        order_by="ContactPhone.position",
    )
    emails: Mapped[list[ContactEmail]] = relationship(
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="selectin",
        order_by="ContactEmail.position",
    )
    addresses: Mapped[list[ContactAddress]] = relationship(
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="selectin",
        order_by="ContactAddress.position",
    )

    __table_args__ = (
        CheckConstraint("length(trim(display_name)) > 0", name="display_name_not_blank"),
        Index("ix_contacts_search_vector", "search_vector", postgresql_using="gin"),
        Index("ix_contacts_owner_id", "owner_id"),
        Index("ix_contacts_display_name", func.lower(display_name)),
    )
