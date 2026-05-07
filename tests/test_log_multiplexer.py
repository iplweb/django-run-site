"""Log multiplexer prefix / threading tests (§18.1)."""

from __future__ import annotations

import io
import time

import pytest

from django_run_site.log_multiplexer import LogMultiplexer, captured_multiplexer


def test_write_uses_prefix() -> None:
    with captured_multiplexer() as (mux, buf):
        mux.write("web", "cyan", "hello")
    out = buf.getvalue()
    assert "web" in out
    assert "hello" in out


def test_write_handles_multiline() -> None:
    with captured_multiplexer() as (mux, buf):
        mux.write("celery", "green", "line one\nline two")
    lines = buf.getvalue().splitlines()
    assert len(lines) == 2
    for line in lines:
        assert line.startswith("celery")


def test_attach_pumps_until_eof() -> None:
    src = io.BytesIO(b"alpha\nbeta\ngamma\n")
    with captured_multiplexer() as (mux, buf):
        spec = mux.stream("worker", "green")
        mux.attach(spec, src)
        # Wait for the thread to drain.
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and "gamma" not in buf.getvalue():
            time.sleep(0.02)
    out = buf.getvalue()
    assert "alpha" in out
    assert "gamma" in out


def test_unknown_color_rejected() -> None:
    mux = LogMultiplexer(color=False)
    with pytest.raises(ValueError, match="Unknown color"):
        mux.stream("foo", "puce")


def test_concurrent_streams_dont_interleave_lines() -> None:
    """Each emitted line should appear with exactly one prefix."""

    a = io.BytesIO(b"a1\na2\na3\n")
    b = io.BytesIO(b"b1\nb2\nb3\n")
    with captured_multiplexer() as (mux, buf):
        spec_a = mux.stream("aaa", "cyan")
        spec_b = mux.stream("bbb", "green")
        mux.attach(spec_a, a)
        mux.attach(spec_b, b)
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and (
            "a3" not in buf.getvalue() or "b3" not in buf.getvalue()
        ):
            time.sleep(0.02)
    for line in buf.getvalue().splitlines():
        # Exactly one of "aaa | " or "bbb | " (after color is off they're
        # plain). Split on " | " and check the prefix is a known stream name.
        prefix, _, _ = line.partition(" | ")
        assert prefix.strip() in {"aaa", "bbb"}
