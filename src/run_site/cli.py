"""Command-line entrypoint and the ``run`` / ``doctor`` flows.

The argument parser is built in two passes so that hook ``cli_args``
can be discovered from the resolved config before being added to the full
parser.
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import signal
import subprocess
import sys
import threading
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from run_site import __version__
from run_site.banner import BannerInfo, render_banner
from run_site.config import HookConfig, RunSiteConfig, load_config, resolve_auto_enabled
from run_site.containers import (
    RunSiteContainers,
    assert_docker_available,
    start_containers,
    stop_containers,
)
from run_site.discovery import (
    detect_required_env_vars,
    detect_services_from_settings,
    discover_local_python,
    discover_manage_py,
    discover_project_root,
    discover_settings_module,
    is_django_manage_py,
)
from run_site.display_detect import HeadlessSignal, detect_headless_session
from run_site.dumps import execute_post_start, plan_dump
from run_site.env import (
    ContainerEndpoints,
    build_subprocess_env,
    format_env_for_print,
    generate_autologin_token,
)
from run_site.errors import RunSiteError
from run_site.hooks import build_hook_context, run_hooks
from run_site.host_discovery import discover_lan_hosts
from run_site.log_multiplexer import LogMultiplexer
from run_site.processes import (
    ProcessGroup,
    TemplateContext,
    docker_logs_follow,
    find_free_port,
    run_oneshot,
    wait_for_http,
)
from run_site.secrets_store import load_or_generate_secret_key
from run_site.sidecar import SidecarInfo, remove_sidecar, write_sidecar
from run_site.source.deps_installer import install_dependencies
from run_site.source.from_git import (
    GitSource,
    cleanup_temp_checkout,
    resolve_git_source,
)
from run_site.source.from_path import resolve_path_source
from run_site.source.venv_setup import ensure_venv
from run_site.sqlite import (
    SqliteState,
    cleanup_sqlite,
    gitignore_warning,
    prepare_sqlite,
)
from run_site.sticky_banner import StickyRegion

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    """Top-level entrypoint. Returns a process exit code."""

    raw_argv = list(sys.argv[1:] if argv is None else argv)
    if not raw_argv:
        raw_argv = ["--help"]

    if raw_argv[0] in ("-V", "--version"):
        print(__version__)
        return 0

    if raw_argv[0] == "doctor":
        return _doctor_command(raw_argv[1:])

    if raw_argv[0] == "run":
        return _run_command(raw_argv[1:])

    if raw_argv[0] == "init":
        from run_site.init_cmd import init_command

        return init_command(raw_argv[1:])

    if raw_argv[0] in ("-h", "--help"):
        _print_top_help()
        return 0

    # Short form: ``run-site path/to/manage.py [extra args]``.
    # Trigger on anything that looks file-like — has a path separator or
    # ends with ``.py`` — so plain typos ("ruh") still get the clearer
    # "Unknown command" error below.
    candidate = raw_argv[0]
    looks_file_like = "/" in candidate or "\\" in candidate or candidate.endswith(".py")
    if looks_file_like:
        ok, reason = is_django_manage_py(Path(candidate))
        if ok:
            return _run_command(["--manage-py", candidate, *raw_argv[1:]])
        sys.stderr.write(
            f"error: {candidate!r} is not a usable Django manage.py: {reason}\n"
            "Use 'run-site run --manage-py <path>' explicitly if you want to "
            "skip this check.\n"
        )
        return 64

    sys.stderr.write(f"Unknown command: {raw_argv[0]!r}. Try 'run-site --help'.\n")
    return 64


def _print_top_help() -> None:
    sys.stdout.write(
        "run-site — CLI orchestrator for local Django development "
        "(package: run-site).\n"
        "\n"
        "Usage:\n"
        "  run-site init [options]         Generate a default runsite.toml\n"
        "  run-site run [options]          Spin up dev stack\n"
        "  run-site <path/to/manage.py>    Short form: equivalent to\n"
        "                                  'run --manage-py <path>'\n"
        "  run-site doctor [options]       Sanity-check config + tooling\n"
        "  run-site --version              Print version and exit\n"
        "\n"
        f"Version: {__version__}\n"
        "Run 'run-site <command> --help' for the full options list.\n"
    )


# ---------------------------------------------------------------------------
# Pre-parse — sniff source flags before loading config
# ---------------------------------------------------------------------------


@dataclass
class PreParsed:
    """Output of the first parsing pass."""

    config: Path | None
    project_root: Path | None
    from_git: str | None
    from_path: str | None
    branch: str | None
    tag: str | None
    commit: str | None
    checkout_path: str | None
    no_cache: bool
    no_pull: bool
    force_reset: bool
    yes: bool


def _build_pre_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--project-root", type=Path, default=None)
    parser.add_argument("--from-git", default=None)
    parser.add_argument("--from-path", default=None)
    parser.add_argument("--branch", default=None)
    parser.add_argument("--tag", default=None)
    parser.add_argument("--commit", default=None)
    parser.add_argument("--checkout-path", default=None)
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument("--no-pull", action="store_true")
    parser.add_argument("--force-reset", action="store_true")
    parser.add_argument("--yes", "-y", action="store_true")
    return parser


def _pre_parse(argv: Sequence[str]) -> PreParsed:
    parser = _build_pre_parser()
    args, _ = parser.parse_known_args(list(argv))
    return PreParsed(
        config=args.config,
        project_root=args.project_root,
        from_git=args.from_git,
        from_path=args.from_path,
        branch=args.branch,
        tag=args.tag,
        commit=args.commit,
        checkout_path=args.checkout_path,
        no_cache=args.no_cache,
        no_pull=args.no_pull,
        force_reset=args.force_reset,
        yes=args.yes,
    )


# ---------------------------------------------------------------------------
# Full parser — built dynamically once config is loaded
# ---------------------------------------------------------------------------


def _build_full_parser(
    *,
    hooks: tuple[HookConfig, ...],
    extra_processes: Sequence[Any],
    program: str,
) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=program,
        description="Orchestrate a local Django dev stack.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    src = parser.add_argument_group("Source")
    src.add_argument("--from-git", default=None, metavar="URL", help="Clone repo and run from it")
    src.add_argument("--from-path", default=None, metavar="PATH", help="Run from a local checkout")
    src.add_argument("--branch", default=None, metavar="BRANCH", help="Git branch (default HEAD)")
    src.add_argument("--commit", default=None, metavar="SHA", help="Git commit SHA")
    src.add_argument("--tag", default=None, metavar="TAG", help="Git tag")
    src.add_argument("--checkout-path", default=None, metavar="PATH", help="Where to clone")
    src.add_argument("--no-cache", action="store_true", help="Fresh tmp clone, cleanup on exit")
    src.add_argument("--no-pull", action="store_true", help="Don't `git pull` existing checkout")
    src.add_argument("--no-install", action="store_true", help="Skip venv setup + deps install")
    src.add_argument(
        "--force-reset",
        action="store_true",
        help="Allow destructive `git reset --hard` in user-owned checkout",
    )
    src.add_argument("--yes", "-y", action="store_true", help="Skip confirmation prompts (CI mode)")

    proj = parser.add_argument_group("Project")
    proj.add_argument(
        "--config", type=Path, default=None, help="Path to runsite.toml or pyproject.toml"
    )
    proj.add_argument("--project-root", type=Path, default=None, help="Project root override")
    proj.add_argument("--manage-py", type=Path, default=None, help="Path to manage.py")
    proj.add_argument("--python", type=Path, default=None, help="Path to Python executable")

    cont = parser.add_argument_group("Containers")
    cont.add_argument("--reuse", dest="reuse", action="store_true", default=False)
    cont.add_argument("--no-reuse", dest="reuse", action="store_false")
    cont.add_argument("--postgres-image", default=None, metavar="IMAGE")
    cont.add_argument("--redis-image", default=None, metavar="IMAGE")
    cont.add_argument(
        "--no-postgres",
        action="store_true",
        help="Don't pull / start a Postgres container. Use when the project "
        "uses SQLite or connects to an external DB.",
    )
    cont.add_argument(
        "--no-redis",
        action="store_true",
        help="Don't pull / start a Redis container.",
    )
    cont.add_argument(
        "--sqlite",
        dest="sqlite",
        action="store_true",
        default=None,
        help="Force-enable managed SQLite mode (overrides [sqlite].enabled).",
    )
    cont.add_argument(
        "--no-sqlite",
        dest="sqlite",
        action="store_false",
        help="Disable managed SQLite mode (overrides [sqlite].enabled).",
    )

    dump = parser.add_argument_group("Dump")
    dump.add_argument("--from-dump", dest="from_dump", type=Path, default=None, metavar="PATH")
    dump.add_argument("--no-dump", action="store_true")
    dump.add_argument(
        "--dump-strategy",
        choices=["auto", "init-script", "post-start"],
        default=None,
    )
    dump.add_argument("--restore-jobs", type=int, default=None, metavar="N")

    dj = parser.add_argument_group("Django")
    dj.add_argument("--port", type=int, default=None)
    dj.add_argument(
        "--bind",
        default=None,
        metavar="HOST",
        help=(
            "Address to bind runserver to. Falls back to RUN_SITE_BIND env "
            "var, then [django].runserver_bind in config (default 127.0.0.1)."
        ),
    )
    # --browser / --no-browser share dest so the last flag wins and the
    # default (None) means "fall through to [django].open_browser".
    browser_group = dj.add_mutually_exclusive_group()
    browser_group.add_argument("--browser", dest="browser", action="store_true", default=None)
    browser_group.add_argument("--no-browser", dest="browser", action="store_false")
    dj.add_argument("--no-migrate", action="store_true")
    dj.add_argument("--no-superuser", action="store_true")

    cel = parser.add_argument_group("Celery")
    cel.add_argument("--with-celery", dest="with_celery", action="store_true", default=None)
    cel.add_argument("--no-celery", dest="with_celery", action="store_false")
    cel.add_argument("--with-celery-beat", dest="with_beat", action="store_true", default=None)
    cel.add_argument("--no-celery-beat", dest="with_beat", action="store_false")

    # Banner display — sticky/non-sticky. Default (None) defers to
    # [banner].sticky in config, which itself defaults to "auto".
    banner_group = parser.add_argument_group("Banner")
    sticky_group = banner_group.add_mutually_exclusive_group()
    sticky_group.add_argument(
        "--sticky-banner",
        dest="sticky_banner",
        action="store_true",
        default=None,
        help="Pin the banner to the top of the terminal; logs scroll below it.",
    )
    sticky_group.add_argument(
        "--no-sticky-banner",
        dest="sticky_banner",
        action="store_false",
        help="Print the banner inline (legacy behavior).",
    )

    diag = parser.add_argument_group("Diagnostics")
    diag.add_argument("--dry-run", action="store_true")
    diag.add_argument("--print-env", action="store_true")
    diag.add_argument("--print-secrets", action="store_true")
    diag.add_argument("-v", "--verbose", action="count", default=0)

    # Extra processes — register --with-<name> / --no-<name>.
    if extra_processes:
        ep = parser.add_argument_group("Extra processes")
        for ep_def in extra_processes:
            with_flag = ep_def.cli_flag or f"--with-{ep_def.name}"
            disable_flag = ep_def.cli_disable_flag or f"--no-{ep_def.name}"
            dest = f"extra_{ep_def.name.replace('-', '_')}"
            ep.add_argument(with_flag, dest=dest, action="store_true", default=None)
            ep.add_argument(disable_flag, dest=dest, action="store_false")

    # Hook arguments (dynamic).
    if hooks:
        hooks_group = parser.add_argument_group("Hook arguments")
        added_disable_flags: set[str] = set()
        added_args: set[str] = set()
        for hook in hooks:
            for arg in hook.cli_args:
                if arg.flag in added_args:
                    continue
                added_args.add(arg.flag)
                kwargs: dict[str, Any] = {
                    "dest": f"hookopt_{arg.dest}",
                    "default": arg.default,
                    "required": arg.required,
                }
                if arg.metavar:
                    kwargs["metavar"] = arg.metavar
                if arg.help:
                    kwargs["help"] = arg.help
                hooks_group.add_argument(arg.flag, **kwargs)
            if hook.cli_disable_flag and hook.cli_disable_flag not in added_disable_flags:
                added_disable_flags.add(hook.cli_disable_flag)
                hooks_group.add_argument(
                    hook.cli_disable_flag,
                    dest=f"hookdisable_{hook.cli_disable_flag.lstrip('-').replace('-', '_')}",
                    action="store_true",
                    default=False,
                )

    return parser


# ---------------------------------------------------------------------------
# `run` flow
# ---------------------------------------------------------------------------


def _run_command(argv: Sequence[str]) -> int:
    try:
        return _run_command_inner(argv)
    except RunSiteError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return exc.exit_code


def _run_command_inner(argv: Sequence[str]) -> int:
    pre = _pre_parse(argv)
    _validate_source_flags_pre(pre)

    git_source: GitSource | None = None
    project_root_pre: Path | None = pre.project_root

    if pre.from_path is not None:
        project_root_pre = resolve_path_source(pre.from_path)
    elif pre.from_git is not None:
        git_source = resolve_git_source(
            url=pre.from_git,
            branch=pre.branch,
            tag=pre.tag,
            commit=pre.commit,
            checkout_path=pre.checkout_path,
            no_cache=pre.no_cache,
            no_pull=pre.no_pull,
            force_reset=pre.force_reset,
            yes=pre.yes,
        )
        project_root_pre = git_source.checkout_path

    cwd = Path.cwd()
    initial_root = discover_project_root(
        cli_root=project_root_pre,
        config_root=None,
        cwd=cwd,
    )
    config = load_config(config_path=pre.config, project_root=initial_root)

    # If config has a [source] but no CLI --from-* flag is given, follow it.
    if git_source is None and pre.from_path is None and config.source.type is not None:
        if config.source.type == "git" and config.source.url:
            git_source = resolve_git_source(
                url=config.source.url,
                branch=config.source.branch,
                tag=config.source.tag,
                commit=config.source.commit,
                checkout_path=config.source.checkout_path,
                no_cache=config.source.no_cache,
                no_pull=config.source.no_pull,
                force_reset=pre.force_reset,
                yes=pre.yes,
            )
            new_root = git_source.checkout_path
            config = load_config(config_path=pre.config, project_root=new_root)
        elif config.source.type == "path" and config.source.path:
            new_root = resolve_path_source(config.source.path)
            config = load_config(config_path=pre.config, project_root=new_root)

    parser = _build_full_parser(
        hooks=config.hooks,
        extra_processes=config.extra_processes,
        program="run-site run",
    )
    opts = parser.parse_args(list(argv))
    _configure_logging(opts.verbose)

    try:
        return _execute_run(config=config, opts=opts, git_source=git_source, parser=parser)
    finally:
        if git_source is not None:
            cleanup_temp_checkout(git_source)


def _validate_source_flags_pre(pre: PreParsed) -> None:
    if pre.from_git and pre.from_path:
        raise RunSiteError("--from-git and --from-path are mutually exclusive")
    if pre.from_path and (pre.branch or pre.tag or pre.commit):
        raise RunSiteError("--branch / --tag / --commit only apply to --from-git")
    if pre.from_path and (pre.no_cache or pre.no_pull):
        raise RunSiteError("--no-cache / --no-pull only apply to --from-git")


def _execute_run(
    *,
    config: RunSiteConfig,
    opts: argparse.Namespace,
    git_source: GitSource | None,
    parser: argparse.ArgumentParser,
) -> int:
    # Apply CLI overrides on top of config (postgres image, redis image, etc).
    config = _apply_cli_overrides(config, opts)

    # Venv setup happens BEFORE local Python discovery.
    # Skip entirely for --dry-run; users want to validate config without
    # touching the filesystem.
    if not opts.dry_run:
        venv_dir = config.project_root / ".venv"
        if not opts.no_install or venv_dir.exists():
            venv = ensure_venv(project_root=config.project_root, no_install=opts.no_install)
            install_dependencies(
                project_root=config.project_root,
                venv_dir=venv.venv_dir,
                no_install=opts.no_install,
            )

    manage_py = discover_manage_py(cli_manage=opts.manage_py, config=config)

    if opts.dry_run:
        # Resolve "auto" so the dry-run report shows the same enabled values
        # as a real run would produce.
        config = _resolve_services(config=config, manage_py=manage_py, mux=None)
        # Local Python may not exist yet for fresh checkouts, fall back gracefully.
        try:
            python = discover_local_python(cli_python=opts.python, config=config)
        except RunSiteError:
            python = (sys.executable,)
        return _dry_run_report(
            config=config,
            opts=opts,
            python=python,
            manage_py=manage_py,
            git_source=git_source,
        )

    python = discover_local_python(cli_python=opts.python, config=config)

    disabled_hooks = _collect_disabled_hooks(config.hooks, opts)
    hook_opts = _collect_hook_opts(config.hooks, opts)
    mux = LogMultiplexer()

    # Resolve "auto" using settings.py detection. Done after mux so we can
    # surface the detection notes immediately.
    config = _resolve_services(config=config, manage_py=manage_py, mux=mux)

    # Docker is only required when at least one container service will be
    # started. SQLite mode doesn't need Docker even if PG/Redis are both off.
    if config.postgres.enabled or config.redis.enabled:
        assert_docker_available()

    # Pre-containers hooks (host only).
    run_hooks(
        stage="pre_containers",
        hooks=config.hooks,
        context=build_hook_context(
            config=config,
            manage_py=manage_py,
            runserver_port=None,
            pg_host=None,
            pg_port=None,
            redis_host=None,
            redis_port=None,
            dump_path=None,
            reuse=opts.reuse,
            pg_created=None,
            redis_created=None,
            superuser=None,
            opts=hook_opts,
        ),
        python=None,
        manage_py=None,
        env=_baseline_env(),
        disabled_flags=disabled_hooks,
    )

    # Decide dump strategy. If init-script, we need the file mounted at PG
    # start, but we don't yet know if PG will be reused. Plan for fresh
    # creation; the post-start branch handles reuse.
    init_script = _maybe_init_script(config=config, opts=opts)

    sqlite_state: SqliteState | None = None
    if config.sqlite.enabled:
        sqlite_state = prepare_sqlite(
            config=config,
            reuse=opts.reuse,
            force_reset=getattr(opts, "force_reset", False),
        )
        mode = "persistent" if not sqlite_state.ephemeral else "ephemeral"
        mux.write("sqlite", "blue", f"[sqlite] {mode}: {sqlite_state.path}")
        if not sqlite_state.ephemeral:
            warning = gitignore_warning(project_root=config.project_root)
            if warning is not None:
                mux.write("sqlite", "yellow", f"[sqlite] WARNING: {warning}")

    containers = start_containers(config=config, reuse=opts.reuse, init_script=init_script)
    runserver_port = opts.port or find_free_port(config.django.runserver_bind)
    autologin_token = generate_autologin_token()
    # SECRET_KEY: read-or-generate-and-persist under .run-site/secret_key.
    # Skipped when the user explicitly disabled the export via
    # ``[env].secret_key = null`` — they're driving the value themselves.
    secret_key: str | None
    if config.env.mapping.get("secret_key", "<default>") is None:
        secret_key = None
    else:
        secret_key = load_or_generate_secret_key(config.project_root)

    proc_group = ProcessGroup(mux)

    try:
        endpoints = ContainerEndpoints(
            pg_host=containers.pg_host,
            pg_port=containers.pg_port,
            redis_host=containers.redis_host,
            redis_port=containers.redis_port,
            sqlite_path=str(sqlite_state.path) if sqlite_state else None,
        )
        # Discover DJANGO_SETTINGS_MODULE from manage.py so subprocesses
        # that don't go through manage.py — notably `python -m celery` —
        # still find Django settings.
        django_settings_module = discover_settings_module(manage_py=manage_py)
        # LAN hosts only matter when bind != loopback; cheap to discover
        # always so the env builder gets a stable input. ``compute_allowed_hosts``
        # short-circuits to ``()`` for loopback binds.
        lan_hosts = discover_lan_hosts()
        env_for_subprocess = build_subprocess_env(
            config=config,
            endpoints=endpoints,
            autologin_token=autologin_token,
            runserver_port=runserver_port,
            is_runserver=False,
            django_settings_module=django_settings_module,
            secret_key=secret_key,
            lan_hosts=lan_hosts,
        )
        env_for_runserver = build_subprocess_env(
            config=config,
            endpoints=endpoints,
            autologin_token=autologin_token,
            runserver_port=runserver_port,
            is_runserver=True,
            django_settings_module=django_settings_module,
            secret_key=secret_key,
            lan_hosts=lan_hosts,
        )

        if opts.print_env:
            print(format_env_for_print(env_for_runserver, redact=not opts.print_secrets))
            # Diagnostic mode — containers were started so the printed env
            # reflects real ports. Tear them down on the way out unless the
            # user asked to keep them alive for reuse.
            if not opts.reuse:
                with _suppress():
                    stop_containers(containers)
                with _suppress():
                    cleanup_sqlite(sqlite_state)
            return 0

        # post_containers hooks.
        ctx_post_containers = _hook_context(
            config=config,
            manage_py=manage_py,
            containers=containers,
            runserver_port=None,
            dump_path=None,
            superuser=None,
            opts=hook_opts,
        )
        run_hooks(
            stage="post_containers",
            hooks=config.hooks,
            context=ctx_post_containers,
            python=python,
            manage_py=manage_py,
            env=env_for_subprocess,
            disabled_flags=disabled_hooks,
        )

        # Dump (re-plan now that we know pg_created). When Postgres is
        # disabled we have no container to load a dump into; refuse loudly
        # if the user asked for one, otherwise just skip. Same applies to
        # SQLite mode — pg_dump-style restores don't apply.
        if not config.postgres.enabled:
            dump_was_requested = opts.from_dump is not None or config.dump.default_path is not None
            if dump_was_requested and not opts.no_dump:
                reason = (
                    "SQLite mode is active"
                    if config.sqlite.enabled
                    else "Postgres is disabled ([postgres].enabled = false or --no-postgres)"
                )
                raise RunSiteError(
                    f"{reason} but a dump was requested. Either drop "
                    "--from-dump / [dump].default_path, or pass --no-dump."
                )
            plan = None
        else:
            plan = plan_dump(
                config=config,
                cli_dump_path=opts.from_dump,
                cli_no_dump=opts.no_dump,
                cli_strategy_override=opts.dump_strategy,
                pg_created=bool(containers.pg_created),
            )
        run_hooks(
            stage="pre_dump",
            hooks=config.hooks,
            context=ctx_post_containers,
            python=python,
            manage_py=manage_py,
            env=env_for_subprocess,
            disabled_flags=disabled_hooks,
        )
        if plan is not None and plan.strategy == "post-start":
            # A non-None plan implies postgres.enabled (see above), so all
            # three values must be populated by start_containers.
            assert containers.pg_host is not None
            assert containers.pg_port is not None
            assert containers.pg_container_id is not None
            execute_post_start(
                plan,
                config=config,
                pg_host=containers.pg_host,
                pg_port=containers.pg_port,
                container_id=containers.pg_container_id,
            )
        elif plan is not None and plan.strategy == "skip":
            mux.write("dump", "yellow", f"[dump] skipped: {plan.reason}")
        run_hooks(
            stage="post_dump",
            hooks=config.hooks,
            context=ctx_post_containers,
            python=python,
            manage_py=manage_py,
            env=env_for_subprocess,
            disabled_flags=disabled_hooks,
        )

        # Migrate.
        if config.django.migrate and not opts.no_migrate:
            mux.write("migrate", "magenta", "[migrate] running migrations…")
            result = run_oneshot(
                (*python, str(manage_py), "migrate", "--noinput"),
                env=env_for_subprocess,
                cwd=config.project_root,
                capture_output=False,
            )
            if not result.ok:
                raise RunSiteError(f"migrate failed (exit {result.returncode})")

        run_hooks(
            stage="post_migrate",
            hooks=config.hooks,
            context=ctx_post_containers,
            python=python,
            manage_py=manage_py,
            env=env_for_subprocess,
            disabled_flags=disabled_hooks,
        )

        # Superuser.
        superuser_payload: dict[str, Any] | None = None
        if config.superuser.enabled and not opts.no_superuser:
            from run_site.superuser import setup_superuser

            mux.write("superuser", "magenta", "[superuser] ensuring dev account…")
            su = setup_superuser(
                config=config, python=python, manage_py=manage_py, env=env_for_subprocess
            )
            superuser_payload = asdict(su)
            ctx_with_su = _hook_context(
                config=config,
                manage_py=manage_py,
                containers=containers,
                runserver_port=runserver_port,
                dump_path=plan.path if plan else None,
                superuser=superuser_payload,
                opts=hook_opts,
            )
            run_hooks(
                stage="post_superuser",
                hooks=config.hooks,
                context=ctx_with_su,
                python=python,
                manage_py=manage_py,
                env=env_for_subprocess,
                disabled_flags=disabled_hooks,
            )

        # Sidecar — written before pre_serve so hooks (and django-dev-helpers
        # at runserver bootstrap) can read the runtime endpoints. Per-service
        # blocks are dropped when that service was disabled and not started.
        sidecar_path = write_sidecar(
            project_root=config.project_root,
            info=SidecarInfo(
                project_slug=config.project_slug,
                web_host=config.django.runserver_display_host,
                web_port=runserver_port,
                pg_host=containers.pg_host,
                pg_port=containers.pg_port,
                pg_db=config.postgres.db if config.postgres.enabled else None,
                pg_user=config.postgres.user if config.postgres.enabled else None,
                pg_password=(config.postgres.password if config.postgres.enabled else None),
                redis_host=containers.redis_host,
                redis_port=containers.redis_port,
                redis_db=config.redis.db if config.redis.enabled else None,
                celery_enabled=_celery_active(config, opts),
                celery_app=config.celery.app,
                sqlite_path=str(sqlite_state.path) if sqlite_state else None,
                sqlite_ephemeral=bool(sqlite_state and sqlite_state.ephemeral),
            ),
        )

        # pre_serve hooks.
        ctx_pre_serve = _hook_context(
            config=config,
            manage_py=manage_py,
            containers=containers,
            runserver_port=runserver_port,
            dump_path=plan.path if plan else None,
            superuser=superuser_payload,
            opts=hook_opts,
        )
        run_hooks(
            stage="pre_serve",
            hooks=config.hooks,
            context=ctx_pre_serve,
            python=python,
            manage_py=manage_py,
            env=env_for_subprocess,
            disabled_flags=disabled_hooks,
        )

        # Banner.
        # Binding to all interfaces means other LAN devices can reach this
        # server too — surface those URLs so the user doesn't have to guess
        # their LAN IP. For any other bind value the primary URL is already
        # the right one to advertise.
        if config.django.runserver_bind == "0.0.0.0":
            extra_hosts = tuple(
                host for host in lan_hosts if host != config.django.runserver_display_host
            )
            extra_app_urls = tuple(f"http://{host}:{runserver_port}/" for host in extra_hosts)
        else:
            extra_app_urls = ()
        homepage_url = f"http://{config.django.runserver_display_host}:{runserver_port}/"
        headless_signal = detect_headless_session()
        should_open_browser, browser_status = _resolve_browser_decision(
            config=config,
            cli_choice=getattr(opts, "browser", None),
            signal=headless_signal,
            homepage=homepage_url,
        )
        banner = render_banner(
            config=config,
            info=BannerInfo(
                appserver_url=f"http://{config.django.runserver_display_host}:{runserver_port}/",
                admin_url=f"http://{config.django.runserver_display_host}:{runserver_port}/admin/",
                pg_host=containers.pg_host,
                pg_port=containers.pg_port,
                redis_host=containers.redis_host,
                redis_port=containers.redis_port,
                celery_status=_celery_status_label(config, opts),
                dump_label=str(plan.path) if plan and plan.strategy != "skip" else None,
                source_kind="git" if git_source else ("path" if opts.from_path else None),
                source_url=(git_source.url if git_source else (opts.from_path or None)),
                source_ref=(
                    f"{git_source.ref_kind}={git_source.ref}"
                    if git_source and git_source.ref
                    else None
                ),
                source_checkout=str(git_source.checkout_path) if git_source else None,
                dev_helpers_installed=_dev_helpers_installed(python),
                reuse=opts.reuse,
                sidecar_path=sidecar_path,
                superuser=superuser_payload,
                sqlite_path=sqlite_state.path if sqlite_state else None,
                sqlite_ephemeral=bool(sqlite_state and sqlite_state.ephemeral),
                extra_app_urls=extra_app_urls,
                browser_status=browser_status,
            ),
        )
        sticky_enabled = _resolve_sticky_choice(opts, config)

        # Pin the banner to the top of the terminal before spawning any
        # subprocess so the mux threads' first lines land inside the
        # scroll region rather than racing the install.
        with StickyRegion(banner, enabled=sticky_enabled):
            # Spawn the web process — runserver by default, or whatever
            # `[django].web_command` overrides it with (daphne, gunicorn, …).
            web_argv = _build_web_argv(
                config=config,
                python=python,
                manage_py=manage_py,
                runserver_port=runserver_port,
            )
            proc_group.spawn(
                name="web",
                argv=web_argv,
                cwd=config.project_root,
                env=env_for_runserver,
                color="cyan",
            )

            if _celery_active(config, opts):
                if config.celery.app is None:
                    raise RunSiteError("Celery is requested but [celery].app is not set in config")
                proc_group.spawn(
                    name="celery",
                    argv=(
                        *python,
                        "-m",
                        "celery",
                        "-A",
                        config.celery.app,
                        "worker",
                        f"--pool={config.celery.worker_pool}",
                        "-l",
                        config.celery.worker_log_level,
                        *config.celery.worker_extra_args,
                    ),
                    cwd=config.project_root,
                    env=env_for_subprocess,
                    color="green",
                )
                with_beat = (
                    opts.with_beat if opts.with_beat is not None else config.celery.with_beat
                )
                if with_beat:
                    proc_group.spawn(
                        name="celery-beat",
                        argv=(
                            *python,
                            "-m",
                            "celery",
                            "-A",
                            config.celery.app,
                            "beat",
                            "-l",
                            config.celery.beat_log_level,
                            *config.celery.beat_extra_args,
                        ),
                        cwd=config.project_root,
                        env=env_for_subprocess,
                        color="magenta",
                    )

            # Extra processes.
            tmpl = TemplateContext(
                python=python,
                manage_py=manage_py,
                manage_dir=manage_py.parent,
                project_root=config.project_root,
                port=runserver_port,
            )
            for ep in config.extra_processes:
                dest = f"extra_{ep.name.replace('-', '_')}"
                chosen = getattr(opts, dest, None)
                enabled = chosen if chosen is not None else ep.enabled_default
                if not enabled:
                    continue
                proc_group.spawn(
                    name=ep.name,
                    argv=tmpl.expand(ep.command),
                    cwd=config.project_root / ep.cwd,
                    env=env_for_subprocess,
                    color=ep.color,
                )

            # docker logs -f for PG (if requested and PG was actually started).
            if (
                config.postgres.enabled
                and config.postgres.stream_logs
                and containers.pg_container_id is not None
            ):
                argv = docker_logs_follow(containers.pg_container_id)
                if argv:
                    proc_group.spawn(
                        name="pg",
                        argv=argv,
                        cwd=config.project_root,
                        env=_baseline_env(),
                        color="yellow",
                    )

            # Probe + browser open. The probe exists only to gate the browser
            # open on a 2xx — when we've already decided not to open, there's
            # nothing left to wait for.
            if should_open_browser:
                probe_url = (
                    f"http://{config.django.runserver_display_host}:{runserver_port}"
                    f"{config.django.browser_probe_path}"
                )
                probe_thread = threading.Thread(
                    target=_probe_and_open_browser,
                    args=(probe_url, homepage_url, config),
                    daemon=True,
                )
                probe_thread.start()

            signal.signal(signal.SIGINT, lambda *_: _trigger_shutdown(proc_group))
            signal.signal(signal.SIGTERM, lambda *_: _trigger_shutdown(proc_group))
            proc_group.wait_any()
        return _shutdown(
            proc_group=proc_group,
            containers=containers,
            sqlite_state=sqlite_state,
            opts=opts,
            python=python,
            manage_py=manage_py,
            config=config,
            ctx=ctx_pre_serve,
            disabled_hooks=disabled_hooks,
            env=env_for_subprocess,
        )
    except Exception:
        proc_group.terminate_all()
        with _suppress():
            stop_containers(containers)
        with _suppress():
            cleanup_sqlite(sqlite_state)
        with _suppress():
            remove_sidecar(project_root=config.project_root)
        raise


def _trigger_shutdown(proc_group: ProcessGroup) -> None:
    proc_group.terminate_all()


def _shutdown(
    *,
    proc_group: ProcessGroup,
    containers: RunSiteContainers,
    sqlite_state: SqliteState | None,
    opts: argparse.Namespace,
    python: tuple[str, ...],
    manage_py: Path,
    config: RunSiteConfig,
    ctx,
    disabled_hooks: set[str],
    env,
) -> int:
    proc_group.terminate_all()
    with _suppress():
        run_hooks(
            stage="post_stop",
            hooks=config.hooks,
            context=ctx,
            python=python,
            manage_py=manage_py,
            env=env,
            disabled_flags=disabled_hooks,
            best_effort=True,
        )
    if not opts.reuse:
        with _suppress():
            stop_containers(containers)
        with _suppress():
            cleanup_sqlite(sqlite_state)
    with _suppress():
        remove_sidecar(project_root=config.project_root)
    primary = proc_group.primary()
    if primary is not None and primary.returncode is not None:
        return primary.returncode
    return 0


def _resolve_browser_decision(
    *,
    config: RunSiteConfig,
    cli_choice: bool | None,
    signal: HeadlessSignal,
    homepage: str,
) -> tuple[bool, str]:
    """Return ``(should_open, banner_status)``.

    Precedence: explicit CLI flag > config setting > headless auto-detect.
    The banner status string always explains what was decided and why,
    so users never wonder "did it skip on purpose or fail silently?".
    """

    if cli_choice is True:
        return True, f"will open {homepage} (forced by --browser)"
    if cli_choice is False:
        return False, "disabled by --no-browser"

    setting = config.django.open_browser
    if setting is True:
        return True, f"will open {homepage} ([django].open_browser = true)"
    if setting is False:
        return False, "disabled by [django].open_browser = false"

    if signal.headless:
        return False, f"skipped — {signal.reason} (pass --browser to override)"
    return True, f"will open {homepage} ({signal.reason})"


def _probe_and_open_browser(url: str, homepage: str, config: RunSiteConfig) -> None:
    if not wait_for_http(url, timeout=config.django.probe_timeout):
        return
    try:
        import webbrowser

        webbrowser.open(homepage)
    except Exception:  # pragma: no cover - browser-open is best-effort
        logger.exception("Failed to open browser at %s", homepage)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_web_argv(
    *,
    config: RunSiteConfig,
    python: tuple[str, ...],
    manage_py: Path,
    runserver_port: int,
) -> tuple[str, ...]:
    """Pick the argv for the web process.

    Default: ``<python> manage.py runserver <bind>:<port>`` — Django's
    builtin dev server with autoreload.

    With ``[django].web_command`` set: substitute ``{python}``,
    ``{manage_py}``, ``{manage_dir}``, ``{project_root}``, ``{port}``,
    ``{bind}`` into the configured tokens and run that instead. Useful
    for ASGI servers (daphne, uvicorn) or production-style runners
    (gunicorn) when ``runserver`` isn't enough.
    """

    if config.django.web_command is None:
        return (
            *python,
            str(manage_py),
            "runserver",
            f"{config.django.runserver_bind}:{runserver_port}",
        )
    tmpl = TemplateContext(
        python=python,
        manage_py=manage_py,
        manage_dir=manage_py.parent,
        project_root=config.project_root,
        port=runserver_port,
        extras={"bind": config.django.runserver_bind},
    )
    return tmpl.expand(config.django.web_command)


def _apply_cli_overrides(config: RunSiteConfig, opts: argparse.Namespace) -> RunSiteConfig:
    from dataclasses import replace

    pg = config.postgres
    if opts.postgres_image:
        pg = replace(pg, image=opts.postgres_image)
    if getattr(opts, "no_postgres", False):
        pg = replace(pg, enabled=False)
    redis = config.redis
    if opts.redis_image:
        redis = replace(redis, image=opts.redis_image)
    if getattr(opts, "no_redis", False):
        redis = replace(redis, enabled=False)
    sqlite = config.sqlite
    sqlite_opt = getattr(opts, "sqlite", None)
    if sqlite_opt is True:
        sqlite = replace(sqlite, enabled=True)
    elif sqlite_opt is False:
        sqlite = replace(sqlite, enabled=False)
    dj = config.django
    # Bind precedence: --bind CLI flag > RUN_SITE_BIND env > config default.
    # Env-var slot lets a developer set ``RUN_SITE_BIND=0.0.0.0`` once in
    # their shell profile and have every project bind to LAN by default,
    # without editing each project's runsite.toml. Empty string is
    # treated as unset.
    bind = opts.bind or os.environ.get("RUN_SITE_BIND") or None
    if bind:
        # An explicit bind should also drive the banner's clickable URL,
        # otherwise we'd print "http://localhost:…" while runserver advertises
        # the bind host. 0.0.0.0 is a listen-everywhere sentinel that isn't
        # itself browseable, so fall back to localhost in that case.
        display_host = "localhost" if bind == "0.0.0.0" else bind
        dj = replace(dj, runserver_bind=bind, runserver_display_host=display_host)
    dump = config.dump
    if opts.restore_jobs is not None:
        dump = replace(dump, restore_jobs=opts.restore_jobs)
    # Fold [source].no_install into opts.no_install so a single boolean
    # drives venv setup downstream.
    if config.source.no_install and not opts.no_install:
        opts.no_install = True
    return replace(config, postgres=pg, redis=redis, sqlite=sqlite, django=dj, dump=dump)


def _resolve_services(
    *,
    config: RunSiteConfig,
    manage_py: Path,
    mux: LogMultiplexer | None,
) -> RunSiteConfig:
    """Resolve ``"auto"`` ``enabled`` fields by scanning settings.py.

    Detection runs in two passes — token scan (``django.db.backends.*``,
    URLs) plus env-var scan (``DATABASE_URL``, ``REDIS_URL`` lookups).
    The env-var pass rescues projects whose settings.py only references
    backends through ``env.db_url(...)``, where the URL — and therefore
    the engine name — never appears as a literal in source.

    Idempotent for configs with no remaining ``"auto"`` values: still
    calls :func:`resolve_auto_enabled` which is a cheap no-op then.
    """

    needs_detection = (
        config.postgres.enabled == "auto"
        or config.redis.enabled == "auto"
        or config.sqlite.enabled == "auto"
    )
    detected = None
    required_env_vars: set[str] | None = None
    if needs_detection:
        detected = detect_services_from_settings(
            manage_py=manage_py,
            project_root=config.project_root,
        )
        required_env_vars = detect_required_env_vars(
            manage_py=manage_py,
            project_root=config.project_root,
        )
    resolved, notes = resolve_auto_enabled(
        config, detected=detected, required_env_vars=required_env_vars
    )
    if mux is not None:
        for note in notes:
            mux.write("config", "blue", f"[config] {note}")
    return resolved


def _maybe_init_script(*, config, opts) -> Path | None:
    # No PG container means no place to mount an init script — and any
    # configured dump simply has nowhere to land.
    if not config.postgres.enabled:
        return None
    plan = plan_dump(
        config=config,
        cli_dump_path=opts.from_dump,
        cli_no_dump=opts.no_dump,
        cli_strategy_override=opts.dump_strategy,
        pg_created=True,  # pre-emptively assume fresh
    )
    if plan is None or plan.strategy != "init-script":
        return None
    return plan.path


def _baseline_env() -> dict[str, str]:
    import os

    env = dict(os.environ)
    env.setdefault("PYTHONUNBUFFERED", "1")
    return env


def _hook_context(*, config, manage_py, containers, runserver_port, dump_path, superuser, opts):
    return build_hook_context(
        config=config,
        manage_py=manage_py,
        runserver_port=runserver_port,
        pg_host=containers.pg_host,
        pg_port=containers.pg_port,
        redis_host=containers.redis_host,
        redis_port=containers.redis_port,
        dump_path=dump_path,
        reuse=containers.reuse,
        pg_created=containers.pg_created,
        redis_created=containers.redis_created,
        superuser=superuser,
        opts=opts,
    )


def _collect_disabled_hooks(hooks: tuple[HookConfig, ...], opts: argparse.Namespace) -> set[str]:
    disabled: set[str] = set()
    for hook in hooks:
        if hook.cli_disable_flag is None:
            continue
        attr = f"hookdisable_{hook.cli_disable_flag.lstrip('-').replace('-', '_')}"
        if getattr(opts, attr, False):
            disabled.add(hook.cli_disable_flag)
    return disabled


def _collect_hook_opts(hooks: tuple[HookConfig, ...], opts: argparse.Namespace) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for hook in hooks:
        for arg in hook.cli_args:
            attr = f"hookopt_{arg.dest}"
            if hasattr(opts, attr):
                out[arg.dest] = getattr(opts, attr)
    return out


def _resolve_sticky_choice(opts: argparse.Namespace, config: RunSiteConfig) -> bool:
    """Effective sticky-banner on/off.

    Precedence: CLI flag (``--sticky-banner`` / ``--no-sticky-banner``)
    wins; then ``[banner].sticky`` (``"auto"`` / ``"always"`` / ``"never"``);
    the actual TTY check happens inside ``StickyRegion`` so passing ``True``
    on a non-TTY stream still degrades gracefully to inline printing.
    """

    cli = getattr(opts, "sticky_banner", None)
    if cli is not None:
        return bool(cli)
    return config.banner.sticky != "never"


def _celery_active(config: RunSiteConfig, opts: argparse.Namespace) -> bool:
    """Effective Celery on/off: ``--with-celery`` forces it on (when an
    app is configured), ``--no-celery`` forces it off, otherwise follow
    ``[celery].enabled``."""

    if opts.with_celery is True:
        return config.celery.app is not None
    if opts.with_celery is False:
        return False
    return config.celery.enabled


def _celery_status_label(config: RunSiteConfig, opts: argparse.Namespace) -> str:
    if not _celery_active(config, opts):
        return "disabled"
    parts = [f"running --pool={config.celery.worker_pool}"]
    with_beat = opts.with_beat if opts.with_beat is not None else config.celery.with_beat
    if with_beat:
        parts.append("+ beat")
    return " ".join(parts)


def _dev_helpers_installed(python: tuple[str, ...]) -> bool:
    """Probe the project's Python for ``django_dev_helpers``."""

    try:
        result = run_oneshot(
            (
                *python,
                "-c",
                "import importlib.util, sys; "
                "sys.exit(0 if importlib.util.find_spec('django_dev_helpers') else 1)",
            ),
            capture_output=True,
            timeout=10,
        )
    except subprocess.TimeoutExpired:
        return False
    except FileNotFoundError:
        return False
    return result.ok


def _dry_run_report(
    *,
    config: RunSiteConfig,
    opts: argparse.Namespace,
    python: tuple[str, ...],
    manage_py: Path,
    git_source: GitSource | None,
) -> int:
    sys.stdout.write("=== run-site dry-run ===\n")
    sys.stdout.write(f"project_slug:   {config.project_slug}\n")
    sys.stdout.write(f"project_root:   {config.project_root}\n")
    sys.stdout.write(f"config_path:    {config.config_path}\n")
    sys.stdout.write(f"manage_py:      {manage_py}\n")
    sys.stdout.write(f"python:         {' '.join(python)}\n")
    sys.stdout.write(f"reuse:          {opts.reuse}\n")
    if config.postgres.enabled:
        sys.stdout.write(f"postgres image: {config.postgres.image}\n")
    else:
        sys.stdout.write("postgres image: <disabled>\n")
    if config.redis.enabled:
        sys.stdout.write(f"redis image:    {config.redis.image}\n")
    else:
        sys.stdout.write("redis image:    <disabled>\n")
    if config.sqlite.enabled:
        sqlite_mode = "persistent (--reuse)" if opts.reuse else "ephemeral"
        sys.stdout.write(f"sqlite:         {sqlite_mode}\n")
    else:
        sys.stdout.write("sqlite:         <disabled>\n")
    sys.stdout.write(f"celery enabled: {config.celery.enabled}\n")
    sys.stdout.write(f"hooks:          {len(config.hooks)} declared\n")
    if git_source is not None:
        sys.stdout.write(
            f"source: git {git_source.url} ref={git_source.ref_kind}={git_source.ref} "
            f"checkout={git_source.checkout_path}\n"
        )
    if opts.from_path:
        sys.stdout.write(f"source: path {opts.from_path}\n")
    sys.stdout.write("=== end ===\n")
    return 0


# ---------------------------------------------------------------------------
# `doctor` command
# ---------------------------------------------------------------------------


def _doctor_command(argv: Sequence[str]) -> int:
    pre = _pre_parse(argv)
    project_root_pre = pre.project_root
    git_source: GitSource | None = None
    try:
        if pre.from_path is not None:
            project_root_pre = resolve_path_source(pre.from_path)
        elif pre.from_git is not None:
            git_source = resolve_git_source(
                url=pre.from_git,
                branch=pre.branch,
                tag=pre.tag,
                commit=pre.commit,
                checkout_path=pre.checkout_path,
                no_cache=pre.no_cache,
                no_pull=pre.no_pull,
                force_reset=pre.force_reset,
                yes=pre.yes,
            )
            project_root_pre = git_source.checkout_path

        cwd = Path.cwd()
        root = discover_project_root(cli_root=project_root_pre, config_root=None, cwd=cwd)
        config = load_config(config_path=pre.config, project_root=root)
        manage_py = discover_manage_py(cli_manage=None, config=config)
        python = discover_local_python(cli_python=None, config=config)

        sys.stdout.write("=== run-site doctor ===\n")
        sys.stdout.write(f"project_root:   {config.project_root}\n")
        sys.stdout.write(f"config_path:    {config.config_path}\n")
        sys.stdout.write(f"manage_py:      {manage_py}\n")
        sys.stdout.write(f"python command: {' '.join(python)}\n")

        sys.stdout.write("\n[1/4] Probing manage.py --help…\n")
        result = run_oneshot((*python, str(manage_py), "--help"), capture_output=True, timeout=30)
        if result.ok:
            sys.stdout.write("    OK\n")
        else:
            sys.stdout.write(f"    FAILED (exit {result.returncode})\n")
            sys.stdout.write(result.stderr)
            return 1

        sys.stdout.write("\n[2/4] Checking Docker daemon…\n")
        try:
            assert_docker_available()
            sys.stdout.write("    OK\n")
        except RunSiteError as exc:
            sys.stdout.write(f"    FAILED: {exc}\n")
            return exc.exit_code

        sys.stdout.write("\n[3/4] Checking git availability…\n")
        if shutil.which("git") is None:
            sys.stdout.write("    Not on PATH (only needed for --from-git).\n")
        else:
            sys.stdout.write("    OK\n")

        sys.stdout.write("\n[4/4] Checking uv / pip availability for venv setup…\n")
        if shutil.which("uv") is not None:
            sys.stdout.write("    uv: OK\n")
        else:
            sys.stdout.write("    uv: not found (will fall back to python -m venv)\n")

        sys.stdout.write("\nAll checks passed.\n")
        return 0
    except RunSiteError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return exc.exit_code
    finally:
        if git_source is not None:
            cleanup_temp_checkout(git_source)


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------


def _configure_logging(verbosity: int) -> None:
    level = logging.WARNING
    if verbosity == 1:
        level = logging.INFO
    elif verbosity >= 2:
        level = logging.DEBUG
    logging.basicConfig(
        level=level,
        format="%(levelname)s [%(name)s] %(message)s",
    )


from contextlib import contextmanager  # noqa: E402


@contextmanager
def _suppress():  # type: ignore[no-untyped-def]
    """Like contextlib.suppress(Exception) but logs the exception."""

    try:
        yield
    except Exception:
        logger.exception("Suppressed exception during shutdown / cleanup")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
