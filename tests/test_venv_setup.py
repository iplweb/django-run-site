"""Venv setup + deps installer tests (§10.4)."""

from __future__ import annotations

import os
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest

from django_run_site.errors import VenvError
from django_run_site.source.deps_installer import (
    DepsRunner,
    DepsStrategy,
    detect_deps_strategy,
    install_dependencies,
)
from django_run_site.source.venv_setup import (
    VenvRunner,
    ensure_venv,
    is_marker_stale,
    touch_marker,
    venv_python_path,
)


class RecordingVenvRunner(VenvRunner):
    def __init__(self) -> None:
        self.calls: list[Sequence[str]] = []

    def run(self, argv: Sequence[str]) -> None:
        self.calls.append(tuple(argv))
        # Pretend the venv was created — touch the python file.
        if argv[0] == "uv" and "venv" in argv:
            target = Path(argv[2])
        elif "-m" in argv and "venv" in argv:
            target = Path(argv[-1])
        else:
            return
        bin_dir = target / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        py = bin_dir / "python"
        py.write_text("#!/bin/sh\n")
        py.chmod(0o755)


def test_ensure_venv_uses_existing(tmp_path: Path) -> None:
    venv_dir = tmp_path / ".venv"
    bin_dir = venv_dir / "bin"
    bin_dir.mkdir(parents=True)
    fake_py = bin_dir / "python"
    fake_py.write_text("#!/bin/sh\n")
    fake_py.chmod(0o755)
    runner = RecordingVenvRunner()
    result = ensure_venv(project_root=tmp_path, no_install=False, runner=runner)
    assert result.created is False
    assert result.python == fake_py
    assert result.backend == "existing"
    assert runner.calls == []


def test_ensure_venv_no_install_missing_errors(tmp_path: Path) -> None:
    runner = RecordingVenvRunner()
    with pytest.raises(VenvError, match="venv missing"):
        ensure_venv(project_root=tmp_path, no_install=True, runner=runner)


def test_ensure_venv_creates_via_python_m_venv_when_no_uv(tmp_path: Path, monkeypatch) -> None:
    """With no `uv` on PATH, we fall back to python -m venv."""

    monkeypatch.setenv("PATH", str(tmp_path / "noPATH"))
    runner = RecordingVenvRunner()
    result = ensure_venv(project_root=tmp_path, no_install=False, runner=runner)
    assert result.created is True
    assert result.backend == "python -m venv"
    assert any("-m" in c and "venv" in c for c in runner.calls)


def test_detect_deps_strategy_uv_lock(tmp_path: Path) -> None:
    (tmp_path / "uv.lock").touch()
    assert detect_deps_strategy(tmp_path) is DepsStrategy.UV_LOCK


def test_detect_deps_strategy_pyproject(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").touch()
    assert detect_deps_strategy(tmp_path) is DepsStrategy.PYPROJECT


def test_detect_deps_strategy_requirements(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").touch()
    assert detect_deps_strategy(tmp_path) is DepsStrategy.REQUIREMENTS


def test_detect_deps_strategy_none(tmp_path: Path) -> None:
    assert detect_deps_strategy(tmp_path) is DepsStrategy.NONE


def test_marker_stale_when_missing(tmp_path: Path) -> None:
    venv = tmp_path / ".venv"
    venv.mkdir()
    deps = tmp_path / "uv.lock"
    deps.touch()
    assert is_marker_stale(venv_dir=venv, deps_signal_files=[deps]) is True


def test_marker_fresh_after_touch(tmp_path: Path) -> None:
    venv = tmp_path / ".venv"
    venv.mkdir()
    deps = tmp_path / "uv.lock"
    deps.touch()
    # Force deps mtime older than now.
    old = os.stat(deps).st_mtime - 100
    os.utime(deps, (old, old))
    touch_marker(venv)
    assert is_marker_stale(venv_dir=venv, deps_signal_files=[deps]) is False


class RecordingDepsRunner(DepsRunner):
    def __init__(self) -> None:
        self.calls: list[Sequence[str]] = []

    def run(self, argv: Sequence[str]) -> None:
        self.calls.append(tuple(argv))


def test_install_dependencies_uv_lock(tmp_path: Path) -> None:
    venv = tmp_path / ".venv"
    venv.mkdir()
    (tmp_path / "uv.lock").touch()
    if not _has_uv():
        pytest.skip("uv not on PATH; cannot exercise UV_LOCK strategy")
    runner = RecordingDepsRunner()
    result = install_dependencies(
        project_root=tmp_path,
        venv_dir=venv,
        no_install=False,
        runner=runner,
    )
    assert result.strategy is DepsStrategy.UV_LOCK
    assert result.skipped is False
    assert any(c[0] == "uv" and c[1] == "sync" for c in runner.calls)


def test_install_dependencies_no_install_skips(tmp_path: Path) -> None:
    venv = tmp_path / ".venv"
    venv.mkdir()
    runner = RecordingDepsRunner()
    result = install_dependencies(
        project_root=tmp_path,
        venv_dir=venv,
        no_install=True,
        runner=runner,
    )
    assert result.skipped is True
    assert runner.calls == []


def test_install_dependencies_requirements_txt(tmp_path: Path) -> None:
    venv = tmp_path / ".venv"
    bin_dir = venv / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "pip").write_text("#!/bin/sh\n")
    (bin_dir / "pip").chmod(0o755)
    (bin_dir / "python").write_text("#!/bin/sh\n")
    (bin_dir / "python").chmod(0o755)
    (tmp_path / "requirements.txt").write_text("django>=5\n")
    runner = RecordingDepsRunner()
    result = install_dependencies(
        project_root=tmp_path,
        venv_dir=venv,
        no_install=False,
        runner=runner,
    )
    assert result.strategy is DepsStrategy.REQUIREMENTS
    assert any("pip" in c[0] and c[1] == "install" and c[2] == "-r" for c in runner.calls)


def _has_uv() -> bool:
    import shutil

    return shutil.which("uv") is not None


def test_venv_python_path() -> None:
    if sys.platform != "win32":
        assert venv_python_path(Path("/x")).name == "python"
        assert venv_python_path(Path("/x")).parent.name == "bin"
