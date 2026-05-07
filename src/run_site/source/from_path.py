"""Resolve a Django project from a local filesystem path."""

from __future__ import annotations

from pathlib import Path

from run_site.errors import SourceError

PROJECT_MARKERS: tuple[str, ...] = ("pyproject.toml", "manage.py", "runsite.toml")


def resolve_path_source(path: str | Path) -> Path:
    """Validate and resolve a ``--from-path PATH`` source.

    Raises :class:`SourceError` if the path doesn't exist or doesn't look
    like a Django project root.
    """

    resolved = Path(path).expanduser().resolve()
    if not resolved.exists():
        raise SourceError(f"--from-path path does not exist: {resolved}")
    if not resolved.is_dir():
        raise SourceError(f"--from-path must be a directory; got file: {resolved}")

    for marker in PROJECT_MARKERS:
        if (resolved / marker).exists():
            return resolved
        if (resolved / "src" / marker).exists():
            return resolved
    raise SourceError(
        f"{resolved} doesn't look like a Django project root — expected one "
        f"of {list(PROJECT_MARKERS)} (or src/manage.py)."
    )
