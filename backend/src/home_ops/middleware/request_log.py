"""Request logging.

Deliberately modest, but it is the first consumer of the client-IP resolution in
`client_ip.py`, which keeps that logic exercised rather than sitting unused until
rate limiting needs it in Phase 1.

Two things it does not do: log query strings or request bodies (both will carry
user data in later phases), and log the liveness endpoint (the container
healthcheck hits it every ten seconds and would bury everything else).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

from home_ops.config import Settings
from home_ops.middleware.client_ip import resolve_client_ip

logger = logging.getLogger("home_ops.request")

QUIET_PATHS = frozenset({"/api/health"})


class RequestLogMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        super().__init__(app)
        self._settings = settings

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        started = time.perf_counter()
        response = await call_next(request)
        elapsed_ms = (time.perf_counter() - started) * 1000

        if request.url.path in QUIET_PATHS:
            return response

        client = resolve_client_ip(
            request.client.host if request.client else None,
            request.headers,
            trusted_proxies=self._settings.trusted_proxy_ips,
            tunnel_proxies=self._settings.tunnel_proxy_ips,
        )
        logger.info(
            "%s %s -> %d (%.1fms) client=%s",
            request.method,
            request.url.path,
            response.status_code,
            elapsed_ms,
            client,
        )
        return response
