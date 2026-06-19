# Support pg_dump archive dumps (custom / directory / tar) via pg_restore

**Date:** 2026-06-01
**Status:** Approved (design)
**Area:** `src/run_site/dumps.py` (+ tests)

## Problem

`run-site --from-dump PATH` fails on PostgreSQL **directory-format** dumps
that were packaged as `.tar.gz`. Concretely, a real backup
(`db-backup-20260601-023000.tar.gz`) is a `pg_dump -Fd` directory
(`toc.dat` + numbered `*.dat.gz`) that was then `tar | gzip`-ed. Running:

```
run-site run --from-dump db-backup-20260601-023000.tar.gz
```

produces:

```
[dump] loading db-backup-20260601-023000.tar.gz (48.0 MB) via gunzip | psql…
error: Piped restore failed: left=-13 right=3
```

### Root cause

`detect_format()` classifies dumps **purely by filename extension**
(`dumps.py:54-70`). The file ends in `.tar.gz`, which is not `.sql.gz`,
so it falls through to the pessimistic "any `.gz` → `GZIPPED_SQL`" branch
(`dumps.py:64-66`). run-site then runs `gunzip -c file | psql`; psql
receives a raw tar byte-stream, errors, and closes the pipe early
(`left=-13` = `gunzip` killed by SIGPIPE, `right=3` = psql exit).

Two underlying gaps:

1. **No archive support beyond single-file custom dumps.** run-site knows
   `PLAIN_SQL`, `GZIPPED_SQL`, and `CUSTOM` (a single-file `pg_restore`
   archive). Directory-format dumps — and the common practice of
   `tar | gzip`-ing them for transport — are unsupported.
2. **Extension-only detection is brittle.** A backup named
   `*.tar.gz`, `*.bak`, or anything non-canonical is misrouted.

### Why pg_restore (from the image), not gzip | psql

The dump was produced by `pg_dump` 16.13; the target `bpp_dbserver` image
is PG 16.13. Archive dumps (custom/dir/tar) are restored with `pg_restore`,
not `psql`. run-site already has a `pg_restore`-in-the-image path for
`CUSTOM` (`dumps.py:206-235`): `docker cp` the dump into the container,
then `docker exec … pg_restore --no-owner --exit-on-error -j N`. Using the
**container's** `pg_restore` (matching the server major version) is
deliberate — the host here has `pg_restore` 17.x, and a newer `pg_restore`
against an older server can emit statements the server rejects.

`pg_restore` **auto-detects** the archive format (custom / directory / tar)
from the archive itself — verified empirically:

```
$ pg_restore -l <extracted-dir>
;     Format: DIRECTORY
;     Dumped from database version: 16.13
;     TOC Entries: 3119
```

No `-Fd` flag, no running server — pointed at the extracted directory it
just reads it. So run-site does **not** need to identify *which* archive
format a dump is. It only needs to (a) choose engine `psql` vs
`pg_restore`, and (b) peel any outer `gzip`/`tar` wrapper, because
`pg_restore` will not strip that wrapper itself.

## Goals

- Restore `tar.gz` / `tgz`-wrapped directory-format dumps via the image's
  `pg_restore`.
- Make detection robust by inspecting **content** (magic bytes), not just
  the filename.
- Keep plain-SQL, gzipped-SQL, and raw single-file custom (`.dump`) paths
  behaviorally identical.

## Non-goals

- No support for `pg_dumpall` cluster dumps (they `CREATE DATABASE` /
  manage roles — out of scope; would restore via psql against `postgres`).
- No gold-plating of exotic packagings beyond what content-sniffing covers
  naturally (bare `.tar` directory dumps and `.dump.gz` come essentially
  for free; nothing further).
- No change to the `init-script` strategy (still plain-SQL only).

## Design

### Mental model: two restore engines

- **`psql`** — text SQL, plain or gzipped (unchanged).
- **`pg_restore`** — every binary archive. run-site never names
  custom/dir/tar; `pg_restore` auto-detects.

Detection answers two narrow questions, nothing more:

1. **Which engine?** SQL (text) vs archive (`PGDMP` magic, or a `tar`
   whose contents include `toc.dat`).
2. **How is it wrapped?** none / gzip / tarred-directory — only enough to
   know how to unwrap before handing to the engine.

### 1. Content-based detection (`detect_format`)

Replace extension-only logic with magic-byte sniffing. Read the first
~512 bytes; if gzip (`1f 8b`), decompress a small prefix and inspect that.

Decision order:

| Observed bytes (after any gunzip)        | Engine      | Wrapping            |
|------------------------------------------|-------------|---------------------|
| `PGDMP` magic                            | pg_restore  | none / gzip         |
| tar (ustar @ offset 257) containing a    | pg_restore  | tar / gzip+tar      |
| `toc.dat` member                         |             |                     |
| printable/SQL text                       | psql        | none / gzip         |

Extension remains a cheap fast-path hint (e.g. `.sql` → text without
sniffing), but **content wins** on conflict. Unknown/empty content with
an unrecognized extension still raises `DumpError` (preserves
`test_detect_unsupported`).

The current `DumpFormat` enum (`PLAIN_SQL`, `GZIPPED_SQL`, `CUSTOM`)
collapses on the archive side. Proposed shape: keep `PLAIN_SQL` and
`GZIPPED_SQL`; replace `CUSTOM` with a single archive route plus a
`wrapping` descriptor recorded on `DumpPlan` so `prepare_archive` knows
how to unwrap. (Exact enum/field naming finalized in the plan; tests in
`tests/test_dump_loaders.py` that reference `DumpFormat.CUSTOM` are updated
in lockstep.)

### 2. `prepare_archive()` — host-side unwrap

New helper, run on the host before the restore argv is built:

- **none** (raw `.dump`, raw `.tar`): return the path unchanged.
- **gzip** (`.dump.gz`): `gunzip` to a temp file; return it.
- **tarred directory** (`.tar` / `.tar.gz` / `.tgz`): `gunzip` (if needed)
  + `tar -x` into a temp dir, then locate the directory containing
  `toc.dat` (the dump may sit under a single top-level folder, as in the
  real backup) and return that directory.

The temp dir/file is registered for cleanup in a `finally` block around
the restore. Cleanup failures are **logged** (warning), never silently
swallowed — per the repo error-handling rules.

Host-side extraction (vs. copying the tarball in and extracting inside the
container) is chosen because `gzip`/`tar` are always present on the host,
it mirrors the existing `docker cp` pattern, and it assumes nothing about
tools inside the image.

### 3. Restore argv — reuse the existing pg_restore path

`build_post_start_argv()`'s archive branch is the current `CUSTOM` logic
(`dumps.py:206-235`), unchanged in mechanism:

```
docker cp  <prepared_path>  <cid>:/tmp/dump
docker exec -e PGPASSWORD=… <cid> pg_restore \
    --no-owner --exit-on-error -j N \
    -h 127.0.0.1 -U <user> -d <db>  /tmp/dump
```

`docker cp` copies directories recursively, so an unwrapped directory
flows through untouched. `-j N` (parallel restore) is a real win for
directory dumps (3119 TOC entries here) and is already wired via
`config.dump.restore_jobs` → `resolve_restore_jobs`.

### Strategy resolution

`_decide_strategy` already routes every non-`PLAIN_SQL` format to
`post-start`, and rejects `init-script` for non-SQL. No change needed:
archives are always `post-start`.

## Data flow

```
--from-dump file.tar.gz
   → plan_dump → detect_format (sniff) → DumpPlan(engine=pg_restore, wrapping=gzip+tar)
   → _decide_strategy → "post-start"
   → execute_post_start
        → prepare_archive: gunzip|tar -x → /tmp/…/<dir with toc.dat>
        → build_post_start_argv (archive branch):
             docker cp <dir> cid:/tmp/dump
             docker exec pg_restore -j N … /tmp/dump
        → finally: rmtree temp (log on failure)
```

## Error handling

- Restore step failure → existing `DumpError` with captured stdout/stderr
  and the offending argv (`_run` / `_run_pipe`, `fail_fast`).
- `gunzip`/`tar`/`docker` missing → existing `_require_tool` raises a clear
  `DumpError`.
- A tar with no `toc.dat` and no recognizable archive → `DumpError`
  ("could not locate a pg_dump archive inside <name>").
- Temp-dir cleanup failure → `logger.warning(..., exc_info=True)`; restore
  result is unaffected.

## Testing

Extend `tests/test_dump_loaders.py`, reusing the existing subprocess-stub
pattern (`_stub_subprocess`) so no real `psql`/`pg_restore`/`docker`/server
is required.

- **Detection (content sniff):** plain `.sql`; `.sql.gz`; raw custom
  (`PGDMP` magic) under both `.dump` and a misleading name; `.tar.gz`
  wrapping a directory dump; bare `.tar` directory dump; misleading
  extension routed by content; unknown/empty → `DumpError`.
- **`prepare_archive`:** synthetic tiny `tar.gz` containing
  `<dir>/toc.dat` extracts to a dir whose path ends at the `toc.dat`
  folder; raw `.dump` passes through; temp dir is removed afterward;
  tar-without-toc.dat raises.
- **`build_post_start_argv` / `execute_post_start`:** directory archive
  yields `docker cp` + `docker exec … pg_restore … /tmp/dump`; progress
  emits a "restoring … via pg_restore" line; the no-progress call still
  works (backward compat).

Fixtures are built in-test with `gzip`/`tarfile` from the stdlib — small,
deterministic, no network or Docker.

## Risks / notes

- `docker cp` of a directory with thousands of small files has some
  overhead; acceptable for dev-stack restores (the real dump is ~50 MB
  uncompressed). If it ever matters, copying the tarball in and extracting
  inside the container is a future optimization.
- Content sniffing reads only a small prefix; gzipped archives are
  partially decompressed (bounded) — no full-file scan.
