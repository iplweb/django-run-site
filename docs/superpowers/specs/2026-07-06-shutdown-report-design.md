# Shutdown container/volume report + deterministic container names — design

Date: 2026-07-06
Status: approved

## Problem

When the user stops `run-site` (ctrl+c), teardown is completely silent: the
PostgreSQL and Redis containers — and, since v0.18.0, their anonymous
volumes — are removed without any message. The user cannot tell what was
deleted, and in `--reuse` / `remove_volumes = false` runs cannot tell what
was intentionally left behind on disk.

Additionally, containers only get a meaningful name in `--reuse` mode
(`{project_slug}-runsite-pg`); in the default mode `name=None` is passed
and Docker assigns a random name like `sharp_ptolemy`, so the user cannot
tell in `docker ps` which containers belong to which run-site run.

## Goal

After shutdown, `run-site` reports per service:

- which containers it removed (name + short id),
- which anonymous volumes it removed,
- and — when nothing was deleted — what was left and why
  (`--reuse`, `remove_volumes = false`).

Full report was chosen over "deletions only": the user always learns what
happened, including what remains on disk.

Secondary goal: containers are always named, in every mode, so both
`docker ps` and the shutdown report show identifiable names.

## Non-goals

- Changing any deletion behavior. This is reporting only; the existing
  stop/removal semantics (including `[containers].remove_volumes`) stay
  byte-for-byte identical.
- Verifying deletions after the fact. The report is intent-based
  (best-effort); actual removal failures are already surfaced via the
  existing `logger.warning` calls in the launchers.
- Reporting named volumes or bind mounts — `docker rm -v` never touches
  them, so there is nothing to report.

## Design

### Container naming (all modes)

`start_containers()` names both containers in every mode:

- `--reuse` (unchanged): `{project_slug}-runsite-pg` /
  `{project_slug}-runsite-redis` — deterministic, so `find_existing`
  keeps locating them across runs.
- default mode (new): `{project_slug}-runsite-pg-{suffix}` /
  `{project_slug}-runsite-redis-{suffix}`, where `suffix` is one random
  6-char lowercase hex string generated once per `start_containers()`
  call and shared by both services. The shared suffix makes it obvious in
  `docker ps` which PG/Redis pair belongs to the same run, and uniqueness
  prevents name collisions between concurrent run-site instances of the
  same project (and with stale containers left by a crash).

`find_existing` is still only consulted in reuse mode — suffixed names
are fresh by construction. Chosen over a PID suffix (possible collision
with a stale container after PID recycling) and over a bare slug
(guaranteed collision for two concurrent runs).

### Shutdown report

Approach: report from `stop_containers()` via a `progress` callback
(chosen over reporting inside the launchers, which would change the
launcher protocol and every test fake, and over returning a report struct,
which would push formatting into three CLI call sites).

### `containers.py`

1. **`_inspect_container(container_id) -> ContainerReportInfo | None`**
   (best-effort): via the existing `_docker_client()`, fetch the container
   name and the names of attached **anonymous** volumes. Anonymous = mount
   of type `volume` whose name is a 64-char hex string (the standard
   heuristic distinguishing anonymous from named volumes). Returns `None`
   when the daemon is unreachable or the container does not exist. Never
   raises; failures are logged at debug level.

2. **`_stop_and_report(service, container_id, stop_fn, remove_volumes,
   progress)`**: inspects **before** removal (after `docker rm -v` there is
   nothing left to ask), calls the launcher's stop exactly as today, then
   emits report lines through `progress`. Used from both
   `stop_containers()` and the rollback path in `start_containers()`
   (Redis failed to start → PG is cleaned up → that cleanup is reported
   through the `emit` callback already available there).

3. **`stop_containers(..., progress: ProgressCallback | None = None)`**:
   - `reuse` and not `force` → emit `left running (reuse)` lines for each
     existing container id (name resolved via inspection, best-effort),
     then return without stopping anything (as today).
   - otherwise delegate to `_stop_and_report` per service.
   - Launcher protocols (`PostgresLauncher` / `RedisLauncher`) and the
     test fakes are **not** changed.
   - Inspection is injectable via a parameter defaulting to
     `_inspect_container` (same pattern as the launcher injection), so
     unit tests never need a Docker daemon.

### `cli.py`

All three `stop_containers` call sites pass `progress=mux.write`:

- the `--print-env` diagnostic path,
- the startup-exception path,
- `_shutdown` (which gains a `mux` parameter — it currently doesn't
  receive the multiplexer).

Shutdown runs inside `StickyRegion`; `mux.write` already handles that, so
no new display machinery is needed.

### Message format

Same visual identity as the startup messages (`_DOCKER_STREAM` /
`_DOCKER_COLOR`, i.e. `[docker]` in blue). Container ids and anonymous
volume names truncated to 12 chars.

Normal ctrl+c (`remove_volumes = true`, default):

```
[docker] postgres: removed container myproj-runsite-pg-3f9a2c (a1b2c3d4e5f6)
[docker] postgres: removed anonymous volume 4f5e6d7c8b9a…
[docker] redis: removed container myproj-runsite-redis-3f9a2c (0f1e2d3c4b5a)
```

`--reuse`:

```
[docker] postgres: left running (reuse) — container myproj-runsite-pg (a1b2c3d4e5f6)
[docker] redis: left running (reuse) — container myproj-runsite-redis (0f1e2d3c4b5a)
```

`remove_volumes = false`:

```
[docker] postgres: removed container myproj-runsite-pg-3f9a2c (a1b2c3d4e5f6)
[docker] postgres: kept volume 4f5e6d7c8b9a… (remove_volumes=false)
```

Edge cases:

- Service disabled in config (container id is `None`): no line — nothing
  was started, nothing to report.
- Inspection failed (daemon gone, container already removed): the line
  degrades to the short container id only, with no name and no volume
  lines — e.g. `[docker] postgres: removed container a1b2c3d4e5f6`.
- No anonymous volumes attached (typical for the stock Redis image): no
  volume lines for that service.
- `progress=None` (default): fully silent, exactly today's behavior — no
  existing caller or test changes behavior implicitly.

## Error handling

The report is intent-based: it states what Docker was asked to remove.
Inspection and reporting are wrapped so they can never break teardown —
any failure degrades the message rather than raising. Removal failures
keep being logged by the existing `logger.warning` calls in
`_stop_container_by_id` and the launchers; the report does not duplicate
that.

## Testing

Unit tests (no Docker), using fake launchers plus a fake inspector:

- full teardown → `removed container` + `removed anonymous volume` lines
  with names from the inspector,
- `remove_volumes=false` → `kept volume …` lines,
- `reuse=True` → `left running (reuse)` lines, launchers' stop **not**
  called (existing semantics preserved),
- inspector returns `None` → id-only degraded lines, no volume lines,
- `progress=None` → no lines emitted, no crash,
- disabled service (`None` container id) → no lines for that service,
- rollback path in `start_containers` (Redis start fails) → PG cleanup
  reported through `emit`.

Naming unit tests (fake launchers record the `name` argument):

- default mode → both launchers receive `{slug}-runsite-{svc}-{suffix}`
  with the same 6-char hex suffix for PG and Redis,
- two consecutive `start_containers()` calls → different suffixes,
- `reuse=True` → unchanged deterministic names, no suffix.

One `docker`-marked integration test: start a real stack, stop it with a
recording progress callback, assert the lines mention the real PG
container name and at least one anonymous volume.

## Documentation

- `docs/configuration.md` / `docs/troubleshooting.md`: note that shutdown
  prints what was removed/kept, and how `--reuse` /
  `[containers].remove_volumes` change the message; document the container
  naming scheme (`{slug}-runsite-{svc}` in reuse mode,
  `{slug}-runsite-{svc}-{suffix}` otherwise).
- CHANGELOG entry for the next release.
