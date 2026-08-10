"""Alembic environment.

The connection string comes from `home_ops.config`, not from `alembic.ini`, so
credentials stay in the environment and there is one source of truth. A caller
may still override it with `config.set_main_option("sqlalchemy.url", ...)` —
`tests/test_migrations.py` uses that to drive a throwaway database.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from home_ops.config import get_settings
from home_ops.db import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# Autogenerate diffs the database against this metadata. Every model package
# must be imported before it is read, or autogenerate will cheerfully propose
# dropping the tables it cannot see. Add each module's models here as it lands.
from home_ops.modules.calendar import models as _calendar_models  # noqa: E402,F401
from home_ops.modules.contacts import models as _contacts_models  # noqa: E402,F401
from home_ops.modules.health import models as _health_models  # noqa: E402,F401
from home_ops.modules.identity import models as _identity_models  # noqa: E402,F401
from home_ops.modules.identity import token_models as _token_models  # noqa: E402,F401
from home_ops.modules.kitchen import models as _kitchen_models  # noqa: E402,F401
from home_ops.modules.kitchen import plan_models as _meal_plan_models  # noqa: E402,F401
from home_ops.modules.notes import models as _notes_models  # noqa: E402,F401
from home_ops.modules.settings import models as _settings_models  # noqa: E402,F401
from home_ops.modules.shopping import models as _shopping_models  # noqa: E402,F401
from home_ops.modules.tasks import models as _tasks_models  # noqa: E402,F401

target_metadata = Base.metadata


def _database_url() -> str:
    override = config.get_main_option("sqlalchemy.url")
    if override:
        return override
    return get_settings().database_url


def run_migrations_offline() -> None:
    """Emit SQL to stdout instead of running it — useful for reviewing a change."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _database_url()

    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # Without these, a changed column type or default is silently
            # missed by autogenerate.
            compare_type=True,
            compare_server_default=True,
            # Postgres runs DDL transactionally, so a failed migration rolls
            # back cleanly instead of leaving a half-applied schema.
            transaction_per_migration=True,
        )
        with context.begin_transaction():
            context.run_migrations()

    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
