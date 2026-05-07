# run-site

> **Like [`uvx`](https://docs.astral.sh/uv/guides/tools/), but for
> full-fledged\* Django sites.** Point it at a Git URL and you get a
> running stack — Postgres, Redis, migrate, superuser, runserver — without
> ever cloning, building a venv, or editing settings by hand.
>
> \* *Full-fledged* = a Django project that expects PostgreSQL, Redis, and
> some kind of seed dump for the database — not a one-file
> `manage.py runserver` toy. **This is the goal we're aiming at; we're
> not fully there yet** — current focus is `--from-git` /
> `--from-path` ergonomics, dump-restore strategies, the runtime banner,
> and the `.run-site-config` sidecar. See [CHANGELOG.md](CHANGELOG.md)
> for what's shipping and the [Status](#status) section for current
> rough edges.

Pure CLI orchestrator for local Django development. PostgreSQL & Redis
testcontainers + dump load + local `runserver`/Celery + log multiplexer +
hooks — all in one command. **Zero Django dependency in the CLI itself.**

[![PyPI version](https://img.shields.io/pypi/v/run-site.svg)](https://pypi.org/project/run-site/)
[![Python](https://img.shields.io/pypi/pyversions/run-site.svg)](https://pypi.org/project/run-site/)
[![CI](https://github.com/iplweb/django-run-site/actions/workflows/test.yml/badge.svg)](https://github.com/iplweb/django-run-site/actions/workflows/test.yml)
[![License](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

## What it does

One command — `run-site run` — to spin up a complete local Django dev
stack:

- **PostgreSQL** + **Redis** testcontainers on random (or stable, with
  `--reuse`) ports.
- Optional dump load (`.sql`, `.sql.gz`, `.dump`/`.pgdump`) using the right
  strategy (init-script for fresh PG, `psql`/`pg_restore` post-start
  otherwise).
- Local `migrate`, **dev superuser** creation/refresh, `runserver`, Celery
  worker/beat, and any extra processes you declare — multiplexed into one
  terminal with colored log prefixes.
- HTTP readiness probe and browser auto-open.
- Lifecycle hooks (`pre_containers`, `post_migrate`, `pre_serve`, …) that can
  shell out (`type = "command"`) or run inside Django (`type = "django"` via
  `manage.py shell -c`).
- `--from-git URL` / `--from-path PATH` — run any Django project from any
  source without manual `git clone` + `uv sync`.
- A **runtime banner** that hands you everything you'd otherwise look up:
  the `psql` connection command, libpq env-var line, sidecar dotfile path,
  whether containers are ephemeral or kept on exit, and the dev superuser
  credentials.
- A `.run-site-config` **sidecar dotfile** at the project root that
  records the live ports + connection URLs for any tool that wants to
  read them (e.g. `django-dev-helpers` at Django bootstrap).

The CLI **does not import Django**, does not modify `urls.py`, does not know
your `settings.py`. It only spawns `<your-python> <your-manage.py> <command>`
as subprocesses and multiplexes their logs.

## Install

```bash
pipx install run-site
# or
uv tool install run-site
```

Requirements: Python 3.11+, Docker daemon running, `git` (only if you use
`--from-git`).

## Quickstart

From your Django project root, generate a config — it auto-detects
`manage.py`, your Django project module, Celery, and `uv`:

```bash
run-site init
```

That writes `runsite.toml` with sensible defaults; for typical projects
no edits are needed. When `uv` is on `PATH` it pins the Python
invocation to `uv run --no-sync python` so deps come from
`pyproject.toml` / `uv.lock` automatically.

Then run:

```bash
run-site run
```

You get migrate, an `admin / admin` superuser, `runserver` listening on
a random free port, browser opened on the homepage, container logs
streaming in your terminal — and the banner below.

## What `run-site run` shows you

```
════════════════════════════════════════
  run-site is running
════════════════════════════════════════

  Project:  myproj
  Root:     /Users/me/code/myproj

  App:       http://localhost:54812/
  Admin:     http://localhost:54812/admin/
  Superuser: admin / admin  (created)
             email=admin@example.com
  Postgres:  127.0.0.1:54321
             db=myproj  user=myproj  password=password
             psql: PGPASSWORD=password psql -h 127.0.0.1 -p 54321 -U myproj -d myproj
             env:  PGHOST=127.0.0.1 PGPORT=54321 PGDATABASE=myproj PGUSER=myproj PGPASSWORD=password
  Redis:     127.0.0.1:16379
  Lifecycle: Postgres + Redis will be removed on exit.
             Pass --reuse to keep them between runs (faster restart, dump preserved).
  Celery:    disabled
             [tip] enable Celery in runsite.toml:
                     [celery]
                     enabled = true
                     app = "<your_django_module>.celery"
                   then re-run (use --no-celery to skip per-run).
  Sidecar:   /Users/me/code/myproj/.run-site-config (removed on shutdown)

════════════════════════════════════════
```

Notable touches:

- **`Superuser:`** tells you `(created)` on a fresh DB, `(existing —
  password reset to dev default)` when the user pre-existed and
  `[superuser].overwrite = true` (the default), or `(existing — password
  unchanged)` when overwrite is off — and only displays the password in
  states where it's actually what's in the DB right now.
- **`psql:`** is a copy-paste-ready command line (passwords with shell
  meta-chars get `shlex.quote`d).
- **`env:`** is the libpq variable line — paste once into your shell and
  every later `psql` / `pg_dump` against the dev DB just works.
- **`Lifecycle:`** is the explicit `--reuse` / no-`--reuse` indicator;
  with `--reuse` it tells you the exact `docker rm -f
  <slug>-runsite-{pg,redis}` to clean up later.
- The `Celery` enable hint only shows when celery is **disabled in the
  config** — running with `--no-celery` against an enabled config doesn't
  trigger it (that's a deliberate per-run override).
- All secrets get hidden behind a single `[banner].show_db_credentials =
  false` switch.

## The `.run-site-config` sidecar

Every `run` writes a TOML file to `<project_root>/.run-site-config`
**before** the runserver starts and **removes it** on shutdown:

```toml
project_slug = "myproj"
generated_at = "2026-05-07T13:42:11+00:00"

[web]
host = "localhost"
port = 54812
url = "http://localhost:54812/"

[postgres]
host = "127.0.0.1"
port = 54321
db = "myproj"
user = "myproj"
password = "password"
url = "postgres://myproj:password@127.0.0.1:54321/myproj"

[redis]
host = "127.0.0.1"
port = 16379
db = 0
url = "redis://127.0.0.1:16379/0"

[celery]
enabled = true
app = "myproj.celery"
```

Use cases: tooling that wants the live ports without parsing logs,
`django-dev-helpers` reading them at app load, scripts you write that
run alongside the dev server. Add `.run-site-config` to your
`.gitignore` — it's regenerated per-run.

### Run from a Git URL

No clone, no venv, no `uv sync` to do by hand. The "uvx mode" — try a
project without even installing run-site first:

```bash
uv tool run run-site run --from-git git@github.com:mpasternak/django-multiseek.git --yes
```

`--yes` skips the cloning-confirmation prompt, making the command
copy-paste-safe in tutorials and CI. The same pattern works against any
public or SSH-accessible repo:

```bash
run-site run --from-git https://github.com/iplweb/bpp.git --branch main
```

The CLI clones the repo to `~/.cache/run-site/checkouts/<slug>/`,
creates a venv, installs deps (auto-detecting `uv.lock` /
`pyproject.toml` / `requirements.txt`), then runs as usual. Reuse with
`--no-pull --no-install`. See [docs/from-git.md](docs/from-git.md).

### Run from any local checkout

```bash
run-site run --from-path ~/Programowanie/some-django-app
```

No need to `cd` first.

### Reuse containers between runs

```bash
run-site run --reuse
```

Stable container names — `<project_slug>-runsite-pg` and `-redis` — survive
between runs so you don't reload the dump each time. The banner's
`Lifecycle:` line tells you which mode you're in and how to clean up.

## What's in the box

| File / module | Role |
|---|---|
| `cli.py` | Argparse entrypoint, two-pass parsing, `run` / `doctor` / `init` dispatch. |
| `init_cmd.py` | `run-site init` — generates `runsite.toml` from project layout. |
| `config.py` | `runsite.toml` / `pyproject.toml` loader + validator. |
| `discovery.py` | Project root, `manage.py`, local Python resolution chain. |
| `containers.py` | testcontainers PG + Redis, named/reuse, Ryuk policy. |
| `dumps.py` | Format detection, init-script vs. post-start strategy. |
| `env.py` | Build env for subprocesses + the `DEV_HELPERS_*` contract. |
| `sidecar.py` | Write/remove the `.run-site-config` runtime file. |
| `processes.py` | Spawn, terminate, HTTP probe. |
| `log_multiplexer.py` | Colored prefixes per stream. |
| `hooks.py` | Command / Django hook execution. |
| `superuser.py` | `manage.py shell -c` with `get_user_model()`. |
| `banner.py` | Orchestrator banner with URLs, credentials, helpers. |
| `source/from_git.py` | Clone/pull, slug extraction, ownership policy. |
| `source/venv_setup.py` | `uv venv` / `python -m venv`. |
| `source/deps_installer.py` | `uv sync` / `pip install -r`. |

## Companion package — `django-dev-helpers`

The features that live *inside* Django (autologin endpoint, dotfile
generation, agent help banner) are intentionally split into a separate
package, [`django-dev-helpers`](https://github.com/iplweb/django-dev-helpers).
You install it in your Django project and the two communicate via a
documented `DEV_HELPERS_*` env-var contract — neither imports the other.
The `.run-site-config` sidecar gives `django-dev-helpers` a second,
file-based path to the same data.

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
  used by integration tests; runs end-to-end with `run-site run`.

## Status

v0.3 is the current release. CLI flags and config schema may still
evolve before 1.0 — see [CHANGELOG.md](CHANGELOG.md) for what's changed.

## License

MIT — see [LICENSE](LICENSE).
