# Runtime lifecycle and native threads

- **Status:** Implemented
- **Protocol baseline:** `codex-cli 0.144.1`, stable API surface
- **Scope:** Phase 1A

## Goal

Make one Codex app-server an application-scoped runtime and provide a workspace-scoped service for persistent thread lifecycle operations without exposing a temporary public API.

## Implemented behavior

- FastAPI creates the runtime during lifespan startup and closes it during shutdown.
- Concurrent startup calls share one app-server process.
- A failed mutating request is not retried automatically.
- The next request after process failure starts a new app-server.
- Runtime health exposes state, process liveness, restart count, and a bounded error.
- Tests disable the authenticated runtime unless they explicitly inject a fake process.
- Thread operations cover `thread/start`, `thread/list`, `thread/read`, `thread/resume`, `thread/name/set`, and `thread/delete`.
- Thread lists are scoped to the exact resolved `MUSELAB_ROOT` and sorted by `updated_at` descending.

## Empty-thread boundary

An app-server thread is not materialized into a rollout until its first turn. Before materialization, the live `0.144.1` server behaves as follows:

- `thread/list` does not return the thread;
- `thread/read` returns error `-32600`;
- `thread/resume` returns error `-32600`;
- `thread/name/set` and `thread/delete` work.

muselab-codex keeps these empty threads in an in-memory pending sidecar. It merges them into list and read results, updates their pending name, and removes them on delete. Pending entries are discarded after runtime restart because an unmaterialized thread cannot be resumed in the new process.

After the first turn completes, `thread/read`, `thread/resume`, `thread/list`, rename, and delete all use Codex as the source of truth.

## Verification

- 17 focused offline tests cover protocol, runtime, FastAPI lifespan, thread operations, and pending empty threads.
- A live persistent thread completed a turn and then passed read, resume, rename, filtered list, and delete checks.
- The live thread and app-server process were both removed at the end of verification.

## Non-goals

- Public HTTP session endpoints.
- Turn streaming and SSE adaptation.
- Browser approval controls and interruption.
- Persistent UI sidecars beyond the pre-first-turn in-memory placeholder.
- Attachments, compact, Skills, MCP, scheduler, subagents, or background terminals.
