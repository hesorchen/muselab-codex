# Native context meter and compaction

- **Status:** Implemented
- **Scope:** Phase 2
- **Protocol baseline:** `codex-cli 0.144.1` stable v2 schema

## Goal

Replace application-estimated context usage and the custom `/compact` workflow with
app-server-owned token usage and `thread/compact/start`, while preserving the
existing browser context ring, breakdown popup, confirmation flow, and compact
progress bubble.

## Token usage semantics

`thread/tokenUsage/updated` contains two different counters:

| Field | Meaning in muselab-codex |
|---|---|
| `tokenUsage.total` | Cumulative input, cached-input, output, and reasoning-output accounting for the thread |
| `tokenUsage.last.totalTokens` | Current context occupancy shown in the context meter |
| `tokenUsage.modelContextWindow` | Context-meter denominator |

Using cumulative `total.totalTokens` as context occupancy would make the ring
grow forever even after compaction. A real `0.144.1` check observed a normal
turn at `last.totalTokens = 13284` and the following compact update at
`last.totalTokens = 4125`, while cumulative `total` stayed at 13284.

The normalized snapshot is persisted at:

```text
$MUSELAB_ROOT/.muselab-codex/usage/<thread-id>.json
```

It contains sanitized numeric usage only—no prompts, messages, credentials, or
file content. This lets the context ring survive a backend restart even though
app-server does not include token usage in `thread/read`.

## Browser contracts

| Endpoint/event | Behavior |
|---|---|
| `GET /api/chat/usage/{thread-id}` | Current meter plus cumulative counters |
| `GET /api/chat/context-breakdown/{thread-id}` | Input, cached input, output, and reasoning-output categories |
| SSE `done.session_usage` | Updates the meter immediately after a turn |
| `POST /api/chat/sessions/{thread-id}/native-compact` | Runs and waits for native compaction |

Both the context-ring action and the `/compact` slash command use the same
native endpoint.

## Compact lifecycle

The implementation subscribes before sending `thread/compact/start`, reserves
the thread so a user turn cannot race the compact operation, and waits for the
compact turn to complete. The real stable event sequence is:

```text
thread/status/changed
turn/started
item/started             type=contextCompaction
thread/tokenUsage/updated
item/completed           type=contextCompaction
turn/completed
```

The deprecated `thread/compacted` notification remains a compatibility terminal
condition. A failed compact turn becomes an HTTP error; a configurable timeout
defaults to 600 seconds through `MUSELAB_CODEX_COMPACT_TIMEOUT_SECONDS`.

`thread/read` exposes the `contextCompaction` item after completion. The HTTP
adapter maps it to the inherited compact marker so the browser renders the
“conversation compacted” boundary without creating a second transcript.

## Limits

- A turn already in progress returns conflict instead of being interrupted.
- A thread already marked history-unavailable is not resumed for compaction,
  because stable `0.144.1` still lacks bounded transcript pagination.
- The breakdown is limited to the token classes exposed by stable app-server;
  it does not invent per-memory-file or per-tool estimates.
