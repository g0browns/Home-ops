"""baseline

Revision ID: 0001_baseline
Revises: none
Created: 2026-07-30

The empty root of the migration tree.

Phase 0 is foundation only (SPEC §5) and defines no domain tables, so there is
genuinely nothing to create here. Inventing a table to make this file look busy
would be exactly the future-phase scaffolding SPEC §0 rules out.

Its job is to exist: it establishes `alembic_version`, gives every later
revision a stable ancestor, and makes `downgrade base` a defined operation.

SPEC §8.2 requires create/apply/roll-back to be demonstrable, which an empty
revision cannot prove on its own. `tests/test_migrations.py` therefore drives a
real `CREATE TABLE` revision from `tests/fixtures/scratch_migrations/` through
upgrade → downgrade → upgrade against a throwaway database. Real DDL, exercised
on every test run, no product table we do not need.
"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "0001_baseline"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """No schema. See the module docstring."""


def downgrade() -> None:
    """Nothing to undo."""
