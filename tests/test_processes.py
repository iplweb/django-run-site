"""Process / HTTP probe tests."""

from __future__ import annotations

import http.server
import socket
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from run_site.log_multiplexer import LogMultiplexer
from run_site.processes import (
    ProcessGroup,
    TemplateContext,
    find_free_port,
    run_oneshot,
    wait_for_http,
)


def test_find_free_port_returns_usable_port() -> None:
    port = find_free_port()
    assert 1024 <= port < 65536
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", port))
    finally:
        sock.close()


def test_run_oneshot_captures_stdout() -> None:
    result = run_oneshot(["python", "-c", "print('hi')"])
    assert result.ok
    assert "hi" in result.stdout


def test_run_oneshot_returns_nonzero_without_check() -> None:
    result = run_oneshot(["python", "-c", "import sys; sys.exit(7)"])
    assert result.returncode == 7
    assert not result.ok


def test_template_expansion_basic(tmp_path: Path) -> None:
    manage = tmp_path / "src" / "manage.py"
    manage.parent.mkdir(parents=True)
    manage.touch()
    tmpl = TemplateContext(
        python=("python",),
        manage_py=manage,
        manage_dir=manage.parent,
        project_root=tmp_path,
        port=8000,
    )
    expanded = tmpl.expand(["{python}", "{manage_py}", "qcluster", "--port", "{port}"])
    assert expanded == ("python", str(manage), "qcluster", "--port", "8000")


def test_template_expansion_python_inside_token(tmp_path: Path) -> None:
    manage = tmp_path / "manage.py"
    manage.touch()
    tmpl = TemplateContext(
        python=("uv", "run", "python"),
        manage_py=manage,
        manage_dir=manage.parent,
        project_root=tmp_path,
        port=8000,
    )
    # When {python} is the entire token, expanded inline as multiple tokens.
    expanded = tmpl.expand(["{python}", "-m", "celery"])
    assert expanded == ("uv", "run", "python", "-m", "celery")


@pytest.mark.slow
def test_wait_for_http_succeeds_against_local_server() -> None:
    """Stand up a tiny HTTP server in a thread and probe it."""

    server = http.server.HTTPServer(("127.0.0.1", 0), _OkHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        ok = wait_for_http(f"http://127.0.0.1:{port}/", timeout=3.0, interval=0.1)
        assert ok
    finally:
        server.shutdown()
        server.server_close()


def test_wait_for_http_times_out_quickly() -> None:
    # Use a port that's almost certainly closed.
    port = 1
    ok = wait_for_http(
        f"http://127.0.0.1:{port}/",
        timeout=0.5,
        interval=0.1,
    )
    assert ok is False


# --- ProcessGroup shutdown / Ctrl+C handling --------------------------------

# A child that sleeps a long time and ignores SIGTERM, mimicking a worker
# doing a slow "warm shutdown" or a server draining open connections. Only
# SIGKILL stops it — which is exactly the escalation path we exercise.
_STUBBORN = (
    "import signal,time;"
    "signal.signal(signal.SIGTERM, signal.SIG_IGN);"
    "print('up', flush=True);"
    "time.sleep(300)"
)

# A plain long-running child that exits cleanly on SIGTERM.
_SLEEPER = "import time; print('up', flush=True); time.sleep(300)"


@pytest.fixture
def proc_group() -> Iterator[ProcessGroup]:
    """A ProcessGroup whose children and stdout pipes are always cleaned up,
    so a killed subprocess never leaks an open fd into a later test."""

    mux = LogMultiplexer()
    pg = ProcessGroup(mux)
    try:
        yield pg
    finally:
        pg.terminate_all()
        # Killing the children gives the pump threads EOF; joining them lets
        # each close its pipe so no fd leaks into a later test.
        mux.join(timeout=1.0)


def _spawn(pg: ProcessGroup, src: str, name: str = "web") -> None:
    pg.spawn(
        name=name,
        argv=[sys.executable, "-c", src],
        cwd=Path("/tmp"),
        env={},
        color="cyan",
    )


@pytest.mark.integration
def test_request_shutdown_unblocks_wait_any_without_killing(proc_group: ProcessGroup) -> None:
    """A Ctrl+C should be able to unblock the main loop by *requesting*
    shutdown — the signal handler must not have to terminate processes
    itself just to make wait_any() return."""

    _spawn(proc_group, _SLEEPER)
    returned = threading.Event()

    def waiter() -> None:
        proc_group.wait_any()
        returned.set()

    threading.Thread(target=waiter, daemon=True).start()
    time.sleep(0.3)
    assert not returned.is_set()  # no process has exited

    proc_group.request_shutdown()

    # wait_any must return promptly even though the child is still alive.
    assert returned.wait(timeout=2.0)
    assert proc_group.all()[0].is_running()  # we did NOT kill it


@pytest.mark.integration
def test_terminate_all_kills_stubborn_child_after_grace(proc_group: ProcessGroup) -> None:
    """A child that ignores SIGTERM must still be reaped via SIGKILL once
    the grace period elapses — shutdown always completes."""

    proc_group.GRACE_SECONDS = 0.5
    _spawn(proc_group, _STUBBORN)
    time.sleep(0.4)  # let the child install its SIGTERM ignore handler

    start = time.monotonic()
    proc_group.terminate_all()
    elapsed = time.monotonic() - start

    assert not proc_group.all()[0].is_running()
    assert elapsed < 5.0  # grace (0.5s) + reap, nowhere near a hang


@pytest.mark.integration
def test_second_request_forces_immediate_kill(proc_group: ProcessGroup) -> None:
    """A second Ctrl+C while shutting down must escalate straight to SIGKILL
    instead of waiting out the full grace period."""

    proc_group.GRACE_SECONDS = 30.0  # huge grace: only the force path can be fast
    _spawn(proc_group, _STUBBORN)
    time.sleep(0.4)

    proc_group.request_shutdown()  # first Ctrl+C — graceful
    proc_group.request_shutdown()  # second Ctrl+C — force

    start = time.monotonic()
    proc_group.terminate_all()
    elapsed = time.monotonic() - start

    assert not proc_group.all()[0].is_running()
    assert elapsed < 5.0  # forced: must NOT wait the 30s grace


class _OkHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok\n")

    def log_message(self, fmt, *args) -> None:  # silence test logs
        pass
