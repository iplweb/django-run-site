"""End-to-end: the pv progress-bar pipeline restores data intact.

Requires Docker + host psql + pv. Excluded from the default unit run
(``-m "not docker"``). The bar itself needs a TTY, but this proves that
inserting ``pv`` as the first pipe stage (``pv file | psql``) streams the
dump through correctly — pv is transparent on stdout, bar on stderr.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from run_site import dumps
from run_site.containers import start_containers, stop_containers
from run_site.dumps import DumpFormat, DumpPlan, execute_post_start

pytestmark = [pytest.mark.docker, pytest.mark.integration]

DUMP = "CREATE TABLE public.pv_marker (id int);\nINSERT INTO public.pv_marker VALUES (42);\n"


@pytest.mark.skipif(
    shutil.which("psql") is None or shutil.which("pv") is None,
    reason="needs host psql and pv",
)
def test_progress_bar_pipeline_restores_intact(
    minimal_config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Force the pv path on even though pytest's stderr is not a TTY.
    monkeypatch.setattr(dumps, "_should_show_progress_bar", lambda: True)

    dump = tmp_path / "marker.sql"
    dump.write_text(DUMP)
    config = replace(minimal_config, redis=replace(minimal_config.redis, enabled=False))
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
                "SELECT id FROM public.pv_marker",
            ],
            env={**os.environ, "PGPASSWORD": config.postgres.password},
            capture_output=True,
            text=True,
            check=True,
        )
        assert out.stdout.strip() == "42"
    finally:
        stop_containers(containers)
