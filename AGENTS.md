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
