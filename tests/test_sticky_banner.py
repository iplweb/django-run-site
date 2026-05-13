"""Tests for the DECSTBM-based sticky banner."""

from __future__ import annotations

import io
from unittest import mock

import pytest

from run_site import sticky_banner
from run_site.sticky_banner import (
    StickyRegion,
    is_sticky_supported,
    update_banner,
)


class _FakeTTY(io.StringIO):
    """StringIO that claims to be a TTY — used to test the TTY-detection
    gate without needing an actual terminal."""

    def __init__(self, *, is_tty: bool = True) -> None:
        super().__init__()
        self._is_tty = is_tty

    def isatty(self) -> bool:  # type: ignore[override]
        return self._is_tty


# ---------------------------------------------------------------------------
# is_sticky_supported
# ---------------------------------------------------------------------------


def test_sticky_supported_when_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TERM", raising=False)
    assert is_sticky_supported(_FakeTTY(is_tty=True))


def test_not_supported_when_not_tty() -> None:
    assert not is_sticky_supported(io.StringIO())


def test_not_supported_when_dumb_term(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TERM", "dumb")
    assert not is_sticky_supported(_FakeTTY(is_tty=True))


# ---------------------------------------------------------------------------
# Inline fallback
# ---------------------------------------------------------------------------


def test_disabled_falls_back_to_inline() -> None:
    buf = io.StringIO()
    banner = "line one\nline two\n"
    with StickyRegion(banner, stream=buf, enabled=False) as region:
        assert not region.installed
    # Inline fallback prints the banner once at entry and (since the region
    # never installed) does not double-print on exit.
    assert buf.getvalue() == banner


def test_non_tty_falls_back_to_inline() -> None:
    buf = io.StringIO()  # not a TTY
    banner = "hello banner\n"
    with StickyRegion(banner, stream=buf, enabled=True) as region:
        assert not region.installed
    assert buf.getvalue() == banner


def test_banner_too_tall_falls_back_to_inline() -> None:
    # Force a tiny terminal so the banner can't fit.
    buf = _FakeTTY()
    banner = "\n".join(f"line {i}" for i in range(50)) + "\n"
    with (
        mock.patch.object(sticky_banner, "_terminal_size", return_value=(80, 10)),
        StickyRegion(banner, stream=buf, enabled=True) as region,
    ):
        assert not region.installed
    # Banner still went to stdout so the user sees it.
    assert "line 0" in buf.getvalue()
    assert "line 49" in buf.getvalue()


# ---------------------------------------------------------------------------
# Region installed — ANSI sequence layout
# ---------------------------------------------------------------------------


def test_installed_emits_decstbm_and_banner(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TERM", raising=False)
    buf = _FakeTTY()
    banner = "row1\nrow2\nrow3\n"
    # Don't install a SIGWINCH handler from inside pytest's main thread —
    # patch it out so the test doesn't mutate process-wide signal state.
    with (
        mock.patch.object(sticky_banner, "_terminal_size", return_value=(80, 24)),
        mock.patch.object(StickyRegion, "_install_winch_handler"),
        mock.patch.object(StickyRegion, "_uninstall_winch_handler"),
        StickyRegion(banner, stream=buf, enabled=True) as region,
    ):
        assert region.installed
        setup = buf.getvalue()
        # Banner contents are written.
        assert "row1" in setup
        assert "row3" in setup
        # Scroll region starts at row 4 (3 banner rows + 1)
        # and ends at the bottom row (24).
        assert "\x1b[4;24r" in setup
        # Cursor parked at the last row after install.
        assert "\x1b[24;1H" in setup
    full = buf.getvalue()
    # Region reset on teardown, plus a re-print of the banner for scrollback.
    assert "\x1b[r" in full
    assert full.count("row1") >= 2  # once in region, once for scrollback


def test_min_log_rows_threshold() -> None:
    """A 3-row banner in a 5-row terminal leaves only 2 rows for logs —
    below MIN_LOG_ROWS, so we must fall back rather than install a
    cramped region."""
    buf = _FakeTTY()
    banner = "a\nb\nc\n"
    with (
        mock.patch.object(sticky_banner, "_terminal_size", return_value=(80, 5)),
        StickyRegion(banner, stream=buf, enabled=True) as region,
    ):
        assert not region.installed


# ---------------------------------------------------------------------------
# update_banner
# ---------------------------------------------------------------------------


def test_update_banner_no_op_when_not_installed() -> None:
    update_banner(None, "anything")  # must not raise


def test_update_banner_redraws_in_active_region() -> None:
    buf = _FakeTTY()
    banner = "old1\nold2\n"
    with (
        mock.patch.object(sticky_banner, "_terminal_size", return_value=(80, 24)),
        mock.patch.object(StickyRegion, "_install_winch_handler"),
        mock.patch.object(StickyRegion, "_uninstall_winch_handler"),
        StickyRegion(banner, stream=buf, enabled=True) as region,
    ):
        update_banner(region, "new1\nnew2\nnew3\n")
        assert "new1" in buf.getvalue()
        assert "new3" in buf.getvalue()
