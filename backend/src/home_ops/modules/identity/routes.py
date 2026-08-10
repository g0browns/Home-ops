"""Authentication, first-run setup, users, permissions, and the audit log.

Handlers stay thin: validation is in `schemas.py`, the security-sensitive work is
in `service.py`, and every permission answer comes from `policy.py` via
`dependencies.require`. Nothing here re-implements a rule.
"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session as DbSessionType

from home_ops import audit
from home_ops.audit import AuditAction
from home_ops.dependencies import (
    AuthContext,
    AuthDep,
    ClientIpDep,
    DbDep,
    SettingsDep,
    TunnelDep,
    current_auth,
    require,
)
from home_ops.modules.identity import schemas, service
from home_ops.modules.identity.models import AccessPermission, AuditLogEntry, User
from home_ops.policy import (
    Action,
    Module,
    Role,
    SubjectType,
    can_administer_user,
    can_change_role,
    resolve_access,
)
from home_ops.scoping import SCOPED_OPTION
from home_ops.security import (
    PasswordTooLongError,
    PasswordTooShortError,
    verify_password,
)

auth_router = APIRouter(prefix="/auth", tags=["auth"])
setup_router = APIRouter(prefix="/setup", tags=["setup"])
users_router = APIRouter(prefix="/users", tags=["users"])
permissions_router = APIRouter(prefix="/permissions", tags=["permissions"])
audit_router = APIRouter(prefix="/audit", tags=["audit"])


# --- helpers ------------------------------------------------------------------


def _permission_map(auth: AuthContext) -> dict[str, str]:
    """Effective module access for the caller, for the UI to hide what it should."""
    return {
        module.value: resolve_access(auth.principal, module, auth.deviations).value
        for module in Module
    }


def _current_user_payload(auth: AuthContext) -> schemas.CurrentUser:
    return schemas.CurrentUser(
        **schemas.UserPublic.model_validate(auth.user).model_dump(),
        permissions=_permission_map(auth),
    )


def _set_session_cookies(
    response: Response, settings: SettingsDep, *, token: str, csrf_token: str, max_age: int
) -> None:
    """Attach the session and CSRF cookies (SPEC §2.1).

    Two things here are load-bearing:

    * `secure` comes from configuration and defaults off. A hardcoded Secure
      cookie is silently dropped over HTTP and would lock out the tailnet and
      LAN paths entirely.
    * **No `domain` is set.** That keeps cookies host-scoped, so the Cloudflare
      hostname, the tailnet name and the LAN IP each hold their own independent
      session and cannot clobber one another.
    """
    response.set_cookie(
        settings.session_cookie_name,
        token,
        max_age=max_age,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
        path="/",
    )
    # Readable by JavaScript on purpose: the frontend must echo it into a header.
    # An attacker can cause the cookie to be *sent* but cannot *read* it.
    response.set_cookie(
        settings.csrf_cookie_name,
        csrf_token,
        max_age=max_age,
        httponly=False,
        secure=settings.session_cookie_secure,
        samesite="lax",
        path="/",
    )


def _clear_session_cookies(response: Response, settings: SettingsDep) -> None:
    for name in (settings.session_cookie_name, settings.csrf_cookie_name):
        response.delete_cookie(name, path="/", samesite="lax")


def _password_error(exc: Exception) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc))


# --- first-run setup ----------------------------------------------------------


def _setup_refusal(settings: SettingsDep, via_tunnel: bool) -> str | None:
    """Why setup is not permitted here, or None if it is."""
    if via_tunnel and not settings.setup_allow_tunnel_path:
        return (
            "First-run setup is disabled over the public tunnel. Claim this "
            "household from the tailnet or the local network, or set "
            "SETUP_ALLOW_TUNNEL_PATH=true."
        )
    return None


@setup_router.get("", response_model=schemas.SetupStatus, summary="Is setup needed?")
def setup_status(db: DbDep, settings: SettingsDep, via_tunnel: TunnelDep) -> schemas.SetupStatus:
    needs_setup = service.household_is_unclaimed(db)
    refusal = _setup_refusal(settings, via_tunnel) if needs_setup else None
    return schemas.SetupStatus(
        needs_setup=needs_setup,
        can_setup_here=needs_setup and refusal is None,
        reason=refusal,
    )


@setup_router.post(
    "",
    response_model=schemas.LoginResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Claim an unconfigured household",
)
def claim_household(
    payload: schemas.SetupRequest,
    response: Response,
    request: Request,
    db: DbDep,
    settings: SettingsDep,
    client_ip: ClientIpDep,
    via_tunnel: TunnelDep,
) -> schemas.LoginResponse:
    """Create the first admin. Unauthenticated by nature, and available exactly once.

    Guarded three ways: it exists only while the users table is empty, it is
    refused over the tunnel unless explicitly allowed, and the creation is
    audited.
    """
    if not service.household_is_unclaimed(db):
        # Not 403: once claimed, this endpoint conceptually does not exist.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="This household has already been set up.",
        )

    refusal = _setup_refusal(settings, via_tunnel)
    if refusal is not None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=refusal)

    try:
        user = service.create_user(
            db,
            username=payload.username,
            display_name=payload.display_name,
            password=payload.password,
            role=Role.ADMIN,
        )
    except (PasswordTooShortError, PasswordTooLongError) as exc:
        raise _password_error(exc) from exc

    issued = service.issue_session(
        db,
        settings,
        user=user,
        user_agent=request.headers.get("user-agent"),
        client_ip=client_ip,
    )
    audit.record(
        db,
        AuditAction.HOUSEHOLD_CLAIMED,
        actor_id=user.id,
        actor_label=user.username,
        resource_type="user",
        resource_id=str(user.id),
        client_ip=client_ip,
        detail={"username": user.username, "via_tunnel": via_tunnel},
    )
    db.commit()

    _set_session_cookies(
        response,
        settings,
        token=issued.token,
        csrf_token=issued.csrf_token,
        max_age=settings.session_ttl_hours * 3600,
    )
    auth = AuthContext(
        user=user,
        session=issued.session,
        principal=service.principal_for(user),
        deviations=(),
    )
    return schemas.LoginResponse(user=_current_user_payload(auth), csrf_token=issued.csrf_token)


# --- authentication -----------------------------------------------------------


@auth_router.post("/login", response_model=schemas.LoginResponse, summary="Log in")
def login(
    payload: schemas.LoginRequest,
    response: Response,
    request: Request,
    db: DbDep,
    settings: SettingsDep,
    client_ip: ClientIpDep,
) -> schemas.LoginResponse:
    result = service.authenticate(
        db,
        settings,
        username=payload.username,
        password=payload.password,
        client_ip=client_ip,
    )

    if result.outcome is service.LoginOutcome.LOCKED_OUT:
        audit.record(
            db,
            AuditAction.LOGIN_BLOCKED,
            actor_label=payload.username,
            client_ip=client_ip,
            detail={"username": payload.username},
        )
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many failed attempts. Try again later.",
            headers={"Retry-After": str(result.retry_after_seconds or 60)},
        )

    if result.user is None or not result.succeeded:
        audit.record(
            db,
            AuditAction.LOGIN_FAILED,
            actor_label=payload.username,
            client_ip=client_ip,
            detail={"username": payload.username, "reason": result.outcome.value},
        )
        db.commit()
        # One message for every failure mode. Distinguishing "no such user" from
        # "wrong password" from "account disabled" hands out an enumeration
        # oracle for free.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password.",
        )

    user = result.user
    issued = service.issue_session(
        db,
        settings,
        user=user,
        user_agent=request.headers.get("user-agent"),
        client_ip=client_ip,
    )
    audit.record(
        db,
        AuditAction.LOGIN_SUCCEEDED,
        actor_id=user.id,
        actor_label=user.username,
        client_ip=client_ip,
    )
    db.commit()

    _set_session_cookies(
        response,
        settings,
        token=issued.token,
        csrf_token=issued.csrf_token,
        max_age=settings.session_ttl_hours * 3600,
    )
    principal = service.principal_for(user)
    auth = AuthContext(
        user=user,
        session=issued.session,
        principal=principal,
        deviations=service.load_deviations(db, principal),
    )
    return schemas.LoginResponse(user=_current_user_payload(auth), csrf_token=issued.csrf_token)


@auth_router.post("/logout", status_code=status.HTTP_204_NO_CONTENT, summary="Log out")
def logout(
    response: Response,
    request: Request,
    db: DbDep,
    settings: SettingsDep,
    client_ip: ClientIpDep,
) -> Response:
    """Idempotent, as SPEC §4.1 requires.

    Logging out when already logged out is a success, not an error — the desired
    end state is "no session", and it is reached either way. It therefore does
    not depend on `current_auth`, so an expired session can still be cleared.
    """
    token = request.cookies.get(settings.session_cookie_name)
    if token:
        session = service.load_session(db, token)
        if session is not None:
            service.revoke_session(db, session.id)
            audit.record(
                db,
                AuditAction.LOGOUT,
                actor_id=session.user_id,
                client_ip=client_ip,
            )
            db.commit()

    _clear_session_cookies(response, settings)
    response.status_code = status.HTTP_204_NO_CONTENT
    return response


@auth_router.get("/me", response_model=schemas.CurrentUser, summary="The current user")
def read_me(auth: AuthDep) -> schemas.CurrentUser:
    return _current_user_payload(auth)


@auth_router.post(
    "/password",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Change your own password",
)
def change_password(
    payload: schemas.ChangePasswordRequest,
    response: Response,
    request: Request,
    auth: AuthDep,
    db: DbDep,
    settings: SettingsDep,
    client_ip: ClientIpDep,
) -> Response:
    """Change your password. Every other session is invalidated (SPEC §4.1)."""
    if auth.user.password_hash is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This account has no password set.",
        )

    if not verify_password(auth.user.password_hash, payload.current_password):
        service.record_attempt(
            db, username=auth.user.username, client_ip=client_ip, succeeded=False
        )
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Current password is incorrect."
        )

    try:
        # Revokes all sessions, including this one.
        service.set_password(db, auth.user, payload.new_password)
    except (PasswordTooShortError, PasswordTooLongError) as exc:
        raise _password_error(exc) from exc

    # Issue a fresh session so the person who just changed their own password is
    # not logged out by their own action.
    issued = service.issue_session(
        db,
        settings,
        user=auth.user,
        user_agent=request.headers.get("user-agent"),
        client_ip=client_ip,
    )
    audit.record(
        db,
        AuditAction.PASSWORD_CHANGED,
        actor_id=auth.user.id,
        actor_label=auth.user.username,
        resource_type="user",
        resource_id=str(auth.user.id),
        client_ip=client_ip,
    )
    db.commit()

    _set_session_cookies(
        response,
        settings,
        token=issued.token,
        csrf_token=issued.csrf_token,
        max_age=settings.session_ttl_hours * 3600,
    )
    response.status_code = status.HTTP_204_NO_CONTENT
    return response


# --- users --------------------------------------------------------------------


@users_router.get(
    "",
    response_model=list[schemas.UserPublic],
    dependencies=[Depends(require(Action.READ, Module.USERS))],
    summary="List household members",
)
def list_users(db: DbDep) -> list[User]:
    return service.list_users(db)


@users_router.post(
    "",
    response_model=schemas.UserPublic,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require(Action.WRITE, Module.USERS))],
    summary="Add a household member",
)
def create_user(
    payload: schemas.CreateUserRequest,
    auth: AuthDep,
    db: DbDep,
    client_ip: ClientIpDep,
) -> User:
    if service.get_user_by_username(db, payload.username) is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="That username is taken.")

    try:
        user = service.create_user(
            db,
            username=payload.username,
            display_name=payload.display_name,
            password=payload.password,
            role=payload.role,
            avatar_color=payload.avatar_color,
        )
    except (PasswordTooShortError, PasswordTooLongError) as exc:
        raise _password_error(exc) from exc

    audit.record(
        db,
        AuditAction.USER_CREATED,
        actor_id=auth.user.id,
        actor_label=auth.user.username,
        resource_type="user",
        resource_id=str(user.id),
        client_ip=client_ip,
        detail={"username": user.username, "role": user.role},
    )
    db.commit()
    return user


@users_router.get(
    "/{user_id}/belongings",
    response_model=schemas.Belongings,
    dependencies=[Depends(require(Action.WRITE, Module.USERS))],
    summary="What deleting this member would destroy",
)
def belongings(user_id: UUID, db: DbDep) -> schemas.Belongings:
    """Counts, so a delete confirmation can name what it is about to remove.

    Deliberately unscoped: it counts rows, never returns any, and the whole
    point is to describe records the caller cannot otherwise see. A count of
    somebody else's private notes discloses that they have notes, which is
    exactly what an administrator about to delete them needs to know and no
    more than that.

    Health is a boolean rather than a count, for the reason `Belongings` gives.
    """
    from home_ops.modules.calendar.models import CalendarEvent
    from home_ops.modules.contacts.models import Contact
    from home_ops.modules.health.models import HEALTH_MODELS
    from home_ops.modules.kitchen.models import Recipe
    from home_ops.modules.kitchen.plan_models import MealPlanEntry
    from home_ops.modules.notes.models import Note
    from home_ops.modules.shopping.models import ShoppingList
    from home_ops.modules.tasks.models import Task

    def owned(model: Any) -> int:
        return int(
            db.scalar(
                select(func.count())
                .select_from(model)
                .where(model.owner_id == user_id)
                .execution_options(**{SCOPED_OPTION: True})
            )
            or 0
        )

    return schemas.Belongings(
        tasks=owned(Task),
        notes=owned(Note),
        recipes=owned(Recipe),
        events=owned(CalendarEvent),
        contacts=owned(Contact),
        shopping_lists=owned(ShoppingList),
        planned_meals=owned(MealPlanEntry),
        has_health_records=any(owned(model) for model in HEALTH_MODELS),
    )


@users_router.delete(
    "/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require(Action.WRITE, Module.USERS))],
    summary="Delete a member and everything they own",
)
def delete_user(
    user_id: UUID,
    auth: AuthDep,
    db: DbDep,
    client_ip: ClientIpDep,
) -> Response:
    """Remove a member outright. **This destroys everything they own.**

    Nineteen foreign keys into `users` cascade, so this takes their tasks,
    notes, recipes, calendar events, contacts, shopping lists, planned meals
    and health records with it. That is the point of the operation and the
    reason the UI makes somebody type the username before it is offered.

    Two refusals, both structural rather than advisory:

    * **Never yourself.** Deleting your own account mid-request destroys the
      session you are using and every record you own, on one click, with no
      way back. Suspending somebody else and then leaving is a decision with
      steps; this would not be.
    * **Never the last administrator.** A household with no admin cannot add
      members, change permissions or reset a password — it is locked out of its
      own management with no route back that does not involve the database.

    Suspending (`is_active = false`) is the non-destructive option and is what
    the UI offers first.
    """
    if user_id == auth.user.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="You cannot delete your own account.",
        )

    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such member.")

    if target.role == Role.ADMIN:
        remaining = (
            db.scalar(
                select(func.count())
                .select_from(User)
                .where(User.role == Role.ADMIN, User.id != user_id, User.is_active.is_(True))
            )
            or 0
        )
        if remaining == 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "This is the only administrator. Make somebody else an administrator first, "
                    "or the household cannot be managed."
                ),
            )

    # Recorded before the row goes, because afterwards there is nothing left to
    # describe it with.
    audit.record(
        db,
        AuditAction.USER_DELETED,
        actor_id=auth.user.id,
        actor_label=auth.user.username,
        resource_type="user",
        resource_id=str(target.id),
        client_ip=client_ip,
        detail={"username": target.username, "display_name": target.display_name},
    )
    db.delete(target)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@users_router.post(
    "/{user_id}/password",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require(Action.WRITE, Module.USERS))],
    summary="Set another member's password",
)
def reset_password(
    user_id: UUID,
    payload: schemas.ResetPasswordRequest,
    auth: AuthDep,
    db: DbDep,
    client_ip: ClientIpDep,
) -> Response:
    """Set somebody else's password, for when they have forgotten it.

    Three things hold this in place, and none of them is optional:

    * **`users` write**, so it is an administrator's capability rather than
      anybody's.
    * **Never your own account.** Changing your own password requires the
      current one (`POST /api/auth/password`); if this endpoint accepted the
      caller's own id it would be a way round that, and a stolen session could
      lock the real owner out without ever knowing their password. The refusal
      is the whole reason the check exists rather than being tidied away.
    * **Every session for that member is revoked**, which `service.set_password`
      does. Somebody whose password has just been reset should not still be
      logged in somewhere on the strength of the old one.

    Audited as its own action: changing your own password and changing
    somebody else's are different events, and only the second is worth
    noticing.
    """
    if user_id == auth.user.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Change your own password from your account settings, with your current one.",
        )

    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such member.")

    service.set_password(db, target, payload.new_password)
    audit.record(
        db,
        AuditAction.PASSWORD_RESET,
        actor_id=auth.user.id,
        actor_label=auth.user.username,
        resource_type="user",
        resource_id=str(target.id),
        client_ip=client_ip,
        # Who, never what. `audit.py` scrubs anyway; this simply does not offer
        # it a password to scrub.
        detail={"username": target.username},
    )
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@users_router.patch(
    "/{user_id}",
    response_model=schemas.UserPublic,
    summary="Update a member",
)
def update_user(
    user_id: UUID,
    payload: schemas.UpdateUserRequest,
    auth: AuthDep,
    db: DbDep,
    client_ip: ClientIpDep,
) -> User:
    """Profile fields need ownership or users:write; role changes need more.

    Deliberately finer-grained than a single dependency could express, so the
    checks are explicit here — each one delegating to `policy.py`.
    """
    target = service.get_user(db, user_id)
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such user.")

    if not can_administer_user(auth.principal, target.id, auth.deviations):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Not permitted to edit this user."
        )

    changes: dict[str, object] = {}

    if payload.display_name is not None:
        target.display_name = payload.display_name.strip()
        changes["display_name"] = target.display_name
    if payload.avatar_color is not None:
        target.avatar_color = payload.avatar_color
        changes["avatar_color"] = target.avatar_color

    new_role = payload.role
    role_changed = new_role is not None and new_role.value != target.role
    if new_role is not None and role_changed:
        if not can_change_role(auth.principal, target.id, auth.deviations):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Not permitted to change this user's role. You cannot change your own.",
            )
        if target.role == Role.ADMIN.value and _would_leave_no_admin(db, target):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="That would leave the household with no admin.",
            )
        changes["role"] = {"from": target.role, "to": new_role.value}
        target.role = new_role.value

    new_active = payload.is_active
    deactivating = new_active is not None and new_active != target.is_active
    if new_active is not None and deactivating:
        if not new_active:
            if auth.user.id == target.id:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="You cannot deactivate your own account.",
                )
            if target.role == Role.ADMIN.value and _would_leave_no_admin(db, target):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="That would leave the household with no admin.",
                )
            # A disabled account must not keep a live session.
            service.revoke_all_sessions(db, target.id)
        target.is_active = new_active
        changes["is_active"] = target.is_active

    db.flush()

    if changes:
        action = AuditAction.USER_UPDATED
        if role_changed:
            action = AuditAction.USER_ROLE_CHANGED
        elif deactivating:
            action = (
                AuditAction.USER_REACTIVATED if target.is_active else AuditAction.USER_DEACTIVATED
            )
        audit.record(
            db,
            action,
            actor_id=auth.user.id,
            actor_label=auth.user.username,
            resource_type="user",
            resource_id=str(target.id),
            client_ip=client_ip,
            detail=changes,
        )
    db.commit()
    return target


def _would_leave_no_admin(db: DbSessionType, target: User) -> bool:
    """True if demoting or disabling `target` removes the last active admin.

    A household with no admin cannot add one back, which is unrecoverable
    without shell access.
    """
    remaining = db.scalars(
        select(User).where(
            User.role == Role.ADMIN.value,
            User.is_active.is_(True),
            User.id != target.id,
        )
    ).first()
    return remaining is None


# --- permission deviations ----------------------------------------------------


@permissions_router.get(
    "",
    response_model=list[schemas.PermissionEntry],
    dependencies=[Depends(require(Action.WRITE, Module.USERS))],
    summary="List stored permission deviations",
)
def list_permissions(db: DbDep) -> list[AccessPermission]:
    """Only deviations are stored; defaults live in policy.ROLE_DEFAULTS."""
    return list(db.scalars(select(AccessPermission).order_by(AccessPermission.subject_id)))


@permissions_router.put(
    "",
    response_model=schemas.PermissionEntry,
    dependencies=[Depends(require(Action.WRITE, Module.USERS))],
    summary="Set a permission deviation",
)
def set_permission(
    payload: schemas.SetPermissionRequest,
    auth: AuthDep,
    db: DbDep,
    client_ip: ClientIpDep,
) -> AccessPermission:
    if payload.subject_type is SubjectType.ROLE and payload.subject_id == Role.ADMIN.value:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Admins bypass module access; a deviation for the admin role has no effect.",
        )

    existing = db.scalar(
        select(AccessPermission).where(
            AccessPermission.subject_type == payload.subject_type.value,
            AccessPermission.subject_id == payload.subject_id,
            AccessPermission.module == payload.module.value,
        )
    )
    if existing is None:
        existing = AccessPermission(
            subject_type=payload.subject_type.value,
            subject_id=payload.subject_id,
            module=payload.module.value,
            access=payload.access.value,
        )
        db.add(existing)
    else:
        existing.access = payload.access.value

    db.flush()
    audit.record(
        db,
        AuditAction.PERMISSION_CHANGED,
        actor_id=auth.user.id,
        actor_label=auth.user.username,
        resource_type="permission",
        resource_id=str(existing.id),
        client_ip=client_ip,
        detail={
            "subject_type": payload.subject_type.value,
            "subject_id": payload.subject_id,
            "module": payload.module.value,
            "access": payload.access.value,
        },
    )
    db.commit()
    return existing


@permissions_router.delete(
    "",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require(Action.WRITE, Module.USERS))],
    summary="Remove a deviation, restoring the role default",
)
def clear_permission(
    subject_type: SubjectType,
    subject_id: str,
    module: Module,
    auth: AuthDep,
    db: DbDep,
    client_ip: ClientIpDep,
) -> Response:
    db.execute(
        delete(AccessPermission).where(
            AccessPermission.subject_type == subject_type.value,
            AccessPermission.subject_id == subject_id,
            AccessPermission.module == module.value,
        )
    )
    audit.record(
        db,
        AuditAction.PERMISSION_CHANGED,
        actor_id=auth.user.id,
        actor_label=auth.user.username,
        resource_type="permission",
        client_ip=client_ip,
        detail={
            "subject_type": subject_type.value,
            "subject_id": subject_id,
            "module": module.value,
            "access": "(reset to role default)",
        },
    )
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- audit log ----------------------------------------------------------------


@audit_router.get(
    "",
    response_model=list[schemas.AuditEntry],
    dependencies=[Depends(require(Action.READ, Module.AUDIT))],
    summary="Read the audit log",
)
def read_audit_log(
    db: DbDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[AuditLogEntry]:
    return list(db.scalars(select(AuditLogEntry).order_by(AuditLogEntry.at.desc()).limit(limit)))


# Re-exported so main.py mounts one object per concern.
__all__ = [
    "audit_router",
    "auth_router",
    "current_auth",
    "permissions_router",
    "setup_router",
    "users_router",
]
