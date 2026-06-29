# Configuration reference

`run-site` reads configuration from one of two places, in order:

1. `runsite.toml` in your project root (preferred — clearer for non-trivial
   configs).
2. `[tool.run-site]` in `pyproject.toml` (handy when you don't want
   another file).

CLI flags always override the config.

## Top-level keys

```toml
project_slug = "myproject"   # optional; auto-derived from the project root
manage_py = "src/manage.py"  # path to manage.py, relative to project_root
```

`project_slug` is used as the Postgres/Redis container name prefix when
`--reuse` is set. If you don't set it, `run-site` derives one from the
project root directory name (sanitized to `[A-Za-z0-9_.-]+`, falling back
to `runsite` if the name has no usable characters).

### Running without a config file

`runsite.toml` (and `[tool.run-site]` in `pyproject.toml`) are optional.
With no config at all, `run-site run` uses the documented defaults for
every section — Postgres + Redis testcontainers, `manage.py` auto-detected
from the project root, `runserver` on `127.0.0.1`, an `admin / admin`
superuser, and the slug derived from the directory name. It's a viable
starting point; add a config only when you need to override something.

## `[python]`

How to invoke the project's Python interpreter.

```toml
[python]
executable = "auto"  # default; runs the resolution chain
# or
executable = ".venv/bin/python"
# or — the form `run-site init` writes when uv is on PATH:
command = ["uv", "run", "--no-sync", "python"]
```

`executable` and `command` are mutually exclusive (except `executable = "auto"`,
which means "ignore me, use the chain"). With `command`, the CLI prepends
those tokens to every Python invocation — useful when you'd rather use
`uv run` than activate a venv.

`--no-sync` matters: run-site already runs `uv sync` once during venv
setup, so re-syncing on every invocation just slows things down.

The `"auto"` chain (in order):

1. `RUN_SITE_PYTHON` env var.
2. `<project_root>/.venv/bin/python` (preferred over `$VIRTUAL_ENV` so
   `uv tool run --from django-run-site run-site` doesn't pick the
   run-site tool's own venv).
3. `$VIRTUAL_ENV/bin/python` (ambient activated venv).
4. `uv run python` (only when `uv.lock` exists and `uv` is on PATH).
5. `sys.executable`.

Symlinks in `.venv/bin/python` are **not** resolved — Python detects venv
membership from the invocation path, and uv creates `bin/python` as a
symlink to the upstream interpreter. Resolving it would land outside the
venv and lose `site-packages`.

## `[postgres]`

```toml
[postgres]
enabled = "auto"         # true | false | "auto" (default "auto", see below)
image = "postgres:16"
user = "myproject"
password = "password"
db = "myproject"
driver = ""              # URL prefix suffix: "", "+psycopg2", "ql", …
stream_logs = true       # prefix PG container logs as `pg | …`

[postgres.env]
POSTGRESQL_UNSAFE_BUT_FAST = "1"
```

With `enabled = false` (or the equivalent `--no-postgres` CLI flag),
`run-site` does **not** pull the image or start the container, does **not**
emit `DEV_HELPERS_DB_*` env vars, does **not** map `database_url` /
`db_*` into your project's `[env]` vars, and omits the `[postgres]`
section from the runtime sidecar. Use this when your project is happy
with SQLite or connects to an external database — your `settings.py`
stays in charge of `DATABASES['default']`.

`enabled = "auto"` is the default: `run-site` statically scans your
project's settings module on each run, resolves `enabled = true` when it
sees `django.db.backends.postgresql` (or `postgres://` / `postgresql://`
URL strings), and `false` otherwise. Set `enabled = true` explicitly if
you want Postgres started unconditionally (e.g. for a fixture import that
doesn't yet appear in settings).

`driver` controls the `postgres<driver>://` scheme of `DATABASE_URL`:

| Value | Resulting URL |
|---|---|
| `""` (default) | `postgres://…` |
| `"+psycopg2"` | `postgres+psycopg2://…` |
| `"ql"` | `postgresql://…` |

`[postgres.env]` is forwarded to the container — useful for custom images
that take tuning knobs via env (e.g. BPP's `iplweb/bpp_dbserver`).

## `[redis]`

```toml
[redis]
enabled = "auto"          # true | false | "auto" (default "auto")
image = "redis:7-alpine"
db = 0
```

`enabled = false` (or `--no-redis`) follows the same contract as for
Postgres: no container, no `DEV_HELPERS_REDIS_*` vars, no `redis_url` /
`redis_host` / `redis_port` mapping, no `[redis]` block in the sidecar.

`enabled = "auto"` (the default) scans your settings for Redis usage —
Redis cache backends (`django_redis`,
`django.core.cache.backends.redis.RedisCache`), `redis://` / `rediss://`
URLs, or a `CELERY_BROKER_URL` / `CELERY_RESULT_BACKEND` reference all
count as evidence.

If neither Postgres nor Redis ends up enabled (and SQLite mode is on),
`run-site` skips the Docker availability check too — useful for laptops
where Docker isn't running.

## `[sqlite]`

Managed SQLite mode. When enabled, `run-site` picks a path, creates the
file, and exposes it to your project via the standard `database_url`
`[env]` mapping (as a `sqlite:///<abspath>` URL).

```toml
[sqlite]
enabled = "auto"   # true | false | "auto" (default "auto")
path = ""          # optional: explicit path for the persistent (--reuse) file
```

Lifecycle mirrors the Postgres container:

| Mode | Path | Lifecycle |
|---|---|---|
| no `--reuse` | `<tempdir>/runsite-<slug>-…/db.sqlite3` | created fresh; **deleted on exit** |
| `--reuse` | `<project_root>/.run-site/<slug>.sqlite3` (or `path` override) | created if missing; **never deleted** |

Combine with `--force-reset` to wipe the persistent file before starting.

`enabled = "auto"` (the default) only enables SQLite mode when settings
explicitly reference SQLite (`django.db.backends.sqlite3` or
`sqlite:///`) **and** Postgres did not also auto-enable from the same
scan. Existing SQLite users whose `settings.py` hardcodes a path
(`BASE_DIR / "db.sqlite3"`) and doesn't read `DATABASE_URL` are
unaffected: the env var is ignored and the historic file keeps being
used.

Explicit `[postgres].enabled = true` together with `[sqlite].enabled =
true` is a config error — pick one database backend per config.

**`.gitignore` warning:** when run-site creates the persistent
`.run-site/` directory it checks your `.gitignore`; if `.run-site/` (or
`.run-site`) is not listed, you'll see a warning at startup. Add it so
the local DB file doesn't end up in commits.

## `[containers]`

```toml
[containers]
ryuk = "auto"   # "auto" | true | false
```

Controls testcontainers' Ryuk reaper. `auto` enables Ryuk for fresh runs
and disables it with `--reuse` (named containers shouldn't be auto-killed
between runs).

## `[dump]`

```toml
[dump]
default_path = "fixtures/baseline.sql"
strategy = "auto"        # "auto" | "init-script" | "post-start"
restore_jobs = "auto"    # int or "auto"
fail_fast = true
fix_search_path = false  # rewrite empty search_path -> public during restore
```

| Strategy | When to use |
|---|---|
| `auto` (default) | Plain `.sql` + fresh container → init-script; otherwise → post-start; reused container → skip with warning. |
| `init-script` | Force PG to load the dump from `/docker-entrypoint-initdb.d/`. Only `.sql`, only fresh containers. |
| `post-start` | Always restore via `psql` / `pg_restore` after PG comes up. Works for `.sql`, `.sql.gz`, `.dump`/`.pgdump`, and `pg_dump` directory dumps packaged as `.tar.gz`/`.tgz`. |

`restore_jobs = "auto"` becomes `min(8, os.cpu_count())`.

### `fix_search_path`

`bool`, default `false`. When `true`, the dump is streamed through `sed`
during restore, applying exactly:

```
s/set_config('search_path', '', false)/set_config('search_path', 'public', false)/
```

Modern `pg_dump` hardens its header (post CVE-2018-1058) by setting an
empty `search_path` for the whole restore session. That breaks restoring
objects whose definitions resolve `public` operators/types eagerly — most
commonly an `hstore` comparison in a trigger `WHEN` clause, which fails
with `operator does not exist: public.hstore = public.hstore` on PG 16+.
Restoring `public` to the path fixes it (safe, because `pg_dump`
schema-qualifies every object). The on-disk dump file is never modified —
the filter is always streamed.

Coverage: applies to plain `.sql` (both `post-start` `psql` and
`init-script`, where a filtered temp copy is mounted), gzipped `.sql.gz`
(`gunzip | sed | psql`), and binary archives. For binary archives the
filter forces `pg_restore -f - | sed | psql`, which **disables parallel
`-j` restore** (a text filter cannot be applied to a parallel binary
restore). If the flag is on but the dump header lacks the empty-search_path
line, run-site logs a warning that the fix is a no-op.

CLI override: `--fix-search-path` / `--no-fix-search-path`.

### Restore progress bar

If [`pv`](https://www.ivarch.com/programs/pv.shtml) is installed and
run-site's output is an interactive terminal, post-start restores of plain
`.sql` and gzipped `.sql.gz` dumps show a live `pv` progress bar (the dump
is streamed `pv file | … | psql`). It is automatic — no configuration — and
silently absent when `pv` is missing or output is non-interactive (CI,
piped, headless). Binary `pg_restore` archives and `init-script` restores
do not show a bar (no host-side stream to measure).

## `[env]`

Two flavors of env passing:

### Project-side mapping

Map values the orchestrator computes (PG host/port, Redis URL, …) to the
env-var **names your project's `settings.py` already reads**:

```toml
[env]
database_url = "DATABASE_URL"
db_host = "DJANGO_BPP_DB_HOST"
db_port = "DJANGO_BPP_DB_PORT"
db_name = "DJANGO_BPP_DB_NAME"
db_user = "DJANGO_BPP_DB_USER"
db_password = "DJANGO_BPP_DB_PASSWORD"
redis_url = "DJANGO_BPP_REDIS_URL"
redis_host = "DJANGO_BPP_REDIS_HOST"
redis_port = "DJANGO_BPP_REDIS_PORT"
```

Set the value to `null` to skip that key. Unknown keys fail validation.

### Project extras

Arbitrary env vars set verbatim:

```toml
[env.extra]
DJANGO_BPP_SKIP_DOTENV = "1"
```

Note: the CLI **always** sets the `DEV_HELPERS_*` contract vars in
addition to your `[env]` mapping (see
[with-django-dev-helpers.md](with-django-dev-helpers.md)). They don't need
to appear in your config.

## `[django]`

```toml
[django]
runserver_bind = "127.0.0.1"
runserver_display_host = "localhost"
browser_probe_path = "/admin/login/"
migrate = true
probe_timeout = 60.0
# Optional: replace the web process. Default = manage.py runserver.
# web_command = ["{python}", "-m", "daphne", "-b", "{bind}", "-p", "{port}", "myproject.asgi:application"]
```

`runserver_display_host` differs from `runserver_bind` so URLs stay clean:
defaulting to `localhost` avoids Safari's HSTS cache being primed by IP
literals.

### Overriding the web process

When `runserver` isn't enough — Django Channels needs ASGI, you want to
test under gunicorn, etc. — set `web_command`. Tokens go through the
same template substitution as `[[extra_processes]].command`:

| Variable | Value |
|---|---|
| `{python}` | resolved Python command (multi-token-aware: `["uv","run","python"]` expands inline) |
| `{manage_py}` | absolute manage.py path |
| `{manage_dir}` | manage.py's directory |
| `{project_root}` | project root |
| `{port}` | runserver port (the same free port the orchestrator picked) |
| `{bind}` | `runserver_bind` value (`127.0.0.1` by default) |

```toml
# Daphne (ASGI):
[django]
web_command = [
    "{python}", "-m", "daphne",
    "-b", "{bind}", "-p", "{port}",
    "myproject.asgi:application",
]

# Uvicorn (ASGI, with autoreload):
[django]
web_command = [
    "{python}", "-m", "uvicorn", "myproject.asgi:application",
    "--host", "{bind}", "--port", "{port}",
    "--reload",
]

# Gunicorn (sync WSGI):
[django]
web_command = [
    "{python}", "-m", "gunicorn", "myproject.wsgi",
    "-b", "{bind}:{port}", "--reload",
]
```

Tradeoffs: you lose `runserver`'s autoreload (each replacement has its
own; daphne has none, uvicorn needs `--reload`, gunicorn needs `--reload`).
The browser probe + auto-open still target `http://<display_host>:<port>/`,
so the banner's `App:` URL stays meaningful.

## `[superuser]`

```toml
[superuser]
enabled = true
username = "admin"
password = "admin"
email = "admin@example.com"
overwrite = true
```

The CLI runs `manage.py shell -c` with `get_user_model()` — works on every
project regardless of `INSTALLED_APPS` or custom user models. With
`overwrite = false`, an existing user is left alone.

## `[celery]`

```toml
[celery]
app = "myproject.celery"
enabled = true
worker_pool = "solo"
worker_log_level = "info"
worker_extra_args = []
with_beat = false
beat_log_level = "info"
beat_extra_args = []
```

`worker_pool = "solo"` is the safest default on macOS — `prefork` regularly
breaks with `psycopg`/`numpy`/`lxml` due to fork issues.

## `[[extra_processes]]`

Spawn anything alongside `runserver`:

```toml
[[extra_processes]]
name = "frontend"
command = ["npm", "run", "dev"]
cwd = "."
enabled_default = false
color = "blue"
cli_flag = "--with-frontend"
cli_disable_flag = "--no-frontend"
```

Template substitutions in `command`:

| Variable | Value |
|---|---|
| `{python}` | resolved local python (multi-token if `command` is set) |
| `{manage_py}` | absolute manage.py path |
| `{manage_dir}` | manage.py's directory |
| `{project_root}` | project root |
| `{port}` | runserver port |

Reserved names (collision = error): `web`, `pg`, `redis`, `celery`,
`celery-beat`.

## `[[hooks.<stage>]]`

See [hooks.md](hooks.md).

## `[banner]`

```toml
[banner]
title = "run-site is running"
show_db_credentials = true
suggest_dev_helpers = true
```

The banner shows, in order: project + root, source (if `--from-git` /
`--from-path`), `App:` / `Admin:` URLs, `Superuser:` (with credentials
when known), `Postgres:` (host:port + db/user/password + a copy-paste
`psql` command + a libpq `PGHOST=… PGPORT=…` line), `Redis:`,
`Lifecycle:` (whether containers are removed on exit or kept under
`--reuse`), `Celery:` (with an enable hint when disabled), `Dump:` (when
applicable), and `Sidecar:` (path to `.run-site-config`).

`show_db_credentials = false` hides every secret in the banner — the
Postgres password line, the `PGPASSWORD=…` in the psql command, the
libpq env line, and the superuser password.

`suggest_dev_helpers = false` removes the "[tip] Install
django-dev-helpers" footer when the helpers app isn't installed.

## `.run-site-config` — the runtime sidecar

While the stack is running, `run-site` drops a TOML file at the project
root with the live ports and connection URLs:

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

It's written **before** `pre_serve` hooks (so they can read it) and
**before** `runserver` starts (so `django-dev-helpers` can read it from
`AppConfig.ready()`). It's removed on clean shutdown. Add
`.run-site-config` to your `.gitignore` — it's regenerated per-run.

## `[source]`

```toml
[source]
type = "git"                                       # or "path"
url = "https://github.com/iplweb/bpp.git"
branch = "main"                                    # or tag/commit
checkout_path = "~/.cache/run-site/checkouts/bpp"
no_cache = false
no_pull = false
no_install = false
```

Useful for CI or scripts. CLI flags `--from-git`, `--branch`, etc. override
this section.

See [from-git.md](from-git.md) for the full story.
