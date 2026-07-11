# Contributing to muselab-codex

muselab-codex is a Codex-native, self-hosted AI workspace under active development. Read [AGENTS.md](AGENTS.md) and [Architecture](docs/architecture.md) before changing the agent-runtime path.

## Development setup

```bash
git clone https://github.com/hesorchen/muselab-codex
cd muselab-codex
uv sync
uv run pytest tests/test_codex_app_server.py
```

The frontend remains plain HTML, Alpine.js, and CSS with no build step.

## Welcome changes

- Codex app-server process, protocol, and lifecycle fixes with offline tests.
- Native thread, turn, streaming, tool, approval, Skills, and MCP integration.
- File-management, preview, PWA, authentication, and accessibility fixes.
- English and Chinese documentation updates that describe implemented behavior.

## Changes that will be declined

- Any compatibility runtime or provider that bypasses Codex app-server.
- A second model runtime, a direct model API, or a protocol-conversion gateway.
- Experimental app-server APIs without a requirement, fallback, and protocol test.
- A frontend bundler, transpiler, or required npm build step.
- Document RAG or pre-indexing over the user's workspace.
- Tests that read a contributor's real files or require real personal data.

## Protocol changes

The initial baseline is `codex-cli 0.144.1` with stable app-server APIs over stdio. When changing the baseline:

1. generate version-matched stable JSON Schema;
2. record the CLI version and v2 schema SHA-256;
3. update offline contract fixtures;
4. run an opt-in live check in a temporary workspace;
5. update both architecture documents when behavior changes.

Do not commit the complete generated schema tree by default.

## Pull request checklist

- [ ] `uv run pytest tests/` passes, or a known teardown limitation is documented accurately.
- [ ] `uv run ruff check backend/ tests/` passes.
- [ ] `bash scripts/lint.sh` passes.
- [ ] `node --check frontend/app.js` passes when frontend code changes.
- [ ] Backend behavior has deterministic offline tests.
- [ ] Live Codex tests are explicit, ephemeral, and use a temporary workspace.
- [ ] User-facing strings exist in both English and Chinese.
- [ ] No secrets, personal data, prompts, file contents, or home-directory names appear in tracked artifacts or logs.
- [ ] No additions to `.env`, `sessions/`, or Codex authentication state.

## Code style

- Python: PEP 8, type hints on public functions, no broad formatter churn.
- JavaScript: modern-browser syntax that runs directly, matching neighboring style.
- CSS: component sections and existing CSS variables; do not hardcode theme colors.
- Disk writes: explicit UTF-8 and atomic writes for user-visible application state.

## Security reports

Follow [SECURITY.md](SECURITY.md). Do not open a public issue for an exploitable authentication, file-access, sandbox, or credential bug.
