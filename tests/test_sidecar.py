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


def test_sidecar_omits_postgres_section_when_disabled(tmp_path: Path) -> None:
    """If Postgres was never started, the ``[postgres]`` block must be
    absent — a consumer reading the sidecar should be able to tell the
    service didn't run."""

    write_sidecar(
        project_root=tmp_path,
        info=_make_info(pg_host=None, pg_port=None, pg_db=None, pg_user=None, pg_password=None),
    )
    data = tomllib.loads((tmp_path / SIDECAR_FILENAME).read_text())
    assert "postgres" not in data
    # Other sections still present.
    assert data["web"]["port"] == 8123
    assert data["redis"]["host"] == "127.0.0.1"


def test_sidecar_omits_redis_section_when_disabled(tmp_path: Path) -> None:
    write_sidecar(
        project_root=tmp_path,
        info=_make_info(redis_host=None, redis_port=None, redis_db=None),
    )
    data = tomllib.loads((tmp_path / SIDECAR_FILENAME).read_text())
    assert "redis" not in data
    assert data["postgres"]["host"] == "127.0.0.1"


def test_sidecar_omits_both_when_both_disabled(tmp_path: Path) -> None:
    write_sidecar(
        project_root=tmp_path,
        info=_make_info(
            pg_host=None,
            pg_port=None,
            pg_db=None,
            pg_user=None,
            pg_password=None,
            redis_host=None,
            redis_port=None,
            redis_db=None,
        ),
    )
    data = tomllib.loads((tmp_path / SIDECAR_FILENAME).read_text())
    assert "postgres" not in data
    assert "redis" not in data
    # Web + celery sections still rendered so the file remains useful.
    assert data["web"]["port"] == 8123
    assert data["celery"]["enabled"] is False


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
    """The plain-key form stores the password untouched so consumers can
    build their own connection string."""

    write_sidecar(project_root=tmp_path, info=_make_info(pg_password="p@ss w0rd!"))
    data = tomllib.loads((tmp_path / SIDECAR_FILENAME).read_text())
    assert data["postgres"]["password"] == "p@ss w0rd!"


def test_sidecar_url_url_encodes_user_and_password(tmp_path: Path) -> None:
    """Special characters in user/password must be URL-encoded in the
    connection URL or downstream parsers (psycopg, sqlalchemy) get
    confused by ``@`` / ``:`` / spaces."""

    write_sidecar(
        project_root=tmp_path,
        info=_make_info(pg_user="user@host", pg_password="p@ss w0rd!:/"),
    )
    data = tomllib.loads((tmp_path / SIDECAR_FILENAME).read_text())
    assert data["postgres"]["user"] == "user@host"
    assert data["postgres"]["password"] == "p@ss w0rd!:/"
    # url field has special chars percent-encoded
    assert "user%40host" in data["postgres"]["url"]
    assert "p%40ss%20w0rd%21%3A%2F" in data["postgres"]["url"]


def test_sidecar_escapes_quote_and_backslash(tmp_path: Path) -> None:
    """A password containing ``"`` or ``\\`` must not break TOML parsing."""

    write_sidecar(
        project_root=tmp_path,
        info=_make_info(pg_password='hard"to\\quote'),
    )
    # Reading back via tomllib is the strictest test — if escaping is
    # broken the file fails to parse.
    data = tomllib.loads((tmp_path / SIDECAR_FILENAME).read_text())
    assert data["postgres"]["password"] == 'hard"to\\quote'


def test_sidecar_escapes_control_chars(tmp_path: Path) -> None:
    """Newlines / tabs in fields must be escaped, not interpolated raw."""

    write_sidecar(
        project_root=tmp_path,
        info=_make_info(pg_password="line1\nline2\twith-tab"),
    )
    data = tomllib.loads((tmp_path / SIDECAR_FILENAME).read_text())
    assert data["postgres"]["password"] == "line1\nline2\twith-tab"
