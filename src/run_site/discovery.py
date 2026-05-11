"""Project root, ``manage.py``, and local Python resolution.

The CLI never imports Django, so a "Python interpreter" here is whatever
will execute ``manage.py`` as a subprocess. It may be a single executable
path or a multi-token command prefix like ``["uv", "run", "python"]``.
"""

from __future__ import annotations

import ast
import os
import re
import shutil
import sys
from collections.abc import Sequence
from pathlib import Path

from run_site.config import DetectedServices, RunSiteConfig
from run_site.errors import DiscoveryError

# Well-known shallow paths checked first — covers full sites at the repo
# root and the ``src/`` layout.
PRIORITY_MANAGE_PY_PATHS: tuple[str, ...] = ("manage.py", "src/manage.py")

# Directories ignored during the wider auto-scan. We deliberately don't
# walk into them: real manage.py files live in source dirs, never in
# build artifacts, caches, or vendored deps.
EXCLUDED_AUTOSCAN_DIRS: frozenset[str] = frozenset(
    {
        "__pycache__",
        "node_modules",
        "build",
        "dist",
        "docs",
        "static",
        "media",
        "egg-info",
        "site-packages",
        ".git",
        ".venv",
        "venv",
        "env",
        ".tox",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
    }
)


def discover_project_root(
    *,
    cli_root: Path | None,
    config_root: Path | None,
    cwd: Path,
) -> Path:
    """Resolve the project root.

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
    """Resolve absolute path to ``manage.py``.

    Priority:

    1. ``--manage-py`` (CLI). Relative paths anchor to the project root,
       not CWD — so ``--from-git`` users can pass ``test_project/manage.py``.
    2. ``manage_py = ...`` from config (also relative to project root).
    3. Auto-scan: try the well-known ``manage.py`` / ``src/manage.py``
       first, then fall back to a depth-limited search. When several
       ``manage.py`` files exist (common in Django *packages* that ship a
       test project), filter to the ones that actually ``import django``;
       error with a candidate list if ambiguity remains.
    """

    if cli_manage is not None:
        path = _resolve_against_project_root(cli_manage, config.project_root)
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

    candidates = autoscan_manage_py(config.project_root)
    if not candidates:
        raise DiscoveryError(
            f"Could not find manage.py in {config.project_root}. "
            "Set --manage-py or 'manage_py' in runsite.toml."
        )
    return _pick_one_manage_py(candidates, project_root=config.project_root)


def autoscan_manage_py(project_root: Path) -> list[Path]:
    """Return ``manage.py`` candidates under *project_root*, sorted by
    priority (shallowest, alphabetical). Excludes obvious noise dirs.

    Caller decides what to do with the list — :func:`discover_manage_py`
    fails on ambiguity, while ``run-site init`` reports the chosen one
    to the user.
    """

    if not project_root.is_dir():
        return []

    # 1. Well-known shallow paths.
    for relpath in PRIORITY_MANAGE_PY_PATHS:
        path = project_root / relpath
        if path.is_file():
            return [path.resolve()]

    candidates: list[Path] = []

    # 2. One level deep: ``<root>/<dir>/manage.py``.
    for entry in sorted(project_root.iterdir()):
        if not _autoscan_dir_ok(entry):
            continue
        m = entry / "manage.py"
        if m.is_file():
            candidates.append(m.resolve())

    if candidates:
        return candidates

    # 3. Two levels deep: ``<root>/<dir>/<sub>/manage.py``.
    for top in sorted(project_root.iterdir()):
        if not _autoscan_dir_ok(top):
            continue
        for sub in sorted(top.iterdir()):
            if not _autoscan_dir_ok(sub):
                continue
            m = sub / "manage.py"
            if m.is_file():
                candidates.append(m.resolve())

    return candidates


def imports_django(path: Path) -> bool:
    """True if *path* parses as Python and references ``django`` at all
    (either ``import django.x`` or ``from django.x import ...``).

    A real Django ``manage.py`` always imports
    ``django.core.management``; a same-named script that just happens to
    sit at the right path won't. AST-based to avoid string-matching
    false positives (comments, docstrings).
    """

    try:
        source = path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source)
    except (OSError, SyntaxError):
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "django" or alias.name.startswith("django."):
                    return True
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod == "django" or mod.startswith("django."):
                return True
    return False


def _abs_no_symlinks(path: Path) -> Path:
    """Return *path* as an absolute, normalized path **without resolving
    symlinks**.

    Critical for venv ``bin/python`` symlinks: ``Path.resolve()`` walks
    the chain to the upstream CPython binary, but Python detects venv
    membership from ``sys.executable``'s *directory* — only when invoked
    via the venv symlink does it find the venv's ``pyvenv.cfg`` and put
    the venv's ``site-packages`` on ``sys.path``. Hand it the resolved
    target and you silently get the bare interpreter, which is why
    ``import django`` blows up even though the venv has Django installed.

    ``os.path.abspath`` does what we need: prepend CWD if relative,
    collapse ``..`` / ``.``, but leave symlinks alone.
    """

    return Path(os.path.abspath(str(path)))


def _autoscan_dir_ok(path: Path) -> bool:
    return (
        path.is_dir() and not path.name.startswith(".") and path.name not in EXCLUDED_AUTOSCAN_DIRS
    )


def _pick_one_manage_py(candidates: list[Path], *, project_root: Path) -> Path:
    """Resolve a candidate list to a single ``manage.py``.

    With multiple candidates we trust ``imports_django`` to separate
    real Django entrypoints from same-named scripts. If that still
    leaves more than one, we refuse to guess and ask the user to pick.
    """

    if len(candidates) == 1:
        return candidates[0]

    legit = [c for c in candidates if imports_django(c)]
    if len(legit) == 1:
        return legit[0]
    chosen = legit if legit else candidates
    rels = ", ".join(str(c.relative_to(project_root)) for c in chosen)
    label = "Django manage.py files" if legit else "manage.py files"
    raise DiscoveryError(
        f"Multiple {label} found under {project_root}: {rels}. "
        "Pass --manage-py or set 'manage_py' in runsite.toml to disambiguate."
    )


def _resolve_against_project_root(path: Path, project_root: Path) -> Path:
    """Anchor a possibly-relative CLI path to the project root.

    Absolute paths and ``~``-prefixed paths are returned untouched
    (after expansion). Bare relative paths anchor to *project_root* —
    that's the right base for ``--from-git`` (where CWD is unrelated to
    the cloned source) and equivalent to the old behavior for runs in
    your own checkout (where CWD ≈ project_root).

    Uses :func:`_abs_no_symlinks` so venv ``bin/python`` symlinks aren't
    walked (see that function for the gory details).
    """

    p = Path(path).expanduser()
    if p.is_absolute():
        return _abs_no_symlinks(p)
    return _abs_no_symlinks(project_root / p)


def discover_local_python(
    *,
    cli_python: Path | None,
    config: RunSiteConfig,
    env: dict[str, str] | None = None,
) -> tuple[str, ...]:
    """Resolve the local Python *command* as a tuple of arguments.

    The result is suitable for use as a subprocess argv prefix:
    ``[*python, manage_py_path, "migrate"]``.
    """

    project_root = config.project_root
    env = dict(env if env is not None else os.environ)

    # 1. CLI flag wins. Relative paths anchor to project_root (same
    # rationale as --manage-py: CWD is meaningless under --from-git).
    if cli_python is not None:
        path = _resolve_against_project_root(cli_python, project_root)
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
        if not path.is_absolute():
            path = project_root / path
        path = _abs_no_symlinks(path)
        if not path.is_file():
            raise DiscoveryError(
                f"[python].executable={config.python.executable!r} does not exist "
                f"(resolved to {path})"
            )
        return (str(path),)

    # 4-8. "auto" fallback chain.
    return _auto_python_chain(project_root, env)


def _auto_python_chain(project_root: Path, env: dict[str, str]) -> tuple[str, ...]:
    """Resolve a Python interpreter for *project_root*.

    Order:

    1. ``RUN_SITE_PYTHON`` env — explicit override.
    2. ``<project_root>/.venv/bin/python`` — the project's own venv.
       Tried *before* ``$VIRTUAL_ENV`` because run-site invoked through
       ``uv tool run`` / ``pipx run`` inherits the wrapper's
       ``VIRTUAL_ENV`` pointing at the *tool's* venv (which has no
       Django). Preferring the project venv prevents that mix-up.
    3. ``$VIRTUAL_ENV/bin/python`` — ambient venv (``source .../activate``).
    4. ``uv run python`` — only with ``uv.lock`` + ``uv`` on PATH.
    5. ``sys.executable`` — last resort.
    """

    # 1. RUN_SITE_PYTHON env.
    run_site_python = env.get("RUN_SITE_PYTHON")
    if run_site_python:
        path = _abs_no_symlinks(Path(run_site_python).expanduser())
        if path.is_file():
            return (str(path),)

    # 2. .venv/bin/python in project root — preferred over $VIRTUAL_ENV.
    candidate = project_root / ".venv" / "bin" / "python"
    if candidate.is_file():
        return (str(_abs_no_symlinks(candidate)),)

    # 3. $VIRTUAL_ENV/bin/python — ambient venv as a fallback.
    virtual_env = env.get("VIRTUAL_ENV")
    if virtual_env:
        candidate = Path(virtual_env) / "bin" / "python"
        if candidate.is_file():
            return (str(_abs_no_symlinks(candidate)),)

    # 4. uv run python — only if uv.lock is present and uv is in PATH.
    if (project_root / "uv.lock").is_file() and shutil.which("uv"):
        return ("uv", "run", "python")

    # 5. sys.executable fallback.
    return (sys.executable,)


def _resolve_command(command: Sequence[str]) -> tuple[str, ...]:
    """Resolve the first token via ``shutil.which`` if it's not a path."""

    if not command:
        raise DiscoveryError("[python].command is empty")
    head, *tail = command
    if "/" in head or os.sep in head:
        path = _abs_no_symlinks(Path(head).expanduser())
        if not path.is_file():
            raise DiscoveryError(f"[python].command[0] does not exist: {path}")
        return (str(path), *tail)
    resolved = shutil.which(head)
    if resolved is None:
        raise DiscoveryError(f"[python].command[0]={head!r} not found on PATH")
    return (resolved, *tail)


# ---------------------------------------------------------------------------
# Settings module discovery + service detection
# ---------------------------------------------------------------------------

# ``os.environ.setdefault('DJANGO_SETTINGS_MODULE', '<value>')`` is the
# canonical manage.py shape. We also accept ``os.environ[...] = '...'`` for
# projects that override it unconditionally.
_SETTINGS_MODULE_RE = re.compile(
    r"""DJANGO_SETTINGS_MODULE["']?\s*,?\s*["']([\w.]+)["']""",
    re.DOTALL,
)


def discover_settings_module(
    *,
    manage_py: Path,
    env: dict[str, str] | None = None,
) -> str | None:
    """Resolve the dotted Django settings module for the project.

    Order: ``DJANGO_SETTINGS_MODULE`` env var (if exported in the shell)
    > the literal string passed to ``os.environ.setdefault`` in
    ``manage.py``. Returns ``None`` when we can't find one — caller
    should treat detection as best-effort and fall back to safe
    defaults.
    """

    env = env if env is not None else dict(os.environ)
    from_env = env.get("DJANGO_SETTINGS_MODULE")
    if from_env:
        return from_env
    try:
        source = manage_py.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    m = _SETTINGS_MODULE_RE.search(source)
    if m is None:
        return None
    return m.group(1)


def _settings_module_to_paths(
    *,
    module: str,
    project_root: Path,
    manage_py: Path,
) -> list[Path]:
    """Convert a dotted module name into one or more candidate file paths.

    Django settings can live either as ``<pkg>/settings.py`` (single
    file) or ``<pkg>/settings/__init__.py`` + ``base.py`` / ``dev.py``
    (package). We search relative to *both* the project root and
    ``manage.py.parent`` because ``src/`` layouts put the package under
    ``src/<pkg>/settings.py`` even though the project root is the repo.
    """

    parts = module.split(".")
    bases = [project_root, manage_py.parent]
    seen: set[Path] = set()
    out: list[Path] = []
    for base in bases:
        single = base.joinpath(*parts[:-1], parts[-1] + ".py")
        pkg_init = base.joinpath(*parts, "__init__.py")
        for candidate in (single, pkg_init):
            try:
                resolved = candidate.resolve()
            except OSError:
                continue
            if resolved in seen:
                continue
            seen.add(resolved)
            if candidate.is_file():
                out.append(candidate)
    return out


# Tokens that indicate the project uses each service. Conservative —
# false positives here cause us to manage a container the user doesn't
# actually need (annoying), so we err on the side of explicit evidence.
_POSTGRES_TOKENS = (
    "django.db.backends.postgresql",
    "django.db.backends.postgresql_psycopg2",
    "postgres://",
    "postgresql://",
    "postgres+",  # postgres+psycopg etc — dj-database-url style
)
_SQLITE_TOKENS = (
    "django.db.backends.sqlite3",
    "sqlite:///",
    "sqlite+",  # sqlite+pysqlite
)
_REDIS_TOKENS = (
    "django_redis",
    "django.core.cache.backends.redis",
    "RedisCache",
    "redis://",
    "rediss://",
    "CELERY_BROKER_URL",
    "CELERY_RESULT_BACKEND",
)


def _scan_tokens(text: str, tokens: Sequence[str]) -> bool:
    return any(tok in text for tok in tokens)


def detect_services_from_settings(
    *,
    manage_py: Path,
    project_root: Path,
    env: dict[str, str] | None = None,
    max_followed: int = 4,
) -> DetectedServices | None:
    """Static-scan the project's settings module(s) for service usage.

    Returns flags for Postgres / SQLite / Redis, or ``None`` if we can't
    locate any settings file at all. The scan reads the settings module
    file's text plus any sibling modules referenced by ``from .x import``
    statements one level deep — that's enough to cover the common
    ``settings/__init__.py`` + ``base.py`` split without resolving
    arbitrary Python imports (which would require executing the code).
    """

    module = discover_settings_module(manage_py=manage_py, env=env)
    if module is None:
        return None
    seeds = _settings_module_to_paths(module=module, project_root=project_root, manage_py=manage_py)
    if not seeds:
        return None

    visited: set[Path] = set()
    blob_parts: list[str] = []
    queue: list[Path] = list(seeds)
    while queue and len(visited) < max_followed:
        path = queue.pop(0)
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if resolved in visited:
            continue
        visited.add(resolved)
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        blob_parts.append(text)
        # Follow ``from .<name> import ...`` siblings one level deep.
        for sibling in _relative_sibling_imports(text, path.parent):
            if sibling.is_file() and sibling.resolve() not in visited:
                queue.append(sibling)

    blob = "\n".join(blob_parts)
    return DetectedServices(
        postgres=_scan_tokens(blob, _POSTGRES_TOKENS),
        sqlite=_scan_tokens(blob, _SQLITE_TOKENS),
        redis=_scan_tokens(blob, _REDIS_TOKENS),
    )


_RELATIVE_IMPORT_RE = re.compile(
    r"^\s*from\s+\.(\w+)\s+import\s+",
    re.MULTILINE,
)


def _relative_sibling_imports(text: str, package_dir: Path) -> list[Path]:
    """Resolve ``from .X import …`` statements to sibling ``.py`` files.

    Best-effort — we only follow single-level relative imports
    (``from .base``), since deeper traversal needs an actual import
    machinery. ``from .. import …`` and ``from .pkg.sub import …`` are
    ignored.
    """

    out: list[Path] = []
    for m in _RELATIVE_IMPORT_RE.finditer(text):
        name = m.group(1)
        candidate = package_dir / f"{name}.py"
        out.append(candidate)
    return out


def get_ignored_patterns(gitignore_path: Path) -> set[str]:
    """Return the (stripped, comment-less) set of patterns in *gitignore_path*.

    Used by :mod:`run_site.sqlite` to warn when ``.run-site`` isn't
    ignored before we create a persistent SQLite file in it.
    """

    if not gitignore_path.is_file():
        return set()
    try:
        text = gitignore_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return set()
    out: set[str] = set()
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        out.add(stripped)
    return out
