# Native context and dashboard surfaces

- **Status:** Implemented
- **Scope:** Phase 2

The Codex runtime now owns the browser-facing context and aggregate usage
compatibility surfaces. `GET /api/chat/context-info` reports only the presence
of Codex instruction/config sources; it never returns their contents. The
aggregate `GET /api/chat/usage` sums the native per-thread token sidecars and
explicitly reports that provider cost is unavailable rather than inventing a
price. Session history continues to use the native `thread/read` path with a
bounded timeout and metadata-only degradation.

`GET /api/chat/interrupted-turns` returns an explicit empty result because
Codex app-server owns turn persistence and has no application-owned active-turn
sidecar. The dismiss route remains an idempotent compatibility endpoint so an
older browser does not fall back to legacy session files.

`GET /api/chat/codex-rate-limit` delegates to the native
`account/rateLimits/read` request. When the current Codex authentication mode
does not expose account limits, it returns an honest non-authoritative empty
snapshot instead of inferring limits from token counts.

The session queue endpoints use a Codex-owned, process-local queue keyed by
thread id. Read, enqueue, remove, clear, pause, and reorder are supported with
the existing browser contract and a ten-item cap. After a successfully
completed native turn, the queue starts exactly one FIFO successor only when
the thread is idle and not paused. A failed start restores the item at the
head and pauses the queue. Queue state is deliberately not persisted across a
service restart, so queued text cannot execute unexpectedly after recovery.

`POST /api/chat/sessions/{thread_id}/fork` delegates to native `thread/fork`.
It preserves the current workspace, sandbox, and approval policy while letting
the caller optionally select a completed last turn and model override. The
forked thread remains owned by Codex rather than a copied legacy transcript.
