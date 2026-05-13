# django-run-site

> **[`uvx`](https://docs.astral.sh/uv/guides/tools/)-wannabe for
> full-fledged\* Django sites.**
>
> Run several Django sites side-by-side — one per worktree, branch, or
> checkout — each with its own PostgreSQL and Redis on
> automatically-picked, non-conflicting ports. The current stack's
> ports + connection URLs are written to `.run-site-config` (a TOML
> dotfile at the project root) so both **you** and your **coding
> agent** can talk to the right services without fighting over `5432`
> and `6379` or parsing logs to find them.
>
> Primary use cases: test automation across branches, multi-agent
> coding workflows, comparing two versions of a site at once. As a
> bonus, the same engine pulls projects straight from a Git URL with
> no manual `git clone` / venv / `uv sync` — see
> [Run from a Git URL](#run-from-a-git-url) below.
>
> \* *Full-fledged* = a Django project that expects PostgreSQL, Redis,
> and some kind of seed dump for the database — not a one-file
> `manage.py runserver` toy. **This is the goal we're aiming at; we're
> not fully there yet** — current focus is `--from-git` /
> `--from-path` ergonomics, dump-restore strategies, the runtime
> banner, and the `.run-site-config` sidecar. See
> [CHANGELOG.md](CHANGELOG.md) for what's shipping and the
> [Status](#status) section for current rough edges.

Pure CLI orchestrator for local Django development. PostgreSQL & Redis
testcontainers + dump load + local `runserver`/Celery + log multiplexer +
hooks — all in one command. **Zero Django dependency in the CLI itself.**

[![PyPI version](https://img.shields.io/pypi/v/django-run-site.svg)](https://pypi.org/project/django-run-site/)
[![Python](https://img.shields.io/pypi/pyversions/django-run-site.svg)](https://pypi.org/project/django-run-site/)
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

## Architecture at a glance

Postgres and Redis run as **testcontainers** with their host ports
auto-assigned to free ephemeral slots. Django and Celery run **locally**
(no container, native subprocesses of run-site). The web port follows the
same rule as the container ports — the configured default if free,
otherwise a random free port. Every process inherits the live URLs from
run-site as environment variables, so nothing in the stack hard-codes a
port and parallel checkouts of the same project don't collide.

```
                     HOST MACHINE — everything on 127.0.0.1 (loopback)

 ┌──────────────────  Local processes (spawned by run-site, native)  ──────────────────┐
 │                                                                                      │
 │    manage.py runserver        celery -A <app> worker        celery -A <app> beat     │
 │    ─────────────────────      ──────────────────────        ─────────────────────    │
 │    binds :<web_port>          no listening port             no listening port        │
 │      default 8000,            (broker / DB client)          (broker / DB client)     │
 │      else random free                                                                 │
 │                                                                                       │
 │    All three inherit env from run-site:                                               │
 │      DATABASE_URL      = postgres://…@127.0.0.1:<pg_port>/…                           │
 │      REDIS_URL         = redis://127.0.0.1:<redis_port>/0                             │
 │      CELERY_BROKER_URL = redis://127.0.0.1:<redis_port>/0                             │
 │                                                                                       │
 └────────────────────────┬────────────────────────────────┬─────────────────────────────┘
                          │ TCP                            │ TCP
                          ▼                                ▼
             ┌──────────────────────────┐     ┌──────────────────────────┐
             │   Docker container       │     │   Docker container       │
             │   ─────────────────      │     │   ─────────────────      │
             │   PostgreSQL :5432       │     │   Redis :6379            │
             │       (inside)           │     │       (inside)           │
             │            │             │     │            │             │
             │            ▼             │     │            ▼             │
             │   host: 127.0.0.1:<pg>   │     │   host: 127.0.0.1:<rds>  │
             │   random ephemeral port  │     │   random ephemeral port  │
             │   (e.g. 49162)           │     │   (e.g. 49163)           │
             └──────────────────────────┘     └──────────────────────────┘
                   (testcontainers)                 (testcontainers)
```

Port allocation flow:

1. run-site asks Docker to publish container ports with `-p 0:5432` and
   `-p 0:6379` (`0` = "any free host port"). Docker assigns ephemeral
   ports; run-site reads them back.
2. run-site picks a free port for `runserver` — the configured default
   if available, else a random free port.
3. run-site exports `DATABASE_URL` / `REDIS_URL` / `CELERY_BROKER_URL`
   into the child env and writes the same values to `.run-site-config`.
4. Django, Celery worker, and Celery beat all start as native processes
   and connect over loopback. None of them know or care which port
   number ended up being used.

## Install

```bash
pipx install django-run-site
# or
uv tool install django-run-site
```

The PyPI distribution is `django-run-site`; the installed CLI command is
`run-site` (also the import name `run_site`).

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
uv tool run --from django-run-site run-site run --from-git git@github.com:mpasternak/django-multiseek.git --yes
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

## Common recipes

### Restore a PostgreSQL dump before the site starts

Dump restoration is a **first-class feature** — not a hook. Point
`[dump]` at any `.sql`, `.sql.gz`, `.dump`, or `.pgdump` file and
`run-site` picks the right loader strategy automatically:

```toml
[dump]
default_path = "fixtures/baseline.sql"   # relative to project root
strategy = "auto"                        # init-script for fresh PG, post-start otherwise
restore_jobs = "auto"                    # parallelism for pg_restore (auto = min(8, cpu_count))
fail_fast = true
```

| Strategy | When to use it |
|---|---|
| `auto` (default) | Plain `.sql` + fresh container → init-script. Otherwise → post-start. Reused container → skipped (existing data preserved). |
| `init-script` | Force PG to load the dump from `/docker-entrypoint-initdb.d/`. Only works for `.sql` on freshly-created containers. |
| `post-start` | Always restore via `psql` / `pg_restore` after PG is up. Handles every supported format. |

Override per-run from the CLI:

```bash
run-site run --from-dump fixtures/2026-05-07.sql.gz
run-site run --no-dump                    # skip the restore for this run
run-site run --dump-strategy=post-start   # force post-start, even on a reused container (nukes data)
```

The full reference lives in [docs/configuration.md#dump](docs/configuration.md#dump).

### Exposing the dev server on your LAN

By default `runserver` binds to `127.0.0.1` — only the host machine can
reach it. To open the dev server to phones / tablets / other laptops on
the same network:

```bash
run-site run --bind 0.0.0.0
```

Two things then happen automatically:

1. **The banner lists every reachable URL.** `run-site` discovers your
   machine's mDNS hostname and primary LAN IP and prints a clickable
   `http://<host>:<port>/` for each one alongside the loopback URL —
   no need to look up your IP with `ifconfig`.
2. **`ALLOWED_HOSTS` is wired up for you.** Without this, Django would
   reject every non-loopback request with `DisallowedHost`. `run-site`
   exports the discovered hosts under two env-var names:
   - `DEV_HELPERS_ALLOWED_HOSTS` — consumed by `django-dev-helpers`
     (>= 0.1.11), which unions the entries into `settings.ALLOWED_HOSTS`
     at app ready. Works with **any** project that has the helper in
     `INSTALLED_APPS`, even if its settings hard-code `ALLOWED_HOSTS`.
   - `DJANGO_ALLOWED_HOSTS` — the conventional name picked up by
     projects that read it themselves (e.g.
     `ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS", default=[...])`).

   The list always contains `localhost`, `127.0.0.1`, `[::1]` plus the
   discovered LAN entries — never `*`. For a loopback-only bind both
   exports are skipped (your project's own `ALLOWED_HOSTS` already
   covers that case).

   Rename the conventional export per project with `[env].allowed_hosts`,
   or set it to `null` to suppress entirely (the `DEV_HELPERS_*`
   contract still fires for the helper):

   ```toml
   [env]
   allowed_hosts = "MY_HOSTS"   # rename
   # or
   allowed_hosts = null         # only export DEV_HELPERS_ALLOWED_HOSTS
   ```

Want this on by default for every project? Drop one line in your shell
profile:

```bash
export RUN_SITE_BIND=0.0.0.0
```

Now `run-site manage.py` (no flag) exposes to the LAN. An explicit
`--bind` on the command line still wins.

### Lifecycle hooks — pre/post each stage

Hooks let you wedge custom logic into the orchestrator's flow. Two
flavors: `type = "command"` (regular subprocess) and `type = "django"`
(through `manage.py shell -c`, with a `ctx` dict containing the live
ports and credentials).

The available stages, in order:

```
pre_containers → post_containers → pre_dump → post_dump → post_migrate
                                                              ↓
                                                       post_superuser → pre_serve
                                                                            ↓
                                                                       (runserver runs)
                                                                            ↓
                                                                        post_stop
```

Note: there is **no** `pre_migrate` stage — use `post_dump` (it runs
right before migrate). And there is **no** `post_serve` stage —
`runserver` blocks until shutdown, so the closest is `post_stop`
(best-effort cleanup; errors get logged, not fatal).

#### `pre_containers` — build assets before anything starts

```toml
[[hooks.pre_containers]]
type = "command"
command = ["make", "assets"]
timeout = 300
cli_disable_flag = "--skip-assets"   # `run-site run --skip-assets` skips this run
```

#### `post_dump` — patch the freshly-loaded baseline

Right after the dump loads, before `migrate`:

```toml
[[hooks.post_dump]]
type = "django"
callable = "myproject.runsite_hooks:rotate_dev_secrets"
timeout = 30
```

```python
# myproject/runsite_hooks.py
def rotate_dev_secrets(ctx: dict) -> None:
    """Replace any production-looking secrets the dump may have shipped
    with safe dev placeholders. Runs once per restore."""
    from django.contrib.auth import get_user_model
    User = get_user_model()
    User.objects.filter(is_superuser=True).update(
        password="!unusable",  # force re-login through the dev superuser flow
    )
```

#### `post_migrate` — load fixtures or run management commands

```toml
[[hooks.post_migrate]]
type = "django"
callable = "myproject.runsite_hooks:load_dev_fixtures"
```

```python
def load_dev_fixtures(ctx: dict) -> None:
    from django.core.management import call_command
    call_command("loaddata", "fixtures/dev_seed.json", verbosity=0)
```

#### `post_superuser` — clean up auth quirks

```toml
[[hooks.post_superuser]]
type = "django"
callable = "myproject.runsite_hooks:clear_password_policy"
```

```python
def clear_password_policy(ctx: dict) -> None:
    from password_policies.models import PasswordChangeRequired
    PasswordChangeRequired.objects.filter(
        user__username=ctx["superuser"]["username"]
    ).delete()
```

#### `pre_serve` — last call before runserver

The `.run-site-config` sidecar is already on disk by this stage, so a
hook can read it.

```toml
[[hooks.pre_serve]]
type = "django"
callable = "myproject.runsite_hooks:warm_caches"
timeout = 60
```

```python
def warm_caches(ctx: dict) -> None:
    """Pre-fill the homepage cache so the first request is fast."""
    from django.test import Client
    Client().get("/")
```

#### Custom CLI flag for a hook

Add `[[hooks.<stage>.cli_args]]` to register a flag dynamically — the
parser is rebuilt after config load so `--help` shows it:

```toml
[[hooks.post_migrate]]
type = "django"
callable = "myproject.runsite_hooks:fetch_token"
timeout = 60

[[hooks.post_migrate.cli_args]]
flag = "--get-token-from"
dest = "ssh_source"
metavar = "USER@HOST"
help = "Pull a deploy token from this SSH host after migrations"
```

```bash
run-site run --get-token-from admin@bpp-prod
```

```python
def fetch_token(ctx: dict) -> None:
    source = ctx["opts"].get("ssh_source")
    if not source:
        return  # flag not passed — no-op
    # … scp / ssh whatever you need …
```

#### `post_stop` — best-effort cleanup

```toml
[[hooks.post_stop]]
type = "command"
command = ["bash", "-lc", "rm -rf .runtime-cache/"]
```

Errors here are **logged, not fatal** — `post_stop` shouldn't be able
to break a clean shutdown.

Full reference + the `ctx` dict schema: [docs/hooks.md](docs/hooks.md).
A real-world hook setup: [examples/runsite.bpp.toml](examples/runsite.bpp.toml).

## Environment variables

A small set of `RUN_SITE_*` env vars provide shell-profile defaults so
you don't have to remember CLI flags or edit `runsite.toml` for
preferences that follow you across projects.

| Variable | Effect |
|---|---|
| `RUN_SITE_BIND` | Default for `--bind`. Set to `0.0.0.0` to expose the dev server to the LAN by default. CLI `--bind` overrides. |
| `RUN_SITE_PYTHON` | Path to a Python interpreter used to execute `manage.py`. Highest-priority entry in the auto-discovery chain (see [`discovery.py`](src/run_site/discovery.py) for the full ordering). |

`run-site` itself also *exports* a number of variables to subprocesses:

- The `DEV_HELPERS_*` contract (DB host/port, Redis host/port, autologin
  token, project root, port, `ALLOWED_HOSTS`) — consumed by
  `django-dev-helpers`. Stable, never renameable.
- Conventional names like `DATABASE_URL`, `REDIS_URL`,
  `DJANGO_SECRET_KEY`, `DJANGO_ALLOWED_HOSTS` — defaults set so
  `env.db_url(...)`-style settings work with zero config. Rename or
  suppress per project via `[env]` mapping. See
  [docs/configuration.md](docs/configuration.md) for the full table.

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
