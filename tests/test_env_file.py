"""Tests for the ``.run-site-env.sh`` sourceable env export."""

from __future__ import annotations

import subprocess
from pathlib import Path

from run_site.config import RunSiteConfig, load_config
from run_site.env import ContainerEndpoints
from run_site.env_file import (
    ENV_FILENAME,
    EnvFileInfo,
    build_env_file_info,
    env_file_path,
    remove_env_file,
    write_env_file,
)


def _endpoints(**overrides) -> ContainerEndpoints:
    base: dict = {
        "pg_host": "127.0.0.1",
        "pg_port": 54321,
        "redis_host": "127.0.0.1",
        "redis_port": 16379,
    }
    base.update(overrides)
    return ContainerEndpoints(**base)


def _exports(info: EnvFileInfo) -> dict[str, str]:
    return dict(info.exports)


# ---------------------------------------------------------------------------
# build_env_file_info — the builder holds all enabled/disabled logic
# ---------------------------------------------------------------------------


def test_full_stack_exports_urls_django_and_libpq(minimal_config: RunSiteConfig) -> None:
    info = build_env_file_info(
        config=minimal_config,
        endpoints=_endpoints(),
        secret_key="s3cr3t",
        lan_hosts=(),
    )
    exports = _exports(info)

    assert exports["DATABASE_URL"] == "postgres://demo:demo-pwd@127.0.0.1:54321/demo"
    assert exports["REDIS_URL"] == "redis://127.0.0.1:16379/0"
    assert exports["DJANGO_SECRET_KEY"] == "s3cr3t"
    # libpq vars so a bare `psql` connects to the running container.
    assert exports["PGHOST"] == "127.0.0.1"
    assert exports["PGPORT"] == "54321"
    assert exports["PGDATABASE"] == "demo"
    assert exports["PGUSER"] == "demo"
    assert exports["PGPASSWORD"] == "demo-pwd"


def test_loopback_bind_omits_allowed_hosts(minimal_config: RunSiteConfig) -> None:
    # minimal_config binds 127.0.0.1 → loopback → no ALLOWED_HOSTS export.
    info = build_env_file_info(
        config=minimal_config,
        endpoints=_endpoints(),
        secret_key="s",
        lan_hosts=("myhost.local",),
    )
    assert "DJANGO_ALLOWED_HOSTS" not in _exports(info)


def test_wildcard_bind_exports_allowed_hosts(tmp_path: Path) -> None:
    cfg_path = tmp_path / "runsite.toml"
    cfg_path.write_text(
        'project_slug = "demo"\n'
        'manage_py = "manage.py"\n'
        '[postgres]\nuser = "demo"\npassword = "demo-pwd"\ndb = "demo"\n'
        "[redis]\n"
        '[django]\nrunserver_bind = "0.0.0.0"\n'
    )
    (tmp_path / "manage.py").write_text("# fake\n")
    config = load_config(config_path=cfg_path, project_root=tmp_path)

    info = build_env_file_info(
        config=config,
        endpoints=_endpoints(),
        secret_key="s",
        lan_hosts=("myhost.local",),
    )
    allowed = _exports(info)["DJANGO_ALLOWED_HOSTS"]
    assert "localhost" in allowed
    assert "127.0.0.1" in allowed
    assert "myhost.local" in allowed


def test_no_secret_key_omits_django_secret_key(minimal_config: RunSiteConfig) -> None:
    info = build_env_file_info(
        config=minimal_config,
        endpoints=_endpoints(),
        secret_key=None,
        lan_hosts=(),
    )
    assert "DJANGO_SECRET_KEY" not in _exports(info)


def test_redis_disabled_omits_redis_url(tmp_path: Path) -> None:
    cfg_path = tmp_path / "runsite.toml"
    cfg_path.write_text(
        'project_slug = "demo"\n'
        'manage_py = "manage.py"\n'
        '[postgres]\nuser = "demo"\npassword = "demo-pwd"\ndb = "demo"\n'
        "[redis]\nenabled = false\n"
    )
    (tmp_path / "manage.py").write_text("# fake\n")
    config = load_config(config_path=cfg_path, project_root=tmp_path)

    info = build_env_file_info(
        config=config,
        endpoints=_endpoints(redis_host=None, redis_port=None),
        secret_key="s",
        lan_hosts=(),
    )
    assert "REDIS_URL" not in _exports(info)


def test_sqlite_mode_uses_sqlite_url_and_omits_libpq(tmp_path: Path) -> None:
    cfg_path = tmp_path / "runsite.toml"
    cfg_path.write_text(
        'project_slug = "demo"\n'
        'manage_py = "manage.py"\n'
        "[postgres]\nenabled = false\n"
        "[redis]\nenabled = false\n"
        "[sqlite]\nenabled = true\n"
    )
    (tmp_path / "manage.py").write_text("# fake\n")
    config = load_config(config_path=cfg_path, project_root=tmp_path)

    info = build_env_file_info(
        config=config,
        endpoints=ContainerEndpoints(
            pg_host=None,
            pg_port=None,
            redis_host=None,
            redis_port=None,
            sqlite_path="/tmp/demo.sqlite3",
        ),
        secret_key="s",
        lan_hosts=(),
    )
    exports = _exports(info)
    assert exports["DATABASE_URL"] == "sqlite:////tmp/demo.sqlite3"
    for pg_var in ("PGHOST", "PGPORT", "PGDATABASE", "PGUSER", "PGPASSWORD"):
        assert pg_var not in exports


def test_env_mapping_rename_renames_url_but_not_libpq(tmp_path: Path) -> None:
    cfg_path = tmp_path / "runsite.toml"
    cfg_path.write_text(
        'project_slug = "demo"\n'
        'manage_py = "manage.py"\n'
        '[postgres]\nuser = "demo"\npassword = "demo-pwd"\ndb = "demo"\n'
        "[redis]\n"
        '[env]\ndatabase_url = "MY_DB_URL"\n'
    )
    (tmp_path / "manage.py").write_text("# fake\n")
    config = load_config(config_path=cfg_path, project_root=tmp_path)

    info = build_env_file_info(config=config, endpoints=_endpoints(), secret_key="s", lan_hosts=())
    exports = _exports(info)
    assert "MY_DB_URL" in exports
    assert "DATABASE_URL" not in exports
    # libpq names are fixed conventions — never remapped.
    assert exports["PGHOST"] == "127.0.0.1"


# ---------------------------------------------------------------------------
# rendering + quoting
# ---------------------------------------------------------------------------


def test_render_emits_export_statements(minimal_config: RunSiteConfig) -> None:
    info = build_env_file_info(
        config=minimal_config, endpoints=_endpoints(), secret_key="s3cr3t", lan_hosts=()
    )
    path = write_env_file(project_root=minimal_config.project_root, info=info)
    text = path.read_text()

    assert text.startswith("#")  # comment header
    assert 'export DATABASE_URL="postgres://demo:demo-pwd@127.0.0.1:54321/demo"' in text
    assert 'export PGPASSWORD="demo-pwd"' in text


def test_nasty_password_survives_a_real_source(tmp_path: Path) -> None:
    """A password full of shell metacharacters must round-trip through
    ``source`` so the exported value equals the original."""

    nasty = 'p@ss "w$o`r\\d'
    info = EnvFileInfo(exports=(("PGPASSWORD", nasty),))
    path = write_env_file(project_root=tmp_path, info=info)

    # Source the file in a real shell and echo the value back.
    result = subprocess.run(
        ["bash", "-c", f'source "{path}" && printf %s "$PGPASSWORD"'],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout == nasty


# ---------------------------------------------------------------------------
# write / remove lifecycle
# ---------------------------------------------------------------------------


def test_write_creates_file_at_project_root(tmp_path: Path) -> None:
    info = EnvFileInfo(exports=(("FOO", "bar"),))
    path = write_env_file(project_root=tmp_path, info=info)
    assert path == (tmp_path / ENV_FILENAME).resolve()
    assert path.is_file()


def test_write_overwrites_stale_file(tmp_path: Path) -> None:
    env_file_path(tmp_path).write_text("export STALE=1\n")
    write_env_file(project_root=tmp_path, info=EnvFileInfo(exports=(("FRESH", "1"),)))
    text = env_file_path(tmp_path).read_text()
    assert "STALE" not in text
    assert "FRESH" in text


def test_remove_is_idempotent(tmp_path: Path) -> None:
    write_env_file(project_root=tmp_path, info=EnvFileInfo(exports=(("FOO", "bar"),)))
    remove_env_file(project_root=tmp_path)
    assert not env_file_path(tmp_path).exists()
    # Second call must not raise even though the file is already gone.
    remove_env_file(project_root=tmp_path)
