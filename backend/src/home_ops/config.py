"""Application configuration.

This module is the *only* place that reads the environment. Everything else
takes a :class:`Settings` instance, which keeps configuration testable and
makes the full set of knobs greppable in one file — `tests/test_config.py`
fails if any field here is missing from `.env.example` (SPEC §8.4).

Several fields exist specifically to satisfy SPEC §2.1, which requires the app
to work simultaneously over HTTPS (Cloudflare Tunnel), and plain HTTP over both
the tailnet and the LAN. Security decisions must never be derived from the
`Host` header, so origins and trusted proxies are explicit configuration.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import (
    BeforeValidator,
    Field,
    IPvAnyNetwork,
    SecretStr,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict
from sqlalchemy import URL
from sqlalchemy.engine import make_url

# /app in the container; <repo>/backend on a workstation.
BACKEND_ROOT = Path(__file__).resolve().parents[2]

LogLevel = Literal["CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"]

# SQLAlchemy URL prefixes we are prepared to talk to. Postgres only: the schema
# relies on real constraints and transactions (SPEC §3).
_ALLOWED_DB_SCHEMES = ("postgresql+psycopg://", "postgresql://")


def _split_csv(value: object) -> object:
    """Accept `a, b, c` for list fields."""
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return value


# `NoDecode` stops pydantic-settings JSON-decoding these at the environment
# source — it happens before validators run, so without it a plain
# `a,b,c` in .env raises a parse error rather than reaching `_split_csv`.
# JSON lists in a hand-edited .env would be miserable; CSV is the point.
CsvStrList = Annotated[list[str], NoDecode, BeforeValidator(_split_csv)]
CsvNetworkList = Annotated[list[IPvAnyNetwork], NoDecode, BeforeValidator(_split_csv)]


class Settings(BaseSettings):
    """Every environment variable the application reads."""

    model_config = SettingsConfigDict(
        # Convenience for host-native runs (pytest, alembic from a shell).
        # Inside Docker the values arrive from compose and this is a no-op.
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Application ---------------------------------------------------------
    app_env: Literal["development", "production"] = "development"
    app_version: str = "0.0.0-dev"
    log_level: LogLevel = "INFO"
    tz: str = "UTC"

    # --- Database ------------------------------------------------------------
    #
    # The password is typed **once**, here, and never again. `database_url` is
    # assembled from these below unless it is set explicitly, which is what
    # removes the class of bug that used to live in this file: a password is not
    # a URL component until something makes it one, and doing that by string
    # interpolation put "@" in the host and cost an afternoon.
    postgres_user: str = "home_ops"
    postgres_password: SecretStr = SecretStr("")
    postgres_db: str = "home_ops"

    postgres_host: str = "db"
    """`db` inside compose. Host-native runs set this to localhost."""

    postgres_port: int = Field(default=5432, ge=1, le=65535)

    database_url: str = ""
    """Assembled from the POSTGRES_* values when blank, which is the normal case.

    Set it explicitly only to reach a database those fields cannot describe — a
    managed instance, a connection needing query parameters, a scratch database
    in the test harness. An explicit value wins and is validated as strictly as
    a built one.
    """

    # --- The three access paths (SPEC §2.1) ----------------------------------
    #
    # The port is typed **once**, in `web_port`. Listing it again inside every
    # origin is how a port change half-lands: the app moves and one of the three
    # access paths silently stops passing its CSRF check.
    web_port: int = Field(default=8080, ge=1, le=65535)
    """The port the app is served on. Origins below are built with it."""

    app_hosts: CsvStrList = Field(default_factory=list)
    """Hostnames and IPs the app answers on — **names only**, no scheme, no port.

    `192.168.1.10, home-server.tailnet.ts.net`. Each becomes an `http://` origin
    on `web_port`. The HTTPS tunnel hostname does not belong here; it goes in
    `public_base_url`, because it is reached on 443 rather than on `web_port`.
    """

    public_base_url: str = ""
    """Absolute base URL for links that leave the app, and the HTTPS origin.

    Defaults to the first `app_hosts` entry on `web_port`. In a real deployment
    this is the Cloudflare hostname: the only path reachable from outside the
    house, and therefore the only sensible thing to put in an email.
    """

    trusted_origins: CsvStrList = Field(default_factory=list)
    """Every origin the app may be served on. Drives CORS, and CSRF from Phase 1.

    Derived from `app_hosts`, `web_port` and `public_base_url` when left empty.
    Set it explicitly only for an origin those cannot express.
    """

    session_cookie_secure: bool = False
    """Never hardcode this True: a Secure cookie is dropped over HTTP, which would
    lock out the tailnet and LAN paths entirely."""

    session_cookie_name: str = "home_ops_session"
    csrf_cookie_name: str = "home_ops_csrf"

    session_ttl_hours: int = Field(default=336, ge=1)
    """Rolling: activity extends it. 14 days suits a household on trusted networks."""

    compose_network_subnet: str = "172.28.0.0/16"
    """The internal compose network. `trusted_proxy_ips` defaults to it."""

    trusted_proxy_ips: CsvNetworkList = Field(default_factory=list)
    """Peers whose forwarding headers we honour — the reverse proxy, not clients.

    **Deliberately not derived from `compose_network_subnet`**, though it
    usually equals it. Empty means "trust nobody", and defaulting it to a subnet
    would turn that into "trust every container on the network" for anyone who
    never set it. Deriving the database URL is a convenience; deriving who may
    tell us a client's IP address is a security decision, and it stays explicit.
    """

    tunnel_proxy_ips: CsvNetworkList = Field(default_factory=list)
    """cloudflared's address(es). `CF-Connecting-IP` is trusted only from here, so
    only on the tunnel path. Empty means never trusted, which is the safe default."""

    # --- Authentication (SPEC §4.1) ------------------------------------------
    login_failure_window_minutes: int = Field(default=15, ge=1)
    """How far back failed attempts are counted for both limits below."""

    login_max_failures_per_username: int = Field(default=5, ge=1)
    """Lockout threshold. Counted against the username *attempted*, whether or not
    it exists, so this cannot be used to discover which accounts are real."""

    login_max_failures_per_ip: int = Field(default=20, ge=1)
    """Catches an attacker spraying many usernames from one address."""

    # --- First-run setup -----------------------------------------------------
    setup_allow_tunnel_path: bool = False
    """Whether the first-run wizard may be claimed over the Cloudflare tunnel.

    Off by default. While the household has no users, the setup endpoint is
    unauthenticated by nature — whoever reaches it first becomes admin — and the
    tunnel is the one path a stranger can reach. Leaving this false keeps setup
    to the tailnet and LAN, where you would realistically do it anyway. It has no
    effect once a user exists; the endpoint is gone by then."""

    # --- Uploads (SPEC §4.6) -------------------------------------------------
    upload_root: Path = Path("/data/uploads")
    """Where uploaded files live. A Docker volume in every deployment.

    **A database dump is no longer a complete backup on its own.** `backup.ps1`
    and `backup.sh` produce one archive covering both this directory and the
    dump, and `restore` puts both back — see §2.2. Anything that changes this
    path has to change those scripts, and `tests/test_backup_contract.py`
    asserts they still name it."""

    upload_max_bytes: int = Field(default=10 * 1024 * 1024, ge=1024)
    """Cap on a single upload, before decoding. The pixel cap in `images.py`
    matters more: a 2 KB PNG can decode to gigabytes."""

    # --- Health checks -------------------------------------------------------
    healthcheck_token: SecretStr | None = None
    """When set, `GET /api/health/ready` requires a matching `X-Healthcheck-Token`.
    The liveness endpoint stays public and discloses nothing either way."""

    @field_validator("healthcheck_token", mode="before")
    @classmethod
    def _blank_token_means_unset(cls, value: object) -> object:
        """`HEALTHCHECK_TOKEN=` in a .env file means "no token", not "empty token".

        Without this the readiness gate switches on and then rejects every
        request, including one presenting the empty string it was configured
        with — a lockout that looks like a code bug rather than a config one.
        """
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("database_url")
    @classmethod
    def _validate_database_url(cls, value: str) -> str:
        if not value:
            return value  # blank means "build it from the POSTGRES_* values"
        if not value.startswith(_ALLOWED_DB_SCHEMES):
            allowed = " or ".join(_ALLOWED_DB_SCHEMES)
            raise ValueError(f"DATABASE_URL must start with {allowed}, got {value[:24]!r}")

        # A password containing "@" silently reshapes the whole URL. SQLAlchemy
        # reads the password as `[^@]*`, so it ends at the *first* "@" and
        # everything after it becomes the host: a password of "abc@7CtV" turns
        # `...:abc@7CtV@db:5432/...` into host "7CtV@db".
        #
        # Left alone, that surfaces minutes later as
        # `failed to resolve host '7CtV@db'` — a DNS error naming a host nobody
        # typed, from a password nobody suspects, at the one moment somebody is
        # setting a strong random password for the first time. No real hostname
        # contains "@", so this is a reliable signal rather than a guess.
        try:
            host = make_url(value).host or ""
        except Exception:
            raise ValueError(
                "DATABASE_URL could not be parsed. If the password contains "
                "@ : / ? # or [ ], percent-encode it (@ becomes %40)."
            ) from None
        if "@" in host:
            # Deliberately *not* quoting the parsed host: everything in it before
            # the "@" is the tail of the password, and this message goes to logs.
            raise ValueError(
                "DATABASE_URL's password contains an unencoded '@', so the host was "
                "parsed as part of the password. Percent-encode it ('@' becomes "
                "'%40'), or — since docker-compose.yml interpolates POSTGRES_PASSWORD "
                "straight into this URL — use a password of letters and digits only."
            )
        return value

    @field_validator("trusted_origins")
    @classmethod
    def _validate_origins(cls, value: list[str]) -> list[str]:
        for origin in value:
            if not origin.startswith(("http://", "https://")):
                raise ValueError(f"TRUSTED_ORIGINS entry must include a scheme: {origin!r}")
            if origin.endswith("/"):
                raise ValueError(
                    f"TRUSTED_ORIGINS entry must not have a trailing slash: {origin!r}"
                )
        return value

    @model_validator(mode="after")
    def _derive_what_can_be_derived(self) -> Settings:
        """Fill in everything that repeats a value typed elsewhere.

        Each of these was a line in `.env` that restated something already
        stated: the password inside a URL, the port inside three origins, the
        subnet inside the proxy list. Every one of them is a place for two
        copies to disagree, and in each case the disagreement is quiet — a DNS
        error naming a host nobody typed, one access path failing CSRF, or
        forwarding headers trusted from the wrong peer.

        An explicit value always wins, so nothing here takes an option away.
        """
        if not self.database_url:
            # URL.create quotes each component, so a password containing @ : /
            # or # is carried correctly instead of reshaping the URL around it.
            self.database_url = URL.create(
                drivername="postgresql+psycopg",
                username=self.postgres_user,
                password=self.postgres_password.get_secret_value() or None,
                host=self.postgres_host,
                port=self.postgres_port,
                database=self.postgres_db,
            ).render_as_string(hide_password=False)

        if not self.public_base_url and self.app_hosts:
            self.public_base_url = self._origin(self.app_hosts[0])

        if not self.trusted_origins:
            # The tunnel hostname arrives on 443 rather than on web_port, so it
            # is taken whole from public_base_url rather than rebuilt.
            origins = [self._origin(host) for host in self.app_hosts]
            public = self.public_base_url.rstrip("/")
            if public and public not in origins:
                origins.append(public)
            self.trusted_origins = origins

        return self

    def _origin(self, host: str) -> str:
        """`host` -> `http://host:port`, omitting the port when it is the default.

        Plain HTTP: two of the three access paths (SPEC §2.1) have no TLS, and
        an origin has to match what the browser actually sends byte for byte —
        including the absence of `:80`.
        """
        host = host.strip().rstrip("/")
        if host.startswith(("http://", "https://")):
            return host
        return f"http://{host}" if self.web_port == 80 else f"http://{host}:{self.web_port}"

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings, read from the environment once.

    Tests that need different values call `get_settings.cache_clear()`.
    """
    # No `type: ignore` needed any more: every field has a default now that
    # `database_url` is assembled rather than required.
    return Settings()
