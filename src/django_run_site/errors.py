"""Exception hierarchy for django-run-site.

All user-facing errors derive from :class:`RunSiteError`. The CLI catches
this base class at the top level and prints a clean message instead of a
traceback. Internal/unexpected errors propagate normally.
"""

from __future__ import annotations


class RunSiteError(Exception):
    """Base class for expected, user-facing errors."""

    exit_code: int = 1


class ConfigError(RunSiteError):
    """Invalid or missing configuration."""

    exit_code = 2


class DiscoveryError(RunSiteError):
    """Could not locate ``manage.py`` or a usable Python interpreter."""

    exit_code = 3


class DockerError(RunSiteError):
    """Docker daemon unreachable or container failure."""

    exit_code = 4


class DumpError(RunSiteError):
    """Dump file missing, format unsupported, or restore failed."""

    exit_code = 5


class HookError(RunSiteError):
    """A hook failed in a non-recoverable stage."""

    exit_code = 6


class SourceError(RunSiteError):
    """Failure in ``--from-git`` / ``--from-path`` resolution."""

    exit_code = 7


class VenvError(RunSiteError):
    """Failure during venv creation or dependency install."""

    exit_code = 8
