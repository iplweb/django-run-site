# Volume cleanup for the test stack

Date: 2026-07-06
Status: approved

## Problem

Every `run-site` run leaks anonymous Docker volumes. The `postgres` and
`redis` images declare `VOLUME` directives (`/var/lib/postgresql/data`,
`/data`), so Docker creates anonymous volumes on each container start.
On shutdown, `cli.py` calls `stop_containers(containers)` without passing
launcher instances, so `stop_containers` builds fresh launchers whose
`_containers` dicts are empty and the stop always takes the fallback path
in `containers.py`, which calls `container.remove(force=True)` — without
`v=True`. The anonymous volumes are orphaned on every graceful shutdown.

Ryuk does not help here: by the time it acts, run-site has already removed
the containers without `-v`, and orphaned anonymous volumes carry no
testcontainers session labels, so Ryuk's volume prune never matches them.
Paradoxically, a hard kill (SIGKILL, closed terminal) in non-reuse mode is
handled correctly today, because Ryuk removes the still-existing containers
with `RemoveVolumes: true`.

## Goal

Anonymous volumes created by the PG/Redis test containers are removed by
default whenever run-site stops the stack. Behavior is controlled by a
config knob `[containers].remove_volumes` (default `true`).

## Design

### 1. Config (`config.py`)

- `ContainersConfig` gains `remove_volumes: bool = True`.
- Parsed next to `ryuk`; a non-boolean value raises
  `ConfigError("[containers].remove_volumes must be a boolean")`.

### 2. Container layer (`containers.py`) — the actual fix

- The `PostgresLauncher.stop` / `RedisLauncher.stop` protocol methods gain
  a keyword-only parameter: `stop(container_id, *, remove_volumes: bool = True)`.
- In both real launchers (`TestcontainersPostgres`, `TestcontainersRedis`):
  - wrapped path: `wrapped.stop(delete_volume=remove_volumes)`
    (today implicitly `True`, but effectively dead from the CLI),
  - fallback path (the one the CLI actually exercises and the one that
    leaks): `container.remove(force=True, v=remove_volumes)`.
- `stop_containers(...)` gains `remove_volumes: bool = True` and forwards
  it to both launchers.
- The rollback inside `start_containers` (Redis fails after PG started)
  also stops PG with `remove_volumes` taken from `config.containers`.

`v=True` only removes anonymous volumes; the read-only bind mount for the
init script is unaffected.

Ownership is never determined by inspecting volumes. run-site never
enumerates or matches volumes (`docker volume ls` is not involved).
`remove(v=True)` is the Docker API's `docker rm -v`: Docker itself tracks
which anonymous volumes are attached to the container being removed and
deletes exactly those. The container, in turn, is unambiguously ours — we
hold its id from `start()` or resolve it via the deterministic reuse name
(`<slug>-runsite-pg` / `<slug>-runsite-redis`). Named volumes and bind
mounts attached to the container are never deleted by `v=True`.

Rejected alternative: a constructor flag on the launchers. `stop_containers`
builds launchers fresh at shutdown anyway, so the value would travel the
same code paths, and an explicit kwarg on `stop` documents the contract
better.

### 3. CLI (`cli.py`)

- All three `stop_containers(containers)` call sites pass
  `remove_volumes=config.containers.remove_volumes`.
- No new CLI flag — the knob lives in config only.

### 4. Tests

- `test_config.py`: knob parsing — default `true`, explicit `false`,
  non-boolean → `ConfigError`.
- `test_containers.py`:
  - fake launchers accept the new kwarg,
  - `stop_containers` forwards `remove_volumes` to the launchers,
  - unit tests for `TestcontainersPostgres.stop` / `TestcontainersRedis.stop`
    with a substituted Docker client asserting `remove(force=True, v=True)`
    (regression for the actual bug) and `v=False` when the knob is off.

### 5. Documentation

- `docs/`: document `[containers].remove_volumes` next to `ryuk`; add a
  short note for historical leftovers: a one-time `docker volume prune`
  removes dangling anonymous volumes (warning: it touches all dangling
  anonymous volumes, not just run-site's).
- `examples/runsite*.toml`: add a commented-out example of the knob.

## Out of scope

- Ctrl+C signal handling — the current sequence (flag in handler, teardown
  on the main thread, second Ctrl+C escalates) is correct. After this fix,
  both graceful and hard-kill paths clean up volumes (the latter via Ryuk
  in non-reuse mode).
- A `run-site prune` command for pre-existing orphaned volumes — they
  cannot be attributed to run-site (anonymous volumes carry no labels), so
  cleanup stays a documented, deliberate manual step.
- Reuse mode semantics — `stop_containers` still returns early with
  `reuse=True`; kept containers keep their volumes, which is intended.
