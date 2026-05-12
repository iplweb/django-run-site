"""SQLite mode: config tri-state, auto-detection, lifecycle, env wiring."""

from __future__ import annotations

from pathlib import Path

import pytest

from run_site.config import (
    DetectedServices,
    PostgresConfig,
    RedisConfig,
    RunSiteConfig,
    SqliteConfig,
    load_config,
    resolve_auto_enabled,
)
from run_site.discovery import (
    detect_services_from_settings,
    discover_settings_module,
)
from run_site.env import ContainerEndpoints, build_subprocess_env
from run_site.errors import ConfigError
from run_site.sqlite import (
    PERSISTENT_DIR_NAME,
    cleanup_sqlite,
    gitignore_warning,
    prepare_sqlite,
)

# ---------------------------------------------------------------------------
# Config — tri-state enabled + mutual exclusion
# ---------------------------------------------------------------------------


def test_sqlite_section_defaults_to_auto(tmp_path: Path) -> None:
    cfg_path = tmp_path / "runsite.toml"
    cfg_path.write_text('project_slug = "demo"\n')
    cfg = load_config(config_path=cfg_path, project_root=tmp_path)
    assert cfg.sqlite.enabled == "auto"
    assert cfg.sqlite.path is None


def test_postgres_and_redis_default_to_auto(tmp_path: Path) -> None:
    """All three service ``enabled`` fields default to ``"auto"`` — the
    decision to boot is driven by settings.py detection, not by an
    unconditional default."""

    cfg_path = tmp_path / "runsite.toml"
    cfg_path.write_text('project_slug = "demo"\n')
    cfg = load_config(config_path=cfg_path, project_root=tmp_path)
    assert cfg.postgres.enabled == "auto"
    assert cfg.redis.enabled == "auto"


def test_enabled_accepts_auto(tmp_path: Path) -> None:
    cfg_path = tmp_path / "runsite.toml"
    cfg_path.write_text(
        'project_slug = "demo"\n'
        '[postgres]\nenabled = "auto"\n'
        '[redis]\nenabled = "auto"\n'
        '[sqlite]\nenabled = "auto"\n'
    )
    cfg = load_config(config_path=cfg_path, project_root=tmp_path)
    assert cfg.postgres.enabled == "auto"
    assert cfg.redis.enabled == "auto"
    assert cfg.sqlite.enabled == "auto"


def test_enabled_rejects_bad_value(tmp_path: Path) -> None:
    cfg_path = tmp_path / "runsite.toml"
    cfg_path.write_text('project_slug = "demo"\n[sqlite]\nenabled = "maybe"\n')
    with pytest.raises(ConfigError, match="must be true, false, or 'auto'"):
        load_config(config_path=cfg_path, project_root=tmp_path)


def test_explicit_pg_and_sqlite_both_true_is_error(tmp_path: Path) -> None:
    cfg_path = tmp_path / "runsite.toml"
    cfg_path.write_text(
        'project_slug = "demo"\n[postgres]\nenabled = true\n[sqlite]\nenabled = true\n'
    )
    with pytest.raises(ConfigError, match="mutually exclusive"):
        load_config(config_path=cfg_path, project_root=tmp_path)


def test_sqlite_path_override_loads(tmp_path: Path) -> None:
    cfg_path = tmp_path / "runsite.toml"
    cfg_path.write_text(
        'project_slug = "demo"\n'
        "[postgres]\nenabled = false\n"
        '[sqlite]\nenabled = true\npath = "custom/db.sqlite3"\n'
    )
    cfg = load_config(config_path=cfg_path, project_root=tmp_path)
    assert cfg.sqlite.enabled is True
    assert cfg.sqlite.path == "custom/db.sqlite3"


# ---------------------------------------------------------------------------
# resolve_auto_enabled
# ---------------------------------------------------------------------------


def _bare_cfg(
    tmp_path: Path,
    *,
    pg_enabled: object = "auto",
    redis_enabled: object = "auto",
    sqlite_enabled: object = "auto",
) -> RunSiteConfig:
    """Build an in-memory config with the tri-state fields explicit."""

    from run_site.config import (
        BannerConfig,
        CeleryConfig,
        ContainersConfig,
        DjangoConfig,
        DumpConfig,
        EnvConfig,
        PythonConfig,
        SourceConfig,
        SuperuserConfig,
    )

    return RunSiteConfig(
        project_root=tmp_path,
        config_path=None,
        project_slug="demo",
        manage_py=None,
        python=PythonConfig(),
        postgres=PostgresConfig(enabled=pg_enabled),  # type: ignore[arg-type]
        redis=RedisConfig(enabled=redis_enabled),  # type: ignore[arg-type]
        sqlite=SqliteConfig(enabled=sqlite_enabled),  # type: ignore[arg-type]
        containers=ContainersConfig(),
        dump=DumpConfig(),
        env=EnvConfig(),
        django=DjangoConfig(),
        superuser=SuperuserConfig(),
        celery=CeleryConfig(),
        extra_processes=(),
        hooks=(),
        banner=BannerConfig(),
        source=SourceConfig(),
    )


def test_resolve_all_auto_with_postgres_detected(tmp_path: Path) -> None:
    cfg = _bare_cfg(tmp_path)
    detected = DetectedServices(postgres=True, sqlite=False, redis=True)
    resolved, notes = resolve_auto_enabled(cfg, detected=detected)
    assert resolved.postgres.enabled is True
    assert resolved.sqlite.enabled is False
    assert resolved.redis.enabled is True
    assert any("postgres" in n for n in notes)


def test_resolve_postgres_wins_when_both_sqlite_and_pg_detected(tmp_path: Path) -> None:
    cfg = _bare_cfg(tmp_path)
    detected = DetectedServices(postgres=True, sqlite=True, redis=False)
    resolved, _ = resolve_auto_enabled(cfg, detected=detected)
    assert resolved.postgres.enabled is True
    # PG wins — SQLite stays disabled even though scan found it.
    assert resolved.sqlite.enabled is False


def test_resolve_sqlite_only(tmp_path: Path) -> None:
    cfg = _bare_cfg(tmp_path)
    detected = DetectedServices(postgres=False, sqlite=True, redis=False)
    resolved, _ = resolve_auto_enabled(cfg, detected=detected)
    assert resolved.postgres.enabled is False
    assert resolved.sqlite.enabled is True


def test_resolve_no_detection_falls_back_to_false(tmp_path: Path) -> None:
    cfg = _bare_cfg(tmp_path)
    resolved, notes = resolve_auto_enabled(cfg, detected=None)
    assert resolved.postgres.enabled is False
    assert resolved.sqlite.enabled is False
    assert resolved.redis.enabled is False
    assert any("Could not locate" in n for n in notes)


def test_resolve_keeps_explicit_values(tmp_path: Path) -> None:
    cfg = _bare_cfg(tmp_path, pg_enabled=True, redis_enabled=False, sqlite_enabled=False)
    detected = DetectedServices(postgres=False, sqlite=True, redis=True)
    resolved, _ = resolve_auto_enabled(cfg, detected=detected)
    assert resolved.postgres.enabled is True
    assert resolved.redis.enabled is False
    assert resolved.sqlite.enabled is False


# ---------------------------------------------------------------------------
# Settings.py discovery + service detection
# ---------------------------------------------------------------------------


def _make_django_project(
    root: Path,
    *,
    settings_module: str = "proj.settings",
    settings_body: str = "",
    extra_files: dict[str, str] | None = None,
) -> Path:
    """Build a minimal Django-shaped tree under *root*. Returns manage.py."""

    manage = root / "manage.py"
    manage.write_text(
        f"import os\nos.environ.setdefault('DJANGO_SETTINGS_MODULE', '{settings_module}')\n"
    )
    parts = settings_module.split(".")
    pkg = root
    for segment in parts[:-1]:
        pkg = pkg / segment
        pkg.mkdir(exist_ok=True)
        (pkg / "__init__.py").touch()
    (pkg / f"{parts[-1]}.py").write_text(settings_body)
    if extra_files:
        for rel, body in extra_files.items():
            target = root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(body)
    return manage


def test_discover_settings_module_from_manage_py(tmp_path: Path) -> None:
    manage = _make_django_project(tmp_path, settings_module="proj.settings")
    assert discover_settings_module(manage_py=manage, env={}) == "proj.settings"


def test_discover_settings_module_env_wins(tmp_path: Path) -> None:
    manage = _make_django_project(tmp_path, settings_module="proj.settings")
    out = discover_settings_module(manage_py=manage, env={"DJANGO_SETTINGS_MODULE": "other.dev"})
    assert out == "other.dev"


def test_detect_sqlite_engine(tmp_path: Path) -> None:
    manage = _make_django_project(
        tmp_path,
        settings_body=(
            "DATABASES = {\n"
            "    'default': {\n"
            "        'ENGINE': 'django.db.backends.sqlite3',\n"
            "        'NAME': 'db.sqlite3',\n"
            "    }\n"
            "}\n"
        ),
    )
    detected = detect_services_from_settings(manage_py=manage, project_root=tmp_path, env={})
    assert detected is not None
    assert detected.sqlite is True
    assert detected.postgres is False


def test_detect_postgres_engine(tmp_path: Path) -> None:
    manage = _make_django_project(
        tmp_path,
        settings_body=(
            "DATABASES = {\n"
            "    'default': {'ENGINE': 'django.db.backends.postgresql', 'NAME': 'x'}\n"
            "}\n"
        ),
    )
    detected = detect_services_from_settings(manage_py=manage, project_root=tmp_path, env={})
    assert detected is not None
    assert detected.postgres is True
    assert detected.sqlite is False


def test_detect_dj_database_url(tmp_path: Path) -> None:
    manage = _make_django_project(
        tmp_path,
        settings_body=(
            "import dj_database_url\n"
            "DATABASES = {'default': dj_database_url.config(default='sqlite:///db.sqlite3')}\n"
        ),
    )
    detected = detect_services_from_settings(manage_py=manage, project_root=tmp_path, env={})
    assert detected is not None
    assert detected.sqlite is True


def test_detect_redis_via_cache_backend(tmp_path: Path) -> None:
    manage = _make_django_project(
        tmp_path,
        settings_body=(
            "CACHES = {'default': {'BACKEND': "
            "'django.core.cache.backends.redis.RedisCache', 'LOCATION': 'redis://x'}}\n"
        ),
    )
    detected = detect_services_from_settings(manage_py=manage, project_root=tmp_path, env={})
    assert detected is not None
    assert detected.redis is True


def test_detect_redis_via_celery_broker(tmp_path: Path) -> None:
    manage = _make_django_project(
        tmp_path,
        settings_body="CELERY_BROKER_URL = 'redis://localhost:6379/0'\n",
    )
    detected = detect_services_from_settings(manage_py=manage, project_root=tmp_path, env={})
    assert detected is not None
    assert detected.redis is True


def test_detect_follows_relative_import(tmp_path: Path) -> None:
    """``from .base import *`` style settings packages should be scanned
    one level deep so detection still works for split configs."""

    manage = _make_django_project(
        tmp_path,
        settings_module="proj.settings",
        settings_body="from .base import *\n",
        extra_files={
            "proj/base.py": ("DATABASES = {'default': {'ENGINE': 'django.db.backends.sqlite3'}}\n")
        },
    )
    detected = detect_services_from_settings(manage_py=manage, project_root=tmp_path, env={})
    assert detected is not None
    assert detected.sqlite is True


def test_detect_returns_none_when_no_settings_module(tmp_path: Path) -> None:
    manage = tmp_path / "manage.py"
    manage.write_text("# no DJANGO_SETTINGS_MODULE setdefault here\n")
    detected = detect_services_from_settings(manage_py=manage, project_root=tmp_path, env={})
    assert detected is None


# ---------------------------------------------------------------------------
# prepare_sqlite + cleanup_sqlite
# ---------------------------------------------------------------------------


def test_prepare_sqlite_ephemeral(tmp_path: Path) -> None:
    cfg = _bare_cfg(tmp_path, pg_enabled=False, sqlite_enabled=True)
    state = prepare_sqlite(config=cfg, reuse=False)
    assert state.ephemeral is True
    assert state.tmpdir is not None
    assert state.path.parent == state.tmpdir
    assert state.path.name == "db.sqlite3"
    # Tmpdir actually exists.
    assert state.tmpdir.is_dir()
    # Cleanup removes it.
    cleanup_sqlite(state)
    assert not state.tmpdir.exists()


def test_prepare_sqlite_persistent_default_path(tmp_path: Path) -> None:
    cfg = _bare_cfg(tmp_path, pg_enabled=False, sqlite_enabled=True)
    state = prepare_sqlite(config=cfg, reuse=True)
    assert state.ephemeral is False
    assert state.tmpdir is None
    assert state.path == tmp_path / PERSISTENT_DIR_NAME / "demo.sqlite3"
    assert state.path.parent.is_dir()
    # Cleanup is a no-op for persistent.
    cleanup_sqlite(state)
    assert state.path.parent.is_dir()


def test_prepare_sqlite_persistent_explicit_relative_path(tmp_path: Path) -> None:
    cfg = _bare_cfg(tmp_path, pg_enabled=False, sqlite_enabled=True)
    cfg = cfg.__class__(**{**cfg.__dict__, "sqlite": SqliteConfig(enabled=True, path="data/x.db")})
    state = prepare_sqlite(config=cfg, reuse=True)
    assert state.path == (tmp_path / "data" / "x.db").resolve()
    assert state.path.parent.is_dir()


def test_prepare_sqlite_persistent_explicit_absolute_path(tmp_path: Path) -> None:
    target = tmp_path / "elsewhere" / "abs.db"
    cfg = _bare_cfg(tmp_path, pg_enabled=False, sqlite_enabled=True)
    cfg = cfg.__class__(**{**cfg.__dict__, "sqlite": SqliteConfig(enabled=True, path=str(target))})
    state = prepare_sqlite(config=cfg, reuse=True)
    assert state.path == target
    assert state.path.parent.is_dir()


def test_prepare_sqlite_force_reset_removes_existing(tmp_path: Path) -> None:
    cfg = _bare_cfg(tmp_path, pg_enabled=False, sqlite_enabled=True)
    state = prepare_sqlite(config=cfg, reuse=True)
    state.path.write_bytes(b"stale-db-content")
    assert state.path.read_bytes() == b"stale-db-content"
    state2 = prepare_sqlite(config=cfg, reuse=True, force_reset=True)
    assert state2.path == state.path
    assert not state2.path.exists()


def test_cleanup_sqlite_none_is_no_op() -> None:
    cleanup_sqlite(None)  # should not raise


# ---------------------------------------------------------------------------
# gitignore_warning
# ---------------------------------------------------------------------------


def test_gitignore_warning_outside_git_repo(tmp_path: Path) -> None:
    """Not a git project — no nagging."""

    assert gitignore_warning(project_root=tmp_path) is None


def test_gitignore_warning_missing_gitignore(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    msg = gitignore_warning(project_root=tmp_path)
    assert msg is not None
    assert ".run-site" in msg


def test_gitignore_warning_missing_entry(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / ".gitignore").write_text("# stuff\n*.log\n.venv/\n")
    msg = gitignore_warning(project_root=tmp_path)
    assert msg is not None
    assert ".run-site" in msg


def test_gitignore_warning_present(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / ".gitignore").write_text(".venv/\n.run-site/\n")
    assert gitignore_warning(project_root=tmp_path) is None


def test_gitignore_warning_bare_name_match(tmp_path: Path) -> None:
    """``.run-site`` (no trailing slash) also counts as ignored."""

    (tmp_path / ".git").mkdir()
    (tmp_path / ".gitignore").write_text(".run-site\n")
    assert gitignore_warning(project_root=tmp_path) is None


# ---------------------------------------------------------------------------
# Env builder: SQLite mode wires database_url + db_name
# ---------------------------------------------------------------------------


def test_env_builder_sqlite_mode(tmp_path: Path) -> None:
    cfg_path = tmp_path / "runsite.toml"
    cfg_path.write_text(
        'project_slug = "demo"\n'
        "[postgres]\nenabled = false\n"
        "[redis]\nenabled = false\n"
        "[sqlite]\nenabled = true\n"
        '[env]\ndatabase_url = "DATABASE_URL"\ndb_name = "DB_NAME"\n'
    )
    cfg = load_config(config_path=cfg_path, project_root=tmp_path)
    endpoints = ContainerEndpoints(
        pg_host=None,
        pg_port=None,
        redis_host=None,
        redis_port=None,
        sqlite_path="/tmp/x/db.sqlite3",
    )
    env = build_subprocess_env(
        config=cfg,
        endpoints=endpoints,
        autologin_token="tok",
        runserver_port=4242,
        is_runserver=True,
    )
    assert env["DATABASE_URL"] == "sqlite:////tmp/x/db.sqlite3"
    assert env["DB_NAME"] == "/tmp/x/db.sqlite3"
    assert env["DEV_HELPERS_DB_NAME"] == "/tmp/x/db.sqlite3"
    # No host/port — SQLite has none.
    assert "DEV_HELPERS_DB_HOST" not in env
    assert "DEV_HELPERS_DB_PORT" not in env


def test_env_builder_pg_wins_when_both_paths_provided(tmp_path: Path) -> None:
    """Defensive: even if a caller accidentally supplies sqlite_path
    while Postgres is enabled, PG wins (no SQLite URL leaks in)."""

    cfg_path = tmp_path / "runsite.toml"
    cfg_path.write_text(
        'project_slug = "demo"\n'
        '[postgres]\nuser = "u"\npassword = "p"\ndb = "d"\n'
        "[redis]\n"
        '[env]\ndatabase_url = "DATABASE_URL"\n'
    )
    cfg = load_config(config_path=cfg_path, project_root=tmp_path)
    endpoints = ContainerEndpoints(
        pg_host="127.0.0.1",
        pg_port=54321,
        redis_host="127.0.0.1",
        redis_port=49153,
        sqlite_path="/tmp/oops.sqlite3",
    )
    env = build_subprocess_env(
        config=cfg,
        endpoints=endpoints,
        autologin_token="tok",
        runserver_port=4242,
        is_runserver=True,
    )
    assert env["DATABASE_URL"].startswith("postgres://u:p@")
    assert "sqlite" not in env["DATABASE_URL"]
