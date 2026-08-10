"""calendars, events and recurrence

Revision ID: 31c70e123e67
Revises: 3bef875c328a
Created: 2026-07-30 19:35:27.760876+00:00

Phase 4a (Calendar). Local events and recurrence; CalDAV sync follows in 4b.

  calendars                    named calendars, exactly one marked default
  calendar_events              masters and detached instances in one table
  calendar_event_exceptions    EXDATE — one deleted occurrence per row
  calendar_event_assignments   who an event is assigned to

The recurrence shape is RFC 5545's rather than a private one, because CalDAV
has to speak it in the next sub-phase and a bespoke model would only need
translating: a master carries the RRULE, a detached instance overrides one
occurrence via (series_id, recurrence_id), and an exception row deletes one.

Hand-written additions autogenerate cannot see:

* **The `access_permissions.module` CHECK**, widened again for `calendar`.
  Alembic does not diff check constraints. This is the third time, and it is
  exactly the reviewable line the "TEXT + CHECK, never Postgres ENUM" decision
  was chosen to produce.
* **`uq_calendars_single_default`**, a partial unique index so a household
  cannot end up with two default calendars and new events landing at random.

Reversible: `downgrade()` drops the four tables and narrows the CHECK, deleting
any deviation naming a module that no longer exists.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "31c70e123e67"
down_revision: str | None = "3bef875c328a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "calendars",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("color_key", sa.String(length=32), nullable=True),
        sa.Column("is_default", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("position", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("length(trim(name)) > 0", name=op.f("ck_calendars_name_not_blank")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_calendars")),
    )
    op.create_index(
        "uq_calendars_name", "calendars", [sa.literal_column("lower(name)")], unique=True
    )
    op.create_index(
        "uq_calendars_single_default",
        "calendars",
        ["is_default"],
        unique=True,
        postgresql_where=sa.text("is_default"),
    )
    op.create_table(
        "calendar_events",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("calendar_id", sa.UUID(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("location", sa.String(length=255), nullable=True),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("is_all_day", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("tzid", sa.String(length=64), server_default=sa.text("'UTC'"), nullable=False),
        sa.Column("recurrence_rule", sa.Text(), nullable=True),
        sa.Column("series_id", sa.UUID(), nullable=True),
        sa.Column("recurrence_id", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("owner_id", sa.UUID(), nullable=False),
        sa.Column("visibility", sa.String(length=16), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(series_id IS NULL) = (recurrence_id IS NULL)",
            name=op.f("ck_calendar_events_detached_instances_are_fully_identified"),
        ),
        sa.CheckConstraint(
            "ends_at >= starts_at", name=op.f("ck_calendar_events_ends_after_it_starts")
        ),
        sa.CheckConstraint(
            "length(trim(title)) > 0", name=op.f("ck_calendar_events_title_not_blank")
        ),
        sa.CheckConstraint(
            "series_id IS NULL OR recurrence_rule IS NULL",
            name=op.f("ck_calendar_events_detached_instances_do_not_recur"),
        ),
        sa.ForeignKeyConstraint(
            ["calendar_id"],
            ["calendars.id"],
            name=op.f("fk_calendar_events_calendar_id_calendars"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"],
            ["users.id"],
            name=op.f("fk_calendar_events_owner_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["series_id"],
            ["calendar_events.id"],
            name=op.f("fk_calendar_events_series_id_calendar_events"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_calendar_events")),
        sa.UniqueConstraint("series_id", "recurrence_id", name="uq_event_override_occurrence"),
    )
    op.create_index(
        "ix_calendar_events_calendar_id", "calendar_events", ["calendar_id"], unique=False
    )
    op.create_index("ix_calendar_events_owner_id", "calendar_events", ["owner_id"], unique=False)
    op.create_index("ix_calendar_events_series_id", "calendar_events", ["series_id"], unique=False)
    op.create_index(
        "ix_calendar_events_window", "calendar_events", ["starts_at", "ends_at"], unique=False
    )
    op.create_table(
        "calendar_event_assignments",
        sa.Column("event_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["calendar_events.id"],
            name=op.f("fk_calendar_event_assignments_event_id_calendar_events"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_calendar_event_assignments_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("event_id", "user_id", name=op.f("pk_calendar_event_assignments")),
    )
    op.create_index(
        "ix_calendar_event_assignments_user_id",
        "calendar_event_assignments",
        ["user_id"],
        unique=False,
    )
    op.create_table(
        "calendar_event_exceptions",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("event_id", sa.UUID(), nullable=False),
        sa.Column("occurrence_start", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["calendar_events.id"],
            name=op.f("fk_calendar_event_exceptions_event_id_calendar_events"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_calendar_event_exceptions")),
        sa.UniqueConstraint("event_id", "occurrence_start", name="uq_event_exception_occurrence"),
    )

    # policy.Module gained CALENDAR; the old CHECK would reject a deviation for
    # it. Autogenerate does not diff check constraints.
    op.drop_constraint("module_is_known", "access_permissions", type_="check")
    op.create_check_constraint(
        "module_is_known", "access_permissions", "module IN ('users', 'settings', 'audit', 'tasks', 'notes', 'calendar')"
    )


def downgrade() -> None:
    op.execute("DELETE FROM access_permissions WHERE module = 'calendar'")
    op.drop_constraint("module_is_known", "access_permissions", type_="check")
    op.create_check_constraint(
        "module_is_known", "access_permissions", "module IN ('users', 'settings', 'audit', 'tasks', 'notes')"
    )

    op.drop_table("calendar_event_exceptions")
    op.drop_index("ix_calendar_event_assignments_user_id", table_name="calendar_event_assignments")
    op.drop_table("calendar_event_assignments")
    op.drop_index("ix_calendar_events_window", table_name="calendar_events")
    op.drop_index("ix_calendar_events_series_id", table_name="calendar_events")
    op.drop_index("ix_calendar_events_owner_id", table_name="calendar_events")
    op.drop_index("ix_calendar_events_calendar_id", table_name="calendar_events")
    op.drop_table("calendar_events")
    op.drop_index(
        "uq_calendars_single_default",
        table_name="calendars",
        postgresql_where=sa.text("is_default"),
    )
    op.drop_index("uq_calendars_name", table_name="calendars")
    op.drop_table("calendars")
