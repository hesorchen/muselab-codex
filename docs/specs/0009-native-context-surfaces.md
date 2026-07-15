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

Active-turn recovery uses `GET /api/chat/sessions/{thread_id}/active` plus the
native turn replay stream. There is no application-owned interrupted-turn
sidecar or compatibility dismiss endpoint.

`GET /api/chat/codex-rate-limit` delegates to the native
`account/rateLimits/read` request. When the current Codex authentication mode
does not expose account limits, it returns an honest non-authoritative empty
snapshot instead of inferring limits from token counts.

The session queue endpoints use a Codex-owned queue keyed by thread id. Read,
enqueue, remove, clear, pause, and reorder are supported with the browser
contract and a ten-item cap. Text, the selected settings snapshot, and
path-free staged-attachment metadata are persisted so queued work remains
visible after a restart. Recovery never executes it automatically: the user
must explicitly resume a dormant queue. After a successfully completed native
turn, exactly one FIFO successor starts when the thread is idle and not paused;
a failed start restores the item at the head and pauses the queue. Enqueue may
atomically start the new head when the browser's busy observation became stale
during the request; its response tells the browser to attach to that turn
immediately. Cancelling or failing a turn never triggers the success drain.

`POST /api/chat/sessions/{thread_id}/fork` delegates to native `thread/fork`.
It preserves the current workspace, sandbox, and approval policy while letting
the caller optionally select a completed last turn and model override. The
forked thread remains owned by Codex rather than a copied legacy transcript.
