"""Tests for the ``run-site <path/to/manage.py>`` short form."""

from __future__ import annotations

from pathlib import Path

import pytest

from run_site.cli import main
from run_site.discovery import is_django_manage_py

REAL_MANAGE_PY = (
    "#!/usr/bin/env python\n"
    '"""Django\'s command-line utility."""\n'
    "import os\n"
    "import sys\n"
    "\n"
    "\n"
    "def main():\n"
    '    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "myproj.settings")\n'
    "    from django.core.management import execute_from_command_line\n"
    "    execute_from_command_line(sys.argv)\n"
    "\n"
    "\n"
    'if __name__ == "__main__":\n'
    "    main()\n"
)


def test_is_django_manage_py_accepts_real_manage_py(tmp_path: Path) -> None:
    manage = tmp_path / "manage.py"
    manage.write_text(REAL_MANAGE_PY)
    ok, reason = is_django_manage_py(manage)
    assert ok is True
    assert reason is None


def test_is_django_manage_py_rejects_missing(tmp_path: Path) -> None:
    ok, reason = is_django_manage_py(tmp_path / "nope.py")
    assert ok is False
    assert reason is not None
    assert "does not exist" in reason


def test_is_django_manage_py_rejects_non_python(tmp_path: Path) -> None:
    path = tmp_path / "manage.py"
    path.write_text("this is not python {{{\n")
    ok, reason = is_django_manage_py(path)
    assert ok is False
    assert reason is not None
    assert "Python" in reason


def test_is_django_manage_py_rejects_without_execute_from_command_line(
    tmp_path: Path,
) -> None:
    path = tmp_path / "manage.py"
    path.write_text(
        "import os\n"
        'os.environ.setdefault("DJANGO_SETTINGS_MODULE", "myproj.settings")\n'
        "# missing the django.core.management import\n"
    )
    ok, reason = is_django_manage_py(path)
    assert ok is False
    assert reason is not None
    assert "execute_from_command_line" in reason


def test_is_django_manage_py_rejects_without_settings_module(tmp_path: Path) -> None:
    path = tmp_path / "manage.py"
    path.write_text(
        "from django.core.management import execute_from_command_line\n"
        "# no settings module reference here\n"
        'execute_from_command_line(["./manage.py", "help"])\n'
    )
    ok, reason = is_django_manage_py(path)
    assert ok is False
    assert reason is not None
    assert "DJANGO_SETTINGS_MODULE" in reason


def test_is_django_manage_py_accepts_combined_import(tmp_path: Path) -> None:
    """``from django.core.management import call_command, execute_from_command_line``."""

    path = tmp_path / "manage.py"
    path.write_text(
        "import os\n"
        'os.environ.setdefault("DJANGO_SETTINGS_MODULE", "x.settings")\n'
        "from django.core.management import call_command, execute_from_command_line\n"
        "execute_from_command_line([])\n"
        "_ = call_command\n"
    )
    ok, _ = is_django_manage_py(path)
    assert ok is True


def run_cli(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    code = main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_short_form_dispatches_to_run(tmp_path: Path, monkeypatch, capsys) -> None:
    """A valid manage.py path triggers a ``run`` invocation.

    We use ``--dry-run --no-install`` so the short form goes through the
    normal pipeline without touching Docker or the filesystem.
    """

    (tmp_path / "runsite.toml").write_text(
        'project_slug = "demo"\n'
        '[postgres]\nimage = "postgres:16"\n'
        '[redis]\nimage = "redis:7-alpine"\n'
    )
    manage = tmp_path / "manage.py"
    manage.write_text(REAL_MANAGE_PY)
    monkeypatch.chdir(tmp_path)

    code, out, _ = run_cli(["manage.py", "--dry-run", "--no-install"], capsys)
    assert code == 0
    assert "run-site dry-run" in out
    assert str(manage.resolve()) in out


def test_short_form_with_subdir_path(tmp_path: Path, monkeypatch, capsys) -> None:
    """``run-site src/manage.py`` — the motivating example."""

    src = tmp_path / "src"
    src.mkdir()
    (tmp_path / "runsite.toml").write_text(
        'project_slug = "demo"\n'
        'manage_py = "src/manage.py"\n'
        '[postgres]\nimage = "postgres:16"\n'
        '[redis]\nimage = "redis:7-alpine"\n'
    )
    manage = src / "manage.py"
    manage.write_text(REAL_MANAGE_PY)
    monkeypatch.chdir(tmp_path)

    code, out, _ = run_cli(["src/manage.py", "--dry-run", "--no-install"], capsys)
    assert code == 0
    assert "run-site dry-run" in out


def test_short_form_invalid_file_specific_error(tmp_path: Path, monkeypatch, capsys) -> None:
    bogus = tmp_path / "manage.py"
    bogus.write_text("print('hi')\n")
    monkeypatch.chdir(tmp_path)

    code, _out, err = run_cli(["manage.py"], capsys)
    assert code == 64
    assert "is not a usable Django manage.py" in err
    assert "execute_from_command_line" in err


def test_short_form_missing_file_specific_error(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)
    code, _out, err = run_cli(["nope/manage.py"], capsys)
    assert code == 64
    assert "is not a usable Django manage.py" in err
    assert "does not exist" in err


def test_non_file_typo_still_unknown_command(capsys) -> None:
    """A plain typo without ``/`` or ``.py`` falls through to the original error."""

    code, _out, err = run_cli(["ruh"], capsys)
    assert code == 64
    assert "Unknown command" in err
