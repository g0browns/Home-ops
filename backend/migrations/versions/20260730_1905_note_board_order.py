"""note board order

Revision ID: 3bef875c328a
Revises: 56993d02d3d8
Created: 2026-07-30 19:05:50.837667+00:00

Manual board order for notes.

`position` is shared across the household rather than per-member, which is the
same choice already made for `is_pinned`: this is one noticeboard, and a note
somebody moved to the front should be at the front for everybody.

Existing notes all default to 0, so the board keeps its current
most-recently-touched order until somebody actually drags something.

Reversible: `downgrade()` drops the column and restores the previous index.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "3bef875c328a"
down_revision: str | None = "56993d02d3d8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "notes", sa.Column("position", sa.Integer(), server_default=sa.text("0"), nullable=False)
    )
    op.drop_index(op.f("ix_notes_pinned_updated"), table_name="notes")
    op.create_index(
        "ix_notes_board_order", "notes", ["is_pinned", "position", "updated_at"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_notes_board_order", table_name="notes")
    op.create_index(
        op.f("ix_notes_pinned_updated"), "notes", ["is_pinned", "updated_at"], unique=False
    )
    op.drop_column("notes", "position")
