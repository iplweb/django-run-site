"""Load and validate ``runsite.toml`` / ``[tool.run-site]`` config.

This module is intentionally side-effect-free. ``load_config`` returns a
fully-validated :class:`RunSiteConfig` dataclass; CLI flags are merged on
top of it via :meth:`RunSiteConfig.with_cli_overrides`.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

from run_site.errors import ConfigError

DumpStrategy = Literal["auto", "init-script", "post-start"]
RyukMode = Literal["auto", "true", "false"]
SourceType = Literal["git", "path"]
HookType = Literal["command", "django"]
LogColor = Literal["cyan", "green", "yellow", "magenta", "blue", "red", "white"]

VALID_LOG_COLORS: frozenset[str] = frozenset(
    ["cyan", "green", "yellow", "magenta", "blue", "red", "white"]
)
RESERVED_PROCESS_NAMES: frozenset[str] = frozenset(["web", "pg", "redis", "celery", "celery-beat"])
ENV_KEYS: frozenset[str] = frozenset(
    [
        "database_url",
        "db_host",
        "db_port",
        "db_name",
        "db_user",
        "db_password",
        "redis_url",
        "redis_host",
        "redis_port",
    ]
)
DRIVER_RE = re.compile(r"^(\+[A-Za-z0-9_]+|q[A-Za-z0-9_]*)?$")


@dataclass(frozen=True)
class PythonConfig:
    """Resolution policy for the local Python interpreter."""

    executable: str | None = "auto"
    command: tuple[str, ...] | None = None


@dataclass(frozen=True)
class PostgresConfig:
    enabled: bool = True
    image: str = "postgres:16"
    user: str = "django"
    password: str = "password"
    db: str = "django"
    driver: str = ""
    stream_logs: bool = True
    env: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class RedisConfig:
    enabled: bool = True
    image: str = "redis:7-alpine"
    db: int = 0


@dataclass(frozen=True)
class ContainersConfig:
    ryuk: RyukMode = "auto"


@dataclass(frozen=True)
class DumpConfig:
    default_path: str | None = None
    strategy: DumpStrategy = "auto"
    restore_jobs: int | str = "auto"
    fail_fast: bool = True


@dataclass(frozen=True)
class EnvConfig:
    """Project-side env mapping plus arbitrary extras."""

    mapping: Mapping[str, str | None] = field(default_factory=dict)
    extra: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class DjangoConfig:
    runserver_bind: str = "127.0.0.1"
    runserver_display_host: str = "localhost"
    browser_probe_path: str = "/admin/login/"
    migrate: bool = True
    probe_timeout: float = 60.0
    # Override the web process. When None, the orchestrator runs
    # ``<python> manage.py runserver <bind>:<port>``. When set, the
    # tokens go through the same template-substitution as
    # ``[[extra_processes]].command`` (``{python}``, ``{manage_py}``,
    # ``{manage_dir}``, ``{project_root}``, ``{port}``, ``{bind}``).
    web_command: tuple[str, ...] | None = None


@dataclass(frozen=True)
class SuperuserConfig:
    enabled: bool = True
    username: str = "admin"
    password: str = "admin"
    email: str = "admin@example.com"
    overwrite: bool = True


@dataclass(frozen=True)
class CeleryConfig:
    app: str | None = None
    enabled: bool = False
    worker_pool: str = "solo"
    worker_log_level: str = "info"
    worker_extra_args: tuple[str, ...] = ()
    with_beat: bool = False
    beat_log_level: str = "info"
    beat_extra_args: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExtraProcess:
    name: str
    command: tuple[str, ...]
    cwd: str = "."
    enabled_default: bool = False
    color: LogColor = "blue"
    cli_flag: str | None = None
    cli_disable_flag: str | None = None


@dataclass(frozen=True)
class HookCliArg:
    flag: str
    dest: str
    metavar: str | None = None
    help: str | None = None
    default: Any = None
    required: bool = False


@dataclass(frozen=True)
class HookConfig:
    stage: str
    type: HookType
    command: tuple[str, ...] | None = None
    callable: str | None = None
    timeout: float | None = None
    cli_disable_flag: str | None = None
    cli_args: tuple[HookCliArg, ...] = ()


@dataclass(frozen=True)
class BannerConfig:
    title: str = "run-site is running"
    show_db_credentials: bool = True
    suggest_dev_helpers: bool = True


@dataclass(frozen=True)
class SourceConfig:
    type: SourceType | None = None
    url: str | None = None
    branch: str | None = None
    tag: str | None = None
    commit: str | None = None
    path: str | None = None
    checkout_path: str | None = None
    no_cache: bool = False
    no_pull: bool = False
    no_install: bool = False


@dataclass(frozen=True)
class RunSiteConfig:
    project_root: Path
    config_path: Path | None
    project_slug: str
    manage_py: str | None
    python: PythonConfig
    postgres: PostgresConfig
    redis: RedisConfig
    containers: ContainersConfig
    dump: DumpConfig
    env: EnvConfig
    django: DjangoConfig
    superuser: SuperuserConfig
    celery: CeleryConfig
    extra_processes: tuple[ExtraProcess, ...]
    hooks: tuple[HookConfig, ...]
    banner: BannerConfig
    source: SourceConfig

    def with_project_root(self, new_root: Path) -> RunSiteConfig:
        return replace(self, project_root=new_root)


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


def find_config(start: Path) -> Path | None:
    """Walk parents of *start* looking for ``runsite.toml`` or
    ``[tool.run-site]`` in ``pyproject.toml``."""

    for candidate in [start, *start.parents]:
        runsite = candidate / "runsite.toml"
        if runsite.is_file():
            return runsite
        pyproj = candidate / "pyproject.toml"
        if pyproj.is_file():
            try:
                with pyproj.open("rb") as fh:
                    data = tomllib.load(fh)
            except tomllib.TOMLDecodeError:
                continue
            if "tool" in data and "run-site" in data["tool"]:
                return pyproj
    return None


def load_config(
    *,
    config_path: Path | None,
    project_root: Path,
) -> RunSiteConfig:
    """Load TOML config from *config_path* (or auto-discover) into a
    validated :class:`RunSiteConfig`. ``project_root`` is the resolved
    project directory used as the anchor for relative paths."""

    raw: Mapping[str, Any]
    resolved_path: Path | None = config_path

    if config_path is not None:
        if not config_path.is_file():
            raise ConfigError(f"Config file not found: {config_path}")
        raw = _read_toml_section(config_path)
    else:
        discovered = find_config(project_root)
        resolved_path = discovered
        raw = _read_toml_section(discovered) if discovered is not None else {}

    return _build_config(raw=raw, project_root=project_root, config_path=resolved_path)


def _read_toml_section(path: Path) -> Mapping[str, Any]:
    """Load *path* and return its run-site config section.

    For ``runsite.toml`` it's the whole file. For ``pyproject.toml`` it's
    ``[tool.run-site]``.
    """

    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Invalid TOML in {path}: {exc}") from exc

    if path.name == "pyproject.toml":
        return data.get("tool", {}).get("run-site", {})
    return data


def _build_config(
    *,
    raw: Mapping[str, Any],
    project_root: Path,
    config_path: Path | None,
) -> RunSiteConfig:
    explicit_slug = raw.get("project_slug")
    if explicit_slug is None:
        # No-config / no-slug case: derive from directory name, but sanitize
        # so dirs with spaces, capital ASCII-friendly chars, or other oddities
        # don't trip the strict regex.
        project_slug = _sanitize_default_slug(project_root.name)
    else:
        if not isinstance(explicit_slug, str):
            raise ConfigError(
                f"Key 'project_slug' must be a string, got {type(explicit_slug).__name__}"
            )
        project_slug = explicit_slug
        if not project_slug or not re.fullmatch(r"[A-Za-z0-9_.-]+", project_slug):
            raise ConfigError(f"Invalid project_slug={project_slug!r}: must match [A-Za-z0-9_.-]+")
    manage_py = _opt_str(raw, "manage_py")

    return RunSiteConfig(
        project_root=project_root,
        config_path=config_path,
        project_slug=project_slug,
        manage_py=manage_py,
        python=_build_python(raw.get("python", {})),
        postgres=_build_postgres(raw.get("postgres", {})),
        redis=_build_redis(raw.get("redis", {})),
        containers=_build_containers(raw.get("containers", {})),
        dump=_build_dump(raw.get("dump", {})),
        env=_build_env(raw.get("env", {})),
        django=_build_django(raw.get("django", {})),
        superuser=_build_superuser(raw.get("superuser", {})),
        celery=_build_celery(raw.get("celery", {})),
        extra_processes=_build_extras(raw.get("extra_processes", [])),
        hooks=_build_hooks(raw.get("hooks", {})),
        banner=_build_banner(raw.get("banner", {})),
        source=_build_source(raw.get("source", {})),
    )


def _build_python(raw: Mapping[str, Any]) -> PythonConfig:
    executable = _opt_str(raw, "executable", default="auto")
    command_raw = raw.get("command")
    command: tuple[str, ...] | None = None
    if command_raw is not None:
        if not isinstance(command_raw, list) or not all(isinstance(x, str) for x in command_raw):
            raise ConfigError("[python].command must be a list of strings")
        command = tuple(command_raw)

    if command is not None and executable not in (None, "auto", ""):
        raise ConfigError(
            "[python].command and [python].executable cannot both be set "
            "(one config = one resolution path)"
        )
    return PythonConfig(executable=executable, command=command)


def _build_postgres(raw: Mapping[str, Any]) -> PostgresConfig:
    driver = _str(raw, "driver", default="")
    if not DRIVER_RE.match(driver):
        raise ConfigError(
            f"[postgres].driver={driver!r} is invalid: must start with '+' or 'q', or be empty"
        )
    env_raw = raw.get("env", {})
    if not isinstance(env_raw, Mapping):
        raise ConfigError("[postgres.env] must be a mapping")
    env: dict[str, str] = {}
    for key, value in env_raw.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise ConfigError("[postgres.env] keys and values must be strings")
        env[key] = value
    return PostgresConfig(
        enabled=_bool(raw, "enabled", default=True),
        image=_str(raw, "image", default="postgres:16"),
        user=_str(raw, "user", default="django"),
        password=_str(raw, "password", default="password"),
        db=_str(raw, "db", default="django"),
        driver=driver,
        stream_logs=_bool(raw, "stream_logs", default=True),
        env=env,
    )


def _build_redis(raw: Mapping[str, Any]) -> RedisConfig:
    db = raw.get("db", 0)
    if not isinstance(db, int) or db < 0:
        raise ConfigError("[redis].db must be a non-negative int")
    return RedisConfig(
        enabled=_bool(raw, "enabled", default=True),
        image=_str(raw, "image", default="redis:7-alpine"),
        db=db,
    )


def _build_containers(raw: Mapping[str, Any]) -> ContainersConfig:
    ryuk_raw = raw.get("ryuk", "auto")
    if ryuk_raw not in ("auto", True, False):
        raise ConfigError("[containers].ryuk must be 'auto', true, or false")
    ryuk: RyukMode = "auto" if ryuk_raw == "auto" else ("true" if ryuk_raw is True else "false")
    return ContainersConfig(ryuk=ryuk)


def _build_dump(raw: Mapping[str, Any]) -> DumpConfig:
    strategy = _str(raw, "strategy", default="auto")
    if strategy not in ("auto", "init-script", "post-start"):
        raise ConfigError(
            f"[dump].strategy={strategy!r} must be one of 'auto', 'init-script', 'post-start'"
        )
    restore_jobs_raw = raw.get("restore_jobs", "auto")
    if not (
        restore_jobs_raw == "auto" or (isinstance(restore_jobs_raw, int) and restore_jobs_raw >= 1)
    ):
        raise ConfigError("[dump].restore_jobs must be a positive int or 'auto'")
    return DumpConfig(
        default_path=_opt_str(raw, "default_path"),
        strategy=strategy,  # type: ignore[arg-type]
        restore_jobs=restore_jobs_raw,
        fail_fast=_bool(raw, "fail_fast", default=True),
    )


def _build_env(raw: Mapping[str, Any]) -> EnvConfig:
    extra_raw = raw.get("extra", {})
    if not isinstance(extra_raw, Mapping):
        raise ConfigError("[env.extra] must be a mapping")
    extra: dict[str, str] = {}
    for k, v in extra_raw.items():
        if not isinstance(k, str) or not isinstance(v, str):
            raise ConfigError("[env.extra] keys and values must be strings")
        extra[k] = v

    mapping: dict[str, str | None] = {}
    for key, value in raw.items():
        if key in ("extra",):
            continue
        if key not in ENV_KEYS:
            raise ConfigError(f"Unknown [env] key: {key!r}. Allowed: {sorted(ENV_KEYS)}")
        if value is None:
            mapping[key] = None
        elif isinstance(value, str):
            mapping[key] = value
        else:
            raise ConfigError(f"[env].{key} must be a string or null, got {type(value).__name__}")
    return EnvConfig(mapping=mapping, extra=extra)


def _build_django(raw: Mapping[str, Any]) -> DjangoConfig:
    timeout = raw.get("probe_timeout", 60.0)
    if not isinstance(timeout, int | float) or timeout <= 0:
        raise ConfigError("[django].probe_timeout must be a positive number")
    web_command_raw = raw.get("web_command")
    web_command: tuple[str, ...] | None = None
    if web_command_raw is not None:
        if not isinstance(web_command_raw, list) or not all(
            isinstance(x, str) for x in web_command_raw
        ):
            raise ConfigError("[django].web_command must be a list of strings")
        if not web_command_raw:
            raise ConfigError("[django].web_command must not be empty")
        web_command = tuple(web_command_raw)
    return DjangoConfig(
        runserver_bind=_str(raw, "runserver_bind", default="127.0.0.1"),
        runserver_display_host=_str(raw, "runserver_display_host", default="localhost"),
        browser_probe_path=_str(raw, "browser_probe_path", default="/admin/login/"),
        migrate=_bool(raw, "migrate", default=True),
        probe_timeout=float(timeout),
        web_command=web_command,
    )


def _build_superuser(raw: Mapping[str, Any]) -> SuperuserConfig:
    return SuperuserConfig(
        enabled=_bool(raw, "enabled", default=True),
        username=_str(raw, "username", default="admin"),
        password=_str(raw, "password", default="admin"),
        email=_str(raw, "email", default="admin@example.com"),
        overwrite=_bool(raw, "overwrite", default=True),
    )


def _build_celery(raw: Mapping[str, Any]) -> CeleryConfig:
    extra_raw = raw.get("worker_extra_args", [])
    if not isinstance(extra_raw, list) or not all(isinstance(x, str) for x in extra_raw):
        raise ConfigError("[celery].worker_extra_args must be a list of strings")
    beat_extra_raw = raw.get("beat_extra_args", [])
    if not isinstance(beat_extra_raw, list) or not all(isinstance(x, str) for x in beat_extra_raw):
        raise ConfigError("[celery].beat_extra_args must be a list of strings")
    return CeleryConfig(
        app=_opt_str(raw, "app"),
        enabled=_bool(raw, "enabled", default=False),
        worker_pool=_str(raw, "worker_pool", default="solo"),
        worker_log_level=_str(raw, "worker_log_level", default="info"),
        worker_extra_args=tuple(extra_raw),
        with_beat=_bool(raw, "with_beat", default=False),
        beat_log_level=_str(raw, "beat_log_level", default="info"),
        beat_extra_args=tuple(beat_extra_raw),
    )


def _build_extras(raw: Any) -> tuple[ExtraProcess, ...]:
    if not isinstance(raw, list):
        raise ConfigError("[[extra_processes]] must be a list of tables")
    out: list[ExtraProcess] = []
    seen_names: set[str] = set()
    for idx, entry in enumerate(raw):
        if not isinstance(entry, Mapping):
            raise ConfigError(f"extra_processes[{idx}] must be a table")
        name = _str(entry, "name")
        if not name:
            raise ConfigError(f"extra_processes[{idx}].name is required")
        if name in seen_names:
            raise ConfigError(f"duplicate extra_processes name: {name!r}")
        if name in RESERVED_PROCESS_NAMES:
            raise ConfigError(
                f"extra_processes.name={name!r} clashes with a reserved name "
                f"({sorted(RESERVED_PROCESS_NAMES)})"
            )
        seen_names.add(name)
        command_raw = entry.get("command")
        if not isinstance(command_raw, list) or not all(isinstance(x, str) for x in command_raw):
            raise ConfigError(f"extra_processes[{idx}].command must be a list of strings")
        color = _str(entry, "color", default="blue")
        if color not in VALID_LOG_COLORS:
            raise ConfigError(
                f"extra_processes[{idx}].color={color!r} must be one of {sorted(VALID_LOG_COLORS)}"
            )
        out.append(
            ExtraProcess(
                name=name,
                command=tuple(command_raw),
                cwd=_str(entry, "cwd", default="."),
                enabled_default=_bool(entry, "enabled_default", default=False),
                color=color,  # type: ignore[arg-type]
                cli_flag=_opt_str(entry, "cli_flag"),
                cli_disable_flag=_opt_str(entry, "cli_disable_flag"),
            )
        )
    return tuple(out)


def _build_hooks(raw: Any) -> tuple[HookConfig, ...]:
    if not isinstance(raw, Mapping):
        raise ConfigError("[hooks] must be a mapping of stage name to list of hooks")
    valid_stages = {
        "pre_containers",
        "post_containers",
        "pre_dump",
        "post_dump",
        "post_migrate",
        "post_superuser",
        "pre_serve",
        "post_stop",
    }
    out: list[HookConfig] = []
    seen_flags: dict[str, str] = {}
    seen_dests: dict[str, str] = {}
    for stage, entries in raw.items():
        if stage not in valid_stages:
            raise ConfigError(f"Unknown hook stage: {stage!r}. Valid: {sorted(valid_stages)}")
        if not isinstance(entries, list):
            raise ConfigError(f"[hooks.{stage}] must be a list of tables")
        for idx, entry in enumerate(entries):
            if not isinstance(entry, Mapping):
                raise ConfigError(f"hooks.{stage}[{idx}] must be a table")
            hook = _build_hook(stage, idx, entry, seen_flags, seen_dests)
            out.append(hook)
    return tuple(out)


def _build_hook(
    stage: str,
    idx: int,
    entry: Mapping[str, Any],
    seen_flags: dict[str, str],
    seen_dests: dict[str, str],
) -> HookConfig:
    type_ = _str(entry, "type")
    if type_ not in ("command", "django"):
        raise ConfigError(f"hooks.{stage}[{idx}].type must be 'command' or 'django', got {type_!r}")
    timeout_raw = entry.get("timeout")
    if timeout_raw is not None and not isinstance(timeout_raw, int | float):
        raise ConfigError(f"hooks.{stage}[{idx}].timeout must be a number or null")
    timeout = float(timeout_raw) if timeout_raw is not None else None

    command: tuple[str, ...] | None = None
    callable_: str | None = None
    if type_ == "command":
        cmd_raw = entry.get("command")
        if not isinstance(cmd_raw, list) or not all(isinstance(x, str) for x in cmd_raw):
            raise ConfigError(f"hooks.{stage}[{idx}].command must be a list of strings")
        command = tuple(cmd_raw)
    else:
        callable_ = _str(entry, "callable")
        if ":" not in callable_:
            raise ConfigError(
                f"hooks.{stage}[{idx}].callable={callable_!r} must be 'module.path:function_name'"
            )

    cli_args_raw = entry.get("cli_args", [])
    if not isinstance(cli_args_raw, list):
        raise ConfigError(f"hooks.{stage}[{idx}].cli_args must be a list of tables")
    cli_args: list[HookCliArg] = []
    for arg_idx, arg_entry in enumerate(cli_args_raw):
        if not isinstance(arg_entry, Mapping):
            raise ConfigError(f"hooks.{stage}[{idx}].cli_args[{arg_idx}] must be a table")
        flag = _str(arg_entry, "flag")
        dest = _str(arg_entry, "dest")
        if not flag.startswith("-"):
            raise ConfigError(f"hooks.{stage}[{idx}].cli_args[{arg_idx}].flag must start with '-'")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", dest):
            raise ConfigError(
                f"hooks.{stage}[{idx}].cli_args[{arg_idx}].dest={dest!r} "
                "must be a valid Python identifier"
            )
        if flag in seen_flags:
            raise ConfigError(f"Duplicate hook CLI flag: {flag!r} (also in {seen_flags[flag]})")
        seen_flags[flag] = f"{stage}[{idx}]"
        if dest in seen_dests and seen_dests[dest] != f"{stage}[{idx}]/{flag}":
            raise ConfigError(f"Duplicate hook CLI dest: {dest!r} (also in {seen_dests[dest]})")
        seen_dests[dest] = f"{stage}[{idx}]/{flag}"
        cli_args.append(
            HookCliArg(
                flag=flag,
                dest=dest,
                metavar=_opt_str(arg_entry, "metavar"),
                help=_opt_str(arg_entry, "help"),
                default=arg_entry.get("default"),
                required=_bool(arg_entry, "required", default=False),
            )
        )

    cli_disable_flag = _opt_str(entry, "cli_disable_flag")
    if cli_disable_flag is not None:
        if not cli_disable_flag.startswith("-"):
            raise ConfigError(f"hooks.{stage}[{idx}].cli_disable_flag must start with '-'")
        if cli_disable_flag in seen_flags:
            raise ConfigError(
                f"Duplicate hook CLI flag: {cli_disable_flag!r} "
                f"(also in {seen_flags[cli_disable_flag]})"
            )
        seen_flags[cli_disable_flag] = f"{stage}[{idx}]"

    return HookConfig(
        stage=stage,
        type=type_,  # type: ignore[arg-type]
        command=command,
        callable=callable_,
        timeout=timeout,
        cli_disable_flag=cli_disable_flag,
        cli_args=tuple(cli_args),
    )


def _build_banner(raw: Mapping[str, Any]) -> BannerConfig:
    return BannerConfig(
        title=_str(raw, "title", default="run-site is running"),
        show_db_credentials=_bool(raw, "show_db_credentials", default=True),
        suggest_dev_helpers=_bool(raw, "suggest_dev_helpers", default=True),
    )


def _build_source(raw: Mapping[str, Any]) -> SourceConfig:
    type_raw = raw.get("type")
    if type_raw is not None and type_raw not in ("git", "path"):
        raise ConfigError(f"[source].type must be 'git' or 'path', got {type_raw!r}")
    refs = [
        ("branch", raw.get("branch")),
        ("tag", raw.get("tag")),
        ("commit", raw.get("commit")),
    ]
    set_refs = [name for name, value in refs if value is not None]
    if len(set_refs) > 1:
        raise ConfigError(
            f"[source] specifies multiple refs ({set_refs}); pick one of branch / tag / commit"
        )
    return SourceConfig(
        type=type_raw,
        url=_opt_str(raw, "url"),
        branch=_opt_str(raw, "branch"),
        tag=_opt_str(raw, "tag"),
        commit=_opt_str(raw, "commit"),
        path=_opt_str(raw, "path"),
        checkout_path=_opt_str(raw, "checkout_path"),
        no_cache=_bool(raw, "no_cache", default=False),
        no_pull=_bool(raw, "no_pull", default=False),
        no_install=_bool(raw, "no_install", default=False),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_MISSING = object()


def _sanitize_default_slug(name: str) -> str:
    """Return a runsite slug derived from *name*, fitted to [A-Za-z0-9_.-]+.

    Used only when no ``project_slug`` was set in config (so the user did
    not commit to a name). Any disallowed character collapses to ``-``;
    if the result is empty (or the directory name was empty), we fall
    back to ``"runsite"`` so a no-config run still validates.
    """

    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", name).strip("-")
    return cleaned or "runsite"


def _str(raw: Mapping[str, Any], key: str, default: Any = _MISSING) -> str:
    value = raw.get(key, default)
    if value is _MISSING:
        raise ConfigError(f"Missing required string key: {key!r}")
    if not isinstance(value, str):
        raise ConfigError(f"Key {key!r} must be a string, got {type(value).__name__}")
    return value


def _opt_str(raw: Mapping[str, Any], key: str, default: str | None = None) -> str | None:
    value = raw.get(key, default)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConfigError(f"Key {key!r} must be a string or null")
    return value


def _bool(raw: Mapping[str, Any], key: str, default: bool) -> bool:
    value = raw.get(key, default)
    if not isinstance(value, bool):
        raise ConfigError(f"Key {key!r} must be a bool")
    return value
