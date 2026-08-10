"""tasks, notes, categories and tags

Revision ID: 56993d02d3d8
Revises: a9151341aa97
Created: 2026-07-30 17:40:40.776062+00:00

Phase 3 (Tasks + Notes). Creates the tables behind SPEC §4.4 and §4.5:

  task_categories   household vocabulary for filing tasks
  tasks             the first visibility-bearing table (owner_id + visibility)
  task_assignments  multi-member assignment, a join table from day one
  notes             markdown, pinning, and a generated full-text search vector
  note_tags         one row per tag per note

Two things autogenerate could not see, added by hand:

* **The `access_permissions.module` CHECK.** `policy.Module` grew by two
  members, and the old constraint would reject a deviation for either. Alembic
  does not diff check constraints, so this is exactly the visibility the
  "enums are TEXT + CHECK, not Postgres ENUM" decision was chosen to give — the
  widening is a reviewable line in a migration rather than an invisible
  `ALTER TYPE`.

* **`uq_tasks_one_live_instance_per_series`.** A partial unique index that makes
  the recurrence model chosen for §4.4 — one open instance at a time — a
  database guarantee rather than something the service layer has to remember.

Reversible: `downgrade()` drops all five tables and narrows the CHECK back.
Note that narrowing would fail if a `tasks` or `notes` deviation existed, so the
downgrade deletes those rows first — losing a permission override on a module
that is itself being removed, which is the only sensible reading.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "56993d02d3d8"
down_revision: str | None = "a9151341aa97"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "task_categories",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("color_key", sa.String(length=32), nullable=True),
        sa.Column("position", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "length(trim(name)) > 0", name=op.f("ck_task_categories_name_not_blank")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_task_categories")),
    )
    op.create_index(
        "uq_task_categories_name",
        "task_categories",
        [sa.literal_column("lower(name)")],
        unique=True,
    )
    op.create_table(
        "notes",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("body", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("color_key", sa.String(length=32), nullable=True),
        sa.Column("is_pinned", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "search_vector",
            postgresql.TSVECTOR(),
            sa.Computed(
                "setweight(to_tsvector('english', coalesce(title, '')), 'A') || setweight(to_tsvector('english', coalesce(body, '')), 'B')",
                persisted=True,
            ),
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
        sa.CheckConstraint("length(trim(title)) > 0", name=op.f("ck_notes_title_not_blank")),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["users.id"], name=op.f("fk_notes_owner_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notes")),
    )
    op.create_index("ix_notes_owner_id", "notes", ["owner_id"], unique=False)
    op.create_index("ix_notes_pinned_updated", "notes", ["is_pinned", "updated_at"], unique=False)
    op.create_index(
        "ix_notes_search_vector", "notes", ["search_vector"], unique=False, postgresql_using="gin"
    )
    op.create_table(
        "tasks",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("category_id", sa.UUID(), nullable=True),
        sa.Column(
            "priority", sa.String(length=16), server_default=sa.text("'none'"), nullable=False
        ),
        sa.Column("status", sa.String(length=16), server_default=sa.text("'open'"), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("due_is_all_day", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("parent_task_id", sa.UUID(), nullable=True),
        sa.Column("recurrence_rule", sa.Text(), nullable=True),
        sa.Column("recurrence_group_id", sa.UUID(), nullable=True),
        sa.Column("recurrence_start_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_by_user_id", sa.UUID(), nullable=True),
        sa.Column("position", sa.Integer(), server_default=sa.text("0"), nullable=False),
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
            "priority IN ('none', 'low', 'medium', 'high', 'urgent')",
            name=op.f("ck_tasks_priority_is_known"),
        ),
        sa.CheckConstraint(
            "status <> 'open' OR completed_at IS NULL",
            name=op.f("ck_tasks_open_tasks_are_not_completed"),
        ),
        sa.CheckConstraint(
            "status IN ('open', 'in_progress', 'done', 'archived')",
            name=op.f("ck_tasks_status_is_known"),
        ),
        sa.CheckConstraint("length(trim(title)) > 0", name=op.f("ck_tasks_title_not_blank")),
        sa.CheckConstraint(
            "parent_task_id IS NULL OR parent_task_id <> id", name=op.f("ck_tasks_no_self_parent")
        ),
        sa.CheckConstraint(
            "recurrence_rule IS NULL OR recurrence_group_id IS NOT NULL",
            name=op.f("ck_tasks_recurring_tasks_have_a_group"),
        ),
        sa.ForeignKeyConstraint(
            ["category_id"],
            ["task_categories.id"],
            name=op.f("fk_tasks_category_id_task_categories"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["completed_by_user_id"],
            ["users.id"],
            name=op.f("fk_tasks_completed_by_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["users.id"], name=op.f("fk_tasks_owner_id_users"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["parent_task_id"],
            ["tasks.id"],
            name=op.f("fk_tasks_parent_task_id_tasks"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tasks")),
    )
    op.create_index("ix_tasks_category_id", "tasks", ["category_id"], unique=False)
    op.create_index("ix_tasks_owner_id", "tasks", ["owner_id"], unique=False)
    op.create_index("ix_tasks_parent_task_id", "tasks", ["parent_task_id"], unique=False)
    op.create_index("ix_tasks_recurrence_group_id", "tasks", ["recurrence_group_id"], unique=False)
    op.create_index("ix_tasks_status_due_at", "tasks", ["status", "due_at"], unique=False)
    op.create_index(
        "uq_tasks_one_live_instance_per_series",
        "tasks",
        ["recurrence_group_id"],
        unique=True,
        postgresql_where=sa.text(
            "recurrence_group_id IS NOT NULL AND status IN ('open', 'in_progress')"
        ),
    )
    op.create_table(
        "note_tags",
        sa.Column("note_id", sa.UUID(), nullable=False),
        sa.Column("tag", sa.String(length=32), nullable=False),
        sa.CheckConstraint("length(trim(tag)) > 0", name=op.f("ck_note_tags_tag_not_blank")),
        sa.CheckConstraint("tag = lower(tag)", name=op.f("ck_note_tags_tag_is_lowercase")),
        sa.ForeignKeyConstraint(
            ["note_id"], ["notes.id"], name=op.f("fk_note_tags_note_id_notes"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("note_id", "tag", name=op.f("pk_note_tags")),
    )
    op.create_index("ix_note_tags_tag", "note_tags", ["tag"], unique=False)
    op.create_table(
        "task_assignments",
        sa.Column("task_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column(
            "assigned_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["tasks.id"],
            name=op.f("fk_task_assignments_task_id_tasks"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_task_assignments_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("task_id", "user_id", name=op.f("pk_task_assignments")),
    )
    op.create_index("ix_task_assignments_user_id", "task_assignments", ["user_id"], unique=False)


    # policy.Module gained TASKS and NOTES; the old CHECK would reject a
    # deviation for either. Autogenerate does not diff check constraints.
    op.drop_constraint("module_is_known", "access_permissions", type_="check")
    op.create_check_constraint(
        "module_is_known",
        "access_permissions",
        "module IN ('users', 'settings', 'audit', 'tasks', 'notes')",
    )


def downgrade() -> None:
    # Narrow the CHECK back. Any deviation naming a module that no longer
    # exists has to go first, or the constraint cannot be created.
    op.execute(
        "DELETE FROM access_permissions WHERE module IN ('tasks', 'notes')"
    )
    op.drop_constraint("module_is_known", "access_permissions", type_="check")
    op.create_check_constraint(
        "module_is_known",
        "access_permissions",
        "module IN ('users', 'settings', 'audit')",
    )

    op.drop_index("ix_task_assignments_user_id", table_name="task_assignments")
    op.drop_table("task_assignments")
    op.drop_index("ix_note_tags_tag", table_name="note_tags")
    op.drop_table("note_tags")
    op.drop_index(
        "uq_tasks_one_live_instance_per_series",
        table_name="tasks",
        postgresql_where=sa.text(
            "recurrence_group_id IS NOT NULL AND status IN ('open', 'in_progress')"
        ),
    )
    op.drop_index("ix_tasks_status_due_at", table_name="tasks")
    op.drop_index("ix_tasks_recurrence_group_id", table_name="tasks")
    op.drop_index("ix_tasks_parent_task_id", table_name="tasks")
    op.drop_index("ix_tasks_owner_id", table_name="tasks")
    op.drop_index("ix_tasks_category_id", table_name="tasks")
    op.drop_table("tasks")
    op.drop_index("ix_notes_search_vector", table_name="notes", postgresql_using="gin")
    op.drop_index("ix_notes_pinned_updated", table_name="notes")
    op.drop_index("ix_notes_owner_id", table_name="notes")
    op.drop_table("notes")
    op.drop_index("uq_task_categories_name", table_name="task_categories")
    op.drop_table("task_categories")
