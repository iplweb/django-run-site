# Troubleshooting

## Docker daemon is not reachable

```
error: Docker daemon is not reachable. Start Docker Desktop / colima /
podman and retry.
```

`run-site` needs a working Docker daemon for testcontainers. Common
fixes:

- **macOS**: start Docker Desktop, OrbStack, colima, or rancher-desktop.
- **Linux**: `sudo systemctl start docker` and make sure your user is in
  the `docker` group (`sudo usermod -aG docker $USER`, then re-login).
- **Podman**: enable the docker socket compat layer
  (`systemctl --user enable --now podman.socket`) and export
  `DOCKER_HOST=unix://$XDG_RUNTIME_DIR/podman/podman.sock`.

`run-site doctor` runs the same probe and prints the same error if
it fails — useful as a one-liner sanity check.

## `manage.py` not found

```
error: Could not find manage.py in /path/. Set --manage-py or
'manage_py' in runsite.toml.
```

Resolution chain:

1. `--manage-py PATH`
2. `manage_py = "..."` in config
3. `<project_root>/src/manage.py`
4. `<project_root>/manage.py`

If your `manage.py` lives somewhere unusual, set `manage_py` in
`runsite.toml`.

## `[python].executable=...` does not exist

The path is interpreted **relative to the project root**, not your CWD.
Either give an absolute path or set `executable = "auto"` and let the
discovery chain pick the right one (`$VIRTUAL_ENV`, `.venv/bin/python`,
`uv run python`, `sys.executable`).

## Port already in use

`runserver` picks a free port via the OS. If you forced one with
`--port` and it's busy, the CLI errors out. Drop `--port` to let the OS
choose.

## Containers from a previous run are still around

Either:

- Run with `--reuse` and the orchestrator will attach to them.
- Or remove them: `docker rm -f <project_slug>-runsite-pg <project_slug>-runsite-redis`.

If `--reuse` containers exist but you've changed `[postgres]` / `[redis]`
config, the existing container takes precedence — the orchestrator never
recreates a reused container automatically. Remove and re-run.

## Dump load skipped with `auto` strategy

```
dump        | [dump] skipped: PG container was reused; existing data preserved.
```

Expected behavior with `--reuse` — the orchestrator never overwrites a
reused container's data. Force a reload with one of:

- `--no-reuse` (then containers go away on exit and the next run loads
  the dump).
- Manually drop the database and restart: `docker exec
  <project_slug>-runsite-pg dropdb -U <user> <db>`.
- `--dump-strategy=post-start` to force a post-start restore even on a
  reused container — but pick this only if you really want to nuke
  existing data.

## `init-script` strategy refuses to run

```
error: dump.strategy='init-script' requires a freshly created PG
container, but an existing one was reused.
```

Either drop `--reuse` for one run, or change strategy to `post-start` /
`auto`.

## `--from-git` refuses in non-interactive context

```
error: --from-git in non-interactive context: pass --yes to confirm.
```

Pass `--yes` (or `-y`) explicitly. The CLI refuses to clone arbitrary
code without user confirmation in CI / piped stdin contexts.

## `--no-install` with no venv

```
error: venv missing in /path/.venv. Run without --no-install to create
venv and install deps, or create venv manually first.
```

`--no-install` means "don't touch the venv". If there's no venv to leave
alone, that's an error — drop the flag, or `python -m venv .venv` first.

## Logs from a subprocess never appear

Set `PYTHONUNBUFFERED=1` (the CLI does this automatically for
spawned Python processes; `[[extra_processes]]` definitions also get it).
If the process is non-Python and buffers stdout when not connected to a
TTY, you may need to disable buffering on its side (e.g. `stdbuf -oL` on
Linux).

## `--print-env --print-secrets`

Don't ever paste the output anywhere shared. The orchestrator redacts
`(?i).*(TOKEN|PASSWORD|SECRET|API_KEY).*` by default; `--print-secrets`
turns redaction off explicitly so you can grep for a specific value while
debugging.

## Django says `Apps aren't loaded yet` from a hook

Django hooks run via `manage.py shell -c`. If your hook tries to use the
ORM at module-import time (a top-level query), Django isn't ready yet.
Move queries inside the hook function:

```python
# WRONG — runs at import
from myapp.models import Foo
COUNT = Foo.objects.count()

def my_hook(ctx): ...

# RIGHT — runs when called
def my_hook(ctx):
    from myapp.models import Foo
    print(Foo.objects.count())
```

## Why does the browser open a tab I didn't expect?

If `django-dev-helpers` is installed, it independently opens an autologin
URL alongside the homepage that the orchestrator opens. To suppress
either:

- Homepage: `--no-browser` on the CLI.
- Autologin: `DJANGO_DEV_HELPERS["browser_open"]["enabled"] = False` in
  `settings.py`.
