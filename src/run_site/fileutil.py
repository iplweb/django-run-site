"""Small filesystem helpers shared across run-site."""

from __future__ import annotations

import os
from pathlib import Path


def write_private_text(path: Path, text: str) -> None:
    """Write *text* to *path* with owner-only (``0o600``) permissions.

    Used for files that hold plaintext secrets — the ``.run-site-env.sh``
    export and the ``.run-site-config`` sidecar both record the DB password
    (and the env file the Django secret key).

    The ``O_CREAT`` mode defeats the process umask when creating a new file;
    the ``fchmod`` also re-secures a looser-mode file left behind by an older
    run. ``fchmod`` is POSIX-only — on Windows the create mode plus the
    platform's default ACLs are the best we can do.
    """

    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
        handle = os.fdopen(fd, "w", encoding="utf-8")
    except BaseException:
        os.close(fd)
        raise
    with handle:
        handle.write(text)
