"""Audit logging for security-relevant events (SPEC §4.1).

The spec names four categories explicitly — logins, permission changes, data
exports, deletions — and `AuditAction` covers them plus the identity events that
sit alongside.

**Nothing secret goes in `detail`.** The audit log is the one table most likely
to be read casually, exported, or pasted into a bug report, so `record()` scrubs
anything whose key looks like a credential rather than trusting every future
caller to remember. The scrub is a backstop, not a licence: do not pass secrets.

Writes here are part of the caller's transaction. An audited action that rolls
back leaves no entry, which is correct — the action did not happen.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from home_ops.modules.identity.models import AuditLogEntry

# Substrings that mark a key as never-loggable. Matched case-insensitively
# against the key, so `new_password` and `csrf_token_hash` are both caught.
_REDACT_KEY_MARKERS = (
    "password",
    "token",
    "secret",
    "hash",
    "credential",
    "authorization",
    "cookie",
    "api_key",
    "apikey",
)

REDACTED = "[redacted]"

# Depth limit for the scrub. Audit detail should be a flat-ish record of what
# happened; anything deeper is a sign someone is dumping an object graph.
_MAX_SCRUB_DEPTH = 4


class AuditAction(StrEnum):
    """Every audited event. Add to this rather than passing free-form strings."""

    # Authentication
    LOGIN_SUCCEEDED = "login.succeeded"
    LOGIN_FAILED = "login.failed"
    LOGIN_BLOCKED = "login.blocked"
    LOGOUT = "logout"
    # S105 suppressed below: this is an event name, not a credential. The whole
    # point of this module is that credentials never appear in it.
    PASSWORD_CHANGED = "password.changed"  # noqa: S105
    #: An administrator setting somebody else's password. A separate action
    #: from `password.changed` on purpose: "somebody changed their own" and
    #: "somebody changed another person's" are different events, and only the
    #: second one is worth waking up for.
    PASSWORD_RESET = "password.reset"  # noqa: S105
    SESSIONS_REVOKED = "sessions.revoked"

    # Household bootstrap
    HOUSEHOLD_CLAIMED = "household.claimed"

    # Users
    USER_CREATED = "user.created"
    USER_UPDATED = "user.updated"
    USER_ROLE_CHANGED = "user.role_changed"
    USER_DEACTIVATED = "user.deactivated"
    USER_REACTIVATED = "user.reactivated"
    USER_DELETED = "user.deleted"

    # Permissions and settings
    PERMISSION_CHANGED = "permission.changed"
    HOUSEHOLD_SETTING_CHANGED = "setting.household_changed"
    USER_SETTING_CHANGED = "setting.user_changed"

    # Tasks and notes (SPEC §4.4, §4.5). Creations and updates are recorded
    # alongside deletions because per-item visibility means an edit can change
    # who can see something, which is a security-relevant change.
    TASK_CREATED = "task.created"
    TASK_UPDATED = "task.updated"
    TASK_COMPLETED = "task.completed"
    TASK_DELETED = "task.deleted"

    EVENT_CREATED = "event.created"
    EVENT_UPDATED = "event.updated"
    EVENT_DELETED = "event.deleted"

    NOTE_CREATED = "note.created"
    NOTE_UPDATED = "note.updated"
    NOTE_DELETED = "note.deleted"

    RECIPE_CREATED = "recipe.created"
    RECIPE_UPDATED = "recipe.updated"
    RECIPE_DELETED = "recipe.deleted"

    # A list is the thing that carries visibility in the Shopping module
    # (SPEC §4.12), so who created one, who changed who can see it, and who
    # deleted one with its contents are all security-relevant. Ticking an item
    # is not, and is deliberately absent: a hundred audit rows per shop would
    # bury the events that matter.
    SHOPPING_LIST_CREATED = "shopping_list.created"
    SHOPPING_LIST_UPDATED = "shopping_list.updated"
    SHOPPING_LIST_DELETED = "shopping_list.deleted"

    # The directory (SPEC §4.7). Who was added, changed or removed, and who
    # imported a file of them — never the numbers themselves, because an audit
    # row holding a phone number has copied the record into a second place with
    # different permissions.
    CONTACT_CREATED = "contact.created"
    CONTACT_UPDATED = "contact.updated"
    CONTACT_DELETED = "contact.deleted"
    CONTACT_IMPORTED = "contact.imported"

    # Health records (SPEC §4.8). The *shape* of a change and whose record it
    # was — never a value. An audit row holding a blood pressure reading has
    # copied the most sensitive data in the app into a second table with
    # different permissions, which is the opposite of what an audit log is for.
    HEALTH_RECORD_CREATED = "health_record.created"
    HEALTH_RECORD_UPDATED = "health_record.updated"
    HEALTH_RECORD_DELETED = "health_record.deleted"
    #: Who may read somebody's health records. The single most consequential
    #: setting in the app, so it is traceable by name.
    HEALTH_SHARING_CHANGED = "health.sharing_changed"

    # API tokens (SPEC §4.10). Creating a credential and revoking one are both
    # security events; the token itself never appears in either.
    TOKEN_CREATED = "token.created"  # noqa: S105
    TOKEN_REVOKED = "token.revoked"  # noqa: S105
    #: A token minted straight from a username and password, rather than from an
    #: already-authenticated session. Its own action because it is a different
    #: event: somebody turned a password into a long-lived credential, from a
    #: device that never held a session. That is what a mobile client does on
    #: first run, and it is also what credential stuffing would look like if it
    #: ever succeeded — so it must be greppable apart from `token.created`.
    TOKEN_ISSUED_FROM_PASSWORD = "token.issued_from_password"  # noqa: S105
    #: Revoked token rows deleted from somebody's list. Its own action because it
    #: is the one place a token row is really destroyed — `token.revoked` keeps
    #: the row — and because this log is what the history falls back to
    #: afterwards. Records how many went, never which secrets they were.
    TOKEN_PURGED = "token.purged"  # noqa: S105

    # Data movement
    DATA_EXPORTED = "data.exported"


def _should_redact(key: str) -> bool:
    lowered = key.lower()
    return any(marker in lowered for marker in _REDACT_KEY_MARKERS)


def scrub(value: Any, _depth: int = 0) -> Any:
    """Recursively replace credential-looking values with a marker."""
    if _depth >= _MAX_SCRUB_DEPTH:
        return REDACTED

    if isinstance(value, dict):
        return {
            key: REDACTED if _should_redact(str(key)) else scrub(item, _depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [scrub(item, _depth + 1) for item in value]
    if isinstance(value, UUID):
        return str(value)
    return value


def record(
    session: Session,
    action: AuditAction,
    *,
    actor_id: UUID | None = None,
    actor_label: str | None = None,
    resource_type: str | None = None,
    resource_id: str | None = None,
    client_ip: str | None = None,
    detail: dict[str, Any] | None = None,
) -> AuditLogEntry:
    """Append an audit entry to the caller's transaction.

    `actor_label` is denormalised on purpose: the foreign key is `ON DELETE SET
    NULL`, so deleting a user must not erase what they did, and the label is what
    keeps the row meaningful afterwards.

    `actor_id` is None for events with no authenticated actor — a failed login,
    or claiming an unconfigured household.
    """
    entry = AuditLogEntry(
        actor_user_id=actor_id,
        actor_label=actor_label,
        action=action.value,
        resource_type=resource_type,
        resource_id=str(resource_id) if resource_id is not None else None,
        client_ip=client_ip,
        detail=scrub(detail or {}),
    )
    session.add(entry)
    return entry
