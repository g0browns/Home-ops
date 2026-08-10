"""Where uploaded files go on disk (SPEC §4.6).

The only module that turns a database value into a filesystem path, and it is
small on purpose: path handling is where "user-controlled string" becomes
"arbitrary file" if you are careless.

Two rules:

* **Keys are generated here, never supplied.** `new_key()` returns a hex token;
  the database stores that, and nothing derived from a filename, a title, or any
  other request field ever reaches a path.
* **Every resolved path is checked to be inside the root anyway.** The key
  format makes traversal impossible, and the check runs regardless — belt and
  braces, because the cost is one comparison and the failure mode is reading
  `/etc/shadow`.

Files are fanned out one level (`ab/abcdef…`) so a household with thousands of
recipes does not end up with one directory holding thousands of entries.
"""

from __future__ import annotations

import re
import secrets
from pathlib import Path
from typing import Final

from home_ops.config import get_settings

#: Exactly what `new_key` produces, and the only thing `_resolve` will accept.
KEY_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{32}$")

FULL_SUFFIX: Final[str] = ".webp"
THUMB_SUFFIX: Final[str] = ".thumb.webp"


class BadKey(ValueError):
    """A stored key that does not match the generated format."""


def new_key() -> str:
    return secrets.token_hex(16)


def root() -> Path:
    return get_settings().upload_root / "recipes"


def _resolve(key: str, suffix: str) -> Path:
    if not KEY_PATTERN.match(key):
        raise BadKey("Not a storage key.")

    base = root().resolve()
    path = (base / key[:2] / f"{key}{suffix}").resolve()

    # Unreachable given the pattern above. Kept because "unreachable" is a
    # claim about today's code, and this is the check that stops a future edit
    # to KEY_PATTERN from turning into a file-read primitive.
    if not path.is_relative_to(base):
        raise BadKey("Resolved outside the upload root.")
    return path


def full_path(key: str) -> Path:
    return _resolve(key, FULL_SUFFIX)


def thumb_path(key: str) -> Path:
    return _resolve(key, THUMB_SUFFIX)


def write(key: str, full: bytes, thumb: bytes) -> None:
    target = full_path(key)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(full)
    thumb_path(key).write_bytes(thumb)


def delete(key: str) -> None:
    """Remove both files. Missing files are not an error.

    A row can outlive its files — a restore from a database-only backup, say —
    and deleting the recipe afterwards should still work rather than 500.
    """
    for path in (full_path(key), thumb_path(key)):
        path.unlink(missing_ok=True)


def read(key: str, *, thumb: bool = False) -> bytes | None:
    path = thumb_path(key) if thumb else full_path(key)
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None
