"""Tests for the headless-session heuristic."""

from __future__ import annotations

import pytest

from run_site.display_detect import detect_headless_session


def _patch_system(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    monkeypatch.setattr("run_site.display_detect.platform.system", lambda: name)


def test_macos_local_session_is_not_headless(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_system(monkeypatch, "Darwin")
    result = detect_headless_session(env={})
    assert result.headless is False
    assert "local macOS" in result.reason


def test_macos_ssh_session_is_headless(monkeypatch: pytest.MonkeyPatch) -> None:
    """The motivating case — SSH'd into a Mac dev box and you do *not*
    want a browser popping in the remote VNC desktop."""

    _patch_system(monkeypatch, "Darwin")
    result = detect_headless_session(env={"SSH_CONNECTION": "192.168.1.5 49862 192.168.1.42 22"})
    assert result.headless is True
    assert "SSH" in result.reason
    assert "SSH_CONNECTION" in result.reason


def test_macos_ssh_client_alone_also_triggers(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_system(monkeypatch, "Darwin")
    result = detect_headless_session(env={"SSH_CLIENT": "192.168.1.5 49862 22"})
    assert result.headless is True
    assert "SSH_CLIENT" in result.reason


def test_linux_with_x11_display(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_system(monkeypatch, "Linux")
    result = detect_headless_session(env={"DISPLAY": ":0"})
    assert result.headless is False
    assert "X11" in result.reason


def test_linux_with_wayland(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_system(monkeypatch, "Linux")
    result = detect_headless_session(env={"WAYLAND_DISPLAY": "wayland-0"})
    assert result.headless is False
    assert "Wayland" in result.reason


def test_linux_no_display_is_headless(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_system(monkeypatch, "Linux")
    result = detect_headless_session(env={})
    assert result.headless is True
    assert "no display server" in result.reason


def test_linux_ssh_without_display_reports_ssh(monkeypatch: pytest.MonkeyPatch) -> None:
    """When both signals point at headless, the SSH reason wins because
    it's more specific than 'no display server'."""

    _patch_system(monkeypatch, "Linux")
    result = detect_headless_session(env={"SSH_TTY": "/dev/pts/1"})
    assert result.headless is True
    assert "SSH" in result.reason


def test_linux_display_wins_over_ssh(monkeypatch: pytest.MonkeyPatch) -> None:
    """SSH with X11 forwarding sets both DISPLAY and SSH_* — we have a
    real display, so we should *not* skip the browser."""

    _patch_system(monkeypatch, "Linux")
    result = detect_headless_session(
        env={"DISPLAY": "localhost:10.0", "SSH_CONNECTION": "1.2.3.4 49862 5.6.7.8 22"}
    )
    assert result.headless is False
    assert "X11" in result.reason


def test_unknown_platform_uses_linux_rule(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_system(monkeypatch, "FreeBSD")
    assert detect_headless_session(env={"DISPLAY": ":0"}).headless is False
    assert detect_headless_session(env={}).headless is True
