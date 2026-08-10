"""Request-scoped dependencies: who is calling, and may they do this.

Two design choices here are deliberate and worth stating.

**CSRF verification lives inside `current_auth`**, not in a separate dependency a
route could forget. Every mutating endpoint needs an authenticated caller, so
folding the check into the thing they all depend on makes "authenticated but
CSRF-unchecked" unrepresentable. The only unauthenticated mutating endpoints are
login and first-run setup, which are covered by the origin check plus
`SameSite=Lax`.

**Permission gates are built by `require()`**, so a route declares the module and
action it needs and the answer comes from `policy.py`. No route re-implements a
permission rule.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy.orm import Session as DbSession

from home_ops import security
from home_ops.config import Settings, get_settings
from home_ops.db import get_session
from home_ops.middleware.client_ip import is_in_networks, parse_ip, resolve_client_ip
from home_ops.modules.identity import service, token_service
from home_ops.modules.identity.models import Session, User
from home_ops.policy import Access, Action, Deviation, Module, Principal, can

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})

CSRF_HEADER = "X-CSRF-Token"


def db_session() -> Iterator[DbSession]:
    yield from get_session()


DbDep = Annotated[DbSession, Depends(db_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


def client_ip(request: Request, settings: SettingsDep) -> str:
    """The caller's address, resolved per SPEC §2.1's trust rules."""
    return resolve_client_ip(
        request.client.host if request.client else None,
        request.headers,
        trusted_proxies=settings.trusted_proxy_ips,
        tunnel_proxies=settings.tunnel_proxy_ips,
    )


ClientIpDep = Annotated[str, Depends(client_ip)]


def arrived_via_tunnel(request: Request, settings: SettingsDep) -> bool:
    """Whether this request came in over the Cloudflare tunnel.

    Same trust rule as `client_ip`: `X-Real-IP` is only meaningful when the
    immediate peer is the reverse proxy, so a direct caller cannot dress itself
    up as having come from anywhere. True only when that trusted hop was a
    configured tunnel address.

    With `TUNNEL_PROXY_IPS` unset this is always False, meaning "not known to be
    the tunnel" rather than "proven local" — callers must treat it that way.
    """
    if not settings.tunnel_proxy_ips:
        return False

    peer = parse_ip(request.client.host if request.client else None)
    if peer is None or not is_in_networks(peer, settings.trusted_proxy_ips):
        return False

    hop = parse_ip(request.headers.get("x-real-ip"))
    return hop is not None and is_in_networks(hop, settings.tunnel_proxy_ips)


TunnelDep = Annotated[bool, Depends(arrived_via_tunnel)]


@dataclass(frozen=True)
class AuthContext:
    """Everything a handler needs to know about the caller."""

    user: User
    #: None when the caller authenticated with an API token: a bearer request
    #: has no session and never creates one.
    session: Session | None
    principal: Principal
    deviations: tuple[Deviation, ...]
    #: The token's own per-module ceiling, when a token is being used. Empty
    #: means "not narrowed"; None means this is a session, not a token.
    token_ceiling: dict[str, Access] | None = None

    def can(self, action: Action, module: Module) -> bool:
        """Module access, with a token's scope applied **after** the role check.

        The order is the whole point. `policy.can` lets an admin bypass module
        access entirely, so a cap applied before it would be discarded and an
        admin's token would silently ignore its own scope — exactly the token
        you would most want narrowed. Capping afterwards means a token can only
        ever *reduce* what its owner could do, never add to it.
        """
        if not can(self.principal, action, module, self.deviations):
            return False
        if self.token_ceiling is None:
            return True
        return token_service.allows(self.token_ceiling, action, module)


def _unauthenticated() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Authentication required",
    )


def _verify_csrf(request: Request, session: Session, provided: str | None) -> None:
    """Double-submit, bound to the session.

    The CSRF cookie is readable by JavaScript by design — the frontend has to
    echo it into a header. What makes it safe is that a cross-site attacker can
    cause the *cookie* to be sent but cannot read it to set the *header*.

    Binding to the session (rather than comparing cookie against header alone)
    means a token harvested from one session cannot be replayed against another.
    """
    if request.method in SAFE_METHODS:
        return

    if not provided or not security.tokens_equal(
        security.hash_token(provided), session.csrf_token_hash
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Missing or invalid {CSRF_HEADER} header",
        )


def current_auth(
    request: Request,
    db: DbDep,
    settings: SettingsDep,
    csrf_token: Annotated[str | None, Header(alias=CSRF_HEADER)] = None,
) -> AuthContext:
    """Resolve the caller, or refuse the request.

    Also verifies CSRF on unsafe methods — see the module docstring for why that
    lives here rather than in a dependency of its own.

    The cookie is read from the request rather than declared with `Cookie(...)`
    because its name is configurable; a declared alias would have to be a
    literal and would silently ignore `SESSION_COOKIE_NAME`.
    """
    # A bearer token is the other way to be a principal (§4.10). Checked first
    # and returned from, so nothing below it — including CSRF, which a token
    # request has no ambient credential to need — applies to that path.
    bearer = _bearer_from(request)
    if bearer is not None:
        return _authenticate_token(db, bearer)

    session_token = request.cookies.get(settings.session_cookie_name)
    if not session_token:
        raise _unauthenticated()

    session = service.load_session(db, session_token)
    if session is None:
        raise _unauthenticated()

    user = service.get_user(db, session.user_id)
    if user is None or not user.is_active:
        # A deactivated user's sessions stop working immediately, without
        # waiting for a sweep.
        raise _unauthenticated()

    _verify_csrf(request, session, csrf_token)
    service.touch_session(db, settings, session)

    principal = service.principal_for(user)
    return AuthContext(
        user=user,
        session=session,
        principal=principal,
        deviations=service.load_deviations(db, principal),
    )


def _bearer_from(request: Request) -> str | None:
    """The token out of an `Authorization: Bearer …` header, if there is one."""
    header = request.headers.get("authorization")
    if not header:
        return None
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        return None
    return value.strip()


def _authenticate_token(db: DbSession, presented: str) -> AuthContext:
    """Resolve an API token into a principal, or refuse.

    **No CSRF check, deliberately.** CSRF exists because a browser sends a
    cookie automatically; a bearer header is not sent automatically, so there is
    nothing for a third-party page to trigger. The cookie path keeps its check,
    which is the half that matters.
    """
    token = token_service.load(db, presented)
    if token is None:
        raise _unauthenticated()

    user = service.get_user(db, token.user_id)
    if user is None or not user.is_active:
        # Suspending somebody stops their tokens too, immediately. A token that
        # outlived its owner's account would be the obvious way back in.
        raise _unauthenticated()

    allowed = token_service.spend_request(db, token)
    # Committed here rather than left to the route. A GET commits nothing, so
    # without this the counter and `last_used_at` are rolled back at the end of
    # every read — which would make the rate limit unenforceable against
    # exactly the requests most worth limiting.
    db.commit()

    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="This token is making too many requests. Try again in a minute.",
            headers={"Retry-After": "60"},
        )

    principal = service.principal_for(user)
    return AuthContext(
        user=user,
        session=None,
        principal=principal,
        deviations=service.load_deviations(db, principal),
        # Resolved per request, never stored: the owner's permissions may have
        # changed since the token was issued, and the lesser of the two wins.
        token_ceiling=token_service.ceiling(token),
    )


AuthDep = Annotated[AuthContext, Depends(current_auth)]


def require(action: Action, module: Module) -> Callable[[AuthContext], AuthContext]:
    """Build a dependency that enforces module access (SPEC §4.2).

    Usage: `dependencies=[Depends(require(Action.WRITE, Module.SETTINGS))]`.

    This is the module-access axis only. Per-item visibility is a separate
    concern handled by `scoping.visible`, and admins do not bypass that one.
    """

    def _dependency(auth: AuthDep) -> AuthContext:
        if not auth.can(action, module):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Requires {action.value} access to {module.value}",
            )
        return auth

    return _dependency
