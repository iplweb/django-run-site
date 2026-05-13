"""CLI smoke tests — --version, --help, --dry-run, doctor."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from run_site import __version__
from run_site.cli import _build_web_argv, main
from run_site.config import RunSiteConfig


def run_cli(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    code = main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_version_prints_and_exits(capsys) -> None:
    code, out, _ = run_cli(["--version"], capsys)
    assert code == 0
    assert __version__ in out


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


def test_force_reset_is_recognised_by_pre_parser() -> None:
    """``--force-reset`` is consumed by the *pre*-parser too, so it
    actually reaches ``resolve_git_source()`` on the dirty-checkout
    path — not just the full parser run after Git resolution."""

    from run_site.cli import _pre_parse

    pre = _pre_parse(["--from-git", "https://example.com/foo.git", "--force-reset", "--yes"])
    assert pre.force_reset is True
    assert pre.from_git == "https://example.com/foo.git"


def test_apply_cli_overrides_restore_jobs_overrides_config(
    minimal_config,
) -> None:
    """``--restore-jobs N`` must end up in the effective ``config.dump``,
    not be silently dropped."""

    import argparse

    from run_site.cli import _apply_cli_overrides

    opts = argparse.Namespace(
        postgres_image=None,
        redis_image=None,
        bind=None,
        restore_jobs=4,
        no_install=False,
    )
    out = _apply_cli_overrides(minimal_config, opts)
    assert out.dump.restore_jobs == 4


def test_apply_cli_overrides_bind_propagates_to_display_host(
    minimal_config,
) -> None:
    """``--bind HOST`` must also set the banner's display host — otherwise
    the banner advertises ``http://localhost:…`` while runserver listens
    on a different hostname."""

    import argparse

    from run_site.cli import _apply_cli_overrides

    opts = argparse.Namespace(
        postgres_image=None,
        redis_image=None,
        bind="mac-mini-micha",
        restore_jobs=None,
        no_install=False,
    )
    out = _apply_cli_overrides(minimal_config, opts)
    assert out.django.runserver_bind == "mac-mini-micha"
    assert out.django.runserver_display_host == "mac-mini-micha"


def test_apply_cli_overrides_bind_zero_falls_back_to_localhost(
    minimal_config,
) -> None:
    """``--bind 0.0.0.0`` binds to all interfaces but isn't itself
    browseable — the banner should still show ``localhost``."""

    import argparse

    from run_site.cli import _apply_cli_overrides

    opts = argparse.Namespace(
        postgres_image=None,
        redis_image=None,
        bind="0.0.0.0",
        restore_jobs=None,
        no_install=False,
    )
    out = _apply_cli_overrides(minimal_config, opts)
    assert out.django.runserver_bind == "0.0.0.0"
    assert out.django.runserver_display_host == "localhost"


def test_apply_cli_overrides_source_no_install_folds_into_opts(
    minimal_config,
) -> None:
    """``[source].no_install = true`` must turn ``opts.no_install`` on so
    venv setup is skipped, mirroring the CLI flag."""

    import argparse
    from dataclasses import replace

    from run_site.cli import _apply_cli_overrides

    cfg = replace(minimal_config, source=replace(minimal_config.source, no_install=True))
    opts = argparse.Namespace(
        postgres_image=None,
        redis_image=None,
        bind=None,
        restore_jobs=None,
        no_install=False,
    )
    _apply_cli_overrides(cfg, opts)
    assert opts.no_install is True


def test_celery_active_with_flag_enables_when_app_set(minimal_config) -> None:
    """``--with-celery`` should enable Celery even when ``[celery].enabled
    = false``, as long as an app is configured. Otherwise the flag is a
    no-op surprise."""

    import argparse
    from dataclasses import replace

    from run_site.cli import _celery_active

    cfg = replace(
        minimal_config,
        celery=replace(minimal_config.celery, enabled=False, app="demo.celery"),
    )
    opts = argparse.Namespace(with_celery=True, with_beat=None)
    assert _celery_active(cfg, opts) is True


def test_celery_active_with_flag_no_app_stays_off(minimal_config) -> None:
    """``--with-celery`` without ``[celery].app`` cannot enable Celery —
    we have nothing to spawn."""

    import argparse

    from run_site.cli import _celery_active

    opts = argparse.Namespace(with_celery=True, with_beat=None)
    assert _celery_active(minimal_config, opts) is False


def test_celery_active_no_celery_flag_overrides_enabled(minimal_config) -> None:
    """``--no-celery`` always wins."""

    import argparse
    from dataclasses import replace

    from run_site.cli import _celery_active

    cfg = replace(
        minimal_config,
        celery=replace(minimal_config.celery, enabled=True, app="demo.celery"),
    )
    opts = argparse.Namespace(with_celery=False, with_beat=None)
    assert _celery_active(cfg, opts) is False


def test_force_reset_threads_into_resolve_git_source(tmp_path: Path, monkeypatch, capsys) -> None:
    """End-to-end: invoking ``run-site run --from-git ... --force-reset``
    must call ``resolve_git_source(force_reset=True)``."""

    from run_site import cli as cli_mod
    from run_site.errors import RunSiteError

    seen: dict[str, object] = {}

    def fake_resolve_git_source(**kwargs):
        seen.update(kwargs)
        # Bail out with a known error so the run flow stops before
        # touching Docker / Postgres in this unit test.
        raise RunSiteError("stopping early in test")

    monkeypatch.setattr(cli_mod, "resolve_git_source", fake_resolve_git_source)
    monkeypatch.chdir(tmp_path)

    code, _out, err = run_cli(
        [
            "run",
            "--from-git",
            "https://example.com/foo.git",
            "--checkout-path",
            str(tmp_path / "checkout"),
            "--force-reset",
            "--yes",
        ],
        capsys,
    )
    assert code != 0
    assert "stopping early in test" in err
    assert seen.get("force_reset") is True


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
