#!/usr/bin/env python
"""Django's command-line utility for the test_site project."""

from __future__ import annotations

import os
import sys


def main() -> None:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "test_site.settings")
    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:
        raise ImportError(
            "Couldn't import Django. Run `uv sync` or `pip install django` first."
        ) from exc
    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
