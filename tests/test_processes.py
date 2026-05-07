"""Process / HTTP probe tests (§14, §16)."""

from __future__ import annotations

import http.server
import socket
import threading
from pathlib import Path

import pytest

from django_run_site.processes import (
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


class _OkHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok\n")

    def log_message(self, fmt, *args) -> None:  # silence test logs
        pass
