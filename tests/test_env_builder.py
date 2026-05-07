"""Env builder tests — DEV_HELPERS_* contract + project [env] mapping."""

from __future__ import annotations

from pathlib import Path

from run_site.config import load_config
from run_site.env import (
    REDACT_VALUE,
    ContainerEndpoints,
    build_subprocess_env,
    format_env_for_print,
    generate_autologin_token,
)


def make_endpoints() -> ContainerEndpoints:
    return ContainerEndpoints(
        pg_host="127.0.0.1",
        pg_port=54321,
        redis_host="127.0.0.1",
        redis_port=49153,
    )


def test_dev_helpers_contract_always_set(minimal_config) -> None:
    env = build_subprocess_env(
        config=minimal_config,
        endpoints=make_endpoints(),
        autologin_token="tok",
        runserver_port=4242,
        is_runserver=True,
    )
    assert env["DEV_HELPERS_AUTOLOGIN_TOKEN"] == "tok"
    assert env["DEV_HELPERS_AUTOLOGIN_USERNAME"] == "admin"
    assert env["DEV_HELPERS_DB_HOST"] == "127.0.0.1"
    assert env["DEV_HELPERS_DB_PORT"] == "54321"
    assert env["DEV_HELPERS_DB_NAME"] == "demo"
    assert env["DEV_HELPERS_DB_USER"] == "demo"
    assert env["DEV_HELPERS_REDIS_HOST"] == "127.0.0.1"
    assert env["DEV_HELPERS_REDIS_PORT"] == "49153"
    assert env["DEV_HELPERS_PORT"] == "4242"
    assert env["DJANGO_DEV_HELPERS_ENABLED"] == "1"


def test_dev_helpers_enabled_only_for_runserver(minimal_config) -> None:
    env = build_subprocess_env(
        config=minimal_config,
        endpoints=make_endpoints(),
        autologin_token="tok",
        runserver_port=4242,
        is_runserver=False,
    )
    assert "DJANGO_DEV_HELPERS_ENABLED" not in env
    # Other DEV_HELPERS_* vars are still set (contract is consistent).
    assert env["DEV_HELPERS_AUTOLOGIN_TOKEN"] == "tok"


def test_project_env_mapping_double_set(tmp_path: Path) -> None:
    cfg_path = tmp_path / "runsite.toml"
    cfg_path.write_text(
        'project_slug = "demo"\n'
        '[postgres]\nuser = "u"\npassword = "p"\ndb = "d"\n'
        "[redis]\n"
        "[env]\n"
        'database_url = "DATABASE_URL"\n'
        'db_host = "DJANGO_BPP_DB_HOST"\n'
    )
    config = load_config(config_path=cfg_path, project_root=tmp_path)
    env = build_subprocess_env(
        config=config,
        endpoints=make_endpoints(),
        autologin_token="tok",
        runserver_port=4242,
        is_runserver=True,
    )
    assert env["DJANGO_BPP_DB_HOST"] == "127.0.0.1"
    assert env["DEV_HELPERS_DB_HOST"] == "127.0.0.1"
    assert env["DATABASE_URL"].startswith("postgres://u:p@127.0.0.1:54321/d")


def test_project_env_extra(tmp_path: Path) -> None:
    cfg_path = tmp_path / "runsite.toml"
    cfg_path.write_text('project_slug = "demo"\n[env.extra]\nDJANGO_BPP_SKIP_DOTENV = "1"\n')
    config = load_config(config_path=cfg_path, project_root=tmp_path)
    env = build_subprocess_env(
        config=config,
        endpoints=make_endpoints(),
        autologin_token="tok",
        runserver_port=4242,
        is_runserver=True,
    )
    assert env["DJANGO_BPP_SKIP_DOTENV"] == "1"


def test_url_password_url_encoded(tmp_path: Path) -> None:
    cfg_path = tmp_path / "runsite.toml"
    cfg_path.write_text(
        'project_slug = "demo"\n'
        '[postgres]\nuser = "u"\npassword = "p@ss/word"\ndb = "d"\n'
        "[redis]\n"
        '[env]\ndatabase_url = "DATABASE_URL"\n'
    )
    config = load_config(config_path=cfg_path, project_root=tmp_path)
    env = build_subprocess_env(
        config=config,
        endpoints=make_endpoints(),
        autologin_token="tok",
        runserver_port=4242,
        is_runserver=True,
    )
    assert "p%40ss%2Fword" in env["DATABASE_URL"]


def test_driver_changes_url_scheme(tmp_path: Path) -> None:
    cfg_path = tmp_path / "runsite.toml"
    cfg_path.write_text(
        'project_slug = "demo"\n'
        '[postgres]\nuser = "u"\npassword = "p"\ndb = "d"\ndriver = "+psycopg2"\n'
        "[redis]\n"
        '[env]\ndatabase_url = "DATABASE_URL"\n'
    )
    config = load_config(config_path=cfg_path, project_root=tmp_path)
    env = build_subprocess_env(
        config=config,
        endpoints=make_endpoints(),
        autologin_token="tok",
        runserver_port=4242,
        is_runserver=True,
    )
    assert env["DATABASE_URL"].startswith("postgres+psycopg2://")


def test_format_env_for_print_redacts_secrets() -> None:
    env = {
        "DJANGO_DB_PASSWORD": "secret",
        "DEV_HELPERS_AUTOLOGIN_TOKEN": "tok",
        "BANNER_API_KEY": "key",
        "PG_HOST": "127.0.0.1",
    }
    out = format_env_for_print(env, redact=True)
    assert "secret" not in out
    assert REDACT_VALUE in out
    assert "127.0.0.1" in out


def test_format_env_for_print_secrets_off() -> None:
    env = {"DJANGO_DB_PASSWORD": "secret"}
    out = format_env_for_print(env, redact=False)
    assert "secret" in out


def test_generate_autologin_token_is_random() -> None:
    a, b = generate_autologin_token(), generate_autologin_token()
    assert a != b
    assert len(a) > 30
