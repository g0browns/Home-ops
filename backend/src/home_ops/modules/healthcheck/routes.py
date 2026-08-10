"""Health endpoints (SPEC §8.6).

Two endpoints with deliberately different audiences:

`GET /api/health` — liveness
    Public, and it must stay that way: SPEC §8.6 has this verified over all
    three access paths, one of which is a public Cloudflare hostname. So it
    returns `{"status": "ok"}` and nothing else. No version, no schema state,
    no database round-trip — a healthy process answers even while the database
    is down, which is what the container orchestrator needs to know.

`GET /api/health/ready` — readiness
    Reports whether this process can actually serve traffic: database reachable,
    schema at the expected revision. That is operational detail, so it sits
    behind `HEALTHCHECK_TOKEN` when one is configured. Returns 503 when not
    ready, so a proxy or `docker compose` can act on it.
"""

from __future__ import annotations

import secrets
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
from pydantic import BaseModel, Field

from home_ops.config import Settings, get_settings
from home_ops.db import probe_database
from home_ops.schema_state import migration_state

router = APIRouter(prefix="/health", tags=["health"])


class LivenessResponse(BaseModel):
    status: Literal["ok"] = "ok"


class DatabaseStatus(BaseModel):
    ok: bool
    latency_ms: float | None = None
    error: str | None = Field(
        default=None,
        description="Exception class name only. Driver errors can embed the DSN.",
    )


class MigrationStatus(BaseModel):
    current: list[str] = Field(description="Revisions the database reports as applied.")
    head: list[str] = Field(description="Revisions at the tip of the migrations on disk.")
    in_sync: bool
    error: str | None = None


class ReadinessResponse(BaseModel):
    status: Literal["ready", "not_ready"]
    version: str
    database: DatabaseStatus
    migration: MigrationStatus


def _verify_healthcheck_token(settings: Settings, provided: str | None) -> None:
    """Gate readiness detail when a token is configured.

    No token configured means no gate — reasonable on a private network, and
    documented as such in `.env.example`.
    """
    expected = settings.healthcheck_token
    if expected is None:
        return

    if provided is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="X-Healthcheck-Token header required",
        )

    # compare_digest over bytes: the str form raises on non-ASCII input, which a
    # caller controls.
    if not secrets.compare_digest(
        provided.encode("utf-8"), expected.get_secret_value().encode("utf-8")
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid healthcheck token",
        )


@router.get(
    "",
    response_model=LivenessResponse,
    summary="Liveness",
    description="Public. Returns ok if the process is serving. Does not touch the database.",
)
def liveness() -> LivenessResponse:
    return LivenessResponse()


@router.get(
    "/ready",
    response_model=ReadinessResponse,
    summary="Readiness",
    description="Database reachability and migration state. 503 when not ready.",
    responses={
        status.HTTP_401_UNAUTHORIZED: {"description": "Missing or invalid healthcheck token"},
        status.HTTP_503_SERVICE_UNAVAILABLE: {"description": "Not ready to serve traffic"},
    },
)
def readiness(
    response: Response,
    settings: Annotated[Settings, Depends(get_settings)],
    x_healthcheck_token: Annotated[str | None, Header()] = None,
) -> ReadinessResponse:
    _verify_healthcheck_token(settings, x_healthcheck_token)

    database = probe_database()
    migration = migration_state()
    ready = database.ok and migration.in_sync

    response.status_code = status.HTTP_200_OK if ready else status.HTTP_503_SERVICE_UNAVAILABLE
    return ReadinessResponse(
        status="ready" if ready else "not_ready",
        version=settings.app_version,
        database=DatabaseStatus(
            ok=database.ok,
            latency_ms=database.latency_ms,
            error=database.error,
        ),
        migration=MigrationStatus(
            current=migration.current,
            head=migration.head,
            in_sync=migration.in_sync,
            error=migration.error,
        ),
    )
