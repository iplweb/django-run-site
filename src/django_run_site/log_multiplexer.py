"""Multiplex local subprocess output with colored prefixes (§18.1)."""

from __future__ import annotations

import io
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

# ANSI escape sequences for terminal colors. Plain ANSI keeps the runtime
# dependency-free. Disabled when stdout isn't a TTY or NO_COLOR is set.
ANSI_RESET = "\x1b[0m"
COLOR_CODES: dict[str, str] = {
    "cyan": "\x1b[36m",
    "green": "\x1b[32m",
    "yellow": "\x1b[33m",
    "magenta": "\x1b[35m",
    "blue": "\x1b[34m",
    "red": "\x1b[31m",
    "white": "\x1b[37m",
    "gray": "\x1b[90m",
    "bold": "\x1b[1m",
}


def _color_supported(stream: object) -> bool:
    """Return True iff we should emit ANSI colors to *stream*."""

    import os

    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    isatty = getattr(stream, "isatty", None)
    return bool(isatty and isatty())


@dataclass(frozen=True)
class StreamSpec:
    """One stream (process name + color) muxed into the multiplexer."""

    name: str
    color: str


class LogMultiplexer:
    """Thread-safe stdout/stderr multiplexer for multiple long-running
    subprocesses.

    Usage::

        mux = LogMultiplexer()
        web = mux.stream("web", "cyan")
        mux.attach(web, process.stdout)

    Output is written to ``sys.stdout`` (or a custom stream passed in for
    tests) under a single lock, so prefixes and lines never interleave.
    """

    DEFAULT_NAME_WIDTH = 12

    def __init__(
        self,
        *,
        out: io.TextIOBase | None = None,
        name_width: int | None = None,
        color: bool | None = None,
    ) -> None:
        self._out = out if out is not None else sys.stdout
        self._lock = threading.Lock()
        self._streams: dict[str, StreamSpec] = {}
        self._threads: list[threading.Thread] = []
        self._name_width = name_width
        self._color = _color_supported(self._out) if color is None else color

    # ------------------------------------------------------------------
    # Stream registration
    # ------------------------------------------------------------------

    def stream(self, name: str, color: str) -> StreamSpec:
        if color not in COLOR_CODES:
            raise ValueError(f"Unknown color {color!r}. Use one of {sorted(COLOR_CODES)}.")
        spec = StreamSpec(name=name, color=color)
        self._streams[name] = spec
        return spec

    def attach(self, spec: StreamSpec, source: object) -> None:
        """Spawn a thread that pumps lines from *source* into the muxed
        output until EOF.

        *source* is anything Popen returns for stdout — a binary or text
        IO stream — or any file-like object yielding bytes/str when iterated.
        """

        thread = threading.Thread(
            target=self._pump,
            args=(spec, source),
            name=f"mux-{spec.name}",
            daemon=True,
        )
        thread.start()
        self._threads.append(thread)

    def join(self, timeout: float | None = None) -> None:
        for t in self._threads:
            t.join(timeout=timeout)

    # ------------------------------------------------------------------
    # Direct emission (e.g. orchestrator banner / mux-internal messages)
    # ------------------------------------------------------------------

    def write(self, name: str, color: str, message: str) -> None:
        spec = self._streams.get(name) or StreamSpec(name=name, color=color)
        for line in message.splitlines() or [""]:
            self._emit(spec, line)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _pump(self, spec: StreamSpec, source: object) -> None:
        wrapped: io.TextIOBase
        if isinstance(source, io.TextIOBase):
            wrapped = source
        else:
            # ``source`` is a ``BinaryIO`` (Popen's stdout when text=False) or
            # similar; wrap it for line-based text iteration. Cast hides the
            # impedance mismatch between the duck-typed file-like and
            # ``BinaryIO``.
            from typing import BinaryIO, cast

            wrapped = io.TextIOWrapper(
                cast(BinaryIO, source),
                encoding="utf-8",
                errors="replace",
                write_through=True,
            )
        try:
            for raw_line in wrapped:
                self._emit(spec, raw_line.rstrip("\n"))
        except ValueError:
            # Source was closed mid-iteration — ignore, this is the normal
            # termination path for terminated subprocesses.
            pass

    def _emit(self, spec: StreamSpec, line: str) -> None:
        width = self._name_width or self.DEFAULT_NAME_WIDTH
        name = spec.name[:width].ljust(width)
        if self._color:
            color = COLOR_CODES[spec.color]
            prefix = f"{color}{name}{ANSI_RESET} | "
        else:
            prefix = f"{name} | "
        with self._lock:
            self._out.write(prefix)
            self._out.write(line)
            self._out.write("\n")
            self._out.flush()


@contextmanager
def captured_multiplexer() -> Iterator[tuple[LogMultiplexer, io.StringIO]]:
    """Test helper — yields a (multiplexer, captured-buffer) pair with
    color disabled and a fixed name width."""

    buf = io.StringIO()
    mux = LogMultiplexer(out=buf, color=False)
    try:
        yield mux, buf
    finally:
        mux.join(timeout=1.0)
