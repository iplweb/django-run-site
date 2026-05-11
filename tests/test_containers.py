"""Container start/stop tests with mocked launchers.

Real-Docker tests that depend on a running daemon should be marked
``@pytest.mark.docker``.
"""

from __future__ import annotations

from pathlib import Path

from run_site.containers import (
    PostgresLauncher,
    RedisLauncher,
    RunSiteContainers,
    start_containers,
    stop_containers,
)


class FakePgLauncher(PostgresLauncher):
    def __init__(self, *, found: tuple[str, str, int] | None = None) -> None:
        self.started: list[dict] = []
        self.stopped: list[str] = []
        self.found = found

    def start(self, *, image, user, password, db, env, name, init_script) -> tuple[str, str, int]:
        self.started.append(
            {
                "image": image,
                "user": user,
                "password": password,
                "db": db,
                "env": dict(env),
                "name": name,
                "init_script": init_script,
            }
        )
        return ("pg-cid", "127.0.0.1", 54321)

    def find_existing(self, name: str) -> tuple[str, str, int] | None:
        return self.found

    def stop(self, container_id: str) -> None:
        self.stopped.append(container_id)

    def stream_logs_argv(self, container_id: str) -> tuple[str, ...]:
        return ("docker", "logs", "-f", container_id)


class FakeRedisLauncher(RedisLauncher):
    def __init__(self, *, found: tuple[str, str, int] | None = None) -> None:
        self.started: list[dict] = []
        self.stopped: list[str] = []
        self.found = found

    def start(self, *, image, name) -> tuple[str, str, int]:
        self.started.append({"image": image, "name": name})
        return ("redis-cid", "127.0.0.1", 49153)

    def find_existing(self, name: str) -> tuple[str, str, int] | None:
        return self.found

    def stop(self, container_id: str) -> None:
        self.stopped.append(container_id)


def test_start_containers_fresh(minimal_config) -> None:
    pg = FakePgLauncher()
    redis = FakeRedisLauncher()
    containers = start_containers(
        config=minimal_config,
        reuse=False,
        init_script=None,
        pg_launcher=pg,
        redis_launcher=redis,
    )
    assert containers.pg_created is True
    assert containers.redis_created is True
    assert containers.pg_port == 54321
    assert containers.redis_port == 49153
    assert pg.started[0]["name"] is None  # not reused → no name


def test_start_containers_reuse_uses_named(minimal_config) -> None:
    pg = FakePgLauncher()
    redis = FakeRedisLauncher()
    containers = start_containers(
        config=minimal_config,
        reuse=True,
        init_script=None,
        pg_launcher=pg,
        redis_launcher=redis,
    )
    # First reuse start with no existing — names assigned but containers created.
    assert pg.started[0]["name"] == "demo-runsite-pg"
    assert redis.started[0]["name"] == "demo-runsite-redis"
    assert containers.reuse is True


def test_start_containers_reuse_attaches_to_existing(minimal_config) -> None:
    pg = FakePgLauncher(found=("existing-pg", "127.0.0.1", 11111))
    redis = FakeRedisLauncher(found=("existing-redis", "127.0.0.1", 22222))
    containers = start_containers(
        config=minimal_config,
        reuse=True,
        init_script=None,
        pg_launcher=pg,
        redis_launcher=redis,
    )
    assert containers.pg_container_id == "existing-pg"
    assert containers.pg_created is False
    assert containers.redis_container_id == "existing-redis"
    assert containers.redis_created is False
    assert pg.started == []  # not started, attached
    assert redis.started == []


def test_init_script_passed_through(minimal_config, tmp_path: Path) -> None:
    pg = FakePgLauncher()
    redis = FakeRedisLauncher()
    init = tmp_path / "baseline.sql"
    init.touch()
    start_containers(
        config=minimal_config,
        reuse=False,
        init_script=init,
        pg_launcher=pg,
        redis_launcher=redis,
    )
    assert pg.started[0]["init_script"] == init


def test_postgres_env_passed(minimal_config, tmp_path: Path) -> None:
    from dataclasses import replace

    config = replace(
        minimal_config,
        postgres=replace(
            minimal_config.postgres,
            env={"POSTGRESQL_UNSAFE_BUT_FAST": "1"},
        ),
    )
    pg = FakePgLauncher()
    redis = FakeRedisLauncher()
    start_containers(
        config=config,
        reuse=False,
        init_script=None,
        pg_launcher=pg,
        redis_launcher=redis,
    )
    assert pg.started[0]["env"]["POSTGRESQL_UNSAFE_BUT_FAST"] == "1"


def test_stop_containers_skipped_when_reuse(minimal_config) -> None:
    pg = FakePgLauncher()
    redis = FakeRedisLauncher()
    containers = RunSiteContainers(
        pg_host="127.0.0.1",
        pg_port=1,
        pg_container_id="pg",
        pg_created=True,
        redis_host="127.0.0.1",
        redis_port=2,
        redis_container_id="redis",
        redis_created=True,
        reuse=True,
    )
    stop_containers(containers, pg_launcher=pg, redis_launcher=redis)
    assert pg.stopped == []
    assert redis.stopped == []


def test_redis_failure_rolls_back_pg(minimal_config) -> None:
    """If Redis startup raises after PG started, PG must be stopped so
    we don't leak a half-started stack."""

    pg = FakePgLauncher()

    class FailingRedis(RedisLauncher):
        def start(self, *, image, name) -> tuple[str, str, int]:
            raise RuntimeError("simulated redis boom")

        def find_existing(self, name: str) -> tuple[str, str, int] | None:
            return None

        def stop(self, container_id: str) -> None:
            pass

    import pytest

    with pytest.raises(RuntimeError, match="simulated redis boom"):
        start_containers(
            config=minimal_config,
            reuse=False,
            init_script=None,
            pg_launcher=pg,
            redis_launcher=FailingRedis(),
        )
    # PG was stopped during rollback even though start_containers raised.
    assert pg.stopped == ["pg-cid"]


def test_redis_failure_does_not_stop_pg_when_attached(minimal_config) -> None:
    """If we *attached* to an existing PG (reuse), don't tear it down
    when Redis fails — it wasn't ours to stop."""

    pg = FakePgLauncher(found=("attached-pg", "127.0.0.1", 11111))

    class FailingRedis(RedisLauncher):
        def start(self, *, image, name) -> tuple[str, str, int]:
            raise RuntimeError("nope")

        def find_existing(self, name: str) -> tuple[str, str, int] | None:
            return None

        def stop(self, container_id: str) -> None:
            pass

    import pytest

    with pytest.raises(RuntimeError):
        start_containers(
            config=minimal_config,
            reuse=True,
            init_script=None,
            pg_launcher=pg,
            redis_launcher=FailingRedis(),
        )
    # We attached, never created → must not stop.
    assert pg.stopped == []


def test_stop_containers_runs_stops_when_not_reuse() -> None:
    pg = FakePgLauncher()
    redis = FakeRedisLauncher()
    containers = RunSiteContainers(
        pg_host="127.0.0.1",
        pg_port=1,
        pg_container_id="pg",
        pg_created=True,
        redis_host="127.0.0.1",
        redis_port=2,
        redis_container_id="redis",
        redis_created=True,
        reuse=False,
    )
    stop_containers(containers, pg_launcher=pg, redis_launcher=redis)
    assert pg.stopped == ["pg"]
    assert redis.stopped == ["redis"]


def test_start_containers_skips_postgres_when_disabled(minimal_config) -> None:
    """``[postgres].enabled = false`` means: do not pull, do not start.
    The result must carry ``None`` for all pg_* fields so downstream
    consumers know to skip emitting DB env vars / sidecar sections.
    """

    from dataclasses import replace

    pg = FakePgLauncher()
    redis = FakeRedisLauncher()
    config = replace(minimal_config, postgres=replace(minimal_config.postgres, enabled=False))
    containers = start_containers(
        config=config,
        reuse=False,
        init_script=None,
        pg_launcher=pg,
        redis_launcher=redis,
    )
    assert pg.started == []
    assert containers.pg_host is None
    assert containers.pg_port is None
    assert containers.pg_container_id is None
    assert containers.pg_created is None
    # Redis still started — disables are independent.
    assert containers.redis_port == 49153


def test_start_containers_skips_redis_when_disabled(minimal_config) -> None:
    from dataclasses import replace

    pg = FakePgLauncher()
    redis = FakeRedisLauncher()
    config = replace(minimal_config, redis=replace(minimal_config.redis, enabled=False))
    containers = start_containers(
        config=config,
        reuse=False,
        init_script=None,
        pg_launcher=pg,
        redis_launcher=redis,
    )
    assert redis.started == []
    assert containers.redis_host is None
    assert containers.redis_port is None
    assert containers.pg_port == 54321


def test_start_containers_skips_both(minimal_config) -> None:
    """SQLite-only / cache-less mode: neither service starts."""

    from dataclasses import replace

    pg = FakePgLauncher()
    redis = FakeRedisLauncher()
    config = replace(
        minimal_config,
        postgres=replace(minimal_config.postgres, enabled=False),
        redis=replace(minimal_config.redis, enabled=False),
    )
    containers = start_containers(
        config=config,
        reuse=False,
        init_script=None,
        pg_launcher=pg,
        redis_launcher=redis,
    )
    assert pg.started == []
    assert redis.started == []
    assert containers.pg_container_id is None
    assert containers.redis_container_id is None


def test_stop_containers_noop_when_ids_are_none() -> None:
    """When the run started with both services disabled, stop_containers
    is called with all-``None`` ids; it must not call into the launchers."""

    pg = FakePgLauncher()
    redis = FakeRedisLauncher()
    containers = RunSiteContainers(
        pg_host=None,
        pg_port=None,
        pg_container_id=None,
        pg_created=None,
        redis_host=None,
        redis_port=None,
        redis_container_id=None,
        redis_created=None,
        reuse=False,
    )
    stop_containers(containers, pg_launcher=pg, redis_launcher=redis)
    assert pg.stopped == []
    assert redis.stopped == []
