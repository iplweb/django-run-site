"""Render the orchestrator banner."""

from __future__ import annotations

import shlex
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from run_site.config import RunSiteConfig
from run_site.log_multiplexer import ANSI_RESET, COLOR_CODES, _color_supported


@dataclass(frozen=True)
class BannerInfo:
    """Everything the banner needs, gathered by the run flow.

    ``pg_*`` / ``redis_*`` are ``None`` when the corresponding service
    was disabled in config; the banner omits those rows entirely.
    """

    appserver_url: str
    admin_url: str
    pg_host: str | None
    pg_port: int | None
    redis_host: str | None
    redis_port: int | None
    celery_status: str
    dump_label: str | None
    source_kind: str | None  # "git" | "path" | None
    source_url: str | None  # git URL or absolute path
    source_ref: str | None  # branch/tag/commit, when --from-git
    source_checkout: str | None  # filesystem path of clone
    dev_helpers_installed: bool
    reuse: bool = False
    sidecar_path: Path | None = None
    # ``{"username", "email", "created"}`` from setup_superuser, or None when
    # superuser setup was skipped (config disabled or ``--no-superuser``).
    superuser: Mapping[str, object] | None = None


def render_banner(*, config: RunSiteConfig, info: BannerInfo) -> str:
    """Return a fully-formed banner string ready to write to stdout."""

    use_color = _color_supported(__import__("sys").stdout)
    bold = COLOR_CODES["bold"] if use_color else ""
    cyan = COLOR_CODES["cyan"] if use_color else ""
    green = COLOR_CODES["green"] if use_color else ""
    yellow = COLOR_CODES["yellow"] if use_color else ""
    gray = COLOR_CODES["gray"] if use_color else ""
    red = COLOR_CODES["red"] if use_color else ""
    reset = ANSI_RESET if use_color else ""

    lines: list[str] = []
    title = config.banner.title
    bar = "═" * max(40, len(title) + 4)
    lines.append(f"{bold}{cyan}{bar}{reset}")
    lines.append(f"{bold}{cyan}  {title}{reset}")
    lines.append(f"{bold}{cyan}{bar}{reset}")
    lines.append("")
    lines.append(f"  {bold}Project:{reset}  {config.project_slug}")
    lines.append(f"  {bold}Root:{reset}     {config.project_root}")
    if info.source_kind == "git":
        lines.append(f"  {bold}Source:{reset}   git {info.source_url}")
        if info.source_ref:
            lines.append(f"  {bold}Ref:{reset}      {info.source_ref}")
        if info.source_checkout:
            lines.append(f"  {bold}Checkout:{reset} {info.source_checkout}")
    elif info.source_kind == "path":
        lines.append(f"  {bold}Source:{reset}   path {info.source_url}")
    lines.append("")
    lines.append(f"  {bold}{green}App:{reset}     {info.appserver_url}")
    lines.append(f"  {bold}{green}Admin:{reset}   {info.admin_url}")
    lines.extend(_render_superuser(info=info, config=config, bold=bold, gray=gray, reset=reset))
    if info.pg_host is not None and info.pg_port is not None:
        lines.append(f"  {bold}{yellow}Postgres:{reset} {info.pg_host}:{info.pg_port}")
        if config.banner.show_db_credentials:
            lines.append(
                f"           db={config.postgres.db}  user={config.postgres.user}  "
                f"password={config.postgres.password}"
            )
            lines.extend(_render_postgres_helpers(info=info, config=config, gray=gray, reset=reset))
    else:
        lines.append(f"  {bold}{yellow}Postgres:{reset} {gray}disabled{reset}")
    if info.redis_host is not None and info.redis_port is not None:
        lines.append(f"  {bold}{yellow}Redis:{reset}    {info.redis_host}:{info.redis_port}")
    else:
        lines.append(f"  {bold}{yellow}Redis:{reset}    {gray}disabled{reset}")
    lines.extend(
        _render_lifecycle(info=info, config=config, gray=gray, bold=bold, red=red, reset=reset)
    )
    lines.append(f"  {bold}Celery:{reset}   {info.celery_status}")
    if not config.celery.enabled:
        lines.extend(_render_celery_enable_hint(gray=gray, reset=reset))
    if info.dump_label is not None:
        lines.append(f"  {bold}Dump:{reset}     {info.dump_label}")
    if info.sidecar_path is not None:
        lines.append(
            f"  {bold}Sidecar:{reset}  {info.sidecar_path} {gray}(removed on shutdown){reset}"
        )
    lines.append("")

    if config.banner.suggest_dev_helpers and not info.dev_helpers_installed:
        lines.append(f"  {gray}[tip] Install django-dev-helpers for autologin + dotfiles:{reset}")
        lines.append(f"  {gray}      uv add django-dev-helpers --group dev{reset}")
        lines.append(f"  {gray}      Then add 'django_dev_helpers' to INSTALLED_APPS.{reset}")
        lines.append("")

    lines.append(f"{bold}{cyan}{bar}{reset}")
    return "\n".join(lines) + "\n"


def _render_postgres_helpers(
    *, info: BannerInfo, config: RunSiteConfig, gray: str, reset: str
) -> list[str]:
    """Return the ``psql`` command + libpq env-var lines for the banner.

    Both forms are useful: the command is one-shot copy-paste, and the
    env-var line is what you ``export`` once for a shell session before
    running ``psql`` / ``pg_dump`` repeatedly.
    """

    db = config.postgres.db
    user = config.postgres.user
    password = config.postgres.password

    # _render_postgres_helpers is only invoked after callers verified that
    # info.pg_host and info.pg_port are non-None.
    assert info.pg_host is not None and info.pg_port is not None
    psql_cmd = " ".join(
        [
            f"PGPASSWORD={shlex.quote(password)}",
            "psql",
            "-h",
            shlex.quote(info.pg_host),
            "-p",
            str(info.pg_port),
            "-U",
            shlex.quote(user),
            "-d",
            shlex.quote(db),
        ]
    )
    env_line = " ".join(
        [
            f"PGHOST={info.pg_host}",
            f"PGPORT={info.pg_port}",
            f"PGDATABASE={db}",
            f"PGUSER={user}",
            f"PGPASSWORD={password}",
        ]
    )
    return [
        f"           {gray}psql:{reset} {psql_cmd}",
        f"           {gray}env:{reset}  {env_line}",
    ]


def _render_superuser(
    *, info: BannerInfo, config: RunSiteConfig, bold: str, gray: str, reset: str
) -> list[str]:
    """Render the dev superuser status — credentials shown only when the
    password we'd see is actually the password the user has.

    Three states:

    * **No superuser** (config disabled or ``--no-superuser``): single
      "disabled" line so the user knows admin login isn't pre-baked.
    * **Created or reset** (overwrite=true, the default): show username +
      password, since both match what's in the DB right now.
    * **Existing, untouched** (overwrite=false, user already existed):
      show username only — we can't claim to know the password.
    """

    if info.superuser is None:
        return [
            f"  {bold}Superuser:{reset} {gray}disabled "
            f"({reset}--no-superuser{gray} or [superuser].enabled = false){reset}"
        ]

    username = str(info.superuser.get("username", "?"))
    email = str(info.superuser.get("email", ""))
    created = bool(info.superuser.get("created", False))
    overwrite = config.superuser.overwrite
    show_secrets = config.banner.show_db_credentials
    password = config.superuser.password

    if created:
        status = f"{bold}created{reset}"
        creds = f"{username} / {password}" if show_secrets else username
    elif overwrite:
        status = f"{gray}existing — password reset to dev default{reset}"
        creds = f"{username} / {password}" if show_secrets else username
    else:
        status = f"{gray}existing — password unchanged ([superuser].overwrite = false){reset}"
        creds = username

    out = [f"  {bold}Superuser:{reset} {creds}  ({status})"]
    if email:
        out.append(f"             {gray}email={email}{reset}")
    return out


def _render_lifecycle(
    *, info: BannerInfo, config: RunSiteConfig, gray: str, bold: str, red: str, reset: str
) -> list[str]:
    """Tell the user whether Postgres / Redis will survive after exit.

    With ``--reuse`` containers persist (named ``<slug>-runsite-{pg,redis}``)
    so subsequent runs reattach. Without it, ``stop_containers`` runs on
    shutdown and the data is gone — important to know before you load a
    big dump.

    A service that was disabled in config (e.g. SQLite-only / cache-less)
    is omitted from the wording entirely so we don't talk about removing
    a container that was never started. When *both* are disabled the
    whole Lifecycle line is suppressed — there's nothing whose lifecycle
    we control.
    """

    pg_on = config.postgres.enabled
    redis_on = config.redis.enabled
    if not pg_on and not redis_on:
        return []

    if pg_on and redis_on:
        services = "Postgres + Redis"
    elif pg_on:
        services = "Postgres"
    else:
        services = "Redis"

    if info.reuse:
        slug = config.project_slug
        names = []
        if pg_on:
            names.append(f"{slug}-runsite-pg")
        if redis_on:
            names.append(f"{slug}-runsite-redis")
        return [
            f"  {bold}Lifecycle:{reset} {services} will be {bold}kept{reset} after "
            f"exit ({gray}--reuse{reset}).",
            f"             {gray}Drop --reuse for a clean run, or remove with:{reset}",
            f"             {gray}  docker rm -f {' '.join(names)}{reset}",
        ]
    return [
        f"  {red}{bold}Lifecycle:{reset}{red} {services} will be "
        f"{bold}removed{reset}{red} on exit.{reset}",
        f"             {gray}Pass --reuse to keep "
        f"{'them' if pg_on and redis_on else 'it'} between runs "
        f"(faster restart, dump preserved).{reset}",
    ]


def _render_celery_enable_hint(*, gray: str, reset: str) -> list[str]:
    """Hint shown when the config has Celery disabled.

    We deliberately don't show this for ``--no-celery`` on an enabled
    config — a user typing that flag already knows how to undo it.
    """

    return [
        f"           {gray}[tip] enable Celery in runsite.toml:{reset}",
        f"           {gray}        [celery]{reset}",
        f"           {gray}        enabled = true{reset}",
        f'           {gray}        app = "<your_django_module>.celery"{reset}',
        f"           {gray}      then re-run (use --no-celery to skip per-run).{reset}",
    ]
