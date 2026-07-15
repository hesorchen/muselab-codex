# muselab-codex development guidance

## Product direction

muselab-codex is a Codex-native, self-hosted AI workspace. Its only supported
agent runtime is `codex app-server`; it does not maintain a compatibility or
multi-provider runtime.

The repository was created from muselab, but its supported product surface is
Codex-native only. Do not introduce a second agent SDK or CLI, vendor-specific
compatibility endpoints, direct model calls, or protocol-conversion gateways.

## Target architecture

```text
Browser -> FastAPI HTTP/SSE -> codex app-server Unix WebSocket -> Codex
```

- `codex app-server` owns threads, turns, transcripts, tools, approvals,
  Skills, MCP, sandboxing, and model interaction.
- The backend owns authentication, HTTP/SSE adaptation, file-management APIs,
  workspace configuration, and process supervision.
- Do not call model APIs directly and do not add a protocol-conversion gateway.
- Keep the repository root and user workspace root separate. Tests must use a
  throwaway workspace and must never read a developer's real archive.

## App-server protocol baseline

- Initial compatibility target: `codex-cli 0.144.1`.
- Shared transport: `codex app-server --listen unix://PATH`, with both the
  backend and `codex --remote unix://PATH` joining the same runtime.
- The current product enables `initialize.params.capabilities.experimentalApi`
  for native structured user input, collaboration modes, account usage,
  paginated thread items, permission profiles, and MCP elicitation. Every use
  must have a fail-closed or compatibility fallback.
- Every connection must complete `initialize` followed by the `initialized`
  notification before sending other requests.
- Treat WebSocket messages as protocol-only JSON-RPC. Never enable WebSocket
  compression against the Unix listener; the pinned CLI does not negotiate it.
- Handle all three inbound message classes: responses, notifications, and
  server-initiated requests such as command or file-change approvals.
- Never log OAuth credentials, API keys, raw prompts, file contents, or full
  protocol payloads that may contain user data.

Generate version-matched protocol references when the pinned CLI changes:

```bash
codex app-server generate-json-schema --experimental --out <temporary-directory>
```

Do not vendor the complete generated schema tree by default. Record the CLI
version and schema digest in the change that upgrades the protocol baseline.

## Engineering constraints

- Python `>=3.12`, FastAPI, and `uv` remain the backend stack.
- Keep the frontend build-free: plain HTML, Alpine.js, CSS, and vendored browser
  libraries only.
- Keep chat transcript DOM bounded with keyed panes. Inside a multi-session
  `tid` loop, pane-relative helpers must take that session id instead of reading
  root `currentId` or `messages`; preserve each session's follow and scroll
  state across switches. Session-scoped transient rows such as first-token,
  compaction, and queue placeholders must stay inside that session's
  `.msg-pane`, not as direct `.chat-body` children outside `.chat-grid`.
- Never calculate a duration by subtracting a server epoch from a browser
  epoch. Browser and backend may run on different devices; expose a relative,
  monotonic server duration for reconnect and completion timing, with a local
  fallback for older servers.
- Derive composer running controls from the same per-session predicate as the
  transcript status. Stop must interrupt the active turn and must never double
  as a queue-removal action; queued messages keep explicit edit/delete controls.
- A session revision cursor means "rendered in this pane", not merely
  "observed in the list". Never advance it before a successful transcript
  load; preserve revisions that arrive during streaming/in-flight loads, drain
  them even after a 304, and bound every single-flight poll with a timeout.
- Treat mobile `visualViewport` notifications as lossy. Never call
  `scrollIntoView()` on the composer (its scrollable transcript is a sibling,
  so iOS pans the root viewport and can leave a clipped header plus bottom
  blank band). While keyboard geometry is present, reconcile its live values
  with a bounded watchdog; on close, clear both the keyboard inset and root
  viewport offset even if the textarea keeps focus and no close event arrives.
- Stable Codex thread reads do not expose `thread/resume.config`. Persist a
  user's pending next-turn effort override in the thread compatibility
  sidecar, merge it over the previous rollout's settings, and retain an
  explicit empty value for auto; otherwise list polling or tab reloads silently
  replace a newly selected effort before the next turn records it.
- Treat session settings as session-owned, serialized state. Capture the target
  thread before every async write, keep an expected server echo while a write
  is pending, and never let a late list/read response mutate another pane's
  composer or rewind a newer model, effort, permission, or thinking choice.
- Queue enqueue must close the turn-completion race atomically: when the browser
  enqueues from a stale busy snapshot, the backend may drain immediately if the
  thread is already idle and the browser must attach to that started turn. Stop
  pauses queued work, cancels every pending reconnect timer, and a cancelled
  terminal event must never run the normal success drain.
- Stream URLs must contain only a short-lived, single-use ticket; never place
  bearer tokens, prompts, attachment ids, or settings in an `EventSource` URL.
  Preserve the last good transcript on refresh failure and leave failed sends
  visibly retryable instead of clearing their text or attachments.
- Attachment completion and failure checks must run before both direct-turn
  validation and queue enqueue. Never downgrade an attachment-bearing prompt
  to text-only: wait for pending uploads, and preserve the composer if any
  upload fails. Live SSE subscribers still receive every delta; only the
  reconnect replay copy may coalesce consecutive plain-text deltas.
- Make background connection starters genuinely idempotent by replacing their
  global listeners as well as clearing intervals. Bound caches whose keys
  contain user searches or opaque pagination cursors.
- Prefer small modules and standard-library primitives over new dependencies.
- Keep flat-file persistence unless a concrete requirement justifies a
  database.
- Preserve token authentication, safe path resolution, atomic writes, and
  local-only defaults.
- User-facing UI strings must be present in both English and Chinese.
- Public documentation must describe the current Codex-native product, avoid
  obsolete runtime terminology, and keep the Chinese and English pages aligned.
- No real personal data, credentials, home-directory names, or private archive
  content may appear in tracked files, fixtures, logs, or examples.

## Verification

Run the narrowest relevant tests while iterating, then run the repository gates
before handing off a completed change:

```bash
uv run pytest tests/
uv run ruff check backend/ tests/
bash scripts/lint.sh
node --check frontend/app.js
```

Backend changes require tests. App-server protocol tests should use a fake
subprocess or captured sanitized messages by default; tests that require a live
Codex login must be explicit and opt-in.
