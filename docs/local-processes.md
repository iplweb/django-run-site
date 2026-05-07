# Local processes — Celery, frontends, anything else

The orchestrator multiplexes any number of long-lived processes alongside
`runserver`, with colored log prefixes per stream.

## Celery

```toml
[celery]
app = "myproject.celery"
enabled = true
worker_pool = "solo"
worker_log_level = "info"
worker_extra_args = ["-Q", "default,billing"]
with_beat = false
beat_log_level = "info"
beat_extra_args = []
```

Toggle per-run:

```bash
run-site run --no-celery                  # don't start the worker
run-site run --with-celery-beat           # also start beat
run-site run --with-celery --no-celery-beat
```

`worker_pool = "solo"` is the default for a reason: macOS's `fork()` plus
psycopg/numpy/lxml is a long-running source of mysterious worker silently
dying. Prefer `solo` for dev unless you have a specific reason not to.

## Extra processes

Anything that should run alongside Django:

```toml
[[extra_processes]]
name = "frontend"
command = ["npm", "run", "dev"]
cwd = "."
enabled_default = false
color = "blue"
cli_flag = "--with-frontend"
cli_disable_flag = "--no-frontend"

[[extra_processes]]
name = "qcluster"
command = ["{python}", "{manage_py}", "qcluster"]
cwd = "{manage_dir}"
enabled_default = true
color = "magenta"
```

### Template variables

| Variable | Value |
|---|---|
| `{python}` | Resolved Python *command* — multi-token (e.g. `uv run python`) when configured that way. When `{python}` is the entire token, it's expanded inline as multiple argv tokens. |
| `{manage_py}` | Absolute path to `manage.py`. |
| `{manage_dir}` | Parent directory of `manage.py`. |
| `{project_root}` | Absolute project root. |
| `{port}` | Port `runserver` is listening on. |

### CLI control

| Flag | Effect |
|---|---|
| `--with-<name>` | Start this process even if `enabled_default = false`. |
| `--no-<name>` | Skip this process even if `enabled_default = true`. |
| `cli_flag = "..."` | Override the auto-generated `--with-<name>` flag. |
| `cli_disable_flag = "..."` | Override the auto-generated `--no-<name>` flag. |

### Reserved names

`web`, `pg`, `redis`, `celery`, `celery-beat` — collision is a config
validation error.

### Colors

`cyan`, `green`, `yellow`, `magenta`, `blue`, `red`, `white`. Anything else
fails validation.

## Log multiplexer

Every line from every process is prefixed with the stream name, padded to
12 chars, in its assigned color:

```
web          | [16/May/2026 14:21:09] "GET / HTTP/1.1" 200 1421
celery       | [2026-05-16 14:21:09] task my.task[abc] succeeded
pg           | 2026-05-16 14:21:10 UTC [42] LOG:  database "myproject" ready
```

`PYTHONUNBUFFERED=1` is set on every Python subprocess so stdout flushes
immediately and lines don't pile up in a buffer.

`NO_COLOR=1` disables ANSI escapes (and any non-TTY stdout disables them
automatically). `FORCE_COLOR=1` forces them on (useful when piping into a
log viewer that understands ANSI).
