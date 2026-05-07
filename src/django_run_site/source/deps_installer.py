"""Detect and install Python dependencies for a project (§10.4)."""

from __future__ import annotations

import logging
import shutil
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from django_run_site.errors import VenvError
from django_run_site.source.venv_setup import (
    is_marker_stale,
    touch_marker,
    venv_python_path,
)

logger = logging.getLogger(__name__)


class DepsStrategy(Enum):
    UV_LOCK = "uv-lock"
    PYPROJECT = "pyproject"
    REQUIREMENTS = "requirements"
    PIPFILE_LOCK = "pipfile-lock"  # not implemented; warn only
    NONE = "none"


@dataclass(frozen=True)
class DepsResult:
    strategy: DepsStrategy
    skipped: bool
    reason: str | None = None


def detect_deps_strategy(project_root: Path) -> DepsStrategy:
    """Pick the best strategy by inspecting files in *project_root*."""

    if (project_root / "uv.lock").is_file():
        return DepsStrategy.UV_LOCK
    if (project_root / "pyproject.toml").is_file():
        return DepsStrategy.PYPROJECT
    if (project_root / "requirements.txt").is_file():
        return DepsStrategy.REQUIREMENTS
    if (project_root / "Pipfile.lock").is_file():
        return DepsStrategy.PIPFILE_LOCK
    return DepsStrategy.NONE


def deps_signal_files(project_root: Path) -> list[Path]:
    """Files whose mtime invalidates the install marker."""

    return [
        project_root / "uv.lock",
        project_root / "pyproject.toml",
        project_root / "requirements.txt",
        project_root / "Pipfile.lock",
    ]


def install_dependencies(
    *,
    project_root: Path,
    venv_dir: Path,
    no_install: bool,
    runner: DepsRunner | None = None,
) -> DepsResult:
    """Install project dependencies into *venv_dir*.

    With ``no_install=True``, returns immediately as skipped. Otherwise
    selects a strategy and installs.
    """

    if no_install:
        return DepsResult(strategy=DepsStrategy.NONE, skipped=True, reason="--no-install")

    strategy = detect_deps_strategy(project_root)
    runner = runner or RealDepsRunner()

    if strategy is DepsStrategy.NONE:
        return DepsResult(
            strategy=strategy,
            skipped=True,
            reason="no uv.lock / pyproject.toml / requirements.txt found",
        )
    if strategy is DepsStrategy.PIPFILE_LOCK:
        return DepsResult(
            strategy=strategy,
            skipped=True,
            reason="Pipfile.lock detected; pipenv install is out of scope (v0.3)",
        )

    signal_files = deps_signal_files(project_root)
    if not is_marker_stale(venv_dir=venv_dir, deps_signal_files=signal_files):
        return DepsResult(
            strategy=strategy,
            skipped=True,
            reason="deps marker fresher than dependency files; nothing to do",
        )

    pip = venv_python_path(venv_dir).with_name("pip")

    if strategy is DepsStrategy.UV_LOCK:
        if shutil.which("uv") is None:
            raise VenvError(
                "uv.lock present but `uv` is not on PATH. "
                "Install uv (https://docs.astral.sh/uv) or remove uv.lock."
            )
        runner.run(["uv", "sync", "--project", str(project_root)])
    elif strategy is DepsStrategy.PYPROJECT:
        if shutil.which("uv") is not None:
            runner.run(["uv", "sync", "--project", str(project_root)])
        else:
            runner.run([str(pip), "install", "-e", str(project_root)])
    elif strategy is DepsStrategy.REQUIREMENTS:
        runner.run([str(pip), "install", "-r", str(project_root / "requirements.txt")])

    touch_marker(venv_dir)
    return DepsResult(strategy=strategy, skipped=False)


# ---------------------------------------------------------------------------
# Runner abstraction
# ---------------------------------------------------------------------------


class DepsRunner:
    def run(self, argv: Sequence[str]) -> None:
        raise NotImplementedError


class RealDepsRunner(DepsRunner):
    def run(self, argv: Sequence[str]) -> None:
        proc = subprocess.run(list(argv), check=False, text=True, capture_output=False)
        if proc.returncode != 0:
            raise VenvError(f"Dependency install failed (exit {proc.returncode}): {' '.join(argv)}")
