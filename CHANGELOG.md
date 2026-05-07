# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.3.0] — 2026-05-07

Initial implementation of the v0.3 spec — pure CLI orchestrator (no Django dependency).

### Added

- `django-run-site run` command — orchestrates a local Django dev stack:
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
- `django-run-site doctor` — config + manage.py + Docker + git/uv sanity
  check, no containers started.
- TOML config (`runsite.toml` or `[tool.django-run-site]` in `pyproject.toml`)
  with full validation.

### Notes

- Django integration features (autologin, dotfiles, agent help) live in the
  separate [`django-dev-helpers`](https://github.com/iplweb/django-dev-helpers)
  package and are wired through env-var contract — see
  [docs/with-django-dev-helpers.md](docs/with-django-dev-helpers.md).
