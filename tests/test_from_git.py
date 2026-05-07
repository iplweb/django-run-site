"""Git source resolution — slug extraction, runner mocking."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest

from run_site.errors import SourceError
from run_site.source.from_git import (
    GitRunner,
    extract_slug,
    resolve_checkout_dir,
    resolve_git_source,
)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://github.com/iplweb/bpp.git", "iplweb/bpp"),
        ("git@github.com:foo/bar.git", "foo/bar"),
        ("https://gitlab.com/group/sub/proj", "sub/proj"),
        ("https://gitlab.com/group/sub/proj.git", "sub/proj"),
        ("https://gitlab.com/group/sub/proj/", "sub/proj"),
    ],
)
def test_extract_slug(url: str, expected: str) -> None:
    assert extract_slug(url) == expected


def test_extract_slug_fallback_to_hash() -> None:
    """A URL with no ``[/:]`` separator falls back to a hash slug."""

    weird = "weird-no-separators-just-text"
    slug = extract_slug(weird)
    # Fallback: 12-char hex.
    assert len(slug) == 12
    assert all(c in "0123456789abcdef" for c in slug)


def test_resolve_checkout_dir_uses_cache_by_default(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "run_site.source.from_git.CACHE_ROOT",
        tmp_path / "cache",
    )
    path, cache_owned, cleanup = resolve_checkout_dir(
        "https://github.com/foo/bar.git",
        explicit_checkout_path=None,
        no_cache=False,
    )
    assert (tmp_path / "cache").resolve() in path.parents or path.parent == (
        tmp_path / "cache" / "foo"
    )
    assert cache_owned is True
    assert cleanup is False


def test_resolve_checkout_dir_explicit_path(tmp_path) -> None:
    explicit = tmp_path / "elsewhere"
    path, cache_owned, cleanup = resolve_checkout_dir(
        "https://github.com/foo/bar.git",
        explicit_checkout_path=str(explicit),
        no_cache=False,
    )
    assert path == explicit.resolve()
    assert cache_owned is False
    assert cleanup is False


def test_resolve_checkout_dir_no_cache_uses_tmp(tmp_path) -> None:
    path, cache_owned, cleanup = resolve_checkout_dir(
        "https://github.com/foo/bar.git",
        explicit_checkout_path=None,
        no_cache=True,
    )
    assert path.exists()
    assert cache_owned is False
    assert cleanup is True
    # Clean up the tmpdir we just made.
    import shutil

    shutil.rmtree(path, ignore_errors=True)


class RecordingGitRunner(GitRunner):
    """Captures every git invocation; pretends `status --porcelain` is empty."""

    def __init__(self) -> None:
        self.calls: list[Sequence[str]] = []

    def run(self, argv: Sequence[str]) -> None:
        self.calls.append(tuple(argv))

    def capture(self, argv: Sequence[str]) -> str:
        self.calls.append(tuple(argv))
        return ""


def fake_confirm(*, url: str, checkout_path: Path) -> bool:
    return True


def test_resolve_git_source_clone_when_missing(tmp_path) -> None:
    runner = RecordingGitRunner()
    target = tmp_path / "checkout"
    source = resolve_git_source(
        url="https://example.com/foo/bar.git",
        branch="main",
        tag=None,
        commit=None,
        checkout_path=str(target),
        no_cache=False,
        no_pull=False,
        force_reset=False,
        yes=True,
        runner=runner,
        confirm=fake_confirm,
    )
    assert source.url == "https://example.com/foo/bar.git"
    assert source.checkout_path == target.resolve()
    # First call: clone. Then checkout main + pull.
    cmd_strs = [" ".join(c) for c in runner.calls]
    assert any("git clone" in s for s in cmd_strs)
    assert any("checkout main" in s for s in cmd_strs)


def test_resolve_git_source_user_owned_with_dirty_state_errors(tmp_path) -> None:
    target = tmp_path / "user-owned"
    target.mkdir()
    (target / ".git").mkdir()

    class DirtyRunner(GitRunner):
        def run(self, argv: Sequence[str]) -> None:
            pass

        def capture(self, argv: Sequence[str]) -> str:
            return " M src/foo.py\n"

    with pytest.raises(SourceError, match="uncommitted changes"):
        resolve_git_source(
            url="https://example.com/foo/bar.git",
            branch="main",
            tag=None,
            commit=None,
            checkout_path=str(target),
            no_cache=False,
            no_pull=False,
            force_reset=False,
            yes=True,
            runner=DirtyRunner(),
            confirm=fake_confirm,
        )


def test_resolve_git_source_force_reset_overrides_user_owned(tmp_path) -> None:
    target = tmp_path / "user-owned"
    target.mkdir()
    (target / ".git").mkdir()
    runner = RecordingGitRunner()
    resolve_git_source(
        url="https://example.com/foo/bar.git",
        branch="main",
        tag=None,
        commit=None,
        checkout_path=str(target),
        no_cache=False,
        no_pull=False,
        force_reset=True,
        yes=True,
        runner=runner,
        confirm=fake_confirm,
    )
    cmd_strs = [" ".join(c) for c in runner.calls]
    assert any("reset --hard" in s for s in cmd_strs)


def test_resolve_git_source_multiple_refs_rejected(tmp_path) -> None:
    runner = RecordingGitRunner()
    with pytest.raises(SourceError, match="Multiple refs"):
        resolve_git_source(
            url="https://example.com/foo/bar.git",
            branch="main",
            tag="v1",
            commit=None,
            checkout_path=str(tmp_path / "x"),
            no_cache=False,
            no_pull=False,
            force_reset=False,
            yes=True,
            runner=runner,
            confirm=fake_confirm,
        )
