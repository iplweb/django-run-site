"""Database dump format detection and restore strategies.

The actual restore is split between two phases of the run flow:

- ``init-script``: file is mounted into ``/docker-entrypoint-initdb.d/``
  *before* PG starts, so PG loads it as part of normal startup. Only works
  for plain ``.sql`` and only when the container is created fresh.
- ``post-start``: file is loaded after PG is up, via ``psql`` (plain or
  gzipped SQL) or ``pg_restore`` (any binary archive — custom, directory,
  or tar format, including directory dumps that were ``tar | gzip``-ed for
  transport).

The ``auto`` strategy picks the right one based on the dump's content and
whether the PG container was just created.

Format detection inspects the file's *magic bytes*, not just its name:
``pg_restore`` auto-detects the specific archive format itself, so run-site
only needs to decide which engine to use (``psql`` vs ``pg_restore``) and
how to peel any outer ``gzip``/``tar`` wrapper that ``pg_restore`` will not
strip on its own.
"""

from __future__ import annotations

import gzip
import logging
import os
import shutil
import subprocess
import tarfile
import tempfile
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from run_site.config import RunSiteConfig
from run_site.errors import DumpError

logger = logging.getLogger(__name__)

# (stream_name, line) — color is fixed at the call site so callers only
# need a 2-arg shim around ``mux.write``.
DumpProgressCallback = Callable[[str, str], None]


class DumpFormat(Enum):
    PLAIN_SQL = "plain_sql"
    GZIPPED_SQL = "gzipped_sql"
    # Any binary archive ``pg_restore`` can read — custom (``-Fc``),
    # directory (``-Fd``), or tar (``-Ft``). run-site does not distinguish
    # between them; ``pg_restore`` auto-detects the specific format.
    PG_RESTORE = "pg_restore"


SQL_SUFFIXES: tuple[str, ...] = (".sql",)
GZIP_SUFFIXES: tuple[str, ...] = (".sql.gz", ".gz")
CUSTOM_SUFFIXES: tuple[str, ...] = (".dump", ".pgdump", ".pg_dump")

# Magic bytes used to classify a dump by content rather than by filename.
_GZIP_MAGIC = b"\x1f\x8b"
_PGDUMP_MAGIC = b"PGDMP"  # first bytes of a custom dump / a directory toc.dat
_TAR_USTAR_OFFSET = 257  # POSIX/GNU tar puts "ustar" here in each header
_SNIFF_BYTES = 512  # one tar header block — enough to see the ustar magic


@dataclass(frozen=True)
class DumpPlan:
    """Resolved (file, format, strategy) tuple."""

    path: Path
    format: DumpFormat
    strategy: str  # "init-script" | "post-start" | "skip"
    reason: str | None = None


@dataclass(frozen=True)
class Pipe:
    """An N-stage shell-free pipeline: stages[i].stdout -> stages[i+1].stdin.

    Replaces the old ``("__pipe__", *left, *right)`` tuple encoding so a
    middle ``sed`` filter can sit between ``gunzip``/``pg_restore`` and
    ``psql``."""

    stages: tuple[Sequence[str], ...]


def _read_head(path: Path, n: int) -> bytes:
    with open(path, "rb") as fh:
        return fh.read(n)


def _gunzip_prefix(path: Path, n: int) -> bytes:
    """Decompress up to *n* bytes from a gzip file. Returns ``b""`` when the
    payload can't be read as gzip (truncated/garbage) — callers then treat
    the gzip-magic file as gzipped SQL rather than crashing."""

    try:
        with gzip.open(path, "rb") as fh:
            return fh.read(n)
    except (OSError, EOFError):
        # BadGzipFile is an OSError subclass; truncated test fixtures land
        # here too. A gzip file we can't peek into is assumed to be SQL.
        return b""


def _looks_like_pg_restore_archive(buf: bytes) -> bool:
    """True if *buf* is the start of a pg_dump archive (custom dump magic)
    or a tar header (a tarred directory-format dump)."""

    if buf[: len(_PGDUMP_MAGIC)] == _PGDUMP_MAGIC:
        return True
    return buf[_TAR_USTAR_OFFSET : _TAR_USTAR_OFFSET + 5] == b"ustar"


def detect_format(path: Path) -> DumpFormat:
    """Classify *path* by content (magic bytes), falling back to the file
    extension only when the content is inconclusive (e.g. empty fixtures).

    ``pg_restore`` auto-detects the specific archive format, so every binary
    archive — raw custom dump, or a directory dump that was ``tar``/``gzip``-ed
    — maps to a single :attr:`DumpFormat.PG_RESTORE`.
    """

    head = _read_head(path, _SNIFF_BYTES)
    if head[:2] == _GZIP_MAGIC:
        inner = _gunzip_prefix(path, _SNIFF_BYTES)
        if _looks_like_pg_restore_archive(inner):
            return DumpFormat.PG_RESTORE
        return DumpFormat.GZIPPED_SQL
    if _looks_like_pg_restore_archive(head):
        return DumpFormat.PG_RESTORE
    return _format_from_extension(path)


def _format_from_extension(path: Path) -> DumpFormat:
    """Last-resort classification when content sniffing is inconclusive."""

    name = path.name.lower()
    if name.endswith(".sql.gz"):
        return DumpFormat.GZIPPED_SQL
    if name.endswith(CUSTOM_SUFFIXES):
        return DumpFormat.PG_RESTORE
    if name.endswith(SQL_SUFFIXES):
        return DumpFormat.PLAIN_SQL
    if name.endswith(GZIP_SUFFIXES):
        # ``foo.gz`` without gzip magic — corrupt, but treat as gzipped SQL
        # pessimistically so the failure surfaces from psql, not detection.
        return DumpFormat.GZIPPED_SQL
    raise DumpError(
        f"Unsupported dump format: {path.name}. Expected .sql, .sql.gz, "
        ".dump, .pgdump, .pg_dump, or a .tar.gz/.tgz pg_dump archive."
    )


@contextmanager
def prepared_archive(path: Path) -> Iterator[Path]:
    """Yield a filesystem path that ``pg_restore`` can read directly.

    ``pg_restore`` reads a raw archive file (custom dump) or a directory
    (directory dump) on its own, but it will not strip an outer ``gzip``/
    ``tar`` wrapper. This unwraps that packaging:

    - raw archive file (no gzip, no tar) → yielded unchanged (no temp dir);
    - ``tar`` (optionally gzipped) wrapping a directory dump → extracted to a
      temp dir; the directory holding ``toc.dat`` is yielded;
    - gzipped single-file archive (e.g. a gzipped custom dump) → decompressed
      to a temp file, which is yielded.

    Any temp directory created is removed when the context exits.
    """

    head = _read_head(path, _SNIFF_BYTES)
    gzipped = head[:2] == _GZIP_MAGIC
    sample = _gunzip_prefix(path, _SNIFF_BYTES) if gzipped else head
    is_tar = sample[_TAR_USTAR_OFFSET : _TAR_USTAR_OFFSET + 5] == b"ustar"

    if not gzipped and not is_tar:
        # Raw single-file archive — pg_restore reads it as-is.
        yield path
        return

    tmp = Path(tempfile.mkdtemp(prefix="run-site-dump-"))
    try:
        if is_tar:
            _extract_tar(path, tmp, gzipped=gzipped)
            yield _find_archive_dir(tmp, source_name=path.name)
        else:
            # Gzipped single-file archive → decompress to a plain file.
            out = tmp / "dump"
            with gzip.open(path, "rb") as src, open(out, "wb") as dst:
                shutil.copyfileobj(src, dst)
            yield out
    finally:
        try:
            shutil.rmtree(tmp)
        except OSError:
            logger.warning("Failed to remove temp dump extraction dir %s", tmp, exc_info=True)


def _extract_tar(path: Path, dest: Path, *, gzipped: bool) -> None:
    # Literal modes (not a str variable) so the typed tarfile.open overloads
    # match — "r:gz" decompresses, "r:" reads an uncompressed tar. Branch the
    # ``with`` rather than the mode string to keep both mypy and ruff happy.
    if gzipped:
        with tarfile.open(path, "r:gz") as tar:
            _extract_all(tar, dest)
    else:
        with tarfile.open(path, "r:") as tar:
            _extract_all(tar, dest)


def _extract_all(tar: tarfile.TarFile, dest: Path) -> None:
    try:
        # ``filter="data"`` (Python 3.12+, backported to recent patch
        # releases) blocks path-traversal / device members during extract.
        tar.extractall(dest, filter="data")
    except TypeError:
        # Older interpreters lack the ``filter`` keyword; fall back to a plain
        # extract. Dumps are operator-supplied, trusted input.
        tar.extractall(dest)


def _find_archive_dir(root: Path, *, source_name: str) -> Path:
    """Return the directory under *root* that directly contains ``toc.dat``.

    Directory dumps are typically tarred under a single top-level folder, so
    the ``toc.dat`` lives one level down; search recursively to be robust.
    """

    for toc in root.rglob("toc.dat"):
        return toc.parent
    raise DumpError(
        f"No pg_dump archive found inside {source_name}: "
        "expected a directory-format dump containing toc.dat."
    )


def plan_dump(
    *,
    config: RunSiteConfig,
    cli_dump_path: Path | None,
    cli_no_dump: bool,
    cli_strategy_override: str | None,
    pg_created: bool,
) -> DumpPlan | None:
    """Resolve the (file, format, strategy) plan for the run, or None if no
    dump should be loaded.

    Resolution:
      - ``--no-dump`` → None.
      - ``--from-dump PATH`` overrides config.
      - ``[dump].default_path`` is the fallback.
      - If no path is set anywhere → None.
    """

    if cli_no_dump:
        return None

    raw_path: Path | None = None
    if cli_dump_path is not None:
        raw_path = cli_dump_path.expanduser()
    elif config.dump.default_path is not None:
        raw_path = (config.project_root / config.dump.default_path).resolve()
    if raw_path is None:
        return None

    if not raw_path.exists():
        raise DumpError(f"Dump file does not exist: {raw_path}")

    format_ = detect_format(raw_path)
    requested_strategy = cli_strategy_override or config.dump.strategy
    return _decide_strategy(
        path=raw_path,
        format_=format_,
        requested_strategy=requested_strategy,
        pg_created=pg_created,
    )


def _decide_strategy(
    *, path: Path, format_: DumpFormat, requested_strategy: str, pg_created: bool
) -> DumpPlan:
    if requested_strategy == "init-script":
        if not pg_created:
            raise DumpError(
                "dump.strategy='init-script' requires a freshly created PG "
                "container, but an existing one was reused."
            )
        if format_ is not DumpFormat.PLAIN_SQL:
            raise DumpError(f"dump.strategy='init-script' only supports .sql, got {path.name}.")
        return DumpPlan(path=path, format=format_, strategy="init-script")

    if requested_strategy == "post-start":
        return DumpPlan(path=path, format=format_, strategy="post-start")

    # "auto"
    if not pg_created:
        return DumpPlan(
            path=path,
            format=format_,
            strategy="skip",
            reason="PG container was reused; existing data preserved.",
        )
    if format_ is DumpFormat.PLAIN_SQL:
        return DumpPlan(path=path, format=format_, strategy="init-script")
    return DumpPlan(path=path, format=format_, strategy="post-start")


def resolve_restore_jobs(jobs: int | str) -> int:
    if isinstance(jobs, int):
        return jobs
    return min(8, os.cpu_count() or 1)


def build_post_start_argv(
    *,
    plan: DumpPlan,
    config: RunSiteConfig,
    pg_host: str,
    pg_port: int,
    container_id: str | None,
    restore_source: Path | None = None,
) -> list[Sequence[str] | Pipe]:
    """Return a list of argv tuples to execute, in order.

    For plain/gzipped dumps the only argv is ``psql`` (with ``gunzip`` piped
    via shell). For ``pg_restore`` archives we ``docker cp`` the dump into the
    container and then ``docker exec pg_restore`` — two argv tuples.

    ``restore_source`` is the unwrapped path to copy into the container (a
    file or a directory produced by :func:`prepared_archive`); it defaults to
    ``plan.path`` for archives that need no unwrapping.
    """

    base_env_args = [
        "-h",
        pg_host,
        "-p",
        str(pg_port),
        "-U",
        config.postgres.user,
        "-d",
        config.postgres.db,
    ]

    if plan.format is DumpFormat.PLAIN_SQL:
        psql = _require_tool("psql")
        return [
            (
                psql,
                *base_env_args,
                "-v",
                "ON_ERROR_STOP=1",
                "-f",
                str(plan.path),
            )
        ]

    if plan.format is DumpFormat.GZIPPED_SQL:
        psql = _require_tool("psql")
        psql_argv = (psql, *base_env_args, "-v", "ON_ERROR_STOP=1")
        return [Pipe(stages=(("gunzip", "-c", str(plan.path)), psql_argv))]

    if plan.format is DumpFormat.PG_RESTORE:
        if container_id is None:
            raise DumpError(
                "pg_restore-format dumps require a known container id "
                "(docker cp + pg_restore inside the container)."
            )
        docker = _require_tool("docker")
        jobs = resolve_restore_jobs(config.dump.restore_jobs)
        source = restore_source if restore_source is not None else plan.path
        return [
            (docker, "cp", str(source), f"{container_id}:/tmp/dump"),
            (
                docker,
                "exec",
                "-e",
                f"PGPASSWORD={config.postgres.password}",
                container_id,
                "pg_restore",
                "--no-owner",
                "--exit-on-error",
                "-j",
                str(jobs),
                "-h",
                "127.0.0.1",
                "-U",
                config.postgres.user,
                "-d",
                config.postgres.db,
                "/tmp/dump",
            ),
        ]

    raise DumpError(f"Unhandled dump format: {plan.format}")


def execute_post_start(
    plan: DumpPlan,
    *,
    config: RunSiteConfig,
    pg_host: str,
    pg_port: int,
    container_id: str | None,
    env_overlay: dict[str, str] | None = None,
    progress: DumpProgressCallback | None = None,
) -> None:
    """Run the post-start restore commands. Fails fast on first error
    when ``config.dump.fail_fast=True``.

    ``progress`` is invoked with ``(stream, line)`` once before each
    sub-command — e.g. ``("dump", "[dump] restoring snapshot.pg_dump (42 MB)
    via pg_restore…")`` — so callers can render lifecycle messages while
    the (otherwise silent) restore is in flight.
    """

    env = dict(os.environ)
    env.update(env_overlay or {})
    env.setdefault("PGPASSWORD", config.postgres.password)
    emit = progress or _noop_dump_progress
    size_label = _format_size(plan.path)

    if plan.format is DumpFormat.PG_RESTORE:
        # Peel any outer gzip/tar wrapper to a path pg_restore can read; the
        # temp extraction (if any) lives only for the duration of the restore.
        with prepared_archive(plan.path) as source:
            argvs = build_post_start_argv(
                plan=plan,
                config=config,
                pg_host=pg_host,
                pg_port=pg_port,
                container_id=container_id,
                restore_source=source,
            )
            _run_argvs(argvs, env=env, emit=emit, plan=plan, size_label=size_label, config=config)
        return

    argvs = build_post_start_argv(
        plan=plan,
        config=config,
        pg_host=pg_host,
        pg_port=pg_port,
        container_id=container_id,
    )
    _run_argvs(argvs, env=env, emit=emit, plan=plan, size_label=size_label, config=config)


def _run_argvs(
    argvs: list[Sequence[str] | Pipe],
    *,
    env: dict[str, str],
    emit: DumpProgressCallback,
    plan: DumpPlan,
    size_label: str,
    config: RunSiteConfig,
) -> None:
    for argv in argvs:
        if isinstance(argv, Pipe):
            emit("dump", _describe_pipe(argv, plan=plan, size_label=size_label))
            _run_pipe(argv.stages, env=env, fail_fast=config.dump.fail_fast)
        else:
            emit("dump", _describe_step(argv, plan=plan, size_label=size_label))
            _run(list(argv), env=env, fail_fast=config.dump.fail_fast)


def _run(argv: Sequence[str], *, env: dict[str, str], fail_fast: bool) -> None:
    proc = subprocess.run(list(argv), env=env, check=False, capture_output=True, text=True)
    if proc.returncode != 0 and fail_fast:
        raise DumpError(
            f"Restore step failed (exit {proc.returncode}): {' '.join(argv)}\n"
            f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
        )


def _run_pipe(
    stages: Sequence[Sequence[str]],
    *,
    env: dict[str, str],
    fail_fast: bool,
) -> None:
    """Run ``stages`` as a pipeline: each stage's stdout is the next stage's
    stdin. The final stage's stdout/stderr are captured for error reporting.
    Raises :class:`DumpError` if any stage exits non-zero and ``fail_fast``
    is set (pipefail semantics)."""

    if not stages:
        return
    procs: list[subprocess.Popen[bytes]] = []
    prev_stdout = None
    for stage in stages[:-1]:
        proc = subprocess.Popen(list(stage), env=env, stdin=prev_stdout, stdout=subprocess.PIPE)
        if prev_stdout is not None:
            # Parent closes its copy so a downstream exit propagates SIGPIPE.
            prev_stdout.close()
        procs.append(proc)
        prev_stdout = proc.stdout
    last = subprocess.run(
        list(stages[-1]),
        env=env,
        stdin=prev_stdout,
        check=False,
        capture_output=True,
        text=True,
    )
    if prev_stdout is not None:
        prev_stdout.close()
    codes = [(list(stage), proc.wait()) for stage, proc in zip(stages[:-1], procs, strict=True)]
    codes.append((list(stages[-1]), last.returncode))
    if any(rc != 0 for _, rc in codes) and fail_fast:
        detail = "\n".join(f"  exit {rc}: {argv}" for argv, rc in codes)
        raise DumpError(
            f"Piped restore failed:\n{detail}\nstdout:\n{last.stdout}\nstderr:\n{last.stderr}"
        )


def _noop_dump_progress(stream: str, line: str) -> None:
    """Default progress sink — discards messages so callers that don't
    care about progress (library use, tests) keep the old silent behavior."""


def _format_size(path: Path) -> str:
    """Render *path*'s size as a human-readable label (e.g. ``"42.3 MB"``).
    Returns ``"unknown size"`` if the file is missing — we never want a
    progress message to crash the restore."""

    try:
        raw = path.stat().st_size
    except OSError:
        return "unknown size"
    n: float = float(raw)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{int(n)} B" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _pipe_stage_label(stage: Sequence[str]) -> str:
    head = Path(stage[0]).name
    if head == "docker" and "pg_restore" in stage:
        return "pg_restore"
    return head


def _describe_pipe(pipe: Pipe, *, plan: DumpPlan, size_label: str) -> str:
    names = " | ".join(_pipe_stage_label(s) for s in pipe.stages)
    return f"[dump] loading {plan.path.name} ({size_label}) via {names}…"


def _describe_step(argv: Sequence[str], *, plan: DumpPlan, size_label: str) -> str:
    """Turn a restore argv into a one-line progress message.

    The argv shape comes from :func:`build_post_start_argv`, so we look at
    the tool name to decide the verb (``docker cp`` → copy, ``pg_restore``
    → restore, ``psql`` → load). Anything else falls back to a generic
    ``[dump] running …`` line so future argv shapes don't go silent.
    """

    head = Path(argv[0]).name
    name = plan.path.name
    if head == "docker" and len(argv) > 1 and argv[1] == "cp":
        return f"[dump] copying {name} ({size_label}) into container…"
    if head == "docker" and len(argv) > 1 and argv[1] == "exec":
        # ``docker exec … pg_restore …`` — surface the inner tool, not docker.
        if "pg_restore" in argv:
            return f"[dump] restoring {name} via pg_restore (this may take a while)…"
        return f"[dump] running {' '.join(argv[:4])}…"
    if head.endswith("psql"):
        return f"[dump] loading {name} ({size_label}) via psql…"
    if head.endswith("pg_restore"):
        return f"[dump] restoring {name} ({size_label}) via pg_restore…"
    return f"[dump] running {head}…"


def _require_tool(tool: str) -> str:
    path = shutil.which(tool)
    if path is None:
        raise DumpError(f"{tool!r} not found on PATH; install it to load this dump format.")
    return path
