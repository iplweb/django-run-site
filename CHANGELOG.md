# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- **`RuntimeError: reentrant call inside <_io.BufferedWriter>` on terminal
  resize.** The sticky banner's `SIGWINCH` handler redrew the banner by
  writing ANSI sequences directly to stdout. Signal handlers run
  synchronously on the main thread, so a second resize landing mid-write
  re-entered the in-progress `BufferedWriter` write and crashed `run-site`
  (an `RLock` does not help — the same thread already holds it). The handler
  now performs **no I/O**: it only flags a redraw via an `Event`, and a
  dedicated `sticky-redraw` worker thread does the actual redraw off the
  signal/main thread, where a concurrent write merely blocks on the stream
  lock instead of re-entering it. Bursts of resize events coalesce.

## [0.16.0] — 2026-06-01

### Added

- **Restore `pg_dump` directory-format dumps packaged as `.tar.gz`/`.tgz`.**
  A directory dump (`pg_dump -Fd`) that was `tar | gzip`-ed for transport is
  now unwrapped to a temp directory and restored via the container's
  `pg_restore` (which auto-detects the archive format). Previously such a
  file was misread as gzipped SQL and piped into `psql`, failing with
  `Piped restore failed: left=-13 right=3`.

### Changed

- **Dump format is detected by content (magic bytes), not just the filename.**
  `detect_format` now inspects the leading bytes — gzip wrapper, `PGDMP`
  custom-dump magic, or a `tar` header — and falls back to the extension only
  when the content is inconclusive. This fixes archives with non-canonical
  names (e.g. `*.tar.gz`, `backup.bin`) being routed to the wrong loader. The
  three `pg_restore` archive formats (custom / directory / tar) are no longer
  distinguished internally; `pg_restore` identifies the specific format.

## [0.15.0] — 2026-05-25

### Added

- **Container and dump lifecycle messages during startup.** Previously,
  the first thing the user saw after launching `run-site` was a long
  silence — testcontainers booted Postgres and Redis, then `pg_restore`
  replayed the dump, all with no visible output until `[migrate] running
  migrations…` finally appeared. For multi-GB dumps that silence could
  stretch past a minute.

  Both phases now stream messages through the existing log multiplexer:

  ```
  [docker] postgres: starting image=postgres:16…
  [docker] postgres: ready @ 127.0.0.1:54321 (3.4s)
  [docker] redis: starting image=redis:7-alpine…
  [docker] redis: ready @ 127.0.0.1:49153 (1.1s)
  [dump] copying db-backup-20260428.pg_dump (412.7 MB) into container…
  [dump] restoring db-backup-20260428.pg_dump via pg_restore (this may take a while)…
  [migrate] running migrations…
  ```

  `--reuse` runs surface `reusing existing container <id12> @ host:port`
  in place of the `starting…` / `ready…` pair.

- New `progress` keyword argument on `start_containers()` and
  `execute_post_start()` — `Callable[[stream, color, line], None]`
  matching `mux.write`. Defaults to a no-op so library/test callers
  keep the previous silent behavior.

### Notes

- Image pulls on first run still happen inside the testcontainers
  launcher and remain silent — the `starting image=…` line precedes
  any pull. Pre-pull-with-progress would require dropping below
  testcontainers into the raw Docker SDK; not in this release.
- `pg_restore -v` per-table output is still captured by
  `subprocess.run`; you see start/end progress lines but not the
  per-object stream.

## [0.8.0] — 2026-05-12

### Fixed

- **Celery worker no longer fails with `ImproperlyConfigured: Requested
  settings, but settings are not configured.`** Run-site now injects
  `DJANGO_SETTINGS_MODULE` into every subprocess environment, discovered
  from the project's `manage.py` via `discover_settings_module()`.
  Previously, `manage.py`-driven subprocesses (migrate, runserver) worked
  because `manage.py` does `os.environ.setdefault("DJANGO_SETTINGS_MODULE",
  ...)` itself, but `python -m celery -A <app> worker` skips `manage.py`
  entirely — so any celery app that does `app.config_from_object(
  "django.conf:settings")` without its own `setdefault` would crash at
  startup. The new behavior uses `setdefault` semantics, so a user-exported
  `DJANGO_SETTINGS_MODULE` still wins.

## [0.7.0] — 2026-05-12

### Changed

- **`[postgres].enabled` and `[redis].enabled` now default to `"auto"`**
  (matching `[sqlite].enabled`). On every run, run-site scans the
  project's settings module and only starts the Postgres / Redis
  container when settings actually reference that backend
  (`django.db.backends.postgresql`, `postgres://`, `django_redis`,
  `redis://`, `CELERY_BROKER_URL`, …). Projects that don't use one of
  these services now boot without the corresponding container — and
  with neither Postgres nor Redis enabled, the Docker availability
  check is skipped entirely.

  **Breaking:** if your `settings.py` doesn't statically reference a
  service that you still want booted (e.g. for a fixture import step
  before settings change), set `enabled = true` explicitly in
  `runsite.toml`.

### Fixed

- `--version` test no longer asserts a hardcoded version string; reads
  the package's `__version__` so it tracks releases automatically.

## [0.6.0] — 2026-05-11

### Added

- **Managed SQLite mode.** New `[sqlite]` section + `--sqlite` / `--no-sqlite`
  CLI flags. With `--reuse`, run-site keeps the file at
  `<project_root>/.run-site/<slug>.sqlite3` (or an explicit
  `[sqlite].path`) across runs. Without `--reuse`, the DB is a random
  temp file removed on exit — mirroring the ephemeral testcontainer
  behavior. `database_url` is exposed to the project as
  `sqlite:///<abspath>` via the standard `[env]` mapping.
- **Tri-state `enabled`.** `[postgres].enabled`, `[redis].enabled`, and
  `[sqlite].enabled` now accept `true | false | "auto"`. The
  `"auto"` value statically scans your project's settings module
  (following one level of `from .base import *`) for engine strings
  (`django.db.backends.{postgresql,sqlite3}`), URL schemes
  (`postgres://`, `sqlite:///`), and Redis hints (`django_redis`,
  `RedisCache`, `redis://`, `CELERY_BROKER_URL`). Defaults: postgres
  and redis stay `true` (backward compatible); sqlite is `"auto"`.
- **`.gitignore` warning.** When run-site creates the persistent
  `.run-site/` directory for a `--reuse` SQLite file, it checks
  `.gitignore` and warns if `.run-site` isn't listed.
- Sidecar gains a `[sqlite]` block (path + url + ephemeral flag) when
  SQLite mode is active, alongside the existing `[postgres]` / `[redis]`
  blocks.

### Fixed

- `--from-dump` / `[dump].default_path` requested under SQLite mode now
  fails fast with a clear message (mirroring the existing
  `--no-postgres` + dump refusal).

## [0.5.0] — 2026-05-11

### Added

- **Optional services.** New `[postgres].enabled` and `[redis].enabled`
  config keys (with matching `--no-postgres` / `--no-redis` CLI flags) let
  you skip the corresponding testcontainer entirely. A disabled service
  pulls no image, starts no container, omits its `DEV_HELPERS_*` env
  vars, drops its `database_url` / `redis_url` / `db_*` / `redis_*`
  mappings from the project `[env]` lookup, and removes its block from
  the runtime sidecar. With both Postgres and Redis disabled, the Docker
  availability check is skipped too — useful for SQLite-only,
  cache-less stacks on machines without Docker running.
- `examples/runsite.sqlite.toml` — ready-to-copy SQLite/no-Docker config.
- The banner gracefully marks disabled services as `disabled` and tailors
  the `Lifecycle:` line to only the services it actually started (or
  hides it entirely when nothing was started).
- `runsite.toml` is now fully optional: with no config file at all,
  `run-site run` falls back to documented defaults for every section
  (Postgres + Redis testcontainers, `manage.py` auto-detected,
  `runserver` on `127.0.0.1`, `admin / admin` superuser, slug derived
  from the project root). Add config only to override.
- Auto-derived `project_slug` is now sanitized to `[A-Za-z0-9_.-]+`
  (falling back to `runsite` for empty / unusable directory names), so
  project roots with spaces or other oddities don't break validation
  when the user never sets `project_slug` explicitly.

### Changed

- **PyPI distribution renamed** `run-site` → `django-run-site`. The
  installed CLI command (`run-site`) and the Python import name
  (`run_site`) are **unchanged** — only the dist name on PyPI moves, so
  installation goes from `uv tool install run-site` to
  `uv tool install django-run-site`. Existing users on `run-site` should
  uninstall it and install `django-run-site`; everything they invoke
  afterwards (the `run-site` command, `from run_site import …`,
  `[tool.run-site]` in `pyproject.toml`) keeps working as before.
- `uv tool run run-site …` recipes in the docs now use the explicit
  `uv tool run --from django-run-site run-site …` form, since the
  distribution name and CLI command name differ.

### Fixed

- Asking for a dump load (`--from-dump` / `[dump].default_path`) with
  Postgres disabled now fails fast with a clear message instead of
  attempting to load against a container that was never started.
- Container teardown on partial-start failures no longer crashes when the
  half-started service has no container id yet.
- Sidecar URL rendering URL-encodes the Postgres user/password, matching
  the contract `build_subprocess_env` already uses, so values with `@`,
  `/`, `:`, or spaces produce a parseable connection URL.

## [0.4.0] — 2026-05-07

First release under the (then-new) PyPI name **`run-site`** — note: a
later Unreleased version renames the distribution again to
**`django-run-site`** (see above). The Python module is `run_site` and
the repo continues to live at `iplweb/django-run-site`.

### Added

- `run-site init` — generate a working `runsite.toml` from the project
  layout. Detects `manage.py` (root, `src/`, or up to two directories
  deep), the Django project module, Celery, and `uv`. When `uv` is on
  PATH it writes `[python].command = ["uv", "run", "--no-sync", "python"]`.
- `.run-site-config` runtime sidecar — TOML dotfile written to the
  project root with live ports + connection URLs (web / postgres / redis /
  celery). Created **before** `pre_serve` hooks and **before**
  `runserver`, so `django-dev-helpers` (and any other tooling) can read
  it from `AppConfig.ready()`. Removed on clean shutdown.
- `[django].web_command` — replace `manage.py runserver` with daphne,
  uvicorn, gunicorn, or anything else. Same template substitution as
  `[[extra_processes]].command`, plus a new `{bind}`.
- Banner overhaul: copy-paste-ready `psql` command, libpq env-var line
  (`PGHOST=… PGPORT=… PGDATABASE=… PGUSER=… PGPASSWORD=…`), dev
  superuser status (created / reset / unchanged / disabled) with
  credentials shown only when the password we'd print actually matches
  what's in the DB, container `Lifecycle:` line that explains
  `--reuse` and the cleanup `docker rm -f` snippet, Celery enable hint
  when disabled in config, and `Sidecar:` path.
- `manage.py` auto-detection now scans one and two directories deep
  (e.g. `test_project/manage.py`, `tests/test_project/manage.py`),
  skipping `.venv`, `node_modules`, `__pycache__`, `build`, `dist`,
  `.git`, `.tox`, …; ambiguous matches are filtered to those that
  AST-confirmed `import django`, with a clear error listing remaining
  candidates if more than one passes.
- Honest top-of-README framing: "uvx-wannabe for full-fledged Django
  sites" with the actual primary use case (parallel per-worktree
  stacks, non-conflicting auto-picked ports, `.run-site-config` for
  human + agent discoverability).

### Changed

- **PyPI distribution renamed** `django-run-site` → `run-site`. The
  Python import name went from `django_run_site` to `run_site`. The
  CLI binary went from `django-run-site` to `run-site`. Repo URL
  unchanged. Existing users: uninstall the old name and install
  `run-site`.
- `--manage-py` and `--python` relative paths now anchor to
  `project_root`, not CWD — important for `--from-git` users who
  invoke the CLI from an unrelated directory.
- `<project_root>/.venv/bin/python` is now preferred over
  `$VIRTUAL_ENV` so `uv tool run run-site` and `pipx run run-site`
  don't pick the wrapper tool's own venv (which has no Django).

### Fixed

- Stop calling `Path.resolve()` on Python interpreter paths — `uv venv`
  symlinks `.venv/bin/python` to the upstream interpreter; resolving
  the symlink lands on a path with no `pyvenv.cfg` in scope, so
  CPython runs in non-venv mode and `import django` fails. Replaced
  with `os.path.abspath` (collapses `..` / `.`, prepends CWD if
  relative, leaves symlinks alone).

## [0.3.0] — 2026-05-07

Initial implementation of the v0.3 spec — pure CLI orchestrator (no Django dependency).

### Added

- `run-site run` command — orchestrates a local Django dev stack:
  - PostgreSQL + Redis testcontainers on random or named ports.
  - Optional dump load (`.sql`, `.sql.gz`, `.dump`/`.pgdump`).
  - Local subprocess `migrate`, superuser creation, `runserver`, Celery worker/beat,
    extra processes — multiplexed log output with colored prefixes.
  - HTTP probe + browser open of homepage.
  - `--reuse` for stable named containers between runs.
- `--from-git URL` — clone a remote Django project, set up venv, install
  deps (`uv sync` / `pip install`), and run it. With `--branch`, `--tag`,
  `--commit`, `--checkout-path`, `--no-cache`, `--no-pull`, `--no-install`,
  `--force-reset`.
- `--from-path PATH` — run a Django project from any local directory without
  changing CWD.
- Two-pass CLI parsing — hooks may register dynamic flags via `cli_args` /
  `cli_disable_flag` in `runsite.toml`.
- Hook stages: `pre_containers`, `post_containers`, `pre_dump`, `post_dump`,
  `post_migrate`, `post_superuser`, `pre_serve`, `post_stop`. Both
  `type = "command"` (host shell) and `type = "django"` (`manage.py shell -c`)
  are supported, with a JSON context passed to Django callables.
- `DEV_HELPERS_*` env contract — every value documented in the spec is set on
  the `runserver` subprocess so [`django-dev-helpers`](https://github.com/iplweb/django-dev-helpers)
  can pick them up without direct dependency.
- `run-site doctor` — config + manage.py + Docker + git/uv sanity
  check, no containers started.
- TOML config (`runsite.toml` or `[tool.run-site]` in `pyproject.toml`)
  with full validation.

### Notes

- Django integration features (autologin, dotfiles, agent help) live in the
  separate [`django-dev-helpers`](https://github.com/iplweb/django-dev-helpers)
  package and are wired through env-var contract — see
  [docs/with-django-dev-helpers.md](docs/with-django-dev-helpers.md).
