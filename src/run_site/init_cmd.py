"""``run-site init`` — generate a sensible default ``runsite.toml``.

The goal is that running ``init`` in a typical Django project root produces
a config the user can use as-is, with no editing required:

* Detects ``manage.py`` at the project root, under ``src/``, or in a
  bundled test/example project (one or two directories deep). Multiple
  candidates are filtered to those that actually ``import django``.
* Detects the Django project module (directory next to ``manage.py``
  containing ``settings.py`` or a ``settings/`` package) and uses its name
  as the default ``project_slug`` / Postgres database / user.
* Detects Celery from ``<django_module>/celery.py`` or ``celery_tasks.py``
  and pre-fills ``[celery]`` enabled.
* Detects ``uv`` and emits ``[python].command = ["uv", "run", "--no-sync",
  "python"]`` as the default Python invocation. Falls back to
  ``executable = "auto"`` if ``uv`` is not on ``PATH``.
* Falls back to ``[project].name`` from ``pyproject.toml`` when no Django
  module can be found.

This module deliberately does no Docker / Django / venv work — it only
inspects the filesystem and writes a TOML file.
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from run_site.discovery import autoscan_manage_py, discover_project_root, imports_django
from run_site.errors import RunSiteError

CONFIG_FILENAME = "runsite.toml"
SLUG_RE = re.compile(r"[A-Za-z0-9_.-]+")
INVALID_SLUG_CHARS = re.compile(r"[^A-Za-z0-9_.-]+")

# Default `uv run` invocation written into runsite.toml when uv is detected.
# --no-sync skips the implicit sync that uv would otherwise run on every
# invocation; we already run `uv sync` once via install_dependencies.
DEFAULT_UV_COMMAND: tuple[str, ...] = ("uv", "run", "--no-sync", "python")


@dataclass(frozen=True)
class DetectedDefaults:
    """The values inferred from the project layout."""

    project_root: Path
    manage_py_rel: str  # relative POSIX path from project_root
    project_slug: str
    django_module: str | None
    celery_app: str | None
    has_uv_lock: bool
    has_venv: bool
    has_uv: bool  # `uv` is on PATH


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------


def init_command(argv: Sequence[str]) -> int:
    """Implements ``run-site init``."""

    try:
        return _init_command_inner(argv)
    except RunSiteError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return exc.exit_code


def _init_command_inner(argv: Sequence[str]) -> int:
    parser = _build_parser()
    opts = parser.parse_args(list(argv))

    cwd = Path.cwd()
    project_root = discover_project_root(
        cli_root=opts.project_root,
        config_root=None,
        cwd=cwd,
    )

    target = (opts.output if opts.output else project_root / CONFIG_FILENAME).resolve()

    if target.exists() and not opts.force:
        raise RunSiteError(
            f"{target} already exists; pass --force to overwrite."
        )

    pyproj_warning = _check_pyproject_section(project_root)

    detected = _detect_defaults(project_root)
    contents = _render_toml(detected, with_celery=detected.celery_app is not None)

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(contents, encoding="utf-8")

    sys.stdout.write(f"Wrote {target}\n")
    sys.stdout.write(f"  project_slug   = {detected.project_slug!r}\n")
    sys.stdout.write(f"  manage_py      = {detected.manage_py_rel!r}\n")
    if detected.django_module:
        sys.stdout.write(f"  Django module  = {detected.django_module!r}\n")
    if detected.celery_app:
        sys.stdout.write(f"  celery.app     = {detected.celery_app!r} (enabled)\n")
    if detected.has_uv:
        sys.stdout.write(
            f"  python.command = {list(DEFAULT_UV_COMMAND)} "
            f"(uv {'+ uv.lock' if detected.has_uv_lock else 'detected'})\n"
        )
    elif detected.has_venv:
        sys.stdout.write(
            "  .venv/ detected — `executable = \"auto\"` will pick its python\n"
        )
    else:
        sys.stdout.write(
            "  uv not on PATH — using `executable = \"auto\"`. "
            "Install uv (https://docs.astral.sh/uv) for `uv run` integration.\n"
        )

    if pyproj_warning:
        sys.stdout.write(f"\nNote: {pyproj_warning}\n")

    sys.stdout.write(
        "\nNext: run `run-site doctor` to sanity-check, "
        "or `run-site run` to start the stack.\n"
    )
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run-site init",
        description=(
            "Generate a default runsite.toml using values inferred from the "
            "project layout (manage.py location, Django project module, "
            "Celery presence, uv availability, etc)."
        ),
    )
    parser.add_argument(
        "--project-root",
        type=Path,
        default=None,
        metavar="PATH",
        help="Override project root (default: auto-discovered from CWD).",
    )
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        default=None,
        metavar="PATH",
        help=f"Where to write the config (default: <project_root>/{CONFIG_FILENAME}).",
    )
    parser.add_argument(
        "--force",
        "-f",
        action="store_true",
        help="Overwrite an existing config file.",
    )
    return parser


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


def _detect_defaults(project_root: Path) -> DetectedDefaults:
    manage_py_rel = _find_manage_py(project_root)
    if manage_py_rel is None:
        raise RunSiteError(
            f"Could not find manage.py under {project_root} (scanned the "
            "root and one+two directories deep, skipping build/cache dirs). "
            "Move into your Django project or pass --project-root."
        )

    manage_py_abs = (project_root / manage_py_rel).resolve()
    django_module = _find_django_module(manage_py_abs.parent)
    pyproj_name = _read_pyproject_name(project_root)

    project_slug = _pick_slug(
        django_module=django_module,
        pyproject_name=pyproj_name,
        project_root=project_root,
    )

    celery_app = None
    if django_module is not None:
        celery_app = _detect_celery(manage_py_abs.parent / django_module, django_module)

    has_uv_lock = (project_root / "uv.lock").is_file()
    has_venv = (project_root / ".venv" / "bin" / "python").is_file()
    has_uv = shutil.which("uv") is not None

    return DetectedDefaults(
        project_root=project_root,
        manage_py_rel=manage_py_rel,
        project_slug=project_slug,
        django_module=django_module,
        celery_app=celery_app,
        has_uv_lock=has_uv_lock,
        has_venv=has_venv,
        has_uv=has_uv,
    )


def _find_manage_py(project_root: Path) -> str | None:
    """Return the relative POSIX path to manage.py, or None.

    Uses the shared :func:`autoscan_manage_py` so init's detection
    matches what ``run-site run`` will pick up later. When several
    matches exist we keep only those that ``import django`` and pick
    the shallowest; init is allowed to commit to a guess (vs. erroring
    out) because the user can edit the generated TOML.
    """

    candidates = autoscan_manage_py(project_root)
    if not candidates:
        return None
    if len(candidates) > 1:
        legit = [c for c in candidates if imports_django(c)]
        if legit:
            candidates = legit
    chosen = candidates[0]
    return chosen.relative_to(project_root).as_posix()


def _find_django_module(manage_py_dir: Path) -> str | None:
    """Find the directory containing ``settings.py`` next to ``manage.py``.

    Returns the module name (a single dir component), or None if nothing
    matches. We deliberately limit the search to direct children — going
    deeper risks picking up vendored apps or test fixtures.
    """

    if not manage_py_dir.is_dir():
        return None

    candidates: list[str] = []
    for entry in sorted(manage_py_dir.iterdir()):
        if not entry.is_dir():
            continue
        if entry.name.startswith(".") or entry.name in {"__pycache__", "node_modules"}:
            continue
        settings_py = entry / "settings.py"
        settings_pkg = entry / "settings" / "__init__.py"
        if settings_py.is_file() or settings_pkg.is_file():
            candidates.append(entry.name)

    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    # Multiple matches — prefer the shortest / first alphabetically and warn
    # the user implicitly by writing what we picked into the file.
    return candidates[0]


def _detect_celery(django_module_dir: Path, django_module: str) -> str | None:
    """Return ``<module>.celery`` (or ``celery_tasks``) if a Celery entry-
    point file exists next to settings, else None."""

    if (django_module_dir / "celery.py").is_file():
        return f"{django_module}.celery"
    if (django_module_dir / "celery_tasks.py").is_file():
        return f"{django_module}.celery_tasks"
    return None


def _read_pyproject_name(project_root: Path) -> str | None:
    pyproj = project_root / "pyproject.toml"
    if not pyproj.is_file():
        return None
    try:
        with pyproj.open("rb") as fh:
            data = tomllib.load(fh)
    except tomllib.TOMLDecodeError:
        return None
    name = data.get("project", {}).get("name")
    if isinstance(name, str) and name.strip():
        return name.strip()
    return None


def _check_pyproject_section(project_root: Path) -> str | None:
    """If pyproject.toml already declares ``[tool.run-site]`` we
    can't *prevent* both from existing, but we can warn — runsite.toml at
    the project root takes precedence."""

    pyproj = project_root / "pyproject.toml"
    if not pyproj.is_file():
        return None
    try:
        with pyproj.open("rb") as fh:
            data = tomllib.load(fh)
    except tomllib.TOMLDecodeError:
        return None
    if "tool" in data and "run-site" in data["tool"]:
        return (
            "pyproject.toml already has [tool.run-site]. "
            "runsite.toml takes precedence; you may want to remove the "
            "pyproject section to avoid drift."
        )
    return None


def _pick_slug(
    *,
    django_module: str | None,
    pyproject_name: str | None,
    project_root: Path,
) -> str:
    for raw in (django_module, pyproject_name, project_root.name):
        if not raw:
            continue
        slug = _sanitize_slug(raw)
        if slug:
            return slug
    return "djangoproject"


def _sanitize_slug(raw: str) -> str:
    slug = INVALID_SLUG_CHARS.sub("_", raw).strip("._-")
    if not slug:
        return ""
    if not SLUG_RE.fullmatch(slug):
        return ""
    return slug


# ---------------------------------------------------------------------------
# TOML rendering
# ---------------------------------------------------------------------------


def _render_toml(detected: DetectedDefaults, *, with_celery: bool) -> str:
    """Hand-write the TOML so the file is readable and well-commented.

    We don't take a TOML-writer dependency for a single greenfield config —
    the format here is small and stable.
    """

    slug = detected.project_slug
    lines: list[str] = []
    lines.append(
        "# Generated by `run-site init`. Edit freely; "
        "`run-site doctor` validates the result."
    )
    lines.append("")
    lines.append(f'project_slug = "{slug}"')
    lines.append(f'manage_py = "{detected.manage_py_rel}"')
    lines.append("")
    lines.append("[python]")
    if detected.has_uv:
        lines.append("# Run manage.py through `uv run` so deps are resolved against")
        lines.append("# pyproject.toml / uv.lock automatically. --no-sync skips the")
        lines.append("# implicit sync on every invocation; run-site already runs")
        lines.append("# `uv sync` once during venv setup.")
        rendered = ", ".join(f'"{token}"' for token in DEFAULT_UV_COMMAND)
        lines.append(f"command = [{rendered}]")
    else:
        lines.append('# "auto" picks .venv/, $VIRTUAL_ENV, RUN_SITE_PYTHON, or `uv run python`.')
        lines.append('# Switch to `command = ["uv", "run", "--no-sync", "python"]` once uv is installed.')
        lines.append('executable = "auto"')
    lines.append("")
    lines.append("[postgres]")
    lines.append('image = "postgres:16"')
    lines.append(f'user = "{slug}"')
    lines.append('password = "password"')
    lines.append(f'db = "{slug}"')
    lines.append("")
    lines.append("[redis]")
    lines.append('image = "redis:7-alpine"')
    lines.append("")
    lines.append("[django]")
    lines.append('runserver_bind = "127.0.0.1"')
    lines.append('runserver_display_host = "localhost"')
    lines.append('browser_probe_path = "/admin/login/"')
    lines.append("migrate = true")
    lines.append("")
    lines.append("[superuser]")
    lines.append("enabled = true")
    lines.append('username = "admin"')
    lines.append('password = "admin"')
    lines.append('email = "admin@example.com"')
    lines.append("")
    lines.append("[env]")
    lines.append("# Names of the env vars your settings.py reads. The orchestrator")
    lines.append("# will export these with values that point at the testcontainers.")
    lines.append('database_url = "DATABASE_URL"')
    lines.append('redis_url = "REDIS_URL"')

    if with_celery and detected.celery_app:
        lines.append("")
        lines.append("[celery]")
        lines.append(f'app = "{detected.celery_app}"')
        lines.append("enabled = true")
        lines.append('worker_pool = "solo"          # safe default on macOS')
        lines.append('worker_log_level = "info"')
        lines.append("with_beat = false")

    lines.append("")
    return "\n".join(lines)
