"""Build subprocess environments — DEV_HELPERS_* contract + project [env]
mapping.

Two consumers:

- The *project* (its ``settings.py``) reads project-specific env-var names
  from the ``[env]`` mapping in ``runsite.toml``.
- The *companion package* :mod:`django_dev_helpers` reads stable
  ``DEV_HELPERS_*`` names that never change between releases.

The CLI is the only place that knows both naming schemes — values are
written under both names (intentional double-set so consumers that look
up either name see a value).
"""

from __future__ import annotations

import os
import re
import secrets
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass

from run_site.config import RunSiteConfig

REDACT_VALUE = "<redacted>"
SECRET_RE = re.compile(r"(?i).*(TOKEN|PASSWORD|SECRET|API_KEY).*")


@dataclass(frozen=True)
class ContainerEndpoints:
    """Subset of :class:`~run_site.containers.RunSiteContainers` info
    needed to build env vars. Decoupled so env builder doesn't depend on
    docker/testcontainers.

    Any of ``pg_*`` / ``redis_*`` / ``sqlite_path`` may be ``None`` when
    the corresponding service was disabled (``[postgres].enabled =
    false`` / ``[redis].enabled = false`` / ``[sqlite].enabled = false``)
    and never started. Consumers must treat those as "not available" and
    skip emitting the related env vars.
    """

    pg_host: str | None
    pg_port: int | None
    redis_host: str | None
    redis_port: int | None
    sqlite_path: str | None = None


def generate_autologin_token() -> str:
    """Cryptographically-strong autologin token."""

    return secrets.token_urlsafe(32)


def build_subprocess_env(
    *,
    config: RunSiteConfig,
    endpoints: ContainerEndpoints,
    autologin_token: str,
    runserver_port: int | None,
    is_runserver: bool,
    django_settings_module: str | None = None,
    base_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build the env dict passed to a subprocess (migrate, runserver, etc).

    The full :data:`os.environ` is inherited via *base_env* (defaults to
    ``os.environ``) and then layered with project [env] mapping plus the
    DEV_HELPERS_* contract.

    ``is_runserver=True`` adds ``DJANGO_DEV_HELPERS_ENABLED=1`` (the hard
    activation flag — only the runserver subprocess runs the helper app).

    When *django_settings_module* is provided, it is set as
    ``DJANGO_SETTINGS_MODULE`` via ``setdefault`` — so we don't clobber a
    value the user already exported in their shell, but we do supply one
    for subprocesses (notably ``python -m celery``) that don't go through
    ``manage.py`` and therefore never get its ``setdefault`` treatment.
    """

    env: dict[str, str] = dict(base_env if base_env is not None else os.environ)

    # Common safety: unbuffered Python so the multiplexer sees output live.
    env.setdefault("PYTHONUNBUFFERED", "1")

    if django_settings_module is not None:
        env.setdefault("DJANGO_SETTINGS_MODULE", django_settings_module)

    # Project-side mapping ([env]).
    project_values = _project_values(config, endpoints)
    for key, var_name in config.env.mapping.items():
        if var_name is None:
            continue
        value = project_values.get(key)
        if value is not None:
            env[var_name] = value

    # Project extras ([env.extra]).
    env.update(config.env.extra)

    # DEV_HELPERS_* contract — only set per-service vars when that
    # service is enabled and we therefore have real endpoint values.
    # AUTOLOGIN_* and PROJECT_ROOT are unconditional.
    env["DEV_HELPERS_AUTOLOGIN_TOKEN"] = autologin_token
    env["DEV_HELPERS_AUTOLOGIN_USERNAME"] = config.superuser.username
    if config.postgres.enabled and endpoints.pg_host is not None and endpoints.pg_port is not None:
        env["DEV_HELPERS_DB_HOST"] = endpoints.pg_host
        env["DEV_HELPERS_DB_PORT"] = str(endpoints.pg_port)
        env["DEV_HELPERS_DB_NAME"] = config.postgres.db
        env["DEV_HELPERS_DB_USER"] = config.postgres.user
    elif config.sqlite.enabled and endpoints.sqlite_path is not None:
        # SQLite has no host/port, but DEV_HELPERS_DB_NAME maps to the
        # absolute path so consumers that build their own DATABASES dict
        # from these still work.
        env["DEV_HELPERS_DB_NAME"] = endpoints.sqlite_path
    if (
        config.redis.enabled
        and endpoints.redis_host is not None
        and endpoints.redis_port is not None
    ):
        env["DEV_HELPERS_REDIS_HOST"] = endpoints.redis_host
        env["DEV_HELPERS_REDIS_PORT"] = str(endpoints.redis_port)
    env["DEV_HELPERS_PROJECT_ROOT"] = str(config.project_root.resolve())
    if runserver_port is not None:
        env["DEV_HELPERS_PORT"] = str(runserver_port)

    if is_runserver:
        env["DJANGO_DEV_HELPERS_ENABLED"] = "1"
    else:
        env.pop("DJANGO_DEV_HELPERS_ENABLED", None)

    return env


def _project_values(config: RunSiteConfig, endpoints: ContainerEndpoints) -> dict[str, str]:
    """Build the lookup dict consumed by the project ``[env]`` mapping.

    Keys for a disabled service are omitted entirely — so a user with
    ``[postgres].enabled = false`` who still maps ``database_url`` will
    simply not get that var set, instead of getting a broken URL pointing
    at None:None.
    """

    out: dict[str, str] = {}
    pg = config.postgres
    if pg.enabled and endpoints.pg_host is not None and endpoints.pg_port is not None:
        quoted_pwd = urllib.parse.quote_plus(pg.password)
        quoted_user = urllib.parse.quote_plus(pg.user)
        database_url = (
            f"postgres{pg.driver}://{quoted_user}:{quoted_pwd}"
            f"@{endpoints.pg_host}:{endpoints.pg_port}/{pg.db}"
        )
        out["database_url"] = database_url
        out["db_host"] = endpoints.pg_host
        out["db_port"] = str(endpoints.pg_port)
        out["db_name"] = pg.db
        out["db_user"] = pg.user
        out["db_password"] = pg.password
    elif config.sqlite.enabled and endpoints.sqlite_path is not None:
        # SQLite mode: only ``database_url`` and ``db_name`` make sense.
        # PG-only keys (db_host/db_port/db_user/db_password) stay unset
        # so they don't get mapped to nonsense values.
        out["database_url"] = f"sqlite:///{endpoints.sqlite_path}"
        out["db_name"] = endpoints.sqlite_path
    if (
        config.redis.enabled
        and endpoints.redis_host is not None
        and endpoints.redis_port is not None
    ):
        redis_url = f"redis://{endpoints.redis_host}:{endpoints.redis_port}/{config.redis.db}"
        out["redis_url"] = redis_url
        out["redis_host"] = endpoints.redis_host
        out["redis_port"] = str(endpoints.redis_port)
    return out


def format_env_for_print(
    env: Mapping[str, str], *, redact: bool, only_keys: set[str] | None = None
) -> str:
    """Render env vars for ``--print-env``. By default redacts secrets
    matching :data:`SECRET_RE`."""

    items = sorted(env.items())
    if only_keys is not None:
        items = [(k, v) for k, v in items if k in only_keys]
    lines = []
    for key, value in items:
        out_value = REDACT_VALUE if (redact and SECRET_RE.match(key)) else value
        lines.append(f"{key}={out_value}")
    return "\n".join(lines)
