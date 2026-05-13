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


# ---------------------------------------------------------------------------
# Disabled-service paths — SQLite / cache-less stacks
# ---------------------------------------------------------------------------


def test_dev_helpers_db_vars_omitted_when_postgres_disabled(tmp_path: Path) -> None:
    cfg_path = tmp_path / "runsite.toml"
    cfg_path.write_text('project_slug = "demo"\n[postgres]\nenabled = false\n[redis]\n')
    config = load_config(config_path=cfg_path, project_root=tmp_path)
    endpoints = ContainerEndpoints(
        pg_host=None, pg_port=None, redis_host="127.0.0.1", redis_port=49153
    )
    env = build_subprocess_env(
        config=config,
        endpoints=endpoints,
        autologin_token="tok",
        runserver_port=4242,
        is_runserver=True,
    )
    # Autologin contract is unconditional.
    assert env["DEV_HELPERS_AUTOLOGIN_TOKEN"] == "tok"
    # No DB env vars — caller must not see broken None:None values.
    assert "DEV_HELPERS_DB_HOST" not in env
    assert "DEV_HELPERS_DB_PORT" not in env
    assert "DEV_HELPERS_DB_NAME" not in env
    assert "DEV_HELPERS_DB_USER" not in env
    # Redis still set.
    assert env["DEV_HELPERS_REDIS_HOST"] == "127.0.0.1"


def test_project_env_mapping_skips_disabled_postgres(tmp_path: Path) -> None:
    """A project that maps ``database_url`` while running without
    Postgres must not get a URL pointing at None:None — the var simply
    isn't set, and the user's ``settings.py`` falls back to its own
    SQLite default."""

    cfg_path = tmp_path / "runsite.toml"
    cfg_path.write_text(
        'project_slug = "demo"\n'
        "[postgres]\nenabled = false\n"
        "[redis]\n"
        '[env]\ndatabase_url = "DATABASE_URL"\n'
    )
    config = load_config(config_path=cfg_path, project_root=tmp_path)
    endpoints = ContainerEndpoints(
        pg_host=None, pg_port=None, redis_host="127.0.0.1", redis_port=49153
    )
    env = build_subprocess_env(
        config=config,
        endpoints=endpoints,
        autologin_token="tok",
        runserver_port=4242,
        is_runserver=True,
    )
    assert "DATABASE_URL" not in env


# ---------------------------------------------------------------------------
# DJANGO_SETTINGS_MODULE injection — covers the celery-needs-settings case
# ---------------------------------------------------------------------------


def test_django_settings_module_injected_when_provided(minimal_config) -> None:
    env = build_subprocess_env(
        config=minimal_config,
        endpoints=make_endpoints(),
        autologin_token="tok",
        runserver_port=4242,
        is_runserver=False,
        django_settings_module="myproject.settings",
        base_env={},
    )
    assert env["DJANGO_SETTINGS_MODULE"] == "myproject.settings"


def test_django_settings_module_does_not_override_existing(minimal_config) -> None:
    """If the user has DJANGO_SETTINGS_MODULE already exported in the
    shell (or otherwise present in base_env), our discovered value must
    not clobber it."""

    env = build_subprocess_env(
        config=minimal_config,
        endpoints=make_endpoints(),
        autologin_token="tok",
        runserver_port=4242,
        is_runserver=False,
        django_settings_module="discovered.settings",
        base_env={"DJANGO_SETTINGS_MODULE": "user.settings"},
    )
    assert env["DJANGO_SETTINGS_MODULE"] == "user.settings"


def test_django_settings_module_absent_when_not_provided(minimal_config) -> None:
    env = build_subprocess_env(
        config=minimal_config,
        endpoints=make_endpoints(),
        autologin_token="tok",
        runserver_port=4242,
        is_runserver=False,
        base_env={},
    )
    assert "DJANGO_SETTINGS_MODULE" not in env


# ---------------------------------------------------------------------------
# SECRET_KEY auto-export + default conventional env-var names
# ---------------------------------------------------------------------------


def test_secret_key_exported_under_default_name(minimal_config) -> None:
    env = build_subprocess_env(
        config=minimal_config,
        endpoints=make_endpoints(),
        autologin_token="tok",
        runserver_port=4242,
        is_runserver=True,
        secret_key="s3cret-value",
        base_env={},
    )
    assert env["DJANGO_SECRET_KEY"] == "s3cret-value"


def test_secret_key_absent_when_not_provided(minimal_config) -> None:
    env = build_subprocess_env(
        config=minimal_config,
        endpoints=make_endpoints(),
        autologin_token="tok",
        runserver_port=4242,
        is_runserver=True,
        base_env={},
    )
    assert "DJANGO_SECRET_KEY" not in env


def test_secret_key_custom_var_name(tmp_path: Path) -> None:
    cfg_path = tmp_path / "runsite.toml"
    cfg_path.write_text(
        'project_slug = "demo"\n[postgres]\n[redis]\n[env]\nsecret_key = "MY_SECRET"\n'
    )
    config = load_config(config_path=cfg_path, project_root=tmp_path)
    env = build_subprocess_env(
        config=config,
        endpoints=make_endpoints(),
        autologin_token="tok",
        runserver_port=4242,
        is_runserver=True,
        secret_key="abc",
        base_env={},
    )
    assert env["MY_SECRET"] == "abc"
    # The default name should NOT also be set when user mapped it elsewhere.
    assert "DJANGO_SECRET_KEY" not in env


def test_secret_key_disabled_by_null_mapping(tmp_path: Path) -> None:
    """Setting ``[env].secret_key`` to a TOML literal that decodes as null
    (we can't write ``null`` in TOML directly, so we test the equivalent
    by building the EnvConfig with an explicit None)."""

    from run_site.config import EnvConfig
    from run_site.env import effective_env_mapping

    mapping = effective_env_mapping(EnvConfig(mapping={"secret_key": None}).mapping)
    assert mapping["secret_key"] is None


def test_default_database_url_export_without_explicit_mapping(tmp_path: Path) -> None:
    """A project that does not configure ``[env]`` at all should still
    receive a conventional ``DATABASE_URL`` when Postgres is on."""

    cfg_path = tmp_path / "runsite.toml"
    cfg_path.write_text('project_slug = "demo"\n[postgres]\n[redis]\n')
    config = load_config(config_path=cfg_path, project_root=tmp_path)
    env = build_subprocess_env(
        config=config,
        endpoints=make_endpoints(),
        autologin_token="tok",
        runserver_port=4242,
        is_runserver=True,
        base_env={},
    )
    assert env["DATABASE_URL"].startswith("postgres://")
    assert env["REDIS_URL"].startswith("redis://")


def test_user_override_wins_over_default_mapping(tmp_path: Path) -> None:
    cfg_path = tmp_path / "runsite.toml"
    cfg_path.write_text(
        'project_slug = "demo"\n[postgres]\n[redis]\n[env]\ndatabase_url = "MY_DB_URL"\n'
    )
    config = load_config(config_path=cfg_path, project_root=tmp_path)
    env = build_subprocess_env(
        config=config,
        endpoints=make_endpoints(),
        autologin_token="tok",
        runserver_port=4242,
        is_runserver=True,
        base_env={},
    )
    assert env["MY_DB_URL"].startswith("postgres://")
    # Default name no longer exported — user took control.
    assert "DATABASE_URL" not in env


def test_redis_vars_omitted_when_redis_disabled(tmp_path: Path) -> None:
    cfg_path = tmp_path / "runsite.toml"
    cfg_path.write_text(
        'project_slug = "demo"\n[postgres]\n[redis]\nenabled = false\n'
        '[env]\nredis_url = "REDIS_URL"\n'
    )
    config = load_config(config_path=cfg_path, project_root=tmp_path)
    endpoints = ContainerEndpoints(
        pg_host="127.0.0.1", pg_port=54321, redis_host=None, redis_port=None
    )
    env = build_subprocess_env(
        config=config,
        endpoints=endpoints,
        autologin_token="tok",
        runserver_port=4242,
        is_runserver=True,
    )
    assert "DEV_HELPERS_REDIS_HOST" not in env
    assert "DEV_HELPERS_REDIS_PORT" not in env
    assert "REDIS_URL" not in env
    # DB still wired.
    assert env["DEV_HELPERS_DB_HOST"] == "127.0.0.1"


# ---------------------------------------------------------------------------
# ALLOWED_HOSTS injection (LAN-aware)
# ---------------------------------------------------------------------------


def test_compute_allowed_hosts_loopback_bind_returns_empty() -> None:
    from run_site.env import compute_allowed_hosts

    assert compute_allowed_hosts(bind="127.0.0.1", lan_hosts=("box.local",)) == ()
    assert compute_allowed_hosts(bind="localhost", lan_hosts=("box.local",)) == ()
    assert compute_allowed_hosts(bind="::1", lan_hosts=()) == ()


def test_compute_allowed_hosts_wildcard_includes_loopback_and_lan() -> None:
    from run_site.env import compute_allowed_hosts

    out = compute_allowed_hosts(bind="0.0.0.0", lan_hosts=("box.local", "192.168.1.10"))
    # Loopback names always present, then LAN, dedup'd, no wildcards.
    assert out == ("localhost", "127.0.0.1", "[::1]", "box.local", "192.168.1.10")


def test_compute_allowed_hosts_dedupes_overlap() -> None:
    from run_site.env import compute_allowed_hosts

    out = compute_allowed_hosts(bind="0.0.0.0", lan_hosts=("localhost", "10.0.0.1"))
    assert out == ("localhost", "127.0.0.1", "[::1]", "10.0.0.1")


def test_allowed_hosts_not_exported_for_loopback_bind(minimal_config) -> None:
    """Default bind is 127.0.0.1 — no LAN exposure, no env vars set."""

    env = build_subprocess_env(
        config=minimal_config,
        endpoints=make_endpoints(),
        autologin_token="tok",
        runserver_port=4242,
        is_runserver=True,
        lan_hosts=("box.local", "10.0.0.5"),
        base_env={},
    )
    assert "DJANGO_ALLOWED_HOSTS" not in env
    assert "DEV_HELPERS_ALLOWED_HOSTS" not in env


def test_allowed_hosts_exported_for_wildcard_bind(tmp_path: Path) -> None:
    from dataclasses import replace

    cfg_path = tmp_path / "runsite.toml"
    cfg_path.write_text('project_slug = "demo"\n[postgres]\n[redis]\n')
    config = load_config(config_path=cfg_path, project_root=tmp_path)
    config = replace(
        config,
        django=replace(config.django, runserver_bind="0.0.0.0"),
    )
    env = build_subprocess_env(
        config=config,
        endpoints=make_endpoints(),
        autologin_token="tok",
        runserver_port=4242,
        is_runserver=True,
        lan_hosts=("box.local", "192.168.1.10"),
        base_env={},
    )
    expected = "localhost,127.0.0.1,[::1],box.local,192.168.1.10"
    # Both names — DEV_HELPERS_* contract for the helper, conventional
    # DJANGO_ALLOWED_HOSTS for projects that read it directly.
    assert env["DEV_HELPERS_ALLOWED_HOSTS"] == expected
    assert env["DJANGO_ALLOWED_HOSTS"] == expected


def test_allowed_hosts_user_renamed_via_env_mapping(tmp_path: Path) -> None:
    """User can rename the conventional export via [env].allowed_hosts."""

    from dataclasses import replace

    cfg_path = tmp_path / "runsite.toml"
    cfg_path.write_text(
        'project_slug = "demo"\n[postgres]\n[redis]\n'
        '[env]\nallowed_hosts = "MY_HOSTS"\n'
    )
    config = load_config(config_path=cfg_path, project_root=tmp_path)
    config = replace(config, django=replace(config.django, runserver_bind="0.0.0.0"))
    env = build_subprocess_env(
        config=config,
        endpoints=make_endpoints(),
        autologin_token="tok",
        runserver_port=4242,
        is_runserver=True,
        lan_hosts=("box.local",),
        base_env={},
    )
    assert "DJANGO_ALLOWED_HOSTS" not in env
    assert env["MY_HOSTS"].startswith("localhost,127.0.0.1,[::1],box.local")
    # DEV_HELPERS_* contract is unchanged regardless of user mapping.
    assert env["DEV_HELPERS_ALLOWED_HOSTS"] == env["MY_HOSTS"]


def test_allowed_hosts_disabled_when_mapping_set_to_null(tmp_path: Path) -> None:
    """User can suppress the conventional export with ``allowed_hosts = null``.
    DEV_HELPERS_* contract still fires — that's the helper's input,
    decoupled from the user-facing name."""

    from dataclasses import replace

    from run_site.config import EnvConfig

    cfg_path = tmp_path / "runsite.toml"
    cfg_path.write_text('project_slug = "demo"\n[postgres]\n[redis]\n')
    config = load_config(config_path=cfg_path, project_root=tmp_path)
    config = replace(
        config,
        django=replace(config.django, runserver_bind="0.0.0.0"),
        env=EnvConfig(mapping={"allowed_hosts": None}),
    )
    env = build_subprocess_env(
        config=config,
        endpoints=make_endpoints(),
        autologin_token="tok",
        runserver_port=4242,
        is_runserver=True,
        lan_hosts=("box.local",),
        base_env={},
    )
    assert "DJANGO_ALLOWED_HOSTS" not in env
    assert env["DEV_HELPERS_ALLOWED_HOSTS"].endswith("box.local")
