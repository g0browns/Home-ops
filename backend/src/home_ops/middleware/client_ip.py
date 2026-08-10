"""Work out the real client address across all three access paths (SPEC §2.1).

The app is reachable three ways at once, and each puts a different thing in
front of it:

    browser ──HTTPS──> Cloudflare ──> cloudflared ──> web (nginx) ──> api
    browser ──HTTP───────── tailnet ────────────────> web (nginx) ──> api
    browser ──HTTP───────── LAN ────────────────────> web (nginx) ──> api

So `request.client.host` is nearly always the reverse proxy, and the true
client address is in a header. Headers are trivially forged, hence the rule in
SPEC §2.1: trust them *only* on the tunnel path. Two settings express that:

`TRUSTED_PROXY_IPS`
    Peers whose forwarding headers we read at all — the `web` container. A
    request straight from a LAN host is not in this range, so a LAN client
    cannot spoof its own address by sending its own headers.

`TUNNEL_PROXY_IPS`
    cloudflared's address. `CF-Connecting-IP` is honoured only when the hop into
    the reverse proxy came from here. Empty (the default) means never.

This relies on nginx setting `X-Real-IP: $remote_addr` **unconditionally**, so a
client-supplied value is always overwritten before we see it. See
`frontend/nginx.conf` — the two files have to agree.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Mapping, Sequence

from pydantic import IPvAnyNetwork

IpAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

UNKNOWN = "unknown"

HEADER_REAL_IP = "x-real-ip"
HEADER_FORWARDED_FOR = "x-forwarded-for"
HEADER_CF_CONNECTING_IP = "cf-connecting-ip"


def parse_ip(value: str | None) -> IpAddress | None:
    """Parse an address from a header or peer string, or return None.

    Tolerates the shapes proxies actually emit — `[::1]:443`, `10.0.0.1:5432` —
    and rejects anything else rather than passing it on. These values reach
    logs and rate-limit keys, so nothing unvalidated may escape this function.
    """
    if not value:
        return None

    candidate = value.strip()
    if not candidate:
        return None

    # Bracketed IPv6, optionally with a port: [::1] / [::1]:443
    if candidate.startswith("["):
        candidate = candidate[1:].partition("]")[0]
    # IPv4 with a port. A bare IPv6 has several colons, so one colon and a
    # numeric tail is unambiguous.
    elif candidate.count(":") == 1:
        host, _, port = candidate.partition(":")
        if port.isdigit():
            candidate = host

    try:
        return ipaddress.ip_address(candidate)
    except ValueError:
        return None


def is_in_networks(ip: IpAddress, networks: Sequence[IPvAnyNetwork]) -> bool:
    """Membership test that tolerates a mixed v4/v6 network list."""
    return any(ip.version == network.version and ip in network for network in networks)


def _forwarded_hop(headers: Mapping[str, str]) -> IpAddress | None:
    """The address the reverse proxy saw as its own TCP peer.

    `X-Real-IP` is authoritative because nginx overwrites it. Leftmost
    `X-Forwarded-For` is a weaker fallback for a proxy that does not set
    `X-Real-IP`: that value is appended to, so its first entry originates with
    the client. It is only read once the peer itself is already trusted.
    """
    hop = parse_ip(headers.get(HEADER_REAL_IP))
    if hop is not None:
        return hop

    forwarded_for = headers.get(HEADER_FORWARDED_FOR)
    if forwarded_for:
        return parse_ip(forwarded_for.split(",")[0])

    return None


def resolve_client_ip(
    peer: str | None,
    headers: Mapping[str, str],
    *,
    trusted_proxies: Sequence[IPvAnyNetwork],
    tunnel_proxies: Sequence[IPvAnyNetwork],
) -> str:
    """Return the client address to use for logging and rate limiting.

    Pure function of its arguments so every branch is directly testable — see
    `tests/test_client_ip.py`.
    """
    peer_ip = parse_ip(peer)
    if peer_ip is None:
        return UNKNOWN

    # A direct hit: tailnet or LAN client talking to the API with nothing in
    # between, or a dev container. Its headers mean nothing to us.
    if not is_in_networks(peer_ip, trusted_proxies):
        return str(peer_ip)

    hop_ip = _forwarded_hop(headers)
    if hop_ip is None:
        # Trusted peer that forwarded nothing usable. Naming the proxy is
        # honest; inventing a client address would not be.
        return str(peer_ip)

    # Tunnel path only: Cloudflare strips any client-supplied CF-Connecting-IP
    # and sets its own, so it is trustworthy exactly when the hop is cloudflared.
    if is_in_networks(hop_ip, tunnel_proxies):
        cf_ip = parse_ip(headers.get(HEADER_CF_CONNECTING_IP))
        if cf_ip is not None:
            return str(cf_ip)

    return str(hop_ip)
