# Quickstart

Get a local Django dev stack — Postgres, Redis, runserver, optional
Celery — running in one command.

## Install

```bash
pipx install django-run-site
# or
uv tool install django-run-site
```

You'll also need:

- **Docker** running locally (Docker Desktop, colima, podman with the docker
  socket exposed). The CLI talks to the daemon via the standard
  `DOCKER_HOST` env var.
- **Python 3.11+** (the CLI itself; your project can use any Python the
  orchestrator can find — see [configuration](configuration.md#python)).

## 1. Drop a `runsite.toml` in your repo

```toml
project_slug = "myproject"
manage_py = "manage.py"

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
django-run-site run
```

That will:

1. Spin up Postgres + Redis in containers on free ports.
2. Run `manage.py migrate --noinput`.
3. Create or update the `admin/admin` superuser.
4. Start `runserver` on a free port.
5. Multiplex container + runserver logs into your terminal.
6. Open your browser on the homepage.

Press **Ctrl-C** to shut everything down. The CLI stops the containers
unless you passed `--reuse`.

## 4. Reuse containers between runs

When you don't want to re-load the dump or wait for cold-start:

```bash
django-run-site run --reuse
```

Containers get stable names — `<project_slug>-runsite-pg` and
`-redis` — so subsequent `--reuse` runs attach to them instead of creating
fresh ones.

## 5. Sanity-check before running

```bash
django-run-site doctor
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
