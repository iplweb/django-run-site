"""Allow ``python -m django_run_site`` as an alias for the console script."""

from django_run_site.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
