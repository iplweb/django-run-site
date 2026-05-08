# Repository Guidelines

## Project Structure & Module Organization

This is a Python 3.11+ CLI package named `run-site`. Source code lives in
`src/run_site/`; keep CLI orchestration, Docker/testcontainers handling, hooks,
sidecar config, and process management in focused modules there. Tests live in
`tests/` and mirror behavior by feature, for example `test_config.py`,
`test_sidecar.py`, and `test_from_git.py`. User-facing documentation is in
`docs/`, while runnable examples and fixture Django projects are under
`examples/`. Packaging, lint, type-check, and pytest settings are centralized in
`pyproject.toml`.

## Build, Test, and Development Commands

The project uses [`uv`](https://docs.astral.sh/uv/) as its primary toolchain
(`uv.lock` is committed). Prefer the `uv` commands below; the `pip` variants
are listed only as a fallback when `uv` is unavailable.

- `uv sync --all-extras`: install the package plus pytest, ruff, and mypy
  for local development. Fallback: `python -m pip install -e ".[dev]"`.
- `uv run pytest -v -m "not docker" --tb=short`: run the default CI-style
  unit suite without Docker-dependent tests.
- `uv run pytest -v`: run the full test suite; Docker must be running for
  tests marked `docker`.
- `uv run ruff check .` and `uv run ruff format .`: lint and format the
  repository.
- `uv run mypy src/run_site`: type-check the package.
- `uv run pre-commit install`: enable the repository hooks locally.
- `uv run pre-commit run --all-files`: run the same formatting and safety
  hooks before committing.
- `uv run run-site --help`: smoke-test the installed console entry point.

## Coding Style & Naming Conventions

Use Ruff formatting with spaces, double quotes, and a 100-character line length.
Prefer typed, small functions with clear boundaries around subprocess, Docker,
and filesystem work. Test files should be named `test_*.py`, and test functions
should describe the behavior being verified. Keep the CLI independent from
Django imports; it should shell out to project `manage.py` commands rather than
importing Django itself.

## Testing Guidelines

Pytest is configured with strict markers and warnings-as-errors. Use existing
markers when appropriate: `docker` for tests needing a Docker daemon,
`integration` for subprocess/end-to-end behavior, and `slow` for longer tests.
Prefer focused unit tests for config parsing, command construction, and
environment handling; add integration coverage when behavior crosses process or
container boundaries.

## Commit & Pull Request Guidelines

The current history uses short, imperative commit subjects such as `Add
Architecture section` and release commits like `Release v0.4.0`. Keep commits
focused and describe the visible change. Pull requests should include a concise
summary, tests run, related issue links when available, and documentation or
example updates for user-facing CLI/config changes.

## Security & Configuration Tips

Do not commit generated runtime files such as `.run-site-config`, local virtual
environments, secrets, database dumps, or private keys. When changing config
behavior, update `docs/` and relevant `examples/runsite*.toml` files so users
and agents can reproduce the intended setup.
