# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.4.0] — 2026-05-07

First release under the new PyPI name **`run-site`** (the Python module
is `run_site`). The repo continues to live at `iplweb/django-run-site`.

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
