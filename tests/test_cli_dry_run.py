"""CLI smoke tests — --version, --help, --dry-run, doctor."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from run_site.cli import _build_web_argv, main
from run_site.config import RunSiteConfig


def run_cli(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    code = main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_version_prints_and_exits(capsys) -> None:
    code, out, _ = run_cli(["--version"], capsys)
    assert code == 0
    assert "0.4.0" in out


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


# ---------------------------------------------------------------------------
# _build_web_argv — runserver default + [django].web_command override
# ---------------------------------------------------------------------------


def test_build_web_argv_defaults_to_runserver(minimal_config: RunSiteConfig) -> None:
    """Without web_command, the orchestrator runs Django's builtin server."""

    argv = _build_web_argv(
        config=minimal_config,
        python=("/usr/bin/python",),
        manage_py=Path("/proj/manage.py"),
        runserver_port=8123,
    )
    assert argv == (
        "/usr/bin/python",
        "/proj/manage.py",
        "runserver",
        "127.0.0.1:8123",
    )


def test_build_web_argv_uses_override_with_substitution(
    minimal_config: RunSiteConfig,
) -> None:
    """web_command tokens go through the same substitution as
    [[extra_processes]].command — including a new ``{bind}``."""

    cfg = replace(
        minimal_config,
        django=replace(
            minimal_config.django,
            web_command=(
                "{python}",
                "-m",
                "daphne",
                "-b",
                "{bind}",
                "-p",
                "{port}",
                "demo.asgi:application",
            ),
        ),
    )
    argv = _build_web_argv(
        config=cfg,
        python=("/usr/bin/python",),
        manage_py=Path("/proj/manage.py"),
        runserver_port=8123,
    )
    assert argv == (
        "/usr/bin/python",
        "-m",
        "daphne",
        "-b",
        "127.0.0.1",
        "-p",
        "8123",
        "demo.asgi:application",
    )


def test_build_web_argv_inline_expands_multitoken_python(
    minimal_config: RunSiteConfig,
) -> None:
    """``{python}`` alone in a token expands to multiple argv entries —
    same rule as extra_processes — so ``["uv", "run", "python"]`` doesn't
    end up shell-quoted."""

    cfg = replace(
        minimal_config,
        django=replace(
            minimal_config.django,
            web_command=("{python}", "-m", "uvicorn", "demo.asgi:application"),
        ),
    )
    argv = _build_web_argv(
        config=cfg,
        python=("uv", "run", "python"),
        manage_py=Path("/proj/manage.py"),
        runserver_port=9000,
    )
    assert argv[:3] == ("uv", "run", "python")
    assert argv[3:] == ("-m", "uvicorn", "demo.asgi:application")
