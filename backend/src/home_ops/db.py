"""Database engine, session handling, and the declarative base.

The naming convention below matters more than it looks: without it Postgres
invents names for indexes and constraints, and an Alembic `downgrade()` that
tries to drop them has nothing reliable to name. SPEC §0 requires every
migration to be reversible, so deterministic constraint names are a day-one
decision rather than something to retrofit.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from dataclasses import dataclass
from functools import lru_cache

from sqlalchemy import MetaData, create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from home_ops.config import get_settings

logger = logging.getLogger(__name__)

# An unreachable database must fail, not hang. Without this the OS connect
# timeout applies — minutes on some platforms — so a request, a readiness probe,
# or a test blocks instead of reporting the problem.
CONNECT_TIMEOUT_SECONDS = 5

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Declarative base for every model in the project.

    Phase 0 defines no tables. Alembic's autogenerate compares the database
    against this metadata, so models must be imported before it runs — see
    `migrations/env.py`.
    """

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    settings = get_settings()
    return create_engine(
        settings.database_url,
        # A home server sleeps, restarts, and loses connections. Without this a
        # stale pooled connection surfaces as a request failure.
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=5,
        echo=False,
        connect_args={"connect_timeout": CONNECT_TIMEOUT_SECONDS},
    )


class AppSession(Session):
    """The application's Session class.

    A named subclass rather than plain `Session` so the visibility guard can be
    installed on it without affecting Alembic's own sessions or anything else
    using SQLAlchemy in this process.
    """


@lru_cache(maxsize=1)
def get_sessionmaker() -> sessionmaker[AppSession]:
    return sessionmaker(
        bind=get_engine(),
        class_=AppSession,
        expire_on_commit=False,
        autoflush=False,
    )


def get_session() -> Iterator[AppSession]:
    """FastAPI dependency yielding a session that is always closed."""
    with get_sessionmaker()() as session:
        yield session


def reset_engine() -> None:
    """Dispose the cached engine and sessionmaker. For tests and config reloads.

    Disposing matters: clearing the cache alone drops the last reference to a
    pool that still owns open connections, leaving them to be closed by the
    garbage collector — which surfaces as an unraisable exception in psycopg's
    `__del__`, and holds locks that block `DROP DATABASE` in the meantime.
    """
    if get_engine.cache_info().currsize:
        get_engine().dispose()
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()


@dataclass(frozen=True)
class DatabaseProbe:
    ok: bool
    latency_ms: float | None
    error: str | None


def probe_database() -> DatabaseProbe:
    """Round-trip a trivial query, for the readiness endpoint.

    The returned `error` is only the exception class name. Driver errors can
    embed the DSN, and readiness output may be read over the public Cloudflare
    hostname, so the detail is logged server-side instead of returned.
    """
    started = time.perf_counter()
    try:
        with get_engine().connect() as connection:
            connection.execute(text("SELECT 1"))
    except (SQLAlchemyError, OSError) as exc:
        logger.warning("database probe failed", exc_info=exc)
        return DatabaseProbe(ok=False, latency_ms=None, error=type(exc).__name__)

    elapsed_ms = (time.perf_counter() - started) * 1000
    return DatabaseProbe(ok=True, latency_ms=round(elapsed_ms, 2), error=None)
