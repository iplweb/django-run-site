"""Dump format detection and strategy resolution (§12)."""

from __future__ import annotations

from pathlib import Path

import pytest

from django_run_site.config import load_config
from django_run_site.dumps import (
    DumpFormat,
    detect_format,
    plan_dump,
    resolve_restore_jobs,
)
from django_run_site.errors import DumpError


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
