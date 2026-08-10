"""Application factory.

Everything is mounted under `/api`, which is what lets the browser use relative
paths and lets one origin serve both the app and the API (SPEC §2.1). The
reverse proxy in front — nginx in production, the Vite dev server locally —
routes `/api` here and everything else to the frontend.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI

from home_ops.config import Settings, get_settings
from home_ops.db import AppSession
from home_ops.middleware.request_log import RequestLogMiddleware
from home_ops.middleware.trusted_origin import configure_cors
from home_ops.modules.calendar import routes as calendar_routes
from home_ops.modules.contacts import routes as contacts_routes
from home_ops.modules.health import routes as health_module_routes
from home_ops.modules.healthcheck import routes as healthcheck_routes
from home_ops.modules.identity import routes as identity_routes
from home_ops.modules.identity import token_routes
from home_ops.modules.kitchen import plan_routes as meal_plan_routes
from home_ops.modules.kitchen import routes as kitchen_routes
from home_ops.modules.notes import routes as notes_routes
from home_ops.modules.settings import routes as settings_routes
from home_ops.modules.shopping import routes as shopping_routes
from home_ops.modules.tasks import routes as tasks_routes
from home_ops.scoping import install_scoping_guard

API_PREFIX = "/api"

LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"

DESCRIPTION = """\
Self-hosted household management system.

Reachable over HTTPS via Cloudflare Tunnel, and over plain HTTP via the
Tailscale tailnet and the LAN. Use relative paths; absolute URLs come from
configuration.
"""


def configure_logging(settings: Settings) -> None:
    logging.basicConfig(
        level=settings.log_level,
        format=LOG_FORMAT,
        force=True,
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings)

    # The OpenAPI schema is the foundation of SPEC §4.10's token-authenticated
    # API, so it costs nothing to have from day one. It is served only outside
    # production for now: in production it would sit unauthenticated on a public
    # hostname. Phase 8 revisits that when tokens exist to gate it with.
    docs_enabled = not settings.is_production

    app = FastAPI(
        title="Home Ops API",
        version=settings.app_version,
        description=DESCRIPTION,
        docs_url=f"{API_PREFIX}/docs" if docs_enabled else None,
        openapi_url=f"{API_PREFIX}/openapi.json" if docs_enabled else None,
        # Defaults to /docs/oauth2-redirect regardless of docs_url, which would
        # land outside /api and be swallowed by the SPA fallback in nginx.
        swagger_ui_oauth2_redirect_url=f"{API_PREFIX}/docs/oauth2-redirect",
        redoc_url=None,
    )

    # Added first, so it ends up innermost: Starlette wraps in reverse order and
    # CORS must be outermost to attach headers to error responses too.
    app.add_middleware(RequestLogMiddleware, settings=settings)
    configure_cors(app, settings)

    # The replacement for the database backstop we gave up by choosing
    # application-layer enforcement over RLS (SPEC §4.2): an unscoped read of a
    # visibility-bearing table raises rather than quietly returning rows.
    install_scoping_guard(AppSession)

    app.include_router(healthcheck_routes.router, prefix=API_PREFIX)
    app.include_router(identity_routes.setup_router, prefix=API_PREFIX)
    app.include_router(identity_routes.auth_router, prefix=API_PREFIX)
    app.include_router(identity_routes.users_router, prefix=API_PREFIX)
    app.include_router(identity_routes.permissions_router, prefix=API_PREFIX)
    app.include_router(identity_routes.audit_router, prefix=API_PREFIX)
    app.include_router(token_routes.router, prefix=API_PREFIX)
    app.include_router(token_routes.auth_router, prefix=API_PREFIX)
    app.include_router(settings_routes.router, prefix=API_PREFIX)
    app.include_router(tasks_routes.router, prefix=API_PREFIX)
    app.include_router(tasks_routes.categories_router, prefix=API_PREFIX)
    app.include_router(notes_routes.router, prefix=API_PREFIX)
    app.include_router(calendar_routes.router, prefix=API_PREFIX)
    app.include_router(calendar_routes.calendars_router, prefix=API_PREFIX)
    app.include_router(kitchen_routes.router, prefix=API_PREFIX)
    app.include_router(kitchen_routes.ingredients_router, prefix=API_PREFIX)
    app.include_router(kitchen_routes.units_router, prefix=API_PREFIX)
    app.include_router(meal_plan_routes.router, prefix=API_PREFIX)
    app.include_router(shopping_routes.router, prefix=API_PREFIX)
    app.include_router(contacts_routes.router, prefix=API_PREFIX)
    app.include_router(health_module_routes.router, prefix=API_PREFIX)

    return app


app = create_app()
