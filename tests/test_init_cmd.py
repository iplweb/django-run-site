"""Tests for ``run-site init`` (init_cmd module)."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from run_site.cli import main
from run_site.config import load_config
from run_site.init_cmd import (
    DEFAULT_UV_COMMAND,
    DetectedDefaults,
    _detect_celery,
    _detect_defaults,
    _find_django_module,
    _find_manage_py,
    _render_toml,
    _sanitize_slug,
)


@pytest.fixture(autouse=True)
def _stable_uv_detection(monkeypatch):
    """Force a deterministic answer to ``shutil.which('uv')`` so tests
    don't depend on whether the dev machine has uv installed.

    Individual tests opt into uv-present mode by setting
    ``monkeypatch.setattr('run_site.init_cmd.shutil.which',
    lambda name: '/usr/bin/uv' if name == 'uv' else None)``.
    """

    monkeypatch.setattr("run_site.init_cmd.shutil.which", lambda name: None)


def _enable_uv(monkeypatch) -> None:
    monkeypatch.setattr(
        "run_site.init_cmd.shutil.which",
        lambda name: "/usr/bin/uv" if name == "uv" else None,
    )


# ---------------------------------------------------------------------------
# Helpers — build fake project layouts
# ---------------------------------------------------------------------------


def _make_django_project(
    root: Path,
    *,
    manage_dir: str = ".",
    module_name: str = "myproject",
    with_celery: bool = False,
    with_settings_pkg: bool = False,
    pyproject_name: str | None = None,
) -> Path:
    manage_root = root / manage_dir if manage_dir != "." else root
    manage_root.mkdir(parents=True, exist_ok=True)
    (manage_root / "manage.py").write_text("# fake manage.py\n")

    module_dir = manage_root / module_name
    module_dir.mkdir(parents=True, exist_ok=True)
    (module_dir / "__init__.py").write_text("")
    if with_settings_pkg:
        settings_dir = module_dir / "settings"
        settings_dir.mkdir(parents=True, exist_ok=True)
        (settings_dir / "__init__.py").write_text("")
    else:
        (module_dir / "settings.py").write_text("# fake settings\n")

    if with_celery:
        (module_dir / "celery.py").write_text("# fake celery app\n")

    if pyproject_name is not None:
        (root / "pyproject.toml").write_text(
            f'[project]\nname = "{pyproject_name}"\nversion = "0.0.0"\n'
        )

    return root


# ---------------------------------------------------------------------------
# Unit tests for detection helpers
# ---------------------------------------------------------------------------


def test_find_manage_py_at_root(tmp_path: Path) -> None:
    (tmp_path / "manage.py").write_text("")
    assert _find_manage_py(tmp_path) == "manage.py"


def test_find_manage_py_under_src(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "manage.py").write_text("")
    assert _find_manage_py(tmp_path) == "src/manage.py"


def test_find_manage_py_missing(tmp_path: Path) -> None:
    assert _find_manage_py(tmp_path) is None


def test_find_django_module_with_settings_py(tmp_path: Path) -> None:
    _make_django_project(tmp_path, module_name="myproj")
    assert _find_django_module(tmp_path) == "myproj"


def test_find_django_module_with_settings_package(tmp_path: Path) -> None:
    _make_django_project(tmp_path, module_name="myproj", with_settings_pkg=True)
    assert _find_django_module(tmp_path) == "myproj"


def test_find_django_module_skips_dotdirs(tmp_path: Path) -> None:
    (tmp_path / ".cache").mkdir()
    (tmp_path / ".cache" / "settings.py").write_text("")
    _make_django_project(tmp_path, module_name="real")
    assert _find_django_module(tmp_path) == "real"


def test_find_django_module_none(tmp_path: Path) -> None:
    (tmp_path / "manage.py").write_text("")
    assert _find_django_module(tmp_path) is None


def test_detect_celery_celery_py(tmp_path: Path) -> None:
    (tmp_path / "celery.py").write_text("")
    assert _detect_celery(tmp_path, "myproj") == "myproj.celery"


def test_detect_celery_celery_tasks_py(tmp_path: Path) -> None:
    (tmp_path / "celery_tasks.py").write_text("")
    assert _detect_celery(tmp_path, "myproj") == "myproj.celery_tasks"


def test_detect_celery_none(tmp_path: Path) -> None:
    assert _detect_celery(tmp_path, "myproj") is None


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("myproject", "myproject"),
        ("my-project", "my-project"),
        ("My_Project", "My_Project"),
        ("my project", "my_project"),
        ("django bpp", "django_bpp"),
        ("..weird..", "weird"),
    ],
)
def test_sanitize_slug(raw: str, expected: str) -> None:
    assert _sanitize_slug(raw) == expected


# ---------------------------------------------------------------------------
# End-to-end: the CLI subcommand
# ---------------------------------------------------------------------------


def run_cli(argv: list[str], capsys) -> tuple[int, str, str]:
    code = main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_init_minimal_project(tmp_path: Path, monkeypatch, capsys) -> None:
    _make_django_project(tmp_path, module_name="myproj")
    monkeypatch.chdir(tmp_path)

    code, out, _ = run_cli(["init"], capsys)
    assert code == 0
    assert "Wrote" in out
    assert "myproj" in out

    config_path = tmp_path / "runsite.toml"
    assert config_path.is_file()
    data = tomllib.loads(config_path.read_text())
    assert data["project_slug"] == "myproj"
    assert data["manage_py"] == "manage.py"
    assert data["postgres"]["db"] == "myproj"
    assert data["postgres"]["user"] == "myproj"
    assert "celery" not in data


def test_init_src_layout(tmp_path: Path, monkeypatch, capsys) -> None:
    _make_django_project(tmp_path, manage_dir="src", module_name="bpp")
    monkeypatch.chdir(tmp_path)

    code, _out, _ = run_cli(["init"], capsys)
    assert code == 0
    data = tomllib.loads((tmp_path / "runsite.toml").read_text())
    assert data["manage_py"] == "src/manage.py"
    assert data["project_slug"] == "bpp"


def test_init_detects_celery(tmp_path: Path, monkeypatch, capsys) -> None:
    _make_django_project(tmp_path, module_name="shop", with_celery=True)
    monkeypatch.chdir(tmp_path)

    code, out, _ = run_cli(["init"], capsys)
    assert code == 0
    assert "celery.app" in out

    data = tomllib.loads((tmp_path / "runsite.toml").read_text())
    assert data["celery"]["app"] == "shop.celery"
    assert data["celery"]["enabled"] is True


def test_init_refuses_to_overwrite(tmp_path: Path, monkeypatch, capsys) -> None:
    _make_django_project(tmp_path, module_name="myproj")
    (tmp_path / "runsite.toml").write_text("# existing\n")
    monkeypatch.chdir(tmp_path)

    code, _out, err = run_cli(["init"], capsys)
    assert code != 0
    assert "already exists" in err
    # Did not overwrite.
    assert (tmp_path / "runsite.toml").read_text() == "# existing\n"


def test_init_force_overwrites(tmp_path: Path, monkeypatch, capsys) -> None:
    _make_django_project(tmp_path, module_name="myproj")
    (tmp_path / "runsite.toml").write_text("# existing\n")
    monkeypatch.chdir(tmp_path)

    code, _out, _ = run_cli(["init", "--force"], capsys)
    assert code == 0
    assert "# existing" not in (tmp_path / "runsite.toml").read_text()


def test_init_falls_back_to_pyproject_name(tmp_path: Path, monkeypatch, capsys) -> None:
    """No Django module found → use pyproject name."""

    (tmp_path / "manage.py").write_text("")
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "fancy-app"\nversion = "0.0.0"\n')
    monkeypatch.chdir(tmp_path)

    code, _out, _ = run_cli(["init"], capsys)
    assert code == 0
    data = tomllib.loads((tmp_path / "runsite.toml").read_text())
    assert data["project_slug"] == "fancy-app"


def test_init_warns_on_existing_pyproject_section(tmp_path: Path, monkeypatch, capsys) -> None:
    _make_django_project(tmp_path, module_name="myproj")
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\nversion = "0.0.0"\n[tool.run-site]\nproject_slug = "x"\n'
    )
    monkeypatch.chdir(tmp_path)

    code, out, _ = run_cli(["init"], capsys)
    assert code == 0
    assert "[tool.run-site]" in out


def test_init_errors_without_manage_py(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)
    code, _out, err = run_cli(["init"], capsys)
    assert code != 0
    assert "manage.py" in err


def test_init_output_loadable_by_load_config(tmp_path: Path, monkeypatch, capsys) -> None:
    """The generated file must round-trip through the real config loader."""

    _make_django_project(tmp_path, module_name="myproj", with_celery=True)
    monkeypatch.chdir(tmp_path)
    run_cli(["init"], capsys)

    config = load_config(config_path=tmp_path / "runsite.toml", project_root=tmp_path)
    assert config.project_slug == "myproj"
    assert config.manage_py == "manage.py"
    assert config.celery.enabled is True
    assert config.celery.app == "myproj.celery"


def test_init_custom_output(tmp_path: Path, monkeypatch, capsys) -> None:
    _make_django_project(tmp_path, module_name="myproj")
    monkeypatch.chdir(tmp_path)

    target = tmp_path / "configs" / "runsite.toml"
    code, _out, _ = run_cli(["init", "--output", str(target)], capsys)
    assert code == 0
    assert target.is_file()


def test_init_emits_uv_run_when_uv_on_path(tmp_path: Path, monkeypatch, capsys) -> None:
    _enable_uv(monkeypatch)
    _make_django_project(tmp_path, module_name="myproj")
    monkeypatch.chdir(tmp_path)

    code, out, _ = run_cli(["init"], capsys)
    assert code == 0
    assert "uv run" in out or "python.command" in out

    data = tomllib.loads((tmp_path / "runsite.toml").read_text())
    assert data["python"]["command"] == list(DEFAULT_UV_COMMAND)
    assert "executable" not in data["python"]

    # And the resulting config must round-trip through load_config without
    # raising the "command + executable" mutual-exclusion error.
    config = load_config(config_path=tmp_path / "runsite.toml", project_root=tmp_path)
    assert config.python.command == DEFAULT_UV_COMMAND
    assert config.python.executable == "auto"  # default; ignored when command is set


def test_init_falls_back_to_executable_auto_without_uv(tmp_path: Path, monkeypatch, capsys) -> None:
    # _stable_uv_detection autouse fixture already sets which() → None.
    _make_django_project(tmp_path, module_name="myproj")
    monkeypatch.chdir(tmp_path)

    code, out, _ = run_cli(["init"], capsys)
    assert code == 0
    assert "uv not on PATH" in out

    data = tomllib.loads((tmp_path / "runsite.toml").read_text())
    assert data["python"]["executable"] == "auto"
    assert "command" not in data["python"]


# ---------------------------------------------------------------------------
# Sanity: rendering doesn't hardcode anything weird
# ---------------------------------------------------------------------------


def _make_detected(**overrides) -> DetectedDefaults:
    base: dict = {
        "project_root": Path("/tmp/x"),
        "manage_py_rel": "manage.py",
        "project_slug": "x",
        "django_module": "x",
        "celery_app": None,
        "has_uv_lock": False,
        "has_venv": False,
        "has_uv": False,
    }
    base.update(overrides)
    return DetectedDefaults(**base)


def test_render_without_celery_omits_section() -> None:
    out = _render_toml(_make_detected(), with_celery=False)
    assert "[celery]" not in out
    tomllib.loads(out)


def test_render_with_celery_includes_section() -> None:
    out = _render_toml(_make_detected(celery_app="x.celery"), with_celery=True)
    data = tomllib.loads(out)
    assert data["celery"]["app"] == "x.celery"


def test_render_with_uv_emits_command() -> None:
    out = _render_toml(_make_detected(has_uv=True), with_celery=False)
    data = tomllib.loads(out)
    assert data["python"]["command"] == list(DEFAULT_UV_COMMAND)
    assert "executable" not in data["python"]


def test_render_without_uv_emits_executable_auto() -> None:
    out = _render_toml(_make_detected(has_uv=False), with_celery=False)
    data = tomllib.loads(out)
    assert data["python"]["executable"] == "auto"
    assert "command" not in data["python"]


def test_detect_defaults_against_bundled_test_site() -> None:
    """The bundled examples/test_site should produce the same shape as its
    hand-written runsite.toml."""

    here = Path(__file__).resolve().parent.parent / "examples" / "test_site"
    if not here.is_dir():
        pytest.skip("examples/test_site not present")
    detected = _detect_defaults(here)
    assert detected.manage_py_rel == "manage.py"
    assert detected.project_slug == "test_site"
    assert detected.django_module == "test_site"
    assert detected.celery_app is None
