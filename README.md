# django-run-site

> Pure CLI orchestrator for local Django development. PostgreSQL & Redis
> testcontainers + dump load + local `runserver`/Celery + log multiplexer +
> hooks — all in one command. **Zero Django dependency in the CLI itself.**

[![PyPI version](https://img.shields.io/pypi/v/django-run-site.svg)](https://pypi.org/project/django-run-site/)
[![Python](https://img.shields.io/pypi/pyversions/django-run-site.svg)](https://pypi.org/project/django-run-site/)
[![CI](https://github.com/iplweb/django-run-site/actions/workflows/test.yml/badge.svg)](https://github.com/iplweb/django-run-site/actions/workflows/test.yml)
[![License](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

## What it does

One command — `django-run-site run` — to spin up a complete local Django dev
stack:

- **PostgreSQL** + **Redis** testcontainers on random (or stable, with
  `--reuse`) ports.
- Optional dump load (`.sql`, `.sql.gz`, `.dump`/`.pgdump`) using the right
  strategy (init-script for fresh PG, `psql`/`pg_restore` post-start
  otherwise).
- Local `migrate`, superuser creation, `runserver`, Celery worker/beat, and
  any extra processes you declare — multiplexed into one terminal with
  colored log prefixes.
- HTTP readiness probe and browser auto-open.
- Lifecycle hooks (`pre_containers`, `post_migrate`, `pre_serve`, …) that can
  shell out (`type = "command"`) or run inside Django (`type = "django"` via
  `manage.py shell -c`).
- `--from-git URL` / `--from-path PATH` — run any Django project from any
  source without manual `git clone` + `uv sync`.

The CLI **does not import Django**, does not modify `urls.py`, does not know
your `settings.py`. It only spawns `<your-python> <your-manage.py> <command>`
as subprocesses and multiplexes their logs.

## Install

```bash
pipx install django-run-site
# or
uv tool install django-run-site
```

Requirements: Python 3.11+, Docker daemon running, `git` (only if you use
`--from-git`).

## Quickstart

In your Django project root, create `runsite.toml`:

```toml
project_slug = "myproject"
manage_py = "src/manage.py"

[python]
executable = "auto"

[postgres]
image = "postgres:16"
user = "myproject"
password = "password"
db = "myproject"

[redis]
image = "redis:7-alpine"

[django]
runserver_bind = "127.0.0.1"
runserver_display_host = "localhost"
browser_probe_path = "/admin/login/"
migrate = true

[superuser]
enabled = true
username = "admin"
password = "admin"
email = "admin@example.com"
```

Then run:

```bash
django-run-site run
```

That's it — you'll get migrate, an `admin/admin` superuser, `runserver`
listening on a random free port, browser opened on the homepage, and
container logs streaming in your terminal.

### Run from a Git URL

No clone, no venv, no `uv sync` to do by hand:

```bash
django-run-site run --from-git https://github.com/iplweb/bpp.git --branch main
```

The CLI clones the repo to `~/.cache/django-run-site/checkouts/<slug>/`,
creates a venv, installs deps (auto-detecting `uv.lock` /
`pyproject.toml` / `requirements.txt`), then runs as usual. Reuse with
`--no-pull --no-install`. See [docs/from-git.md](docs/from-git.md).

### Run from any local checkout

```bash
django-run-site run --from-path ~/Programowanie/some-django-app
```

No need to `cd` first.

### Reuse containers between runs

```bash
django-run-site run --reuse
```

Stable container names — `<project_slug>-runsite-pg` and `-redis` — survive
between runs so you don't reload the dump each time.

## What's in the box

| File / module | Role |
|---|---|
| `cli.py` | Argparse entrypoint, two-pass parsing, `run` and `doctor` flow. |
| `config.py` | `runsite.toml` / `pyproject.toml` loader + validator. |
| `discovery.py` | Project root, `manage.py`, local Python resolution chain. |
| `containers.py` | testcontainers PG + Redis, named/reuse, Ryuk policy. |
| `dumps.py` | Format detection, init-script vs. post-start strategy. |
| `env.py` | Build env for subprocesses + the `DEV_HELPERS_*` contract. |
| `processes.py` | Spawn, terminate, HTTP probe. |
| `log_multiplexer.py` | Colored prefixes per stream. |
| `hooks.py` | Command / Django hook execution. |
| `superuser.py` | `manage.py shell -c` with `get_user_model()`. |
| `banner.py` | Orchestrator banner with URLs and credentials. |
| `source/from_git.py` | Clone/pull, slug extraction, ownership policy. |
| `source/venv_setup.py` | `uv venv` / `python -m venv`. |
| `source/deps_installer.py` | `uv sync` / `pip install -r`. |

## Companion package — `django-dev-helpers`

The features that live *inside* Django (autologin endpoint, dotfile
generation, agent help banner) are intentionally split into a separate
package, [`django-dev-helpers`](https://github.com/iplweb/django-dev-helpers).
You install it in your Django project and the two communicate via a
documented `DEV_HELPERS_*` env-var contract — neither imports the other.

```bash
uv add django-dev-helpers --group dev
```

```python
INSTALLED_APPS = [..., "django_dev_helpers"]
```

See [docs/with-django-dev-helpers.md](docs/with-django-dev-helpers.md) for
the full integration story.

## Documentation

- [Quickstart](docs/quickstart.md)
- [Configuration reference](docs/configuration.md)
- [Run from Git or path](docs/from-git.md)
- [Local processes (Celery, extras)](docs/local-processes.md)
- [Hooks](docs/hooks.md)
- [Integration with `django-dev-helpers`](docs/with-django-dev-helpers.md)
- [Troubleshooting](docs/troubleshooting.md)

## Examples

- [`examples/runsite.minimal.toml`](examples/runsite.minimal.toml) — bare
  minimum config.
- [`examples/runsite.celery.toml`](examples/runsite.celery.toml) — adds
  Celery worker + beat.
- [`examples/runsite.bpp.toml`](examples/runsite.bpp.toml) — full
  BPP-style config with custom PG image, dump, hooks, dynamic CLI args.
- [`examples/test_site/`](examples/test_site/) — a small Django project
  used by integration tests; runs end-to-end with `django-run-site run`.

## Status

v0.3 is the first implementation matching the
[`DJANGO-RUN-SITE-SPEC-v0.3.md`](DJANGO-RUN-SITE-SPEC-v0.3.md) spec. APIs
documented under §13 (env contract) are stable; CLI flags and config
schema may still evolve before 1.0 — see [CHANGELOG.md](CHANGELOG.md).

## License

MIT — see [LICENSE](LICENSE).
