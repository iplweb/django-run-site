"""Example hooks loaded by run-site for the test_site demo project.

These are referenced from ``runsite.toml`` and the BPP-style example config.
A hook callable receives a single ``ctx`` dict — see ``docs/hooks.md``.
"""

from __future__ import annotations

from typing import Any


def announce_post_migrate(ctx: dict[str, Any]) -> None:
    """Trivial hook — print a line confirming we ran in the project's
    interpreter, and that the context dict was wired through."""

    pg = f"{ctx['pg_host']}:{ctx['pg_port']}"
    print(f"[runsite_hooks] post_migrate ran. PG endpoint: {pg}")


def clear_password_policy(ctx: dict[str, Any]) -> None:
    """Stand-in for the BPP password-policy clear. No-op for the demo."""

    superuser = ctx.get("superuser") or {}
    print(
        f"[runsite_hooks] post_superuser: would clear password-policy flags "
        f"on user {superuser.get('username')!r} (no-op in test_site)"
    )
