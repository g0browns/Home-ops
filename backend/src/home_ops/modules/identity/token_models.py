"""API tokens (SPEC §4.10).

**A token is a second way to be a principal**, so everything the session path
gets right has to be got right again here rather than assumed.

**Stored as a SHA-256 hash, never in the clear.** Exactly what sessions do, and
for the same reason: a `pg_dump` of this table, or a backup file, must not be a
set of working credentials. The plaintext exists once, in the response that
creates it, and is never retrievable again.

**`prefix` is for humans, not for authentication.** Eight characters of the
token, stored plainly so the UI can say *which* token — "ho_8f3a…" — in a list
where every other identifying detail is a hash. Short enough to be useless on
its own.

**A scope is a ceiling, never a grant.** `ApiTokenScope` records the most a
token may do per module, and the effective permission at request time is the
*lesser* of that and what its owner can do **now**. Two consequences that are
the whole reason it works this way:

* Demoting somebody, or taking a module off them, immediately narrows every
  token they hold. A token that kept yesterday's permissions would be a way to
  keep access after it was removed.
* An **admin's** token is capped too. `policy.can` lets admins bypass module
  access, so the cap has to be applied *after* that bypass or a token belonging
  to an admin would silently ignore its own scope — which is precisely the
  token you would most want narrowed.

A token with no scope rows means "everything its owner can do", which is the
honest reading of an unnarrowed token and the default the UI offers last.

**`expires_at` is nullable and the UI should push against that.** A token with
no expiry is a credential that outlives whoever remembers issuing it; §4.10 does
not require expiry, so this stores it rather than insisting on it, and the
decision is recorded here rather than hidden in a form default.
"""

from __future__ import annotations

import datetime as dt
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from home_ops.db import Base

#: What the plaintext token starts with, so one found in a log or a paste is
#: recognisable as this application's credential and can be revoked rather than
#: puzzled over.
TOKEN_PREFIX = "ho_"  # noqa: S105 - a label on the credential, not a credential

#: Characters of the token kept in the clear for display. Enough to tell two
#: tokens apart in a list, far too few to help anybody guess the rest.
PREFIX_LENGTH = 8

MAX_TOKEN_NAME = 60
#: Enough for a household's integrations; small enough that a runaway script
#: cannot fill the table.
MAX_TOKENS_PER_USER = 25


class ApiToken(Base):
    """One bearer credential belonging to one member."""

    __tablename__ = "api_tokens"

    id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )

    #: What it is for, in the owner's words — "Home Assistant", "the shopping
    #: script". A token nobody can identify is a token nobody dares revoke.
    name: Mapped[str] = mapped_column(String(MAX_TOKEN_NAME), nullable=False)

    #: SHA-256 of the plaintext. Unique so a lookup is an index hit rather than
    #: a scan, and so two tokens cannot collide unnoticed.
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    #: The first few characters, in the clear, for display only.
    prefix: Mapped[str] = mapped_column(String(16), nullable=False)

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    #: §4.10 asks for last-used in the UI. Null until it is first used, which is
    #: itself worth showing: a token that has never been used is a token that
    #: can be revoked without asking anybody.
    last_used_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    #: The fixed rate-limit window this token is currently inside, and how many
    #: requests it has spent. Kept on the token row rather than in a counter
    #: table: the row is already written on every request to stamp
    #: `last_used_at`, so this costs nothing extra and cannot drift out of step
    #: with the thing it is limiting.
    window_started_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    window_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    #: Revoked rather than deleted, so the audit trail and the last-used record
    #: survive the revocation. A revoked token authenticates nothing.
    revoked_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    scopes: Mapped[list[ApiTokenScope]] = relationship(
        cascade="all, delete-orphan", passive_deletes=True, lazy="selectin"
    )

    __table_args__ = (
        CheckConstraint("length(trim(name)) > 0", name="name_not_blank"),
        Index("ix_api_tokens_user_id", "user_id"),
        Index("ix_api_tokens_token_hash", "token_hash"),
    )


class ApiTokenScope(Base):
    """The most one token may do in one module.

    A ceiling, not a grant: see the module docstring. Absent rows mean the token
    is not narrowed at all, so the owner's own permissions are the only limit.
    """

    __tablename__ = "api_token_scopes"

    token_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("api_tokens.id", ondelete="CASCADE"), primary_key=True
    )
    module: Mapped[str] = mapped_column(String(32), primary_key=True)
    access: Mapped[str] = mapped_column(String(16), nullable=False)

    __table_args__ = (
        CheckConstraint("access IN ('none', 'read', 'write')", name="access_is_known"),
    )
