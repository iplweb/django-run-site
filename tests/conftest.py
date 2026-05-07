"""Shared pytest fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest

from run_site.config import RunSiteConfig, load_config


@pytest.fixture
def minimal_toml(tmp_path: Path) -> Path:
    path = tmp_path / "runsite.toml"
    path.write_text(
        'project_slug = "demo"\n'
        'manage_py = "manage.py"\n'
        "[postgres]\n"
        'image = "postgres:16"\n'
        'user = "demo"\n'
        'password = "demo-pwd"\n'
        'db = "demo"\n'
        "[redis]\n"
        'image = "redis:7-alpine"\n'
        "[django]\n"
        'runserver_bind = "127.0.0.1"\n'
        'runserver_display_host = "localhost"\n'
        'browser_probe_path = "/admin/login/"\n'
        "[superuser]\n"
        "enabled = true\n"
        'username = "admin"\n'
        'password = "admin-pwd"\n'
        'email = "admin@example.com"\n'
    )
    (tmp_path / "manage.py").write_text("# fake manage.py\n")
    return path


@pytest.fixture
def minimal_config(tmp_path: Path, minimal_toml: Path) -> RunSiteConfig:
    return load_config(config_path=minimal_toml, project_root=tmp_path)


@pytest.fixture
def project_root(tmp_path: Path, minimal_toml: Path) -> Path:
    """A tmp dir that already has a minimal runsite.toml + manage.py."""

    return tmp_path
