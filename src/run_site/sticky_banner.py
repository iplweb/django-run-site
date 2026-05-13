"""Pin the banner to the top of the terminal while logs scroll below.

We use the venerable DECSTBM (Set Top and Bottom Margins) escape sequence —
``ESC [ <top> ; <bottom> r`` — which every modern terminal honors (xterm,
iTerm, kitty, alacritty, gnome-terminal, Windows Terminal, VS Code,
tmux/screen). It tells the terminal to *only scroll* the rows between
``top`` and ``bottom`` inclusive. Any output written while the cursor is
inside that region scrolls within it; rows above stay frozen. This is the
same mechanism ``less``, ``vim``, and ``htop`` use.

Flow:

* compute banner height H
* set scroll region to rows ``(H+1, term_height)``
* draw the banner once into rows ``1..H``
* park the cursor on the last row of the region — subsequent ``stdout``
  writes from the log multiplexer scroll naturally inside the region
* on SIGWINCH (terminal resize): re-measure, re-draw, re-set the region
* on exit / signal / exception: reset the region (``ESC [ r``), re-print
  the banner inline so it lands in scrollback, and show the cursor

The whole thing is best-effort: if stdout isn't a TTY, the terminal is too
short for the banner, or anything goes wrong while installing the region,
we fall back to plain inline printing — no surprises in CI or under pipes.
"""

from __future__ import annotations

import os
import shutil
import signal
import sys
import threading
from contextlib import AbstractContextManager, suppress
from types import FrameType, TracebackType
from typing import IO, TextIO

# ANSI building blocks. Kept as module constants so tests can grep for them.
_CSI = "\x1b["
_SAVE_CURSOR = f"{_CSI}s"
_RESTORE_CURSOR = f"{_CSI}u"
_HIDE_CURSOR = f"{_CSI}?25l"
_SHOW_CURSOR = f"{_CSI}?25h"
_CLEAR_SCREEN = f"{_CSI}2J"
_RESET_REGION = f"{_CSI}r"


def _set_region(top: int, bottom: int) -> str:
    return f"{_CSI}{top};{bottom}r"


def _move_cursor(row: int, col: int = 1) -> str:
    return f"{_CSI}{row};{col}H"


def _clear_line() -> str:
    return f"{_CSI}2K"


def _strip_trailing_newline(text: str) -> str:
    """The banner is rendered with a trailing ``\\n`` for inline printing;
    inside a scroll region we draw each line by hand so we strip it."""
    return text[:-1] if text.endswith("\n") else text


def _terminal_size(stream: IO[str]) -> tuple[int, int]:
    """Return (columns, rows). ``shutil.get_terminal_size`` falls back to
    ``$COLUMNS/$LINES`` or 80x24 when the stream isn't a TTY."""

    fd: int | None
    try:
        fd = stream.fileno()
    except (AttributeError, OSError, ValueError):
        fd = None
    if fd is not None:
        try:
            size = os.get_terminal_size(fd)
            return size.columns, size.lines
        except OSError:
            pass
    fallback = shutil.get_terminal_size((80, 24))
    return fallback.columns, fallback.lines


def is_sticky_supported(stream: TextIO | None = None) -> bool:
    """Best-effort detection of whether sticky mode will work.

    A real TTY plus a non-``dumb`` ``$TERM`` is enough — all the terminals
    we care about implement DECSTBM. Returning False here triggers the
    inline-print fallback in the caller.
    """

    out = stream if stream is not None else sys.stdout
    isatty = getattr(out, "isatty", None)
    if not (isatty and isatty()):
        return False
    return os.environ.get("TERM", "") != "dumb"


class StickyRegion(AbstractContextManager["StickyRegion"]):
    """Pin ``banner_text`` to the top of the terminal for the duration of
    the ``with`` block. Logs written to ``stream`` during the block scroll
    inside the region below the banner.

    The class is a no-op (degenerates to plain inline printing) when
    ``is_sticky_supported`` returns False or the banner is taller than the
    terminal — see ``installed`` to check which path was taken.
    """

    # Below this many free rows for logs we refuse to install the region —
    # a 1-row scroll area is unusable and confusing. Inline print instead.
    MIN_LOG_ROWS = 3

    def __init__(
        self,
        banner_text: str,
        *,
        stream: TextIO | None = None,
        enabled: bool = True,
    ) -> None:
        self._banner = _strip_trailing_newline(banner_text)
        self._banner_lines = self._banner.split("\n")
        self._stream: TextIO = stream if stream is not None else sys.stdout
        self._requested = enabled
        self._installed = False
        self._lock = threading.RLock()
        # SIGWINCH handler can only be installed from the main thread; we
        # capture the prior handler so we can restore it on exit.
        self._prev_winch: signal._HANDLER | None = None  # type: ignore[name-defined]

    # ------------------------------------------------------------------
    # Public surface
    # ------------------------------------------------------------------

    @property
    def installed(self) -> bool:
        """True iff the scroll region is currently active."""
        return self._installed

    def __enter__(self) -> StickyRegion:
        if not self._requested or not is_sticky_supported(self._stream):
            self._inline_fallback()
            return self
        cols, rows = _terminal_size(self._stream)
        if not self._fits(rows):
            self._inline_fallback()
            return self
        try:
            self._install(rows=rows, cols=cols)
        except OSError:
            # Writes can fail on a closed stream during early teardown —
            # leave the user with the inline banner rather than a half-set
            # scroll region.
            self._installed = False
            self._inline_fallback()
            return self
        self._install_winch_handler()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._uninstall_winch_handler()
        if not self._installed:
            return
        with self._lock:
            try:
                self._teardown()
            except OSError:
                # Best-effort cleanup; if the terminal is already gone
                # there's nothing useful we can do.
                pass
            finally:
                self._installed = False
        # Re-print the banner inline so the user keeps a copy in scrollback
        # after the sticky region is torn down.
        self._stream.write(self._banner)
        self._stream.write("\n")
        with suppress(OSError):
            self._stream.flush()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _fits(self, rows: int) -> bool:
        return rows - len(self._banner_lines) >= self.MIN_LOG_ROWS

    def _inline_fallback(self) -> None:
        self._stream.write(self._banner)
        self._stream.write("\n")
        with suppress(OSError):
            self._stream.flush()

    def _install(self, *, rows: int, cols: int) -> None:
        del cols  # reserved for future use (e.g. truncating banner lines)
        with self._lock:
            banner_height = len(self._banner_lines)
            self._stream.write(_HIDE_CURSOR)
            # Park the cursor on the last row so the very first write below
            # the banner doesn't pile on top of the banner area.
            self._stream.write(_CLEAR_SCREEN)
            self._stream.write(_move_cursor(1, 1))
            for line in self._banner_lines:
                self._stream.write(line)
                self._stream.write("\n")
            self._stream.write(_set_region(banner_height + 1, rows))
            self._stream.write(_move_cursor(rows, 1))
            self._stream.write(_SHOW_CURSOR)
            self._stream.flush()
            self._installed = True

    def _teardown(self) -> None:
        # Reset margins to full screen, then move the cursor below the
        # banner area so the next prompt doesn't overwrite anything.
        cols, rows = _terminal_size(self._stream)
        del cols
        self._stream.write(_HIDE_CURSOR)
        self._stream.write(_RESET_REGION)
        self._stream.write(_move_cursor(rows, 1))
        self._stream.write(_SHOW_CURSOR)
        with suppress(OSError):
            self._stream.flush()

    # -- SIGWINCH handling ---------------------------------------------

    def _install_winch_handler(self) -> None:
        if not hasattr(signal, "SIGWINCH"):
            return  # Windows — no resize signal, no-op
        if threading.current_thread() is not threading.main_thread():
            return  # signal handlers must be installed from the main thread
        try:
            self._prev_winch = signal.signal(signal.SIGWINCH, self._on_resize)
        except (ValueError, OSError):
            self._prev_winch = None

    def _uninstall_winch_handler(self) -> None:
        if self._prev_winch is None or not hasattr(signal, "SIGWINCH"):
            return
        with suppress(ValueError, OSError):
            signal.signal(signal.SIGWINCH, self._prev_winch)
        self._prev_winch = None

    def _on_resize(self, signum: int, frame: FrameType | None) -> None:
        del signum, frame
        if not self._installed:
            return
        with self._lock:
            cols, rows = _terminal_size(self._stream)
            if not self._fits(rows):
                # Window got too short — tear down rather than render a
                # broken layout. The banner survives in scrollback.
                with suppress(OSError):
                    self._teardown()
                self._installed = False
                return
            try:
                self._install(rows=rows, cols=cols)
            except OSError:
                self._installed = False


def update_banner(region: StickyRegion | None, banner_text: str) -> None:
    """Replace the pinned banner text and redraw, if a region is active.

    Currently unused by the run flow — kept for future "banner status
    updates while server runs" (e.g. flipping `(starting)` → `(ready)`).
    """

    if region is None or not region.installed:
        return
    with region._lock:
        region._banner = _strip_trailing_newline(banner_text)
        region._banner_lines = region._banner.split("\n")
        cols, rows = _terminal_size(region._stream)
        if region._fits(rows):
            with suppress(OSError):
                region._install(rows=rows, cols=cols)
