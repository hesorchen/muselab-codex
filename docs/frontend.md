# Frontend internals

> [简体中文](frontend_zh.md)

How muselab's browser code is structured and how it runs.
For PWA install steps, see [mobile.md](mobile.md).
For the server side of the SSE stream, see [routing.md](routing.md).
For the overall system layout, see [architecture.md](architecture.md).

---

## 1. No build step, by design

muselab ships no bundler — no Webpack, Vite, or Rollup.
The frontend is vanilla HTML + CSS + JavaScript served directly from `frontend/`.
For contributors this means: edit a file, refresh the browser.

**Script load order** ([`index.html:50–67`](../frontend/index.html#L50)) — all `defer`, preserving order:

```
i18n/index.js → data/constants.js → app.js
  → vendor/marked.min.js → vendor/purify.min.js → vendor/alpine.min.js
```

`i18n/index.js` and `data/constants.js` must precede `app.js` because `app.js`
reads `window.MUSELAB_STRINGS` at module top-level.

**Lazy-loaded heavy libs** — injected as `<script>` tags on first use
([`_loadHljs`](../frontend/app.js#L10946), [`_loadKatex`](../frontend/app.js#L10986), and equivalents), never preloaded at boot:

| Library | Approx. size | Triggered by |
|---------|-------------|--------------|
| Mermaid | ~3.3 MB | First `mermaid` code block |
| CodeMirror (core + modes) | ~308 KB | First "Edit" click in preview pane |
| highlight.js | ~124 KB | First `<pre><code>` in chat |
| KaTeX (JS + fonts) | ~562 KB | First `$`, `$$`, `\(`, or `\[` in a message |

**Cache busting** — the `/` route re-renders `index.html` on every request,
rewriting every `/static/…` URL to append `?v=<asset_version>`
([`backend/main.py:396–430`](../backend/main.py#L396)).
The service worker is served at `/sw.js` (not `/static/sw.js`) so its scope
covers the whole origin ([`backend/main.py:486–494`](../backend/main.py#L486)).

---

## 2. One Alpine component

The entire UI is a single [`x-data="portal()"`](../frontend/index.html#L231)
root component.  All reactive state — sessions, messages, streaming flags, file
tree, tabs, settings, toasts — lives as properties of the object returned by the
[`portal()`](../frontend/app.js#L178) factory function.
There are no Alpine stores and no sub-components.
`x-init` removes the pre-Alpine splash immediately; `x-effect` keeps `<title>`
reactive without a manual watcher ([`index.html:231–232`](../frontend/index.html#L231)).

### Three-pane layout

| Pane | Element | Lines in index.html | Contents |
|------|---------|---------------------|----------|
| Left — file tree | `<aside class="pane files">` | [277–499](../frontend/index.html#L277) | Upload / new-dir buttons, search bar, open-files strip, `<ul class="filelist">` via `x-for`, drag-and-drop trash target |
| Center — preview | `<section class="pane preview">` | [504–938](../frontend/index.html#L504) | Tab bar, CodeMirror editor (`x-ref="cmHost"`), preview body with `x-show` branches for md / text / html / img / pdf / xlsx / csv modes |
| Right — chat | `<aside class="pane chat">` | [943–end](../frontend/index.html#L943) | Session tab strip, message body (`x-for` over `messages`), composer with @-mention autocomplete, model/permission/effort selectors |

---

## 3. Rendering pipeline

### marked → DOMPurify → KaTeX → linkify

Every assistant message goes through
[`_mdRenderUncached()`](../frontend/app.js#L3177):

1. **Pre-process** — close uncounted ` ``` ` / `~~~` fences mid-stream
   ([`app.js:3188–3192`](../frontend/app.js#L3188)); swap math spans
   (`$$…$$`, `$…$`, `\(…\)`, `\[…\]`) with opaque placeholders before marked
   so LaTeX underscores/asterisks are not consumed as Markdown emphasis
   ([`app.js:3193–3201`](../frontend/app.js#L3193)).
2. **Parse** — `window.marked.parse(parseInput)`; exceptions fall back to `<pre>`
   ([`app.js:3206–3210`](../frontend/app.js#L3206)).
3. **Sanitize** — `DOMPurify.sanitize(raw, { USE_PROFILES: { html:true, mathMl:true }, FORBID_TAGS: ["style","iframe","form","object","embed"], FORBID_ATTR: ["style","formaction"], ADD_ATTR: ["aria-hidden"] })`
   ([`app.js:3213–3218`](../frontend/app.js#L3213)).
4. **Math restore + KaTeX** — unmask placeholders, call `renderMathInElement`
   if KaTeX is loaded ([`app.js:3261–3273`](../frontend/app.js#L3261)).
5. **File-path linkify** — walk the detached DOM with `_linkifyFilePaths`
   ([`app.js:3275`](../frontend/app.js#L3275)).

**Mid-stream cheap path vs. final `flushRender`** — when called with
`{ streaming: true }` the function returns after step 3, skipping KaTeX and
linkify ([`app.js:3240`](../frontend/app.js#L3240)).
Those DOM walks run only in `flushRender()` at stream end
([`app.js:13735–13741`](../frontend/app.js#L13735)).

**Mermaid** — `_renderMermaidBlock` lazy-loads `vendor/mermaid.min.js` with
`securityLevel: "strict"` and re-renders only when the source hash changes.

**HTML artifacts in sandboxed iframes** — AI-generated HTML blocks are mounted
via `srcdoc` (never `src`) with
[`app.js:11229`](../frontend/app.js#L11229):

```
sandbox="allow-scripts allow-popups allow-popups-to-escape-sandbox allow-forms"
```

`allow-same-origin` is **intentionally absent**, giving the iframe a null
origin so it cannot access muselab's DOM, `localStorage`, cookies, or auth
token regardless of what the AI-generated code attempts.
Preview-pane HTML files use the even stricter `sandbox="allow-scripts"` only
([`index.html:780–782`](../frontend/index.html#L780)).

**LRU cache** — [`mdRender()`](../frontend/app.js#L3154) wraps
`_mdRenderUncached` with a Map-based LRU (cap 400 entries); live streaming
bubbles bypass the cache.

---

## 4. Consuming the SSE stream

Each chat turn opens a per-turn [`EventSource`](../frontend/app.js#L13582)
to `/api/chat/stream?prompt=…&session_id=…&model=…&permission=…&token=…`
([`app.js:13575–13582`](../frontend/app.js#L13575)).
The token is a query parameter because `EventSource` does not support custom
headers.

### Handled event types

| Event | Handler | Action |
|-------|---------|--------|
| `text` | [`app.js:13777`](../frontend/app.js#L13777) | Append `d.text` to accumulator; call `scheduleRender()` |
| `thinking` | [`app.js:13802`](../frontend/app.js#L13802) | Coalesce into last thinking bubble or push new one |
| `tool_use` | [`app.js:13822`](../frontend/app.js#L13822) | Push `{role:"tool_use", name, id, …}`; maybe reload preview |
| `tool_result` | [`app.js:13857`](../frontend/app.js#L13857) | Push `{role:"tool_result", id, tool_name, preview, …}` |
| `task_started` | [`app.js:13879`](../frontend/app.js#L13879) | Stamp matching `tool_use` with `{state:"running", …}` |
| `task_progress` | [`app.js:13889`](../frontend/app.js#L13889) | Update running state + usage on matching `tool_use` |
| `task_notification` | [`app.js:13899`](../frontend/app.js#L13899) | Stamp terminal state (`completed`/`failed`/`stopped`) |
| `rate_limit` | [`app.js:13915`](../frontend/app.js#L13915) | Merge rate-limit window data; recompute `rlBadge` |
| `ask_user_question` | [`app.js:13927`](../frontend/app.js#L13927) | Push interactive question bubble with pre-populated `pendingAnswers` |
| `permission_request` | [`app.js:13956`](../frontend/app.js#L13956) | Push permission bubble with approve/deny controls |
| `done` | [`app.js:14047`](../frontend/app.js#L14047) | `flushRender()` (full pass with KaTeX + linkify); parse cost/stats; close `EventSource`; trigger `highlightCode` |
| `ping` / `cancelled` | [`app.js:13611–13614`](../frontend/app.js#L13611) | Bump `_lastSseActivity` only |

**Throttled rendering: 80 ms → 1600 ms** — re-parsing the full accumulator on
every token is O(n²) over a long reply.
`scheduleRender()` stretches the interval with accumulator size
([`app.js:13694–13707`](../frontend/app.js#L13694)):
80 ms (< 2 KB) → 160 ms (< 8 KB) → 320 ms (< 20 KB) → 600 ms (< 50 KB) →
1000 ms (< 120 KB) → 1600 ms (≥ 120 KB).
`flushRender()` on `done` always paints the complete final text.

### 40-second stall watchdog vs. 15-second server ping

The server heartbeats a named `ping` event every 15 seconds
(see [routing.md](routing.md)).
A `setInterval` watchdog fires every 10 seconds; if no SSE activity has arrived
for > 40 seconds (≥ 2 missed pings), it synthesizes a transport-level `error`
event to trigger the reconnect path
([`app.js:13615–13627`](../frontend/app.js#L13615)).

---

## 5. i18n

[`frontend/i18n/index.js`](../frontend/i18n/index.js) (735 lines) exports
`window.MUSELAB_STRINGS = { zh: {…}, en: {…} }` — a flat dictionary with
~200+ keys per locale.

Default locale is `zh`; auto-detected to `en` if `navigator.language` starts
with `"en"` ([`app.js:1946–1950`](../frontend/app.js#L1946)).
A `localStorage` entry (`muselab_lang`) overrides auto-detection and persists
across sessions; toggled in **Settings → Language**
([`app.js:1952`](../frontend/app.js#L1952)).

[`t(key)`](../frontend/app.js#L1960) looks up `STRINGS[this.lang][key]`, falls
back to `STRINGS.zh[key]`, then to the raw key — missing translations surface
as their own key rather than silently blank.

The pre-Alpine splash is also localized via an inline `<script>` that checks
`navigator.language` before Alpine boots
([`index.html:84–95`](../frontend/index.html#L84)).

---

## 6. Vendored libraries

All third-party code lives under `frontend/vendor/`.
**No CDN** — the app works fully offline once installed.
Licenses are in [`THIRD_PARTY_LICENSES.md`](../THIRD_PARTY_LICENSES.md).

| Library | Loaded | Purpose |
|---------|--------|---------|
| `alpine.min.js` | Boot | Alpine.js v3 — reactive UI framework |
| `marked.min.js` | Boot | Markdown-to-HTML parser |
| `purify.min.js` | Boot | DOMPurify — HTML sanitizer applied after marked |
| `highlight-theme.css` / `highlight-theme-light.css` | Boot | highlight.js theme CSS (preloaded to prevent FOUC) |
| `highlight.min.js` + `hljs-langs/*.min.js` | Lazy | Syntax highlighting for code blocks (~124 KB core + 5 extra languages) |
| `katex/katex.min.js` + fonts + `auto-render.min.js` | Lazy | Math rendering for `$…$` / `$$…$$` / `\(…\)` / `\[…\]` (~562 KB with fonts) |
| `mermaid.min.js` | Lazy | Diagram / flowchart renderer (~3.3 MB) |
| `cm/codemirror.min.js` + modes + addons | Lazy | CodeMirror 5 — preview-pane edit mode (~308 KB with all modes) |

---

## 7. Service worker: push-only, no caching

[`frontend/sw.js`](../frontend/sw.js) (94 lines) is intentionally minimal.

**No caching** — the SW deliberately does not cache any requests or assets
([`sw.js:1–8`](../frontend/sw.js#L1)).
Static assets already carry `?v=<mtime>` stamps, so a stale-while-revalidate
layer would mostly confuse contributors during development.

**Install / activate** — `skipWaiting()` + `clients.claim()` so the SW
activates immediately and takes control of all pages
([`sw.js:10–15`](../frontend/sw.js#L10)).

**Web Push delivery** is the SW's sole feature.
It is registered lazily from [`pushSubscribe()`](../frontend/app.js#L15455)
and served from `/sw.js` (not `/static/sw.js`) for whole-origin scope.
See [mobile.md](mobile.md) for user-facing push setup.

**Visible-window suppression** — on a push event the SW calls
`clients.matchAll()` and drops the notification if any muselab window on the
same device has `visibilityState === "visible"` — the user already sees the
reply arrive in-app ([`sw.js:47–60`](../frontend/sw.js#L47)).

**Stale-PWA hard reload** — on each `visibilitychange` the app fetches
`/api/meta` and compares `asset_version` to the value baked into
`<meta name="muselab-asset-version">`.
If the versions differ and no stream is active, the page hard-reloads
([`app.js:1685–1713`](../frontend/app.js#L1685)).
This handles the common mobile Safari pattern of resuming a backgrounded PWA
tab with stale JavaScript in memory.
