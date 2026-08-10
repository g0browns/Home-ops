"""Identity tables: users, sessions, permission deviations, audit log (SPEC §4.1, §4.2).

Two schema conventions worth stating once, since every later module follows them:

**Enums are `TEXT` with a `CHECK`, not Postgres `ENUM` types.** SPEC §0 requires
every migration to be reversible, and native enum types are the least reversible
thing in Postgres — `ALTER TYPE ... ADD VALUE` cannot be undone, and removing a
value means rebuilding the type and every column using it. A check constraint is
dropped and recreated trivially.

**Timestamps are `TIMESTAMP WITH TIME ZONE`, always.** A household calendar that
spans DST changes cannot afford naive timestamps, and getting this wrong is
expensive to fix once there is data.
"""

from __future__ import annotations

import datetime as dt
from enum import StrEnum
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from home_ops.db import Base
from home_ops.policy import Access, Module, Role, SubjectType


def _in_values(column: str, enum_cls: type[StrEnum]) -> str:
    """Render a CHECK expression restricting a column to an enum's members.

    Generated from the same enum the application uses, so the constraint and the
    code cannot drift. Values are enum members, never user input.
    """
    rendered = ", ".join(f"'{member.value}'" for member in enum_cls)
    return f"{column} IN ({rendered})"


def uuid_pk() -> Mapped[UUID]:
    return mapped_column(
        PgUUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )


def created_at_column() -> Mapped[dt.datetime]:
    return mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class User(Base):
    """A household member (SPEC §4.2 — members are full users, not contacts)."""

    __tablename__ = "users"

    id: Mapped[UUID] = uuid_pk()

    # Stored lower-cased so lookups are case-insensitive without a functional
    # index or the citext extension. `display_name` carries the presentation
    # form, so nothing is lost.
    username: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)

    # A palette key, not a hex value — SPEC §6 forbids hardcoded colours in
    # components, and the palette itself is not decided until Phase 2.
    avatar_color: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # Nullable so an OIDC-only account is possible later without a migration.
    # Today every account has one; the CHECK below enforces that an account
    # always has *some* way to authenticate.
    password_hash: Mapped[str | None] = mapped_column(Text, nullable=True)

    # SPEC §4.1: "not now, but don't design it out". No OIDC code exists yet.
    oidc_subject: Mapped[str | None] = mapped_column(String(255), nullable=True)
    oidc_provider: Mapped[str | None] = mapped_column(String(64), nullable=True)

    role: Mapped[str] = mapped_column(String(16), nullable=False, default=Role.ADULT.value)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))

    created_at: Mapped[dt.datetime] = created_at_column()
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    sessions: Mapped[list[Session]] = relationship(
        back_populates="user", cascade="all, delete-orphan", passive_deletes=True
    )

    __table_args__ = (
        CheckConstraint(_in_values("role", Role), name="role_is_known"),
        CheckConstraint("username = lower(username)", name="username_is_lowercase"),
        CheckConstraint("length(username) >= 2", name="username_min_length"),
        CheckConstraint("length(trim(display_name)) > 0", name="display_name_not_blank"),
        # An account with neither a password nor a federated identity could
        # never be logged into, and would look like a working account.
        CheckConstraint(
            "password_hash IS NOT NULL OR oidc_subject IS NOT NULL",
            name="has_an_authentication_method",
        ),
        # Both OIDC columns are set together or not at all.
        CheckConstraint(
            "(oidc_subject IS NULL) = (oidc_provider IS NULL)",
            name="oidc_fields_are_paired",
        ),
        # Partial unique index, as Yuvomi has it: uniqueness applies only to
        # rows that actually carry a federated identity.
        Index(
            "uq_users_oidc_identity",
            "oidc_provider",
            "oidc_subject",
            unique=True,
            postgresql_where=text("oidc_subject IS NOT NULL"),
        ),
    )


class Session(Base):
    """A logged-in session (SPEC §4.1).

    The token itself is never stored — only its SHA-256. A database dump or a
    `pg_dump` backup therefore contains no usable session.
    """

    __tablename__ = "sessions"

    id: Mapped[UUID] = uuid_pk()
    user_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )

    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    # CSRF is bound to the session rather than being a bare double-submit, so a
    # token lifted from one session cannot be replayed against another.
    csrf_token_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    created_at: Mapped[dt.datetime] = created_at_column()
    last_seen_at: Mapped[dt.datetime] = created_at_column()
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # Recorded so a user can recognise their own sessions in a future UI.
    user_agent: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_ip: Mapped[str | None] = mapped_column(String(45), nullable=True)

    user: Mapped[User] = relationship(back_populates="sessions")

    __table_args__ = (
        Index("ix_sessions_user_id", "user_id"),
        # Expiry sweeps scan this.
        Index("ix_sessions_expires_at", "expires_at"),
    )


class AccessPermission(Base):
    """A stored deviation from the role defaults in `policy.ROLE_DEFAULTS`.

    Sparse on purpose (the shape Yuvomi uses): the defaults stay readable in
    code, and only genuine exceptions become rows.
    """

    __tablename__ = "access_permissions"

    id: Mapped[UUID] = uuid_pk()

    subject_type: Mapped[str] = mapped_column(String(16), nullable=False)
    # A role name or a user id, per subject_type. Text rather than a foreign key
    # because it addresses two different things.
    subject_id: Mapped[str] = mapped_column(String(64), nullable=False)

    module: Mapped[str] = mapped_column(String(32), nullable=False)
    access: Mapped[str] = mapped_column(String(16), nullable=False)

    created_at: Mapped[dt.datetime] = created_at_column()
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "subject_type", "subject_id", "module", name="uq_access_permissions_subject_module"
        ),
        CheckConstraint(_in_values("subject_type", SubjectType), name="subject_type_is_known"),
        CheckConstraint(_in_values("module", Module), name="module_is_known"),
        CheckConstraint(_in_values("access", Access), name="access_is_known"),
    )


class AuditLogEntry(Base):
    """Append-only record of security-relevant events (SPEC §4.1).

    Covers logins, permission changes, data exports, and deletions. Nothing in
    the application updates or deletes rows here.
    """

    __tablename__ = "audit_log"

    # BigInteger identity rather than a UUID: this table is append-only and will
    # be by far the largest, and a monotonic key keeps inserts and time-ordered
    # reads on the happy path.
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    at: Mapped[dt.datetime] = created_at_column()

    # SET NULL, not CASCADE: deleting a user must not erase the record of what
    # they did. `actor_label` preserves who it was.
    actor_user_id: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    actor_label: Mapped[str | None] = mapped_column(String(128), nullable=True)

    action: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    resource_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    client_ip: Mapped[str | None] = mapped_column(String(45), nullable=True)

    # Never put credentials, tokens, or health values in here — see audit.py.
    detail: Mapped[dict[str, object]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )

    # A plain B-tree serves descending scans too, so no DESC index is needed for
    # "most recent first".
    __table_args__ = (
        Index("ix_audit_log_at", "at"),
        Index("ix_audit_log_actor_user_id", "actor_user_id"),
        Index("ix_audit_log_action", "action"),
    )


class AuthAttempt(Base):
    """Login attempts, for rate limiting and lockout (SPEC §4.1).

    Kept separate from the audit log: this table is swept and pruned on a short
    horizon, and the audit log is not.
    """

    __tablename__ = "auth_attempts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    at: Mapped[dt.datetime] = created_at_column()

    # Recorded even when no such user exists, so username enumeration attempts
    # are rate limited too. Lower-cased to match `users.username`.
    username: Mapped[str] = mapped_column(String(64), nullable=False)
    client_ip: Mapped[str] = mapped_column(String(45), nullable=False)
    succeeded: Mapped[bool] = mapped_column(Boolean, nullable=False)

    __table_args__ = (
        Index("ix_auth_attempts_username_at", "username", "at"),
        Index("ix_auth_attempts_client_ip_at", "client_ip", "at"),
    )
