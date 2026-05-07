"""Source resolvers — turn ``--from-git`` / ``--from-path`` / CWD into a
concrete project root with deps installed."""

from django_run_site.source.deps_installer import (
    DepsResult,
    detect_deps_strategy,
    install_dependencies,
)
from django_run_site.source.from_git import (
    GitSource,
    extract_slug,
    resolve_git_source,
)
from django_run_site.source.from_path import resolve_path_source
from django_run_site.source.venv_setup import VenvResult, ensure_venv

__all__ = [
    "DepsResult",
    "GitSource",
    "VenvResult",
    "detect_deps_strategy",
    "ensure_venv",
    "extract_slug",
    "install_dependencies",
    "resolve_git_source",
    "resolve_path_source",
]
