# Codex-native chat MVP

- **Status:** Implemented
- **Protocol baseline:** `codex-cli 0.144.1`, stable API surface
- **Scope:** Phase 1B

## Goal

Replace the first browser chat slice without adding a temporary `/api/codex/*` API. The frontend keeps its existing `/api/chat/*` URLs while the default backend path translates them to Codex threads, turns, notifications, and server-initiated approvals.

## Runtime composition

The FastAPI lifespan now owns six cooperating objects:

1. `CodexRuntime` supervises one app-server process.
2. `CodexEventRouter` is the only consumer of the app-server notification queue and fans events out by `threadId`.
3. `CodexThreadService` owns persistent thread lifecycle operations.
4. `CodexTurnService` owns one active turn per thread, event replay buffers, and interruption.
5. `CodexApprovalBroker` correlates app-server request IDs with browser decisions.
6. `CodexHistoryService` bounds full transcript reads and prevents one large rollout from monopolizing the shared app-server.

Individual SSE responses never read directly from app-server. This prevents two simultaneous threads from consuming each other's events.

## HTTP compatibility slice

The Codex-native router currently implements:

- create, list, read, outline, rename, and delete under `/api/chat/sessions`;
- model discovery through `/api/chat/providers` backed by `model/list`;
- one-time stream tickets through `/api/chat/stream/start`;
- turn start and reconnect through `/api/chat/stream`;
- active-turn status;
- command and file-change approval submission;
- `turn/interrupt` through the existing interrupt endpoint.

Codex owns thread identifiers. The browser therefore adopts the UUID returned by `thread/start`; it does not assign an application-generated UUID in the native path.

The pinned stable protocol does not expose paginated turn or item reads. `thread/read(includeTurns=true)` can take over a minute for a large rollout even when the HTTP caller asks for `tail=80`, because slicing happens only after app-server returns the full thread. muselab-codex gives the first full read eight seconds. On timeout it restarts the still-busy app-server, marks that thread degraded for the current application process, and serves metadata without history. Per-thread locks prevent simultaneous browser startup requests from causing a restart storm. The response exposes `history_unavailable=true`.

A fresh app-server does not automatically load persisted threads. The turn service therefore calls `thread/resume` once per thread and runtime generation before `turn/start`; subsequent turns on the same process skip the redundant full resume. A slow history read degrades only transcript display: the browser warns once, but sending still asks app-server to resume the original thread. muselab-codex never silently replaces the user's thread merely because history rendering timed out.

## Event mapping

| App-server event | Browser SSE event |
|---|---|
| `item/agentMessage/delta` | `text` |
| `item/reasoning/summaryTextDelta` | `thinking` |
| `item/reasoning/textDelta` | `thinking` |
| tool-like `item/started` | `tool_use` |
| tool-like `item/completed` | `tool_result` |
| server command/file approval request | `permission_request` |
| `turn/completed` | `done` |

Unknown notifications are ignored so additive app-server changes do not break the stream.

## Approval decisions

The browser's inherited values are mapped to stable app-server values:

| Browser | App-server |
|---|---|
| `allow` | `accept` |
| `always` | `acceptForSession` |
| `deny` | `decline` |
| `cancel` | `cancel` |

The protocol client passes the original server request ID into the broker. A decision can therefore resolve only the exact pending callback for its thread.

## Runtime boundary

When `MUSELAB_CODEX_RUNTIME_ENABLED=1` (the default), `backend.main` imports the Codex-native chat router and no second chat or scheduler runtime. Setting it to `0` retains only a regression-test boundary. New product work must target the native path.

Attachments, compact, MCP configuration, Skills UI, scheduler execution, ask-user-question, subagents, background terminals, and detailed usage accounting remain outside this slice.

## Verification

- 30 focused offline tests cover the protocol client, runtime, threads, bounded history reads, notification fan-out, approvals, turn mapping, interruption, and the HTTP/SSE roundtrip.
- The HTTP test starts the deterministic app-server subprocess, creates a native thread, streams reasoning, tool, text, and completion events, reloads persisted history, renames the thread, and deletes it.
- A default-mode import check confirms that no second agent SDK is loaded.
- A live ChatGPT-authenticated HTTP/SSE smoke check discovered seven models, created a persistent thread in a temporary workspace, streamed `OK` plus a completed turn, read the persisted transcript, and deleted the thread.
