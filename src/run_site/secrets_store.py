"""Per-project secret storage — currently just ``SECRET_KEY``.

The CLI auto-generates a Django ``SECRET_KEY`` on first run and persists
it under ``<project_root>/.run-site/secret_key`` (chmod 0600). On
subsequent runs we read the existing value, so sessions, password reset
tokens, and any other ``SECRET_KEY``-derived material stay valid across
restarts — including across ``--reuse`` toggles.

The file lives next to the persistent SQLite path (same ``.run-site/``
directory), so the same gitignore warning already shown for SQLite covers
the secret file too. The file is plain text, single line, no trailing
newline — minimal surface for parsers.
"""

from __future__ import annotations

import contextlib
import logging
import os
import secrets
from pathlib import Path

from run_site.sqlite import PERSISTENT_DIR_NAME

logger = logging.getLogger(__name__)

SECRET_KEY_FILENAME = "secret_key"
_FILE_MODE = 0o600
_DIR_MODE = 0o700


def secret_key_path(project_root: Path) -> Path:
    """Where the persisted SECRET_KEY lives for *project_root*."""

    return (project_root / PERSISTENT_DIR_NAME / SECRET_KEY_FILENAME).resolve()


def _generate() -> str:
    return secrets.token_urlsafe(50)


def load_or_generate_secret_key(project_root: Path) -> str:
    """Return the project's persisted SECRET_KEY, generating one if absent.

    Reads ``<project_root>/.run-site/secret_key`` if present and
    non-empty. Otherwise generates a fresh URL-safe token, writes it
    with mode 0600 (creating the parent ``.run-site/`` directory with
    mode 0700 if needed), and returns it.

    Permission errors on read are non-fatal — we fall back to generating
    a fresh value for this run and warn. Permission errors on write are
    also non-fatal — we return the fresh value, the user just won't get
    persistence (their sessions will reset on the next run, matching the
    pre-feature behavior).
    """

    path = secret_key_path(project_root)
    existing = _read_existing(path)
    if existing is not None:
        return existing

    value = _generate()
    _write_new(path, value)
    return value


def _read_existing(path: Path) -> str | None:
    """Read an existing secret file; return ``None`` if absent or empty."""

    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError:
        logger.warning(
            "Could not read existing SECRET_KEY at %s; will generate a fresh one.",
            path,
        )
        return None
    stripped = text.strip()
    return stripped or None


def _write_new(path: Path, value: str) -> None:
    """Persist *value* to *path* with restrictive permissions.

    Best-effort — failures are logged but do not raise: the caller still
    gets a usable in-memory secret for this run.
    """

    try:
        parent = path.parent
        parent.mkdir(parents=True, exist_ok=True)
        # Tighten permissions on the directory. Filesystems that don't
        # support chmod (e.g. some Windows mounts) silently no-op.
        with contextlib.suppress(OSError):
            os.chmod(parent, _DIR_MODE)

        # Write atomically: temp file + rename, so a crashed write never
        # leaves a half-written secret on disk.
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(value, encoding="utf-8")
        with contextlib.suppress(OSError):
            os.chmod(tmp, _FILE_MODE)
        os.replace(tmp, path)
    except OSError:
        logger.warning(
            "Could not persist SECRET_KEY to %s; using an in-memory value for this run.",
            path,
        )
