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

### `docker ps` works but `run-site` still says it's unreachable

If the `docker` CLI works (`docker ps` succeeds) but `run-site` reports
the daemon as unreachable, you are almost certainly using a non-default
**Docker context** — for example OrbStack, colima, or a Docker Desktop
install where `/var/run/docker.sock` is missing or a dangling symlink.
The CLI follows the active `docker context`; `run-site` now does too,
resolving the context endpoint automatically.

If it still fails, point `run-site` at the daemon explicitly by exporting
`DOCKER_HOST` to the active context's socket (which always takes
precedence):

```
# see the active context and its endpoint
docker context ls
docker context inspect -f '{{.Endpoints.docker.Host}}'

# e.g. OrbStack
export DOCKER_HOST=unix://$HOME/.orbstack/run/docker.sock
# e.g. colima
export DOCKER_HOST=unix://$HOME/.colima/default/docker.sock
```

## `manage.py` not found

```
error: Could not find manage.py in /path/. Set --manage-py or
'manage_py' in runsite.toml.
```

Resolution chain:

1. `--manage-py PATH` (relative paths anchor to the **project root**, not
   your CWD — important under `--from-git`).
2. `manage_py = "..."` in config (also relative to project root).
3. Auto-scan: `manage.py` → `src/manage.py` → one level deep
   (`<dir>/manage.py`) → two levels deep (`<dir>/<sub>/manage.py`),
   skipping `.venv`, `node_modules`, `__pycache__`, `build`, `dist`,
   `docs`, `static`, `media`, `egg-info`, `.git`, `.tox`, etc.
4. Tie-breaker: when several `manage.py` files match, those that
   actually `import django` (AST-checked) are preferred. If multiple
   pass that filter, the run aborts with the candidate list and asks
   for `--manage-py`.

## Multiple Django manage.py files found

```
error: Multiple Django manage.py files found under <root>:
demo/manage.py, test_project/manage.py. Pass --manage-py or set
'manage_py' in runsite.toml to disambiguate.
```

Some Django package repos ship more than one usable `manage.py` (e.g. a
demo + a test project). Pick one explicitly:

```bash
run-site run --from-git URL --manage-py test_project/manage.py
```

…or commit a `runsite.toml` to the repo with `manage_py = "..."` set.

## `[python].executable=...` does not exist

The path is interpreted **relative to the project root**, not your CWD.
Either give an absolute path or set `executable = "auto"` and let the
discovery chain pick the right one. The current order is:

1. `RUN_SITE_PYTHON` env var (explicit override).
2. `<project_root>/.venv/bin/python` (preferred — see next entry for
   why this beats `$VIRTUAL_ENV`).
3. `$VIRTUAL_ENV/bin/python` (the venv you've activated, if any).
4. `uv run python` (only when `uv.lock` is present and `uv` is on PATH).
5. `sys.executable` (whatever interpreter is running run-site).

## `ModuleNotFoundError: No module named 'django'` from `runserver`/`migrate`

Two causes, both common under `uv tool run --from django-run-site run-site` / `pipx run`:

1. **Wrong venv picked.** The wrapper sets `VIRTUAL_ENV` to the *tool's*
   venv (which has run-site but not Django). run-site preferentially
   uses `<project_root>/.venv/bin/python` over `$VIRTUAL_ENV` to avoid
   this — but if your project doesn't have a `.venv`, the wrapper's
   venv wins. Run `--from-git` once to let run-site create the project
   venv, or activate the right venv yourself before running.
2. **`Path.resolve()`-ing a venv symlink.** uv creates `.venv/bin/python`
   as a symlink to the upstream interpreter; resolving the symlink
   gives a path with no `pyvenv.cfg` in scope, so CPython runs in
   non-venv mode (no project deps on `sys.path`). run-site uses
   `os.path.abspath` (collapses `..` / `.` but does not follow
   symlinks), which is what every test exercises. If you wrote a
   custom `[python].executable` or hook that calls `Path(...).resolve()`,
   you'll see this error — switch to `os.path.abspath`.

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
