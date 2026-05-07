"""Create a project venv with ``uv venv`` (preferred) or ``python -m venv``.

This module is testable without actually creating venvs by injecting a
:class:`VenvRunner` mock.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from django_run_site.errors import VenvError

MARKER_FILE = ".dev_helpers_installed_marker"


@dataclass(frozen=True)
class VenvResult:
    """Outcome of venv setup."""

    venv_dir: Path
    python: Path
    created: bool  # True when freshly created in this run
    backend: str  # "uv" | "python -m venv" | "existing"


def venv_python_path(venv_dir: Path) -> Path:
    """The python interpreter inside *venv_dir*."""

    if sys.platform == "win32":  # pragma: no cover - non-POSIX
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def ensure_venv(
    *,
    project_root: Path,
    no_install: bool,
    runner: VenvRunner | None = None,
) -> VenvResult:
    """Make sure a usable venv exists at ``<project_root>/.venv``.

    With ``no_install=True``, only verify; never create. With
    ``no_install=False``, create if missing using ``uv venv`` if available
    else ``python -m venv``.
    """

    runner = runner or RealVenvRunner()
    venv_dir = project_root / ".venv"
    python = venv_python_path(venv_dir)

    if python.is_file():
        return VenvResult(venv_dir=venv_dir, python=python, created=False, backend="existing")

    if no_install:
        raise VenvError(
            f"venv missing in {venv_dir}. Run without --no-install to create "
            "venv and install deps, or create venv manually first."
        )

    if shutil.which("uv"):
        runner.run(["uv", "venv", str(venv_dir)])
        backend = "uv"
    else:
        runner.run([sys.executable, "-m", "venv", str(venv_dir)])
        backend = "python -m venv"

    if not python.is_file():
        raise VenvError(f"venv creation appeared to succeed but {python} doesn't exist")
    return VenvResult(venv_dir=venv_dir, python=python, created=True, backend=backend)


def is_marker_stale(*, venv_dir: Path, deps_signal_files: list[Path]) -> bool:
    """Return True if any of *deps_signal_files* is newer than the marker.

    Used to detect "deps file changed since last install".
    """

    marker = venv_dir / MARKER_FILE
    if not marker.exists():
        return True
    marker_mtime = marker.stat().st_mtime
    return any(f.exists() and f.stat().st_mtime > marker_mtime for f in deps_signal_files)


def touch_marker(venv_dir: Path) -> None:
    """Create or update the install-success marker."""

    (venv_dir / MARKER_FILE).touch(exist_ok=True)


# ---------------------------------------------------------------------------
# Runner abstraction
# ---------------------------------------------------------------------------


class VenvRunner:
    def run(self, argv: Sequence[str]) -> None:
        raise NotImplementedError


class RealVenvRunner(VenvRunner):
    def run(self, argv: Sequence[str]) -> None:
        proc = subprocess.run(list(argv), check=False, text=True, capture_output=True)
        if proc.returncode != 0:
            raise VenvError(
                f"venv command failed (exit {proc.returncode}): {' '.join(argv)}\n"
                f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
            )
