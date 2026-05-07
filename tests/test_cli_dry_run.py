"""CLI smoke tests — --version, --help, --dry-run, doctor."""

from __future__ import annotations

from pathlib import Path

import pytest

from run_site.cli import main


def run_cli(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    code = main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_version_prints_and_exits(capsys) -> None:
    code, out, _ = run_cli(["--version"], capsys)
    assert code == 0
    assert "0.3.0" in out


def test_top_level_help(capsys) -> None:
    code, out, _ = run_cli(["--help"], capsys)
    assert code == 0
    assert "run-site" in out
    assert "doctor" in out


def test_unknown_command(capsys) -> None:
    code, _out, err = run_cli(["bogus"], capsys)
    assert code == 64
    assert "Unknown command" in err


def test_run_dry_run(tmp_path: Path, monkeypatch, capsys) -> None:
    """--dry-run should print a report and exit 0 without touching Docker."""

    cfg = tmp_path / "runsite.toml"
    cfg.write_text(
        'project_slug = "demo"\n'
        '[postgres]\nimage = "postgres:16"\n'
        '[redis]\nimage = "redis:7-alpine"\n'
    )
    (tmp_path / "manage.py").write_text("# fake\n")
    # No .venv exists in tmp_path, so we pass --no-install to skip venv setup.
    monkeypatch.chdir(tmp_path)
    code, out, _ = run_cli(["run", "--dry-run", "--no-install"], capsys)
    assert code == 0
    assert "run-site dry-run" in out
    assert "demo" in out


def test_run_help_includes_hook_args(tmp_path: Path, monkeypatch, capsys) -> None:
    """When config has hook cli_args they appear in --help."""

    cfg = tmp_path / "runsite.toml"
    cfg.write_text(
        'project_slug = "demo"\n'
        "[[hooks.post_migrate]]\n"
        'type = "django"\n'
        'callable = "myproj.hooks:fetch_pbn_token"\n'
        "[[hooks.post_migrate.cli_args]]\n"
        'flag = "--get-pbn-token-from"\n'
        'dest = "pbn_ssh_source"\n'
        'metavar = "USER@HOST"\n'
        'help = "Fetch token from SSH host"\n'
    )
    (tmp_path / "manage.py").write_text("")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit):
        main(["run", "--help"])
    out = capsys.readouterr().out
    assert "--get-pbn-token-from" in out
    assert "Hook arguments" in out


def test_mutually_exclusive_source_flags(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)
    code, _out, err = run_cli(
        ["run", "--from-git", "https://example.com/r.git", "--from-path", str(tmp_path)],
        capsys,
    )
    assert code != 0
    assert "mutually exclusive" in err
