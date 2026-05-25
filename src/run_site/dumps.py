"""Database dump format detection and restore strategies.

The actual restore is split between two phases of the run flow:

- ``init-script``: file is mounted into ``/docker-entrypoint-initdb.d/``
  *before* PG starts, so PG loads it as part of normal startup. Only works
  for plain ``.sql`` and only when the container is created fresh.
- ``post-start``: file is loaded after PG is up, via ``psql`` (plain or
  gzipped) or ``pg_restore`` (custom format).

The ``auto`` strategy picks the right one based on file extension and
whether the PG container was just created.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from run_site.config import RunSiteConfig
from run_site.errors import DumpError

# (stream_name, line) — color is fixed at the call site so callers only
# need a 2-arg shim around ``mux.write``.
DumpProgressCallback = Callable[[str, str], None]


class DumpFormat(Enum):
    PLAIN_SQL = "plain_sql"
    GZIPPED_SQL = "gzipped_sql"
    CUSTOM = "custom"


SQL_SUFFIXES: tuple[str, ...] = (".sql",)
GZIP_SUFFIXES: tuple[str, ...] = (".sql.gz", ".gz")
CUSTOM_SUFFIXES: tuple[str, ...] = (".dump", ".pgdump", ".pg_dump")


@dataclass(frozen=True)
class DumpPlan:
    """Resolved (file, format, strategy) tuple."""

    path: Path
    format: DumpFormat
    strategy: str  # "init-script" | "post-start" | "skip"
    reason: str | None = None


def detect_format(path: Path) -> DumpFormat:
    """Inspect *path* extension and return the dump format."""

    name = path.name.lower()
    if name.endswith(GZIP_SUFFIXES) and name.endswith(".sql.gz"):
        return DumpFormat.GZIPPED_SQL
    if name.endswith(CUSTOM_SUFFIXES):
        return DumpFormat.CUSTOM
    if name.endswith(SQL_SUFFIXES):
        return DumpFormat.PLAIN_SQL
    if name.endswith(GZIP_SUFFIXES):
        # ``foo.gz`` without ``.sql`` — treat as gzipped SQL pessimistically.
        return DumpFormat.GZIPPED_SQL
    raise DumpError(
        f"Unsupported dump format: {path.name}. "
        "Expected .sql, .sql.gz, .dump, .pgdump, or .pg_dump."
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
) -> list[Sequence[str]]:
    """Return a list of argv tuples to execute, in order.

    For plain/gzipped dumps the only argv is ``psql`` (with ``gunzip`` piped
    via shell). For custom dumps we ``docker cp`` the file into the
    container and then ``docker exec pg_restore`` — two argv tuples.
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
        # We rely on the shell-pipe via subprocess.run(shell=True) at the
        # call site; here we return tokens for the dumps runner to compose.
        return [
            (
                "__pipe__",
                "gunzip",
                "-c",
                str(plan.path),
                psql,
                *base_env_args,
                "-v",
                "ON_ERROR_STOP=1",
            )
        ]

    if plan.format is DumpFormat.CUSTOM:
        if container_id is None:
            raise DumpError(
                "Custom-format dumps require a known container id "
                "(docker cp + pg_restore inside the container)."
            )
        docker = _require_tool("docker")
        jobs = resolve_restore_jobs(config.dump.restore_jobs)
        return [
            (docker, "cp", str(plan.path), f"{container_id}:/tmp/dump"),
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

    argvs = build_post_start_argv(
        plan=plan,
        config=config,
        pg_host=pg_host,
        pg_port=pg_port,
        container_id=container_id,
    )
    env = dict(os.environ)
    env.update(env_overlay or {})
    env.setdefault("PGPASSWORD", config.postgres.password)
    emit = progress or _noop_dump_progress
    size_label = _format_size(plan.path)

    for argv in argvs:
        if argv[0] == "__pipe__":
            # ("__pipe__", *left_argv, *right_argv) — encoded as marker plus
            # left/right cmd halves separated by "psql".
            tokens = list(argv[1:])
            psql_idx = next(i for i, t in enumerate(tokens) if t.endswith("psql"))
            left = tokens[:psql_idx]
            right = tokens[psql_idx:]
            emit(
                "dump",
                f"[dump] loading {plan.path.name} ({size_label}) via "
                f"{Path(left[0]).name} | {Path(right[0]).name}…",
            )
            _run_pipe(left, right, env=env, fail_fast=config.dump.fail_fast)
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
    left: Sequence[str],
    right: Sequence[str],
    *,
    env: dict[str, str],
    fail_fast: bool,
) -> None:
    left_proc = subprocess.Popen(list(left), env=env, stdout=subprocess.PIPE)
    try:
        right_proc = subprocess.run(
            list(right),
            env=env,
            stdin=left_proc.stdout,
            check=False,
            capture_output=True,
            text=True,
        )
    finally:
        if left_proc.stdout is not None:
            left_proc.stdout.close()
        left_proc.wait()
    if (left_proc.returncode != 0 or right_proc.returncode != 0) and fail_fast:
        raise DumpError(
            f"Piped restore failed: left={left_proc.returncode} "
            f"right={right_proc.returncode}\n"
            f"left argv: {list(left)}\nright argv: {list(right)}\n"
            f"stdout:\n{right_proc.stdout}\nstderr:\n{right_proc.stderr}"
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
