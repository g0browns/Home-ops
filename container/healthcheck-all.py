"""Container healthcheck for the single-container image.

It must fail if ANY of the three processes is down. A container reporting healthy
with a dead database is worse than having no healthcheck at all: it tells the
person looking at `docker ps` that the thing they are debugging is fine.

Python, and no wget or curl, for the reason `backend/docker/healthcheck.py`
already gives: one fewer package in the image to keep patched. The first version
of this file was a shell script calling wget, and it failed on the first boot
with `wget: not found` while every service behind it was working perfectly —
which is exactly the misleading signal this check exists to avoid.

Exit 0 when healthy, 1 otherwise, with a line on stderr naming what failed.
`docker inspect` keeps that line, so the reason survives to be read later.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request

TIMEOUT_SECONDS = 5

# Through nginx on 8080 rather than straight to uvicorn on 8000. That is the path
# a browser takes, so a broken proxy_pass fails this check instead of passing it.
CHECKS = (
    ("nginx is not serving the app", "http://127.0.0.1:8080/"),
    ("the API is not answering", "http://127.0.0.1:8080/api/health"),
)


def http_ok(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT_SECONDS) as response:
            return response.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def postgres_ok() -> bool:
    """`/api/health` is deliberately database-free, so this has to be separate.

    SPEC §8.6 has liveness verified over the public Cloudflare hostname, which is
    why it discloses nothing and touches no database — so a dead Postgres behind
    a live API is invisible to the checks above.
    """
    pg_isready = shutil.which("pg_isready") or "/usr/lib/postgresql/16/bin/pg_isready"
    try:
        return (
            subprocess.run(  # noqa: S603 - fixed binary, no shell, no user input
                [
                    pg_isready,
                    "-h",
                    "127.0.0.1",
                    "-p",
                    "5432",
                    "-U",
                    os.environ.get("POSTGRES_USER", "home_ops"),
                    "-q",
                ],
                timeout=TIMEOUT_SECONDS,
                check=False,
            ).returncode
            == 0
        )
    except (OSError, subprocess.SubprocessError):
        return False


def main() -> int:
    for reason, url in CHECKS:
        if not http_ok(url):
            print(f"unhealthy: {reason}", file=sys.stderr)
            return 1

    if not postgres_ok():
        print("unhealthy: Postgres is not ready", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
