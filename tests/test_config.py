"""Config loader / validator tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from run_site.config import find_config, load_config
from run_site.errors import ConfigError


def test_loads_minimal_runsite_toml(tmp_path: Path, minimal_toml: Path) -> None:
    config = load_config(config_path=minimal_toml, project_root=tmp_path)
    assert config.project_slug == "demo"
    assert config.postgres.image == "postgres:16"
    assert config.redis.image == "redis:7-alpine"
    assert config.superuser.username == "admin"
    assert config.django.runserver_display_host == "localhost"


def test_loads_pyproject_section(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[project]\nname = "demo"\n\n'
        "[tool.run-site]\n"
        'project_slug = "demo"\n'
        "[tool.run-site.postgres]\n"
        'image = "postgres:17"\n'
    )
    (tmp_path / "manage.py").write_text("")
    config = load_config(config_path=pyproject, project_root=tmp_path)
    assert config.project_slug == "demo"
    assert config.postgres.image == "postgres:17"


def test_find_config_prefers_runsite_toml(tmp_path: Path) -> None:
    (tmp_path / "runsite.toml").write_text('project_slug = "x"\n')
    (tmp_path / "pyproject.toml").write_text('[tool.run-site]\nproject_slug = "y"\n')
    found = find_config(tmp_path)
    assert found is not None and found.name == "runsite.toml"


def test_find_config_walks_parents(tmp_path: Path) -> None:
    nested = tmp_path / "a" / "b" / "c"
    nested.mkdir(parents=True)
    (tmp_path / "runsite.toml").write_text('project_slug = "x"\n')
    found = find_config(nested)
    assert found is not None and found == tmp_path / "runsite.toml"


def test_invalid_dump_strategy_rejected(tmp_path: Path) -> None:
    cfg = tmp_path / "runsite.toml"
    cfg.write_text('project_slug = "x"\n[dump]\nstrategy = "bogus"\n')
    with pytest.raises(ConfigError, match="dump"):
        load_config(config_path=cfg, project_root=tmp_path)


def test_invalid_postgres_driver_rejected(tmp_path: Path) -> None:
    cfg = tmp_path / "runsite.toml"
    cfg.write_text('project_slug = "x"\n[postgres]\ndriver = "tcp"\n')
    with pytest.raises(ConfigError, match="driver"):
        load_config(config_path=cfg, project_root=tmp_path)


def test_python_command_and_executable_mutually_exclusive(tmp_path: Path) -> None:
    cfg = tmp_path / "runsite.toml"
    cfg.write_text(
        'project_slug = "x"\n[python]\n'
        'executable = ".venv/bin/python"\n'
        'command = ["uv", "run", "python"]\n'
    )
    with pytest.raises(ConfigError, match="cannot both be set"):
        load_config(config_path=cfg, project_root=tmp_path)


def test_python_command_with_auto_executable_is_ok(tmp_path: Path) -> None:
    cfg = tmp_path / "runsite.toml"
    cfg.write_text(
        'project_slug = "x"\n[python]\nexecutable = "auto"\ncommand = ["uv", "run", "python"]\n'
    )
    config = load_config(config_path=cfg, project_root=tmp_path)
    assert config.python.command == ("uv", "run", "python")


def test_reserved_extra_process_name(tmp_path: Path) -> None:
    cfg = tmp_path / "runsite.toml"
    cfg.write_text(
        'project_slug = "x"\n[[extra_processes]]\nname = "web"\ncommand = ["echo", "x"]\n'
    )
    with pytest.raises(ConfigError, match="reserved name"):
        load_config(config_path=cfg, project_root=tmp_path)


def test_invalid_color(tmp_path: Path) -> None:
    cfg = tmp_path / "runsite.toml"
    cfg.write_text(
        'project_slug = "x"\n'
        "[[extra_processes]]\n"
        'name = "frontend"\n'
        'command = ["npm", "run", "dev"]\n'
        'color = "puce"\n'
    )
    with pytest.raises(ConfigError, match="color"):
        load_config(config_path=cfg, project_root=tmp_path)


def test_duplicate_hook_flag_rejected(tmp_path: Path) -> None:
    cfg = tmp_path / "runsite.toml"
    cfg.write_text(
        'project_slug = "x"\n'
        "[[hooks.pre_serve]]\n"
        'type = "django"\n'
        'callable = "myproj.hooks:a"\n'
        "[[hooks.pre_serve.cli_args]]\n"
        'flag = "--my-flag"\n'
        'dest = "x"\n'
        "[[hooks.post_migrate]]\n"
        'type = "django"\n'
        'callable = "myproj.hooks:b"\n'
        "[[hooks.post_migrate.cli_args]]\n"
        'flag = "--my-flag"\n'
        'dest = "y"\n'
    )
    with pytest.raises(ConfigError, match="Duplicate"):
        load_config(config_path=cfg, project_root=tmp_path)


def test_unknown_env_key_rejected(tmp_path: Path) -> None:
    cfg = tmp_path / "runsite.toml"
    cfg.write_text('project_slug = "x"\n[env]\nbogus = "BOGUS"\n')
    with pytest.raises(ConfigError, match="Unknown"):
        load_config(config_path=cfg, project_root=tmp_path)


def test_missing_config_file_returns_defaults(tmp_path: Path) -> None:
    config = load_config(config_path=None, project_root=tmp_path)
    assert config.project_slug == tmp_path.name
    assert config.postgres.image == "postgres:16"


def test_callable_must_have_colon(tmp_path: Path) -> None:
    cfg = tmp_path / "runsite.toml"
    cfg.write_text(
        'project_slug = "x"\n[[hooks.pre_serve]]\ntype = "django"\ncallable = "no_colon_here"\n'
    )
    with pytest.raises(ConfigError, match="callable"):
        load_config(config_path=cfg, project_root=tmp_path)


def test_source_multiple_refs_rejected(tmp_path: Path) -> None:
    cfg = tmp_path / "runsite.toml"
    cfg.write_text(
        'project_slug = "x"\n'
        "[source]\n"
        'type = "git"\n'
        'url = "https://example.com/r.git"\n'
        'branch = "main"\n'
        'tag = "v1"\n'
    )
    with pytest.raises(ConfigError, match="multiple refs"):
        load_config(config_path=cfg, project_root=tmp_path)
