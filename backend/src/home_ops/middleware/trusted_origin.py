"""CORS configured from the trusted-origin allowlist (SPEC §2.1).

The app answers on three origins simultaneously, so the allowlist comes from
`TRUSTED_ORIGINS` and never from the `Host` header. Two consequences:

* `allow_origins` is always an explicit list. `"*"` is incompatible with
  credentialed requests, and would defeat the point besides.
* An empty list in production is a configuration error, not a default. Starting
  anyway would produce an app that works over whichever path the developer
  happened to test and fails on the other two. Better to refuse to start.

Phase 1 reuses this same allowlist for CSRF origin checks.
"""

from __future__ import annotations

from fastapi import FastAPI
from starlette.middleware.cors import CORSMiddleware

from home_ops.config import Settings

ALLOWED_METHODS = ["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"]

ALLOWED_HEADERS = [
    "accept",
    "authorization",
    "content-type",
    #: `If-None-Match` is **not** a CORS-safelisted request header, so without
    #: naming it here the preflight for a conditional GET is refused and the
    #: request never leaves the browser. Easy to miss, because the symptom is
    #: not "the ETag was ignored" — it is the whole request failing, and only
    #: from another origin. `GET /api/shopping-lists` is the one endpoint that
    #: answers `304`, and this is what lets a client ask for one.
    "if-none-match",
    "x-csrf-token",
    "x-healthcheck-token",
]

#: Response headers a cross-origin client is allowed to *read*. Without this the
#: browser hides everything but the CORS-safelisted set, and two things the API
#: says become unsayable across an origin:
#:
#: * `ETag` — `GET /api/shopping-lists` answers `304` to a matching
#:   `If-None-Match`, but a client that cannot read the tag can never send one,
#:   so conditional polling degrades to a full download every time.
#: * `Retry-After` — a rate-limited client that cannot read it has to guess how
#:   long to wait, and guessing wrong is what turns a limit into a stampede.
#:
#: Both are safe to expose: neither carries anything a caller did not already
#: have, and both exist to be acted on.
EXPOSED_HEADERS = ["ETag", "Retry-After"]

PREFLIGHT_CACHE_SECONDS = 600


class TrustedOriginConfigError(RuntimeError):
    """Raised at startup when the origin allowlist cannot be honoured."""


def configure_cors(app: FastAPI, settings: Settings) -> None:
    if settings.is_production and not settings.trusted_origins:
        raise TrustedOriginConfigError(
            "TRUSTED_ORIGINS is empty. Set it to every origin the app is reached on "
            "(Cloudflare hostname, tailnet name, LAN IP) — see SPEC §2.1."
        )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.trusted_origins),
        allow_credentials=True,
        allow_methods=ALLOWED_METHODS,
        allow_headers=ALLOWED_HEADERS,
        expose_headers=EXPOSED_HEADERS,
        max_age=PREFLIGHT_CACHE_SECONDS,
    )
