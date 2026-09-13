"""Watch Django migration files and re-run ``migrate`` when they change.

runserver's autoreloader restarts the web process on code changes, but the
database schema stays wherever the startup ``migrate`` left it — a ``git
pull``, branch switch or ``makemigrations`` that brings new migrations leaves
the running site on a stale schema. :class:`MigrationWatcher` polls
``*/migrations/*.py`` under the project root (stdlib only, no Django import)
and fires a callback once a burst of changes has settled.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable
from pathlib import Path

logger = logging.getLogger(__name__)

# Never descended into (on top of hidden dirs such as .git / .venv and any
# directory holding a pyvenv.cfg): installed packages and JS deps. Their
# migrations don't change under a running dev server, and walking them on
# every poll is the expensive part.
_PRUNED_DIRS = frozenset({"node_modules", "__pycache__", "site-packages"})

# Relative POSIX path → (mtime_ns, size).
Snapshot = dict[str, tuple[int, int]]


def snapshot_migrations(root: Path) -> Snapshot:
    """Fingerprint every ``<app>/migrations/*.py`` file under *root*."""

    root_str = str(root)
    snapshot: Snapshot = {}
    for dirpath, dirnames, filenames in os.walk(root_str, onerror=_log_walk_error):
        if dirpath != root_str and "pyvenv.cfg" in filenames:
            dirnames[:] = []
            continue
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in _PRUNED_DIRS]
        if os.path.basename(dirpath) != "migrations":
            continue
        for name in filenames:
            if not name.endswith(".py"):
                continue
            path = os.path.join(dirpath, name)
            try:
                st = os.stat(path)
            except OSError as exc:
                # Typically removed between listing and stat (mid-checkout);
                # the next poll sees the settled state.
                logger.debug("skipping %s: %s", path, exc)
                continue
            rel = Path(os.path.relpath(path, root_str)).as_posix()
            snapshot[rel] = (st.st_mtime_ns, st.st_size)
    return snapshot


def _log_walk_error(exc: OSError) -> None:
    logger.debug("migration watcher cannot scan %s: %s", exc.filename, exc)


def changed_paths(old: Snapshot, new: Snapshot) -> list[str]:
    """Paths added, removed or modified between two snapshots, sorted."""

    modified = {path for path in old.keys() & new.keys() if old[path] != new[path]}
    return sorted((old.keys() ^ new.keys()) | modified)


class MigrationWatcher:
    """Poll migration files in a background thread and call *on_change* with
    the changed paths once they have stopped changing for *settle* seconds.

    The baseline snapshot is taken at construction, so changes landing
    between construction and :meth:`start` still fire. The callback runs on
    the watcher thread, so runs never overlap; changes made while it runs
    are picked up by the next poll.
    """

    def __init__(
        self,
        root: Path,
        on_change: Callable[[list[str]], None],
        *,
        interval: float = 1.0,
        settle: float = 1.0,
    ) -> None:
        self._root = root
        self._on_change = on_change
        self._interval = interval
        self._settle = settle
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._baseline = snapshot_migrations(root)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="migration-watcher", daemon=True)
        self._thread.start()

    def stop(self, timeout: float | None = None) -> None:
        """Stop polling and wait up to *timeout* for an in-flight callback."""

        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            current = snapshot_migrations(self._root)
            if current == self._baseline:
                continue
            settled = self._wait_until_settled(current)
            if settled is None:
                return
            changed = changed_paths(self._baseline, settled)
            self._baseline = settled
            if not changed:
                continue
            try:
                self._on_change(changed)
            except Exception:
                logger.exception("migration watcher callback failed; still watching")

    def _wait_until_settled(self, current: Snapshot) -> Snapshot | None:
        """Re-snapshot every *settle* seconds until two in a row match (so a
        checkout touching many files triggers one run). None if stopped."""

        while not self._stop.wait(self._settle):
            latest = snapshot_migrations(self._root)
            if latest == current:
                return latest
            current = latest
        return None
