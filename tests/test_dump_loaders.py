"""Dump format detection and strategy resolution."""

from __future__ import annotations

import gzip
import io
import tarfile
from pathlib import Path
from typing import Any

import pytest

from run_site.config import load_config
from run_site.dumps import (
    DumpFormat,
    DumpPlan,
    build_post_start_argv,
    detect_format,
    execute_post_start,
    plan_dump,
    prepared_archive,
    resolve_restore_jobs,
)
from run_site.errors import DumpError

# ---------------------------------------------------------------------------
# Fixtures builders — synthesize tiny, realistic dump bytes in-memory so
# detection can be exercised without a real pg_dump / Docker / server.
# ---------------------------------------------------------------------------

PGDMP_MAGIC = b"PGDMP" + b"\x00" * 700  # toc.dat starts with this magic


def _tar_bytes(members: dict[str, bytes]) -> bytes:
    """Return an (uncompressed) tar archive containing *members*."""

    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return raw.getvalue()


def _write_dir_dump_tar(path: Path, *, gzipped: bool, top_dir: str = "db-backup-20260601") -> Path:
    """Write a tar (optionally gzipped) wrapping a pg_dump *directory*-format
    archive: ``<top_dir>/toc.dat`` (PGDMP magic) plus a compressed data file."""

    data = _tar_bytes(
        {
            f"{top_dir}/toc.dat": PGDMP_MAGIC,
            f"{top_dir}/3079.dat.gz": b"\x1f\x8b\x08\x00datafile",
        }
    )
    if gzipped:
        data = gzip.compress(data)
    path.write_bytes(data)
    return path


def test_detect_plain_sql(tmp_path: Path) -> None:
    f = tmp_path / "baseline.sql"
    f.touch()
    assert detect_format(f) is DumpFormat.PLAIN_SQL


def test_detect_gzipped_sql(tmp_path: Path) -> None:
    f = tmp_path / "baseline.sql.gz"
    f.touch()
    assert detect_format(f) is DumpFormat.GZIPPED_SQL


def test_detect_custom_by_extension(tmp_path: Path) -> None:
    # No conclusive magic bytes → fall back to the .pgdump extension hint.
    f = tmp_path / "snapshot.pgdump"
    f.touch()
    assert detect_format(f) is DumpFormat.PG_RESTORE


def test_detect_unsupported(tmp_path: Path) -> None:
    f = tmp_path / "foo.txt"
    f.touch()
    with pytest.raises(DumpError, match="Unsupported"):
        detect_format(f)


def test_detect_tar_gz_directory_dump_is_pg_restore(tmp_path: Path) -> None:
    # The real-world failure: a pg_dump -Fd directory archive that was
    # tar|gzip-ed for transport. Name ends in .tar.gz, NOT .sql.gz.
    f = _write_dir_dump_tar(tmp_path / "db-backup-20260601.tar.gz", gzipped=True)
    assert detect_format(f) is DumpFormat.PG_RESTORE


def test_detect_content_wins_over_misleading_name(tmp_path: Path) -> None:
    # A directory dump tarball named nothing like a dump — content must win.
    f = _write_dir_dump_tar(tmp_path / "backup.bin", gzipped=True)
    assert detect_format(f) is DumpFormat.PG_RESTORE


def test_detect_bare_tar_directory_dump_is_pg_restore(tmp_path: Path) -> None:
    # Uncompressed tar of a directory dump (no outer gzip).
    f = _write_dir_dump_tar(tmp_path / "backup.tar", gzipped=False)
    assert detect_format(f) is DumpFormat.PG_RESTORE


def test_detect_raw_custom_magic_is_pg_restore(tmp_path: Path) -> None:
    # A single-file custom dump identified by its PGDMP magic, not extension.
    f = tmp_path / "snapshot.bin"
    f.write_bytes(PGDMP_MAGIC)
    assert detect_format(f) is DumpFormat.PG_RESTORE


def test_detect_real_gzipped_sql_not_misrouted(tmp_path: Path) -> None:
    # Boundary guard: genuinely gzipped SQL text must stay GZIPPED_SQL even
    # though it shares the gzip wrapper with tar.gz archives.
    f = tmp_path / "weird.gz"
    f.write_bytes(gzip.compress(b"-- dump\nSET statement_timeout = 0;\n" * 50))
    assert detect_format(f) is DumpFormat.GZIPPED_SQL


def test_plan_returns_none_with_no_dump(minimal_config) -> None:
    plan = plan_dump(
        config=minimal_config,
        cli_dump_path=None,
        cli_no_dump=True,
        cli_strategy_override=None,
        pg_created=True,
    )
    assert plan is None


def test_plan_auto_init_script_when_pg_fresh_and_sql(tmp_path: Path, minimal_config) -> None:
    dump = tmp_path / "baseline.sql"
    dump.write_text("-- sql\n")
    plan = plan_dump(
        config=minimal_config,
        cli_dump_path=dump,
        cli_no_dump=False,
        cli_strategy_override=None,
        pg_created=True,
    )
    assert plan is not None
    assert plan.strategy == "init-script"


def test_plan_auto_post_start_for_gzipped(tmp_path: Path, minimal_config) -> None:
    dump = tmp_path / "baseline.sql.gz"
    dump.write_bytes(b"\x1f\x8b\x08\x00")
    plan = plan_dump(
        config=minimal_config,
        cli_dump_path=dump,
        cli_no_dump=False,
        cli_strategy_override=None,
        pg_created=True,
    )
    assert plan is not None
    assert plan.strategy == "post-start"


def test_plan_auto_skips_when_pg_reused(tmp_path: Path, minimal_config) -> None:
    dump = tmp_path / "baseline.sql"
    dump.write_text("-- sql\n")
    plan = plan_dump(
        config=minimal_config,
        cli_dump_path=dump,
        cli_no_dump=False,
        cli_strategy_override=None,
        pg_created=False,
    )
    assert plan is not None
    assert plan.strategy == "skip"
    assert plan.reason is not None


def test_plan_init_script_explicit_with_reused_pg_errors(tmp_path: Path, minimal_config) -> None:
    dump = tmp_path / "baseline.sql"
    dump.write_text("-- sql\n")
    with pytest.raises(DumpError, match="freshly created"):
        plan_dump(
            config=minimal_config,
            cli_dump_path=dump,
            cli_no_dump=False,
            cli_strategy_override="init-script",
            pg_created=False,
        )


def test_plan_init_script_with_non_sql_format_errors(tmp_path: Path, minimal_config) -> None:
    dump = tmp_path / "snap.pgdump"
    dump.write_text("\x00")
    with pytest.raises(DumpError, match=r"only supports \.sql"):
        plan_dump(
            config=minimal_config,
            cli_dump_path=dump,
            cli_no_dump=False,
            cli_strategy_override="init-script",
            pg_created=True,
        )


def test_plan_default_path_from_config(tmp_path: Path) -> None:
    dump = tmp_path / "baseline.sql"
    dump.write_text("-- sql\n")
    cfg_path = tmp_path / "runsite.toml"
    cfg_path.write_text(f'project_slug = "x"\n[dump]\ndefault_path = "{dump.name}"\n')
    config = load_config(config_path=cfg_path, project_root=tmp_path)
    plan = plan_dump(
        config=config,
        cli_dump_path=None,
        cli_no_dump=False,
        cli_strategy_override=None,
        pg_created=True,
    )
    assert plan is not None
    assert plan.path.name == "baseline.sql"


def test_plan_missing_file_errors(tmp_path: Path, minimal_config) -> None:
    dump = tmp_path / "missing.sql"
    with pytest.raises(DumpError, match="does not exist"):
        plan_dump(
            config=minimal_config,
            cli_dump_path=dump,
            cli_no_dump=False,
            cli_strategy_override=None,
            pg_created=True,
        )


def test_resolve_restore_jobs() -> None:
    assert resolve_restore_jobs(4) == 4
    assert resolve_restore_jobs("auto") >= 1


# ---------------------------------------------------------------------------
# execute_post_start progress messages — exercised with subprocess stubs so
# we never need a real psql / pg_restore / docker on the test host.
# ---------------------------------------------------------------------------


def _stub_subprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the dumps module's process runners with no-ops."""

    def fake_run(argv: Any, *, env: Any, fail_fast: bool) -> None:
        return None

    def fake_run_pipe(left: Any, right: Any, *, env: Any, fail_fast: bool) -> None:
        return None

    monkeypatch.setattr("run_site.dumps._run", fake_run)
    monkeypatch.setattr("run_site.dumps._run_pipe", fake_run_pipe)
    # build_post_start_argv calls _require_tool("psql"/"docker"/"gunzip"),
    # which shells out to ``shutil.which``. Stub it so tests don't depend
    # on the host having psql installed.
    monkeypatch.setattr("run_site.dumps._require_tool", lambda tool: f"/fake/bin/{tool}")


def test_execute_post_start_emits_progress_for_plain_sql(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, minimal_config
) -> None:
    _stub_subprocess(monkeypatch)
    dump = tmp_path / "baseline.sql"
    dump.write_bytes(b"-- sql\n" * 1024)  # ~7 KB
    plan = DumpPlan(path=dump, format=DumpFormat.PLAIN_SQL, strategy="post-start")

    events: list[tuple[str, str]] = []
    execute_post_start(
        plan,
        config=minimal_config,
        pg_host="127.0.0.1",
        pg_port=5432,
        container_id=None,
        progress=lambda stream, line: events.append((stream, line)),
    )

    lines = [line for _, line in events]
    assert any("baseline.sql" in line and "psql" in line for line in lines), lines
    # All progress lines belong to the "dump" stream so the mux paints them
    # in one consistent color.
    assert {stream for stream, _ in events} == {"dump"}


def test_execute_post_start_emits_progress_for_gzipped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, minimal_config
) -> None:
    _stub_subprocess(monkeypatch)
    dump = tmp_path / "baseline.sql.gz"
    dump.write_bytes(b"\x1f\x8b\x08\x00" + b"\x00" * 2048)
    plan = DumpPlan(path=dump, format=DumpFormat.GZIPPED_SQL, strategy="post-start")

    events: list[tuple[str, str]] = []
    execute_post_start(
        plan,
        config=minimal_config,
        pg_host="127.0.0.1",
        pg_port=5432,
        container_id=None,
        progress=lambda stream, line: events.append((stream, line)),
    )

    lines = [line for _, line in events]
    assert any(
        "baseline.sql.gz" in line and "gunzip" in line and "psql" in line for line in lines
    ), lines


def test_execute_post_start_emits_progress_for_custom(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, minimal_config
) -> None:
    """Custom (pg_dump) format runs two argvs — docker cp then pg_restore.
    Each step should announce itself so the user knows the dump is
    actually progressing during a long restore."""

    _stub_subprocess(monkeypatch)
    dump = tmp_path / "snapshot.pg_dump"
    # A raw single-file custom dump: PGDMP magic + padding to ~4 MB so the
    # size formatter has a non-zero number to render.
    dump.write_bytes(PGDMP_MAGIC + b"\x00" * (4 * 1024 * 1024))
    plan = DumpPlan(path=dump, format=DumpFormat.PG_RESTORE, strategy="post-start")

    events: list[tuple[str, str]] = []
    execute_post_start(
        plan,
        config=minimal_config,
        pg_host="127.0.0.1",
        pg_port=5432,
        container_id="cid-12345",
        progress=lambda stream, line: events.append((stream, line)),
    )

    lines = [line for _, line in events]
    assert any("snapshot.pg_dump" in line and "copy" in line.lower() for line in lines), lines
    assert any("pg_restore" in line for line in lines), lines
    # Size should appear somewhere — that's the main reason the user wants
    # progress in the first place ("how big is this restore?").
    assert any("MB" in line or "KB" in line for line in lines), lines


def test_execute_post_start_works_without_progress(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, minimal_config
) -> None:
    """Backward compatibility: omitting ``progress`` must not raise."""

    _stub_subprocess(monkeypatch)
    dump = tmp_path / "baseline.sql"
    dump.write_text("-- sql\n")
    plan = DumpPlan(path=dump, format=DumpFormat.PLAIN_SQL, strategy="post-start")

    execute_post_start(
        plan,
        config=minimal_config,
        pg_host="127.0.0.1",
        pg_port=5432,
        container_id=None,
    )


# ---------------------------------------------------------------------------
# prepared_archive — host-side unwrap of gzip/tar packaging into a path that
# pg_restore can read.
# ---------------------------------------------------------------------------


def test_prepared_archive_extracts_tar_gz_directory(tmp_path: Path) -> None:
    f = _write_dir_dump_tar(tmp_path / "backup.tar.gz", gzipped=True, top_dir="db-1")
    captured: Path | None = None
    with prepared_archive(f) as src:
        captured = src
        assert src.is_dir()
        assert (src / "toc.dat").exists()
    # Temp extraction dir is cleaned up once the context exits.
    assert captured is not None and not captured.exists()


def test_prepared_archive_extracts_bare_tar_directory(tmp_path: Path) -> None:
    f = _write_dir_dump_tar(tmp_path / "backup.tar", gzipped=False, top_dir="db-2")
    with prepared_archive(f) as src:
        assert src.is_dir()
        assert (src / "toc.dat").exists()


def test_prepared_archive_passthrough_for_raw_custom(tmp_path: Path) -> None:
    # A raw single-file custom dump needs no unwrapping — pg_restore reads it.
    f = tmp_path / "snapshot.dump"
    f.write_bytes(PGDMP_MAGIC)
    with prepared_archive(f) as src:
        assert src == f


def test_prepared_archive_raises_when_no_toc(tmp_path: Path) -> None:
    data = _tar_bytes({"junk/not-a-dump.txt": b"nope"})
    f = tmp_path / "bad.tar.gz"
    f.write_bytes(gzip.compress(data))
    with pytest.raises(DumpError, match=r"toc\.dat"), prepared_archive(f):
        pass


# ---------------------------------------------------------------------------
# build_post_start_argv + execute_post_start for the directory case.
# ---------------------------------------------------------------------------


def test_build_post_start_argv_uses_restore_source(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, minimal_config
) -> None:
    monkeypatch.setattr("run_site.dumps._require_tool", lambda tool: f"/fake/bin/{tool}")
    src_dir = tmp_path / "extracted"
    src_dir.mkdir()
    (src_dir / "toc.dat").write_bytes(PGDMP_MAGIC)
    plan = DumpPlan(
        path=tmp_path / "backup.tar.gz",
        format=DumpFormat.PG_RESTORE,
        strategy="post-start",
    )

    argvs = build_post_start_argv(
        plan=plan,
        config=minimal_config,
        pg_host="127.0.0.1",
        pg_port=5432,
        container_id="cid-77",
        restore_source=src_dir,
    )

    cp, restore = argvs
    # docker cp copies the *unwrapped* source (not the .tar.gz) into the container.
    assert cp[1] == "cp"
    assert str(src_dir) in cp
    assert "cid-77:/tmp/dump" in cp
    assert "pg_restore" in restore
    assert restore[-1] == "/tmp/dump"


def _capture_argvs(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Record every restore argv instead of shelling out."""

    calls: list[list[str]] = []

    def fake_run(argv: Any, *, env: Any, fail_fast: bool) -> None:
        calls.append(list(argv))

    def fake_run_pipe(left: Any, right: Any, *, env: Any, fail_fast: bool) -> None:
        calls.append(["__pipe__", *left, *right])

    monkeypatch.setattr("run_site.dumps._run", fake_run)
    monkeypatch.setattr("run_site.dumps._run_pipe", fake_run_pipe)
    monkeypatch.setattr("run_site.dumps._require_tool", lambda tool: f"/fake/bin/{tool}")
    return calls


def test_execute_post_start_restores_tar_gz_directory_via_pg_restore(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, minimal_config
) -> None:
    """End to end (with stubbed subprocesses): a tar.gz-wrapped directory
    dump is unwrapped, docker cp'd, and restored via pg_restore."""

    calls = _capture_argvs(monkeypatch)
    dump = _write_dir_dump_tar(tmp_path / "db-backup.tar.gz", gzipped=True, top_dir="db-3")
    plan = DumpPlan(path=dump, format=DumpFormat.PG_RESTORE, strategy="post-start")

    events: list[str] = []
    execute_post_start(
        plan,
        config=minimal_config,
        pg_host="127.0.0.1",
        pg_port=5432,
        container_id="cid-9",
        progress=lambda stream, line: events.append(line),
    )

    cp = next(c for c in calls if len(c) > 1 and c[1] == "cp")
    # The cp source is a freshly extracted temp dir, not the original .tar.gz.
    assert "run-site-dump-" in cp[2]
    assert cp[2].endswith(".tar.gz") is False
    assert "cid-9:/tmp/dump" in cp
    assert any("pg_restore" in " ".join(c) for c in calls), calls
    assert any("pg_restore" in line for line in events), events
