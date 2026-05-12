# Quickstart

Get a local Django dev stack — Postgres, Redis, runserver, optional
Celery — running in one command.

## Install

```bash
pipx install django-run-site
# or
uv tool install django-run-site
```

The distribution on PyPI is `django-run-site`; the installed CLI command
is `run-site`.

You can also try run-site against any public repo without installing it
first:

```bash
uv tool run --from django-run-site run-site run --from-git URL --yes
```

See [from-git.md](from-git.md) for the full story.

You'll also need:

- **Docker** running locally (Docker Desktop, colima, podman with the docker
  socket exposed). The CLI talks to the daemon via the standard
  `DOCKER_HOST` env var. *Skip this if your project doesn't use Postgres
  or Redis — the default `enabled = "auto"` will detect that from your
  settings and won't try to start either container. See the [SQLite
  example](../examples/runsite.sqlite.toml).*
- **Python 3.11+** (the CLI itself; your project can use any Python the
  orchestrator can find — see [configuration](configuration.md#python)).

## 1. Generate `runsite.toml`

The fastest path is `run-site init`, which detects `manage.py`, your Django
project module, Celery, and `uv`, and writes a working config you can use
as-is:

```bash
run-site init
```

If you prefer to author it by hand, the minimal shape is:

```toml
project_slug = "myproject"
manage_py = "manage.py"

[python]
# When uv is installed `run-site init` will write this instead:
# command = ["uv", "run", "--no-sync", "python"]
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

## 2. Add an `[env]` mapping (or use `DATABASE_URL` directly)

The orchestrator passes connection info to your Django process via env
vars. The simplest case is `DATABASE_URL` + `REDIS_URL`:

```toml
[env]
database_url = "DATABASE_URL"
redis_url = "REDIS_URL"
```

Then in your `settings.py`:

```python
import os
import dj_database_url

DATABASES = {"default": dj_database_url.parse(os.environ["DATABASE_URL"])}
CACHES = {"default": {
    "BACKEND": "django.core.cache.backends.redis.RedisCache",
    "LOCATION": os.environ["REDIS_URL"],
}}
```

If your project already reads `DJANGO_DB_HOST` / `DJANGO_DB_PORT` / etc.,
map those names instead — see [configuration](configuration.md#env).

## 3. Run

```bash
run-site run
```

That will:

1. Spin up Postgres + Redis in containers on **free ports** (so multiple
   sites can run side-by-side with no port collisions).
2. Run `manage.py migrate --noinput`.
3. Create or update the `admin/admin` superuser.
4. Drop a `.run-site-config` TOML at the project root with the live
   ports + connection URLs (read by `django-dev-helpers` and any other
   tooling). Removed on clean shutdown.
5. Start `runserver` on a free port.
6. Print the **banner** with admin URL, copy-paste `psql` command, libpq
   env-var line, dev superuser credentials, container lifecycle info,
   and the sidecar path.
7. Multiplex container + runserver logs into your terminal.
8. Open your browser on the homepage.

Press **Ctrl-C** to shut everything down. The CLI stops the containers
and removes `.run-site-config` unless you passed `--reuse` (which keeps
the containers; the sidecar is still removed because it's per-run).

## 4. Reuse containers between runs

When you don't want to re-load the dump or wait for cold-start:

```bash
run-site run --reuse
```

Containers get stable names — `<project_slug>-runsite-pg` and
`-redis` — so subsequent `--reuse` runs attach to them instead of creating
fresh ones.

## 5. Sanity-check before running

```bash
run-site doctor
```

Doctor verifies:

- Config loads and is valid.
- `manage.py --help` works in your project's Python.
- Docker daemon is reachable.
- `git` and `uv` are on PATH (only relevant for `--from-git`).

## What's next

- [Configuration reference](configuration.md) — every TOML key explained.
- [Run from Git or path](from-git.md) — for orchestrating projects that
  aren't your CWD.
- [Hooks](hooks.md) — wire in pre-flight commands and Django callables.
- [`django-dev-helpers`](with-django-dev-helpers.md) — autologin and
  dotfile generation inside Django.
