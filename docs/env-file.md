# The `.run-site-env.sh` env file

While a stack is running, `run-site` writes a small **sourceable shell file**
at the project root: `.run-site-env.sh`. It exists so you can open a *second
terminal* and talk to the exact same running Postgres / Redis without copying
ports around by hand.

```sh
source .run-site-env.sh

python manage.py dbshell      # uses DATABASE_URL / DJANGO_*
python manage.py shell
psql                          # uses PGHOST/PGPORT/PGDATABASE/PGUSER/PGPASSWORD
pg_dump > backup.sql          # libpq tools need no arguments
```

The running banner prints the file path plus ready-to-paste `source … && …`
commands, so you never have to remember the syntax.

## What's in it

`export KEY="value"` statements for the project-facing configuration only:

| Variable | When |
|----------|------|
| `DATABASE_URL` | Postgres or SQLite enabled |
| `REDIS_URL` | Redis enabled |
| `DJANGO_SECRET_KEY` | a managed secret key is in use |
| `DJANGO_ALLOWED_HOSTS` | binding to a non-loopback interface |
| `PGHOST` `PGPORT` `PGDATABASE` `PGUSER` `PGPASSWORD` | Postgres enabled |

The URL/Django variable **names** follow your `[env]` mapping in `runsite.toml`
(so a renamed `DATABASE_URL` carries through here too). The `PG*` names are
fixed [libpq](https://www.postgresql.org/docs/current/libpq-envars.html)
conventions and are not remappable. Disabled services omit their lines; in
SQLite mode `DATABASE_URL` becomes the `sqlite:///…` form and the `PG*` lines
are dropped.

The values are double-quoted and shell-escaped, so a password containing `$`,
`"`, `` ` ``, `\`, or spaces survives a `source` intact.

## Notes

- **It's an artifact, not an input.** `run-site` builds its own subprocess
  environment in-memory; it never reads this file back. The file is a
  convenience for *you* and other tooling.
- **It's `source`-friendly *and* dotenv-friendly.** Shells `source` it
  directly; `python-dotenv` / `django-environ` also parse it (they strip the
  `export ` prefix).
- **It contains secrets.** The file is recreated on each start and removed on
  clean shutdown. It's covered by the recommended `.gitignore` pattern
  (`.run-site-*`) — keep it out of version control.

For the stable TOML view of the same endpoints (consumed by
`django-dev-helpers`), see the `.run-site-config` sidecar described in the
[quickstart](quickstart.md).
