"""API token endpoints (SPEC §4.10).

**Your own tokens, always.** There is no endpoint that lists or revokes
somebody else's. A token is a credential belonging to a person, and an
administrator who could mint one for another member could act as them while the
audit log recorded that member's name — which is worse than no audit log,
because it reads as evidence.

**The plaintext is returned once**, by the create call, and is not recoverable
afterwards by anybody, including whoever runs the database. Everything else in
this module talks about tokens without ever being able to produce one.
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, BeforeValidator, Field

from home_ops import audit
from home_ops.audit import AuditAction
from home_ops.dependencies import AuthDep, ClientIpDep, DbDep, SettingsDep
from home_ops.modules.identity import service, token_service
from home_ops.modules.identity.token_models import MAX_TOKEN_NAME, ApiToken
from home_ops.policy import Access, Module

router = APIRouter(prefix="/tokens", tags=["tokens"])

#: `POST /api/auth/token` sits here rather than beside `/auth/login` because
#: everything it returns is a token shape, and a response carrying a credential
#: should be defined once. Mounted separately in `main.py`, which is how every
#: other module with two surfaces does it.
auth_router = APIRouter(prefix="/auth", tags=["auth"])

Stripped = BeforeValidator(lambda value: value.strip() if isinstance(value, str) else value)
TokenName = Annotated[str, Stripped, Field(min_length=1, max_length=MAX_TOKEN_NAME)]


class ScopeOut(BaseModel):
    module: Module
    access: Access


class TokenOut(BaseModel):
    id: UUID
    name: str
    #: The first few characters, so a list can name which token this is. Never
    #: enough to be a credential.
    prefix: str
    created_at: dt.datetime
    last_used_at: dt.datetime | None
    expires_at: dt.datetime | None
    revoked_at: dt.datetime | None
    #: Empty means the token is not narrowed, so its owner's own permissions are
    #: the only limit.
    scopes: list[ScopeOut] = Field(default_factory=list)


class TokenCreated(TokenOut):
    """The one response that carries the secret.

    Named differently from `TokenOut` so it is obvious at a glance which shape
    contains a credential and which does not.
    """

    token: str


class TokenIn(BaseModel):
    name: TokenName
    #: Per-module ceiling. Omitted entirely means "everything I can do"; naming
    #: *any* module narrows the token to those modules alone.
    scopes: dict[Module, Access] = Field(default_factory=dict)
    expires_at: dt.datetime | None = None


def _out(token: ApiToken) -> TokenOut:
    return TokenOut(
        id=token.id,
        name=token.name,
        prefix=token.prefix,
        created_at=token.created_at,
        last_used_at=token.last_used_at,
        expires_at=token.expires_at,
        revoked_at=token.revoked_at,
        scopes=[
            ScopeOut(module=Module(scope.module), access=Access(scope.access))
            for scope in token.scopes
        ],
    )


@router.get(
    "",
    response_model=list[TokenOut],
    summary="Your API tokens",
)
def list_tokens(db: DbDep, auth: AuthDep) -> list[TokenOut]:
    return [_out(token) for token in token_service.list_for(db, auth.user.id)]


@router.post(
    "",
    response_model=TokenCreated,
    status_code=status.HTTP_201_CREATED,
    summary="Create a token, and see it once",
)
def create_token(
    payload: TokenIn, db: DbDep, auth: AuthDep, client_ip: ClientIpDep
) -> TokenCreated:
    """Issue a token for **yourself**.

    Gated on nothing beyond being signed in, deliberately. A token can never
    exceed what its owner could already do by logging in, so requiring a
    *permission* to hold one protects nothing and excludes the wrong people:
    gating it on `settings` locked out read-only members, who have no settings
    access at all and every reason to want a read-only token.
    """
    if auth.token_ceiling is not None:
        # A token cannot mint another token. Otherwise a narrowed token would be
        # one request away from an unnarrowed one, and the scope would be
        # decoration.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Sign in to create a token. A token cannot create another one.",
        )

    try:
        token, plaintext = token_service.issue(
            db,
            auth.user.id,
            name=payload.name,
            scopes=payload.scopes,
            expires_at=payload.expires_at,
        )
    except token_service.TooManyTokens as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    audit.record(
        db,
        AuditAction.TOKEN_CREATED,
        actor_id=auth.user.id,
        actor_label=auth.user.username,
        resource_type="api_token",
        resource_id=str(token.id),
        client_ip=client_ip,
        # The name and the scope, never the token. `audit.py` scrubs anyway;
        # this does not offer it anything to scrub.
        detail={"name": token.name, "scopes": sorted(m.value for m in payload.scopes)},
    )
    db.commit()

    return TokenCreated(**_out(token).model_dump(), token=plaintext)


class PurgeResult(BaseModel):
    #: How many rows went, so the screen can say it rather than guess.
    deleted: int


#: **Declared before `/{token_id}`, and it has to be.** FastAPI matches in
#: declaration order, and `token_id` is a `UUID`, so the other way round
#: `DELETE /tokens/revoked` is matched by the parameterised route and answered
#: with a 422 about a malformed UUID — a confusing error for a correct request.
@router.delete(
    "/revoked",
    response_model=PurgeResult,
    summary="Delete your revoked tokens",
)
def clear_revoked_tokens(db: DbDep, auth: AuthDep, client_ip: ClientIpDep) -> PurgeResult:
    """Tidy the list: remove the rows for tokens you have already revoked.

    Revoking keeps the row on purpose, and for one token that is right. For a
    list grown long — mostly in development, where a device is paired and
    re-paired — it stops the list being readable, and an unreadable list of
    credentials is its own hazard, because the *live* ones are what matter.

    **Nothing live is touched**, so this cannot take away access by accident;
    a revoked token already authenticates nothing. Expired-but-not-revoked
    tokens stay, because nobody has decided about those yet.

    **Refused on the bearer path.** A token may revoke, but it may not erase
    the record of its siblings: a stolen credential should not be able to tidy
    away the evidence of what else was issued. Same reasoning as a token being
    unable to mint another one.
    """
    if auth.token_ceiling is not None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Sign in to clear revoked tokens. A token cannot delete token records.",
        )

    deleted = token_service.purge_revoked(db, auth.user.id)

    # Only when something actually went. A no-op is not an event, and an audit
    # log padded with them is one people stop reading.
    if deleted:
        audit.record(
            db,
            AuditAction.TOKEN_PURGED,
            actor_id=auth.user.id,
            actor_label=auth.user.username,
            resource_type="api_token",
            client_ip=client_ip,
            detail={"deleted": deleted},
        )
    db.commit()

    return PurgeResult(deleted=deleted)


@router.delete(
    "/{token_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke a token",
)
def revoke_token(token_id: UUID, db: DbDep, auth: AuthDep, client_ip: ClientIpDep) -> None:
    """Revoke one of your own. Somebody else's is a 404, not a 403.

    The row stays: deleting it would take the last-used record with it, and
    "when was this last used" is exactly what somebody asks *after* revoking
    something they did not recognise.
    """
    token = next(
        (
            candidate
            for candidate in token_service.list_for(db, auth.user.id)
            if candidate.id == token_id
        ),
        None,
    )
    if token is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="No such token.")

    token_service.revoke(db, token)
    audit.record(
        db,
        AuditAction.TOKEN_REVOKED,
        actor_id=auth.user.id,
        actor_label=auth.user.username,
        resource_type="api_token",
        resource_id=str(token.id),
        client_ip=client_ip,
        detail={"name": token.name},
    )
    db.commit()


# --- a token from a password, for clients that cannot hold a cookie -------------


class CredentialTokenIn(BaseModel):
    """Credentials, plus what to call the device they came from."""

    username: str
    password: str
    #: Shown in the owner's token list. A token nobody can identify is a token
    #: nobody dares revoke, and this is the one creation path where the person
    #: naming it is not sitting in front of the settings screen.
    name: TokenName = "Mobile device"
    scopes: dict[Module, Access] = Field(default_factory=dict)
    expires_at: dt.datetime | None = None


@auth_router.post(
    "/token",
    response_model=TokenCreated,
    status_code=status.HTTP_201_CREATED,
    summary="Exchange a username and password for an API token",
)
def issue_token_for_credentials(
    payload: CredentialTokenIn,
    db: DbDep,
    settings: SettingsDep,
    client_ip: ClientIpDep,
) -> TokenCreated:
    """Sign in from a client that cannot use cookies.

    **Why this exists.** Session cookies are `SameSite=Lax` and carry no
    `Domain`, deliberately — that is what keeps the three access paths (§2.1)
    from colliding. A consequence nobody had to face until now is that a client
    on a *different origin* can never hold one, so it could never reach
    `POST /api/tokens`, which requires a session. That left the bearer path
    reachable in principle and unreachable in practice for anything but a token
    pasted in by hand.

    **This is a new way to turn a password into a long-lived credential**, so it
    is deliberately no weaker than `/auth/login`: the same
    `service.authenticate` does the work, which means the same lockout, the same
    recorded attempts, the same constant-ish timing, and the same refusal to say
    *which* part was wrong. Re-implementing the check here would have been the
    way to get one of those subtly different.

    There is no refresh token, because there is nothing to refresh: the token is
    the credential, it is revocable from Settings, and `expires_at` bounds it if
    the caller asks for that.
    """
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
            detail={"username": payload.username, "via": "token"},
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
            detail={"username": payload.username, "reason": result.outcome.value, "via": "token"},
        )
        db.commit()
        # One message for every failure mode, exactly as `/auth/login` does.
        # Distinguishing "no such user" from "wrong password" from "disabled"
        # hands out an enumeration oracle for free.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password.",
        )

    user = result.user

    # **Nothing is revoked here, deliberately.**
    #
    # This used to replace any live token with the same name, so that re-pairing
    # a device did not consume another of the twenty-five slots. It is a bad
    # trade: a name is a label a person types, the default is something like
    # "My phone", and two phones that share one would revoke each other on every
    # sign-in — an app that logs you out whenever somebody else opens theirs.
    # We watched exactly that happen.
    #
    # A client that wants to replace its own credential can: it knows the id of
    # the token it is holding, and `DELETE /api/tokens/{id}` revokes it. That is
    # precise, because only the device that owns a token knows which one it is,
    # where a name is a guess about identity that the server has no business
    # making.
    #
    # The cap still protects the table, and reaching it is now a 400 that says
    # so rather than a silent revocation of something in use.

    try:
        token, plaintext = token_service.issue(
            db,
            user.id,
            name=payload.name,
            scopes=payload.scopes,
            expires_at=payload.expires_at,
        )
    except token_service.TooManyTokens as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    audit.record(
        db,
        AuditAction.TOKEN_ISSUED_FROM_PASSWORD,
        actor_id=user.id,
        actor_label=user.username,
        resource_type="api_token",
        resource_id=str(token.id),
        client_ip=client_ip,
        detail={"name": token.name, "scopes": sorted(m.value for m in payload.scopes)},
    )
    db.commit()

    return TokenCreated(**_out(token).model_dump(), token=plaintext)
