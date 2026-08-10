"""The permission engine (SPEC §4.2).

The single place that answers "can user U perform action A on resource R". Pure
functions with no database and no framework, so every branch is directly
testable — see `tests/test_policy.py`.

**There are two axes and they behave differently.** Conflating them is the
mistake this module exists to prevent:

*Module access* — may this user touch this feature at all? Role defaults live in
`ROLE_DEFAULTS` below; the database stores only deviations from them. **Admins
bypass this axis entirely.**

*Record visibility* — may this user see this particular row? Driven by the row's
owner and its `private` / `assignees` / `household` setting. **Nobody bypasses
this axis, including admins.** A private task stays private from an admin, which
is the whole point of §4.2's per-item visibility and the strict default §4.8
requires for health data. There is deliberately no `is_admin` check anywhere in
the visibility functions, so there is no bypass to accidentally add.

Yuvomi arrived at the same split and documents it the same way: admins bypass
module access, and no admin bypasses record visibility.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final
from uuid import UUID


class Role(StrEnum):
    """Household roles, per SPEC §4.2's stated minimum."""

    ADMIN = "admin"
    ADULT = "adult"
    LIMITED = "limited"
    READONLY = "readonly"


class Access(StrEnum):
    NONE = "none"
    READ = "read"
    WRITE = "write"


class Action(StrEnum):
    READ = "read"
    WRITE = "write"


class Module(StrEnum):
    """Feature areas that can be permissioned.

    Only what exists today. Modules are added here as their phase lands, so this
    enum never advertises a capability the app does not have.
    """

    USERS = "users"
    SETTINGS = "settings"
    AUDIT = "audit"
    TASKS = "tasks"
    NOTES = "notes"
    CALENDAR = "calendar"
    KITCHEN = "kitchen"
    SHOPPING = "shopping"
    CONTACTS = "contacts"
    HEALTH = "health"


class Visibility(StrEnum):
    PRIVATE = "private"
    ASSIGNEES = "assignees"
    HOUSEHOLD = "household"


class SubjectType(StrEnum):
    ROLE = "role"
    USER = "user"


# Write implies read (Yuvomi's convention, and the obvious one).
_ACCESS_RANK: Final[dict[Access, int]] = {Access.NONE: 0, Access.READ: 1, Access.WRITE: 2}
_ACTION_REQUIRES: Final[dict[Action, Access]] = {
    Action.READ: Access.READ,
    Action.WRITE: Access.WRITE,
}


# Defaults per role. The database stores only deviations from this table, which
# keeps the common case free of rows and makes the intended policy readable in
# one place rather than reconstructed from data.
#
# This is the axis that says "may you touch this feature at all". It is not the
# axis that decides which rows you see — a limited member has write access to
# tasks, but per-item visibility still keeps another member's private ones out
# of their list entirely.
ROLE_DEFAULTS: Final[dict[Role, dict[Module, Access]]] = {
    Role.ADMIN: {
        Module.USERS: Access.WRITE,
        Module.SETTINGS: Access.WRITE,
        Module.AUDIT: Access.READ,
        Module.TASKS: Access.WRITE,
        Module.NOTES: Access.WRITE,
        Module.CALENDAR: Access.WRITE,
        Module.KITCHEN: Access.WRITE,
        Module.SHOPPING: Access.WRITE,
        Module.CONTACTS: Access.WRITE,
        # Module access only. It says an admin may open the Health section — it
        # says nothing about whose records they can see, and §4.8 is explicit
        # that admins get no implicit access to another member's. That is the
        # visibility axis, which has no admin branch anywhere.
        Module.HEALTH: Access.WRITE,
    },
    Role.ADULT: {
        Module.USERS: Access.READ,
        Module.SETTINGS: Access.READ,
        Module.AUDIT: Access.NONE,
        Module.TASKS: Access.WRITE,
        Module.NOTES: Access.WRITE,
        Module.CALENDAR: Access.WRITE,
        Module.KITCHEN: Access.WRITE,
        Module.SHOPPING: Access.WRITE,
        Module.CONTACTS: Access.WRITE,
        Module.HEALTH: Access.WRITE,
    },
    Role.LIMITED: {
        Module.USERS: Access.READ,
        Module.SETTINGS: Access.NONE,
        Module.AUDIT: Access.NONE,
        # A kid does chores and leaves notes; that is the point of the app.
        Module.TASKS: Access.WRITE,
        Module.NOTES: Access.WRITE,
        # Read-only on the shared calendar: a limited member should see what
        # is happening without being able to move the family's appointments.
        Module.CALENDAR: Access.READ,
        # Recipes, though, are worth encouraging: a kid who wants to cook should
        # be able to write down what they cooked. Nothing here is shared
        # infrastructure the way a calendar slot is.
        Module.KITCHEN: Access.WRITE,
        # Adding "more cereal" to the list is the point of a shared list, and a
        # limited member cannot reach a list they are not on: per-list
        # visibility does that work, not this key.
        Module.SHOPPING: Access.WRITE,
        # Read-only on the directory. A limited member should be able to find
        # the doctor's number; the household's address book is not theirs to
        # rewrite, and an import replaces records in bulk.
        Module.CONTACTS: Access.READ,
        # Write, because this is how somebody records *their own* health data
        # and a limited member's health is their own. What stops them reading
        # anybody else's is the share table, not this key.
        Module.HEALTH: Access.WRITE,
    },
    Role.READONLY: {
        Module.USERS: Access.READ,
        Module.SETTINGS: Access.NONE,
        Module.AUDIT: Access.NONE,
        Module.TASKS: Access.READ,
        Module.NOTES: Access.READ,
        Module.CALENDAR: Access.READ,
        Module.KITCHEN: Access.READ,
        Module.SHOPPING: Access.READ,
        Module.CONTACTS: Access.READ,
        # A read-only member still owns their own body. They can look; they
        # cannot write, here as everywhere else.
        Module.HEALTH: Access.READ,
    },
}


@dataclass(frozen=True)
class Principal:
    """Who is acting. Built from the session, never from request input."""

    id: UUID
    role: Role
    is_active: bool = True


@dataclass(frozen=True)
class Deviation:
    """A stored departure from the role defaults.

    Sparse by design: only deviations are rows, so the defaults above remain the
    readable source of policy.
    """

    subject_type: SubjectType
    subject_id: str
    module: Module
    access: Access


# --- axis one: module access (admins bypass) ----------------------------------


def resolve_access(
    principal: Principal,
    module: Module,
    deviations: tuple[Deviation, ...] = (),
) -> Access:
    """Effective access for this principal on this module.

    Precedence: a user-specific deviation beats a role deviation, which beats
    the role default.
    """
    if not principal.is_active:
        return Access.NONE

    # Admins bypass this axis entirely, deviations included. A restricted admin
    # is not a thing; demote them instead.
    if principal.role is Role.ADMIN:
        return Access.WRITE

    for subject_type, subject_id in (
        (SubjectType.USER, str(principal.id)),
        (SubjectType.ROLE, str(principal.role)),
    ):
        for deviation in deviations:
            if (
                deviation.subject_type is subject_type
                and deviation.subject_id == subject_id
                and deviation.module is module
            ):
                return deviation.access

    return ROLE_DEFAULTS[principal.role].get(module, Access.NONE)


def can(
    principal: Principal,
    action: Action,
    module: Module,
    deviations: tuple[Deviation, ...] = (),
) -> bool:
    """The single question: may this principal perform this action on this module."""
    effective = resolve_access(principal, module, deviations)
    return _ACCESS_RANK[effective] >= _ACCESS_RANK[_ACTION_REQUIRES[action]]


# --- axis two: record visibility (nobody bypasses) ----------------------------


def can_see_record(
    principal: Principal,
    *,
    owner_id: UUID,
    visibility: Visibility,
    assignee_ids: frozenset[UUID] = frozenset(),
) -> bool:
    """May this principal see this specific record?

    Note what is absent: any check of `principal.role`. Admins get no special
    treatment here and must not — SPEC §4.2 requires per-item visibility to hold
    against everyone, and §4.8 depends on it for health data. Keeping role out of
    the signature's logic means there is no bypass to accidentally introduce.
    """
    if not principal.is_active:
        return False
    if principal.id == owner_id:
        return True
    if visibility is Visibility.HOUSEHOLD:
        return True
    if visibility is Visibility.ASSIGNEES:
        return principal.id in assignee_ids
    return False


def can_edit_record(
    principal: Principal,
    action: Action,
    module: Module,
    *,
    owner_id: UUID,
    visibility: Visibility,
    assignee_ids: frozenset[UUID] = frozenset(),
    deviations: tuple[Deviation, ...] = (),
) -> bool:
    """Both axes, in the order that matters.

    A record you cannot see is a record you cannot act on, whatever your module
    access says — so visibility is checked first and independently.
    """
    if not can_see_record(
        principal, owner_id=owner_id, visibility=visibility, assignee_ids=assignee_ids
    ):
        return False
    return can(principal, action, module, deviations)


# --- identity-specific rules --------------------------------------------------


def can_edit_own_account(principal: Principal) -> bool:
    """Anyone active may change their own display name, avatar, and password.

    Deliberately not gated on the users module: a read-only member still gets to
    change their own password, and locking that away would be a footgun rather
    than a safeguard.
    """
    return principal.is_active


def can_administer_user(
    principal: Principal,
    target_user_id: UUID,
    deviations: tuple[Deviation, ...] = (),
) -> bool:
    """Create, deactivate, or edit *another* user's account."""
    if principal.id == target_user_id:
        return can_edit_own_account(principal)
    return can(principal, Action.WRITE, Module.USERS, deviations)


def can_change_role(
    principal: Principal,
    target_user_id: UUID,
    deviations: tuple[Deviation, ...] = (),
) -> bool:
    """Change a user's role — never your own.

    Self-service role changes are privilege escalation when the actor is not an
    admin, and an accidental self-demotion that locks the household out of its
    own admin account when they are. Both are avoided by refusing outright; a
    second admin can always do it.
    """
    if principal.id == target_user_id:
        return False
    return can(principal, Action.WRITE, Module.USERS, deviations)
