"""Closing the terminal (SIGHUP) must tear the stack down like Ctrl+C does.

Managed processes run in their own sessions (``os.setsid``), so the kernel's
hangup never reaches them — only run-site sees it. If run-site simply dies,
runserver / celery survive as orphans re-parented to init.
"""

from __future__ import annotations

import contextlib
import os
import select
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from run_site.processes import find_free_port

pytestmark = [
    pytest.mark.integration,
    pytest.mark.slow,
    pytest.mark.skipif(sys.platform == "win32", reason="no SIGHUP / pty on Windows"),
]


def _write_fake_project(root: Path, marker: str) -> None:
    (root / "manage.py").write_text("import sys\nsys.exit(0)\n")
    web = f"import time; print('{marker} up', flush=True); time.sleep(600)"
    (root / "runsite.toml").write_text(
        'project_slug = "fake"\n'
        'manage_py = "manage.py"\n'
        "[python]\n"
        f"executable = {str(sys.executable)!r}\n"
        "[postgres]\n"
        "enabled = false\n"
        "[redis]\n"
        "enabled = false\n"
        "[django]\n"
        "migrate = false\n"
        f'web_command = ["{{python}}", "-c", "{web}"]\n'
        "[superuser]\n"
        "enabled = false\n"
    )


def _pids_matching(marker: str) -> list[str]:
    result = subprocess.run(["pgrep", "-f", marker], capture_output=True, text=True, check=False)
    return result.stdout.split()


def _read_until(fd: int, needle: bytes, timeout: float) -> bytes:
    buf = b""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and needle not in buf:
        ready, _, _ = select.select([fd], [], [], 0.2)
        if ready:
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                break  # EIO: the child side of the pty is gone
            if not chunk:
                break
            buf += chunk
    return buf


def _wait_until(predicate, timeout: float) -> bool:  # type: ignore[no-untyped-def]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return predicate()


def test_terminal_hangup_stops_managed_processes(tmp_path: Path) -> None:
    import pty

    marker = f"run-site-hangup-{uuid.uuid4().hex}"
    _write_fake_project(tmp_path, marker)
    port = find_free_port()

    pid, fd = pty.fork()
    if pid == 0:  # child: become run-site on the pty
        os.chdir(tmp_path)
        os.execv(
            sys.executable,
            [sys.executable, "-m", "run_site", "run", "--no-browser", "--port", str(port)],
        )
    try:
        output = _read_until(fd, f"{marker} up".encode(), timeout=30)
        assert f"{marker} up".encode() in output, output.decode(errors="replace")[-2000:]
        assert _pids_matching(marker), "web process should be running before the hangup"

        os.close(fd)  # the terminal window goes away -> SIGHUP to run-site
        fd = -1

        assert _wait_until(lambda: os.waitpid(pid, os.WNOHANG) != (0, 0), timeout=20)
        pid = -1
        assert _wait_until(lambda: not _pids_matching(marker), timeout=5), (
            "web process was orphaned after the terminal hung up"
        )
        assert not (tmp_path / ".run-site-config").exists(), "shutdown cleanup did not run"
    finally:
        if fd >= 0:
            os.close(fd)
        if pid > 0:
            os.kill(pid, 9)
            os.waitpid(pid, 0)
        for leftover in _pids_matching(marker):
            # May exit between pgrep and kill; gone is exactly what we want.
            with contextlib.suppress(ProcessLookupError):
                os.kill(int(leftover), 9)


@pytest.mark.parametrize("detach", [False, True])
def test_writes_after_hangup_fail_unless_output_is_detached(tmp_path: Path, detach: bool) -> None:
    """Without detaching, the first shutdown message after a hangup raises EIO
    (which would abort the rest of the cleanup); detached, writes succeed."""

    import pty

    from run_site.cli import _detach_output_from_terminal, _tty_output_fds

    result_file = tmp_path / "result"
    pid, fd = pty.fork()
    if pid == 0:  # child: stand-in for run-site's shutdown path
        try:
            import signal

            tty_fds = _tty_output_fds()
            signal.signal(signal.SIGHUP, signal.SIG_IGN)
            while not result_file.with_suffix(".go").exists():
                time.sleep(0.05)
            if detach:
                _detach_output_from_terminal(tty_fds)
            try:
                # Straight to fd 1: pytest swaps sys.stdout for a capture buffer.
                os.write(1, b"[docker] removed container\n")
                outcome = f"ok {tty_fds}"
            except OSError as exc:
                outcome = f"error {exc.errno}"
            result_file.write_text(outcome)
        finally:
            os._exit(0)
    time.sleep(0.3)
    os.close(fd)  # hang up
    result_file.with_suffix(".go").touch()
    os.waitpid(pid, 0)

    outcome = result_file.read_text()
    if detach:
        assert outcome == "ok (1, 2)"
    else:
        assert outcome.startswith("error ")
