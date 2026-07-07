# Live-reload + browser tab reuse Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** On restart, already-open browser tabs refresh themselves instead of new tabs being opened, via SSE live-reload injected by django-dev-helpers plus symmetric tab-open suppression in both packages.

**Architecture:** django-dev-helpers gains a per-boot `BOOT_ID`, an SSE endpoint `/__dev_reload__/` and a `/__dev_reload__/clients` count endpoint (served via a new auto-installed `LiveReloadMiddleware` that also injects a tiny `<script>` into every `text/html` response). The injected client reloads the page when it reconnects and sees a new `BOOT_ID`. Before opening a browser, each opener (dev-helpers in-process, run-site over HTTP) samples the live-connection count across a grace window and skips opening if any tab is already connected.

**Tech Stack:** Python 3.11+, Django (dev runserver, WSGI), Server-Sent Events (`StreamingHttpResponse`), `EventSource` (browser), `urllib` (run-site HTTP probe, no Django import), pytest, ruff, mypy.

## Global Constraints

- Two repos. django-dev-helpers = `~/Programowanie/django-dev-helpers` (repo **DH**). django-run-site = the repo this plan lives in (repo **RS**).
- Python 3.11+. Ruff formatting: spaces, double quotes, 100-char line length.
- No bare `except:` / `except Exception: pass`. Every handler logs, re-raises, raises different, or returns meaningful value. Narrow exception types with a justifying comment when suppressing.
- RS rule: the CLI must NOT import Django. run-site's live-tab probe uses only `urllib`/`json`.
- Live-reload must never run in production: gate every active path on `settings.DEBUG` **and** `cfg.is_active()` **and** `cfg.live_reload.enabled`.
- The `/__dev_reload__/` contract is additive and backward-compatible: old DH doesn't serve it; new RS treats the resulting 404/unreachable as "open normally". Packages release independently.
- DH tests run: `cd ~/Programowanie/django-dev-helpers && uv run pytest -q`. RS tests run: `uv run pytest -q -m "not docker"`. Both: `uv run ruff format . && uv run ruff check .` before each commit.
- DH config namespace pattern: add defaults dict → add to `_DEFAULTS` → add to `_validate` → `self.x = _dict_to_namespace(merged["x"])` in `DevHelpersConfig.__init__`. Tests reset config via the autouse `_reset_config` fixture (`reset_config()`), and override via `override_settings(DJANGO_DEV_HELPERS={...})` + constructing `DevHelpersConfig()`.

---

## Setup (once, before Phase A)

- [ ] Create a feature branch in DH:

```bash
cd ~/Programowanie/django-dev-helpers && git checkout -b feat/live-reload-tab-reuse
```

RS is already on branch `feat/live-reload-tab-reuse` with the design spec committed.

---

## Phase A — django-dev-helpers

### Task A1: `live_reload` config namespace

**Repo:** DH

**Files:**
- Modify: `src/django_dev_helpers/conf.py`
- Test: `tests/test_conf_validation.py`

**Interfaces:**
- Produces: `cfg.live_reload.enabled: bool` (default `True`), `cfg.live_reload.reuse_tabs: bool` (default `True`), `cfg.live_reload.grace_seconds: float` (default `2.0`).

- [ ] **Step 1: Write the failing tests** — append to `tests/test_conf_validation.py`:

```python
def test_live_reload_defaults():
    from django_dev_helpers.conf import DevHelpersConfig

    with override_settings(DJANGO_DEV_HELPERS={"enabled": True}):
        cfg = DevHelpersConfig()
    assert cfg.live_reload.enabled is True
    assert cfg.live_reload.reuse_tabs is True
    assert cfg.live_reload.grace_seconds == 2.0


def test_live_reload_enabled_must_be_bool():
    from django_dev_helpers.conf import DevHelpersConfig

    with (
        override_settings(DJANGO_DEV_HELPERS={"enabled": True, "live_reload": {"enabled": "yes"}}),
        pytest.raises(ImproperlyConfigured, match=r"live_reload.*enabled"),
    ):
        DevHelpersConfig()


def test_live_reload_grace_seconds_must_be_number():
    from django_dev_helpers.conf import DevHelpersConfig

    with (
        override_settings(DJANGO_DEV_HELPERS={"enabled": True, "live_reload": {"grace_seconds": "soon"}}),
        pytest.raises(ImproperlyConfigured, match=r"live_reload.*grace_seconds"),
    ):
        DevHelpersConfig()
```

- [ ] **Step 2: Run to verify failure**

Run: `cd ~/Programowanie/django-dev-helpers && uv run pytest tests/test_conf_validation.py -q -k live_reload`
Expected: FAIL (`AttributeError: ... 'live_reload'` / no validation).

- [ ] **Step 3: Implement** in `src/django_dev_helpers/conf.py`.

Add the defaults block next to `_BROWSER_OPEN_DEFAULTS`:

```python
_LIVE_RELOAD_DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "reuse_tabs": True,
    "grace_seconds": 2.0,
}
```

Add it to `_DEFAULTS`:

```python
    "browser_open": _BROWSER_OPEN_DEFAULTS,
    "live_reload": _LIVE_RELOAD_DEFAULTS,
```

Add validation at the end of `_validate`:

```python
    live_reload = merged["live_reload"]
    if not isinstance(live_reload["enabled"], bool):
        raise ImproperlyConfigured("DJANGO_DEV_HELPERS['live_reload']['enabled'] must be a bool.")
    if not isinstance(live_reload["reuse_tabs"], bool):
        raise ImproperlyConfigured("DJANGO_DEV_HELPERS['live_reload']['reuse_tabs'] must be a bool.")
    grace = live_reload["grace_seconds"]
    if isinstance(grace, bool) or not isinstance(grace, (int, float)) or grace < 0:
        raise ImproperlyConfigured(
            "DJANGO_DEV_HELPERS['live_reload']['grace_seconds'] must be a non-negative number."
        )
```

Assign it in `DevHelpersConfig.__init__` after `self.browser_open = ...`:

```python
        self.live_reload = _dict_to_namespace(merged["live_reload"])
```

- [ ] **Step 4: Run to verify pass**

Run: `cd ~/Programowanie/django-dev-helpers && uv run pytest tests/test_conf_validation.py -q -k live_reload`
Expected: PASS (3 tests).

- [ ] **Step 5: Format, lint, commit**

```bash
cd ~/Programowanie/django-dev-helpers && uv run ruff format . && uv run ruff check . \
  && git add src/django_dev_helpers/conf.py tests/test_conf_validation.py \
  && git commit -m "Add live_reload config namespace"
```

---

### Task A2: `live_reload.py` core — boot id, registry, injection

**Repo:** DH

**Files:**
- Create: `src/django_dev_helpers/live_reload.py`
- Test: `tests/test_live_reload.py`

**Interfaces:**
- Produces: `BOOT_ID: str`; `register() -> int`; `unregister(cid: int) -> None`; `count() -> int`; `_reset() -> None` (test helper); `CLIENT_SCRIPT: str`; `inject(html: str) -> str`.

- [ ] **Step 1: Write the failing tests** — create `tests/test_live_reload.py`:

```python
from django_dev_helpers import live_reload


def test_boot_id_is_stable_nonempty():
    assert isinstance(live_reload.BOOT_ID, str)
    assert live_reload.BOOT_ID
    assert live_reload.BOOT_ID == live_reload.BOOT_ID


def test_registry_tracks_connections():
    live_reload._reset()
    assert live_reload.count() == 0
    a = live_reload.register()
    b = live_reload.register()
    assert live_reload.count() == 2
    live_reload.unregister(a)
    assert live_reload.count() == 1
    live_reload.unregister(b)
    assert live_reload.count() == 0


def test_unregister_unknown_is_noop():
    live_reload._reset()
    live_reload.unregister(999)
    assert live_reload.count() == 0


def test_inject_inserts_script_before_body():
    html = "<html><body>hi</body></html>"
    out = live_reload.inject(html)
    assert "EventSource" in out
    assert out.index("EventSource") < out.rindex("</body>")


def test_inject_noop_without_body():
    html = "<p>no body tag</p>"
    assert live_reload.inject(html) == html


def test_inject_uses_last_body_case_insensitive():
    html = "<BODY>x</BODY>"
    out = live_reload.inject(html)
    assert "EventSource" in out
    assert out.rindex("EventSource") < out.rindex("</BODY>")
```

- [ ] **Step 2: Run to verify failure**

Run: `cd ~/Programowanie/django-dev-helpers && uv run pytest tests/test_live_reload.py -q`
Expected: FAIL (`ModuleNotFoundError: ... live_reload`).

- [ ] **Step 3: Implement** — create `src/django_dev_helpers/live_reload.py`:

```python
from __future__ import annotations

import secrets
import threading

# One id per server process. Changes on every full restart AND on every
# autoreload child (a page reload on code change is a desirable side effect).
BOOT_ID = secrets.token_hex(8)

_lock = threading.Lock()
_connections: set[int] = set()
_next_id = 0

# Inline client. Opens an EventSource; remembers the first boot_id it sees;
# reloads when a later "hello" reports a different boot_id. EventSource
# auto-reconnects on error, so a restart transparently re-attaches.
CLIENT_SCRIPT = (
    "<script>(function(){var b=null;"
    'var s=new EventSource("/__dev_reload__/");'
    's.addEventListener("hello",function(e){'
    "var i=JSON.parse(e.data).boot_id;"
    "if(b===null){b=i;}else if(i!==b){location.reload();}});})();</script>"
)


def register() -> int:
    global _next_id
    with _lock:
        _next_id += 1
        cid = _next_id
        _connections.add(cid)
        return cid


def unregister(cid: int) -> None:
    with _lock:
        _connections.discard(cid)


def count() -> int:
    with _lock:
        return len(_connections)


def _reset() -> None:
    """Test helper: clear the connection registry."""
    with _lock:
        _connections.clear()


def inject(html: str) -> str:
    """Insert CLIENT_SCRIPT immediately before the last </body> (case-
    insensitive). Returns html unchanged when there is no </body>."""
    idx = html.lower().rfind("</body>")
    if idx == -1:
        return html
    return html[:idx] + CLIENT_SCRIPT + html[idx:]
```

- [ ] **Step 4: Run to verify pass**

Run: `cd ~/Programowanie/django-dev-helpers && uv run pytest tests/test_live_reload.py -q`
Expected: PASS (6 tests).

- [ ] **Step 5: Format, lint, commit**

```bash
cd ~/Programowanie/django-dev-helpers && uv run ruff format . && uv run ruff check . \
  && git add src/django_dev_helpers/live_reload.py tests/test_live_reload.py \
  && git commit -m "Add live_reload core: boot id, connection registry, script injection"
```

---

### Task A3: SSE + clients response builders

**Repo:** DH

**Files:**
- Modify: `src/django_dev_helpers/live_reload.py`
- Test: `tests/test_live_reload.py`

**Interfaces:**
- Produces: `event_stream(heartbeat_interval: float = 2.0) -> Iterator[bytes]` (generator; registers on first `next()`, unregisters in `finally`); `sse_response() -> StreamingHttpResponse`; `clients_response() -> JsonResponse`; `request_shutdown() -> None` (stops heartbeat loops).

- [ ] **Step 1: Write the failing tests** — append to `tests/test_live_reload.py`:

```python
import json


def test_event_stream_first_chunk_has_boot_id_and_retry():
    live_reload._reset()
    live_reload._shutdown.clear()
    gen = live_reload.event_stream()
    first = next(gen).decode()
    assert "event: hello" in first
    assert "retry:" in first
    assert live_reload.BOOT_ID in first
    gen.close()


def test_event_stream_registers_and_unregisters():
    live_reload._reset()
    live_reload._shutdown.clear()
    gen = live_reload.event_stream()
    assert live_reload.count() == 0
    next(gen)
    assert live_reload.count() == 1
    gen.close()
    assert live_reload.count() == 0


def test_sse_response_headers():
    live_reload._shutdown.clear()
    resp = live_reload.sse_response()
    assert resp["Content-Type"] == "text/event-stream"
    assert resp["Cache-Control"] == "no-cache"
    resp.streaming_content.close()


def test_clients_response_reports_count():
    live_reload._reset()
    resp = live_reload.clients_response()
    assert json.loads(resp.content) == {"count": 0}
    cid = live_reload.register()
    resp = live_reload.clients_response()
    assert json.loads(resp.content) == {"count": 1}
    live_reload.unregister(cid)
```

- [ ] **Step 2: Run to verify failure**

Run: `cd ~/Programowanie/django-dev-helpers && uv run pytest tests/test_live_reload.py -q -k "event_stream or sse_response or clients_response"`
Expected: FAIL (`AttributeError: ... event_stream`).

- [ ] **Step 3: Implement** — add to `src/django_dev_helpers/live_reload.py`.

Add imports at the top (below existing imports):

```python
import json
from collections.abc import Iterator
```

Add the shutdown event next to the registry globals:

```python
_shutdown = threading.Event()
```

Append the response machinery:

```python
def request_shutdown() -> None:
    """Signal open event_stream generators to stop heartbeating promptly."""
    _shutdown.set()


def event_stream(heartbeat_interval: float = 2.0) -> Iterator[bytes]:
    cid = register()
    try:
        payload = json.dumps({"boot_id": BOOT_ID})
        yield f"retry: 1000\nevent: hello\ndata: {payload}\n\n".encode()
        while not _shutdown.is_set():
            # wait() returns True when shutdown is set (stop), False on
            # timeout (send a heartbeat). Short waits keep runserver
            # shutdown from blocking on this thread for long.
            if _shutdown.wait(heartbeat_interval):
                break
            yield b": ping\n\n"
    finally:
        unregister(cid)


def sse_response():
    from django.http import StreamingHttpResponse

    response = StreamingHttpResponse(event_stream(), content_type="text/event-stream")
    response["Cache-Control"] = "no-cache"
    response["X-Accel-Buffering"] = "no"
    return response


def clients_response():
    from django.http import JsonResponse

    return JsonResponse({"count": count()})
```

- [ ] **Step 4: Run to verify pass**

Run: `cd ~/Programowanie/django-dev-helpers && uv run pytest tests/test_live_reload.py -q`
Expected: PASS (all).

- [ ] **Step 5: Format, lint, commit**

```bash
cd ~/Programowanie/django-dev-helpers && uv run ruff format . && uv run ruff check . \
  && git add src/django_dev_helpers/live_reload.py tests/test_live_reload.py \
  && git commit -m "Add SSE stream, clients count, and shutdown signalling for live_reload"
```

---

### Task A4: `LiveReloadMiddleware` — endpoint interception + HTML injection

**Repo:** DH

**Files:**
- Modify: `src/django_dev_helpers/middleware.py`
- Test: `tests/test_live_reload_middleware.py`

**Interfaces:**
- Consumes: `live_reload.sse_response`, `live_reload.clients_response`, `live_reload.inject` (Task A2/A3); `cfg.is_active()`, `cfg.live_reload.enabled` (Task A1).
- Produces: `django_dev_helpers.middleware.LiveReloadMiddleware`.

- [ ] **Step 1: Write the failing tests** — create `tests/test_live_reload_middleware.py`:

```python
import json

from django.http import HttpResponse
from django.test import RequestFactory, override_settings

from django_dev_helpers.conf import reset_config
from django_dev_helpers.middleware import LiveReloadMiddleware

rf = RequestFactory()


def _html_view(request):
    return HttpResponse("<html><body>hi</body></html>", content_type="text/html")


def test_sse_path_intercepted():
    mw = LiveReloadMiddleware(_html_view)
    resp = mw(rf.get("/__dev_reload__/"))
    assert resp["Content-Type"] == "text/event-stream"
    resp.streaming_content.close()


def test_clients_path_returns_count():
    mw = LiveReloadMiddleware(_html_view)
    resp = mw(rf.get("/__dev_reload__/clients"))
    assert resp.status_code == 200
    assert "count" in json.loads(resp.content)


def test_script_injected_into_html():
    mw = LiveReloadMiddleware(_html_view)
    resp = mw(rf.get("/"))
    assert b"EventSource" in resp.content


def test_content_length_updated_after_injection():
    def view(request):
        resp = HttpResponse("<html><body>hi</body></html>", content_type="text/html")
        resp["Content-Length"] = str(len(resp.content))
        return resp

    mw = LiveReloadMiddleware(view)
    resp = mw(rf.get("/"))
    assert int(resp["Content-Length"]) == len(resp.content)


def test_no_injection_for_non_html():
    def view(request):
        return HttpResponse('{"a": 1}', content_type="application/json")

    mw = LiveReloadMiddleware(view)
    resp = mw(rf.get("/"))
    assert b"EventSource" not in resp.content


@override_settings(DEBUG=False)
def test_no_injection_when_debug_off():
    reset_config()
    mw = LiveReloadMiddleware(_html_view)
    resp = mw(rf.get("/"))
    assert b"EventSource" not in resp.content


@override_settings(DJANGO_DEV_HELPERS={"enabled": True, "live_reload": {"enabled": False}})
def test_disabled_config_skips_everything():
    reset_config()
    mw = LiveReloadMiddleware(_html_view)
    assert b"EventSource" not in mw(rf.get("/")).content
    # endpoint falls through to the wrapped view, not intercepted
    assert mw(rf.get("/__dev_reload__/")).content == b"<html><body>hi</body></html>"
```

- [ ] **Step 2: Run to verify failure**

Run: `cd ~/Programowanie/django-dev-helpers && uv run pytest tests/test_live_reload_middleware.py -q`
Expected: FAIL (`ImportError: ... LiveReloadMiddleware`).

- [ ] **Step 3: Implement** — append to `src/django_dev_helpers/middleware.py`:

```python
class LiveReloadMiddleware:
    """Serve the /__dev_reload__/ SSE + clients endpoints and inject the
    live-reload client script into text/html responses.

    Auto-installed by apps.ready() when live_reload.enabled. Every active
    path is gated on settings.DEBUG so this never runs in production, even
    if the middleware is left in MIDDLEWARE by mistake.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def _live(self, cfg):
        from django.conf import settings

        return settings.DEBUG and cfg.is_active() and cfg.live_reload.enabled

    def __call__(self, request):
        from . import live_reload
        from .conf import get_config

        cfg = get_config()
        if self._live(cfg):
            if request.path == "/__dev_reload__/":
                return live_reload.sse_response()
            if request.path == "/__dev_reload__/clients":
                return live_reload.clients_response()

        response = self.get_response(request)

        if self._live(cfg):
            self._maybe_inject(response, live_reload)
        return response

    def _maybe_inject(self, response, live_reload):
        if getattr(response, "streaming", False):
            return
        if not response.get("Content-Type", "").startswith("text/html"):
            return
        try:
            body = response.content.decode(response.charset)
        except (UnicodeDecodeError, LookupError):
            # Undecodable body — leave it untouched rather than corrupt it.
            return
        new_body = live_reload.inject(body)
        if new_body == body:
            return
        response.content = new_body.encode(response.charset)
        if response.has_header("Content-Length"):
            response["Content-Length"] = str(len(response.content))
```

- [ ] **Step 4: Run to verify pass**

Run: `cd ~/Programowanie/django-dev-helpers && uv run pytest tests/test_live_reload_middleware.py -q`
Expected: PASS (7 tests).

- [ ] **Step 5: Format, lint, commit**

```bash
cd ~/Programowanie/django-dev-helpers && uv run ruff format . && uv run ruff check . \
  && git add src/django_dev_helpers/middleware.py tests/test_live_reload_middleware.py \
  && git commit -m "Add LiveReloadMiddleware: serve dev-reload endpoints, inject client script"
```

---

### Task A5: Auto-install `LiveReloadMiddleware` in `apps.ready()`

**Repo:** DH

**Files:**
- Modify: `src/django_dev_helpers/apps.py`
- Test: `tests/test_apps_ready.py`

**Interfaces:**
- Consumes: `LiveReloadMiddleware` (Task A4), `cfg.live_reload.enabled` (Task A1).
- Produces: `django_dev_helpers.apps.install_live_reload_middleware_if_enabled(cfg) -> None`.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_apps_ready.py`:

```python
def test_install_live_reload_middleware_appends_when_enabled():
    from django_dev_helpers.apps import install_live_reload_middleware_if_enabled
    from django_dev_helpers.conf import get_config

    mw = "django_dev_helpers.middleware.LiveReloadMiddleware"
    with override_settings(MIDDLEWARE=[], DJANGO_DEV_HELPERS={"enabled": True}):
        reset_config()
        install_live_reload_middleware_if_enabled(get_config())
        from django.conf import settings

        assert mw in settings.MIDDLEWARE


def test_install_live_reload_middleware_skipped_when_disabled():
    from django_dev_helpers.apps import install_live_reload_middleware_if_enabled
    from django_dev_helpers.conf import get_config

    mw = "django_dev_helpers.middleware.LiveReloadMiddleware"
    with override_settings(
        MIDDLEWARE=[], DJANGO_DEV_HELPERS={"enabled": True, "live_reload": {"enabled": False}}
    ):
        reset_config()
        install_live_reload_middleware_if_enabled(get_config())
        from django.conf import settings

        assert mw not in settings.MIDDLEWARE


def test_install_live_reload_middleware_idempotent():
    from django_dev_helpers.apps import install_live_reload_middleware_if_enabled
    from django_dev_helpers.conf import get_config

    mw = "django_dev_helpers.middleware.LiveReloadMiddleware"
    with override_settings(MIDDLEWARE=[mw], DJANGO_DEV_HELPERS={"enabled": True}):
        reset_config()
        install_live_reload_middleware_if_enabled(get_config())
        from django.conf import settings

        assert settings.MIDDLEWARE.count(mw) == 1
```

Confirm the imports `override_settings` and `reset_config` are already present at the top of `tests/test_apps_ready.py`; if not, add `from django.test import override_settings` and `from django_dev_helpers.conf import reset_config`.

- [ ] **Step 2: Run to verify failure**

Run: `cd ~/Programowanie/django-dev-helpers && uv run pytest tests/test_apps_ready.py -q -k live_reload`
Expected: FAIL (`ImportError: ... install_live_reload_middleware_if_enabled`).

- [ ] **Step 3: Implement** in `src/django_dev_helpers/apps.py`.

Add a module-level constant next to `_AUTOLOGIN_MIDDLEWARE`:

```python
_LIVE_RELOAD_MIDDLEWARE = "django_dev_helpers.middleware.LiveReloadMiddleware"
```

Add the installer function next to `install_autologin_middleware_if_enabled`:

```python
def install_live_reload_middleware_if_enabled(cfg) -> None:
    """Append ``LiveReloadMiddleware`` to ``settings.MIDDLEWARE`` when
    live-reload is enabled, so the /__dev_reload__/ endpoints are served and
    the client script is injected without the user wiring anything."""
    if not cfg.live_reload.enabled:
        return

    from django.conf import settings

    raw = getattr(settings, "MIDDLEWARE", None) or []
    middleware = list(raw)
    if _LIVE_RELOAD_MIDDLEWARE in middleware:
        return

    middleware.append(_LIVE_RELOAD_MIDDLEWARE)
    settings.MIDDLEWARE = middleware
    logger.debug("django-dev-helpers: auto-installed %s", _LIVE_RELOAD_MIDDLEWARE)
```

Call it in `DjangoDevHelpersConfig.ready()`, immediately after the existing `install_autologin_middleware_if_enabled(cfg)` call:

```python
        install_autologin_middleware_if_enabled(cfg)
        install_live_reload_middleware_if_enabled(cfg)
```

- [ ] **Step 4: Run to verify pass**

Run: `cd ~/Programowanie/django-dev-helpers && uv run pytest tests/test_apps_ready.py -q`
Expected: PASS.

- [ ] **Step 5: Format, lint, commit**

```bash
cd ~/Programowanie/django-dev-helpers && uv run ruff format . && uv run ruff check . \
  && git add src/django_dev_helpers/apps.py tests/test_apps_ready.py \
  && git commit -m "Auto-install LiveReloadMiddleware on app ready"
```

---

### Task A6: Suppress dev-helpers' own browser-open when a live tab exists

**Repo:** DH

**Files:**
- Modify: `src/django_dev_helpers/browser.py`
- Test: `tests/test_browser_probe.py`

**Interfaces:**
- Consumes: `live_reload.count` (Task A2), `cfg.live_reload.{enabled,reuse_tabs,grace_seconds}` (Task A1).
- Produces: `browser.existing_live_tab(cfg) -> bool`. Behavior change: `wait_for_http` skips `open_browser` when `existing_live_tab(cfg)` is True.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_browser_probe.py`. First extend the `_make_cfg` helper so cfgs carry a `live_reload` namespace (edit the existing helper):

```python
def _make_cfg(
    probe_timeout_seconds=0.1,
    autologin_enabled=False,
    browser_url_path=None,
    live_reload_enabled=True,
    reuse_tabs=True,
    grace_seconds=1.0,
):
    return SimpleNamespace(
        browser_open=SimpleNamespace(
            enabled=True,
            url_path=browser_url_path,
            probe_path="/admin/login/",
            probe_timeout_seconds=probe_timeout_seconds,
        ),
        autologin=SimpleNamespace(enabled=autologin_enabled, url_path="dev-helpers/autologin/"),
        live_reload=SimpleNamespace(
            enabled=live_reload_enabled,
            reuse_tabs=reuse_tabs,
            grace_seconds=grace_seconds,
        ),
    )
```

Then add:

```python
def test_existing_live_tab_true_when_client_connected():
    cfg = _make_cfg(grace_seconds=1.0)
    with (
        patch.object(browser.live_reload, "count", return_value=1),
        patch.object(browser.time, "sleep"),
    ):
        assert browser.existing_live_tab(cfg) is True


def test_existing_live_tab_false_when_no_clients():
    cfg = _make_cfg(grace_seconds=1.0)
    with (
        patch.object(browser.live_reload, "count", return_value=0),
        patch.object(browser.time, "sleep"),
    ):
        assert browser.existing_live_tab(cfg) is False


def test_existing_live_tab_false_when_reuse_disabled():
    cfg = _make_cfg(reuse_tabs=False)
    with patch.object(browser.live_reload, "count", return_value=5) as count:
        assert browser.existing_live_tab(cfg) is False
    count.assert_not_called()


def test_wait_for_http_skips_open_when_live_tab_present():
    cfg = _make_cfg()
    response = MagicMock()
    response.status = 200
    with (
        patch.object(browser.urllib.request, "urlopen", return_value=response),
        patch.object(browser, "existing_live_tab", return_value=True),
        patch.object(browser, "open_browser") as open_browser,
        patch.object(browser.time, "sleep"),
    ):
        browser.wait_for_http(cfg)
    open_browser.assert_not_called()
```

- [ ] **Step 2: Run to verify failure**

Run: `cd ~/Programowanie/django-dev-helpers && uv run pytest tests/test_browser_probe.py -q -k "live_tab or skips_open"`
Expected: FAIL (`AttributeError: ... existing_live_tab`).

- [ ] **Step 3: Implement** in `src/django_dev_helpers/browser.py`.

Add the import near the top:

```python
from django_dev_helpers import live_reload
```

Add the helper:

```python
def existing_live_tab(cfg) -> bool:
    """True if a live-reload client is already connected (so a surviving tab
    will reload itself and we should not open a new one). Samples the count
    across grace_seconds to bridge the brief unregister/re-register gap when a
    tab reconnects and immediately reloads."""
    if not (cfg.live_reload.enabled and cfg.live_reload.reuse_tabs):
        return False
    grace = cfg.live_reload.grace_seconds
    samples = max(1, int(grace / 0.5))
    for i in range(samples):
        if live_reload.count() >= 1:
            return True
        if i < samples - 1:
            time.sleep(0.5)
    return live_reload.count() >= 1
```

In `wait_for_http`, replace **both** `open_browser(cfg); return` occurrences (the 200 branch and the `< 500` HTTPError branch) with a call to a local guard:

```python
                _open_unless_reused(cfg)
                return
```

and add the guard function above `wait_for_http`:

```python
def _open_unless_reused(cfg) -> None:
    if existing_live_tab(cfg):
        logger.info(
            "django-dev-helpers: existing tab detected — refreshing in place, "
            "not opening a new browser tab"
        )
        return
    open_browser(cfg)
```

- [ ] **Step 4: Run to verify pass**

Run: `cd ~/Programowanie/django-dev-helpers && uv run pytest tests/test_browser_probe.py -q`
Expected: PASS (existing + new).

- [ ] **Step 5: Format, lint, commit**

```bash
cd ~/Programowanie/django-dev-helpers && uv run ruff format . && uv run ruff check . \
  && git add src/django_dev_helpers/browser.py tests/test_browser_probe.py \
  && git commit -m "Suppress dev-helpers browser-open when a live tab is already connected"
```

---

### Task A7: dev-helpers docs + full suite

**Repo:** DH

**Files:**
- Modify: `README.md` (or the appropriate section under `docs/`)

- [ ] **Step 1: Document the feature.** Add a "Live reload / tab reuse" section covering: what it does (already-open tabs reload on restart; no duplicate tabs); the `live_reload` config knobs (`enabled`, `reuse_tabs`, `grace_seconds`); the `/__dev_reload__/` + `/__dev_reload__/clients` endpoints; the CSP caveat (inline script may be blocked by a strict `script-src` in DEBUG — disable with `live_reload.enabled = False`); and the "closed one of two tabs won't be reopened" compromise.

- [ ] **Step 2: Run the full DH suite + lint/type**

Run: `cd ~/Programowanie/django-dev-helpers && uv run pytest -q && uv run ruff check . && uv run mypy src/django_dev_helpers`
Expected: all green.

- [ ] **Step 3: Commit**

```bash
cd ~/Programowanie/django-dev-helpers && git add -A \
  && git commit -m "Document live-reload / browser tab reuse"
```

---

## Phase B — django-run-site

### Task B1: `[django]` reuse knobs

**Repo:** RS

**Files:**
- Modify: `src/run_site/config.py` (the `DjangoConfig` dataclass + `_build_django` / `_from_*` builder around lines 640–665)
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `config.django.reuse_browser_tab: bool` (default `True`); `config.django.browser_reuse_grace: float` (default `2.0`).

- [ ] **Step 1: Write the failing tests** — add to `tests/test_config.py` (follow the file's existing TOML-loading style; adapt the loader call to whatever helper the file already uses, e.g. `load_config_from_toml`):

```python
def test_django_reuse_browser_tab_defaults_true(tmp_path):
    cfg = _load_minimal(tmp_path)  # use this file's existing config loader
    assert cfg.django.reuse_browser_tab is True
    assert cfg.django.browser_reuse_grace == 2.0


def test_django_reuse_browser_tab_can_be_disabled(tmp_path):
    cfg = _load_minimal(tmp_path, django={"reuse_browser_tab": False, "browser_reuse_grace": 0.5})
    assert cfg.django.reuse_browser_tab is False
    assert cfg.django.browser_reuse_grace == 0.5
```

(If `tests/test_config.py` has no `_load_minimal` helper, mirror the loader pattern used by the nearest existing `[django]` test — e.g. the ones covering `open_browser` — and pass the `[django]` table with the two new keys.)

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_config.py -q -k reuse_browser`
Expected: FAIL (`AttributeError: ... reuse_browser_tab`).

- [ ] **Step 3: Implement** in `src/run_site/config.py`.

Add fields to the `DjangoConfig` dataclass (next to `open_browser`):

```python
    # After the server is up, if django-dev-helpers' live-reload reports an
    # already-connected tab, refresh it in place instead of opening a new one.
    reuse_browser_tab: bool = True
    browser_reuse_grace: float = 2.0
```

In the `DjangoConfig` builder (where `open_browser` is parsed, ~line 655), add to the returned `DjangoConfig(...)`:

```python
        reuse_browser_tab=_bool(raw, "reuse_browser_tab", default=True),
        browser_reuse_grace=float(raw.get("browser_reuse_grace", 2.0)),
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_config.py -q -k reuse_browser`
Expected: PASS.

- [ ] **Step 5: Format, lint, commit**

```bash
uv run ruff format . && uv run ruff check . \
  && git add src/run_site/config.py tests/test_config.py \
  && git commit -m "Add [django].reuse_browser_tab / browser_reuse_grace knobs"
```

---

### Task B2: run-site queries `/__dev_reload__/clients` and skips opening

**Repo:** RS

**Files:**
- Modify: `src/run_site/cli.py` (`_probe_and_open_browser` ~1141, `_resolve_browser_decision` ~1111)
- Test: `tests/test_browser_decision.py`

**Interfaces:**
- Consumes: `config.django.reuse_browser_tab`, `config.django.browser_reuse_grace` (Task B1).
- Produces: `run_site.cli._existing_live_tab(host: str | None, port: int | None, grace: float) -> bool`. Behavior change: `_probe_and_open_browser` skips `webbrowser.open` when a live tab exists; `_resolve_browser_decision` gains a `reuse` suffix.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_browser_decision.py`:

```python
from unittest.mock import patch

from run_site.cli import _existing_live_tab


def test_existing_live_tab_true_on_positive_count():
    payload = b'{"count": 2}'
    resp = MagicMock()
    resp.read.return_value = payload
    resp.__enter__ = lambda s: s
    resp.__exit__ = lambda *a: False
    with (
        patch("run_site.cli.urllib.request.urlopen", return_value=resp),
        patch("run_site.cli.time.sleep"),
    ):
        assert _existing_live_tab("localhost", 8000, 1.0) is True


def test_existing_live_tab_false_on_zero_count():
    payload = b'{"count": 0}'
    resp = MagicMock()
    resp.read.return_value = payload
    resp.__enter__ = lambda s: s
    resp.__exit__ = lambda *a: False
    with (
        patch("run_site.cli.urllib.request.urlopen", return_value=resp),
        patch("run_site.cli.time.sleep"),
    ):
        assert _existing_live_tab("localhost", 8000, 1.0) is False


def test_existing_live_tab_false_when_endpoint_unreachable():
    import urllib.error

    with (
        patch(
            "run_site.cli.urllib.request.urlopen",
            side_effect=urllib.error.URLError("nope"),
        ),
        patch("run_site.cli.time.sleep"),
    ):
        assert _existing_live_tab("localhost", 8000, 1.0) is False
```

Add a MagicMock import at the top of the test file if absent: `from unittest.mock import MagicMock, patch`.

Also add a decision-suffix test:

```python
def test_reuse_suffix_added_when_enabled(config: RunSiteConfig) -> None:
    cfg = replace(config, django=replace(config.django, open_browser=True, reuse_browser_tab=True))
    _, status = _resolve_browser_decision(
        config=cfg, cli_choice=None, signal=LOCAL, homepage=HOMEPAGE
    )
    assert "refresh an existing tab" in status
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_browser_decision.py -q -k "live_tab or reuse_suffix"`
Expected: FAIL (`ImportError: ... _existing_live_tab`).

- [ ] **Step 3: Implement** in `src/run_site/cli.py`.

Confirm the module already imports `time`, `urllib.request`, `urllib.error`, `json`, `urllib.parse`; add any that are missing to the top-level imports.

Add the helper near `_probe_and_open_browser`:

```python
def _existing_live_tab(host: str | None, port: int | None, grace: float) -> bool:
    """True if django-dev-helpers reports an already-connected live-reload
    client for this server, meaning a surviving tab will reload itself and we
    should not open a new one. Samples across ``grace`` seconds; any 404 /
    non-JSON / connection error means "no live-reload → open normally"."""
    if host is None or port is None:
        return False
    url = f"http://{host}:{port}/__dev_reload__/clients"
    samples = max(1, int(grace / 0.5))
    for i in range(samples):
        try:
            with urllib.request.urlopen(url, timeout=1) as resp:
                data = json.loads(resp.read().decode())
            if int(data.get("count", 0)) >= 1:
                return True
        except (urllib.error.URLError, OSError, ValueError):
            # Endpoint absent/unreachable/garbage → treat as "no live tab".
            pass
        if i < samples - 1:
            time.sleep(0.5)
    return False
```

Update `_probe_and_open_browser` to consult it after the probe:

```python
def _probe_and_open_browser(url: str, homepage: str, config: RunSiteConfig) -> None:
    if not wait_for_http(url, timeout=config.django.probe_timeout):
        return
    if config.django.reuse_browser_tab:
        parts = urllib.parse.urlsplit(homepage)
        if _existing_live_tab(parts.hostname, parts.port, config.django.browser_reuse_grace):
            logger.info(
                "existing browser tab detected — refreshing in place, "
                "not opening a new tab"
            )
            return
    try:
        import webbrowser

        webbrowser.open(homepage)
    except Exception:  # pragma: no cover - browser-open is best-effort
        logger.exception("Failed to open browser at %s", homepage)
```

Add the banner suffix in `_resolve_browser_decision`. At the two points that return `True` on the config/auto paths, thread the reuse hint. Simplest: wrap the two success returns so that when `config.django.reuse_browser_tab` is True the status string gets a suffix. Replace:

```python
    if setting is True:
        return True, f"will open {homepage} ([django].open_browser = true)"
    ...
    return True, f"will open {homepage} ({signal.reason})"
```

with a helper applied to both:

```python
    def _reuse_suffix(status: str) -> str:
        if config.django.reuse_browser_tab:
            return status + " (or refresh an existing tab)"
        return status

    if setting is True:
        return True, _reuse_suffix(f"will open {homepage} ([django].open_browser = true)")
    if setting is False:
        return False, "disabled by [django].open_browser = false"

    if signal.headless:
        return False, f"skipped — {signal.reason} (pass --browser to override)"
    return True, _reuse_suffix(f"will open {homepage} ({signal.reason})")
```

Also apply `_reuse_suffix` to the `cli_choice is True` (`--browser`) return so the forced-open path is consistent:

```python
    if cli_choice is True:
        return True, _reuse_suffix(f"will open {homepage} (forced by --browser)")
```

(Define `_reuse_suffix` at the top of the function, before the first `return`.)

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_browser_decision.py -q`
Expected: PASS (existing + new).

- [ ] **Step 5: Format, lint, type, commit**

```bash
uv run ruff format . && uv run ruff check . && uv run mypy src/run_site \
  && git add src/run_site/cli.py tests/test_browser_decision.py \
  && git commit -m "Skip opening a new browser tab when a live-reload tab already exists"
```

---

### Task B3: run-site docs + example config + full suite

**Repo:** RS

**Files:**
- Modify: `docs/with-django-dev-helpers.md`, `docs/configuration.md`, `docs/troubleshooting.md`
- Modify: an `examples/runsite*.toml` if one documents `[django]` browser knobs

- [ ] **Step 1: Document.**
  - `docs/configuration.md`: add `[django].reuse_browser_tab` (default `true`) and `browser_reuse_grace` (default `2.0`) to the `[django]` reference, explaining they refresh an existing tab (via dev-helpers live-reload) instead of opening a new one on restart, and that they no-op when dev-helpers/live-reload is absent.
  - `docs/with-django-dev-helpers.md`: in "Default UX with both packages", note that on restart both tabs refresh in place rather than duplicating, driven by dev-helpers' `/__dev_reload__/` live-reload; cross-link the dev-helpers `live_reload` knobs.
  - `docs/troubleshooting.md`: near the existing dev-helpers browser section, add "tabs multiply on restart → ensure live_reload is enabled" and "want the old behavior → set `[django].reuse_browser_tab = false`".

- [ ] **Step 2: Run the default RS suite + lint/type**

Run: `uv run pytest -q -m "not docker" && uv run ruff check . && uv run mypy src/run_site`
Expected: all green.

- [ ] **Step 3: Commit**

```bash
git add -A && git commit -m "Document browser tab reuse across restarts"
```

---

## Manual end-to-end verification (after both phases)

Not a test file — run once by hand in a project that has both packages:

1. `run-site` → two tabs open (homepage + autologin).
2. Ctrl+C, `run-site` again → the two existing tabs **reload themselves**, no new tabs appear. Server console shows one `/__dev_reload__/` line per tab (not per-second spam).
3. Close both tabs, `run-site` again → two fresh tabs open (suppression correctly stands down).
4. Edit a template while running → the open tab reloads (autoreload bonus).

---

## Self-review notes (checked against the spec)

- Spec "boot_id changes per boot" → Task A2 (`secrets.token_hex`, module-level). ✓
- Spec "SSE hello + heartbeat + shutdown" → Task A3. ✓
- Spec "inject before </body>, fix Content-Length, skip non-html/streaming" → Task A4. ✓
- Spec "auto-install middleware" → Task A5. ✓
- Spec "dev-helpers suppression samples count across grace" → Task A6. ✓
- Spec "run-site queries clients, samples, 404→open, banner suffix, log skip" → Tasks B1/B2. ✓
- Spec "config knobs (both packages)" → Tasks A1/B1. ✓
- Spec "docs + CSP caveat + edge cases" → Tasks A7/B3. ✓
- Names consistent across tasks: `count()`, `existing_live_tab` (DH) / `_existing_live_tab` (RS), `LiveReloadMiddleware`, `install_live_reload_middleware_if_enabled`, `/__dev_reload__/` + `/__dev_reload__/clients`, `live_reload.{enabled,reuse_tabs,grace_seconds}`, `[django].{reuse_browser_tab,browser_reuse_grace}`. ✓
