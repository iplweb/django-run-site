# Run any Django project from any source

## TL;DR

```bash
# Zero-install: try a project without installing run-site first.
# `--yes` skips the cloning prompt — copy-paste-safe in tutorials/CI.
uv tool run --from django-run-site run-site run --from-git git@github.com:mpasternak/django-multiseek.git --yes

# Same effect once run-site is installed:
run-site run --from-git https://github.com/iplweb/bpp.git --branch main

# Pin to a tag:
run-site run --from-git https://github.com/iplweb/bpp.git --tag v1.0

# Or a specific commit:
run-site run --from-git https://github.com/iplweb/bpp.git --commit a1b2c3d

# Reuse an existing clone, no pull, no reinstall:
run-site run \
    --from-git https://github.com/iplweb/bpp.git \
    --no-pull --no-install --reuse

# Local checkout from someone else:
run-site run --from-path ~/Downloads/some-django-app

# Sanity check before doing the full run:
run-site doctor --from-git https://github.com/iplweb/bpp.git
```

## What happens

1. **Resolve the checkout path**
   - `--checkout-path PATH` → use that path.
   - `--no-cache` → fresh tempdir, cleaned up on exit.
   - default → `~/.cache/run-site/checkouts/<slug>/`, where `<slug>`
     is extracted from the URL (e.g. `iplweb/bpp` for
     `https://github.com/iplweb/bpp.git`).

2. **Clone or update**
   - If the path doesn't exist → `git clone`.
   - If it exists and is **cache-owned** (under `~/.cache/...`):
     `git fetch && git reset --hard origin/<ref>` — destructive is fine
     because the cache is CLI-owned.
   - If it exists and is **user-owned** (anywhere else): `git status
     --porcelain` first; refuse on any uncommitted change. Otherwise
     `git fetch && git merge --ff-only origin/<ref>`.
   - With `--no-pull`: skip the update entirely.
   - With `--force-reset`: explicit opt-in to destructive
     `git reset --hard` even on user-owned checkouts.

3. **Check out the requested ref**
   - `--branch BR` → `git checkout BR && git pull --ff-only origin BR`
   - `--tag TAG` → `git checkout tags/TAG`
   - `--commit SHA` → `git checkout SHA`
   - default → leave at HEAD of the default branch.

4. **Set up the venv**
   - If `.venv/bin/python` exists → reuse.
   - Otherwise → `uv venv .venv` (preferred) or `python -m venv .venv`.
   - With `--no-install`: skip; error if venv is missing (don't silently
     fall back to the host Python).

5. **Install dependencies**
   - `uv.lock` present → `uv sync`.
   - `pyproject.toml` only → `uv sync` if uv is on PATH else `pip install -e .`.
   - `requirements.txt` → `pip install -r requirements.txt`.
   - Nothing recognizable → warn and skip.
   - The CLI tracks a marker file (`.dev_helpers_installed_marker`) so
     subsequent runs detect when deps files have changed and need
     reinstall.

6. **Run as usual** — discover `manage.py`, start containers, etc.
   `manage.py` auto-detection scans the repo root, `src/`, and one or
   two directories deep (e.g. `test_project/manage.py`,
   `tests/test_project/manage.py`); when several `manage.py` files
   exist, those that actually `import django` are preferred. Pass
   `--manage-py PATH` (relative paths anchor to the cloned root, not
   your CWD) to override.

## Security

`--from-git URL` runs **arbitrary code** from the URL — Django's
`settings.py`, hooks, every dependency `pip install -e` runs. The CLI does
**not** sandbox.

Defenses:

- TTY: interactive `Continue with cloning [URL]? [y/N]` prompt with a 10s
  timeout that defaults to no.
- Non-TTY (CI, pipes): refuses to proceed unless `--yes` / `-y` is passed.
- `--no-cache` is opt-in — by default the cache survives between runs, so
  `rm -rf ~/.cache/run-site/checkouts/` is the user's call.
- `doctor --from-git URL` clones + checks config + manage.py without
  starting containers or runserver. Safer first step.

## CI usage

```bash
run-site run \
    --from-git https://github.com/myorg/myapp.git \
    --branch main \
    --reuse \
    --yes              # required in non-interactive context
```

## Private repos

Use git's standard auth — SSH keys in `~/.ssh/`, or a `~/.netrc`/PAT for
HTTPS. The CLI just shells out to `git`; whatever `git` can do, it can.

Out of scope for v0.3: explicit `--git-token`, deploy-key generation, etc.

## `[source]` in `runsite.toml`

For environments where you can't pass CLI flags (e.g. CI defining the
config alongside the run), put the same info in the config:

```toml
[source]
type = "git"
url = "https://github.com/iplweb/bpp.git"
branch = "main"
no_install = false
```

CLI flags still take precedence.
