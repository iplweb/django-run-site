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


def test_prepared_init_script_feeds_filtered_copy_to_launcher(
    minimal_config, tmp_path: Path
) -> None:
    """With fix_search_path on, the path handed to the PG launcher is a
    filtered temp copy whose search_path line is rewritten — and it still
    exists at launch time (cleanup happens only after start)."""
    from dataclasses import replace

    from run_site.dumps import prepared_init_script

    src = tmp_path / "baseline.sql"
    src.write_text("SELECT pg_catalog.set_config('search_path', '', false);\n")
    config = replace(minimal_config, dump=replace(minimal_config.dump, fix_search_path=True))

    pg = FakePgLauncher()
    redis = FakeRedisLauncher()
    with prepared_init_script(src, fix_search_path=config.dump.fix_search_path) as init_script:
        start_containers(
            config=config,
            reuse=False,
            init_script=init_script,
            pg_launcher=pg,
            redis_launcher=redis,
        )
        mounted = pg.started[0]["init_script"]
        assert mounted is not None and mounted != src
        assert mounted.exists()  # present while the container is being created
        assert "set_config('search_path', 'public', false)" in mounted.read_text()
    assert not mounted.exists()  # removed after the with-block


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


def test_start_containers_emits_progress(minimal_config) -> None:
    """The CLI should be able to surface container lifecycle messages by
    passing ``progress=mux.write``. Verify start/ready pairs are emitted
    for both services with hostname/port in the ready line so the user
    sees the same endpoint info the banner will later show."""

    pg = FakePgLauncher()
    redis = FakeRedisLauncher()
    events: list[tuple[str, str, str]] = []
    start_containers(
        config=minimal_config,
        reuse=False,
        init_script=None,
        pg_launcher=pg,
        redis_launcher=redis,
        progress=lambda name, color, line: events.append((name, color, line)),
    )
    lines = [line for _, _, line in events]
    assert any("postgres" in line and "starting" in line for line in lines), lines
    assert any("postgres" in line and "ready" in line and "54321" in line for line in lines), lines
    assert any("redis" in line and "starting" in line for line in lines), lines
    assert any("redis" in line and "ready" in line and "49153" in line for line in lines), lines


def test_start_containers_progress_includes_image(minimal_config) -> None:
    pg = FakePgLauncher()
    redis = FakeRedisLauncher()
    events: list[tuple[str, str, str]] = []
    start_containers(
        config=minimal_config,
        reuse=False,
        init_script=None,
        pg_launcher=pg,
        redis_launcher=redis,
        progress=lambda name, color, line: events.append((name, color, line)),
    )
    lines = [line for _, _, line in events]
    assert any(minimal_config.postgres.image in line for line in lines), lines
    assert any(minimal_config.redis.image in line for line in lines), lines


def test_start_containers_emits_progress_when_reusing(minimal_config) -> None:
    pg = FakePgLauncher(found=("existing-pg-cid-abcdefghij", "127.0.0.1", 11111))
    redis = FakeRedisLauncher(found=("existing-redis-cid-zyxwvutsr", "127.0.0.1", 22222))
    events: list[tuple[str, str, str]] = []
    start_containers(
        config=minimal_config,
        reuse=True,
        init_script=None,
        pg_launcher=pg,
        redis_launcher=redis,
        progress=lambda name, color, line: events.append((name, color, line)),
    )
    lines = [line for _, _, line in events]
    assert any("postgres" in line and "reusing" in line for line in lines), lines
    assert any("redis" in line and "reusing" in line for line in lines), lines
    # No "starting" line for either — we attached, didn't start.
    assert not any("postgres" in line and "starting" in line for line in lines), lines
    assert not any("redis" in line and "starting" in line for line in lines), lines


def test_start_containers_progress_skipped_for_disabled_service(minimal_config) -> None:
    """``[redis].enabled = false`` means no redis progress messages.
    Only services that actually start should appear in the progress stream."""

    from dataclasses import replace

    pg = FakePgLauncher()
    redis = FakeRedisLauncher()
    config = replace(minimal_config, redis=replace(minimal_config.redis, enabled=False))
    events: list[tuple[str, str, str]] = []
    start_containers(
        config=config,
        reuse=False,
        init_script=None,
        pg_launcher=pg,
        redis_launcher=redis,
        progress=lambda name, color, line: events.append((name, color, line)),
    )
    lines = [line for _, _, line in events]
    assert any("postgres" in line for line in lines), lines
    assert not any("redis" in line for line in lines), lines


def test_start_containers_works_without_progress(minimal_config) -> None:
    """Backward compatibility: omitting ``progress`` must not error."""

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


# ---------------------------------------------------------------------------
# Docker client resolution: honor the active `docker context`.
#
# docker.from_env() honors DOCKER_HOST but — unlike the docker CLI — ignores
# the active `docker context`. On OrbStack / colima / Docker Desktop the daemon
# lives on a non-default socket and /var/run/docker.sock may be absent or a
# dangling symlink, so the daemon looks unreachable even though `docker ps`
# works. These tests pin the CLI-faithful precedence: DOCKER_HOST first, then
# the active context, then from_env() as a fallback.
# ---------------------------------------------------------------------------


def test_docker_client_uses_active_context_when_no_docker_host(monkeypatch) -> None:
    import docker
    from docker.context import ContextAPI

    from run_site import containers as containers_mod

    monkeypatch.delenv("DOCKER_HOST", raising=False)

    captured: dict[str, object] = {}

    def fake_docker_client(base_url=None, **kwargs):
        captured["base_url"] = base_url
        return "ctx-client"

    def fake_from_env(*args, **kwargs):
        captured["from_env"] = True
        return "from-env-client"

    class FakeCtx:
        Host = "unix:///Users/me/.orbstack/run/docker.sock"
        TLSConfig = None

    monkeypatch.setattr(docker, "DockerClient", fake_docker_client)
    monkeypatch.setattr(docker, "from_env", fake_from_env)
    monkeypatch.setattr(ContextAPI, "get_current_context", classmethod(lambda cls: FakeCtx()))

    assert containers_mod._docker_client() == "ctx-client"
    assert captured["base_url"] == "unix:///Users/me/.orbstack/run/docker.sock"
    assert "from_env" not in captured


def test_docker_client_prefers_docker_host_env(monkeypatch) -> None:
    import docker

    from run_site import containers as containers_mod

    monkeypatch.setenv("DOCKER_HOST", "unix:///tmp/explicit.sock")

    monkeypatch.setattr(docker, "from_env", lambda *a, **k: "from-env-client")

    def fail_docker_client(**kwargs):
        raise AssertionError("must not resolve context when DOCKER_HOST is set")

    monkeypatch.setattr(docker, "DockerClient", fail_docker_client)

    assert containers_mod._docker_client() == "from-env-client"


def test_docker_client_falls_back_to_from_env_without_context(monkeypatch) -> None:
    import docker
    from docker.context import ContextAPI

    from run_site import containers as containers_mod

    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.setattr(docker, "from_env", lambda *a, **k: "from-env-client")
    monkeypatch.setattr(ContextAPI, "get_current_context", classmethod(lambda cls: None))

    assert containers_mod._docker_client() == "from-env-client"


# ---------------------------------------------------------------------------
# DOCKER_HOST export: make testcontainers honor the active `docker context`.
#
# testcontainers builds its OWN docker client via docker.from_env(), which —
# like docker.from_env() everywhere — ignores the active `docker context`. It
# only consults DOCKER_HOST / ~/.testcontainers.properties. So even after our
# own client honors the context, PostgresContainer(...) construction still
# crashes with FileNotFoundError on OrbStack / colima / Docker-Desktop-stopped.
# We bridge the gap by exporting DOCKER_HOST from the active context before any
# testcontainers client is built. This also routes Ryuk's socket bind-mount
# (testcontainers' get_docker_socket → from_env) to the same endpoint.
# ---------------------------------------------------------------------------


def test_ensure_docker_host_env_exports_active_context_host(monkeypatch) -> None:
    from run_site import containers as containers_mod

    monkeypatch.delenv("DOCKER_HOST", raising=False)

    class FakeCtx:
        Host = "unix:///Users/me/.orbstack/run/docker.sock"

    monkeypatch.setattr(containers_mod, "_active_docker_context", lambda: FakeCtx())

    containers_mod._ensure_docker_host_env()

    import os

    assert os.environ["DOCKER_HOST"] == "unix:///Users/me/.orbstack/run/docker.sock"


def test_ensure_docker_host_env_respects_existing_docker_host(monkeypatch) -> None:
    from run_site import containers as containers_mod

    monkeypatch.setenv("DOCKER_HOST", "unix:///tmp/explicit.sock")

    def fail_context():
        raise AssertionError("must not resolve context when DOCKER_HOST is set")

    monkeypatch.setattr(containers_mod, "_active_docker_context", fail_context)

    containers_mod._ensure_docker_host_env()

    import os

    assert os.environ["DOCKER_HOST"] == "unix:///tmp/explicit.sock"


def test_ensure_docker_host_env_noop_without_context(monkeypatch) -> None:
    from run_site import containers as containers_mod

    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.setattr(containers_mod, "_active_docker_context", lambda: None)

    containers_mod._ensure_docker_host_env()

    import os

    assert "DOCKER_HOST" not in os.environ


def test_start_containers_exports_docker_host_before_launching(minimal_config, monkeypatch) -> None:
    """The DOCKER_HOST export must happen BEFORE launchers run, since
    testcontainers builds its client during PostgresContainer construction."""
    import os

    from run_site import containers as containers_mod

    monkeypatch.delenv("DOCKER_HOST", raising=False)

    class FakeCtx:
        Host = "unix:///Users/me/.orbstack/run/docker.sock"

    monkeypatch.setattr(containers_mod, "_active_docker_context", lambda: FakeCtx())

    seen: dict[str, object] = {}

    class RecordingPgLauncher(FakePgLauncher):
        def start(self, **kwargs):
            seen["docker_host_at_start"] = os.environ.get("DOCKER_HOST")
            return super().start(**kwargs)

    pg = RecordingPgLauncher()
    redis = FakeRedisLauncher()
    start_containers(
        config=minimal_config,
        reuse=False,
        init_script=None,
        pg_launcher=pg,
        redis_launcher=redis,
    )

    assert seen["docker_host_at_start"] == "unix:///Users/me/.orbstack/run/docker.sock"
