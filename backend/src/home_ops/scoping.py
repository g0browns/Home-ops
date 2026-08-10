"""Query scoping for per-item visibility, and the guard that enforces it.

SPEC §4.2 requires per-item visibility to be enforced in the data layer rather
than by hiding UI. We chose application-layer enforcement over Postgres RLS,
which buys testability and one place to read the logic — but it gives up the
database backstop. A single read path that forgets to scope is a silent leak
with nothing underneath to catch it.

So this module provides both halves:

`visible(stmt, Model, principal)`
    The mandatory scoping helper. Every read of a visibility-bearing model goes
    through it.

`ScopingGuard`
    The replacement for the net RLS would have given us. It hooks ORM execution
    and **raises** if a query touches a visibility-bearing table without having
    gone through `visible()`. A forgotten call site becomes a loud failure in
    the test suite instead of a quiet disclosure.

Phase 1 defines no visibility-bearing models — users, sessions and audit entries
are governed by module access, not per-item visibility. The machinery is here
because §4.2 says to get the permission layer right *before* building features
on it, and Phase 3's tasks and notes are the first real consumers. It is
exercised today by a fixture model in the test suite.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, ClassVar
from uuid import UUID

from sqlalchemy import (
    ColumnElement,
    DateTime,
    ForeignKey,
    String,
    Table,
    false,
    func,
    or_,
    select,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, Session, declared_attr, mapped_column
from sqlalchemy.sql import Select, visitors

from home_ops.policy import Principal, Visibility

# Execution option set by `visible()` and checked by the guard.
SCOPED_OPTION = "home_ops_visibility_scoped"

# Tables that carry per-item visibility, populated by OwnedVisibleMixin below.
_GUARDED_TABLES: set[str] = set()


class VisibilityScopingError(RuntimeError):
    """A visibility-bearing table was queried without `visible()`.

    Not a warning. The alternative to raising is returning rows the caller may
    not be entitled to see.
    """


class OwnedVisibleMixin:
    """Adds ownership and per-item visibility to a model (SPEC §4.2).

    Any model mixing this in is registered with the guard, so it cannot be read
    without scoping.
    """

    __visibility_guarded__: ClassVar[bool] = True

    @declared_attr
    @classmethod
    def owner_id(cls) -> Mapped[UUID]:
        return mapped_column(
            PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        )

    @declared_attr
    @classmethod
    def visibility(cls) -> Mapped[str]:
        # Defaults to the most private option. A new record leaking because
        # someone forgot to set visibility is the failure mode worth designing
        # against; an over-private record is merely inconvenient.
        return mapped_column(String(16), nullable=False, default=Visibility.PRIVATE.value)

    @declared_attr
    @classmethod
    def created_at(cls) -> Mapped[dt.datetime]:
        return mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    @classmethod
    def assignee_clause(cls, principal: Principal) -> ColumnElement[bool] | None:
        """SQL for "this principal is an assignee of this row", or None.

        Models with an assignment join table override this. The default of None
        means `assignees` visibility behaves as `private`, which is the safe
        direction to fail.
        """
        return None

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        table = getattr(cls, "__tablename__", None)
        if isinstance(table, str):
            _GUARDED_TABLES.add(table)


def guarded_tables() -> frozenset[str]:
    """Tables the guard will refuse to serve unscoped. For tests and diagnostics."""
    return frozenset(_GUARDED_TABLES)


def visible(stmt: Select[Any], model: type[OwnedVisibleMixin], principal: Principal) -> Select[Any]:
    """Restrict `stmt` to rows `principal` may see.

    The rule, and note what it does *not* contain:

    * you own it, or
    * it is visible to the household, or
    * it is visible to assignees and you are one.

    There is no admin branch. SPEC §4.2 requires per-item visibility to hold
    against everyone, and §4.8 leans on that for health data. `policy.py` says
    the same thing about the same axis; the two must stay in agreement.
    """
    if not principal.is_active:
        # Deactivated accounts see nothing, rather than seeing their own history.
        return stmt.where(false()).execution_options(**{SCOPED_OPTION: True})

    clauses: list[ColumnElement[bool]] = [
        model.owner_id == principal.id,
        model.visibility == Visibility.HOUSEHOLD.value,
    ]

    assignee = model.assignee_clause(principal)
    if assignee is not None:
        clauses.append((model.visibility == Visibility.ASSIGNEES.value) & assignee)

    return stmt.where(or_(*clauses)).execution_options(**{SCOPED_OPTION: True})


def visible_select(model: type[OwnedVisibleMixin], principal: Principal) -> Select[Any]:
    """`select(Model)` already scoped. The shortest correct thing to reach for."""
    return visible(select(model), model, principal)


def _statement_tables(statement: Any) -> set[str]:
    """Every table named anywhere in a statement, subqueries included."""
    return {element.name for element in visitors.iterate(statement) if isinstance(element, Table)}


_GUARD_INSTALLED_ON: set[type[Session]] = set()


def install_scoping_guard(session_class: type[Session]) -> None:
    """Refuse to execute an unscoped read of a visibility-bearing table.

    Registered on the application's Session class at startup. Only SELECTs are
    checked: writes are gated by `policy.can_edit_record` at the route layer,
    and a write already knows which row it is touching.

    Idempotent — the app factory runs once per application, but tests build many,
    and registering the listener repeatedly would raise the same error several
    times over.
    """
    from sqlalchemy import event

    if session_class in _GUARD_INSTALLED_ON:
        return
    _GUARD_INSTALLED_ON.add(session_class)

    @event.listens_for(session_class, "do_orm_execute")
    def _require_scoping(orm_execute_state: Any) -> None:
        if not orm_execute_state.is_select:
            return
        if orm_execute_state.execution_options.get(SCOPED_OPTION):
            return

        touched = _statement_tables(orm_execute_state.statement) & _GUARDED_TABLES
        if touched:
            raise VisibilityScopingError(
                f"Unscoped SELECT touching visibility-bearing table(s): {sorted(touched)}. "
                "Reads of these must go through home_ops.scoping.visible() so per-item "
                "visibility is applied (SPEC §4.2). If this query is genuinely exempt — "
                "a migration or an aggregate that exposes no rows — say so explicitly "
                f"with .execution_options({SCOPED_OPTION}=True) and a comment explaining why."
            )
