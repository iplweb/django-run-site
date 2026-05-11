"""run-site — pure CLI orchestrator for local Django dev stacks.

This package intentionally has zero Django dependency. It only knows how to:

1. Resolve a project source (local path, Git URL, or CWD discovery).
2. Start PostgreSQL/Redis testcontainers.
3. Load a database dump.
4. Spawn local subprocesses: ``manage.py migrate``, superuser setup,
   ``runserver``, Celery worker/beat, and any extra processes.
5. Multiplex their logs and clean up on exit.

Django-side conveniences (autologin, dotfiles, agent help) live in the
companion ``django-dev-helpers`` package and integrate through a documented
``DEV_HELPERS_*`` env-var contract.
"""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

try:
    __version__ = _pkg_version("django-run-site")
except PackageNotFoundError:
    # Package metadata is unavailable when running from a source tree that
    # has not been installed (e.g. plain `python -m run_site` without
    # `pip install -e .`). Fall back to a sentinel rather than crashing.
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
