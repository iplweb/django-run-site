"""Project root, ``manage.py``, and local Python resolution (§7).

The CLI never imports Django, so a "Python interpreter" here is whatever
will execute ``manage.py`` as a subprocess. It may be a single executable
path or a multi-token command prefix like ``["uv", "run", "python"]``.
"""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Sequence
from pathlib import Path

from django_run_site.config import RunSiteConfig
from django_run_site.errors import DiscoveryError


def discover_project_root(
    *,
    cli_root: Path | None,
    config_root: Path | None,
    cwd: Path,
) -> Path:
    """Resolve the project root following §7.1.

    Priority: ``--project-root`` > ``project_root`` from config > nearest
    parent with ``runsite.toml`` > ``pyproject.toml`` > ``.git`` > CWD.
    """

    if cli_root is not None:
        return cli_root.expanduser().resolve()
    if config_root is not None:
        return config_root.expanduser().resolve()

    for marker in ("runsite.toml", "pyproject.toml", ".git"):
        for candidate in [cwd, *cwd.parents]:
            if (candidate / marker).exists():
                return candidate.resolve()
    return cwd.resolve()


def discover_manage_py(
    *,
    cli_manage: Path | None,
    config: RunSiteConfig,
) -> Path:
    """Resolve absolute path to ``manage.py`` (§7.2)."""

    if cli_manage is not None:
        path = cli_manage.expanduser().resolve()
        if not path.is_file():
            raise DiscoveryError(f"--manage-py path does not exist: {path}")
        return path

    if config.manage_py is not None:
        path = (config.project_root / config.manage_py).resolve()
        if not path.is_file():
            raise DiscoveryError(
                f"manage_py from config does not exist: {path} (configured as {config.manage_py!r})"
            )
        return path

    for candidate in ("src/manage.py", "manage.py"):
        path = (config.project_root / candidate).resolve()
        if path.is_file():
            return path

    raise DiscoveryError(
        f"Could not find manage.py in {config.project_root}. "
        "Set --manage-py or 'manage_py' in runsite.toml."
    )


def discover_local_python(
    *,
    cli_python: Path | None,
    config: RunSiteConfig,
    env: dict[str, str] | None = None,
) -> tuple[str, ...]:
    """Resolve the local Python *command* (§7.3) as a tuple of arguments.

    The result is suitable for use as a subprocess argv prefix:
    ``[*python, manage_py_path, "migrate"]``.
    """

    project_root = config.project_root
    env = dict(env if env is not None else os.environ)

    # 1. CLI flag wins.
    if cli_python is not None:
        path = Path(cli_python).expanduser().resolve()
        if not path.is_file():
            raise DiscoveryError(f"--python path does not exist: {path}")
        return (str(path),)

    # 2. [python].command (multi-token prefix).
    if config.python.command is not None:
        return _resolve_command(config.python.command)

    # 3. [python].executable (single path or "auto").
    executable = config.python.executable
    if executable is not None and executable not in ("", "auto"):
        path = Path(executable).expanduser()
        path = path.resolve() if path.is_absolute() else (project_root / path).resolve()
        if not path.is_file():
            raise DiscoveryError(
                f"[python].executable={config.python.executable!r} does not exist "
                f"(resolved to {path})"
            )
        return (str(path),)

    # 4-8. "auto" fallback chain.
    return _auto_python_chain(project_root, env)


def _auto_python_chain(project_root: Path, env: dict[str, str]) -> tuple[str, ...]:
    # 4. RUN_SITE_PYTHON env.
    run_site_python = env.get("RUN_SITE_PYTHON")
    if run_site_python:
        path = Path(run_site_python).expanduser().resolve()
        if path.is_file():
            return (str(path),)

    # 5. $VIRTUAL_ENV/bin/python.
    virtual_env = env.get("VIRTUAL_ENV")
    if virtual_env:
        candidate = Path(virtual_env) / "bin" / "python"
        if candidate.is_file():
            return (str(candidate.resolve()),)

    # 6. .venv/bin/python in project root.
    candidate = project_root / ".venv" / "bin" / "python"
    if candidate.is_file():
        return (str(candidate.resolve()),)

    # 7. uv run python — only if uv.lock is present and uv is in PATH.
    if (project_root / "uv.lock").is_file() and shutil.which("uv"):
        return ("uv", "run", "python")

    # 8. sys.executable fallback.
    return (sys.executable,)


def _resolve_command(command: Sequence[str]) -> tuple[str, ...]:
    """Resolve the first token via ``shutil.which`` if it's not a path."""

    if not command:
        raise DiscoveryError("[python].command is empty")
    head, *tail = command
    if "/" in head or os.sep in head:
        path = Path(head).expanduser()
        if not path.is_absolute():
            path = path.resolve()
        if not path.is_file():
            raise DiscoveryError(f"[python].command[0] does not exist: {path}")
        return (str(path), *tail)
    resolved = shutil.which(head)
    if resolved is None:
        raise DiscoveryError(f"[python].command[0]={head!r} not found on PATH")
    return (resolved, *tail)
