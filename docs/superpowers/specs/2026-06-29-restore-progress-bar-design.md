# Design: `pv` progress bar for DB restore

Date: 2026-06-29
Status: Approved — implementing

## Problem

Restoring a large dump is silent except for a single `[dump] loading … via
gunzip | psql…` line. The user wants a live progress bar, like the bpp
script's `pv "$SQL_FILE" | sed | psql`.

## Approach

Insert `pv` as the **first** stage of the restore pipeline. `pv` reads the
dump file (so it auto-detects total size → a real percentage bar), writes
the data to stdout (feeding the rest of the pipe), and draws its bar to
**stderr**.

Why this works (confirmed by recon):

- run-site's output mux (`LogMultiplexer`) is line-only, writes to
  `sys.stdout`, and never reassigns/captures `sys.stderr`.
- In `_run_pipe`, **intermediate** stages are `Popen` with only `stdout`
  redirected — their stderr inherits the parent process's real stderr
  (the TTY). The final stage and plain `_run` commands capture stderr.
- So `pv` as a non-final stage renders to the terminal; everything else
  in the pipe is unaffected.
- Restore runs before the sticky-banner scroll-region takeover, so pv has
  the terminal to itself.

## Scope (which restore paths get a bar)

| Format / strategy | Bar? | Pipeline with bar |
|---|---|---|
| Plain SQL, post-start | ✅ | `pv file \| psql` (or `pv file \| sed \| psql` with fix) |
| Gzipped SQL, post-start | ✅ | `pv file \| gunzip -c \| psql` (bar = compressed bytes read) |
| Binary, post-start | ⛔ | `docker cp` + parallel `pg_restore` — no host stream / unknown size |
| init-script | ⛔ | PG runs it internally |

Plain `psql -f file` (today a non-pipe `_run`) becomes a pipe `pv file |
psql` when the bar is enabled — that's the only way its stderr can reach
the TTY.

## Gate (when the bar turns on)

Automatic, zero-config:

```python
def _should_show_progress_bar() -> bool:
    return shutil.which("pv") is not None and sys.stderr.isatty()
```

- No `pv` installed → no bar (graceful fallback to today's behavior).
- Non-interactive (CI, piped, headless) → `sys.stderr.isatty()` is False →
  no bar. This also keeps the test suite on the unchanged argv shapes.

No config flag for now (YAGNI; matches bpp's `[ -t 2 ] && command -v pv`).

## Implementation

`src/run_site/dumps.py`:

- `import sys`.
- `_should_show_progress_bar() -> bool` (the gate above).
- `build_post_start_argv(..., progress_bar: bool = False)` — new keyword
  param (default False keeps callers/tests unchanged).
- Unify the PLAIN_SQL + GZIPPED_SQL branches into one stages-builder that,
  in order, optionally prepends `(pv, "-N", <name>, <file>)`, then the
  gunzip stage (reading stdin when pv is upstream, else the file), then
  the sed fix stage (stdin vs file likewise), then `psql`. If only `psql`
  remains (plain, no pv, no fix) → return the `psql -f file` fast path
  unchanged.
- `execute_post_start` computes `progress_bar = _should_show_progress_bar()`
  once and passes it into both `build_post_start_argv` calls. The binary
  branch ignores it.

`pv` is resolved via `_require_tool("pv")`, only reached when the gate
already confirmed pv exists.

## Non-goals

- No bar for binary `pg_restore` (would need a size-less throughput-only
  `pv`, and the parallel default isn't a host stream). Keep the existing
  text messages there.
- No config flag.

## Testing

- `build_post_start_argv(progress_bar=True)` prepends a `pv -N <name> <file>`
  first stage for plain and gzip; downstream tools read stdin; psql is
  last. With `progress_bar=False`, argvs are byte-for-byte unchanged
  (regression guard) — fix on/off, plain/gzip.
- `progress_bar=True` + `fix_search_path=True` → `pv | gunzip -c | sed |
  psql` (gzip) and `pv | sed | psql` (plain), order asserted.
- `_should_show_progress_bar` is False when `which("pv")` is None and when
  `sys.stderr.isatty()` is False (monkeypatched).
- pv is intermediate (never the final stage) so its bar reaches the TTY —
  asserted by position (`stages[0]` is pv, `stages[-1]` is psql).
