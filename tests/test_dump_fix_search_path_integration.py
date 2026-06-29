"""End-to-end: fix_search_path rewrites the stream through real psql.

Requires Docker + a host psql. Excluded from the default unit run
(``-m "not docker"``). A marker row records ``current_setting('search_path')``
at restore time — it is ``public`` only if the sed substitution applied.
"""

from __future__ import annotations

import os
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
def test_fix_search_path_end_to_end(
    minimal_config, tmp_path: Path, fix: bool, expected: str
) -> None:
    dump = tmp_path / "marker.sql"
    dump.write_text(MARKER_DUMP)
    # Postgres only — no need to spin Redis for a restore test.
    config = replace(
        minimal_config,
        dump=replace(minimal_config.dump, fix_search_path=fix),
        redis=replace(minimal_config.redis, enabled=False),
    )
    plan = DumpPlan(path=dump, format=DumpFormat.PLAIN_SQL, strategy="post-start")

    containers = start_containers(config=config, reuse=False, init_script=None)
    try:
        execute_post_start(
            plan,
            config=config,
            pg_host=containers.pg_host,
            pg_port=containers.pg_port,
            container_id=containers.pg_container_id,
        )
        out = subprocess.run(
            [
                "psql",
                "-h",
                str(containers.pg_host),
                "-p",
                str(containers.pg_port),
                "-U",
                config.postgres.user,
                "-d",
                config.postgres.db,
                "-tAc",
                "SELECT val FROM public.sp_marker",
            ],
            env={**os.environ, "PGPASSWORD": config.postgres.password},
            capture_output=True,
            text=True,
            check=True,
        )
        assert out.stdout.strip() == expected
    finally:
        stop_containers(containers)
