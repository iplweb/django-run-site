# Design: `fix_search_path` dump filter

Date: 2026-06-29
Status: Approved (design), pending implementation plan

## Problem

Dumps produced by modern `pg_dump` harden their header against
CVE-2018-1058 by emitting:

```sql
SELECT pg_catalog.set_config('search_path', '', false);
```

An empty `search_path` for the whole restore session breaks restores of
some objects whose definitions are parsed eagerly and reference operators
or types living in `public`. The canonical failure (seen migrating bpp to
PG18) is restoring an old denorm trigger whose `WHEN` clause compares an
`hstore` column with `IS DISTINCT FROM` — that expands to
`public.hstore = public.hstore`, an operator that is invisible with an
empty `search_path`, producing:

```
operator does not exist: public.hstore = public.hstore
```

The fix used in `bpp-deploy/scripts/pg-collation-migrate-3-load.sh` is a
streaming `sed` that rewrites the header to restore `public` to the
search path for the duration of the restore, **without modifying the
on-disk dump**:

```sh
sed "s/set_config('search_path', '', false)/set_config('search_path', 'public', false)/"
```

run-site has no way to apply such a filter during restore. This change
adds one.

## Goal

Add an opt-in `[dump].fix_search_path` boolean. When enabled, run-site
applies exactly the bpp substitution to the SQL stream during restore,
across every restore path. When disabled (the default), behavior is
byte-for-byte identical to today.

## Non-goals

- A general-purpose / arbitrary `sed` or filter-command mechanism. This
  is a single, named, well-understood fix. (Considered and rejected in
  favor of a dedicated flag — YAGNI, zero footguns.)
- Modifying the on-disk dump file. The filter is always streamed.

## The substitution

A single source of truth in `dumps.py`:

```python
# Restore `public` to the restore-session search_path (pre-CVE-2018-1058
# behaviour). Matches modern pg_dump / pg_restore header hardening.
# Safe because pg_dump qualifies every object with its schema.
SEARCH_PATH_SED = (
    "s/set_config('search_path', '', false)/"
    "set_config('search_path', 'public', false)/"
)
```

Note the match string is a substring of the dumped
`pg_catalog.set_config('search_path', '', false)` line, so it matches
both the `pg_catalog.`-qualified and bare forms, exactly like the bpp
script.

## Per-format wiring

The filter is applied to **text SQL only**. The four restore paths:

| Format / strategy        | Today                                          | With `fix_search_path=true`                                                              |
|--------------------------|------------------------------------------------|-----------------------------------------------------------------------------------------|
| Plain SQL, post-start    | `psql -f <file>` (no pipe)                      | `sed <expr> <file> \| psql … -v ON_ERROR_STOP=1` (host, 2-stage)                         |
| Gzipped SQL, post-start  | `gunzip -c <file> \| psql`                      | `gunzip -c <file> \| sed <expr> \| psql` (host, 3-stage)                                 |
| Binary, post-start       | `docker cp` + `docker exec pg_restore … -j N`  | `docker cp` + (`docker exec … pg_restore -f - --no-owner /tmp/dump` \| host `sed` \| host `psql`) — **drops `-j`** |
| Plain SQL, init-script   | bind-mount `<file>` into init dir              | bind-mount a `sed`-filtered temp copy of `<file>`                                        |

Key architectural facts that make this clean:

- Post-start plain/gzip restores already run **host** `psql` against the
  container's published port (`_require_tool("psql")` in
  `build_post_start_argv`). So the binary path can be unified host-side
  too: `docker exec … pg_restore -f -` streams plain SQL to host stdout;
  host `sed` filters; host `psql` loads. **No `sh -c` is needed**, which
  matters because the sed script is full of single quotes that would be
  painful to quote inside `docker exec … sh -c '…'`.
- `pg_restore -f -` only *converts* the archive to SQL on stdout; it does
  not connect to a database, so that stage needs no `PGPASSWORD`.
- init-script strategy is **plain-SQL-only** (`_decide_strategy` raises
  `DumpError` for any non-plain format on init-script). So
  "init-script + gzip/binary" cannot occur — no special handling needed.

### The bpp baseline path (most important)

The bpp baseline is plain SQL with `strategy = "auto"`. On a freshly
created PG container, `_decide_strategy` returns **init-script** for plain
SQL. So the bpp use case primarily exercises the **init-script** row
above: run-site writes a filtered temp copy and mounts that. (When the
container is reused, auto returns `skip` and no restore — and thus no
filter — runs, which is correct.)

## Plumbing changes

### N-stage pipes

Today the pipe machinery is two-stage only:

- `build_post_start_argv` encodes the gzip pipe as a single tuple
  `("__pipe__", *left, *right)` and `_run_argvs` splits it at the token
  ending in `psql`.
- `_run_pipe(left, right, …)` wires exactly one `Popen` into one
  `subprocess.run`.

To support `gunzip | sed | psql` and `pg_restore | sed | psql`, replace
the stringly-typed `"__pipe__"` encoding with a small dataclass:

```python
@dataclass(frozen=True)
class Pipe:
    """An N-stage shell-free pipeline: stages[i].stdout -> stages[i+1].stdin."""
    stages: tuple[Sequence[str], ...]
```

- `build_post_start_argv` returns `list[Sequence[str] | Pipe]`.
- `_run_argvs` dispatches on `isinstance(argv, Pipe)`.
- `_run_pipe(stages, *, env, fail_fast)` chains N processes: each
  non-final stage is a `Popen(stdout=PIPE)` reading the previous stage's
  stdout; the final stage is `subprocess.run(stdin=prev.stdout,
  capture_output=True)`. After the final stage returns, close each
  intermediate stdout and `wait()` every process. `pipefail` semantics:
  if **any** stage exits non-zero and `fail_fast`, raise `DumpError`
  including each stage's argv and return code (and the final stage's
  captured stdout/stderr). This preserves the current "a failing psql
  aborts" behavior and extends it to a failing `gunzip`/`sed`/
  `pg_restore`.

This is internal-only; the public `build_post_start_argv` /
`execute_post_start` signatures are unchanged except for the richer
return element type.

### init-script filtered copy

Where the init-script path is consumed (cli.py, passed to
`start_containers` as `init_script`), when `config.dump.fix_search_path`
is true and `plan.strategy == "init-script"`, write a filtered copy and
mount that instead of `plan.path`:

```python
# sed <expr> <src>  with stdout redirected to a temp file (no shell)
```

A helper in `dumps.py`, e.g.
`write_search_path_filtered(src: Path, dst: Path) -> None`, runs
`sed SEARCH_PATH_SED <src>` with `stdout` redirected to `dst` (using
`subprocess.run(..., stdout=fh)`, no shell). The temp file must exist
from container creation until PG has finished its init phase; it is
written before `start_containers` and cleaned up after the run flow has
confirmed the container is up (mirroring how other transient restore
temp files are scoped). Exact lifecycle/cleanup point to be pinned in
the implementation plan against the existing cli.py init-script handling.

## Config & CLI

### `DumpConfig`

```python
@dataclass(frozen=True)
class DumpConfig:
    default_path: str | None = None
    strategy: DumpStrategy = "auto"
    restore_jobs: int | str = "auto"
    fail_fast: bool = True
    fix_search_path: bool = False   # NEW
```

`_build_dump` parses it with the existing helper:
`fix_search_path=_bool(raw, "fix_search_path", default=False)`.

### TOML

```toml
[dump]
default_path = "src/baseline-sql/baseline.sql"
strategy = "auto"
fix_search_path = true   # '' -> 'public' during restore
```

### CLI

Add to the "Dump" argument group, mirroring the `--restore-jobs` merge
pattern:

```python
dump.add_argument(
    "--fix-search-path",
    action=argparse.BooleanOptionalAction,  # --fix-search-path / --no-fix-search-path
    default=None,
)
```

Merge in `_apply_cli` (next to the `restore_jobs` merge):

```python
if opts.fix_search_path is not None:
    dump = replace(dump, fix_search_path=opts.fix_search_path)
```

`default=None` keeps "unset = use config"; an explicit flag overrides.

## Safety / observability

- **No-op warning.** When `fix_search_path` is true but the dump header
  does not contain `set_config('search_path', '', false)`, log a warning
  that the fix is a no-op (the dump format may have changed) — like the
  bpp script's `head -n 100 | grep` check. Applies to **plain** and
  **gzipped** SQL only (peek the first ~64 KB, gunzipping for the gzip
  case). Skipped for binary, where the statement is generated by
  `pg_restore` and is not present in the raw archive.
- **`-j` disabled notice.** When format is binary and the flag is on, log
  that parallel `-j` restore is disabled because the archive is being
  streamed through `sed` (the user opted into this tradeoff).
- `sed` is resolved via `_require_tool("sed")`, giving a clear error if
  somehow absent.

## Testing

Unit (no Docker):

- `build_post_start_argv` with `fix_search_path=False` returns today's
  argv shapes verbatim (regression guard) for all three formats.
- `build_post_start_argv` with `fix_search_path=True`:
  - plain → a `Pipe` of `[sed <expr> <file>, psql … -v ON_ERROR_STOP=1]`;
  - gzip → a `Pipe` of `[gunzip -c <file>, sed <expr>, psql …]`;
  - binary → `docker cp` then a `Pipe` of
    `[docker exec … pg_restore -f - --no-owner /tmp/dump, sed <expr>,
    psql …]` with no `-j` token anywhere.
- The `SEARCH_PATH_SED` constant is exactly the bpp string.
- `_run_pipe` chains 3 fake processes and (a) feeds stage output to the
  next stage's input, (b) raises `DumpError` when a middle stage exits
  non-zero under `fail_fast`, and (c) raises when the final stage fails.
- `write_search_path_filtered` rewrites `set_config('search_path', '',
  false)` → `… 'public' …` in the output copy and leaves other lines
  untouched; source file is unmodified.
- Config: `[dump].fix_search_path` round-trips through `_build_dump`
  (true/false/default); CLI `--fix-search-path` / `--no-fix-search-path`
  override config, absence leaves it unchanged.
- No-op warning fires for a header lacking the pattern; does not fire
  when present.

Integration / Docker (marked `docker` / `integration`):

- End-to-end restore of a small plain dump containing the hardened
  header + an object that needs `public` on the search path, with the
  flag on, succeeds; with the flag off, fails with the
  `operator does not exist` error — proving the filter does the job.

## Files touched

- `src/run_site/dumps.py` — `SEARCH_PATH_SED`, `Pipe`, N-stage
  `_run_pipe`, `_run_argvs` dispatch, `build_post_start_argv` branches,
  `write_search_path_filtered`, header no-op check, `-j` notice.
- `src/run_site/config.py` — `DumpConfig.fix_search_path`, `_build_dump`.
- `src/run_site/cli.py` — `--fix-search-path` arg + `_apply_cli` merge;
  init-script filtered-copy wiring.
- `examples/runsite.bpp.toml` — document `fix_search_path` in `[dump]`.
- `docs/` — document the new option.
- `tests/` — `test_dumps.py` / `test_config.py` (and an integration test).
```
