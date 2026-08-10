"""Container healthcheck for the api service.

Hits the public liveness endpoint over loopback. Written in Python so the image
needs neither curl nor wget — one fewer package to keep patched.

Exit 0 when healthy, 1 otherwise.
"""

from __future__ import annotations

import sys
import urllib.error
import urllib.request

URL = "http://127.0.0.1:8000/api/health"
TIMEOUT_SECONDS = 3


def main() -> int:
    try:
        with urllib.request.urlopen(URL, timeout=TIMEOUT_SECONDS) as response:
            return 0 if response.status == 200 else 1
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print(f"healthcheck failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
