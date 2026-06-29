# fix_search_path Dump Filter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an opt-in `[dump].fix_search_path` flag that streams the dump
through `sed` to rewrite `set_config('search_path', '', false)` →
`set_config('search_path', 'public', false)` during restore, across every
restore path, without touching the on-disk dump.

**Architecture:** A single `sed` substitution constant lives in
`dumps.py`. The two-stage restore pipe is generalized to an N-stage `Pipe`
so `gunzip | sed | psql` and `pg_restore -f - | sed | psql` work. Each
`build_post_start_argv` format branch gains a fix path; the binary path
streams `pg_restore -f -` host-side (no `sh -c`), dropping `-j`. The
init-script strategy (plain-SQL-only) mounts a `sed`-filtered temp copy.

**Tech Stack:** Python 3.11+, `subprocess`, `argparse`, `tomllib` (via
existing `config.py` helpers), pytest, ruff, mypy. Toolchain: `uv`.

## Global Constraints

- Default behavior unchanged: with `fix_search_path=false` (the default),
  every restore path produces byte-for-byte the same argv as today.
- The substitution string is exactly (copied verbatim from
  `bpp-deploy/scripts/pg-collation-migrate-3-load.sh`):
  `s/set_config('search_path', '', false)/set_config('search_path', 'public', false)/`
- The filter is streamed; the on-disk dump file is never modified.
- Filter applies to text SQL only (plain, gzipped, and `pg_restore -f -`
  output). init-script is plain-SQL-only — `_decide_strategy` already
  rejects non-plain formats there.
- Ruff: spaces, double quotes, 100-char lines. Tests named `test_*.py`.
  pytest runs with strict markers and warnings-as-errors.
- Lint/type/test gate per change: `uv run ruff format .`,
  `uv run ruff check .`, `uv run mypy src/run_site`,
  `uv run pytest -v -m "not docker" --tb=short`.

---

### Task 1: Config field `DumpConfig.fix_search_path`

**Files:**
- Modify: `src/run_site/config.py` (`DumpConfig` ~103-108; `_build_dump` ~580-596)
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: existing `_bool(raw, key, default)` helper in `config.py`.
- Produces: `DumpConfig.fix_search_path: bool` (default `False`), parsed
  from `[dump].fix_search_path`. Later tasks read
  `config.dump.fix_search_path`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_config.py`:

```python
def test_dump_fix_search_path_defaults_false(tmp_path: Path) -> None:
    cfg = tmp_path / "runsite.toml"
    cfg.write_text('project_slug = "x"\n')
    config = load_config(config_path=cfg, project_root=tmp_path)
    assert config.dump.fix_search_path is False


def test_dump_fix_search_path_parsed(tmp_path: Path) -> None:
    cfg = tmp_path / "runsite.toml"
    cfg.write_text('project_slug = "x"\n[dump]\nfix_search_path = true\n')
    config = load_config(config_path=cfg, project_root=tmp_path)
    assert config.dump.fix_search_path is True


def test_dump_fix_search_path_must_be_bool(tmp_path: Path) -> None:
    cfg = tmp_path / "runsite.toml"
    cfg.write_text('project_slug = "x"\n[dump]\nfix_search_path = "yes"\n')
    with pytest.raises(ConfigError):
        load_config(config_path=cfg, project_root=tmp_path)
```

Confirm the test file already imports `ConfigError` and `pytest` (it
does — used by `test_invalid_dump_strategy_rejected`).

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_config.py -k fix_search_path -v`
Expected: FAIL — `AttributeError: 'DumpConfig' object has no attribute 'fix_search_path'` (first two) and no error raised (third).

- [ ] **Step 3: Add the field and parsing**

In `src/run_site/config.py`, add the field to `DumpConfig`:

```python
@dataclass(frozen=True)
class DumpConfig:
    default_path: str | None = None
    strategy: DumpStrategy = "auto"
    restore_jobs: int | str = "auto"
    fail_fast: bool = True
    fix_search_path: bool = False
```

In `_build_dump`, add the parse to the returned `DumpConfig(...)`:

```python
    return DumpConfig(
        default_path=_opt_str(raw, "default_path"),
        strategy=strategy,  # type: ignore[arg-type]
        restore_jobs=restore_jobs_raw,
        fail_fast=_bool(raw, "fail_fast", default=True),
        fix_search_path=_bool(raw, "fix_search_path", default=False),
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_config.py -k fix_search_path -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Lint, type-check, commit**

```bash
uv run ruff format src/run_site/config.py tests/test_config.py
uv run ruff check src/run_site/config.py tests/test_config.py
uv run mypy src/run_site
git add src/run_site/config.py tests/test_config.py
git commit -m "Add [dump].fix_search_path config flag"
```

---

### Task 2: Generalize the restore pipe to N stages (`Pipe`)

This is a behavior-preserving refactor: it replaces the stringly-typed
`"__pipe__"` encoding (single split at `psql`) with a `Pipe` dataclass and
an N-stage `_run_pipe`, and converts the existing gzip branch to use it.
No `fix_search_path` logic yet. All existing tests must stay green (after
their stubs are updated to the new `_run_pipe` signature).

**Files:**
- Modify: `src/run_site/dumps.py` (`build_post_start_argv` gzip branch
  ~350-365; `_run_argvs` ~452-477; `_run_pipe` ~489-516; add `Pipe`,
  `_describe_pipe`)
- Modify: `tests/test_dump_loaders.py` (`_stub_subprocess` ~242-256,
  `_capture_argvs` ~435-449)
- Test: `tests/test_dump_loaders.py`

**Interfaces:**
- Produces:
  - `class Pipe` (frozen dataclass) with `stages: tuple[Sequence[str], ...]`.
  - `build_post_start_argv(...) -> list[Sequence[str] | Pipe]` (return
    element type widened).
  - `_run_pipe(stages: Sequence[Sequence[str]], *, env: dict[str, str],
    fail_fast: bool) -> None` (signature changed from `(left, right, ...)`).
- Consumes (later tasks): Task 3 builds `Pipe(stages=(...))` instances.

- [ ] **Step 1: Write the failing test for N-stage `_run_pipe`**

Add to `tests/test_dump_loaders.py` (top-level, near the other
`_run`/`_run_pipe` tests):

```python
def test_run_pipe_chains_three_stages(tmp_path: Path) -> None:
    """gunzip-style 3-stage pipe: stage output feeds the next stage's stdin.
    Use real shell tools that are always present: cat | tr | tee."""
    from run_site.dumps import _run_pipe

    out = tmp_path / "out.txt"
    src = tmp_path / "in.txt"
    src.write_text("abc")
    # cat in.txt | tr a-z A-Z | tee out.txt  (last stage writes out.txt)
    _run_pipe(
        [["cat", str(src)], ["tr", "a-z", "A-Z"], ["tee", str(out)]],
        env=dict(__import__("os").environ),
        fail_fast=True,
    )
    assert out.read_text() == "ABC"


def test_run_pipe_raises_when_a_stage_fails(tmp_path: Path) -> None:
    from run_site.dumps import _run_pipe
    from run_site.errors import DumpError

    with pytest.raises(DumpError, match="Piped restore failed"):
        _run_pipe(
            [["cat", str(tmp_path / "does-not-exist")], ["cat"]],
            env=dict(__import__("os").environ),
            fail_fast=True,
        )
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_dump_loaders.py -k run_pipe -v`
Expected: FAIL — current `_run_pipe(left, right, ...)` rejects a single
list arg / the 3-stage call.

- [ ] **Step 3: Add `Pipe`, rewrite `_run_pipe`, `_run_argvs`, gzip branch, add `_describe_pipe`**

In `src/run_site/dumps.py`, add the dataclass near `DumpPlan` (after its
definition, ~line 76):

```python
@dataclass(frozen=True)
class Pipe:
    """An N-stage shell-free pipeline: stages[i].stdout -> stages[i+1].stdin.

    Replaces the old ``("__pipe__", *left, *right)`` tuple encoding so a
    middle ``sed`` filter can sit between ``gunzip``/``pg_restore`` and
    ``psql``."""

    stages: tuple[Sequence[str], ...]
```

Change `build_post_start_argv`'s return annotation:

```python
def build_post_start_argv(
    ...
) -> list[Sequence[str] | Pipe]:
```

Replace the GZIPPED_SQL branch (currently the `"__pipe__"` tuple) with:

```python
    if plan.format is DumpFormat.GZIPPED_SQL:
        psql = _require_tool("psql")
        psql_argv = (psql, *base_env_args, "-v", "ON_ERROR_STOP=1")
        return [Pipe(stages=(("gunzip", "-c", str(plan.path)), psql_argv))]
```

Replace `_run_argvs`'s loop body (the `if argv[0] == "__pipe__"` block)
with an `isinstance` dispatch:

```python
    for argv in argvs:
        if isinstance(argv, Pipe):
            emit("dump", _describe_pipe(argv, plan=plan, size_label=size_label))
            _run_pipe(argv.stages, env=env, fail_fast=config.dump.fail_fast)
        else:
            emit("dump", _describe_step(argv, plan=plan, size_label=size_label))
            _run(list(argv), env=env, fail_fast=config.dump.fail_fast)
```

Update `_run_argvs`'s parameter annotation:
`argvs: list[Sequence[str] | Pipe]`.

Replace `_run_pipe` entirely:

```python
def _run_pipe(
    stages: Sequence[Sequence[str]],
    *,
    env: dict[str, str],
    fail_fast: bool,
) -> None:
    """Run ``stages`` as a pipeline: each stage's stdout is the next
    stage's stdin. The final stage's stdout/stderr are captured for error
    reporting. Raises :class:`DumpError` if any stage exits non-zero and
    ``fail_fast`` is set (pipefail semantics)."""

    if not stages:
        return
    procs: list[subprocess.Popen[bytes]] = []
    prev_stdout = None
    for stage in stages[:-1]:
        proc = subprocess.Popen(list(stage), env=env, stdin=prev_stdout, stdout=subprocess.PIPE)
        if prev_stdout is not None:
            # Parent closes its copy so a downstream exit propagates SIGPIPE.
            prev_stdout.close()
        procs.append(proc)
        prev_stdout = proc.stdout
    last = subprocess.run(
        list(stages[-1]),
        env=env,
        stdin=prev_stdout,
        check=False,
        capture_output=True,
        text=True,
    )
    if prev_stdout is not None:
        prev_stdout.close()
    codes = [(list(stage), proc.wait()) for stage, proc in zip(stages, procs)]
    codes.append((list(stages[-1]), last.returncode))
    if any(rc != 0 for _, rc in codes) and fail_fast:
        detail = "\n".join(f"  exit {rc}: {argv}" for argv, rc in codes)
        raise DumpError(
            f"Piped restore failed:\n{detail}\n"
            f"stdout:\n{last.stdout}\nstderr:\n{last.stderr}"
        )
```

Add `_describe_pipe` (and a stage-label helper) near `_describe_step`:

```python
def _pipe_stage_label(stage: Sequence[str]) -> str:
    head = Path(stage[0]).name
    if head == "docker" and "pg_restore" in stage:
        return "pg_restore"
    return head


def _describe_pipe(pipe: Pipe, *, plan: DumpPlan, size_label: str) -> str:
    names = " | ".join(_pipe_stage_label(s) for s in pipe.stages)
    return f"[dump] loading {plan.path.name} ({size_label}) via {names}…"
```

- [ ] **Step 4: Update the existing test stubs to the new signature**

In `tests/test_dump_loaders.py`, update `_stub_subprocess`'s
`fake_run_pipe`:

```python
    def fake_run_pipe(stages: Any, *, env: Any, fail_fast: bool) -> None:
        return None
```

And `_capture_argvs`'s `fake_run_pipe`:

```python
    def fake_run_pipe(stages: Any, *, env: Any, fail_fast: bool) -> None:
        calls.append(["__pipe__", *[tok for stage in stages for tok in stage]])
```

(The flattened `"__pipe__"`-prefixed list keeps existing gzip assertions —
which look for `"gunzip"` / `"psql"` substrings — working.)

- [ ] **Step 5: Run the full dump + config suite**

Run: `uv run pytest tests/test_dump_loaders.py tests/test_config.py -v -m "not docker"`
Expected: PASS — including the two new `_run_pipe` tests and the
pre-existing gzip/custom progress tests.

- [ ] **Step 6: Lint, type-check, commit**

```bash
uv run ruff format src/run_site/dumps.py tests/test_dump_loaders.py
uv run ruff check src/run_site/dumps.py tests/test_dump_loaders.py
uv run mypy src/run_site
git add src/run_site/dumps.py tests/test_dump_loaders.py
git commit -m "Generalize restore pipe to N stages via Pipe dataclass"
```

---

### Task 3: `SEARCH_PATH_SED` + fix branches in `build_post_start_argv`

**Files:**
- Modify: `src/run_site/dumps.py` (`build_post_start_argv` all three
  format branches; add `SEARCH_PATH_SED` constant; `logger`)
- Test: `tests/test_dump_loaders.py`

**Interfaces:**
- Consumes: `config.dump.fix_search_path` (Task 1), `Pipe` (Task 2),
  `_require_tool` (existing).
- Produces: the exact `SEARCH_PATH_SED` constant; fix-on argv shapes:
  - plain → `[Pipe(stages=((sed, SEARCH_PATH_SED, file), psql_argv))]`
  - gzip → `[Pipe(stages=((gunzip,-c,file), (sed, SEARCH_PATH_SED), psql_argv))]`
  - binary → `[cp_argv, Pipe(stages=((docker,exec,cid,pg_restore,--no-owner,-f,-,/tmp/dump), (sed, SEARCH_PATH_SED), psql_argv))]`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_dump_loaders.py`. Use a helper to flip the flag on a
config:

```python
def _with_fix(config):
    from dataclasses import replace

    return replace(config, dump=replace(config.dump, fix_search_path=True))


def test_search_path_sed_constant_matches_bpp() -> None:
    from run_site.dumps import SEARCH_PATH_SED

    assert SEARCH_PATH_SED == (
        "s/set_config('search_path', '', false)/"
        "set_config('search_path', 'public', false)/"
    )


def test_build_plain_sql_fix_pipes_through_sed(monkeypatch, tmp_path, minimal_config) -> None:
    from run_site.dumps import Pipe

    monkeypatch.setattr("run_site.dumps._require_tool", lambda tool: f"/fake/bin/{tool}")
    dump = tmp_path / "baseline.sql"
    dump.write_text("-- sql\n")
    plan = DumpPlan(path=dump, format=DumpFormat.PLAIN_SQL, strategy="post-start")

    argvs = build_post_start_argv(
        plan=plan, config=_with_fix(minimal_config),
        pg_host="127.0.0.1", pg_port=5432, container_id=None,
    )
    assert len(argvs) == 1 and isinstance(argvs[0], Pipe)
    stages = argvs[0].stages
    assert stages[0][0] == "/fake/bin/sed"
    assert stages[0][1].startswith("s/set_config('search_path', '', false)")
    assert str(dump) == stages[0][2]
    assert stages[-1][0] == "/fake/bin/psql"
    # No -f file arg on psql — it reads sed's stdout.
    assert "-f" not in stages[-1]


def test_build_plain_sql_without_fix_unchanged(monkeypatch, tmp_path, minimal_config) -> None:
    monkeypatch.setattr("run_site.dumps._require_tool", lambda tool: f"/fake/bin/{tool}")
    dump = tmp_path / "baseline.sql"
    dump.write_text("-- sql\n")
    plan = DumpPlan(path=dump, format=DumpFormat.PLAIN_SQL, strategy="post-start")
    argvs = build_post_start_argv(
        plan=plan, config=minimal_config,
        pg_host="127.0.0.1", pg_port=5432, container_id=None,
    )
    # Unchanged fast path: a single psql -f tuple.
    assert argvs == [("/fake/bin/psql", "-h", "127.0.0.1", "-p", "5432", "-U",
                      "demo", "-d", "demo", "-v", "ON_ERROR_STOP=1", "-f", str(dump))]


def test_build_gzip_fix_inserts_sed_between_gunzip_and_psql(
    monkeypatch, tmp_path, minimal_config
) -> None:
    from run_site.dumps import Pipe

    monkeypatch.setattr("run_site.dumps._require_tool", lambda tool: f"/fake/bin/{tool}")
    dump = tmp_path / "baseline.sql.gz"
    dump.write_bytes(b"\x1f\x8b\x08\x00")
    plan = DumpPlan(path=dump, format=DumpFormat.GZIPPED_SQL, strategy="post-start")
    argvs = build_post_start_argv(
        plan=plan, config=_with_fix(minimal_config),
        pg_host="127.0.0.1", pg_port=5432, container_id=None,
    )
    assert isinstance(argvs[0], Pipe)
    labels = [s[0] for s in argvs[0].stages]
    assert labels == ["gunzip", "/fake/bin/sed", "/fake/bin/psql"]


def test_build_binary_fix_streams_pg_restore_through_sed_no_jobs(
    monkeypatch, tmp_path, minimal_config
) -> None:
    from run_site.dumps import Pipe

    monkeypatch.setattr("run_site.dumps._require_tool", lambda tool: f"/fake/bin/{tool}")
    plan = DumpPlan(path=tmp_path / "snap.dump", format=DumpFormat.PG_RESTORE, strategy="post-start")
    argvs = build_post_start_argv(
        plan=plan, config=_with_fix(minimal_config),
        pg_host="127.0.0.1", pg_port=5432, container_id="cid-9",
        restore_source=tmp_path / "snap.dump",
    )
    cp, pipe = argvs
    assert cp[1] == "cp" and "cid-9:/tmp/dump" in cp
    assert isinstance(pipe, Pipe)
    restore_stage = pipe.stages[0]
    assert "pg_restore" in restore_stage and "-f" in restore_stage and "-" in restore_stage
    assert "/tmp/dump" in restore_stage
    # Parallel -j must be gone — sed can only filter a serial text stream.
    assert "-j" not in restore_stage
    assert pipe.stages[1][0] == "/fake/bin/sed"
    assert pipe.stages[-1][0] == "/fake/bin/psql"
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_dump_loaders.py -k "fix or sed_constant or unchanged" -v`
Expected: FAIL — `SEARCH_PATH_SED` undefined; fix branches not present.

- [ ] **Step 3: Add the constant and the fix branches**

In `src/run_site/dumps.py`, add the constant near the top (after the
magic-byte constants, ~line 65):

```python
# Restore `public` to the restore-session search_path. Modern pg_dump /
# pg_restore hardens its header (post CVE-2018-1058) with
# `set_config('search_path', '', false)`; an empty path breaks restoring
# objects whose definitions resolve operators/types in `public` eagerly
# (e.g. an hstore comparison in a trigger WHEN clause). Safe because
# pg_dump qualifies every object with its schema. Matches the bpp
# pg-collation-migrate-3-load.sh streaming fix exactly.
SEARCH_PATH_SED = (
    "s/set_config('search_path', '', false)/"
    "set_config('search_path', 'public', false)/"
)
```

Rewrite the three branches of `build_post_start_argv` (keep `base_env_args`
as-is). PLAIN_SQL:

```python
    if plan.format is DumpFormat.PLAIN_SQL:
        psql = _require_tool("psql")
        psql_argv = (psql, *base_env_args, "-v", "ON_ERROR_STOP=1")
        if config.dump.fix_search_path:
            sed = _require_tool("sed")
            return [Pipe(stages=((sed, SEARCH_PATH_SED, str(plan.path)), psql_argv))]
        return [(*psql_argv, "-f", str(plan.path))]
```

GZIPPED_SQL (extends the Task 2 version):

```python
    if plan.format is DumpFormat.GZIPPED_SQL:
        psql = _require_tool("psql")
        psql_argv = (psql, *base_env_args, "-v", "ON_ERROR_STOP=1")
        gunzip_argv = ("gunzip", "-c", str(plan.path))
        if config.dump.fix_search_path:
            sed = _require_tool("sed")
            return [Pipe(stages=(gunzip_argv, (sed, SEARCH_PATH_SED), psql_argv))]
        return [Pipe(stages=(gunzip_argv, psql_argv))]
```

PG_RESTORE:

```python
    if plan.format is DumpFormat.PG_RESTORE:
        if container_id is None:
            raise DumpError(
                "pg_restore-format dumps require a known container id "
                "(docker cp + pg_restore inside the container)."
            )
        docker = _require_tool("docker")
        source = restore_source if restore_source is not None else plan.path
        cp_argv = (docker, "cp", str(source), f"{container_id}:/tmp/dump")
        if config.dump.fix_search_path:
            sed = _require_tool("sed")
            psql = _require_tool("psql")
            logger.warning(
                "fix_search_path is enabled: streaming %s through sed disables "
                "parallel -j restore (the archive is converted to a serial SQL "
                "stream).",
                plan.path.name,
            )
            # pg_restore -f - only converts the archive to SQL on stdout; it
            # does not connect to a DB, so it needs no PGPASSWORD. The host
            # psql (same as plain/gzip) loads the filtered stream.
            restore_stage = (
                docker, "exec", container_id,
                "pg_restore", "--no-owner", "-f", "-", "/tmp/dump",
            )
            psql_argv = (psql, *base_env_args, "-v", "ON_ERROR_STOP=1")
            return [cp_argv, Pipe(stages=(restore_stage, (sed, SEARCH_PATH_SED), psql_argv))]
        jobs = resolve_restore_jobs(config.dump.restore_jobs)
        return [
            cp_argv,
            (
                docker,
                "exec",
                "-e",
                f"PGPASSWORD={config.postgres.password}",
                container_id,
                "pg_restore",
                "--no-owner",
                "--exit-on-error",
                "-j",
                str(jobs),
                "-h",
                "127.0.0.1",
                "-U",
                config.postgres.user,
                "-d",
                config.postgres.db,
                "/tmp/dump",
            ),
        ]
```

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/test_dump_loaders.py -v -m "not docker"`
Expected: PASS (new fix tests + all existing tests).

- [ ] **Step 5: Lint, type-check, commit**

```bash
uv run ruff format src/run_site/dumps.py tests/test_dump_loaders.py
uv run ruff check src/run_site/dumps.py tests/test_dump_loaders.py
uv run mypy src/run_site
git add src/run_site/dumps.py tests/test_dump_loaders.py
git commit -m "Apply search_path sed filter across all restore formats"
```

---

### Task 4: init-script filtered copy + header no-op warning

**Files:**
- Modify: `src/run_site/dumps.py` (add `write_search_path_filtered`,
  `prepared_init_script`, `_dump_has_empty_search_path`; call the warning
  in `execute_post_start`)
- Test: `tests/test_dump_loaders.py`

**Interfaces:**
- Consumes: `SEARCH_PATH_SED`, `_require_tool`, `_read_head`,
  `_gunzip_prefix`, `DumpFormat`, `DumpPlan` (all existing in `dumps.py`),
  `tempfile`, `os`, `contextmanager`, `Iterator` (already imported).
- Produces:
  - `write_search_path_filtered(src: Path, dst: Path) -> None`
  - `prepared_init_script(path: Path | None, *, fix_search_path: bool) ->
    Iterator[Path | None]` (a `@contextmanager`; yields a filtered temp
    copy when enabled, else `path` unchanged; cleans up the temp file)
  - `_dump_has_empty_search_path(plan: DumpPlan) -> bool` (header peek)
  - `execute_post_start` emits a `logger.warning` when the flag is on but
    the header lacks the pattern (plain/gzip only).

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_dump_loaders.py`:

```python
def test_write_search_path_filtered_rewrites_line(tmp_path: Path) -> None:
    from run_site.dumps import write_search_path_filtered

    src = tmp_path / "in.sql"
    src.write_text(
        "SELECT pg_catalog.set_config('search_path', '', false);\n"
        "CREATE TABLE public.t (id int);\n"
    )
    dst = tmp_path / "out.sql"
    write_search_path_filtered(src, dst)
    text = dst.read_text()
    assert "set_config('search_path', 'public', false)" in text
    assert "set_config('search_path', '', false)" not in text
    assert "CREATE TABLE public.t (id int);" in text
    # Source file is never modified.
    assert "set_config('search_path', '', false)" in src.read_text()


def test_prepared_init_script_passthrough_when_disabled(tmp_path: Path) -> None:
    from run_site.dumps import prepared_init_script

    src = tmp_path / "baseline.sql"
    src.write_text("-- x\n")
    with prepared_init_script(src, fix_search_path=False) as p:
        assert p == src


def test_prepared_init_script_none_passthrough() -> None:
    from run_site.dumps import prepared_init_script

    with prepared_init_script(None, fix_search_path=True) as p:
        assert p is None


def test_prepared_init_script_filters_and_cleans_up(tmp_path: Path) -> None:
    from run_site.dumps import prepared_init_script

    src = tmp_path / "baseline.sql"
    src.write_text("SELECT pg_catalog.set_config('search_path', '', false);\n")
    captured: Path | None = None
    with prepared_init_script(src, fix_search_path=True) as p:
        assert p is not None and p != src
        captured = p
        assert "set_config('search_path', 'public', false)" in p.read_text()
    # Temp filtered copy removed on context exit.
    assert captured is not None and not captured.exists()


def test_execute_post_start_warns_on_missing_pattern(
    monkeypatch, tmp_path, minimal_config, caplog
) -> None:
    import logging
    from dataclasses import replace

    _stub_subprocess(monkeypatch)
    dump = tmp_path / "baseline.sql"
    dump.write_text("-- no search_path line here\n")
    plan = DumpPlan(path=dump, format=DumpFormat.PLAIN_SQL, strategy="post-start")
    config = replace(minimal_config, dump=replace(minimal_config.dump, fix_search_path=True))
    with caplog.at_level(logging.WARNING, logger="run_site.dumps"):
        execute_post_start(plan, config=config, pg_host="127.0.0.1", pg_port=5432, container_id=None)
    assert any("no-op" in r.message or "no set_config" in r.message.lower()
               for r in caplog.records), caplog.records
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_dump_loaders.py -k "filtered or prepared_init or warns_on_missing" -v`
Expected: FAIL — symbols undefined; no warning emitted.

- [ ] **Step 3: Implement the helpers and the warning**

In `src/run_site/dumps.py`, add after `_require_tool` (or near
`prepared_archive`):

```python
def write_search_path_filtered(src: Path, dst: Path) -> None:
    """Write a copy of *src* to *dst* with the search_path fix applied via
    ``sed`` (single source of truth with the streaming restore paths). The
    source file is never modified."""

    sed = _require_tool("sed")
    with open(dst, "wb") as out:
        proc = subprocess.run([sed, SEARCH_PATH_SED, str(src)], stdout=out, check=False)
    if proc.returncode != 0:
        raise DumpError(f"sed failed filtering {src} for init-script (exit {proc.returncode}).")


@contextmanager
def prepared_init_script(path: Path | None, *, fix_search_path: bool) -> Iterator[Path | None]:
    """Yield the init-script path to bind-mount. With ``fix_search_path``,
    yield a ``sed``-filtered temp copy (removed on exit); otherwise yield
    *path* unchanged. The temp copy must outlive container creation, which
    is why callers scope this around ``start_containers`` (PG runs the
    init script before that returns)."""

    if path is None or not fix_search_path:
        yield path
        return
    fd, tmp = tempfile.mkstemp(prefix="run-site-initdb-", suffix=".sql")
    os.close(fd)
    tmp_path = Path(tmp)
    try:
        write_search_path_filtered(path, tmp_path)
        yield tmp_path
    finally:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass  # already removed, not an error


def _dump_has_empty_search_path(plan: DumpPlan) -> bool:
    """Peek the dump header for pg_dump's empty-search_path statement.
    Binary archives generate it at restore time (not present in the raw
    file), so return True there to suppress the no-op warning."""

    if plan.format is DumpFormat.PG_RESTORE:
        return True
    needle = b"set_config('search_path', '', false)"
    if plan.format is DumpFormat.GZIPPED_SQL:
        head = _gunzip_prefix(plan.path, 65536)
    else:
        head = _read_head(plan.path, 65536)
    return needle in head
```

In `execute_post_start`, at the top (after `size_label` is computed,
before the format branches), add:

```python
    if config.dump.fix_search_path and not _dump_has_empty_search_path(plan):
        logger.warning(
            "fix_search_path is enabled but %s's header has no "
            "set_config('search_path', '', false) line — the fix is a no-op "
            "(the dump format may have changed).",
            plan.path.name,
        )
```

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/test_dump_loaders.py -k "filtered or prepared_init or warns_on_missing" -v`
Expected: PASS

- [ ] **Step 5: Lint, type-check, commit**

```bash
uv run ruff format src/run_site/dumps.py tests/test_dump_loaders.py
uv run ruff check src/run_site/dumps.py tests/test_dump_loaders.py
uv run mypy src/run_site
git add src/run_site/dumps.py tests/test_dump_loaders.py
git commit -m "Add init-script search_path filtering and no-op warning"
```

---

### Task 5: CLI `--fix-search-path` flag

The CLI override function is `_apply_cli_overrides(config, opts)`
(cli.py). Its unit tests in `tests/test_cli_dry_run.py` build
`argparse.Namespace(...)` objects with exactly the fields the function
reads (`postgres_image`, `redis_image`, `bind`, `restore_jobs`,
`no_install`). Because the function will now also read
`opts.fix_search_path`, **every existing Namespace passed to it must gain
`fix_search_path=None`** or it raises `AttributeError`. There are 6 of
them (around lines 114, 136, 158, 181, 203, 226).

**Files:**
- Modify: `src/run_site/cli.py` (Dump arg group, after `--restore-jobs`
  ~292; `_apply_cli_overrides` restore_jobs merge ~1195-1197)
- Modify: `tests/test_cli_dry_run.py` (add `fix_search_path=None` to the 6
  existing `_apply_cli_overrides` Namespaces; add the new override test)

**Interfaces:**
- Consumes: `argparse`, `replace` (imported locally inside
  `_apply_cli_overrides`), `config.dump.fix_search_path` (Task 1).
- Produces: argparse sets `opts.fix_search_path: bool | None` (default
  `None`); `_apply_cli_overrides` overrides `config.dump.fix_search_path`
  only when it is not `None`.

- [ ] **Step 1: Update existing Namespaces + write the failing test**

In `tests/test_cli_dry_run.py`, add `fix_search_path=None,` to each of the
6 `argparse.Namespace(...)` blocks passed to `_apply_cli_overrides` (the
ones currently ending with `no_install=False,`). Then add the new test
next to `test_apply_cli_overrides_restore_jobs_overrides_config`:

```python
def test_apply_cli_overrides_fix_search_path(minimal_config) -> None:
    """--fix-search-path / --no-fix-search-path override config; absence
    (None) leaves the config value untouched."""

    import argparse
    from dataclasses import replace

    from run_site.cli import _apply_cli_overrides

    base = dict(
        postgres_image=None, redis_image=None, bind=None,
        restore_jobs=None, no_install=False,
    )
    # Flag on overrides a config that has it off.
    on = _apply_cli_overrides(minimal_config, argparse.Namespace(fix_search_path=True, **base))
    assert on.dump.fix_search_path is True

    # Flag off overrides a config that has it on.
    cfg_on = replace(minimal_config, dump=replace(minimal_config.dump, fix_search_path=True))
    off = _apply_cli_overrides(cfg_on, argparse.Namespace(fix_search_path=False, **base))
    assert off.dump.fix_search_path is False

    # Absent (None) leaves config as-is.
    unset = _apply_cli_overrides(cfg_on, argparse.Namespace(fix_search_path=None, **base))
    assert unset.dump.fix_search_path is True
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_cli_dry_run.py -k "fix_search_path or apply_cli_overrides" -v`
Expected: FAIL — the new test errors (`AttributeError: ... fix_search_path`
inside `_apply_cli_overrides`).

- [ ] **Step 3: Add the argument and the merge**

In `src/run_site/cli.py`, in the Dump argument group (after
`--restore-jobs`, ~line 292):

```python
    dump.add_argument(
        "--fix-search-path",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "Stream the dump through sed to restore 'public' to the restore "
            "search_path (fixes 'operator does not exist: public.hstore = "
            "public.hstore' on PG16+ restores). Overrides [dump].fix_search_path."
        ),
    )
```

In `_apply_cli_overrides`, extend the existing `restore_jobs` merge
(~1195-1197) — add only the two `fix_search_path` lines:

```python
    dump = config.dump
    if opts.restore_jobs is not None:
        dump = replace(dump, restore_jobs=opts.restore_jobs)
    if opts.fix_search_path is not None:
        dump = replace(dump, fix_search_path=opts.fix_search_path)
```

(Keep the single `dump` flowing into the final
`replace(config, ..., dump=dump)` return.)

- [ ] **Step 4: Run to verify it passes (and existing overrides tests stay green)**

Run: `uv run pytest tests/test_cli_dry_run.py -v`
Expected: PASS — the new test plus all 6 updated-Namespace tests.

- [ ] **Step 5: Lint, type-check, commit**

```bash
uv run ruff format src/run_site/cli.py tests/test_cli_dry_run.py
uv run ruff check src/run_site/cli.py tests/test_cli_dry_run.py
uv run mypy src/run_site
git add src/run_site/cli.py tests/test_cli_dry_run.py
git commit -m "Add --fix-search-path / --no-fix-search-path CLI flag"
```

---

### Task 6: Wire init-script filtering into the run flow

**Files:**
- Modify: `src/run_site/cli.py` (import ~42; `_run_command_inner` around
  the `init_script = _maybe_init_script(...)` at ~558 and the
  `start_containers(...)` call at ~574-579)
- Test: `tests/test_containers.py` style — but the wiring is in `cli.py`,
  so test the `prepared_init_script`+`start_containers` integration with
  the existing `FakePgLauncher`.

**Interfaces:**
- Consumes: `prepared_init_script` (Task 4), `config.dump.fix_search_path`
  (Task 1), `FakePgLauncher` (existing test helper).
- Produces: the init-script bind-mount path is a filtered temp copy when
  `fix_search_path` is on; the temp file is cleaned up after
  `start_containers` returns.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_containers.py` (it already imports `start_containers`
and defines `FakePgLauncher`):

```python
def test_prepared_init_script_feeds_filtered_copy_to_launcher(
    minimal_config, tmp_path: Path
) -> None:
    """With fix_search_path on, the path handed to the PG launcher is a
    filtered temp copy whose search_path line is rewritten — and it still
    exists at launch time (cleanup happens only after start)."""
    from dataclasses import replace

    from run_site.dumps import prepared_init_script

    src = tmp_path / "baseline.sql"
    src.write_text("SELECT pg_catalog.set_config('search_path', '', false);\n")
    config = replace(minimal_config, dump=replace(minimal_config.dump, fix_search_path=True))

    pg = FakePgLauncher()
    redis = FakeRedisLauncher()
    with prepared_init_script(src, fix_search_path=config.dump.fix_search_path) as init_script:
        start_containers(
            config=config, reuse=False, init_script=init_script,
            pg_launcher=pg, redis_launcher=redis,
        )
        mounted = pg.started[0]["init_script"]
        assert mounted is not None and mounted != src
        assert mounted.exists()  # present while the container is being created
        assert "set_config('search_path', 'public', false)" in mounted.read_text()
    assert not mounted.exists()  # removed after the with-block
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_containers.py -k prepared_init_script_feeds -v`
Expected: FAIL — `cannot import name 'prepared_init_script'` only if Task 4
wasn't done; if Task 4 is done this test should already PASS (it exercises
the dumps-level helper, not cli). If it PASSES here, that's fine — it locks
the contract the cli wiring depends on. Proceed to wire cli.py anyway.

- [ ] **Step 3: Wire `prepared_init_script` into `cli.py`**

Update the import at `src/run_site/cli.py:42`:

```python
from run_site.dumps import execute_post_start, plan_dump, prepared_init_script
```

Rename the assignment at ~558 and wrap the `start_containers` call
(~574-579):

```python
    init_script_src = _maybe_init_script(config=config, opts=opts)
```

```python
    with prepared_init_script(
        init_script_src, fix_search_path=config.dump.fix_search_path
    ) as init_script:
        containers = start_containers(
            config=config,
            reuse=opts.reuse,
            init_script=init_script,
            progress=mux.write,
        )
```

(`init_script_src` is only consumed here; `init_script` is the possibly-
filtered path. `containers` stays bound after the `with` exits — Python
does not unbind it. The sqlite block between the old 558 and 574 is
untouched.)

- [ ] **Step 4: Run the full suite (no docker)**

Run: `uv run pytest -v -m "not docker" --tb=short`
Expected: PASS — the new test plus every existing test (regression check
for the cli wiring change).

- [ ] **Step 5: Lint, type-check, commit**

```bash
uv run ruff format src/run_site/cli.py tests/test_containers.py
uv run ruff check src/run_site/cli.py tests/test_containers.py
uv run mypy src/run_site
git add src/run_site/cli.py tests/test_containers.py
git commit -m "Mount sed-filtered init-script copy when fix_search_path is on"
```

---

### Task 7: Documentation + example TOML

**Files:**
- Modify: `examples/runsite.bpp.toml` (the `[dump]` section, ~25-29)
- Modify: the dump/config docs page under `docs/` (find with
  `grep -rln "restore_jobs\|\\[dump\\]\|default_path" docs/`)
- Test: none (docs); a config-parse smoke test already exists if the
  example is loaded in tests — if so, run it.

**Interfaces:** none (documentation only).

- [ ] **Step 1: Document the option in the example TOML**

In `examples/runsite.bpp.toml`, extend the `[dump]` section:

```toml
[dump]
default_path = "src/baseline-sql/baseline.sql"
strategy = "auto"
restore_jobs = 8
fail_fast = true
# Stream the dump through sed during restore to restore 'public' to the
# session search_path (modern pg_dump emits an empty search_path, which
# breaks restoring objects that resolve public operators/types eagerly —
# e.g. 'operator does not exist: public.hstore = public.hstore' on PG16+).
# Off by default; the on-disk dump is never modified. For binary archives
# this disables parallel -j restore.
fix_search_path = true
```

- [ ] **Step 2: Document the option in `docs/`**

Find the dump docs: `grep -rln "restore_jobs" docs/`. In that page, add a
`fix_search_path` entry next to `restore_jobs` / `fail_fast`, with: type
`bool`, default `false`; what it does (the sed substitution, verbatim);
that it covers plain, gzipped, binary (`pg_restore -f -`), and init-script
restores; that binary loses `-j`; and the `--fix-search-path` /
`--no-fix-search-path` CLI overrides. Keep the prose consistent with the
existing option descriptions on that page.

- [ ] **Step 3: Verify example/doc consistency**

Run: `grep -rn "fix_search_path" examples/ docs/`
Expected: the new lines appear in both the example and the docs page.
If a test loads `examples/runsite.bpp.toml`, run it:
`uv run pytest -v -m "not docker" -k bpp` (skip if no such test).

- [ ] **Step 4: Commit**

```bash
git add examples/runsite.bpp.toml docs/
git commit -m "Document [dump].fix_search_path option"
```

---

### Task 8: Docker integration test (real restore through the sed pipe)

Marked `docker` + `integration`, so it is excluded from the default CI run
(`-m "not docker"`). It proves the sed actually rewrites the stream through
a real `psql` against a real Postgres container, without needing an hstore
repro: a marker row records `current_setting('search_path')` at restore
time, which is `public` only if the substitution applied.

**Files:**
- Create: `tests/test_dump_fix_search_path_integration.py`

**Interfaces:**
- Consumes: `start_containers`, `stop_containers` (real launchers, real
  daemon), `execute_post_start`, `DumpPlan`, `DumpFormat`, host `psql`.

- [ ] **Step 1: Write the test**

```python
"""End-to-end: fix_search_path rewrites the stream through real psql.

Requires Docker + a host psql. Excluded from the default unit run.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from run_site.containers import start_containers, stop_containers
from run_site.dumps import DumpFormat, DumpPlan, execute_post_start

pytestmark = [pytest.mark.docker, pytest.mark.integration]

MARKER_DUMP = (
    "SELECT pg_catalog.set_config('search_path', '', false);\n"
    "CREATE TABLE public.sp_marker (val text);\n"
    "INSERT INTO public.sp_marker VALUES (current_setting('search_path'));\n"
)


@pytest.mark.skipif(shutil.which("psql") is None, reason="needs host psql")
@pytest.mark.parametrize("fix,expected", [(True, "public"), (False, "")])
def test_fix_search_path_end_to_end(minimal_config, tmp_path: Path, fix, expected) -> None:
    dump = tmp_path / "marker.sql"
    dump.write_text(MARKER_DUMP)
    config = replace(minimal_config, dump=replace(minimal_config.dump, fix_search_path=fix))
    plan = DumpPlan(path=dump, format=DumpFormat.PLAIN_SQL, strategy="post-start")

    containers = start_containers(config=config, reuse=False, init_script=None)
    try:
        execute_post_start(
            plan, config=config,
            pg_host=containers.pg_host, pg_port=containers.pg_port,
            container_id=containers.pg_container_id,
        )
        out = subprocess.run(
            [
                "psql", "-h", containers.pg_host, "-p", str(containers.pg_port),
                "-U", config.postgres.user, "-d", config.postgres.db,
                "-tAc", "SELECT val FROM public.sp_marker",
            ],
            env={"PGPASSWORD": config.postgres.password, "PATH": __import__("os").environ["PATH"]},
            capture_output=True, text=True, check=True,
        )
        assert out.stdout.strip() == expected
    finally:
        stop_containers(containers)
```

- [ ] **Step 2: Run it (requires Docker)**

Run: `uv run pytest tests/test_dump_fix_search_path_integration.py -v -m "docker and integration"`
Expected: PASS — 2 params: fix=True → `public`, fix=False → `` (empty).
If Docker is unavailable, this task is deferred; note it in the PR.

- [ ] **Step 3: Confirm default CI run still excludes it**

Run: `uv run pytest -v -m "not docker" --tb=short`
Expected: PASS, and the integration test is NOT collected.

- [ ] **Step 4: Lint, commit**

```bash
uv run ruff format tests/test_dump_fix_search_path_integration.py
uv run ruff check tests/test_dump_fix_search_path_integration.py
git add tests/test_dump_fix_search_path_integration.py
git commit -m "Add docker integration test for fix_search_path restore"
```

---

## Final verification

- [ ] `uv run ruff format . && uv run ruff check .`
- [ ] `uv run mypy src/run_site`
- [ ] `uv run pytest -v -m "not docker" --tb=short`
- [ ] (If Docker available) `uv run pytest -v -m "docker"`
- [ ] `uv run run-site --help` shows `--fix-search-path` / `--no-fix-search-path`

## Self-review notes (author)

- **Spec coverage:** config flag (T1), N-stage pipe (T2), sed across
  plain/gzip/binary with `-j` drop (T3), init-script filtered copy +
  header no-op warning + `-j` notice (T3/T4), CLI flag (T5), run-flow
  wiring (T6), docs/example (T7), integration proof (T8). All spec
  sections map to a task.
- **Type consistency:** `Pipe.stages` is the only new type; `_run_pipe`
  takes `stages` everywhere (T2 definition, T2 test stubs, T3 consumers).
  `build_post_start_argv -> list[Sequence[str] | Pipe]` consumed by
  `_run_argvs` with the matching annotation.
- **CLI override naming:** verified against the codebase — the function is
  `_apply_cli_overrides` (not `_apply_cli`), tested via direct
  `argparse.Namespace` construction. Task 5 updates the 6 existing
  Namespaces so the new `opts.fix_search_path` read does not break them.
- **No placeholders:** every code step contains real code; the only
  deliberately deferred detail is the exact `docs/` page path in Task 7
  (resolved by the inline `grep` command).
