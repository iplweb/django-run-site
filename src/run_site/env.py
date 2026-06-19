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

# Default env-var names used when the project's ``[env]`` table does not
# explicitly map a key. Lets common 12-factor consumers (django-environ,
# dj-database-url, plain ``os.environ['DJANGO_SECRET_KEY']``) Just Work
# without any runsite.toml configuration.
#
# Resolution rules:
# - Key not present in the user's [env] mapping → use this default name.
# - Key present with a string → use the user's name (overrides default).
# - Key present with ``null`` → disabled, no env var set.
DEFAULT_ENV_MAPPING: dict[str, str] = {
    "database_url": "DATABASE_URL",
    "redis_url": "REDIS_URL",
    "secret_key": "DJANGO_SECRET_KEY",
    "allowed_hosts": "DJANGO_ALLOWED_HOSTS",
}

# Loopback addresses always allowed regardless of bind. Mirrors what
# Django's ``manage.py runserver`` is willing to serve on by default.
_LOOPBACK_HOSTS: tuple[str, ...] = ("localhost", "127.0.0.1", "[::1]")


def compute_allowed_hosts(
    *,
    bind: str,
    lan_hosts: tuple[str, ...],
) -> tuple[str, ...]:
    """Build the ALLOWED_HOSTS list to export for *bind*.

    Returns ``()`` when binding to a loopback-only address — the project's
    own ``ALLOWED_HOSTS`` already covers that case (Django allows
    loopback by default in DEBUG mode), so we don't bother exporting.

    For a wildcard bind (``0.0.0.0`` / ``::``) or any explicit non-
    loopback IP, returns loopback names plus *lan_hosts* (typically the
    machine's hostname and primary LAN IP from
    :func:`run_site.host_discovery.discover_lan_hosts`). Order is stable
    and dedup'd; no wildcards are emitted — we only inject hosts the
    user can verify against the banner output.
    """

    if _is_loopback_bind(bind):
        return ()
    seen: set[str] = set()
    out: list[str] = []
    for host in (*_LOOPBACK_HOSTS, *lan_hosts):
        if host and host not in seen:
            seen.add(host)
            out.append(host)
    return tuple(out)


def _is_loopback_bind(bind: str) -> bool:
    """``True`` when *bind* points at a loopback-only interface."""

    if not bind:
        return True
    return bind == "localhost" or bind.startswith("127.") or bind in ("::1", "[::1]")


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
    secret_key: str | None = None,
    lan_hosts: tuple[str, ...] = (),
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

    *secret_key*, when provided, is exported under the configured
    ``[env].secret_key`` name (default ``DJANGO_SECRET_KEY``). Pass
    ``None`` to skip the SECRET_KEY export entirely (the project supplies
    its own).
    """

    env: dict[str, str] = dict(base_env if base_env is not None else os.environ)

    # Common safety: unbuffered Python so the multiplexer sees output live.
    env.setdefault("PYTHONUNBUFFERED", "1")

    if django_settings_module is not None:
        env.setdefault("DJANGO_SETTINGS_MODULE", django_settings_module)

    # Project-side mapping ([env]) — layered on top of default conventional
    # names so DATABASE_URL / REDIS_URL / DJANGO_SECRET_KEY get exported
    # even when the user did not configure them.
    allowed_hosts = compute_allowed_hosts(bind=config.django.runserver_bind, lan_hosts=lan_hosts)
    project_values = project_env_values(
        config, endpoints, secret_key=secret_key, allowed_hosts=allowed_hosts
    )
    effective_mapping = effective_env_mapping(config.env.mapping)
    for key, var_name in effective_mapping.items():
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
    if allowed_hosts:
        # Comma-joined; consumed by django-dev-helpers' apps.ready() to
        # union into settings.ALLOWED_HOSTS at startup. Same string the
        # default DJANGO_ALLOWED_HOSTS export carries — single source of
        # truth.
        env["DEV_HELPERS_ALLOWED_HOSTS"] = ",".join(allowed_hosts)

    if is_runserver:
        env["DJANGO_DEV_HELPERS_ENABLED"] = "1"
    else:
        env.pop("DJANGO_DEV_HELPERS_ENABLED", None)

    return env


def effective_env_mapping(
    user_mapping: Mapping[str, str | None],
) -> dict[str, str | None]:
    """Layer the user's ``[env]`` mapping on top of :data:`DEFAULT_ENV_MAPPING`.

    The user's mapping wins on every key it sets (including explicit
    ``null`` to disable the default export). Keys the user did not touch
    fall through to the defaults.
    """

    out: dict[str, str | None] = dict(DEFAULT_ENV_MAPPING)
    out.update(user_mapping)
    return out


def project_env_values(
    config: RunSiteConfig,
    endpoints: ContainerEndpoints,
    *,
    secret_key: str | None = None,
    allowed_hosts: tuple[str, ...] = (),
) -> dict[str, str]:
    """Build the lookup dict consumed by the project ``[env]`` mapping.

    Public because :mod:`run_site.env_file` reuses it as the single source
    of truth for the ``.run-site-env.sh`` export (so the sourceable file
    and the in-memory subprocess env never drift).

    Keys for a disabled service are omitted entirely — so a user with
    ``[postgres].enabled = false`` who still maps ``database_url`` will
    simply not get that var set, instead of getting a broken URL pointing
    at None:None.
    """

    out: dict[str, str] = {}
    if secret_key is not None:
        out["secret_key"] = secret_key
    if allowed_hosts:
        out["allowed_hosts"] = ",".join(allowed_hosts)
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
