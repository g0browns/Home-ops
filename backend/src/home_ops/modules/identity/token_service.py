"""Issuing, checking and revoking API tokens (SPEC §4.10).

The one rule everything else serves: **a scope is a ceiling, never a grant.**
`effective_access` takes the lesser of what the token allows and what its owner
can do *now*, so the answer changes the moment somebody's permissions change.
Nothing about a token's authority is stored at issue time, because a stored
authority is one that outlives the decision that granted it.
"""

from __future__ import annotations

import datetime as dt
import secrets
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

from home_ops.modules.identity.token_models import (
    MAX_TOKENS_PER_USER,
    PREFIX_LENGTH,
    TOKEN_PREFIX,
    ApiToken,
    ApiTokenScope,
)
from home_ops.policy import Access, Action, Module
from home_ops.security import hash_token

#: 256 bits, like a session token. There is no dictionary to attack, which is
#: why the storage hash is SHA-256 rather than Argon2 — see `security.py`.
TOKEN_BYTES = 32

#: Requests per token per minute. A household integration polls; it does not
#: hammer. Generous enough that nothing legitimate notices, low enough that a
#: leaked token cannot be used to walk the whole database quickly.
RATE_LIMIT_PER_MINUTE = 120


class TooManyTokens(ValueError):
    pass


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def issue(
    db: DbSession,
    user_id: UUID,
    *,
    name: str,
    scopes: dict[Module, Access] | None = None,
    expires_at: dt.datetime | None = None,
) -> tuple[ApiToken, str]:
    """Create a token and return it with its **plaintext, once**.

    The caller must hand the plaintext straight to the response and keep no
    copy: it is not recoverable afterwards, by anybody, including whoever runs
    the database.
    """
    existing = (
        db.scalar(
            select(func.count())
            .select_from(ApiToken)
            .where(ApiToken.user_id == user_id, ApiToken.revoked_at.is_(None))
        )
        or 0
    )
    if existing >= MAX_TOKENS_PER_USER:
        raise TooManyTokens(f"A member may hold {MAX_TOKENS_PER_USER} tokens at once.")

    plaintext = f"{TOKEN_PREFIX}{secrets.token_urlsafe(TOKEN_BYTES)}"
    token = ApiToken(
        user_id=user_id,
        name=name.strip(),
        token_hash=hash_token(plaintext),
        prefix=plaintext[: len(TOKEN_PREFIX) + PREFIX_LENGTH],
        expires_at=expires_at,
    )
    db.add(token)
    db.flush()

    for module, access in (scopes or {}).items():
        db.add(ApiTokenScope(token_id=token.id, module=module.value, access=access.value))
    db.flush()
    db.expire(token, ["scopes"])
    return token, plaintext


def load(db: DbSession, plaintext: str) -> ApiToken | None:
    """The live token for a presented string, or None.

    Revoked and expired both return None rather than raising, so the caller has
    one "this is not a credential" branch instead of three.
    """
    if not plaintext.startswith(TOKEN_PREFIX):
        return None

    token = (
        db.scalars(select(ApiToken).where(ApiToken.token_hash == hash_token(plaintext)))
        .unique()
        .one_or_none()
    )
    if token is None or token.revoked_at is not None:
        return None
    if token.expires_at is not None and token.expires_at <= utcnow():
        return None
    return token


def list_for(db: DbSession, user_id: UUID) -> list[ApiToken]:
    """Somebody's own tokens, newest first. Never anybody else's."""
    return list(
        db.scalars(
            select(ApiToken)
            .where(ApiToken.user_id == user_id)
            .order_by(ApiToken.revoked_at.is_not(None), ApiToken.created_at.desc())
        ).unique()
    )


def revoke(db: DbSession, token: ApiToken) -> ApiToken:
    """Stop it authenticating, and keep the row.

    Deleting would take the last-used record with it, and "when was this last
    used" is exactly the question somebody asks *after* revoking something they
    did not recognise.
    """
    if token.revoked_at is None:
        token.revoked_at = utcnow()
        db.flush()
    return token


def purge_revoked(db: DbSession, user_id: UUID) -> int:
    """Delete somebody's revoked tokens, and say how many went.

    **The only place a token row is really destroyed.** `revoke` keeps the row
    deliberately — "when was this last used" is exactly what somebody asks
    *after* revoking something they did not recognise — and that remains the
    right default for one revocation. It is the wrong default for fifty: a list
    that only ever grows stops being read at all, and the live tokens, which are
    the ones that matter, end up buried under development leftovers.

    What actually survives this is the audit log. `token.created`,
    `token.revoked` and `token.purged` are all still there with their timestamps,
    so the history is not lost — only the display row is.

    Revoked rows only, and only this user's. An expired-but-not-revoked token
    keeps its place: it authenticates nothing, but nobody has decided about it
    yet, and quietly clearing it would be deciding on their behalf.
    """
    doomed = list(
        db.scalars(
            select(ApiToken).where(
                ApiToken.user_id == user_id,
                ApiToken.revoked_at.is_not(None),
            )
        ).unique()
    )
    for token in doomed:
        # Through the ORM rather than a bulk DELETE, so the scope rows go with
        # it by the relationship cascade as well as the database's own.
        db.delete(token)
    db.flush()
    return len(doomed)


def ceiling(token: ApiToken) -> dict[str, Access]:
    """The token's own limit, per module. Empty means "not narrowed"."""
    return {scope.module: Access(scope.access) for scope in token.scopes}


def allows(token_ceiling: dict[str, Access], action: Action, module: Module) -> bool:
    """Does the token's scope permit this, ignoring the owner entirely?

    A module with no scope row is unnarrowed, so the owner's own permission is
    the only limit. A module scoped to `none` is closed whatever the owner can
    do, and `read` refuses a write for the same reason.
    """
    if not token_ceiling:
        return True

    allowed = token_ceiling.get(module.value)
    if allowed is None:
        # Narrowed to *some* modules means narrowed to those modules. A token
        # scoped to the shopping list must not reach the health records simply
        # because nobody thought to write a row saying so.
        return False
    if allowed is Access.NONE:
        return False
    if action is Action.WRITE:
        return allowed is Access.WRITE
    return True


def spend_request(db: DbSession, token: ApiToken) -> bool:
    """Count one request against the token, and say whether it may proceed.

    A fixed window rather than a sliding one: a sliding window needs a row per
    request and a sweep to keep it from growing, and §4.10 asks for a limit,
    not for a precise one. The window state lives on the token row, which is
    already being written to stamp `last_used_at` — so this costs no extra
    write and cannot drift out of step with the thing it limits.

    Counted *before* the request is served, so the request that crosses the
    line is the one refused rather than the one after it.
    """
    now = utcnow()
    window = dt.timedelta(minutes=1)

    if token.window_started_at is None or now - token.window_started_at >= window:
        token.window_started_at = now
        token.window_count = 1
        token.last_used_at = now
        db.flush()
        return True

    token.window_count += 1
    token.last_used_at = now
    db.flush()
    return token.window_count <= RATE_LIMIT_PER_MINUTE
