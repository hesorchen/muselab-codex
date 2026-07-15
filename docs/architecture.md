# Architecture

> [简体中文](architecture_zh.md) · [← Documentation index](README.md)

muselab-codex is a local web workspace for `codex app-server`. Its architecture preserves native Codex thread, tool, and permission semantics while adding a browser, file workspace, and long-running service boundary.

```text
┌─────────────┐     HTTP/SSE + token     ┌─────────────────┐
│ Browser/PWA │ ───────────────────────→ │ FastAPI backend │
└─────────────┘                          └────────┬────────┘
                                                │ Unix WebSocket
                                                ▼
                                       ┌──────────────────┐
                                       │ codex app-server │
                                       └────────┬─────────┘
                                                ▼
                                              Codex
```

## Ownership boundary

| Domain | Authority | muselab-codex role |
|---|---|---|
| Threads, turns, transcripts | Codex app-server | Map them to HTTP resources and browser views |
| Models and reasoning controls | Codex configuration and thread state | Display the catalog and submit per-thread choices |
| Tools, sandbox, approvals | Codex app-server | Surface requests and return the user's decision |
| Skills, MCP, Memory | Codex app-server and `CODEX_HOME` | Provide controls without recreating discovery rules |
| File browsing and previews | FastAPI and browser | Safely operate below `MUSELAB_ROOT` |
| Login and remote access | Deployment environment | Provide single-user token authentication |
| Attachment and usage UI metadata | FastAPI sidecars | Store only what browser recovery needs |

When Codex already owns a capability, the backend adapts the protocol rather than copying its semantics. This avoids two transcripts, two tool loops, or competing configuration precedence rules.

## Process lifecycle

One `CodexRuntime` supervises one `codex app-server --listen unix://PATH` child
and connects to it over a compression-free Unix WebSocket with application
keepalive disabled (kernel socket closure remains the liveness signal):

1. create a private socket directory and start the listener;
2. send `initialize` and validate its response;
3. send the `initialized` notification;
4. start the single event reader, scheduler, and peripheral services;
5. accept HTTP and streaming requests;
6. close turns, terminal processes, scheduler, event routing, and app-server in order.

The Unix listener can also serve `codex --remote unix://PATH`, so the browser
and terminal share one in-memory thread store. Plain `codex` sessions are still
separate by design.

The tested protocol baseline is `codex-cli 0.144.1`. A baseline upgrade requires matching generated schemas, updated offline fixtures, and an explicit live-login check.

## One message end to end

```text
POST /api/chat/stream/start
  → validate token, thread, model, and attachments
  → thread/resume when required
  → subscribe to thread events
  → turn/start
  → return a one-use stream ticket

GET /api/chat/stream?ticket=…
  → replay buffered events for the active turn
  → stream text, tool, approval, usage, and completion events over SSE
  → drain the queued message after completion
```

The stream ticket keeps the primary token out of long-lived SSE URLs. Browser channels that cannot set custom headers use constrained query credentials together with access-log redaction and a restrictive `Referrer-Policy`.

## Single-reader event routing

App-server notifications share one stream. If every SSE connection called `next_notification()` directly, concurrent threads would consume each other's events.

`CodexEventRouter` is therefore the only reader. It extracts `threadId`, fans events out to thread subscriptions, supports connection-scoped terminal events, and closes old subscriptions when the runtime generation changes.

## Threads, turns, and browser tabs

- The Codex thread ID is the session key; Codex-native history is the transcript source of truth.
- Multiple browser tabs hold independent message and stream state. Desktop users can drag up to four threads into a live chat grid; only the active pane receives the shared composer, while mobile stays single-pane. The composer can create a thread in any explicitly registered working directory without changing the file browser's `MUSELAB_ROOT` boundary.
- One thread can have one active turn; additional user messages enter the application queue.
- Fork uses native thread branching. Compact rewrites the active context of the same thread and records a compaction boundary in its transcript.
- After an app-server restart, persisted threads are resumed once per runtime generation before a new turn starts.

## Persistence layout

```text
MUSELAB_ROOT/
├── AGENTS.md
├── .codex/
├── .muselab-codex/
│   ├── attachments/{staged,threads/}
│   └── usage/<thread-id>.json
└── user files

CODEX_HOME/              # normally ~/.codex
├── config.toml
├── skills/
└── login, Memory, and native thread history
```

`.muselab-codex` is not a second session database. It contains attachment ownership and sanitized numeric usage snapshots that the browser needs after restart but that are not part of the transcript.

## Filesystem boundary

All file APIs resolve below `MUSELAB_ROOT`. Traversal and escaping symlinks are rejected, credential-shaped files are blocked, visible writes are atomic, deletion uses a workspace trash area, and upload/preview endpoints enforce size and type limits.

This is an application boundary, not multi-tenant isolation. Anyone holding `MUSELAB_TOKEN` can still drive tools within the active Codex sandbox and approval policy.

## Failure model

| Failure | Behavior |
|---|---|
| app-server cannot initialize | FastAPI lifespan fails; health never reports ready |
| app-server exits during use | Runtime becomes failed; a later explicit request creates a new process without replaying ambiguous mutations |
| SSE disconnects | The turn continues and in-process events can be replayed on reconnect |
| Browser reloads | Transcript returns from Codex history; attachments and usage return from workspace sidecars |
| MCP or push degrades | Core chat remains available where possible and the affected UI reports degraded state |

Mutating app-server requests are not automatically retried because the server may have applied the operation before the client lost its response.

See [native implementation specs](specs/) for protocol decisions and verification evidence.
