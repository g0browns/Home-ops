# =============================================================================
# Home Ops — the whole application as ONE image.
#
#   docker build -t home-ops:single .
#   docker run -v home_ops_data:/data --env-file .env -p 8080:8080 home-ops:single
#
# nginx, uvicorn and Postgres in one container, supervised by s6-overlay, with a
# single /data volume. Built for people deploying this somewhere else: one image,
# one volume, one port, nothing to wire together.
#
# `docker-compose.yml` remains the better shape for running it at home — separate
# images, the official Postgres, and a small layer to re-pull when only the
# application changed. This is packaging, not a replacement.
#
# ---------------------------------------------------------------------------
# LAYER ORDER IS LOAD-BEARING. Read before editing.
#
# Everything that changes on an ordinary release must sit at the END of this
# file. A monolithic image re-pulls every layer from the first changed one
# onward, so putting `COPY src` above the apt install would make a one-line fix
# a ~450 MB download for every person who deployed it. The order is:
#
#   1. apt packages (Postgres, nginx)   — changes when a version is bumped
#   2. s6-overlay                       — changes when s6 is bumped
#   3. Python dependencies              — changes when pyproject.toml does
#   4. application code and the SPA     — changes every release
#
# Step 3 installs against a *stub* package so that dependencies land in their own
# layer; the real source is copied over it afterwards. Without that trick the
# editable install and the source share a layer, and every code change re-pulls
# every dependency.
# ---------------------------------------------------------------------------
#
# Debian rather than Alpine, deliberately: psycopg[binary] publishes manylinux
# wheels only, so on musl it would compile from source and this image would need
# a C toolchain and libpq-dev in it.
# =============================================================================

ARG PYTHON_VERSION=3.12
ARG POSTGRES_MAJOR=16
ARG S6_OVERLAY_VERSION=3.2.0.2


# -----------------------------------------------------------------------------
# The SPA. Built exactly as frontend/Dockerfile builds it — `npm run build`
# type-checks first, so a type error fails the image rather than shipping.
# -----------------------------------------------------------------------------
FROM node:22-alpine AS frontend-build

WORKDIR /app
COPY frontend/package.json ./
RUN npm install --no-audit --no-fund
COPY frontend/ ./
RUN npm run build


# -----------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS runtime

ARG POSTGRES_MAJOR
ARG S6_OVERLAY_VERSION
# Provided by BuildKit. Used only to pick the right s6 tarball.
ARG TARGETARCH

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PGDATA=/data/postgres \
    PATH=/usr/lib/postgresql/${POSTGRES_MAJOR}/bin:$PATH \
    # The database is in this container. Set here as well as in the service
    # scripts so that a `docker exec` — a psql session, a one-off script — gets
    # the right answer too. An .env carried over from the multi-container stack
    # still overrides this, which is what `scripts/app-env` is for.
    POSTGRES_HOST=127.0.0.1 \
    POSTGRES_PORT=5432 \
    # s6: let a service failing to start take the container down rather than
    # leaving it up and half-working, and give Postgres time to shut down
    # cleanly on `docker stop` — a killed Postgres recovers on next boot, but
    # recovery is a thing to avoid rather than rely on.
    S6_BEHAVIOUR_IF_STAGE2_FAILS=2 \
    S6_KILL_GRACETIME=10000 \
    S6_SERVICES_GRACETIME=10000

# --- 1. apt: Postgres and nginx ----------------------------------------------
# PGDG rather than Debian's own, so the major version is pinned by us and not by
# whatever the base image's release happens to ship.
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl gnupg xz-utils; \
    install -d /usr/share/postgresql-common/pgdg; \
    curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
        -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc; \
    echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] \
https://apt.postgresql.org/pub/repos/apt $(. /etc/os-release && echo $VERSION_CODENAME)-pgdg main" \
        > /etc/apt/sources.list.d/pgdg.list; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        "postgresql-${POSTGRES_MAJOR}" \
        "postgresql-client-${POSTGRES_MAJOR}" \
        nginx; \
    apt-get purge -y --auto-remove gnupg; \
    rm -rf /var/lib/apt/lists/*; \
    # Postgres's own package creates a cluster in /var/lib/postgresql. We put
    # PGDATA on the volume instead, so that one is dead weight and confusing to
    # find later.
    pg_dropcluster --stop "${POSTGRES_MAJOR}" main || true

# --- 2. s6-overlay ------------------------------------------------------------
RUN set -eux; \
    case "${TARGETARCH:-amd64}" in \
        amd64) s6_arch=x86_64 ;; \
        arm64) s6_arch=aarch64 ;; \
        arm)   s6_arch=armhf ;; \
        *) echo "unsupported architecture: ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    cd /tmp; \
    curl -fsSL -O "https://github.com/just-containers/s6-overlay/releases/download/v${S6_OVERLAY_VERSION}/s6-overlay-noarch.tar.xz"; \
    curl -fsSL -O "https://github.com/just-containers/s6-overlay/releases/download/v${S6_OVERLAY_VERSION}/s6-overlay-${s6_arch}.tar.xz"; \
    tar -C / -Jxpf s6-overlay-noarch.tar.xz; \
    tar -C / -Jxpf "s6-overlay-${s6_arch}.tar.xz"; \
    rm -f /tmp/s6-overlay-*.tar.xz

# The application's own user. Postgres brings its own (`postgres`) with the
# package, and nginx runs its workers as www-data.
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin appuser

WORKDIR /app

# --- 3. Python dependencies, in their own layer -------------------------------
# The stub package is the point: `pip install -e .` resolves and installs every
# dependency, and the real source lands in a later layer. Copying src first
# would put ~170 MB of wheels behind every code change.
COPY backend/pyproject.toml ./
RUN set -eux; \
    mkdir -p src/home_ops; \
    : > src/home_ops/__init__.py; \
    pip install --no-cache-dir -e .

# --- 4. Everything that changes on a release ---------------------------------
COPY backend/src ./src
COPY backend/alembic.ini ./
COPY backend/migrations ./migrations
COPY backend/docker/healthcheck.py ./docker/healthcheck.py

COPY frontend/nginx-security-headers.conf /etc/nginx/snippets/security-headers.conf
COPY frontend/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=frontend-build /app/dist /usr/share/nginx/html

COPY container/rootfs /
COPY container/healthcheck-all.py /usr/local/bin/healthcheck-all.py

RUN set -eux; \
    # nginx and the API are in the same container now, so the proxy target is
    # loopback. See the note at the top of nginx.conf: substituted at build time
    # rather than made an nginx variable, which would need a resolver.
    sed -i 's|__API_UPSTREAM__|127.0.0.1:8000|' /etc/nginx/conf.d/default.conf; \
    # Debian's nginx ships a default server on :80 that would shadow ours.
    rm -f /etc/nginx/sites-enabled/default; \
    # The exec bit does not survive a Windows checkout reliably, so it is set
    # here rather than trusted from git. Parenthesised: `find -name a -o -name b`
    # without them applies the implicit -print to the last term only.
    chmod +x /etc/s6-overlay/scripts/* /usr/local/bin/home-ops-migrate; \
    find /etc/s6-overlay/s6-rc.d -type f \( -name run -o -name up -o -name finish \) \
        -exec chmod +x {} +; \
    chown -R appuser:appuser /app

VOLUME ["/data"]
EXPOSE 8080

# `start-period` is generous because the first boot does real work: initdb, then
# creating the database, then optionally every migration from empty. None of that
# should count as a failure.
HEALTHCHECK --interval=15s --timeout=10s --start-period=180s --retries=5 \
    CMD ["python", "/usr/local/bin/healthcheck-all.py"]

ENTRYPOINT ["/init"]
