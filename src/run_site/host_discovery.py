"""Discover reachable LAN hostnames/IPs for the banner.

Used when the user binds to ``0.0.0.0`` and we want to tell them which
hosts on the LAN can reach the dev server, beyond the loopback default.
"""

from __future__ import annotations

import socket


def discover_lan_hosts() -> tuple[str, ...]:
    """Return extra hosts (beyond ``localhost``) that should reach the
    machine from the LAN. Best-effort — returns an empty tuple on any
    failure rather than raising, since this powers a display nicety, not
    a correctness path.

    Strategy: combine two cheap signals.

    * ``socket.gethostname()`` — on macOS this is typically ``<name>.local``
      thanks to mDNS, reachable from any Bonjour-aware device on the same
      LAN (Apple, modern Linux via Avahi, Windows 10+).
    * Primary outbound LAN IP — the address the kernel would source from
      for an internet-bound packet. We get it via the well-known
      UDP-connect trick: ``connect()`` on a datagram socket only sets the
      default destination, never sends, so 8.8.8.8 acts as a routing
      probe. Whatever IP the socket ends up bound to is by definition on
      an interface that routes to the internet — which rules out Docker
      bridges (``docker0`` / ``br-*``) that look like LAN IPs but go
      nowhere useful from another device. That's important here because
      run-site itself spins up testcontainers, so those bridges are
      almost always present and would otherwise pollute the list.
    """

    hosts: list[str] = []

    try:
        hostname = socket.gethostname()
    except OSError:
        hostname = ""
    if hostname and hostname not in ("localhost", "0.0.0.0"):
        hosts.append(hostname)

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            ip = sock.getsockname()[0]
        if not _is_loopback_or_sentinel(ip):
            hosts.append(ip)
    except OSError:
        pass

    seen: set[str] = set()
    deduped: list[str] = []
    for host in hosts:
        if host not in seen:
            seen.add(host)
            deduped.append(host)
    return tuple(deduped)


def _is_loopback_or_sentinel(ip: str) -> bool:
    return ip.startswith("127.") or ip in ("0.0.0.0", "::1", "::")
