"""identity, permissions, audit log, settings

Revision ID: a9151341aa97
Revises: 0001_baseline
Created: 2026-07-30 16:10:53.368555+00:00

Phase 1 (Identity). Creates the tables behind SPEC §4.1, §4.2 and §4.9:

  users               household members, with nullable OIDC columns so §4.1's
                      "don't design it out" costs nothing later
  sessions            server-side sessions; only token *hashes* are stored
  access_permissions  sparse deviations from the role defaults in policy.py
  audit_log           append-only security events
  auth_attempts       short-lived, for login rate limiting and lockout
  household_settings  shared configuration
  user_settings       per-member preferences

Two things worth noting for anyone reading this later:

* The CHECK constraints on role/module/access/subject_type are generated from
  the same Python enums the application uses, so the database and the code
  cannot drift. Adding an enum member means a migration that redefines the
  check — which is exactly the visibility we want.
* No Postgres ENUM types. They are the least reversible construct in the
  database, and SPEC §0 requires every migration to be reversible.

Reversible: `downgrade()` drops all seven tables and their indexes. It is
exercised on every test run by tests/test_migrations.py.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a9151341aa97"
down_revision: str | None = "0001_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "access_permissions",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("subject_type", sa.String(length=16), nullable=False),
        sa.Column("subject_id", sa.String(length=64), nullable=False),
        sa.Column("module", sa.String(length=32), nullable=False),
        sa.Column("access", sa.String(length=16), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "access IN ('none', 'read', 'write')",
            name=op.f("ck_access_permissions_access_is_known"),
        ),
        sa.CheckConstraint(
            "module IN ('users', 'settings', 'audit')",
            name=op.f("ck_access_permissions_module_is_known"),
        ),
        sa.CheckConstraint(
            "subject_type IN ('role', 'user')",
            name=op.f("ck_access_permissions_subject_type_is_known"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_access_permissions")),
        sa.UniqueConstraint(
            "subject_type", "subject_id", "module", name="uq_access_permissions_subject_module"
        ),
    )
    op.create_table(
        "auth_attempts",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("client_ip", sa.String(length=45), nullable=False),
        sa.Column("succeeded", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_auth_attempts")),
    )
    op.create_index(
        "ix_auth_attempts_client_ip_at", "auth_attempts", ["client_ip", "at"], unique=False
    )
    op.create_index(
        "ix_auth_attempts_username_at", "auth_attempts", ["username", "at"], unique=False
    )
    op.create_table(
        "users",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.String(length=128), nullable=False),
        sa.Column("avatar_color", sa.String(length=32), nullable=True),
        sa.Column("password_hash", sa.Text(), nullable=True),
        sa.Column("oidc_subject", sa.String(length=255), nullable=True),
        sa.Column("oidc_provider", sa.String(length=64), nullable=True),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "role IN ('admin', 'adult', 'limited', 'readonly')", name=op.f("ck_users_role_is_known")
        ),
        sa.CheckConstraint(
            "(oidc_subject IS NULL) = (oidc_provider IS NULL)",
            name=op.f("ck_users_oidc_fields_are_paired"),
        ),
        sa.CheckConstraint(
            "length(trim(display_name)) > 0", name=op.f("ck_users_display_name_not_blank")
        ),
        sa.CheckConstraint("length(username) >= 2", name=op.f("ck_users_username_min_length")),
        sa.CheckConstraint(
            "password_hash IS NOT NULL OR oidc_subject IS NOT NULL",
            name=op.f("ck_users_has_an_authentication_method"),
        ),
        sa.CheckConstraint(
            "username = lower(username)", name=op.f("ck_users_username_is_lowercase")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("username", name=op.f("uq_users_username")),
    )
    op.create_index(
        "uq_users_oidc_identity",
        "users",
        ["oidc_provider", "oidc_subject"],
        unique=True,
        postgresql_where=sa.text("oidc_subject IS NOT NULL"),
    )
    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("actor_user_id", sa.UUID(), nullable=True),
        sa.Column("actor_label", sa.String(length=128), nullable=True),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("resource_type", sa.String(length=64), nullable=True),
        sa.Column("resource_id", sa.String(length=64), nullable=True),
        sa.Column("client_ip", sa.String(length=45), nullable=True),
        sa.Column(
            "detail",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            ["users.id"],
            name=op.f("fk_audit_log_actor_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_log")),
    )
    op.create_index("ix_audit_log_action", "audit_log", ["action"], unique=False)
    op.create_index("ix_audit_log_actor_user_id", "audit_log", ["actor_user_id"], unique=False)
    op.create_index("ix_audit_log_at", "audit_log", ["at"], unique=False)
    op.create_table(
        "household_settings",
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("value", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("updated_by_user_id", sa.UUID(), nullable=True),
        sa.ForeignKeyConstraint(
            ["updated_by_user_id"],
            ["users.id"],
            name=op.f("fk_household_settings_updated_by_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_household_settings")),
    )
    op.create_table(
        "sessions",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("csrf_token_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("user_agent", sa.String(length=255), nullable=True),
        sa.Column("created_ip", sa.String(length=45), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_sessions_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sessions")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_sessions_token_hash")),
    )
    op.create_index("ix_sessions_expires_at", "sessions", ["expires_at"], unique=False)
    op.create_index("ix_sessions_user_id", "sessions", ["user_id"], unique=False)
    op.create_table(
        "user_settings",
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("value", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_user_settings_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("user_id", "key", name=op.f("pk_user_settings")),
    )


def downgrade() -> None:
    op.drop_table("user_settings")
    op.drop_index("ix_sessions_user_id", table_name="sessions")
    op.drop_index("ix_sessions_expires_at", table_name="sessions")
    op.drop_table("sessions")
    op.drop_table("household_settings")
    op.drop_index("ix_audit_log_at", table_name="audit_log")
    op.drop_index("ix_audit_log_actor_user_id", table_name="audit_log")
    op.drop_index("ix_audit_log_action", table_name="audit_log")
    op.drop_table("audit_log")
    op.drop_index(
        "uq_users_oidc_identity",
        table_name="users",
        postgresql_where=sa.text("oidc_subject IS NOT NULL"),
    )
    op.drop_table("users")
    op.drop_index("ix_auth_attempts_username_at", table_name="auth_attempts")
    op.drop_index("ix_auth_attempts_client_ip_at", table_name="auth_attempts")
    op.drop_table("auth_attempts")
    op.drop_table("access_permissions")
