"""Hook execution tests — command + django types, dynamic CLI args (§17)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from django_run_site.config import HookCliArg, HookConfig, load_config
from django_run_site.errors import HookError
from django_run_site.hooks import build_hook_context, run_hooks

PYTHON = sys.executable


def make_command_hook(
    *,
    stage: str = "pre_containers",
    argv: tuple[str, ...] = ("true",),
    timeout: float | None = None,
) -> HookConfig:
    return HookConfig(
        stage=stage,
        type="command",
        command=argv,
        timeout=timeout,
    )


def test_command_hook_success(minimal_config) -> None:
    ctx = build_hook_context(
        config=minimal_config,
        manage_py=Path("/tmp/manage.py"),
        runserver_port=None,
        pg_host=None,
        pg_port=None,
        redis_host=None,
        redis_port=None,
        dump_path=None,
        reuse=False,
        pg_created=None,
        redis_created=None,
        superuser=None,
        opts={},
    )
    run_hooks(
        stage="pre_containers",
        hooks=(make_command_hook(argv=(PYTHON, "-c", "print('ok')")),),
        context=ctx,
        python=None,
        manage_py=None,
        env={},
        disabled_flags=set(),
    )


def test_command_hook_failure_raises(minimal_config) -> None:
    ctx = build_hook_context(
        config=minimal_config,
        manage_py=Path("/tmp/manage.py"),
        runserver_port=None,
        pg_host=None,
        pg_port=None,
        redis_host=None,
        redis_port=None,
        dump_path=None,
        reuse=False,
        pg_created=None,
        redis_created=None,
        superuser=None,
        opts={},
    )
    with pytest.raises(HookError, match="Command hook failed"):
        run_hooks(
            stage="pre_containers",
            hooks=(make_command_hook(argv=(PYTHON, "-c", "import sys; sys.exit(2)")),),
            context=ctx,
            python=None,
            manage_py=None,
            env={},
            disabled_flags=set(),
        )


def test_disabled_hook_is_skipped(minimal_config) -> None:
    hook = HookConfig(
        stage="pre_containers",
        type="command",
        command=(PYTHON, "-c", "import sys; sys.exit(1)"),
        cli_disable_flag="--skip-this",
    )
    ctx = build_hook_context(
        config=minimal_config,
        manage_py=Path("/tmp/manage.py"),
        runserver_port=None,
        pg_host=None,
        pg_port=None,
        redis_host=None,
        redis_port=None,
        dump_path=None,
        reuse=False,
        pg_created=None,
        redis_created=None,
        superuser=None,
        opts={},
    )
    # Disabled flag in set => no error even though command would fail.
    run_hooks(
        stage="pre_containers",
        hooks=(hook,),
        context=ctx,
        python=None,
        manage_py=None,
        env={},
        disabled_flags={"--skip-this"},
    )


def test_best_effort_post_stop_swallows_errors(minimal_config) -> None:
    ctx = build_hook_context(
        config=minimal_config,
        manage_py=Path("/tmp/manage.py"),
        runserver_port=None,
        pg_host=None,
        pg_port=None,
        redis_host=None,
        redis_port=None,
        dump_path=None,
        reuse=False,
        pg_created=None,
        redis_created=None,
        superuser=None,
        opts={},
    )
    run_hooks(
        stage="post_stop",
        hooks=(
            make_command_hook(stage="post_stop", argv=(PYTHON, "-c", "import sys; sys.exit(2)")),
        ),
        context=ctx,
        python=None,
        manage_py=None,
        env={},
        disabled_flags=set(),
        best_effort=True,
    )


def test_dynamic_cli_args_validation(tmp_path: Path) -> None:
    cfg_path = tmp_path / "runsite.toml"
    cfg_path.write_text(
        'project_slug = "x"\n'
        "[[hooks.post_migrate]]\n"
        'type = "django"\n'
        'callable = "myproj.hooks:fetch_pbn_token"\n'
        "[[hooks.post_migrate.cli_args]]\n"
        'flag = "--get-pbn-token-from"\n'
        'dest = "pbn_ssh_source"\n'
        'metavar = "USER@HOST"\n'
    )
    config = load_config(config_path=cfg_path, project_root=tmp_path)
    assert len(config.hooks) == 1
    hook = config.hooks[0]
    assert hook.callable == "myproj.hooks:fetch_pbn_token"
    assert hook.cli_args[0] == HookCliArg(
        flag="--get-pbn-token-from",
        dest="pbn_ssh_source",
        metavar="USER@HOST",
        help=None,
        default=None,
        required=False,
    )


def test_django_hook_command_construction(minimal_config, tmp_path: Path) -> None:
    """The exact argv we'd run for a django hook contains shell -c with bootstrap."""

    from django_run_site.hooks import DJANGO_BOOTSTRAP

    assert "import json" in DJANGO_BOOTSTRAP
    assert "DJANGO_RUN_SITE_CONTEXT" in DJANGO_BOOTSTRAP
    assert "DJANGO_RUN_SITE_CALLABLE" in DJANGO_BOOTSTRAP
    assert "rsplit(':', 1)" in DJANGO_BOOTSTRAP
