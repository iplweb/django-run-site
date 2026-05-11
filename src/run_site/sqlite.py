"""Managed SQLite database lifecycle.

Mirrors the Postgres testcontainer flow conceptually:

* No ``--reuse`` — pick a path under ``tempfile.mkdtemp`` (ephemeral),
  delete on exit.
* ``--reuse`` — use ``<project_root>/.run-site/<slug>.sqlite3`` (or the
  explicit ``[sqlite].path`` override), create it if missing, never
  delete.

The path is plumbed into the env builder so the project's ``settings.py``
picks it up via the same ``[env]`` mapping it would use for a PG URL
(``database_url`` → ``sqlite:///<abspath>``).

The CLI never imports Django — we just touch the filesystem here.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from run_site.config import RunSiteConfig
from run_site.discovery import get_ignored_patterns

logger = logging.getLogger(__name__)

# Persistent SQLite (and any future per-project state) lives here, next
# to the project root so users find it on disk and can wipe it manually.
PERSISTENT_DIR_NAME = ".run-site"

# Acceptable matches for ".run-site" in a project .gitignore. We're
# generous: ``.run-site``, ``.run-site/``, ``/.run-site``, ``/.run-site/``,
# and wildcards like ``.run-site/*`` all count.
_GITIGNORE_MATCHES: frozenset[str] = frozenset(
    {
        ".run-site",
        ".run-site/",
        "/.run-site",
        "/.run-site/",
        ".run-site/*",
        "/.run-site/*",
        "/.run-site/**",
    }
)


@dataclass(frozen=True)
class SqliteState:
    """Result of :func:`prepare_sqlite`.

    Carries the absolute path used for the SQLite DB plus everything
    the shutdown code needs to clean up safely. ``ephemeral=True`` means
    the file lives under :attr:`tmpdir` (created by us) and the whole
    directory is removed on exit. ``ephemeral=False`` means the file
    persists across runs and ``tmpdir`` is ``None``.
    """

    path: Path
    ephemeral: bool
    tmpdir: Path | None
    # Was the persistent directory (``.run-site``) created by this run?
    # Used to decide whether to bother with the gitignore warning.
    created_persistent_dir: bool = False


def prepare_sqlite(
    *,
    config: RunSiteConfig,
    reuse: bool,
    force_reset: bool = False,
) -> SqliteState:
    """Pick the SQLite path and create the surrounding directory.

    With ``reuse=True`` we use a deterministic path under the project
    root and leave any existing file in place (unless *force_reset*).
    Without ``--reuse`` we create a fresh temp directory and put a
    brand-new file path inside.

    Caller is responsible for invoking :func:`cleanup_sqlite` at exit.
    """

    if not reuse:
        tmpdir = Path(tempfile.mkdtemp(prefix=f"runsite-{config.project_slug}-"))
        db_path = tmpdir / "db.sqlite3"
        return SqliteState(path=db_path, ephemeral=True, tmpdir=tmpdir)

    # Persistent mode.
    if config.sqlite.path:
        configured = Path(config.sqlite.path).expanduser()
        if configured.is_absolute():
            db_path = configured
        else:
            db_path = (config.project_root / configured).resolve()
        parent = db_path.parent
        created_dir = not parent.exists()
        parent.mkdir(parents=True, exist_ok=True)
    else:
        persistent_dir = config.project_root / PERSISTENT_DIR_NAME
        created_dir = not persistent_dir.exists()
        persistent_dir.mkdir(parents=True, exist_ok=True)
        db_path = persistent_dir / f"{config.project_slug}.sqlite3"

    if force_reset and db_path.is_file():
        db_path.unlink()

    return SqliteState(
        path=db_path,
        ephemeral=False,
        tmpdir=None,
        created_persistent_dir=created_dir,
    )


def cleanup_sqlite(state: SqliteState | None) -> None:
    """Remove the ephemeral tmp directory, if any. No-op otherwise."""

    if state is None or not state.ephemeral:
        return
    if state.tmpdir is None:
        return
    with suppress(FileNotFoundError):
        shutil.rmtree(state.tmpdir, ignore_errors=True)


def gitignore_warning(*, project_root: Path) -> str | None:
    """Return a warning string if ``.run-site`` isn't ignored.

    Called only when we're about to (or just did) create
    ``<project_root>/.run-site`` for a persistent SQLite file. The
    returned message tells the user what to add and why; ``None`` means
    they're already ignoring it (or there's no git repo here).
    """

    gitignore = project_root / ".gitignore"
    if not (project_root / ".git").exists() and not gitignore.exists():
        # Not a git project — no value in nagging.
        return None
    patterns = get_ignored_patterns(gitignore)
    if patterns & _GITIGNORE_MATCHES:
        return None
    if not gitignore.exists():
        return (
            f"No .gitignore found in {project_root}. "
            "run-site just created the .run-site/ directory for the "
            "persistent SQLite file — add '.run-site/' to .gitignore "
            "so the local DB doesn't end up in commits."
        )
    return (
        f".gitignore in {project_root} doesn't list '.run-site/'. "
        "run-site keeps the persistent SQLite file in that directory "
        "with --reuse; add '.run-site/' so it stays out of commits."
    )
