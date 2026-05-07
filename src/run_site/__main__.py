"""Allow ``python -m run_site`` as an alias for the console script."""

from run_site.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
