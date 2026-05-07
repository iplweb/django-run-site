"""Local subprocess spawning, supervision, and HTTP probing.

Three flavors of subprocess work here:

- :func:`run_oneshot` — fire-and-wait (migrate, superuser setup, hooks).
  Stdout/stderr is captured or streamed to a multiplexer.
- :class:`ManagedProcess` — long-lived (runserver, celery, extras), tied
  to a multiplexer stream and terminated by :class:`ProcessGroup`.
- :func:`wait_for_http` — readiness probe.
"""

from __future__ import annotations

import errno
import http.client
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from run_site.log_multiplexer import LogMultiplexer, StreamSpec


@dataclass
class ProcessResult:
    """Outcome of a one-shot subprocess invocation."""

    returncode: int
    stdout: str
    stderr: str
    duration: float

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def find_free_port(host: str = "127.0.0.1") -> int:
    """Ask the OS for a free TCP port."""

    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        sock.bind((host, 0))
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return sock.getsockname()[1]


def run_oneshot(
    argv: Sequence[str],
    *,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    timeout: float | None = None,
    check: bool = False,
    capture_output: bool = True,
    mux: LogMultiplexer | None = None,
    mux_stream: StreamSpec | None = None,
) -> ProcessResult:
    """Run *argv* to completion. Returns :class:`ProcessResult`.

    With *mux* + *mux_stream*, output is streamed live to the multiplexer
    rather than captured. *check=True* raises :class:`subprocess.CalledProcessError`
    on non-zero exit.
    """

    if mux is not None and mux_stream is not None:
        return _run_oneshot_streamed(
            argv, cwd=cwd, env=env, timeout=timeout, check=check, mux=mux, spec=mux_stream
        )

    started = time.monotonic()
    proc = subprocess.run(
        list(argv),
        cwd=str(cwd) if cwd is not None else None,
        env=dict(env) if env is not None else None,
        capture_output=capture_output,
        text=True,
        timeout=timeout,
        check=check,
    )
    duration = time.monotonic() - started
    return ProcessResult(
        returncode=proc.returncode,
        stdout=proc.stdout if proc.stdout is not None else "",
        stderr=proc.stderr if proc.stderr is not None else "",
        duration=duration,
    )


def _run_oneshot_streamed(
    argv: Sequence[str],
    *,
    cwd: Path | None,
    env: Mapping[str, str] | None,
    timeout: float | None,
    check: bool,
    mux: LogMultiplexer,
    spec: StreamSpec,
) -> ProcessResult:
    started = time.monotonic()
    proc = subprocess.Popen(
        list(argv),
        cwd=str(cwd) if cwd is not None else None,
        env=dict(env) if env is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    if proc.stdout is not None:
        mux.attach(spec, proc.stdout)  # type: ignore[arg-type]
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        raise
    duration = time.monotonic() - started
    if check and proc.returncode != 0:
        raise subprocess.CalledProcessError(proc.returncode, list(argv))
    return ProcessResult(returncode=proc.returncode, stdout="", stderr="", duration=duration)


@dataclass
class ManagedProcess:
    """A long-lived subprocess plumbed into the log multiplexer."""

    name: str
    argv: tuple[str, ...]
    cwd: Path
    env: Mapping[str, str]
    color: str
    popen: subprocess.Popen[bytes] | None = None
    returncode: int | None = None

    def is_running(self) -> bool:
        return self.popen is not None and self.popen.poll() is None


class ProcessGroup:
    """Track the lifecycle of multiple ManagedProcesses with mux output.

    Use :meth:`spawn` to start a process and attach it to the muxer; use
    :meth:`wait_any` to block until one of them exits; use :meth:`terminate_all`
    to fan out SIGTERM (then SIGKILL after a grace period).
    """

    GRACE_SECONDS = 5.0

    def __init__(self, mux: LogMultiplexer) -> None:
        self._mux = mux
        self._procs: list[ManagedProcess] = []
        self._exited = threading.Event()

    def spawn(
        self,
        *,
        name: str,
        argv: Sequence[str],
        cwd: Path,
        env: Mapping[str, str],
        color: str,
    ) -> ManagedProcess:
        proc = ManagedProcess(
            name=name,
            argv=tuple(argv),
            cwd=cwd,
            env=dict(env),
            color=color,
        )
        proc.popen = subprocess.Popen(
            list(argv),
            cwd=str(cwd),
            env=dict(env),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=1,
            preexec_fn=os.setsid if sys.platform != "win32" else None,
        )
        spec = self._mux.stream(name, color)
        if proc.popen.stdout is not None:
            self._mux.attach(spec, proc.popen.stdout)
        self._procs.append(proc)
        threading.Thread(target=self._watch_one, args=(proc,), daemon=True).start()
        return proc

    def _watch_one(self, proc: ManagedProcess) -> None:
        if proc.popen is None:
            return
        try:
            proc.returncode = proc.popen.wait()
        finally:
            self._exited.set()

    def all(self) -> list[ManagedProcess]:
        return list(self._procs)

    def primary(self) -> ManagedProcess | None:
        """The first process spawned — typically runserver."""

        return self._procs[0] if self._procs else None

    def wait_any(self) -> ManagedProcess | None:
        """Block until at least one managed process exits.

        Returns the first exited process, or None if interrupted.
        """

        self._exited.wait()
        for proc in self._procs:
            if proc.popen is not None and proc.popen.poll() is not None:
                return proc
        return None

    def terminate_all(self) -> None:
        """Send SIGTERM to all process groups, then SIGKILL after grace."""

        deadline = time.monotonic() + self.GRACE_SECONDS
        for proc in self._procs:
            if proc.popen is None:
                continue
            try:
                if sys.platform == "win32":
                    proc.popen.terminate()
                else:
                    os.killpg(os.getpgid(proc.popen.pid), signal.SIGTERM)
            except (ProcessLookupError, OSError) as exc:
                if exc.errno not in (errno.ESRCH, errno.EPERM):
                    raise
        for proc in self._procs:
            if proc.popen is None:
                continue
            remaining = max(deadline - time.monotonic(), 0.1)
            try:
                proc.popen.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                try:
                    if sys.platform == "win32":
                        proc.popen.kill()
                    else:
                        os.killpg(os.getpgid(proc.popen.pid), signal.SIGKILL)
                except (ProcessLookupError, OSError) as exc:
                    if exc.errno not in (errno.ESRCH, errno.EPERM):
                        raise
                proc.popen.wait()


def wait_for_http(
    url: str,
    *,
    timeout: float = 60.0,
    interval: float = 0.5,
    accept_below_status: int = 500,
) -> bool:
    """Block until *url* returns a status code below *accept_below_status*,
    or *timeout* seconds elapse.

    Returns True on success, False on timeout. 5xx responses keep retrying;
    connection refused keeps retrying."""

    parsed = urlparse(url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        conn: http.client.HTTPConnection | http.client.HTTPSConnection
        if parsed.scheme == "https":
            conn = http.client.HTTPSConnection(host, port, timeout=interval * 2)
        else:
            conn = http.client.HTTPConnection(host, port, timeout=interval * 2)
        try:
            conn.request("GET", path)
            response = conn.getresponse()
            status = response.status
            response.read()
            if status < accept_below_status:
                return True
        except (
            TimeoutError,
            ConnectionRefusedError,
            ConnectionResetError,
            socket.gaierror,
            http.client.RemoteDisconnected,
            OSError,
        ):
            pass
        finally:
            try:
                conn.close()
            except OSError as exc:
                if exc.errno != errno.EBADF:
                    raise
        time.sleep(interval)
    return False


@dataclass
class TemplateContext:
    """Variables substituted into ``extra_processes.command``."""

    python: tuple[str, ...]
    manage_py: Path
    manage_dir: Path
    project_root: Path
    port: int
    extras: dict[str, str] = field(default_factory=dict)

    def expand(self, command: Sequence[str]) -> tuple[str, ...]:
        out: list[str] = []
        for token in command:
            replaced = token
            if "{python}" in replaced:
                # Inline-expand multi-token python prefix only when it's the
                # only contents of the token.
                if replaced == "{python}":
                    out.extend(self.python)
                    continue
                replaced = replaced.replace("{python}", " ".join(self.python))
            replaced = replaced.replace("{manage_py}", str(self.manage_py))
            replaced = replaced.replace("{manage_dir}", str(self.manage_dir))
            replaced = replaced.replace("{project_root}", str(self.project_root))
            replaced = replaced.replace("{port}", str(self.port))
            for key, value in self.extras.items():
                replaced = replaced.replace("{" + key + "}", value)
            out.append(replaced)
        return tuple(out)


def docker_logs_follow(container_id: str) -> tuple[str, ...]:
    """Build a ``docker logs -f`` argv. Returns empty tuple if docker not in PATH."""

    docker = shutil.which("docker")
    if docker is None:
        return ()
    return (docker, "logs", "-f", "--tail", "0", container_id)
