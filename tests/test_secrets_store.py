"""Tests for ``run_site.secrets_store`` — auto-persisted SECRET_KEY."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from unittest.mock import patch

import pytest

from run_site.secrets_store import (
    SECRET_KEY_FILENAME,
    load_or_generate_secret_key,
    secret_key_path,
)
from run_site.sqlite import PERSISTENT_DIR_NAME


def test_secret_key_path_lives_under_persistent_dir(tmp_path: Path) -> None:
    path = secret_key_path(tmp_path)
    assert path.parent.name == PERSISTENT_DIR_NAME
    assert path.name == SECRET_KEY_FILENAME


def test_generates_when_missing(tmp_path: Path) -> None:
    value = load_or_generate_secret_key(tmp_path)
    assert isinstance(value, str)
    # token_urlsafe(50) → ~67 chars; min sanity bound.
    assert len(value) >= 40
    # File got created and contains exactly the returned value.
    persisted = (tmp_path / PERSISTENT_DIR_NAME / SECRET_KEY_FILENAME).read_text()
    assert persisted == value


def test_reuses_existing_value(tmp_path: Path) -> None:
    first = load_or_generate_secret_key(tmp_path)
    second = load_or_generate_secret_key(tmp_path)
    assert first == second


def test_different_projects_get_different_keys(tmp_path: Path) -> None:
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    key_a = load_or_generate_secret_key(a)
    key_b = load_or_generate_secret_key(b)
    assert key_a != key_b


@pytest.mark.skipif(os.name == "nt", reason="POSIX chmod semantics only")
def test_persisted_file_is_chmod_0600(tmp_path: Path) -> None:
    load_or_generate_secret_key(tmp_path)
    path = tmp_path / PERSISTENT_DIR_NAME / SECRET_KEY_FILENAME
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600, f"expected 0o600, got {oct(mode)}"


def test_strips_trailing_whitespace_in_existing_file(tmp_path: Path) -> None:
    dir_ = tmp_path / PERSISTENT_DIR_NAME
    dir_.mkdir()
    (dir_ / SECRET_KEY_FILENAME).write_text("hand-written-key\n  \n")
    value = load_or_generate_secret_key(tmp_path)
    assert value == "hand-written-key"


def test_empty_existing_file_triggers_regeneration(tmp_path: Path) -> None:
    dir_ = tmp_path / PERSISTENT_DIR_NAME
    dir_.mkdir()
    (dir_ / SECRET_KEY_FILENAME).write_text("   \n")
    value = load_or_generate_secret_key(tmp_path)
    assert value != ""
    assert len(value) >= 40
    # File should now contain the freshly generated value.
    assert (dir_ / SECRET_KEY_FILENAME).read_text() == value


def test_write_failure_still_returns_usable_value(tmp_path: Path) -> None:
    """If we can't persist (read-only fs), the caller still gets a working key."""

    with patch("run_site.secrets_store.os.replace", side_effect=OSError("EROFS")):
        value = load_or_generate_secret_key(tmp_path)
    assert isinstance(value, str)
    assert len(value) >= 40
