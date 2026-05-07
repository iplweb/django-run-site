"""Tests for the ``.run-site-config`` sidecar."""

from __future__ import annotations

import tomllib
from pathlib import Path

from run_site.sidecar import (
    SIDECAR_FILENAME,
    SidecarInfo,
    remove_sidecar,
    sidecar_path,
    write_sidecar,
)


def _make_info(**overrides) -> SidecarInfo:
    base: dict = dict(
        project_slug="myproj",
        web_host="localhost",
        web_port=8123,
        pg_host="127.0.0.1",
        pg_port=54321,
        pg_db="myproj",
        pg_user="myproj",
        pg_password="password",
        redis_host="127.0.0.1",
        redis_port=16379,
        redis_db=0,
        celery_enabled=False,
        celery_app=None,
    )
    base.update(overrides)
    return SidecarInfo(**base)


def test_write_sidecar_creates_file_at_project_root(tmp_path: Path) -> None:
    path = write_sidecar(project_root=tmp_path, info=_make_info())
    assert path == (tmp_path / SIDECAR_FILENAME).resolve()
    assert path.is_file()


def test_sidecar_round_trips_through_tomllib(tmp_path: Path) -> None:
    write_sidecar(
        project_root=tmp_path,
        info=_make_info(celery_enabled=True, celery_app="myproj.celery"),
    )
    data = tomllib.loads((tmp_path / SIDECAR_FILENAME).read_text())

    assert data["project_slug"] == "myproj"
    assert "generated_at" in data
    assert data["web"] == {
        "host": "localhost",
        "port": 8123,
        "url": "http://localhost:8123/",
    }
    assert data["postgres"]["host"] == "127.0.0.1"
    assert data["postgres"]["port"] == 54321
    assert data["postgres"]["db"] == "myproj"
    assert data["postgres"]["user"] == "myproj"
    assert data["postgres"]["password"] == "password"
    assert data["postgres"]["url"] == "postgres://myproj:password@127.0.0.1:54321/myproj"
    assert data["redis"]["url"] == "redis://127.0.0.1:16379/0"
    assert data["celery"]["enabled"] is True
    assert data["celery"]["app"] == "myproj.celery"


def test_sidecar_omits_celery_app_when_none(tmp_path: Path) -> None:
    write_sidecar(project_root=tmp_path, info=_make_info(celery_enabled=False, celery_app=None))
    data = tomllib.loads((tmp_path / SIDECAR_FILENAME).read_text())
    assert data["celery"]["enabled"] is False
    assert "app" not in data["celery"]


def test_write_overwrites_existing_file(tmp_path: Path) -> None:
    """A crashed prior run might leave a stale sidecar — overwrite it."""

    (tmp_path / SIDECAR_FILENAME).write_text("# stale junk\nbroken = ???\n")
    write_sidecar(project_root=tmp_path, info=_make_info())
    contents = (tmp_path / SIDECAR_FILENAME).read_text()
    assert "stale junk" not in contents
    assert tomllib.loads(contents)["project_slug"] == "myproj"


def test_remove_sidecar_deletes_file(tmp_path: Path) -> None:
    write_sidecar(project_root=tmp_path, info=_make_info())
    assert (tmp_path / SIDECAR_FILENAME).is_file()
    remove_sidecar(project_root=tmp_path)
    assert not (tmp_path / SIDECAR_FILENAME).exists()


def test_remove_sidecar_missing_file_is_noop(tmp_path: Path) -> None:
    """Idempotent — calling remove without write must not raise."""

    remove_sidecar(project_root=tmp_path)  # no exception


def test_sidecar_path_helper(tmp_path: Path) -> None:
    assert sidecar_path(tmp_path) == (tmp_path / SIDECAR_FILENAME).resolve()


def test_sidecar_password_is_kept_verbatim(tmp_path: Path) -> None:
    """We don't try to be clever about quoting in the URL — but the
    plain-key form stores it untouched so consumers can build their own."""

    write_sidecar(project_root=tmp_path, info=_make_info(pg_password="p@ss w0rd!"))
    data = tomllib.loads((tmp_path / SIDECAR_FILENAME).read_text())
    assert data["postgres"]["password"] == "p@ss w0rd!"
