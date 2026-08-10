"""Password hashing and opaque token handling (SPEC §4.1).

Two rules govern everything here:

**Passwords are hashed with Argon2id**, and normalised to Unicode NFC first.
Without normalisation the same typed password can produce different bytes
depending on the client's keyboard or platform — macOS hands over decomposed
forms where Windows hands over composed ones — so a password with an accent in
it would work on the machine that set it and nowhere else. Yuvomi does the same
thing for the same reason.

**Tokens are stored hashed, never in the clear.** Session tokens are high-entropy
random values, so a single SHA-256 is sufficient (there is nothing to brute
force) and it means a `pg_dump` backup, or anyone who reads the table, holds no
usable session. Comparison is constant-time.
"""

from __future__ import annotations

import hashlib
import secrets
import unicodedata

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

# Defaults from argon2-cffi, which tracks the RFC 9106 recommendations. Explicit
# rather than implicit so a future change is a visible diff: raising these later
# is safe, because `needs_rehash` upgrades a user's hash on their next login.
_hasher = PasswordHasher(
    time_cost=3,
    memory_cost=64 * 1024,  # 64 MiB
    parallelism=4,
    hash_len=32,
    salt_len=16,
)

# 256 bits. token_urlsafe(32) yields 43 characters, cookie-safe.
TOKEN_BYTES = 32

# Rejecting absurdly long input matters: Argon2 cost is driven by the parameters
# above rather than by length, but an unbounded field is still free work for an
# attacker, and hashing happens before authentication.
MAX_PASSWORD_LENGTH = 1024
MIN_PASSWORD_LENGTH = 12


class PasswordTooShortError(ValueError):
    pass


class PasswordTooLongError(ValueError):
    pass


def normalize_password(password: str) -> str:
    """NFC-normalise so the same typed password hashes identically everywhere."""
    return unicodedata.normalize("NFC", password)


def validate_password_length(password: str) -> None:
    """Length is the only rule we impose.

    Composition rules (an uppercase, a digit, a symbol) push people towards
    predictable substitutions and are not worth the friction for a household
    application. Length is what actually helps.
    """
    normalized = normalize_password(password)
    if len(normalized) < MIN_PASSWORD_LENGTH:
        raise PasswordTooShortError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
    if len(normalized) > MAX_PASSWORD_LENGTH:
        raise PasswordTooLongError(f"Password must be at most {MAX_PASSWORD_LENGTH} characters.")


def hash_password(password: str) -> str:
    validate_password_length(password)
    return _hasher.hash(normalize_password(password))


def verify_password(password_hash: str, password: str) -> bool:
    """Constant-time verify. Returns False rather than raising on any failure."""
    try:
        return _hasher.verify(password_hash, normalize_password(password))
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    """True when the stored hash predates the current cost parameters.

    Call after a successful verify and re-hash if set, so raising the cost
    later upgrades users transparently instead of stranding old hashes.
    """
    try:
        return _hasher.check_needs_rehash(password_hash)
    except InvalidHashError:
        return True


def generate_token() -> str:
    """A new opaque session token. Returned once; only its hash is stored."""
    return secrets.token_urlsafe(TOKEN_BYTES)


def hash_token(token: str) -> str:
    """SHA-256 of a token, for storage and lookup.

    Deliberately not Argon2: tokens are 256 bits of entropy from `secrets`, so
    there is no dictionary to attack, and a session lookup happens on every
    request — a deliberately slow hash there would be a self-inflicted denial of
    service.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def tokens_equal(left: str, right: str) -> bool:
    """Constant-time comparison for tokens and CSRF values."""
    return secrets.compare_digest(left.encode("utf-8"), right.encode("utf-8"))
