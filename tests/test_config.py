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
    assert config.django.web_command is None  # default = use runserver


def test_django_web_command_parsed(tmp_path: Path) -> None:
    cfg = tmp_path / "runsite.toml"
    cfg.write_text(
        'project_slug = "demo"\n'
        "[django]\n"
        'web_command = ["{python}", "-m", "daphne", "-b", "{bind}", '
        '"-p", "{port}", "demo.asgi:application"]\n'
    )
    config = load_config(config_path=cfg, project_root=tmp_path)
    assert config.django.web_command == (
        "{python}",
        "-m",
        "daphne",
        "-b",
        "{bind}",
        "-p",
        "{port}",
        "demo.asgi:application",
    )


def test_django_web_command_must_be_list_of_strings(tmp_path: Path) -> None:
    cfg = tmp_path / "runsite.toml"
    cfg.write_text('project_slug = "x"\n[django]\nweb_command = "daphne"\n')
    with pytest.raises(ConfigError, match="must be a list of strings"):
        load_config(config_path=cfg, project_root=tmp_path)


def test_django_web_command_must_not_be_empty(tmp_path: Path) -> None:
    cfg = tmp_path / "runsite.toml"
    cfg.write_text('project_slug = "x"\n[django]\nweb_command = []\n')
    with pytest.raises(ConfigError, match="must not be empty"):
        load_config(config_path=cfg, project_root=tmp_path)


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


def test_postgres_defaults_to_enabled(tmp_path: Path) -> None:
    """Backward compat: existing configs without ``enabled`` keep starting
    Postgres."""

    cfg = tmp_path / "runsite.toml"
    cfg.write_text('project_slug = "x"\n[postgres]\n[redis]\n')
    config = load_config(config_path=cfg, project_root=tmp_path)
    assert config.postgres.enabled is True
    assert config.redis.enabled is True


def test_postgres_can_be_disabled(tmp_path: Path) -> None:
    cfg = tmp_path / "runsite.toml"
    cfg.write_text('project_slug = "x"\n[postgres]\nenabled = false\n[redis]\nenabled = false\n')
    config = load_config(config_path=cfg, project_root=tmp_path)
    assert config.postgres.enabled is False
    assert config.redis.enabled is False


def test_postgres_enabled_must_be_bool(tmp_path: Path) -> None:
    cfg = tmp_path / "runsite.toml"
    cfg.write_text('project_slug = "x"\n[postgres]\nenabled = "yes"\n')
    with pytest.raises(ConfigError, match="enabled"):
        load_config(config_path=cfg, project_root=tmp_path)


def test_load_config_works_without_any_file(tmp_path: Path) -> None:
    """Running ``run-site run`` in a directory with no ``runsite.toml``
    and no ``[tool.run-site]`` produces a usable config — slug derived
    from the directory name, everything else defaulted."""

    config = load_config(config_path=None, project_root=tmp_path)
    # tmp_path.name is something like "test_load_config_works_without_any_file0"
    assert config.project_slug != ""
    assert config.postgres.enabled is True
    assert config.redis.enabled is True
    assert config.django.runserver_bind == "127.0.0.1"


def test_project_slug_sanitized_when_dir_name_has_spaces(tmp_path: Path) -> None:
    weird = tmp_path / "my project (v2)"
    weird.mkdir()
    config = load_config(config_path=None, project_root=weird)
    # Spaces and parens collapse to '-', and the result must match the
    # strict allowed-chars pattern.
    import re

    assert re.fullmatch(r"[A-Za-z0-9_.-]+", config.project_slug)


def test_explicit_invalid_project_slug_still_rejected(tmp_path: Path) -> None:
    """Sanitization only kicks in for the auto-derived default; if the
    user spells out an invalid slug we still error."""

    cfg = tmp_path / "runsite.toml"
    cfg.write_text('project_slug = "no spaces allowed"\n')
    with pytest.raises(ConfigError, match="project_slug"):
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
