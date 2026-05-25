"""Dump format detection and strategy resolution."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from run_site.config import load_config
from run_site.dumps import (
    DumpFormat,
    DumpPlan,
    detect_format,
    execute_post_start,
    plan_dump,
    resolve_restore_jobs,
)
from run_site.errors import DumpError


def test_detect_plain_sql(tmp_path: Path) -> None:
    f = tmp_path / "baseline.sql"
    f.touch()
    assert detect_format(f) is DumpFormat.PLAIN_SQL


def test_detect_gzipped_sql(tmp_path: Path) -> None:
    f = tmp_path / "baseline.sql.gz"
    f.touch()
    assert detect_format(f) is DumpFormat.GZIPPED_SQL


def test_detect_custom(tmp_path: Path) -> None:
    f = tmp_path / "snapshot.pgdump"
    f.touch()
    assert detect_format(f) is DumpFormat.CUSTOM


def test_detect_unsupported(tmp_path: Path) -> None:
    f = tmp_path / "foo.txt"
    f.touch()
    with pytest.raises(DumpError, match="Unsupported"):
        detect_format(f)


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
    # 4 MB so the size formatter has a non-zero number to render.
    dump.write_bytes(b"\x00" * (4 * 1024 * 1024))
    plan = DumpPlan(path=dump, format=DumpFormat.CUSTOM, strategy="post-start")

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
