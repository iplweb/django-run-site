# Live-reload + browser tab reuse across restarts

Date: 2026-07-06
Status: Design approved, pending implementation
Repos touched: `django-dev-helpers` (bulk), `django-run-site` (small)

## Problem

On every launch, two browser tabs open automatically:

- Tab 1 (homepage `http://<host>:<port>/`) — opened by **run-site**
  (`cli._probe_and_open_browser` → `webbrowser.open`).
- Tab 2 (autologin `/__autologin__/?token=…`, which then 302-redirects to
  `autologin.redirect_to`, default `/`) — opened by **django-dev-helpers**
  (`apps.ready()` → `browser.spawn_self_probe_thread`).

Each full restart of `run-site` (Ctrl+C, re-run) is a fresh process with a
fresh subprocess env, so **both openers fire again and accumulate new tabs**.
The user wants: on restart the *already-open* tabs should **refresh in place**,
and **no new tabs** should be opened.

Django autoreload (code change) already does **not** re-open tabs — both
packages guard with sentinels (`DEV_HELPERS_BROWSER_OPENED` env survives the
`os.execv`; run-site's probe thread runs once per invocation). So the problem
is specific to full `run-site` restarts.

## Why "open with a target/window" is impossible

An external launcher (Python `webbrowser`, macOS `open`, `xdg-open`) cannot
name or target a specific existing tab. `window.open(url, "name")` reuse only
works from JS *inside* a page and only within the same browsing-context group
— it does not cross independent OS-level launches, so it cannot survive a
process restart. The only mechanisms that reach into an existing tab are
browser automation (AppleScript, Chrome DevTools Protocol, WebDriver), which
the user rejected. Therefore the goal is achieved **from the page/server
side**, not by reaching into the browser.

## Goal

1. An already-open tab **reloads itself** when the server comes back after a
   restart (or after Django autoreload).
2. On restart, an opener **suppresses** its browser-open when a live tab
   already exists, so tabs are refreshed rather than duplicated.

## Non-goals

- Refreshing a tab the user has closed (we cannot; if all tabs are closed we
  open fresh again — desired).
- Distinguishing tab 1 from tab 2 for suppression (both end up at `/`, so
  suppression is coarse: "is any live tab present?"). See Edge cases.
- Hot-module-replacement / partial reload. We do a full `location.reload()`.
- Any change to how the browser itself is launched (still `webbrowser.open`).
- Cross-browser automation (AppleScript/CDP). Explicitly rejected.

## Design overview

- **`boot_id`** — a random id generated once per server process. It changes on
  every full restart *and* on every autoreload child (reloading the page on a
  code change is a desirable bonus). The injected client remembers the first
  `boot_id` it saw; when a later connection reports a **different** `boot_id`,
  it calls `location.reload()`.
- **Transport = Server-Sent Events (SSE).** One long-lived connection per tab:
  quiet server log (one line per tab per boot, not per-second polling spam),
  one Network-tab entry, and an accurate "who is connected" registry (open =
  alive, close = gone) for suppression. `EventSource` auto-reconnects, so a
  tab transparently re-attaches when the server returns.
- **Injection** — a new auto-installed middleware appends a ~10-line
  `<script>` before the last `</body>` of every `text/html` response. Both
  tabs load HTML from the same Django server, so both get the script.
- **Suppression** — before opening, each opener watches the live-connection
  count across a short grace window (`grace_seconds`, default 2.0, ≥ the
  EventSource reconnect delay) so surviving tabs re-attach to the new boot.
  The count is **sampled a few times** across the window (not a single shot):
  suppress if **any** sample is `≥ 1`. Sampling bridges the brief
  unregister/re-register gap when a surviving tab reconnects and then
  immediately reloads (its old SSE connection closes on navigation before the
  reloaded page re-attaches). All samples `0` → open fresh.

## Protocol: `/__dev_reload__/` (owned by django-dev-helpers)

Served by `LiveReloadMiddleware` via path interception (like
`AutologinMiddleware`), so it works without wiring `urls.py`. Both endpoints
are gated by `is_active()` + `settings.DEBUG` + `live_reload.enabled`; when
not gated they are not intercepted (fall through → 404), which run-site treats
as "no live-reload → open normally".

### `GET /__dev_reload__/` — SSE stream

- Response headers: `Content-Type: text/event-stream`,
  `Cache-Control: no-cache`, `X-Accel-Buffering: no`.
- On connect, immediately emit the reconnect hint and the boot id:

  ```
  retry: 1000
  event: hello
  data: {"boot_id": "<hex>"}

  ```

- Then emit heartbeat comments (`: ping\n\n`) every ~2 s so drops are noticed
  and proxies don't buffer. The generator registers the connection in a
  module-level thread-safe registry on start and deregisters in `finally`
  (covers `GeneratorExit` on client disconnect). The heartbeat loop checks a
  `threading.Event` shutdown flag and uses short sleeps so it does not block
  `runserver` shutdown for long. Served on a daemon thread by the dev server.

### `GET /__dev_reload__/clients` — suppression probe

- Returns JSON `{"count": <int>}` — the number of currently-open SSE
  connections (registry size, under a lock). Used by both openers to decide
  whether to open a browser. No side effects.

### Injected client script (inline, v1)

```js
(function () {
  var boot = null;
  var es = new EventSource("/__dev_reload__/");
  es.addEventListener("hello", function (e) {
    var id = JSON.parse(e.data).boot_id;
    if (boot === null) { boot = id; }
    else if (id !== boot) { location.reload(); }
  });
  // EventSource auto-reconnects on error; the next "hello" after a restart
  // carries a new boot_id and triggers the reload above.
})();
```

Inline keeps v1 simple. CSP caveat and the external-script alternative are in
Out of scope.

## django-dev-helpers implementation

New module `live_reload.py`:

- `BOOT_ID` — module-level, `secrets.token_hex(8)`, computed once at import.
- A thread-safe connection registry (a `set` of connection ids + `Lock`, or an
  atomic counter) with `register()`, `unregister()`, `count()`.
- `sse_response(cfg)` — builds the `StreamingHttpResponse` with the generator
  described above. Honors a shutdown `Event`.
- `clients_response()` — builds the JSON count response.
- `CLIENT_SCRIPT` — the inline JS above (as a string), and an
  `inject(html: str) -> str` helper that inserts it before the last `</body>`
  (case-insensitive), returning the html unchanged if no `</body>` present.

New middleware `LiveReloadMiddleware` (in `middleware.py` or its own module),
auto-installed by `apps.py` alongside `AutologinMiddleware`, gated by
`live_reload.enabled`:

- Intercept `GET /__dev_reload__/` → `sse_response`;
  `GET /__dev_reload__/clients` → `clients_response` (only when active + DEBUG;
  otherwise pass through).
- Otherwise `response = get_response(request)`; if `response` is **not**
  streaming and `Content-Type` starts with `text/html` and body contains
  `</body>`: inject the script and fix `Content-Length` if that header is set
  (`response["Content-Length"] = str(len(response.content))`). Guard against
  `response.streaming` and non-html.

`browser.py` suppression — before calling `open_browser(cfg)` inside
`wait_for_http`, when `live_reload.enabled` and `live_reload.reuse_tabs`:

- after the HTTP probe confirms the server is up, sample the in-process
  `live_reload.count()` a few times across `grace_seconds` (e.g. every ~0.5 s).
  If any sample is `≥ 1`, log "existing tab detected — refreshing in place, not
  opening a new tab" and return without opening. Else open as today.
  (In-process read; no HTTP.)

`apps.py` — install `LiveReloadMiddleware` when `live_reload.enabled`
(reuse the existing `install_*_middleware_if_enabled` pattern), independent of
the browser-open sentinel (the middleware must run on every boot, including
autoreload children, so injected tabs can reconnect).

`conf.py` — new namespace, following the existing pattern:

```python
_LIVE_RELOAD_DEFAULTS = {
    "enabled": True,
    "reuse_tabs": True,
    "grace_seconds": 2.0,
}
```

Add to `_DEFAULTS`, add `self.live_reload = _dict_to_namespace(...)` in
`__init__`, and add validation (`enabled`/`reuse_tabs` are bools,
`grace_seconds` is a non-negative number).

## django-run-site implementation

`cli._probe_and_open_browser(url, homepage, config)` — after
`wait_for_http(url)` succeeds, when the new `[django].reuse_browser_tab` knob
(default `true`) is on:

- `GET http://<host>:<port>/__dev_reload__/clients` (short timeout) sampled a
  few times across `config.django.browser_reuse_grace` (new knob, default 2.0;
  e.g. every ~0.5 s), parsing `count`. If any sample is `≥ 1` → **skip**
  `webbrowser.open` and `logger.info` "existing browser tab detected —
  refreshing in place, not opening a new tab". If every sample is `0`, or the
  endpoint is `404` / non-JSON / unreachable → open as today. Endpoint 404
  (dev-helpers absent or live-reload off) means standalone run-site behaves
  exactly as before.

**Banner note:** the reuse decision happens asynchronously in the probe
thread, *after* the sticky banner has already rendered, so it cannot be
written back into the banner. Instead, when `reuse_browser_tab` is on and the
open decision is `True`, `_resolve_browser_decision` appends a
"(or refresh an existing tab)" suffix to `browser_status` up front, so the
banner sets the right expectation; the actual skip is logged, not shown in the
banner.

## Config reference

django-dev-helpers `DJANGO_DEV_HELPERS`:

| Key | Default | Meaning |
| --- | --- | --- |
| `live_reload.enabled` | `True` | inject script + serve `/__dev_reload__/` |
| `live_reload.reuse_tabs` | `True` | suppress dev-helpers' own re-open when a live tab exists |
| `live_reload.grace_seconds` | `2.0` | wait for surviving tabs to reconnect before deciding |

run-site `runsite.toml` `[django]`:

| Key | Default | Meaning |
| --- | --- | --- |
| `reuse_browser_tab` | `true` | query `/__dev_reload__/clients`; skip opening tab 1 if a live tab exists |
| `browser_reuse_grace` | `2.0` | grace before the clients query |

## Edge cases

- **Both tabs closed + restart** → `count 0` → both openers open fresh (2
  tabs). Correct.
- **One tab closed + restart** → `count 1` → both openers suppress → the
  surviving tab reloads; the closed one is not restored. Documented compromise
  (the two tabs are indistinguishable because both live at `/`).
- **Non-HTML homepage** (project `/` returns JSON / a redirect) → no injection
  in that tab → it will not self-reload. The autologin tab (lands on `/`,
  usually HTML) still reloads. Documented.
- **Strict CSP in DEBUG** blocks the inline script → no reload; disable via
  `live_reload.enabled = False` or use the external-script variant (future).
- **First launch** pays `grace_seconds` (~2 s) of latency before opening,
  because we wait to see whether a surviving tab reconnects. Tunable.
- **SSE + runserver shutdown**: an open stream can briefly delay shutdown; the
  daemon thread + heartbeat-with-shutdown-check keeps this short. django-
  browser-reload lives with the same trade-off.

## Testing

django-dev-helpers:

- `boot_id` is stable within a process; differs across `reset`/re-import.
- SSE endpoint: `Content-Type: text/event-stream`; first chunk carries
  `event: hello` + the current `boot_id` (read from `streaming_content`).
- `clients` count increments on register and decrements on unregister.
- Injection: script added only for `text/html` with `</body>`; `Content-Length`
  updated; skipped for non-html, streaming, and bodies without `</body>`.
- Middleware auto-installed when `live_reload.enabled`, absent when disabled;
  endpoints 404 when inactive / DEBUG off.
- Config defaults + validation (bad types rejected).
- Suppression: with `count ≥ 1` after grace, `open_browser` is not called;
  with `0`, it is. (Grace patched/short in tests.)

django-run-site:

- Decision helper: `clients` returns `count ≥ 1` → no `webbrowser.open`;
  `0` / `404` / error / unreachable → opens. `reuse_browser_tab = false` →
  always opens (current behavior). Banner status string reflects the outcome.
  (`webbrowser`, `urllib`, and sleep patched.)

## Out of scope / future

- **CSP-safe external script**: serve the client at
  `/__dev_reload__/client.js` and inject `<script src>` instead of inline, so
  strict `script-src 'self'` in DEBUG still works. Deferred to keep v1 small.
- **Per-tab role suppression**: have the client report its role/path so a
  closed tab can be individually reopened. Deferred — both tabs live at `/`,
  so it needs an explicit role signal.
- **Chrome DevTools Protocol** reuse (true focus-existing-tab) — rejected
  (requires launching Chrome with `--remote-debugging-port`).

## Rollout note

Two packages ship the change. The `/__dev_reload__/` contract is additive and
backward-compatible: an old dev-helpers simply doesn't serve the endpoint, and
a new run-site treats the resulting 404 as "open normally". So the packages can
be released independently, in either order, without breaking mixed versions.
