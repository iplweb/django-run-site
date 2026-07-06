# Volume Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Anonymous Docker volumes created by the PG/Redis test containers are removed by default when run-site stops the stack, controlled by `[containers].remove_volumes` (default `true`).

**Architecture:** A config knob flows from `config.py` through `stop_containers` / the launcher `stop()` protocol down to the docker SDK call, where the actual bug lives: the fallback stop path calls `container.remove(force=True)` without `v=True`, orphaning anonymous volumes on every graceful shutdown. Spec: `docs/superpowers/specs/2026-07-06-volume-cleanup-design.md`.

**Tech Stack:** Python 3.11+, uv, pytest (strict markers, warnings-as-errors), ruff (100 cols, double quotes), mypy, docker SDK 7.1.0, testcontainers 4.14.2.

## Global Constraints

- Toolchain: `uv run pytest …`, `uv run ruff check .`, `uv run ruff format .`, `uv run mypy src/run_site`.
- Unit tests must not require Docker (no `docker` marker needed for any test in this plan — everything is faked/monkeypatched).
- No silent exception swallowing: every new `except` block logs (`logger.warning(..., exc_info=True)` / `logger.debug(..., exc_info=True)`), re-raises, or has a narrow type plus a comment saying why suppression is correct.
- Commit style: short imperative subject (no `feat:` prefix — repo uses "Add …", "Fix …"). Every commit message ends with the trailer:
  `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`
- Line length 100, double quotes (ruff enforces).
- The CLI must stay Django-import-free (not touched by this plan, just don't add imports).

---

### Task 1: Config knob `[containers].remove_volumes`

**Files:**
- Modify: `src/run_site/config.py` (dataclass ~line 98-100, parser ~line 573-578)
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: existing `ContainersConfig`, `_build_containers`, `ConfigError`.
- Produces: `ContainersConfig.remove_volumes: bool` (default `True`) — Tasks 2 and 4 read `config.containers.remove_volumes`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_config.py` (file already imports `Path`, `pytest`, `load_config`, `ConfigError` at the top — no new imports needed):

```python
def test_containers_remove_volumes_defaults_true(tmp_path: Path) -> None:
    cfg = tmp_path / "runsite.toml"
    cfg.write_text('project_slug = "x"\n')
    config = load_config(config_path=cfg, project_root=tmp_path)
    assert config.containers.remove_volumes is True


def test_containers_remove_volumes_explicit_false(tmp_path: Path) -> None:
    cfg = tmp_path / "runsite.toml"
    cfg.write_text('project_slug = "x"\n[containers]\nremove_volumes = false\n')
    config = load_config(config_path=cfg, project_root=tmp_path)
    assert config.containers.remove_volumes is False


def test_containers_remove_volumes_rejects_non_boolean(tmp_path: Path) -> None:
    cfg = tmp_path / "runsite.toml"
    cfg.write_text('project_slug = "x"\n[containers]\nremove_volumes = "yes"\n')
    with pytest.raises(ConfigError, match=r"\[containers\]\.remove_volumes must be a boolean"):
        load_config(config_path=cfg, project_root=tmp_path)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_config.py -v -k remove_volumes`
Expected: 3 failed — `AttributeError: 'ContainersConfig' object has no attribute 'remove_volumes'` (first two) and `Failed: DID NOT RAISE` (third).

- [ ] **Step 3: Implement the knob**

In `src/run_site/config.py`, change the `ContainersConfig` dataclass (currently ~line 98):

```python
@dataclass(frozen=True)
class ContainersConfig:
    ryuk: RyukMode = "auto"
    remove_volumes: bool = True
```

Change `_build_containers` (currently ~line 573):

```python
def _build_containers(raw: Mapping[str, Any]) -> ContainersConfig:
    ryuk_raw = raw.get("ryuk", "auto")
    if ryuk_raw not in ("auto", True, False):
        raise ConfigError("[containers].ryuk must be 'auto', true, or false")
    ryuk: RyukMode = "auto" if ryuk_raw == "auto" else ("true" if ryuk_raw is True else "false")
    remove_volumes = raw.get("remove_volumes", True)
    if not isinstance(remove_volumes, bool):
        raise ConfigError("[containers].remove_volumes must be a boolean")
    return ContainersConfig(ryuk=ryuk, remove_volumes=remove_volumes)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_config.py -v -k remove_volumes`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add src/run_site/config.py tests/test_config.py
git commit -m "Add [containers].remove_volumes config knob

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: Thread `remove_volumes` through the container layer

**Files:**
- Modify: `src/run_site/containers.py` (protocols ~lines 63-90, rollback ~lines 349-356, `stop_containers` ~lines 371-393)
- Test: `tests/test_containers.py` (fakes at lines 20-64, inline `FailingRedis` fakes at ~211 and ~241, rollback test at ~205)

**Interfaces:**
- Consumes: `ContainersConfig.remove_volumes` from Task 1 (via the `config` param `start_containers` already receives).
- Produces:
  - Protocol methods `PostgresLauncher.stop(self, container_id: str, *, remove_volumes: bool = True) -> None` and `RedisLauncher.stop(self, container_id: str, *, remove_volumes: bool = True) -> None` — Task 3 implements them in the real launchers.
  - `stop_containers(containers, *, pg_launcher=None, redis_launcher=None, force=False, remove_volumes: bool = True)` — Task 4 passes the knob here.
  - Test fakes gain `stop_remove_volumes: list[bool]` recording each call's kwarg.

- [ ] **Step 1: Update the fakes and write the failing tests**

In `tests/test_containers.py`, update `FakePgLauncher` (line 20) — add the recording list in `__init__` and change `stop`:

```python
class FakePgLauncher(PostgresLauncher):
    def __init__(self, *, found: tuple[str, str, int] | None = None) -> None:
        self.started: list[dict] = []
        self.stopped: list[str] = []
        self.stop_remove_volumes: list[bool] = []
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

    def stop(self, container_id: str, *, remove_volumes: bool = True) -> None:
        self.stopped.append(container_id)
        self.stop_remove_volumes.append(remove_volumes)

    def stream_logs_argv(self, container_id: str) -> tuple[str, ...]:
        return ("docker", "logs", "-f", container_id)
```

Same change to `FakeRedisLauncher` (line 50): add `self.stop_remove_volumes: list[bool] = []` to `__init__`, and:

```python
    def stop(self, container_id: str, *, remove_volumes: bool = True) -> None:
        self.stopped.append(container_id)
        self.stop_remove_volumes.append(remove_volumes)
```

Update both inline `FailingRedis` classes (inside `test_redis_failure_rolls_back_pg` ~line 211 and `test_redis_failure_does_not_stop_pg_when_attached` ~line 241) to the new signature:

```python
        def stop(self, container_id: str, *, remove_volumes: bool = True) -> None:
            pass
```

Extend the existing rollback assertion at the end of `test_redis_failure_rolls_back_pg` (~line 232):

```python
    # PG was stopped during rollback even though start_containers raised.
    assert pg.stopped == ["pg-cid"]
    assert pg.stop_remove_volumes == [True]
```

Append three new tests at the end of the `stop_containers` test group (after `test_stop_containers_runs_stops_when_not_reuse`, ~line 282):

```python
def test_stop_containers_removes_volumes_by_default() -> None:
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
    assert pg.stop_remove_volumes == [True]
    assert redis.stop_remove_volumes == [True]


def test_stop_containers_forwards_remove_volumes_false() -> None:
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
    stop_containers(containers, pg_launcher=pg, redis_launcher=redis, remove_volumes=False)
    assert pg.stopped == ["pg"]
    assert redis.stopped == ["redis"]
    assert pg.stop_remove_volumes == [False]
    assert redis.stop_remove_volumes == [False]


def test_rollback_honors_remove_volumes_knob(minimal_config) -> None:
    """When Redis fails after PG started and the knob is off, the PG
    rollback must not delete volumes either."""

    from dataclasses import replace

    import pytest

    config = replace(
        minimal_config,
        containers=replace(minimal_config.containers, remove_volumes=False),
    )
    pg = FakePgLauncher()

    class FailingRedis(RedisLauncher):
        def start(self, *, image, name) -> tuple[str, str, int]:
            raise RuntimeError("simulated redis boom")

        def find_existing(self, name: str) -> tuple[str, str, int] | None:
            return None

        def stop(self, container_id: str, *, remove_volumes: bool = True) -> None:
            pass

    with pytest.raises(RuntimeError, match="simulated redis boom"):
        start_containers(
            config=config,
            reuse=False,
            init_script=None,
            pg_launcher=pg,
            redis_launcher=FailingRedis(),
        )
    assert pg.stopped == ["pg-cid"]
    assert pg.stop_remove_volumes == [False]
```

- [ ] **Step 2: Run tests to verify the new ones fail**

Run: `uv run pytest tests/test_containers.py -v`
Expected: exactly two of the new tests FAIL —
- `test_stop_containers_forwards_remove_volumes_false` with `TypeError: stop_containers() got an unexpected keyword argument 'remove_volumes'`,
- `test_rollback_honors_remove_volumes_knob` with `assert [True] == [False]` (production code doesn't pass the kwarg yet, so the fake records its own default `True`).

`test_stop_containers_removes_volumes_by_default` and the extended assertion in `test_redis_failure_rolls_back_pg` PASS already (the fake's default is `True`) — they are regression guards for Step 3, not TDD discriminators. All previously existing tests must still PASS.

- [ ] **Step 3: Implement the threading in `src/run_site/containers.py`**

Protocol methods (lines 79 and 89) — change both:

```python
    def stop(self, container_id: str, *, remove_volumes: bool = True) -> None: ...
```

(For `PostgresLauncher` the current line is `def stop(self, container_id: str) -> None:` with a docstring-free body `...`; keep the surrounding methods untouched.)

`stop_containers` (line 371) — new signature and forwarding:

```python
def stop_containers(
    containers: RunSiteContainers,
    *,
    pg_launcher: PostgresLauncher | None = None,
    redis_launcher: RedisLauncher | None = None,
    force: bool = False,
    remove_volumes: bool = True,
) -> None:
    """Stop both containers unless ``reuse=True``, in which case leave them.

    A ``None`` container id means the service was disabled and never
    started — nothing to stop. ``remove_volumes`` removes the anonymous
    volumes together with each container; ``force=True`` (the reuse
    override) honors it the same way.
    """

    if containers.reuse and not force:
        return
    pg_launcher = pg_launcher or TestcontainersPostgres()
    redis_launcher = redis_launcher or TestcontainersRedis()
    if containers.pg_container_id is not None:
        with suppress(Exception):
            pg_launcher.stop(containers.pg_container_id, remove_volumes=remove_volumes)
    if containers.redis_container_id is not None:
        with suppress(Exception):
            redis_launcher.stop(containers.redis_container_id, remove_volumes=remove_volumes)
```

Rollback inside `start_containers` (lines 349-356) — pass the knob (the `config` param is in scope):

```python
    except BaseException:
        # Roll back PG so we don't leak a half-started stack. Only stop
        # what we created — never tear down a container the caller asked
        # us to attach to via reuse.
        if pg_created and pg_id is not None:
            with suppress(Exception):
                pg_launcher.stop(pg_id, remove_volumes=config.containers.remove_volumes)
        raise
```

Note: this step will make `TestcontainersPostgres.stop` / `TestcontainersRedis.stop` no longer match the protocol — that is Task 3. To keep this task green on its own, ALSO update the two real launchers' `stop` signatures now, minimally (behavior change comes in Task 3):

```python
    def stop(self, container_id: str, *, remove_volumes: bool = True) -> None:
```

(only the `def` line in both classes; bodies unchanged in this task).

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_containers.py -v`
Expected: all PASS (including the pre-existing ones).

Run: `uv run mypy src/run_site`
Expected: no errors (protocol and implementations agree).

- [ ] **Step 5: Commit**

```bash
git add src/run_site/containers.py tests/test_containers.py
git commit -m "Thread remove_volumes through container stop paths

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: Fix the leak in the real launchers' fallback stop path

**Files:**
- Modify: `src/run_site/containers.py` (`TestcontainersPostgres.stop` ~line 164, `TestcontainersRedis.stop` ~line 225; new module-level helper next to `_docker_client`)
- Test: `tests/test_containers.py`

**Interfaces:**
- Consumes: protocol signature from Task 2; module-level `_docker_client()` (tests monkeypatch it).
- Produces: `_stop_container_by_id(container_id: str, *, remove_volumes: bool) -> None` (module-private helper, used by both launchers); fallback removal now calls `container.remove(force=True, v=remove_volumes)`.

- [ ] **Step 1: Write the failing tests**

In `tests/test_containers.py`:

Add `import logging` and `import pytest` to the module-level imports at the top of the file, and extend the `run_site.containers` import to include the real launchers:

```python
from run_site.containers import (
    PostgresLauncher,
    RedisLauncher,
    RunSiteContainers,
    TestcontainersPostgres,
    TestcontainersRedis,
    start_containers,
    stop_containers,
)
```

Append at the end of the file:

```python
# ---------------------------------------------------------------------------
# Real launchers, fallback stop path (no Docker daemon involved).
#
# The CLI calls stop_containers() without launcher instances, so the stop
# always goes through the by-id fallback, not the wrapped testcontainers
# object. Before the fix that path called remove(force=True) without v=True
# and orphaned the anonymous volumes (postgres:/var/lib/postgresql/data,
# redis:/data) on every graceful shutdown.
# ---------------------------------------------------------------------------


class _FakeFallbackContainer:
    """Mimics the docker SDK Container for the by-id fallback path."""

    def __init__(self, *, stop_raises: bool = False) -> None:
        self.stop_calls = 0
        self.remove_kwargs: list[dict] = []
        self._stop_raises = stop_raises

    def stop(self) -> None:
        self.stop_calls += 1
        if self._stop_raises:
            raise RuntimeError("simulated daemon hiccup")

    def remove(self, **kwargs) -> None:
        self.remove_kwargs.append(kwargs)


def _patch_docker_client(monkeypatch, container: _FakeFallbackContainer) -> None:
    from types import SimpleNamespace

    from run_site import containers as containers_mod

    client = SimpleNamespace(containers=SimpleNamespace(get=lambda cid: container))
    monkeypatch.setattr(containers_mod, "_docker_client", lambda: client)


@pytest.mark.parametrize("launcher_cls", [TestcontainersPostgres, TestcontainersRedis])
def test_fallback_stop_removes_anonymous_volumes(monkeypatch, launcher_cls) -> None:
    """Regression for the volume leak: the by-id fallback must remove the
    container together with its anonymous volumes (docker rm -v)."""

    fake = _FakeFallbackContainer()
    _patch_docker_client(monkeypatch, fake)
    launcher_cls().stop("cid1234567890")
    assert fake.stop_calls == 1
    assert fake.remove_kwargs == [{"force": True, "v": True}]


@pytest.mark.parametrize("launcher_cls", [TestcontainersPostgres, TestcontainersRedis])
def test_fallback_stop_keeps_volumes_when_knob_off(monkeypatch, launcher_cls) -> None:
    fake = _FakeFallbackContainer()
    _patch_docker_client(monkeypatch, fake)
    launcher_cls().stop("cid1234567890", remove_volumes=False)
    assert fake.remove_kwargs == [{"force": True, "v": False}]


@pytest.mark.parametrize("launcher_cls", [TestcontainersPostgres, TestcontainersRedis])
def test_fallback_removal_survives_stop_failure(monkeypatch, caplog, launcher_cls) -> None:
    """A hiccup in the graceful stop() must not skip removal — remove
    (force=True) kills the container on its own, and the failure is logged."""

    fake = _FakeFallbackContainer(stop_raises=True)
    _patch_docker_client(monkeypatch, fake)
    with caplog.at_level(logging.WARNING, logger="run_site.containers"):
        launcher_cls().stop("cid1234567890")
    assert fake.remove_kwargs == [{"force": True, "v": True}]
    assert any("forcing removal" in record.message for record in caplog.records)


@pytest.mark.parametrize("launcher_cls", [TestcontainersPostgres, TestcontainersRedis])
def test_wrapped_stop_forwards_delete_volume(launcher_cls) -> None:
    """When the stop goes through the wrapped testcontainers object (same
    launcher instance that started it), the knob maps to delete_volume."""

    recorded: dict[str, bool] = {}

    class FakeWrapped:
        def stop(self, delete_volume: bool = True) -> None:
            recorded["delete_volume"] = delete_volume

    launcher = launcher_cls()
    launcher._containers["cid1234567890"] = FakeWrapped()
    launcher.stop("cid1234567890", remove_volumes=False)
    assert recorded == {"delete_volume": False}
```

The two tests that use `import pytest` inline inside existing test functions (`test_redis_failure_rolls_back_pg`, `test_redis_failure_does_not_stop_pg_when_attached`) can keep their local imports — the new module-level `import pytest` makes them redundant but harmless; do not refactor them in this task.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_containers.py -v -k "fallback or wrapped"`
Expected: FAIL —
- `test_fallback_stop_removes_anonymous_volumes`: `assert [{'force': True}] == [{'force': True, 'v': True}]` (the leak, pinned),
- `test_fallback_stop_keeps_volumes_when_knob_off`: same shape,
- `test_fallback_removal_survives_stop_failure`: `assert [] == [...]` (removal skipped after stop() raised),
- `test_wrapped_stop_forwards_delete_volume`: `recorded == {}` is falsy → assertion error (kwarg not forwarded).

- [ ] **Step 3: Implement the fix in `src/run_site/containers.py`**

Add a module-level helper in the "Internals" section, right after `_docker_client` (~line 443):

```python
def _stop_container_by_id(container_id: str, *, remove_volumes: bool) -> None:
    """Stop and remove a container by id (the fallback when the launcher
    that started it is gone — the CLI's normal shutdown path).

    ``remove_volumes`` maps to ``docker rm -v``: Docker deletes exactly the
    anonymous volumes it attached to this container; named volumes and bind
    mounts are never touched.
    """

    client = _docker_client()
    try:
        container = client.containers.get(container_id)
    except Exception:
        # Container already gone (or daemon unreachable) — nothing to stop.
        logger.debug("Container %s not found; skipping stop", container_id[:12], exc_info=True)
        return
    try:
        container.stop()
    except Exception:
        # remove(force=True) below kills the container on its own.
        logger.warning(
            "Graceful stop of container %s failed; forcing removal",
            container_id[:12],
            exc_info=True,
        )
    try:
        container.remove(force=True, v=remove_volumes)
    except Exception:
        logger.warning("Removing container %s failed", container_id[:12], exc_info=True)
```

Replace `TestcontainersPostgres.stop` (currently lines 164-177, signature already updated in Task 2) — the body becomes:

```python
    def stop(self, container_id: str, *, remove_volumes: bool = True) -> None:
        wrapped = self._containers.pop(container_id, None)
        if wrapped is not None:
            try:
                wrapped.stop(delete_volume=remove_volumes)
            except Exception:
                logger.warning(
                    "Stopping container %s via testcontainers failed",
                    container_id[:12],
                    exc_info=True,
                )
            return
        _stop_container_by_id(container_id, remove_volumes=remove_volumes)
```

Replace `TestcontainersRedis.stop` (currently lines 225-238) with the **identical** body (both classes previously duplicated the same fallback code; it now lives once in `_stop_container_by_id`).

After this change nothing in the two `stop` methods uses `suppress` anymore; `from contextlib import suppress` is still needed by `find_existing` and `start_containers` — leave the import alone.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_containers.py -v`
Expected: all PASS.

Run: `uv run mypy src/run_site && uv run ruff check .`
Expected: clean.

- [ ] **Step 5: Commit**

```bash
git add src/run_site/containers.py tests/test_containers.py
git commit -m "Remove anonymous volumes when stopping containers

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: Wire the knob through the CLI shutdown paths

**Files:**
- Modify: `src/run_site/cli.py` (three `stop_containers(containers)` call sites: ~line 653, ~line 1045, ~line 1082)

**Interfaces:**
- Consumes: `stop_containers(..., remove_volumes=...)` from Task 2 and `config.containers.remove_volumes` from Task 1. `config` is in scope at all three sites (local in the serve function for the first two; a parameter of `_shutdown` for the third).
- Produces: nothing new — behavior wiring only. Coverage note: the spec leaves CLI-wiring tests to the unit layers (Tasks 1-3); do not add a CLI test here.

- [ ] **Step 1: Edit call site 1 — `--print-env` diagnostic path (~line 651)**

Current code:

```python
            if not opts.reuse:
                with _suppress():
                    stop_containers(containers)
                with _suppress():
                    cleanup_sqlite(sqlite_state)
            return 0
```

Change the `stop_containers` line:

```python
            if not opts.reuse:
                with _suppress():
                    stop_containers(
                        containers, remove_volumes=config.containers.remove_volumes
                    )
                with _suppress():
                    cleanup_sqlite(sqlite_state)
            return 0
```

- [ ] **Step 2: Edit call site 2 — `except Exception` cleanup block (~line 1042)**

Current code:

```python
    except Exception:
        proc_group.terminate_all()
        with _suppress():
            stop_containers(containers)
        with _suppress():
            cleanup_sqlite(sqlite_state)
```

Change the `stop_containers` line:

```python
    except Exception:
        proc_group.terminate_all()
        with _suppress():
            stop_containers(containers, remove_volumes=config.containers.remove_volumes)
        with _suppress():
            cleanup_sqlite(sqlite_state)
```

- [ ] **Step 3: Edit call site 3 — `_shutdown` (~line 1080)**

Current code:

```python
    if not opts.reuse:
        with _suppress():
            stop_containers(containers)
        with _suppress():
            cleanup_sqlite(sqlite_state)
```

Change the `stop_containers` line:

```python
    if not opts.reuse:
        with _suppress():
            stop_containers(containers, remove_volumes=config.containers.remove_volumes)
        with _suppress():
            cleanup_sqlite(sqlite_state)
```

(`_shutdown` already takes `config: RunSiteConfig` as a keyword parameter.)

- [ ] **Step 4: Verify no call site was missed and the suite is green**

Run: `grep -n "stop_containers(" src/run_site/cli.py`
Expected: exactly 3 hits, all containing `remove_volumes=config.containers.remove_volumes` (plus the import line if grep matches it — imports don't have `(` after the name, so 3 hits total).

Run: `uv run pytest -v -m "not docker" --tb=short`
Expected: full unit suite PASSES.

Run: `uv run ruff check . && uv run ruff format --check . && uv run mypy src/run_site`
Expected: clean.

- [ ] **Step 5: Commit**

```bash
git add src/run_site/cli.py
git commit -m "Pass remove_volumes knob from CLI shutdown paths

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 5: Documentation and example config

**Files:**
- Modify: `docs/configuration.md` (section `## [containers]`, lines 171-180)
- Modify: `examples/runsite.bpp.toml` (after the `[redis]` block)

**Interfaces:**
- Consumes: final knob semantics from Tasks 1-4 (names must match: `[containers].remove_volumes`, default `true`).
- Produces: user-facing docs; no code.

- [ ] **Step 1: Rewrite the `[containers]` section in `docs/configuration.md`**

Replace lines 171-180 (from `## [containers]` up to, but not including, `## [dump]`) with:

````markdown
## `[containers]`

```toml
[containers]
ryuk = "auto"           # "auto" | true | false
remove_volumes = true   # delete anonymous volumes when run-site stops the stack
```

Controls testcontainers' Ryuk reaper. `auto` enables Ryuk for fresh runs
and disables it with `--reuse` (named containers shouldn't be auto-killed
between runs).

`remove_volumes` (default `true`) deletes the anonymous Docker volumes
the PG/Redis images declare (`/var/lib/postgresql/data`, `/data`)
together with their containers when run-site tears the stack down. Only
volumes Docker attached to run-site's own containers are affected —
named volumes and bind mounts are never touched (`docker rm -v`
semantics).

Set `remove_volumes = false` only for post-mortem inspection of a
container's data: after the container is gone the volume is dangling and
unlabeled, so this is not a persistence mechanism — keeping data between
runs is what `--reuse` is for (the container stays alive together with
its volume). The knob only governs teardown performed by run-site
itself; when the process dies without cleaning up (SIGKILL, closed
terminal), Ryuk removes the containers **including** their anonymous
volumes regardless of this setting. For volumes that must survive a hard
kill, also set `ryuk = false`.

**Cleaning up historical leftovers:** earlier versions of run-site
orphaned one anonymous volume per PG/Redis container on every non-reuse
run. A one-time

```bash
docker volume prune
```

removes dangling anonymous volumes — note it touches *all* dangling
anonymous volumes on the machine, not just run-site's.

````

- [ ] **Step 2: Add a commented example to `examples/runsite.bpp.toml`**

After the `[redis]` block (currently:

```toml
[redis]
image = "redis:7-alpine"
```

) insert:

```toml
# [containers]
# Anonymous PG/Redis data volumes are deleted on shutdown by default.
# Uncomment to keep them for post-mortem inspection (they will be left
# dangling; combine with ryuk = false to also survive a hard kill).
# For keeping data between runs use --reuse instead.
# remove_volumes = false
```

- [ ] **Step 3: Sanity-check rendering and suite**

Run: `uv run pytest -v -m "not docker" --tb=short`
Expected: PASS (docs changes can't break tests; this is the final green gate).

Run: `uv run pre-commit run --all-files`
Expected: hooks pass (formatting, safety).

- [ ] **Step 4: Commit**

```bash
git add docs/configuration.md examples/runsite.bpp.toml
git commit -m "Document [containers].remove_volumes

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```
