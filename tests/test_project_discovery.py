"""Project root, manage.py, local Python discovery tests."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from run_site.config import RunSiteConfig, load_config
from run_site.discovery import (
    autoscan_manage_py,
    discover_local_python,
    discover_manage_py,
    discover_project_root,
    imports_django,
)
from run_site.errors import DiscoveryError

# A stock Django manage.py — what `django-admin startproject` writes.
DJANGO_MANAGE_PY = (
    "import os, sys\n"
    "from django.core.management import execute_from_command_line\n"
    "execute_from_command_line(sys.argv)\n"
)
NON_DJANGO_MANAGE_PY = "# A unrelated script that shares the manage.py name.\nprint('hi')\n"


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


def test_autoscan_finds_test_project_manage_py(tmp_path: Path) -> None:
    """Django *packages* (not full sites) often ship a test project; the
    scan must reach into ``test_project/manage.py``."""

    (tmp_path / "test_project").mkdir()
    target = tmp_path / "test_project" / "manage.py"
    target.write_text(DJANGO_MANAGE_PY)

    candidates = autoscan_manage_py(tmp_path)
    assert candidates == [target.resolve()]


def test_autoscan_finds_two_levels_deep(tmp_path: Path) -> None:
    """``tests/test_project/manage.py`` — two-level deep is in scope."""

    (tmp_path / "tests" / "test_project").mkdir(parents=True)
    target = tmp_path / "tests" / "test_project" / "manage.py"
    target.write_text(DJANGO_MANAGE_PY)

    candidates = autoscan_manage_py(tmp_path)
    assert candidates == [target.resolve()]


def test_autoscan_skips_noise_dirs(tmp_path: Path) -> None:
    """Don't pick up manage.py from ``.venv``, ``node_modules`` etc."""

    for noise in (".venv", "node_modules", ".tox", "__pycache__"):
        (tmp_path / noise).mkdir()
        (tmp_path / noise / "manage.py").write_text(DJANGO_MANAGE_PY)

    real = tmp_path / "test_project"
    real.mkdir()
    target = real / "manage.py"
    target.write_text(DJANGO_MANAGE_PY)

    candidates = autoscan_manage_py(tmp_path)
    assert candidates == [target.resolve()]


def test_imports_django_distinguishes_real_from_namesake(tmp_path: Path) -> None:
    real = tmp_path / "real-manage.py"
    real.write_text(DJANGO_MANAGE_PY)
    fake = tmp_path / "fake-manage.py"
    fake.write_text(NON_DJANGO_MANAGE_PY)
    assert imports_django(real) is True
    assert imports_django(fake) is False


def test_discover_manage_py_filters_out_non_django_when_ambiguous(tmp_path: Path) -> None:
    """A bundled tools dir can have a same-named file; auto-detect picks
    the one that actually imports django."""

    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "manage.py").write_text(NON_DJANGO_MANAGE_PY)
    (tmp_path / "test_project").mkdir()
    real = tmp_path / "test_project" / "manage.py"
    real.write_text(DJANGO_MANAGE_PY)

    cfg = load_config(config_path=None, project_root=tmp_path)
    assert discover_manage_py(cli_manage=None, config=cfg) == real.resolve()


def test_discover_manage_py_errors_on_multiple_django_candidates(tmp_path: Path) -> None:
    """Two real-looking Django manage.py files → ask the user to pick."""

    for sub in ("test_project", "demo"):
        (tmp_path / sub).mkdir()
        (tmp_path / sub / "manage.py").write_text(DJANGO_MANAGE_PY)

    cfg = load_config(config_path=None, project_root=tmp_path)
    with pytest.raises(DiscoveryError) as excinfo:
        discover_manage_py(cli_manage=None, config=cfg)
    msg = str(excinfo.value)
    assert "Multiple Django manage.py files" in msg
    # Both candidates listed, sorted alphabetically.
    assert "demo/manage.py" in msg
    assert "test_project/manage.py" in msg


def test_discover_manage_py_relative_cli_anchors_to_project_root(tmp_path: Path) -> None:
    """The user's bug report: ``--manage-py test_project/manage.py`` with
    ``--from-git`` was resolving against CWD instead of the cloned root."""

    project_root = tmp_path / "cloned"
    (project_root / "test_project").mkdir(parents=True)
    target = project_root / "test_project" / "manage.py"
    target.write_text(DJANGO_MANAGE_PY)

    # Pretend we run-site invoked from a totally unrelated CWD.
    cwd = tmp_path / "unrelated"
    cwd.mkdir()
    cfg = load_config(config_path=None, project_root=project_root)
    resolved = discover_manage_py(
        cli_manage=Path("test_project/manage.py"), config=cfg
    )
    assert resolved == target.resolve()


def test_discover_manage_py_absolute_cli_path_unchanged(tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    target = elsewhere / "manage.py"
    target.write_text(DJANGO_MANAGE_PY)

    project_root = tmp_path / "cloned"
    project_root.mkdir()
    cfg = load_config(config_path=None, project_root=project_root)
    resolved = discover_manage_py(cli_manage=target, config=cfg)
    assert resolved == target.resolve()


def test_discover_local_python_relative_cli_anchors_to_project_root(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "cloned"
    venv = project_root / ".venv" / "bin"
    venv.mkdir(parents=True)
    target = venv / "python"
    target.write_text("#!/bin/sh\necho fake\n")
    target.chmod(0o755)

    cfg_path = project_root / "runsite.toml"
    cfg_path.write_text('project_slug = "x"\n')
    cfg = load_config(config_path=cfg_path, project_root=project_root)
    python = discover_local_python(
        cli_python=Path(".venv/bin/python"), config=cfg, env={}
    )
    assert python == (str(target.resolve()),)


def test_discover_local_python_prefers_project_venv_over_ambient_virtualenv(
    tmp_path: Path,
) -> None:
    """Regression: ``uv tool run run-site …`` (and ``pipx run …``) sets
    ``VIRTUAL_ENV`` to the *tool's* venv. That venv has run-site but no
    Django, so picking it for ``manage.py migrate`` blows up with
    ModuleNotFoundError. The project's own ``.venv`` must win."""

    # Project's venv — set up by `ensure_venv` + `install_dependencies`
    # in real runs; we just stub the python binary here.
    project_root = tmp_path / "cloned"
    project_venv = project_root / ".venv" / "bin"
    project_venv.mkdir(parents=True)
    project_python = project_venv / "python"
    project_python.write_text("#!/bin/sh\necho project\n")
    project_python.chmod(0o755)

    # Wrapper tool's venv — what `uv tool run` / `pipx run` injects.
    wrapper_venv = tmp_path / "tool-venv"
    (wrapper_venv / "bin").mkdir(parents=True)
    wrapper_python = wrapper_venv / "bin" / "python"
    wrapper_python.write_text("#!/bin/sh\necho wrapper\n")
    wrapper_python.chmod(0o755)

    cfg_path = project_root / "runsite.toml"
    cfg_path.write_text('project_slug = "x"\n[python]\nexecutable = "auto"\n')
    cfg = load_config(config_path=cfg_path, project_root=project_root)

    python = discover_local_python(
        cli_python=None,
        config=cfg,
        env={"VIRTUAL_ENV": str(wrapper_venv), "PATH": os.environ["PATH"]},
    )
    assert python == (str(project_python.resolve()),)


def test_discover_local_python_does_not_resolve_venv_symlink(tmp_path: Path) -> None:
    """Regression: ``Path('.venv/bin/python').resolve()`` walks the
    symlink to the upstream interpreter, which has no ``pyvenv.cfg`` in
    scope — Python then runs in non-venv mode and ``import django`` (or
    any other project dep) blows up. The venv's symlink path must be
    handed to the subprocess unchanged.

    Reproduces the django-multiseek failure: ``uv venv`` symlinks
    ``.venv/bin/python`` to ``~/.local/share/uv/python/cpython-…``;
    invoking the resolved target loses the venv's ``site-packages``.
    """

    upstream_dir = tmp_path / "upstream-py" / "bin"
    upstream_dir.mkdir(parents=True)
    upstream = upstream_dir / "python3.12"
    upstream.write_text("#!/bin/sh\necho upstream\n")
    upstream.chmod(0o755)

    project_root = tmp_path / "cloned"
    venv_bin = project_root / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    venv_python = venv_bin / "python"
    venv_python.symlink_to(upstream)

    cfg_path = project_root / "runsite.toml"
    cfg_path.write_text('project_slug = "x"\n[python]\nexecutable = "auto"\n')
    cfg = load_config(config_path=cfg_path, project_root=project_root)

    python = discover_local_python(cli_python=None, config=cfg, env={})
    # Must invoke via the symlink itself, not the resolved upstream
    # (otherwise CPython doesn't see the venv's pyvenv.cfg).
    (resolved,) = python
    assert Path(resolved) == venv_python.absolute()
    assert "upstream-py" not in resolved


def test_discover_local_python_explicit_path_does_not_resolve_symlinks(
    tmp_path: Path,
) -> None:
    """Same constraint applies to ``--python``: a user pointing at a
    venv symlink expects that symlink to be invoked, not its target."""

    upstream_dir = tmp_path / "upstream-py" / "bin"
    upstream_dir.mkdir(parents=True)
    upstream = upstream_dir / "python3.12"
    upstream.write_text("#!/bin/sh\necho upstream\n")
    upstream.chmod(0o755)

    venv_bin = tmp_path / "myvenv" / "bin"
    venv_bin.mkdir(parents=True)
    venv_python = venv_bin / "python"
    venv_python.symlink_to(upstream)

    cfg_path = tmp_path / "runsite.toml"
    cfg_path.write_text('project_slug = "x"\n')
    cfg = load_config(config_path=cfg_path, project_root=tmp_path)
    python = discover_local_python(cli_python=venv_python, config=cfg, env={})
    (resolved,) = python
    assert "upstream-py" not in resolved


def test_discover_local_python_falls_back_to_ambient_virtualenv(tmp_path: Path) -> None:
    """When the project has no .venv, ``$VIRTUAL_ENV`` is still a valid
    fallback (e.g. the user activated a venv by hand)."""

    project_root = tmp_path / "cloned"
    project_root.mkdir()
    ambient = tmp_path / "ambient-venv"
    (ambient / "bin").mkdir(parents=True)
    ambient_python = ambient / "bin" / "python"
    ambient_python.write_text("#!/bin/sh\necho ambient\n")
    ambient_python.chmod(0o755)

    cfg_path = project_root / "runsite.toml"
    cfg_path.write_text('project_slug = "x"\n[python]\nexecutable = "auto"\n')
    cfg = load_config(config_path=cfg_path, project_root=project_root)

    python = discover_local_python(
        cli_python=None,
        config=cfg,
        env={"VIRTUAL_ENV": str(ambient), "PATH": os.environ["PATH"]},
    )
    assert python == (str(ambient_python.resolve()),)


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
