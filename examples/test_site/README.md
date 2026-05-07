# test_site — integration fixture for `django-run-site`

This is a deliberately minimal Django project used by `django-run-site`
integration tests and as a "show me it works" demo:

- One app (`test_site.pages`) with two views: homepage `/` and `/healthz/`
  (used as the orchestrator's HTTP probe target).
- DB / cache config read from `DATABASE_URL` / `REDIS_URL` env vars (set by
  the orchestrator) with a SQLite + locmem fallback for `manage.py check`.
- Compatible with Django 5.2 LTS and Django 6.0 — see CI matrix.

## Try it

```bash
cd examples/test_site
django-run-site run
```

The orchestrator will:

1. Set up `.venv` and install Django + dj-database-url (per the
   `pyproject.toml` next to this README).
2. Spin up Postgres and Redis testcontainers.
3. Run migrations and create an `admin/admin` superuser.
4. Start `runserver` on a free port.
5. Open your browser on `http://localhost:<port>/`.

## What it doesn't do

There's no styling, no real models, no tests of its own. The point is to
be a stable shape for the orchestrator to drive — so changes here should
be very rare and only when the orchestrator's expectations change.
