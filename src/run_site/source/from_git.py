"""Clone or update a Git repository for ``--from-git``.

Two notions of "ownership" govern destructive operations:

- **cache-owned** (``~/.cache/run-site/checkouts/<slug>/``): the CLI
  considers itself the sole owner. We can ``git reset --hard`` freely.
- **user-owned** (any other path, e.g. ``--checkout-path ~/projects/foo``):
  the user might have local changes. Default: refuse to discard. Require
  explicit ``--force-reset`` to do destructive ops.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from run_site.errors import SourceError

CACHE_ROOT = Path.home() / ".cache" / "run-site" / "checkouts"
SLUG_RE = re.compile(r"(?:[/:])([^/:]+/[^/]+?)(?:\.git)?/?$")
SLUG_SAFE_RE = re.compile(r"[^A-Za-z0-9_/-]")


@dataclass(frozen=True)
class GitSource:
    """Result of resolving a ``--from-git`` invocation."""

    url: str
    checkout_path: Path
    ref_kind: str  # "branch" | "tag" | "commit" | "head"
    ref: str | None
    cache_owned: bool
    cleanup_on_exit: bool


def extract_slug(url: str) -> str:
    """Derive a stable, filesystem-safe slug from a git URL.

    Examples:
      ``https://github.com/iplweb/bpp.git`` → ``iplweb/bpp``
      ``git@github.com:foo/bar.git``        → ``foo/bar``
      ``https://gitlab.com/group/sub/proj`` → ``sub/proj``
    """

    match = SLUG_RE.search(url)
    if match is not None:
        candidate = match.group(1)
        return SLUG_SAFE_RE.sub("_", candidate)
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:12]


def resolve_checkout_dir(
    url: str,
    *,
    explicit_checkout_path: str | None,
    no_cache: bool,
) -> tuple[Path, bool, bool]:
    """Return (checkout_dir, cache_owned, cleanup_on_exit) for a git URL."""

    if explicit_checkout_path is not None:
        path = Path(explicit_checkout_path).expanduser().resolve()
        cache_owned = _is_cache_path(path)
        return path, cache_owned, False
    if no_cache:
        # Use a tempdir; cleanup_on_exit=True so the run flow can rm-rf it.
        tmp = Path(tempfile.mkdtemp(prefix="run-site-"))
        return tmp, False, True
    slug = extract_slug(url)
    return CACHE_ROOT / slug, True, False


def _is_cache_path(path: Path) -> bool:
    try:
        path.resolve().relative_to(CACHE_ROOT.resolve())
    except (ValueError, OSError):
        return False
    return True


def resolve_git_source(
    *,
    url: str,
    branch: str | None,
    tag: str | None,
    commit: str | None,
    checkout_path: str | None,
    no_cache: bool,
    no_pull: bool,
    force_reset: bool,
    yes: bool,
    runner: GitRunner | None = None,
    confirm: ConfirmFn | None = None,
) -> GitSource:
    """Clone (or update) the repo and return a :class:`GitSource`.

    Refuses to clone in non-TTY contexts unless ``yes=True``.
    """

    refs = [("branch", branch), ("tag", tag), ("commit", commit)]
    set_refs = [(k, v) for k, v in refs if v is not None]
    if len(set_refs) > 1:
        raise SourceError(
            f"Multiple refs specified: {[k for k, _ in set_refs]}; pick one of "
            "--branch / --tag / --commit"
        )
    ref_kind = set_refs[0][0] if set_refs else "head"
    ref_value = set_refs[0][1] if set_refs else None

    runner = runner or RealGitRunner()

    checkout_dir, cache_owned, cleanup_on_exit = resolve_checkout_dir(
        url=url, explicit_checkout_path=checkout_path, no_cache=no_cache
    )

    if not yes:
        confirm = confirm or _stdin_confirm
        if not confirm(url=url, checkout_path=checkout_dir):
            raise SourceError("Cloning aborted by user. Pass --yes to skip the prompt.")

    checkout_dir.parent.mkdir(parents=True, exist_ok=True)

    if not checkout_dir.exists():
        runner.run(["git", "clone", url, str(checkout_dir)])
    elif no_pull:
        if not (checkout_dir / ".git").exists():
            raise SourceError(f"{checkout_dir} exists but is not a git checkout.")
    else:
        _update_checkout(
            runner=runner,
            checkout_dir=checkout_dir,
            ref_kind=ref_kind,
            ref_value=ref_value,
            cache_owned=cache_owned,
            force_reset=force_reset,
        )

    _checkout_ref(
        runner=runner,
        checkout_dir=checkout_dir,
        ref_kind=ref_kind,
        ref_value=ref_value,
        no_pull=no_pull,
    )

    return GitSource(
        url=url,
        checkout_path=checkout_dir,
        ref_kind=ref_kind,
        ref=ref_value,
        cache_owned=cache_owned,
        cleanup_on_exit=cleanup_on_exit,
    )


def _update_checkout(
    *,
    runner: GitRunner,
    checkout_dir: Path,
    ref_kind: str,
    ref_value: str | None,
    cache_owned: bool,
    force_reset: bool,
) -> None:
    if cache_owned or force_reset:
        runner.run(["git", "-C", str(checkout_dir), "fetch", "--all", "--prune"])
        if ref_kind == "branch" and ref_value is not None:
            runner.run(["git", "-C", str(checkout_dir), "reset", "--hard", f"origin/{ref_value}"])
        elif (ref_kind == "tag" and ref_value is not None) or (
            ref_kind == "commit" and ref_value is not None
        ):
            runner.run(["git", "-C", str(checkout_dir), "reset", "--hard", ref_value])
        return

    # User-owned: paranoid mode.
    status = runner.capture(["git", "-C", str(checkout_dir), "status", "--porcelain"])
    if status.strip():
        raise SourceError(
            f"User-owned checkout {checkout_dir} has uncommitted changes. "
            "Use --no-pull to keep them, or --force-reset to discard them."
        )
    runner.run(["git", "-C", str(checkout_dir), "fetch", "--all", "--prune"])
    if ref_kind == "branch" and ref_value is not None:
        runner.run(["git", "-C", str(checkout_dir), "merge", "--ff-only", f"origin/{ref_value}"])


def _checkout_ref(
    *,
    runner: GitRunner,
    checkout_dir: Path,
    ref_kind: str,
    ref_value: str | None,
    no_pull: bool,
) -> None:
    if ref_kind == "branch" and ref_value is not None:
        runner.run(["git", "-C", str(checkout_dir), "checkout", ref_value])
        if not no_pull:
            runner.run(["git", "-C", str(checkout_dir), "pull", "--ff-only", "origin", ref_value])
    elif ref_kind == "tag" and ref_value is not None:
        runner.run(["git", "-C", str(checkout_dir), "checkout", f"tags/{ref_value}"])
    elif ref_kind == "commit" and ref_value is not None:
        runner.run(["git", "-C", str(checkout_dir), "checkout", ref_value])
    # ref_kind == "head": leave whatever clone/pull picked.


def cleanup_temp_checkout(source: GitSource) -> None:
    """Remove a tempdir checkout when ``--no-cache`` was used."""

    if source.cleanup_on_exit and source.checkout_path.exists():
        shutil.rmtree(source.checkout_path, ignore_errors=True)


# ---------------------------------------------------------------------------
# Runner abstraction (testable without git)
# ---------------------------------------------------------------------------


class GitRunner:
    """Protocol for executing git commands. Tests substitute a recorder."""

    def run(self, argv: Sequence[str]) -> None:
        raise NotImplementedError

    def capture(self, argv: Sequence[str]) -> str:
        raise NotImplementedError


class RealGitRunner(GitRunner):
    def run(self, argv: Sequence[str]) -> None:
        if shutil.which("git") is None:
            raise SourceError("`git` not found on PATH; cannot use --from-git.")
        proc = subprocess.run(list(argv), check=False, text=True, capture_output=True)
        if proc.returncode != 0:
            raise SourceError(
                f"git command failed (exit {proc.returncode}): {' '.join(argv)}\n"
                f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
            )

    def capture(self, argv: Sequence[str]) -> str:
        if shutil.which("git") is None:
            raise SourceError("`git` not found on PATH; cannot use --from-git.")
        proc = subprocess.run(list(argv), check=False, text=True, capture_output=True)
        if proc.returncode != 0:
            raise SourceError(
                f"git command failed (exit {proc.returncode}): {' '.join(argv)}\n"
                f"stderr:\n{proc.stderr}"
            )
        return proc.stdout


# ---------------------------------------------------------------------------
# Confirmation
# ---------------------------------------------------------------------------


from typing import Protocol  # noqa: E402


class ConfirmFn(Protocol):
    def __call__(self, *, url: str, checkout_path: Path) -> bool: ...


def _stdin_confirm(*, url: str, checkout_path: Path) -> bool:
    if not sys.stdin.isatty():
        raise SourceError("--from-git in non-interactive context: pass --yes to confirm.")
    sys.stderr.write(f"Clone {url} to {checkout_path}? [y/N] (defaults to no in 10s) ")
    sys.stderr.flush()
    answer = _read_with_timeout(10.0)
    return answer.strip().lower() in ("y", "yes")


def _read_with_timeout(timeout: float) -> str:
    """Read a single line from stdin with a timeout. Returns empty string
    on timeout."""

    if os.name != "posix":  # pragma: no cover - POSIX-only helper
        return input() if not _stdin_closed() else ""
    import select

    rlist, _, _ = select.select([sys.stdin], [], [], timeout)
    if rlist:
        return sys.stdin.readline()
    return ""


def _stdin_closed() -> bool:
    try:
        return sys.stdin.closed
    except Exception:
        return True
