"""Tests for the browser-open decision tree in cli._resolve_browser_decision."""

from __future__ import annotations

from dataclasses import replace

import pytest

from run_site.cli import _resolve_browser_decision
from run_site.config import RunSiteConfig
from run_site.display_detect import HeadlessSignal

HEADLESS = HeadlessSignal(headless=True, reason="SSH session ($SSH_CONNECTION set)")
LOCAL = HeadlessSignal(headless=False, reason="local macOS session")
HOMEPAGE = "http://localhost:8123/"


@pytest.fixture
def config(minimal_config: RunSiteConfig) -> RunSiteConfig:
    return minimal_config


def _with_open_browser(cfg: RunSiteConfig, value: object) -> RunSiteConfig:
    return replace(cfg, django=replace(cfg.django, open_browser=value))  # type: ignore[arg-type]


def test_cli_browser_force_overrides_headless(config: RunSiteConfig) -> None:
    should, status = _resolve_browser_decision(
        config=config, cli_choice=True, signal=HEADLESS, homepage=HOMEPAGE
    )
    assert should is True
    assert "--browser" in status


def test_cli_no_browser_overrides_force_config(config: RunSiteConfig) -> None:
    cfg = _with_open_browser(config, True)
    should, status = _resolve_browser_decision(
        config=cfg, cli_choice=False, signal=LOCAL, homepage=HOMEPAGE
    )
    assert should is False
    assert "--no-browser" in status


def test_config_true_overrides_headless(config: RunSiteConfig) -> None:
    """[django].open_browser = true means "always open, I know what I'm doing"
    — useful for X11-forwarded SSH where the heuristic can't tell."""

    cfg = _with_open_browser(config, True)
    should, status = _resolve_browser_decision(
        config=cfg, cli_choice=None, signal=HEADLESS, homepage=HOMEPAGE
    )
    assert should is True
    assert "open_browser = true" in status


def test_config_false_skips_even_on_graphical_session(config: RunSiteConfig) -> None:
    cfg = _with_open_browser(config, False)
    should, status = _resolve_browser_decision(
        config=cfg, cli_choice=None, signal=LOCAL, homepage=HOMEPAGE
    )
    assert should is False
    assert "open_browser = false" in status


def test_auto_skips_when_headless(config: RunSiteConfig) -> None:
    should, status = _resolve_browser_decision(
        config=config, cli_choice=None, signal=HEADLESS, homepage=HOMEPAGE
    )
    assert should is False
    assert "skipped" in status
    assert "SSH_CONNECTION" in status
    assert "--browser" in status  # the override hint


def test_auto_opens_when_local(config: RunSiteConfig) -> None:
    should, status = _resolve_browser_decision(
        config=config, cli_choice=None, signal=LOCAL, homepage=HOMEPAGE
    )
    assert should is True
    assert HOMEPAGE in status
    assert "local macOS" in status
