"""Render the orchestrator banner (§18.2)."""

from __future__ import annotations

from dataclasses import dataclass

from django_run_site.config import RunSiteConfig
from django_run_site.log_multiplexer import ANSI_RESET, COLOR_CODES, _color_supported


@dataclass(frozen=True)
class BannerInfo:
    """Everything the banner needs, gathered by the run flow."""

    appserver_url: str
    admin_url: str
    pg_host: str
    pg_port: int
    redis_host: str
    redis_port: int
    celery_status: str
    dump_label: str | None
    source_kind: str | None  # "git" | "path" | None
    source_url: str | None  # git URL or absolute path
    source_ref: str | None  # branch/tag/commit, when --from-git
    source_checkout: str | None  # filesystem path of clone
    dev_helpers_installed: bool


def render_banner(*, config: RunSiteConfig, info: BannerInfo) -> str:
    """Return a fully-formed banner string ready to write to stdout."""

    use_color = _color_supported(__import__("sys").stdout)
    bold = COLOR_CODES["bold"] if use_color else ""
    cyan = COLOR_CODES["cyan"] if use_color else ""
    green = COLOR_CODES["green"] if use_color else ""
    yellow = COLOR_CODES["yellow"] if use_color else ""
    gray = COLOR_CODES["gray"] if use_color else ""
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
    lines.append(f"  {bold}{yellow}Postgres:{reset} {info.pg_host}:{info.pg_port}")
    if config.banner.show_db_credentials:
        lines.append(
            f"           db={config.postgres.db}  user={config.postgres.user}  "
            f"password={config.postgres.password}"
        )
    lines.append(f"  {bold}{yellow}Redis:{reset}    {info.redis_host}:{info.redis_port}")
    lines.append(f"  {bold}Celery:{reset}   {info.celery_status}")
    if info.dump_label is not None:
        lines.append(f"  {bold}Dump:{reset}     {info.dump_label}")
    lines.append("")

    if config.banner.suggest_dev_helpers and not info.dev_helpers_installed:
        lines.append(f"  {gray}[tip] Install django-dev-helpers for autologin + dotfiles:{reset}")
        lines.append(f"  {gray}      uv add django-dev-helpers --group dev{reset}")
        lines.append(f"  {gray}      Then add 'django_dev_helpers' to INSTALLED_APPS.{reset}")
        lines.append("")

    lines.append(f"{bold}{cyan}{bar}{reset}")
    return "\n".join(lines) + "\n"
