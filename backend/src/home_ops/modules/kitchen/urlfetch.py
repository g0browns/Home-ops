"""Fetching a URL the user typed (SPEC §4.6 recipe import).

**This is the most dangerous code in the project**, and it is worth being blunt
about why. Everywhere else, the server acts on data the user sent. Here it makes
a network request *to an address the user chose*, from inside the house — a box
that sits on a tailnet and a LAN alongside the router, the NAS, the printer, and
whatever else has a web interface and no password because it is "only on the
local network". An unguarded fetcher turns the recipe importer into a probe for
every one of them. That is server-side request forgery, and the mitigations
below are the whole point of this module.

What it enforces:

1. **`http` and `https` only, on their normal ports.** No `file:`, no `gopher:`,
   no `redis:`, no port 22.
2. **Every resolved address is checked**, not the hostname. `localhost` is easy
   to block and useless to block on its own: a name under the attacker's control
   can resolve to 127.0.0.1, and `http://127.0.0.1.nip.io/` already does.
   Loopback, private, link-local (including the cloud metadata address),
   multicast, reserved and unspecified ranges are all refused — every address a
   name resolves to, not just the first.
3. **The connection is pinned to the address that was checked.** Validating a
   name and then handing the *name* to the HTTP client leaves a window where DNS
   can answer differently the second time — DNS rebinding, and it is a real
   attack, not a theoretical one. We connect to the vetted IP and carry the
   hostname in the `Host` header and the TLS SNI, so certificate verification
   still works and the address cannot change underneath us.
4. **Redirects are followed by hand, and every hop is re-validated.** A public
   URL redirecting to `http://192.168.1.1/` is the oldest trick here.
5. **Size and time are capped**, and the size cap is enforced while streaming
   rather than after, so a response that never ends cannot exhaust memory.
6. **Nothing is sent that identifies the household.** No cookies, no auth
   headers, no referrer.

None of this makes fetching arbitrary URLs *safe*. It makes it defensible, and
the residual risk — that a public host we are allowed to reach is itself
interesting — is the risk inherent in the feature §4.6 asks for.
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from typing import Final
from urllib.parse import urlparse, urlunparse

# The package is `httpx2` — the successor to httpx 0.x, which starlette's
# TestClient already required. It installs under its own module name, so the
# alias keeps the code below reading as ordinary httpx.
import httpx2 as httpx

MAX_BYTES: Final[int] = 2 * 1024 * 1024
MAX_REDIRECTS: Final[int] = 3
TIMEOUT_SECONDS: Final[float] = 10.0
ALLOWED_SCHEMES: Final[frozenset[str]] = frozenset({"http", "https"})
ALLOWED_PORTS: Final[frozenset[int]] = frozenset({80, 443})

#: Sent so sites that block unknown clients still answer, and so anyone reading
#: their logs can tell what this is. Deliberately not a browser impersonation.
USER_AGENT: Final[str] = "HomeOps/0.1 (self-hosted recipe importer)"


class UnsafeUrl(ValueError):
    """The URL is one we refuse to fetch. The message is shown to the user."""


class FetchFailed(ValueError):
    """The URL was allowed but could not be fetched."""


@dataclass(frozen=True)
class Fetched:
    url: str
    content_type: str
    body: bytes


def _is_public(address: str) -> bool:
    """Is this an address on the public internet?

    Written as an allowlist of "not any of the special ranges" rather than a
    denylist of a few, because the list of special ranges is long and Python
    already knows it. `is_global` is exactly this question.
    """
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        # ::ffff:127.0.0.1 is loopback wearing a hat.
        ip = ip.ipv4_mapped
    return bool(ip.is_global) and not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def _resolve(host: str, port: int) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError) as exc:
        raise UnsafeUrl(f"Could not look up {host}.") from exc
    # getaddrinfo's sockaddr is (host, port) for IPv4 and a 4-tuple for IPv6;
    # the first element is the address string in both.
    return list(dict.fromkeys(str(info[4][0]) for info in infos))


def validate(url: str) -> tuple[str, str, int, str]:
    """Check a URL and return `(scheme, host, port, address)` to connect to.

    Every address the name resolves to has to be public — not merely the first.
    A name answering with one public and one private address is a rebinding
    attack mid-flight, and picking the public one would be walking into it.
    """
    try:
        parsed = urlparse(url.strip())
    except ValueError as exc:
        raise UnsafeUrl("That does not look like a web address.") from exc

    scheme = (parsed.scheme or "").lower()
    if scheme not in ALLOWED_SCHEMES:
        raise UnsafeUrl("Only http:// and https:// addresses can be imported.")

    host = (parsed.hostname or "").strip()
    if not host:
        raise UnsafeUrl("That address has no host.")

    try:
        port = parsed.port or (443 if scheme == "https" else 80)
    except ValueError as exc:
        raise UnsafeUrl("That address has an invalid port.") from exc
    if port not in ALLOWED_PORTS:
        raise UnsafeUrl("Only the standard web ports can be imported from.")

    if parsed.username or parsed.password:
        # Credentials in a URL are either a phishing shape or an attempt to
        # authenticate to something on the local network.
        raise UnsafeUrl("Addresses with a username or password are not imported.")

    # A literal IP skips DNS entirely and is checked directly.
    try:
        ipaddress.ip_address(host)
    except ValueError:
        addresses = _resolve(host, port)
    else:
        addresses = [host]

    if not addresses:
        raise UnsafeUrl(f"Could not look up {host}.")

    for address in addresses:
        if not _is_public(address):
            raise UnsafeUrl(
                "That address is on a private network. Recipes can only be "
                "imported from the public internet."
            )

    return scheme, host, port, addresses[0]


def _pinned_url(scheme: str, address: str, port: int, parsed_path: str, query: str) -> str:
    """The same request, addressed to the vetted IP instead of the name."""
    literal = f"[{address}]" if ":" in address else address
    netloc = literal if port in (80, 443) else f"{literal}:{port}"
    return urlunparse((scheme, netloc, parsed_path or "/", "", query, ""))


def fetch(url: str, *, max_bytes: int = MAX_BYTES) -> Fetched:
    """Fetch a URL, following redirects by hand and vetting each hop.

    Synchronous on purpose: it runs in FastAPI's threadpool like the database
    calls around it, and an async client here would buy nothing but a second
    concurrency model to reason about.
    """
    current = url
    with httpx.Client(
        follow_redirects=False,
        timeout=TIMEOUT_SECONDS,
        headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
        # No cookie jar: nothing about this household travels to a recipe site.
        cookies=None,
        trust_env=False,
    ) as client:
        for _ in range(MAX_REDIRECTS + 1):
            scheme, host, port, address = validate(current)
            parsed = urlparse(current)
            target = _pinned_url(scheme, address, port, parsed.path, parsed.query)

            try:
                with client.stream(
                    "GET",
                    target,
                    headers={"Host": host},
                    # SNI and certificate verification follow the *name*, while
                    # the socket goes to the address we vetted. This is what
                    # closes the rebinding window rather than narrowing it.
                    extensions={"sni_hostname": host},
                ) as response:
                    if response.is_redirect:
                        location = response.headers.get("location", "")
                        if not location:
                            raise FetchFailed("That site redirected to nowhere.")
                        current = str(httpx.URL(current).join(location))
                        continue

                    if response.status_code >= 400:
                        raise FetchFailed(f"That site answered {response.status_code}.")

                    body = bytearray()
                    for chunk in response.iter_bytes():
                        body.extend(chunk)
                        # Checked while streaming: a response with no end must
                        # not be able to exhaust memory before it is rejected.
                        if len(body) > max_bytes:
                            raise FetchFailed("That page is too large to import.")

                    return Fetched(
                        url=current,
                        content_type=response.headers.get("content-type", ""),
                        body=bytes(body),
                    )
            except httpx.HTTPError as exc:
                raise FetchFailed("Could not reach that page.") from exc

    raise FetchFailed("That address redirected too many times.")


def decode(fetched: Fetched) -> str:
    """Bytes to text, honouring the charset the site declared.

    `errors="replace"` because a page with one bad byte is still a page with a
    recipe in it, and refusing the whole import over an encoding wobble would be
    the wrong trade.
    """
    charset = "utf-8"
    for part in fetched.content_type.split(";"):
        key, _, value = part.partition("=")
        if key.strip().lower() == "charset" and value.strip():
            charset = value.strip().strip("\"'")
            break
    try:
        return fetched.body.decode(charset, errors="replace")
    except LookupError:
        return fetched.body.decode("utf-8", errors="replace")
