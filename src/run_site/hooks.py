"""Hook execution — host commands and Django callables.

Two hook flavors:

- ``type = "command"``: argv runs as a regular subprocess on the host.
- ``type = "django"``: argv is built around ``manage.py shell -c`` with a
  small bootstrap that imports the configured callable and passes a JSON
  context built by :func:`build_hook_context`.

Hook errors are fatal in pre-* / post-* stages except ``post_stop`` which
is best-effort.
"""

from __future__ import annotations

import json
import logging
import subprocess
import tempfile
import textwrap
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from run_site.config import HookConfig, RunSiteConfig
from run_site.errors import HookError
from run_site.processes import run_oneshot

logger = logging.getLogger(__name__)


DJANGO_BOOTSTRAP = textwrap.dedent(
    """
    import json, os
    from importlib import import_module
    ctx_path = os.environ['DJANGO_RUN_SITE_CONTEXT']
    with open(ctx_path) as fh:
        ctx = json.load(fh)
    callable_spec = os.environ['DJANGO_RUN_SITE_CALLABLE']
    module_path, func_name = callable_spec.rsplit(':', 1)
    func = getattr(import_module(module_path), func_name)
    func(ctx)
    """
).strip()


@dataclass(frozen=True)
class HookContext:
    """Inputs available to every hook callable."""

    project_root: Path
    manage_py: Path
    runserver_url: str | None
    runserver_port: int | None
    pg_host: str | None
    pg_port: int | None
    redis_host: str | None
    redis_port: int | None
    dump_path: Path | None
    reuse: bool
    pg_created: bool | None
    redis_created: bool | None
    superuser: dict[str, Any] | None
    opts: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_root": str(self.project_root),
            "manage_py": str(self.manage_py),
            "runserver_url": self.runserver_url,
            "runserver_port": self.runserver_port,
            "pg_host": self.pg_host,
            "pg_port": self.pg_port,
            "redis_host": self.redis_host,
            "redis_port": self.redis_port,
            "dump_path": str(self.dump_path) if self.dump_path is not None else None,
            "reuse": self.reuse,
            "pg_created": self.pg_created,
            "redis_created": self.redis_created,
            "superuser": self.superuser,
            "opts": self.opts,
        }


def build_hook_context(
    *,
    config: RunSiteConfig,
    manage_py: Path,
    runserver_port: int | None,
    pg_host: str | None,
    pg_port: int | None,
    redis_host: str | None,
    redis_port: int | None,
    dump_path: Path | None,
    reuse: bool,
    pg_created: bool | None,
    redis_created: bool | None,
    superuser: dict[str, Any] | None,
    opts: dict[str, Any],
) -> HookContext:
    runserver_url: str | None = None
    if runserver_port is not None:
        runserver_url = f"http://{config.django.runserver_display_host}:{runserver_port}"
    return HookContext(
        project_root=config.project_root,
        manage_py=manage_py,
        runserver_url=runserver_url,
        runserver_port=runserver_port,
        pg_host=pg_host,
        pg_port=pg_port,
        redis_host=redis_host,
        redis_port=redis_port,
        dump_path=dump_path,
        reuse=reuse,
        pg_created=pg_created,
        redis_created=redis_created,
        superuser=superuser,
        opts=opts,
    )


def run_hooks(
    *,
    stage: str,
    hooks: tuple[HookConfig, ...],
    context: HookContext,
    python: tuple[str, ...] | None,
    manage_py: Path | None,
    env: Mapping[str, str],
    disabled_flags: set[str],
    best_effort: bool = False,
) -> None:
    """Run all hooks for *stage* in declaration order.

    *disabled_flags* is the set of ``cli_disable_flag`` strings the user
    passed; hooks whose ``cli_disable_flag`` is in the set are skipped.
    """

    for hook in hooks:
        if hook.stage != stage:
            continue
        if hook.cli_disable_flag is not None and hook.cli_disable_flag in disabled_flags:
            logger.info(
                "Skipping %s hook %s due to %s",
                stage,
                hook.callable or " ".join(hook.command or ()),
                hook.cli_disable_flag,
            )
            continue
        try:
            _run_one(
                hook=hook,
                context=context,
                python=python,
                manage_py=manage_py,
                env=env,
            )
        except HookError:
            if best_effort:
                logger.exception("Hook in best-effort stage %s failed; continuing", stage)
                continue
            raise
        except Exception as exc:  # pragma: no cover - defensive
            if best_effort:
                logger.exception(
                    "Unexpected hook failure in best-effort stage %s; continuing",
                    stage,
                )
                continue
            raise HookError(
                f"Hook {hook.callable or hook.command!r} in stage {stage} "
                f"raised an unexpected error: {exc}"
            ) from exc


def _run_one(
    *,
    hook: HookConfig,
    context: HookContext,
    python: tuple[str, ...] | None,
    manage_py: Path | None,
    env: Mapping[str, str],
) -> None:
    if hook.type == "command":
        if hook.command is None:
            raise HookError("hook of type=command must have a command")
        try:
            result = run_oneshot(
                hook.command,
                cwd=context.project_root,
                env=env,
                timeout=hook.timeout,
                check=False,
                capture_output=True,
            )
        except subprocess.TimeoutExpired as exc:
            raise HookError(
                f"Command hook timed out after {hook.timeout}s: {' '.join(hook.command)}"
            ) from exc
        except FileNotFoundError as exc:
            raise HookError(
                f"Command hook failed: executable not found: "
                f"{hook.command[0]!r} (full argv: {' '.join(hook.command)})"
            ) from exc
        if not result.ok:
            raise HookError(
                f"Command hook failed (exit {result.returncode}): "
                f"{' '.join(hook.command)}\n"
                f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
            )
        return

    # type == "django"
    if python is None or manage_py is None:
        raise HookError("Django hook requires resolved python + manage.py — fixture/test bug?")
    if hook.callable is None:
        raise HookError("hook of type=django must have a callable")

    with tempfile.NamedTemporaryFile(
        prefix="run-site-ctx-", suffix=".json", mode="w", delete=False
    ) as ctx_file:
        json.dump(context.to_dict(), ctx_file)
        ctx_path = ctx_file.name

    try:
        sub_env = dict(env)
        sub_env["DJANGO_RUN_SITE_CONTEXT"] = ctx_path
        sub_env["DJANGO_RUN_SITE_CALLABLE"] = hook.callable
        argv = (*python, str(manage_py), "shell", "-c", DJANGO_BOOTSTRAP)
        try:
            result = run_oneshot(
                argv,
                env=sub_env,
                timeout=hook.timeout,
                check=False,
                capture_output=True,
            )
        except subprocess.TimeoutExpired as exc:
            raise HookError(
                f"Django hook {hook.callable!r} timed out after {hook.timeout}s"
            ) from exc
        if not result.ok:
            raise HookError(
                f"Django hook {hook.callable!r} failed (exit {result.returncode}):\n"
                f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
            )
    finally:
        Path(ctx_path).unlink(missing_ok=True)
