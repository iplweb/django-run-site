"""Project root, manage.py, local Python discovery tests (§7)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from django_run_site.config import RunSiteConfig, load_config
from django_run_site.discovery import (
    discover_local_python,
    discover_manage_py,
    discover_project_root,
)
from django_run_site.errors import DiscoveryError


def test_discover_project_root_uses_cli_override(tmp_path: Path) -> None:
    target = tmp_path / "explicit"
    target.mkdir()
    root = discover_project_root(cli_root=target, config_root=None, cwd=tmp_path)
    assert root == target.resolve()


def test_discover_project_root_walks_to_runsite_toml(tmp_path: Path) -> None:
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    (tmp_path / "runsite.toml").touch()
    root = discover_project_root(cli_root=None, config_root=None, cwd=nested)
    assert root == tmp_path.resolve()


def test_discover_project_root_falls_back_to_cwd(tmp_path: Path) -> None:
    root = discover_project_root(cli_root=None, config_root=None, cwd=tmp_path)
    assert root == tmp_path.resolve()


def test_discover_manage_py_from_config(minimal_config: RunSiteConfig) -> None:
    path = discover_manage_py(cli_manage=None, config=minimal_config)
    assert path.name == "manage.py"


def test_discover_manage_py_cli_takes_precedence(
    minimal_config: RunSiteConfig, tmp_path: Path
) -> None:
    other = tmp_path / "src"
    other.mkdir()
    cli_manage = other / "manage.py"
    cli_manage.write_text("# explicit override\n")
    path = discover_manage_py(cli_manage=cli_manage, config=minimal_config)
    assert path == cli_manage.resolve()


def test_discover_manage_py_missing_explodes(tmp_path: Path) -> None:
    cfg = load_config(config_path=None, project_root=tmp_path)
    with pytest.raises(DiscoveryError, match=r"manage\.py"):
        discover_manage_py(cli_manage=None, config=cfg)


def test_discover_local_python_explicit_executable(tmp_path: Path) -> None:
    venv = tmp_path / ".venv" / "bin"
    venv.mkdir(parents=True)
    fake = venv / "python"
    fake.write_text("#!/bin/sh\necho fake\n")
    fake.chmod(0o755)
    cfg_path = tmp_path / "runsite.toml"
    cfg_path.write_text('project_slug = "x"\n[python]\nexecutable = ".venv/bin/python"\n')
    cfg = load_config(config_path=cfg_path, project_root=tmp_path)
    python = discover_local_python(cli_python=None, config=cfg)
    assert python == (str(fake.resolve()),)


def test_discover_local_python_command_prefix(tmp_path: Path, monkeypatch) -> None:
    """[python].command should resolve via PATH."""

    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir()
    fake_uv = bin_dir / "uv"
    fake_uv.write_text("#!/bin/sh\necho fake\n")
    fake_uv.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ["PATH"])

    cfg_path = tmp_path / "runsite.toml"
    cfg_path.write_text('project_slug = "x"\n[python]\ncommand = ["uv", "run", "python"]\n')
    cfg = load_config(config_path=cfg_path, project_root=tmp_path)
    python = discover_local_python(cli_python=None, config=cfg)
    assert python[0] == str(fake_uv.resolve())
    assert python[1:] == ("run", "python")


def test_discover_local_python_auto_falls_back_to_sys_executable(
    tmp_path: Path, monkeypatch
) -> None:
    """With "auto" and no .venv / uv.lock / VIRTUAL_ENV, falls back to sys.executable."""

    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    monkeypatch.delenv("RUN_SITE_PYTHON", raising=False)
    cfg_path = tmp_path / "runsite.toml"
    cfg_path.write_text('project_slug = "x"\n[python]\nexecutable = "auto"\n')
    cfg = load_config(config_path=cfg_path, project_root=tmp_path)
    python = discover_local_python(cli_python=None, config=cfg, env={"PATH": os.environ["PATH"]})
    assert python == (sys.executable,)


def test_discover_local_python_picks_dot_venv_python(tmp_path: Path) -> None:
    venv = tmp_path / ".venv" / "bin"
    venv.mkdir(parents=True)
    fake = venv / "python"
    fake.write_text("#!/bin/sh\necho fake\n")
    fake.chmod(0o755)
    cfg_path = tmp_path / "runsite.toml"
    cfg_path.write_text('project_slug = "x"\n[python]\nexecutable = "auto"\n')
    cfg = load_config(config_path=cfg_path, project_root=tmp_path)
    python = discover_local_python(cli_python=None, config=cfg, env={})
    assert python == (str(fake.resolve()),)
