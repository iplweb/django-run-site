"""Migration file watcher tests."""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

import pytest

from run_site.migration_watcher import MigrationWatcher, changed_paths, snapshot_migrations


def _write(path: Path, content: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def test_snapshot_collects_only_project_migration_modules(tmp_path: Path) -> None:
    _write(tmp_path / "app" / "migrations" / "__init__.py")
    _write(tmp_path / "app" / "migrations" / "0001_initial.py", "x = 1\n")
    _write(tmp_path / "app" / "migrations" / "README.txt")
    _write(tmp_path / "app" / "models.py")
    _write(tmp_path / "src" / "other" / "migrations" / "0001_initial.py")
    _write(tmp_path / "node_modules" / "pkg" / "migrations" / "0001.py")
    _write(tmp_path / ".git" / "migrations" / "0001.py")
    _write(tmp_path / "env" / "pyvenv.cfg")
    _write(tmp_path / "env" / "lib" / "django" / "contrib" / "migrations" / "0001.py")
    _write(tmp_path / "lib" / "site-packages" / "thirdparty" / "migrations" / "0001.py")

    assert set(snapshot_migrations(tmp_path)) == {
        "app/migrations/__init__.py",
        "app/migrations/0001_initial.py",
        "src/other/migrations/0001_initial.py",
    }


def test_changed_paths_reports_added_removed_and_modified() -> None:
    old = {"a/migrations/0001.py": (1, 10), "a/migrations/0002.py": (1, 10), "keep.py": (1, 1)}
    new = {"a/migrations/0001.py": (2, 12), "a/migrations/0003.py": (1, 10), "keep.py": (1, 1)}

    assert changed_paths(old, new) == [
        "a/migrations/0001.py",
        "a/migrations/0002.py",
        "a/migrations/0003.py",
    ]


class _Recorder:
    def __init__(self, fail_first: bool = False) -> None:
        self.calls: list[list[str]] = []
        self.called = threading.Event()
        self._fail_first = fail_first

    def __call__(self, changed: list[str]) -> None:
        self.calls.append(changed)
        self.called.set()
        if self._fail_first and len(self.calls) == 1:
            raise RuntimeError("boom")

    def wait(self, count: int, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout
        while len(self.calls) < count:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.02)
        return True


@pytest.fixture
def project(tmp_path: Path) -> Path:
    _write(tmp_path / "app" / "migrations" / "__init__.py")
    _write(tmp_path / "app" / "migrations" / "0001_initial.py", "x = 1\n")
    _write(tmp_path / "app" / "models.py")
    return tmp_path


def test_watcher_fires_once_for_a_burst_of_new_migrations(project: Path) -> None:
    recorder = _Recorder()
    watcher = MigrationWatcher(project, recorder, interval=0.05, settle=0.2)
    watcher.start()
    try:
        _write(project / "app" / "migrations" / "0002_a.py", "a = 1\n")
        _write(project / "app" / "migrations" / "0003_b.py", "b = 1\n")
        assert recorder.wait(1)
        time.sleep(0.4)
    finally:
        watcher.stop(timeout=2)

    assert recorder.calls == [["app/migrations/0002_a.py", "app/migrations/0003_b.py"]]


def test_watcher_ignores_non_migration_changes(project: Path) -> None:
    recorder = _Recorder()
    watcher = MigrationWatcher(project, recorder, interval=0.05, settle=0.05)
    watcher.start()
    try:
        _write(project / "app" / "models.py", "class Changed: ...\n")
        _write(project / "app" / "views.py", "def view(): ...\n")
        time.sleep(0.4)
    finally:
        watcher.stop(timeout=2)

    assert recorder.calls == []


def test_watcher_notices_changes_made_before_start(project: Path) -> None:
    recorder = _Recorder()
    watcher = MigrationWatcher(project, recorder, interval=0.05, settle=0.05)
    _write(project / "app" / "migrations" / "0002_early.py", "early = 1\n")
    watcher.start()
    try:
        assert recorder.wait(1)
    finally:
        watcher.stop(timeout=2)

    assert recorder.calls == [["app/migrations/0002_early.py"]]


def test_watcher_survives_a_failing_callback(
    project: Path, caplog: pytest.LogCaptureFixture
) -> None:
    recorder = _Recorder(fail_first=True)
    watcher = MigrationWatcher(project, recorder, interval=0.05, settle=0.05)
    watcher.start()
    try:
        with caplog.at_level(logging.ERROR, logger="run_site.migration_watcher"):
            _write(project / "app" / "migrations" / "0002_a.py", "a = 1\n")
            assert recorder.wait(1)
            _write(project / "app" / "migrations" / "0003_b.py", "b = 1\n")
            assert recorder.wait(2)
    finally:
        watcher.stop(timeout=2)

    assert recorder.calls[1] == ["app/migrations/0003_b.py"]
    assert "callback failed" in caplog.text


def test_stop_before_start_is_a_no_op(project: Path) -> None:
    MigrationWatcher(project, _Recorder()).stop(timeout=1)
