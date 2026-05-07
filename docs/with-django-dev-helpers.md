# Integration with `django-dev-helpers`

`django-run-site` is a pure CLI orchestrator — it doesn't import Django,
doesn't modify your `urls.py`, doesn't write dotfiles, doesn't open
autologin URLs. Those features live in a separate package,
[`django-dev-helpers`](https://github.com/iplweb/django-dev-helpers), which
is a normal Django reusable app.

The two integrate through a documented **env-var contract**. Neither
imports the other; you can use either standalone.

## Install

```bash
# CLI orchestrator
uv tool install django-run-site

# Django app (in your project's dev deps)
uv add django-dev-helpers --group dev
```

```python
# settings.py
INSTALLED_APPS = [
    # ...
    "django_dev_helpers",
]
```

```python
# urls.py
from django_dev_helpers.urls import autologin_urlpatterns

urlpatterns = [
    *autologin_urlpatterns(),  # no-op when DEBUG=False
    # ...
]
```

## What each side does

### `django-run-site` (orchestrator)

- Generates a per-run autologin token (`secrets.token_urlsafe(32)`).
- Sets `DEV_HELPERS_*` env vars on the runserver subprocess (see below).
- Starts `runserver` and multiplexes its stdout into the terminal.
- Opens the browser on the **homepage** (not the autologin URL — that's
  dev-helpers' job).
- **Does not** write dotfiles. **Does not** check `.gitignore`. **Does
  not** know `autologin.url_path`.

### `django-dev-helpers` (Django app)

- AppConfig.ready() reads `DEV_HELPERS_*` env vars (with sensible
  fallbacks to `settings.DATABASES` etc.).
- Writes dotfiles in the project root: `.dev_helpers_token`,
  `.dev_helpers_port`, `.dev_helpers_pg_port`, `.dev_helpers_redis_port`.
- Prints an "agent help" banner to stdout (mux'd as the `web` stream by
  the orchestrator).
- Exposes the autologin endpoint that exchanges the token for a session.
- Opens a second browser tab on the autologin URL once `runserver` is
  warm.
- Optionally checks `.gitignore` for the dotfiles and adds them.
- Cleans up dotfiles on SIGTERM / atexit.

## The env-var contract (§13.2)

The orchestrator **always** sets these on the `runserver` subprocess —
regardless of your `[env]` mapping in `runsite.toml`. Names are stable
public API and won't change between v0.x → v1.0 without a deprecation
cycle.

| Env var | Set to |
|---|---|
| `DJANGO_DEV_HELPERS_ENABLED` | `"1"` — only on `runserver` (not on migrate/superuser/hook subprocesses). Hard activation flag. |
| `DEV_HELPERS_AUTOLOGIN_TOKEN` | `secrets.token_urlsafe(32)` |
| `DEV_HELPERS_AUTOLOGIN_USERNAME` | `superuser.username` from runsite.toml |
| `DEV_HELPERS_PORT` | runserver port |
| `DEV_HELPERS_DB_HOST` | host-side host of PG container |
| `DEV_HELPERS_DB_PORT` | host-side port of PG container |
| `DEV_HELPERS_DB_NAME` | PG db name |
| `DEV_HELPERS_DB_USER` | PG user |
| `DEV_HELPERS_REDIS_HOST` | host-side host of Redis container |
| `DEV_HELPERS_REDIS_PORT` | host-side port of Redis container |
| `DEV_HELPERS_PROJECT_ROOT` | absolute project root |

### Why double-set with `[env]`?

Project-side `settings.py` reads its own env-var names (e.g.
`DJANGO_BPP_DB_HOST`); `django-dev-helpers` reads `DEV_HELPERS_DB_HOST`.
The orchestrator is the only place that knows both naming schemes, so the
same value lands under both names. Intentional, documented, and tested.

### `DJANGO_DEV_HELPERS_ENABLED=1`

This is the hard activation flag. It's set **only on the runserver
subprocess** so that `manage.py migrate` and friends don't run the helper
app's `AppConfig.ready()` side effects (writing dotfiles, opening a
browser thread, …). That avoids race conditions when multiple subprocesses
spawn in close succession.

`settings.DJANGO_DEV_HELPERS["enabled"] = False` in your project hard-overrides
`DJANGO_DEV_HELPERS_ENABLED=1` — your settings always win.

## Default UX with both packages

Two browser tabs open automatically:

- Tab 1 (orchestrator): homepage `http://<host>:<port>/`
- Tab 2 (dev-helpers): autologin URL with the token, e.g.
  `http://<host>:<port>/__autologin__/<token>/`

`--no-browser` on the CLI suppresses tab 1. To suppress tab 2, set
`DJANGO_DEV_HELPERS["browser_open"]["enabled"] = False` in `settings.py`.

## Standalone modes

### Just the CLI, no helpers app

Works fine — you just don't get autologin or dotfiles. The CLI prints a
one-line tip after the banner:

```
[tip] Install django-dev-helpers for autologin + dotfiles:
      uv add django-dev-helpers --group dev
      Then add 'django_dev_helpers' to INSTALLED_APPS.
```

Disable the tip with `[banner].suggest_dev_helpers = false`.

### Just the helpers app, no CLI

Also works. The helpers app generates its own token and opens its own
autologin URL. Use this when running with `docker compose` or any other
orchestrator. The `DEV_HELPERS_*` env-var contract is honored if you
happen to set the variables, but it's optional — the app falls back to
reading `settings.DATABASES` and minting its own token.
