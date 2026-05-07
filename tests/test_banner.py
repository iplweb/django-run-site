"""Tests for the orchestrator banner."""

from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path

import pytest

from run_site.banner import BannerInfo, render_banner
from run_site.config import RunSiteConfig

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(s: str) -> str:
    return ANSI_RE.sub("", s)


def _make_info(**overrides) -> BannerInfo:
    base: dict = dict(
        appserver_url="http://localhost:8123/",
        admin_url="http://localhost:8123/admin/",
        pg_host="127.0.0.1",
        pg_port=54321,
        redis_host="127.0.0.1",
        redis_port=16379,
        celery_status="disabled",
        dump_label=None,
        source_kind=None,
        source_url=None,
        source_ref=None,
        source_checkout=None,
        dev_helpers_installed=True,
        sidecar_path=None,
        superuser={"username": "admin", "email": "admin@example.com", "created": True},
    )
    base.update(overrides)
    return BannerInfo(**base)


@pytest.fixture
def config(minimal_config: RunSiteConfig) -> RunSiteConfig:
    """Reuse the minimal_config fixture from conftest.py."""

    return minimal_config


def test_banner_includes_psql_command_with_libpq_env(config: RunSiteConfig) -> None:
    out = _strip_ansi(render_banner(config=config, info=_make_info()))

    assert "PGPASSWORD=demo-pwd psql -h 127.0.0.1 -p 54321 -U demo -d demo" in out
    assert "PGHOST=127.0.0.1" in out
    assert "PGPORT=54321" in out
    assert "PGDATABASE=demo" in out
    assert "PGUSER=demo" in out
    assert "PGPASSWORD=demo-pwd" in out


def test_banner_psql_quotes_passwords_with_special_chars(
    config: RunSiteConfig,
) -> None:
    cfg = replace(config, postgres=replace(config.postgres, password="p@ss w0rd!"))
    out = _strip_ansi(render_banner(config=cfg, info=_make_info()))
    # shlex.quote should single-quote anything with shell meta-chars.
    assert "PGPASSWORD='p@ss w0rd!' psql" in out


def test_banner_omits_psql_helpers_when_credentials_hidden(
    config: RunSiteConfig,
) -> None:
    cfg = replace(config, banner=replace(config.banner, show_db_credentials=False))
    out = _strip_ansi(render_banner(config=cfg, info=_make_info()))
    assert "PGPASSWORD=" not in out
    assert "psql -h" not in out


def test_banner_celery_enable_hint_when_disabled(config: RunSiteConfig) -> None:
    """Default minimal_config has celery.enabled = False."""

    out = _strip_ansi(render_banner(config=config, info=_make_info()))

    assert "[tip] enable Celery" in out
    assert "[celery]" in out
    assert "enabled = true" in out
    assert "<your_django_module>.celery" in out


def test_banner_no_celery_hint_when_enabled(config: RunSiteConfig) -> None:
    cfg = replace(
        config, celery=replace(config.celery, enabled=True, app="myproj.celery")
    )
    out = _strip_ansi(
        render_banner(config=cfg, info=_make_info(celery_status="running --pool=solo"))
    )
    assert "enable Celery" not in out


def test_banner_shows_sidecar_path_when_provided(config: RunSiteConfig) -> None:
    sidecar = Path("/tmp/proj/.run-site-config")
    out = _strip_ansi(render_banner(config=config, info=_make_info(sidecar_path=sidecar)))
    assert "Sidecar:" in out
    assert str(sidecar) in out
    assert "removed on shutdown" in out


def test_banner_omits_sidecar_line_without_path(config: RunSiteConfig) -> None:
    out = _strip_ansi(render_banner(config=config, info=_make_info(sidecar_path=None)))
    assert "Sidecar:" not in out


def test_banner_lifecycle_says_removed_without_reuse(config: RunSiteConfig) -> None:
    out = _strip_ansi(render_banner(config=config, info=_make_info(reuse=False)))
    assert "Lifecycle:" in out
    assert "removed on exit" in out
    assert "Pass --reuse to keep them between runs" in out
    # Must not show the cleanup hint that only applies under --reuse.
    assert "docker rm -f" not in out


def test_banner_lifecycle_says_kept_with_reuse(config: RunSiteConfig) -> None:
    out = _strip_ansi(render_banner(config=config, info=_make_info(reuse=True)))
    assert "Lifecycle:" in out
    assert "kept" in out
    assert "--reuse" in out
    # Includes the docker-rm hint with the project slug from minimal_config.
    assert "docker rm -f demo-runsite-pg demo-runsite-redis" in out


def test_banner_superuser_created_shows_credentials(
    config: RunSiteConfig,
) -> None:
    """Newly-created superuser → show username + password from config."""

    payload = {"username": "admin", "email": "admin@example.com", "created": True}
    out = _strip_ansi(render_banner(config=config, info=_make_info(superuser=payload)))

    assert "Superuser: admin / admin-pwd" in out
    assert "(created)" in out
    assert "email=admin@example.com" in out


def test_banner_superuser_existing_overwrite_shows_password_reset(
    config: RunSiteConfig,
) -> None:
    """User existed; overwrite=true (default) → password got reset; show it."""

    payload = {"username": "admin", "email": "admin@example.com", "created": False}
    out = _strip_ansi(render_banner(config=config, info=_make_info(superuser=payload)))

    assert "Superuser: admin / admin-pwd" in out
    assert "password reset to dev default" in out


def test_banner_superuser_existing_no_overwrite_hides_password(
    config: RunSiteConfig,
) -> None:
    """When overwrite=false the dev password we have isn't necessarily the
    real one — never display it."""

    cfg = replace(config, superuser=replace(config.superuser, overwrite=False))
    payload = {"username": "admin", "email": "admin@example.com", "created": False}
    out = _strip_ansi(render_banner(config=cfg, info=_make_info(superuser=payload)))

    assert "Superuser: admin" in out
    assert "admin-pwd" not in out  # password must not leak when overwrite=false
    assert "password unchanged" in out
    assert "[superuser].overwrite = false" in out


def test_banner_superuser_disabled_message(config: RunSiteConfig) -> None:
    """When setup was skipped — no payload → show a 'disabled' line."""

    out = _strip_ansi(render_banner(config=config, info=_make_info(superuser=None)))

    assert "Superuser: disabled" in out
    assert "--no-superuser" in out
    assert "[superuser].enabled = false" in out


def test_banner_superuser_credentials_hidden_when_show_db_credentials_false(
    config: RunSiteConfig,
) -> None:
    """show_db_credentials=false suppresses *all* secrets, including
    superuser password — keeps the toggle one-knob simple."""

    cfg = replace(config, banner=replace(config.banner, show_db_credentials=False))
    payload = {"username": "admin", "email": "admin@example.com", "created": True}
    out = _strip_ansi(render_banner(config=cfg, info=_make_info(superuser=payload)))

    assert "Superuser: admin" in out
    assert "admin-pwd" not in out
    assert "(created)" in out
