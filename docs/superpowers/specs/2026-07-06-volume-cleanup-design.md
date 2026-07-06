# Volume cleanup for the test stack

Date: 2026-07-06
Status: approved

## Problem

Every non-reuse `run-site` run with PG/Redis enabled leaks anonymous
Docker volumes. The `postgres` and
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

The knob's intended use case for `false` is post-mortem inspection of a
container's data after a run — not data persistence between runs. After
`docker rm` without `-v` an anonymous volume is dangling, unlabeled, and
impractical to re-attach; persistence is what `--reuse` is for (the
container stays alive together with its volume).

## Design

### 1. Config (`config.py`)

- `ContainersConfig` gains `remove_volumes: bool = True`.
- Parsed next to `ryuk`; a non-boolean value raises
  `ConfigError("[containers].remove_volumes must be a boolean")`.

### 2. Container layer (`containers.py`) — the actual fix

- The `PostgresLauncher.stop` / `RedisLauncher.stop` protocol methods gain
  a keyword-only parameter: `stop(container_id, *, remove_volumes: bool = True)`.
  The default is repeated in the real launchers and the test fakes; it
  exists for direct callers that don't thread the knob (e.g. the
  integration tests calling `stop_containers` bare).
- In both real launchers (`TestcontainersPostgres`, `TestcontainersRedis`):
  - wrapped path: `wrapped.stop(delete_volume=remove_volumes)`
    (today implicitly `True`, but effectively dead from the CLI),
  - fallback path (the one the CLI actually exercises and the one that
    leaks): `container.remove(force=True, v=remove_volumes)`.
- Fallback error path: `container.stop()` (graceful DB shutdown) and
  `container.remove(...)` are guarded separately — a failure in `stop()`
  is logged and does not prevent the `remove()` attempt, which kills and
  removes on its own (`force=True`). Today a single `suppress` wraps both,
  so a `stop()` hiccup silently skips removal entirely.
- `stop_containers(...)` gains `remove_volumes: bool = True` and forwards
  it to both launchers. `force=True` (the reuse override) honors the knob
  the same way — no special casing. Note `find_existing` attaches by name,
  so a forced stop can remove volumes of a container run-site did not
  create; no CLI path passes `force=True` today.
- The rollback inside `start_containers` (Redis fails after PG started)
  also stops PG with `remove_volumes` taken from `config.containers`.
  (This path flows through the wrapped testcontainers object, which
  already deletes volumes today; the change makes it honor the knob.)

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

### Knob scope vs Ryuk (important semantics)

`remove_volumes` governs only teardown performed by run-site itself.
When the process dies without running its teardown — SIGKILL, closed
terminal, or a Ctrl+C in the window before the signal handlers are
installed (`KeyboardInterrupt` is not caught by the `except Exception`
cleanup block) — Ryuk removes the containers in non-reuse mode, and Ryuk
always removes attached anonymous volumes (`RemoveVolumes: true`),
regardless of the knob. So `remove_volumes = false` is best-effort: it
holds for run-site-initiated shutdown, and additionally requires
`[containers].ryuk = false` to survive a hard kill. This is documented
next to the knob in `docs/configuration.md`.

### 4. Tests

- `test_config.py`: knob parsing — default `true`, explicit `false`,
  non-boolean → `ConfigError`.
- `test_containers.py`:
  - all fakes accept the new kwarg (the module-level `FakePgLauncher` /
    `FakeRedisLauncher` and the inline failing fakes),
  - `stop_containers` forwards `remove_volumes` to the launchers,
  - unit tests for `TestcontainersPostgres.stop` / `TestcontainersRedis.stop`
    fallback path with a substituted Docker client asserting
    `remove(force=True, v=True)` (regression for the actual bug) and
    `v=False` when the knob is off,
  - wrapped path forwards `wrapped.stop(delete_volume=remove_volumes)`,
  - fallback removal still runs (and the failure is logged) when
    `container.stop()` raises,
  - the `start_containers` rollback passes the knob (extend
    `test_redis_failure_rolls_back_pg` with an assertion on the kwarg
    recorded by the fake).
- CLI wiring (three call sites pass the knob) is covered if practical
  via the existing dry-run/CLI test seams; otherwise left to the unit
  layers above.

### 5. Documentation

- `docs/configuration.md`: document `[containers].remove_volumes` next to
  `ryuk`, including the knob-scope caveat (Ryuk ignores the knob on hard
  kill; combine with `ryuk = false` for guaranteed post-mortem volumes)
  and the intended use case (inspection, not persistence — use `--reuse`
  for persistence). Add a short note for historical leftovers: a one-time
  `docker volume prune` removes dangling anonymous volumes (warning: it
  touches all dangling anonymous volumes, not just run-site's).
- `examples/runsite*.toml`: add a commented-out example of the knob.

## Out of scope

- Ctrl+C signal handling — the current sequence (flag in handler, teardown
  on the main thread, second Ctrl+C escalates) is correct. After this fix,
  with default settings both graceful and hard-kill paths clean up volumes
  (the latter via Ryuk in non-reuse mode; see "Knob scope vs Ryuk").
- A `run-site prune` command for pre-existing orphaned volumes — they
  cannot be attributed to run-site (anonymous volumes carry no labels), so
  cleanup stays a documented, deliberate manual step.
- Reuse mode semantics — `stop_containers` still returns early with
  `reuse=True`; kept containers keep their volumes, which is intended.
