# Hooks

Hooks run at fixed points in the orchestrator's flow — before containers
start, after migrate, before `runserver`, etc. Two flavors:

- `type = "command"` — runs as a regular subprocess on your host. Same
  Python you'd use in a shell. Good for `make assets`, dump fetching,
  filesystem prep.
- `type = "django"` — runs through `manage.py shell -c` inside the
  project's Python, with a JSON context dict passed in. Good for ORM
  fixups, reading from the dev database, anything that needs Django
  loaded.

## Stages

| Stage | Notes |
|---|---|
| `pre_containers` | Host hooks only — no PG/Redis exist yet. |
| `post_containers` | Host or Django. PG/Redis up but no migrations. |
| `pre_dump` | Semantics depend on dump strategy (see below). |
| `post_dump` | After the dump is loaded. |
| `post_migrate` | After `manage.py migrate`. |
| `post_superuser` | After superuser created/updated. Skipped when superuser is disabled. |
| `pre_serve` | Just before `runserver` spawns. |
| `post_stop` | Best-effort cleanup. Errors are logged, not fatal. |

### `pre_dump` quirk

`pre_dump` runs *after* containers are up, and what "before the dump"
means depends on the strategy:

| Strategy | When `pre_dump` runs |
|---|---|
| `init-script` | After PG is up — but PG already loaded the dump as part of its startup. So `pre_dump` is effectively `post_dump` for this strategy. |
| `post-start` | After PG is up, before `psql` / `pg_restore` runs. The intuitive "before the dump" timing. |
| `auto` w/ reused PG | Doesn't run (no dump happens). |
| `--no-dump` | Doesn't run. |

If a hook genuinely needs to run before *any* data is loaded, use
`post_containers`. If it needs to run after data, `post_dump` is always
correctly post-data.

## Schema

```toml
[[hooks.<stage>]]
type = "command"                    # or "django"
command = ["bash", "-lc", "..."]    # for type=command
callable = "myproj.hooks:my_func"   # for type=django (module:function)
timeout = 300                       # seconds; SIGTERM then SIGKILL after +5s
cli_disable_flag = "--skip-this"    # optional; user can skip per-run

[[hooks.<stage>.cli_args]]          # optional; register dynamic CLI flags
flag = "--my-flag"
dest = "my_dest"
metavar = "VALUE"
help = "Short description"
default = null
required = false
```

## Examples

### Build assets before containers

```toml
[[hooks.pre_containers]]
type = "command"
command = ["make", "assets"]
timeout = 300
cli_disable_flag = "--skip-assets"
```

```bash
run-site run                 # runs `make assets` first
run-site run --skip-assets   # skips it for this run only
```

### Per-project superuser cleanup

```toml
[[hooks.post_superuser]]
type = "django"
callable = "myproject.runsite_hooks:clear_password_policy"
```

```python
# myproject/runsite_hooks.py
def clear_password_policy(ctx: dict) -> None:
    from password_policies.models import PasswordChangeRequired
    PasswordChangeRequired.objects.filter(
        user__username=ctx["superuser"]["username"]
    ).delete()
```

### Hook with a dynamic CLI argument

```toml
[[hooks.post_migrate]]
type = "django"
callable = "myproject.runsite_hooks:fetch_token"
timeout = 60

[[hooks.post_migrate.cli_args]]
flag = "--get-token-from"
dest = "ssh_source"
metavar = "USER@HOST"
help = "Pull a token from this SSH host after migrations"
```

```python
def fetch_token(ctx: dict) -> None:
    source = ctx["opts"].get("ssh_source")
    if not source:
        return  # flag not passed — no-op
    ...
```

```bash
run-site run --get-token-from admin@bpp-prod
```

`--help` will show the new flag automatically — the parser is rebuilt
after config load (two-pass parsing: discover hooks first, then add their
flags before the final `parse_args`).

## Context dict (`ctx`)

Every Django hook receives the same dict:

```python
{
    "project_root": "/absolute/path",
    "manage_py": "/absolute/path/manage.py",
    "runserver_url": "http://localhost:49152",   # null in pre-serve stages
    "runserver_port": 49152,
    "pg_host": "127.0.0.1",
    "pg_port": 54321,
    "redis_host": "127.0.0.1",
    "redis_port": 49153,
    "dump_path": "/path/to/baseline.sql",        # null when no dump
    "reuse": false,
    "pg_created": true,
    "redis_created": true,
    "superuser": {
        "username": "admin",
        "email": "admin@example.com",
        "created": true,
    },
    "opts": {
        "ssh_source": "admin@bpp-prod",
        "remote_deploy_path": "~/bpp-deploy",
    },
}
```

Hooks read only their own `dest` keys from `opts`. The orchestrator passes
the whole map through.

## Error policy

| Stage | On error |
|---|---|
| `pre_containers` | Fail-fast. No containers to clean up. |
| `post_containers` … `pre_serve` | Fail-fast, then run normal cleanup (stop containers unless `--reuse`). |
| `post_stop` | Best-effort: log + continue. |

A timeout sends SIGTERM, waits 5 seconds, then SIGKILL.

## Validation

`run-site doctor` and `--dry-run` validate:

- `type` is `"command"` or `"django"`.
- `callable` looks like `module.path:function_name`.
- Duplicate `flag` between hooks → error.
- Two different `flag`s with the same `dest` → error.
- `cli_args.flag` and `cli_disable_flag` start with `-`.
- `cli_args.dest` is a valid Python identifier.
