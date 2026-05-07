# Configuration reference

`django-run-site` reads configuration from one of two places, in order:

1. `runsite.toml` in your project root (preferred — clearer for non-trivial
   configs).
2. `[tool.django-run-site]` in `pyproject.toml` (handy when you don't want
   another file).

CLI flags always override the config.

## Top-level keys

```toml
project_slug = "myproject"   # required when not derivable from project_root
manage_py = "src/manage.py"  # path to manage.py, relative to project_root
```

`project_slug` is used as the Postgres/Redis container name prefix when
`--reuse` is set.

## `[python]`

How to invoke the project's Python interpreter. See
[discovery flow §7.3](../DJANGO-RUN-SITE-SPEC-v0.3.md#73-lokalny-python).

```toml
[python]
executable = "auto"  # default; runs the resolution chain
# or
executable = ".venv/bin/python"
# or
command = ["uv", "run", "python"]
```

`executable` and `command` are mutually exclusive (except `executable = "auto"`,
which means "ignore me, use the chain"). With `command`, the CLI prepends
those tokens to every Python invocation — useful when you'd rather use
`uv run` than activate a venv.

## `[postgres]`

```toml
[postgres]
image = "postgres:16"
user = "myproject"
password = "password"
db = "myproject"
driver = ""              # URL prefix suffix: "", "+psycopg2", "ql", …
stream_logs = true       # prefix PG container logs as `pg | …`

[postgres.env]
POSTGRESQL_UNSAFE_BUT_FAST = "1"
```

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
image = "redis:7-alpine"
db = 0
```

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
```

| Strategy | When to use |
|---|---|
| `auto` (default) | Plain `.sql` + fresh container → init-script; otherwise → post-start; reused container → skip with warning. |
| `init-script` | Force PG to load the dump from `/docker-entrypoint-initdb.d/`. Only `.sql`, only fresh containers. |
| `post-start` | Always restore via `psql` / `pg_restore` after PG comes up. Works for `.sql`, `.sql.gz`, `.dump`/`.pgdump`. |

`restore_jobs = "auto"` becomes `min(8, os.cpu_count())`.

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
```

`runserver_display_host` differs from `runserver_bind` so URLs stay clean:
defaulting to `localhost` avoids Safari's HSTS cache being primed by IP
literals.

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
title = "django-run-site is running"
show_db_credentials = true
suggest_dev_helpers = true
```

`show_db_credentials = false` hides the password line. Doctor warns when
your password looks production-y.

## `[source]`

```toml
[source]
type = "git"                                       # or "path"
url = "https://github.com/iplweb/bpp.git"
branch = "main"                                    # or tag/commit
checkout_path = "~/.cache/django-run-site/checkouts/bpp"
no_cache = false
no_pull = false
no_install = false
```

Useful for CI or scripts. CLI flags `--from-git`, `--branch`, etc. override
this section.

See [from-git.md](from-git.md) for the full story.
