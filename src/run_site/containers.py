"""Start/stop PostgreSQL and Redis testcontainers.

This module sits behind a thin abstraction so the rest of the CLI can stay
testable without docker. The tests substitute fakes for
:class:`PostgresLauncher` and :class:`RedisLauncher`.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from run_site.config import RunSiteConfig
from run_site.errors import DockerError

if TYPE_CHECKING:  # heavy imports only when types are checked
    from docker.models.containers import Container

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RunSiteContainers:
    """Result of :func:`start_containers`."""

    pg_host: str
    pg_port: int
    pg_container_id: str
    pg_created: bool
    redis_host: str
    redis_port: int
    redis_container_id: str
    redis_created: bool
    reuse: bool


# ---------------------------------------------------------------------------
# Launcher protocol — testable without Docker
# ---------------------------------------------------------------------------


class PostgresLauncher(Protocol):
    def start(
        self,
        *,
        image: str,
        user: str,
        password: str,
        db: str,
        env: Mapping[str, str],
        name: str | None,
        init_script: Path | None,
    ) -> tuple[str, str, int]:
        """Start (or attach to) a PG container. Returns (container_id, host, port)."""

    def find_existing(self, name: str) -> tuple[str, str, int] | None: ...

    def stop(self, container_id: str) -> None: ...

    def stream_logs_argv(self, container_id: str) -> tuple[str, ...]: ...


class RedisLauncher(Protocol):
    def start(self, *, image: str, name: str | None) -> tuple[str, str, int]: ...

    def find_existing(self, name: str) -> tuple[str, str, int] | None: ...

    def stop(self, container_id: str) -> None: ...


# ---------------------------------------------------------------------------
# Real launchers (testcontainers + docker)
# ---------------------------------------------------------------------------


class TestcontainersPostgres:
    """Real Postgres launcher using ``testcontainers``."""

    def __init__(self) -> None:
        self._containers: dict[str, Any] = {}

    def start(
        self,
        *,
        image: str,
        user: str,
        password: str,
        db: str,
        env: Mapping[str, str],
        name: str | None,
        init_script: Path | None,
    ) -> tuple[str, str, int]:
        try:
            from testcontainers.postgres import PostgresContainer
        except ImportError as exc:  # pragma: no cover - import guard
            raise DockerError(
                "testcontainers[postgres] is required to start PG containers"
            ) from exc

        container = PostgresContainer(
            image=image,
            username=user,
            password=password,
            dbname=db,
        )
        if name is not None:
            container.with_name(name)
        for key, value in env.items():
            container.with_env(key, value)
        if init_script is not None:
            container.with_volume_mapping(
                str(init_script.resolve()),
                "/docker-entrypoint-initdb.d/01-baseline.sql",
                "ro",
            )
        try:
            container.start()
        except Exception as exc:  # pragma: no cover - depends on docker
            raise DockerError(f"Failed to start PG container: {exc}") from exc

        host = container.get_container_host_ip()
        port = int(container.get_exposed_port(5432))
        wrapped = container.get_wrapped_container()
        cid = wrapped.id if wrapped is not None else ""
        if not cid:
            raise DockerError("PG container started but has no id")
        self._containers[cid] = container
        return cid, host, port

    def find_existing(self, name: str) -> tuple[str, str, int] | None:
        client = _docker_client()
        try:
            container = client.containers.get(name)
        except Exception:
            return None
        if container.status != "running":
            with suppress(Exception):
                container.start()
        host = "127.0.0.1"
        port = _published_port(container, 5432)
        return container.id, host, port

    def stop(self, container_id: str) -> None:
        wrapped = self._containers.pop(container_id, None)
        if wrapped is not None:
            with suppress(Exception):
                wrapped.stop()
            return
        client = _docker_client()
        try:
            container = client.containers.get(container_id)
        except Exception:
            return
        with suppress(Exception):
            container.stop()
            container.remove(force=True)

    def stream_logs_argv(self, container_id: str) -> tuple[str, ...]:
        return ("docker", "logs", "-f", "--tail", "0", container_id)


class TestcontainersRedis:
    """Real Redis launcher using ``testcontainers``."""

    def __init__(self) -> None:
        self._containers: dict[str, Any] = {}

    def start(self, *, image: str, name: str | None) -> tuple[str, str, int]:
        try:
            from testcontainers.redis import RedisContainer
        except ImportError as exc:  # pragma: no cover
            raise DockerError(
                "testcontainers[redis] is required to start Redis containers"
            ) from exc
        container = RedisContainer(image=image)
        if name is not None:
            container.with_name(name)
        try:
            container.start()
        except Exception as exc:  # pragma: no cover
            raise DockerError(f"Failed to start Redis container: {exc}") from exc
        host = container.get_container_host_ip()
        port = int(container.get_exposed_port(6379))
        wrapped = container.get_wrapped_container()
        cid = wrapped.id if wrapped is not None else ""
        if not cid:
            raise DockerError("Redis container started but has no id")
        self._containers[cid] = container
        return cid, host, port

    def find_existing(self, name: str) -> tuple[str, str, int] | None:
        client = _docker_client()
        try:
            container = client.containers.get(name)
        except Exception:
            return None
        if container.status != "running":
            with suppress(Exception):
                container.start()
        host = "127.0.0.1"
        port = _published_port(container, 6379)
        return container.id, host, port

    def stop(self, container_id: str) -> None:
        wrapped = self._containers.pop(container_id, None)
        if wrapped is not None:
            with suppress(Exception):
                wrapped.stop()
            return
        client = _docker_client()
        try:
            container = client.containers.get(container_id)
        except Exception:
            return
        with suppress(Exception):
            container.stop()
            container.remove(force=True)


# ---------------------------------------------------------------------------
# Top-level start/stop
# ---------------------------------------------------------------------------


def start_containers(
    *,
    config: RunSiteConfig,
    reuse: bool,
    init_script: Path | None,
    pg_launcher: PostgresLauncher | None = None,
    redis_launcher: RedisLauncher | None = None,
) -> RunSiteContainers:
    """Start PG and Redis (or attach to existing if ``reuse=True``)."""

    pg_launcher = pg_launcher or TestcontainersPostgres()
    redis_launcher = redis_launcher or TestcontainersRedis()

    _apply_ryuk_policy(config, reuse)

    pg_name = f"{config.project_slug}-runsite-pg" if reuse else None
    redis_name = f"{config.project_slug}-runsite-redis" if reuse else None

    pg_existing = pg_launcher.find_existing(pg_name) if pg_name else None
    if pg_existing is not None:
        pg_id, pg_host, pg_port = pg_existing
        pg_created = False
    else:
        pg_id, pg_host, pg_port = pg_launcher.start(
            image=config.postgres.image,
            user=config.postgres.user,
            password=config.postgres.password,
            db=config.postgres.db,
            env=config.postgres.env,
            name=pg_name,
            init_script=init_script,
        )
        pg_created = True

    redis_existing = redis_launcher.find_existing(redis_name) if redis_name else None
    if redis_existing is not None:
        redis_id, redis_host, redis_port = redis_existing
        redis_created = False
    else:
        redis_id, redis_host, redis_port = redis_launcher.start(
            image=config.redis.image,
            name=redis_name,
        )
        redis_created = True

    return RunSiteContainers(
        pg_host=pg_host,
        pg_port=pg_port,
        pg_container_id=pg_id,
        pg_created=pg_created,
        redis_host=redis_host,
        redis_port=redis_port,
        redis_container_id=redis_id,
        redis_created=redis_created,
        reuse=reuse,
    )


def stop_containers(
    containers: RunSiteContainers,
    *,
    pg_launcher: PostgresLauncher | None = None,
    redis_launcher: RedisLauncher | None = None,
    force: bool = False,
) -> None:
    """Stop both containers unless ``reuse=True``, in which case leave them."""

    if containers.reuse and not force:
        return
    pg_launcher = pg_launcher or TestcontainersPostgres()
    redis_launcher = redis_launcher or TestcontainersRedis()
    with suppress(Exception):
        pg_launcher.stop(containers.pg_container_id)
    with suppress(Exception):
        redis_launcher.stop(containers.redis_container_id)


def assert_docker_available() -> None:
    """Raise :class:`DockerError` if the daemon isn't reachable."""

    try:
        client = _docker_client()
        client.ping()
    except Exception as exc:
        raise DockerError(
            "Docker daemon is not reachable. Start Docker Desktop / colima / podman and retry."
        ) from exc


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _docker_client():  # type: ignore[no-untyped-def]
    try:
        import docker

        return docker.from_env()
    except Exception as exc:
        raise DockerError("Could not create Docker client. Is the docker daemon running?") from exc


def _published_port(container: Container, internal: int) -> int:  # type: ignore[no-any-unimported]
    ports = container.attrs.get("NetworkSettings", {}).get("Ports", {})
    bindings = ports.get(f"{internal}/tcp") or []
    if not bindings:
        raise DockerError(f"Container {container.id[:12]} has no published port for {internal}/tcp")
    return int(bindings[0]["HostPort"])


def _apply_ryuk_policy(config: RunSiteConfig, reuse: bool) -> None:
    """Set the testcontainers Ryuk env knob from ``[containers].ryuk``."""

    mode = config.containers.ryuk
    if mode == "true":
        os.environ["TESTCONTAINERS_RYUK_DISABLED"] = "false"
        return
    if mode == "false":
        os.environ["TESTCONTAINERS_RYUK_DISABLED"] = "true"
        return
    # "auto"
    os.environ["TESTCONTAINERS_RYUK_DISABLED"] = "true" if reuse else "false"
