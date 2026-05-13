"""Detect whether the current session is likely headless (no usable display).

Used to decide whether to auto-open a browser tab after the dev server is
up. Opening a browser from an SSH session pops a window on the *remote*
display (or whatever VNC session the user later logs into) — almost never
what was intended.
"""

from __future__ import annotations

import os
import platform
from dataclasses import dataclass


@dataclass(frozen=True)
class HeadlessSignal:
    """Result of :func:`detect_headless_session`.

    ``reason`` is a short human-readable phrase suitable for the banner —
    e.g. ``"SSH session ($SSH_CONNECTION set)"`` or
    ``"local macOS session"``. Showing the reason makes the auto-decision
    transparent: users can see why the browser was (or wasn't) opened.
    """

    headless: bool
    reason: str


def detect_headless_session(env: dict[str, str] | None = None) -> HeadlessSignal:
    """Return whether this session looks headless, plus a one-line reason.

    The ``env`` parameter exists for testability — production callers omit
    it and we read ``os.environ`` directly.

    Heuristics by platform:

    * **macOS (Darwin)** — assume a graphical session unless we're inside
      SSH. Local Terminal/iTerm sessions always have a windowserver to
      talk to; the only common "wrong window" case is ``ssh user@mac-box``
      where ``SSH_CONNECTION`` is set on the remote side. VNC sessions
      don't set ``SSH_CONNECTION``, so a user at the VNC console still
      gets a browser.
    * **Linux / *BSD** — graphical sessions set ``DISPLAY`` (X11) or
      ``WAYLAND_DISPLAY`` (Wayland). Absence of both means we're on a
      tty / SSH / container — open-browser would either error out or pop
      a text-mode browser into the terminal, both worse than skipping.
    * **Other / unknown** — fall back to the Linux rule, which is the
      least surprising default.
    """

    e = os.environ if env is None else env
    system = platform.system()

    if system == "Darwin":
        ssh = _ssh_indicator(e)
        if ssh:
            return HeadlessSignal(headless=True, reason=ssh)
        return HeadlessSignal(headless=False, reason="local macOS session")

    if e.get("DISPLAY"):
        return HeadlessSignal(headless=False, reason="$DISPLAY set (X11)")
    if e.get("WAYLAND_DISPLAY"):
        return HeadlessSignal(headless=False, reason="$WAYLAND_DISPLAY set (Wayland)")
    ssh = _ssh_indicator(e)
    if ssh:
        return HeadlessSignal(headless=True, reason=ssh)
    return HeadlessSignal(headless=True, reason="no display server detected")


def _ssh_indicator(env: dict[str, str] | os._Environ[str]) -> str:
    """Return a non-empty reason string when any standard SSH env var is set."""

    for var in ("SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY"):
        if env.get(var):
            return f"SSH session (${var} set)"
    return ""
