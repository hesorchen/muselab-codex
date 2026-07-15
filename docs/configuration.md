# Configuration

> [简体中文](configuration_zh.md) · [← Documentation index](README.md)

Configuration has three authoritative layers:

| Layer | Authority | Examples |
|---|---|---|
| Deployment and workspace | repository `.env` / process environment | host, port, token, `MUSELAB_ROOT` |
| Codex user configuration | `CODEX_HOME` | login, `config.toml`, Memory, user Skills, MCP |
| Workspace configuration | `MUSELAB_ROOT` | `AGENTS.md`, `.codex/`, workspace Skills |

The browser does not create a fourth configuration system. Model, Skill, and MCP changes are written through app-server to native Codex configuration.

## Application environment

| Variable | Required | Default | Purpose |
|---|:---:|---|---|
| `MUSELAB_TOKEN` | yes | none | At least 16 characters; protects meaningful HTTP and SSE operations |
| `MUSELAB_ROOT` | yes | none | Existing absolute workspace owned by the service user |
| `MUSELAB_HOST` | no | `127.0.0.1` | Listen address; do not use `0.0.0.0` without a controlled network boundary |
| `MUSELAB_PORT` | no | `8765` | Listen port |
| `CODEX_BIN` | no | discovered `codex` | Explicit Codex executable |
| `MUSELAB_CODEX_HISTORY_READ_TIMEOUT_SECONDS` | no | `8` | Client timeout for large history reads |
| `MUSELAB_CODEX_COMPACT_TIMEOUT_SECONDS` | no | `600` | Maximum compact-summary wait |
| `MUSELAB_VAPID_SUBJECT` | no | `mailto:noreply@muselab.dev` | Web Push VAPID subject; must be a `mailto:` address |

`MUSELAB_ROOT` is the default workspace. It must exist and cannot be a system root such as `/`, `/home`, or `/etc`. Additional existing directories can be registered from the workspace picker; they pass the same broad/sensitive-root validation before file APIs or new threads can use them. File endpoints additionally reject traversal, escaping symlinks, and credential-shaped files inside the selected workspace.

## Workspace and Codex state

`AGENTS.md` is the native workspace instruction file. `scripts/intake.sh` can create a starter file and neutral directory skeleton. Workspace `.codex/` may contain Codex configuration and Skills.

`CODEX_HOME` is normally `~/.codex` and may contain `config.toml`, login credentials, user Skills, Memory, MCP configuration, and native thread history. It must remain private and writable by the service user.

## Native providers

| ID | Model | Base URL | Environment variable |
|---|---|---|---|
| `minimax` | `minimax-m2.7` | `https://api.minimaxi.com/v1` | `MINIMAX_API_KEY` |
| `qwen` | `qwen3.7-plus` | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `DASHSCOPE_API_KEY` |
| `mimo` | `mimo-v2.5-pro` | `https://api.xiaomimimo.com/v1` | `XIAOMI_MIMO_API_KEY` |

Put the key in the private service environment, restart so app-server inherits it, enable the provider in Settings, and select the model in a new thread. The switch writes `model_providers.<id>` through `config/value/write`; the browser never receives the key value.

All three use `wire_api = "responses"`. Web Search is disabled for compatibility, while native file, terminal, Skill, and MCP tools remain available.

## What the UI can change

| Setting | Persistence |
|---|---|
| Provider enablement | Codex `config.toml` `model_providers` |
| Skill enablement | app-server `skills/config/write` |
| MCP servers | app-server MCP configuration APIs |
| Thread model, approval policy, effort, Fast tier | Codex thread/turn state; explicit choices omitted by stable reads use a minimal compatibility sidecar |
| Theme and layout | browser local storage |

The UI does not modify `MUSELAB_ROOT`, listen address, primary token, or provider keys. Deployment changes require a private environment update and restart.

Fast is a native Codex `serviceTier`, not an Effort level. The control appears
only when the current model advertises a tier named Fast in
`model/list.serviceTiers`; the native tier id is catalog-driven (`priority` in
the Codex 0.144.1 catalog), rather than a hard-coded UI value. It generally
produces output faster while consuming more account credits; Standard/Fast is
saved per thread and applies from the next turn.

## Docker

Compose maps the workspace to `/data` and `CODEX_HOME` to `/home/muse/.codex`. Both are private writable volumes. Port 8765 binds to loopback by default.

## Reload behavior

| Change | Restart required? |
|---|:---:|
| `.env`, provider key, `CODEX_BIN` | yes |
| Native provider switch | normally no; applies to new threads |
| `AGENTS.md` | loaded by later Codex threads/turns according to native rules |
| Skill install/remove | reopen the Skills drawer to force reload |
| MCP configuration | refresh/reload through Settings |
