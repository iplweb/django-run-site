"""Views for the test_site demo — homepage + healthz probe target."""

from __future__ import annotations

from django.db import connection
from django.http import HttpRequest, HttpResponse, JsonResponse


def home(request: HttpRequest) -> HttpResponse:
    """Smoke-test homepage. Renders a minimal HTML page; intentionally
    contains the project name in the body so integration tests can grep."""

    body = (
        "<!doctype html>\n"
        "<html><head><title>django-run-site test_site</title></head>\n"
        "<body><h1>django-run-site test_site</h1>"
        "<p>This page confirms the orchestrator wired up Django, PG, and "
        "Redis correctly.</p>"
        '<p><a href="/admin/">Admin</a> · '
        '<a href="/healthz/">healthz</a></p>'
        "</body></html>"
    )
    return HttpResponse(body, content_type="text/html")


def healthz(request: HttpRequest) -> JsonResponse:
    """Used by the orchestrator's HTTP probe. Returns 200 once the DB is
    reachable; failures cause 503 so the probe keeps retrying."""

    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except Exception as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=503)
    return JsonResponse({"ok": True})
