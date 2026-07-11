# App-server stdio spike

- **Status:** Implemented
- **Protocol baseline:** `codex-cli 0.144.1`, stable API surface
- **Transport:** JSONL over stdio
- **Stable v2 schema SHA-256:**
  `8930c4041f091a05eb34303e081848833790967ab737e45f425e3d5e6b5e14a2`

## Goal

Prove that muselab-codex can own a long-running `codex app-server` process and
complete one safe, observable Codex turn without a second agent SDK, a
protocol gateway, or a direct model API.

This is a vertical protocol slice, not a production chat migration.

## Scope

The spike shall provide:

- an asynchronous app-server process wrapper;
- request and response ID correlation;
- notification delivery;
- server-initiated request handling;
- the `initialize` and `initialized` handshake;
- `thread/start` and `turn/start`;
- streamed agent-message text and terminal turn status;
- command-execution and file-change approval responses;
- explicit process shutdown and unexpected-exit reporting.

The live verification shall run against a throwaway workspace. It shall ask
Codex to read one fixture file and perform one controlled file modification.

## Requirements

1. When the client starts, it shall launch `codex app-server --stdio` without
   forwarding an application-level OpenAI API key.
2. When the process is ready, the client shall send exactly one `initialize`
   request and then one `initialized` notification before any thread request.
3. When multiple requests are in flight, the client shall resolve each caller
   using the matching response ID.
4. When app-server emits a notification, the client shall route it without
   blocking the stdout reader.
5. When app-server requests command or file-change approval, the client shall
   expose the request to a caller-supplied approval handler and return the
   handler's decision.
6. When a turn emits agent-message deltas, the spike shall reconstruct the
   displayed text without treating deltas as separate messages.
7. When a turn completes, the client shall expose its final status and token
   usage notifications received for the thread.
8. If stdout contains malformed JSON, a request fails, or the subprocess exits,
   the client shall fail pending requests with a bounded diagnostic that does
   not include raw user content.
9. When the client closes, it shall stop reader tasks and terminate only the
   app-server process it owns.

## Non-goals

- Browser UI or FastAPI endpoint integration.
- Session-list migration, fork, compact, attachments, Skills, MCP, scheduler,
  background terminals, or subagent visualization.
- Experimental app-server methods or fields.
- Removal of the legacy runtime in the same change.
- Docker or systemd support.

## Test strategy

Unit and protocol tests shall use a fake stdio subprocess and cover:

- handshake ordering;
- concurrent response correlation;
- interleaved notifications;
- command and file-change approval round trips;
- malformed lines and JSON-RPC errors;
- unexpected process exit;
- clean shutdown with pending requests.

A separate opt-in live check may use the locally installed Codex CLI. It must
create its workspace under a temporary directory, start an ephemeral thread,
and must not inspect or modify the user's real files.

## Acceptance criteria

The spike is complete when:

- no spike module imports a second agent runtime or uses a protocol gateway;
- the fake-protocol test suite is deterministic and offline;
- the live check confirms ChatGPT-authenticated app-server startup;
- one thread streams a text response;
- one file read and one approved file modification succeed in the temporary
  workspace;
- the client observes `turn/completed` and exits without leaving a child
  process behind.

## Verification record

Verified on 2026-07-10 with `codex-cli 0.144.1` authenticated through ChatGPT:

- seven offline protocol tests passed;
- app-server started with `OPENAI_API_KEY` removed from the child environment;
- an ephemeral thread completed successfully over stdio;
- streamed agent-message and terminal turn notifications were observed;
- command-execution approvals completed through the client request handler;
- Codex read a temporary fixture and derived the exact output bytes
  `phase-zero-native\n` in the same temporary workspace;
- app-server emitted no stderr lines during the successful live check.
