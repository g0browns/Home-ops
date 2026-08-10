"""Reports whether the database schema matches the migrations on disk.

Surfaced by the readiness endpoint so a container that started against an
un-migrated database is visibly not ready, rather than failing later with a
confusing missing-column error.

Read-only. Nothing here applies a migration — SPEC §0 requires schema changes
to be deliberate, never a side effect of a process starting.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import lru_cache

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy.exc import SQLAlchemyError

from home_ops.config import BACKEND_ROOT
from home_ops.db import get_engine

logger = logging.getLogger(__name__)

ALEMBIC_INI = BACKEND_ROOT / "alembic.ini"
MIGRATIONS_DIR = BACKEND_ROOT / "migrations"


@dataclass(frozen=True)
class MigrationState:
    current: list[str]
    head: list[str]
    in_sync: bool
    error: str | None = None


@lru_cache(maxsize=1)
def _script_directory() -> ScriptDirectory:
    config = Config(str(ALEMBIC_INI))
    # Set explicitly: the value in alembic.ini is relative, and this code can be
    # imported from any working directory.
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    return ScriptDirectory.from_config(config)


def head_revisions() -> list[str]:
    """Revisions at the tip of the migration tree on disk."""
    return sorted(_script_directory().get_heads())


def applied_revisions() -> list[str]:
    """Revisions the connected database believes it has applied."""
    with get_engine().connect() as connection:
        context = MigrationContext.configure(connection)
        return sorted(context.get_current_heads())


def migration_state() -> MigrationState:
    head = head_revisions()
    try:
        current = applied_revisions()
    except (SQLAlchemyError, OSError) as exc:
        logger.warning("could not read applied migrations", exc_info=exc)
        return MigrationState(current=[], head=head, in_sync=False, error=type(exc).__name__)

    return MigrationState(current=current, head=head, in_sync=current == head)
