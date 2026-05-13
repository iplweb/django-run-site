"""Tests for the LAN host discovery used by the ``--bind 0.0.0.0`` banner."""

from __future__ import annotations

import socket
from typing import Any

import pytest

from run_site import host_discovery


class _FakeUDPSocket:
    """Minimal stand-in for socket.socket(AF_INET, SOCK_DGRAM) — only
    implements the methods discover_lan_hosts touches."""

    def __init__(self, sockname: tuple[str, int] | None) -> None:
        self._sockname = sockname

    def __enter__(self) -> _FakeUDPSocket:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def connect(self, _addr: tuple[str, int]) -> None:
        if self._sockname is None:
            raise OSError("no route")

    def getsockname(self) -> tuple[str, int]:
        assert self._sockname is not None
        return self._sockname


def _patch_socket(
    monkeypatch: pytest.MonkeyPatch,
    *,
    hostname: str | OSError,
    udp_sockname: tuple[str, int] | None,
) -> None:
    if isinstance(hostname, OSError):

        def _gethostname() -> str:
            raise hostname

        monkeypatch.setattr(host_discovery.socket, "gethostname", _gethostname)
    else:
        monkeypatch.setattr(host_discovery.socket, "gethostname", lambda: hostname)

    def _socket(family: int, kind: int) -> Any:
        assert family == socket.AF_INET and kind == socket.SOCK_DGRAM
        return _FakeUDPSocket(udp_sockname)

    monkeypatch.setattr(host_discovery.socket, "socket", _socket)


def test_returns_hostname_and_outbound_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_socket(monkeypatch, hostname="mac-mini-micha.local", udp_sockname=("192.168.1.42", 0))
    assert host_discovery.discover_lan_hosts() == ("mac-mini-micha.local", "192.168.1.42")


def test_skips_loopback_outbound_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the machine has no real network, the UDP-connect trick can
    yield 127.0.0.1 — that's already covered by ``localhost`` and would
    just duplicate the primary URL."""

    _patch_socket(monkeypatch, hostname="myhost.local", udp_sockname=("127.0.0.1", 0))
    assert host_discovery.discover_lan_hosts() == ("myhost.local",)


def test_skips_localhost_hostname(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_socket(monkeypatch, hostname="localhost", udp_sockname=("10.0.0.5", 0))
    assert host_discovery.discover_lan_hosts() == ("10.0.0.5",)


def test_handles_socket_failure_silently(monkeypatch: pytest.MonkeyPatch) -> None:
    """Banner is a display nicety — a failing UDP probe must not abort
    startup or surface a stack trace."""

    _patch_socket(monkeypatch, hostname="dev-box", udp_sockname=None)
    assert host_discovery.discover_lan_hosts() == ("dev-box",)


def test_returns_empty_when_everything_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_socket(monkeypatch, hostname=OSError("name lookup broken"), udp_sockname=None)
    assert host_discovery.discover_lan_hosts() == ()


def test_dedupes_when_hostname_resolves_to_outbound_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_socket(monkeypatch, hostname="192.168.1.42", udp_sockname=("192.168.1.42", 0))
    assert host_discovery.discover_lan_hosts() == ("192.168.1.42",)
