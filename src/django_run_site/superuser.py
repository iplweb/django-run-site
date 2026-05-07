"""Create or update the dev superuser via ``manage.py shell -c`` (§14.2).

The reason we don't use ``createsuperuser`` is that it's interactive and
doesn't let us set the password and ``is_active``/``is_staff``/``is_superuser``
flags atomically. Going through ``shell -c`` with ``get_user_model()`` works
on every project without requiring ``django_run_site`` in ``INSTALLED_APPS``.
"""

from __future__ import annotations

import json
import textwrap
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from django_run_site.config import RunSiteConfig
from django_run_site.processes import ProcessResult, run_oneshot

# This script runs INSIDE the project's Python via ``manage.py shell -c``.
# Inputs come through env vars to avoid quoting issues; outputs come back
# via stdout as a single JSON line for easy parsing.
SETUP_SCRIPT = textwrap.dedent(
    """
    import json, os
    from django.contrib.auth import get_user_model

    User = get_user_model()
    username = os.environ['DEV_HELPERS_SUPERUSER_USERNAME']
    password = os.environ['DEV_HELPERS_SUPERUSER_PASSWORD']
    email = os.environ['DEV_HELPERS_SUPERUSER_EMAIL']
    overwrite = os.environ.get('DEV_HELPERS_SUPERUSER_OVERWRITE', '0') == '1'
    lookup_field = getattr(User, 'USERNAME_FIELD', 'username')

    user = User.objects.filter(**{lookup_field: username}).first()
    created = False
    if user is None:
        user = User(**{lookup_field: username})
        if hasattr(user, 'email'):
            user.email = email
        user.is_active = True
        user.is_staff = True
        user.is_superuser = True
        user.set_password(password)
        user.save()
        created = True
    elif overwrite:
        if hasattr(user, 'email'):
            user.email = email
        user.is_active = True
        user.is_staff = True
        user.is_superuser = True
        user.set_password(password)
        user.save()

    print('__DEV_HELPERS_SUPERUSER__' + json.dumps({
        'username': username,
        'email': email,
        'created': created,
    }))
    """
).strip()


@dataclass(frozen=True)
class SuperuserResult:
    username: str
    email: str
    created: bool


def setup_superuser(
    *,
    config: RunSiteConfig,
    python: tuple[str, ...],
    manage_py: Path,
    env: Mapping[str, str],
) -> SuperuserResult:
    """Run the superuser setup script via ``manage.py shell -c``."""

    if not config.superuser.enabled:
        raise ValueError("setup_superuser called with superuser.enabled=False")

    sub_env = dict(env)
    sub_env["DEV_HELPERS_SUPERUSER_USERNAME"] = config.superuser.username
    sub_env["DEV_HELPERS_SUPERUSER_PASSWORD"] = config.superuser.password
    sub_env["DEV_HELPERS_SUPERUSER_EMAIL"] = config.superuser.email
    sub_env["DEV_HELPERS_SUPERUSER_OVERWRITE"] = "1" if config.superuser.overwrite else "0"

    argv = (*python, str(manage_py), "shell", "-c", SETUP_SCRIPT)
    result: ProcessResult = run_oneshot(argv, env=sub_env, capture_output=True)
    if not result.ok:
        raise RuntimeError(
            "superuser setup failed:\n"
            f"argv: {argv}\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )

    payload = _extract_payload(result.stdout)
    return SuperuserResult(
        username=str(payload["username"]),
        email=str(payload["email"]),
        created=bool(payload["created"]),
    )


def _extract_payload(stdout: str) -> dict[str, Any]:
    marker = "__DEV_HELPERS_SUPERUSER__"
    for line in stdout.splitlines():
        if marker in line:
            _, _, json_part = line.partition(marker)
            return json.loads(json_part)  # type: ignore[no-any-return]
    raise RuntimeError(
        f"superuser setup script did not emit the expected marker; stdout was:\n{stdout}"
    )
