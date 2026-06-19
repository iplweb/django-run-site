"""Tests for run_site.fileutil — secret-bearing file writes."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from run_site.fileutil import write_private_text


def test_writes_content(tmp_path: Path) -> None:
    path = tmp_path / "secret.txt"
    write_private_text(path, "top secret\n")
    assert path.read_text() == "top secret\n"


@pytest.mark.skipif(os.name == "nt", reason="POSIX file-mode bits")
def test_new_file_is_owner_only(tmp_path: Path) -> None:
    path = tmp_path / "secret.txt"
    write_private_text(path, "x")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.skipif(os.name == "nt", reason="POSIX file-mode bits")
def test_tightens_a_preexisting_world_readable_file(tmp_path: Path) -> None:
    path = tmp_path / "secret.txt"
    path.write_text("old")
    path.chmod(0o644)
    write_private_text(path, "new")
    assert path.read_text() == "new"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
