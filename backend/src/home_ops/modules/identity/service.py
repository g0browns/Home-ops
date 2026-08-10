"""Identity operations: users, authentication, lockout, sessions (SPEC §4.1).

Route handlers stay thin and call into here, so the security-sensitive parts are
testable without HTTP and reviewable in one file.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from enum import StrEnum, auto
from typing import Any, cast
from uuid import UUID

from sqlalchemy import CursorResult, delete, func, select
from sqlalchemy.orm import Session as DbSession

from home_ops import security
from home_ops.config import Settings
from home_ops.modules.identity.models import AccessPermission, AuthAttempt, Session, User
from home_ops.policy import Access, Deviation, Module, Principal, Role, SubjectType

# Hashing this on a missing username keeps the failure path the same cost as the
# success path. Without it, "no such user" returns measurably faster than "wrong
# password" and the difference enumerates accounts.
_TIMING_EQUALISER_HASH = security.hash_password("timing-equaliser-not-a-real-password")


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def normalize_username(username: str) -> str:
    """Usernames are case-insensitive; the canonical form is lower-cased."""
    return username.strip().lower()


class LoginOutcome(StrEnum):
    SUCCESS = auto()
    INVALID_CREDENTIALS = auto()
    LOCKED_OUT = auto()
    ACCOUNT_DISABLED = auto()


@dataclass(frozen=True)
class LoginResult:
    outcome: LoginOutcome
    user: User | None = None
    retry_after_seconds: int | None = None

    @property
    def succeeded(self) -> bool:
        return self.outcome is LoginOutcome.SUCCESS


@dataclass(frozen=True)
class IssuedSession:
    """A newly created session. The raw values exist only here and in the response."""

    session: Session
    token: str
    csrf_token: str


# --- users --------------------------------------------------------------------


def household_is_unclaimed(db: DbSession) -> bool:
    """True while no user exists at all — the only time setup is available."""
    return db.scalar(select(func.count()).select_from(User)) == 0


def get_user_by_username(db: DbSession, username: str) -> User | None:
    return db.scalar(select(User).where(User.username == normalize_username(username)))


def get_user(db: DbSession, user_id: UUID) -> User | None:
    return db.get(User, user_id)


def list_users(db: DbSession) -> list[User]:
    """Every member.

    Not visibility-scoped, and deliberately so: users are governed by module
    access (`Module.USERS`), not per-item visibility. A household where members
    cannot see that each other exist is not a household.
    """
    return list(db.scalars(select(User).order_by(User.display_name)))


# Member hues, in assignment order. These are KEYS, not colours: the hex values
# live in the frontend's tokens.css and differ between light and dark, so
# storing one here would pin a member to a colour unreadable in the other theme.
#
# Must stay in step with MEMBER_HUES in frontend/src/lib/members.ts —
# tests/test_members.py asserts the two lists match.
MEMBER_HUES: tuple[str, ...] = ("clay", "forest", "ochre", "indigo", "plum", "teal")


def next_member_hue(db: DbSession) -> str:
    """The least-used hue, ties broken by the order above.

    Round-robin rather than random so a household of three gets three visibly
    different colours instead of rolling the same one twice. Beyond six members
    hues start repeating, which is why a name or initials always accompanies the
    colour — see the note in frontend/src/lib/members.ts.
    """
    counts = dict.fromkeys(MEMBER_HUES, 0)
    for (colour,) in db.execute(select(User.avatar_color)):
        if colour in counts:
            counts[colour] += 1
    return min(MEMBER_HUES, key=lambda hue: (counts[hue], MEMBER_HUES.index(hue)))


def create_user(
    db: DbSession,
    *,
    username: str,
    display_name: str,
    password: str,
    role: Role,
    avatar_color: str | None = None,
) -> User:
    """Create a local account. Raises on a weak or over-long password.

    A hue is assigned automatically when none is given, because the Rota design
    direction (SPEC §6) uses member colour as its primary way of scanning a list
    — an account without one would be invisible in exactly the way the design
    relies on.
    """
    user = User(
        username=normalize_username(username),
        display_name=display_name.strip(),
        password_hash=security.hash_password(password),
        role=role.value,
        avatar_color=avatar_color or next_member_hue(db),
    )
    db.add(user)
    db.flush()
    return user


def set_password(db: DbSession, user: User, new_password: str) -> None:
    """Change a password and invalidate every existing session (SPEC §4.1).

    Invalidation is the point: changing a password because you think it is
    compromised is worthless if the attacker's session survives it. Callers that
    want the current session to continue must issue a fresh one afterwards.
    """
    user.password_hash = security.hash_password(new_password)
    db.flush()
    revoke_all_sessions(db, user.id)


def principal_for(user: User) -> Principal:
    return Principal(id=user.id, role=Role(user.role), is_active=user.is_active)


# --- permission deviations ----------------------------------------------------


def load_deviations(db: DbSession, principal: Principal) -> tuple[Deviation, ...]:
    """Fetch only the deviations that could apply to this principal.

    Role defaults live in `policy.ROLE_DEFAULTS`; the table holds exceptions.
    """
    rows = db.scalars(
        select(AccessPermission).where(
            (
                (AccessPermission.subject_type == SubjectType.USER.value)
                & (AccessPermission.subject_id == str(principal.id))
            )
            | (
                (AccessPermission.subject_type == SubjectType.ROLE.value)
                & (AccessPermission.subject_id == str(principal.role))
            )
        )
    )
    return tuple(
        Deviation(
            subject_type=SubjectType(row.subject_type),
            subject_id=row.subject_id,
            module=Module(row.module),
            access=Access(row.access),
        )
        for row in rows
    )


# --- lockout ------------------------------------------------------------------


def _recent_failures(db: DbSession, settings: Settings) -> tuple[dt.datetime, int, int]:
    since = utcnow() - dt.timedelta(minutes=settings.login_failure_window_minutes)
    return since, settings.login_max_failures_per_username, settings.login_max_failures_per_ip


def check_lockout(
    db: DbSession, settings: Settings, *, username: str, client_ip: str
) -> int | None:
    """Seconds to wait before another attempt is allowed, or None if allowed now.

    Two independent limits: one per username (someone guessing one account) and
    one per address (someone spraying many). Username counting includes accounts
    that do not exist, so an attacker cannot tell real usernames apart by
    watching which ones lock.
    """
    since, max_username, max_ip = _recent_failures(db, settings)

    username_failures = (
        db.scalar(
            select(func.count())
            .select_from(AuthAttempt)
            .where(
                AuthAttempt.username == normalize_username(username),
                AuthAttempt.succeeded.is_(False),
                AuthAttempt.at >= since,
            )
        )
        or 0
    )
    ip_failures = (
        db.scalar(
            select(func.count())
            .select_from(AuthAttempt)
            .where(
                AuthAttempt.client_ip == client_ip,
                AuthAttempt.succeeded.is_(False),
                AuthAttempt.at >= since,
            )
        )
        or 0
    )

    if username_failures < max_username and ip_failures < max_ip:
        return None

    # Retry-After is the remaining life of the oldest counted failure, i.e. when
    # the window will have moved far enough to let one attempt through.
    oldest = db.scalar(
        select(func.min(AuthAttempt.at)).where(
            AuthAttempt.succeeded.is_(False),
            AuthAttempt.at >= since,
            (AuthAttempt.username == normalize_username(username))
            | (AuthAttempt.client_ip == client_ip),
        )
    )
    if oldest is None:
        return None
    unlock_at = oldest + dt.timedelta(minutes=settings.login_failure_window_minutes)
    return max(1, int((unlock_at - utcnow()).total_seconds()))


def record_attempt(db: DbSession, *, username: str, client_ip: str, succeeded: bool) -> None:
    db.add(
        AuthAttempt(
            username=normalize_username(username),
            client_ip=client_ip,
            succeeded=succeeded,
        )
    )
    db.flush()


def prune_auth_attempts(db: DbSession, settings: Settings) -> int:
    """Drop attempts older than the window. Nothing here is worth retaining."""
    cutoff = utcnow() - dt.timedelta(minutes=settings.login_failure_window_minutes * 4)
    result = cast(
        "CursorResult[Any]", db.execute(delete(AuthAttempt).where(AuthAttempt.at < cutoff))
    )
    return result.rowcount or 0


# --- authentication -----------------------------------------------------------


def authenticate(
    db: DbSession, settings: Settings, *, username: str, password: str, client_ip: str
) -> LoginResult:
    """Verify credentials, honouring lockout, in constant-ish time."""
    retry_after = check_lockout(db, settings, username=username, client_ip=client_ip)
    if retry_after is not None:
        return LoginResult(LoginOutcome.LOCKED_OUT, retry_after_seconds=retry_after)

    user = get_user_by_username(db, username)

    if user is None or user.password_hash is None:
        # Same work as a real verify, so timing reveals nothing.
        security.verify_password(_TIMING_EQUALISER_HASH, password)
        record_attempt(db, username=username, client_ip=client_ip, succeeded=False)
        return LoginResult(LoginOutcome.INVALID_CREDENTIALS)

    if not security.verify_password(user.password_hash, password):
        record_attempt(db, username=username, client_ip=client_ip, succeeded=False)
        return LoginResult(LoginOutcome.INVALID_CREDENTIALS)

    if not user.is_active:
        # Counted as a failure so a disabled account cannot be used as an oracle
        # for password guessing.
        record_attempt(db, username=username, client_ip=client_ip, succeeded=False)
        return LoginResult(LoginOutcome.ACCOUNT_DISABLED)

    # Transparently upgrade the hash if the cost parameters have been raised.
    if security.needs_rehash(user.password_hash):
        user.password_hash = security.hash_password(password)

    record_attempt(db, username=username, client_ip=client_ip, succeeded=True)
    return LoginResult(LoginOutcome.SUCCESS, user=user)


# --- sessions -----------------------------------------------------------------


def issue_session(
    db: DbSession,
    settings: Settings,
    *,
    user: User,
    user_agent: str | None,
    client_ip: str | None,
) -> IssuedSession:
    """Create a session and return its raw token exactly once."""
    token = security.generate_token()
    csrf_token = security.generate_token()

    session = Session(
        user_id=user.id,
        token_hash=security.hash_token(token),
        csrf_token_hash=security.hash_token(csrf_token),
        expires_at=utcnow() + dt.timedelta(hours=settings.session_ttl_hours),
        user_agent=(user_agent or "")[:255] or None,
        created_ip=client_ip,
    )
    db.add(session)
    db.flush()
    return IssuedSession(session=session, token=token, csrf_token=csrf_token)


def load_session(db: DbSession, token: str) -> Session | None:
    """Look a session up by raw token. Expired sessions are treated as absent."""
    session = db.scalar(select(Session).where(Session.token_hash == security.hash_token(token)))
    if session is None:
        return None
    if session.expires_at <= utcnow():
        return None
    return session


def touch_session(db: DbSession, settings: Settings, session: Session) -> None:
    """Roll the expiry forward on activity.

    Throttled to once a minute: without that, every request writes to the
    sessions table, which is a lot of churn for no benefit.
    """
    now = utcnow()
    if (now - session.last_seen_at) < dt.timedelta(minutes=1):
        return
    session.last_seen_at = now
    session.expires_at = now + dt.timedelta(hours=settings.session_ttl_hours)
    db.flush()


def revoke_session(db: DbSession, session_id: UUID) -> None:
    db.execute(delete(Session).where(Session.id == session_id))


def revoke_all_sessions(db: DbSession, user_id: UUID) -> int:
    result = cast(
        "CursorResult[Any]", db.execute(delete(Session).where(Session.user_id == user_id))
    )
    return result.rowcount or 0


def prune_expired_sessions(db: DbSession) -> int:
    result = cast(
        "CursorResult[Any]", db.execute(delete(Session).where(Session.expires_at <= utcnow()))
    )
    return result.rowcount or 0
