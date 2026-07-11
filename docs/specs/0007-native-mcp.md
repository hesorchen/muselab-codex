# Codex-native MCP

- **Status:** Implemented
- **Scope:** Phase 2
- **Protocol baseline:** `codex-cli 0.144.1` stable app-server API

## Goal

Replace the inherited provider-specific configuration scanner and muselab-owned
`mcp.json` UI path with the MCP configuration and live inventory that Codex
actually uses. The browser must not show a server as available merely because
it exists in a second configuration store.

## Source of truth

Codex owns MCP configuration in user or trusted-project `config.toml` files.
The FastAPI lifespan creates one `CodexMcpService` beside the shared
app-server runtime. Its operations map to stable protocol methods:

| muselab-codex operation | app-server request |
|---|---|
| Read effective configuration | `config/read` with the workspace `cwd` and layers |
| List actual tools, resources, and auth state | paginated `mcpServerStatus/list` |
| Add, enable, disable, or delete a user server | `config/value/write` under `mcp_servers.<name>` |
| Hot-reload after a write | `config/mcpServer/reload` |
| Start remote-server OAuth | `mcpServer/oauth/login` |

The authenticated HTTP adapter exposes these operations below
`/api/chat/mcp`. Normal turns do not need an extra adapter: app-server mounts
the enabled MCP tools and owns model-initiated calls.

Live inventory runs through a short-lived, isolated app-server process. A
remote MCP server can block `mcpServerStatus/list` while it initializes; doing
that on the main process would also delay unrelated thread requests. Inventory
timeouts therefore degrade to config-only results with an explicit warning,
while the shared chat runtime stays responsive. Results and failures are
briefly cached to avoid duplicate probes.

## Configuration boundary

The UI supports Codex's two native transports:

- local STDIO with `command` and `args`;
- remote Streamable HTTP with `url` and optional
  `bearer_token_env_var`.

New servers are written to the user Codex configuration. Effective servers
from a trusted project, managed layer, or plugin remain visible and usable but
are read-only in this settings surface. This prevents a user-config action
from silently editing or pretending to override a higher-precedence layer.

Server names accepted for browser writes are restricted to ASCII letters,
numbers, `_`, and `-`. Remote URLs must be HTTP or HTTPS and may not contain
embedded credentials. Deletion uses the app-server-supported `null` value for
the exact server key, followed by a native reload.

## Secret handling

The inventory response deliberately omits environment values and static HTTP
header values. It reports only that those fields exist. The browser form asks
for a bearer-token environment-variable name, not a bearer token or raw
`Authorization` header. OAuth credentials and callbacks remain owned by Codex.

Command arguments are shown because they are part of the user-visible launch
configuration; users should place credentials in environment variables rather
than command-line arguments.

## Browser behavior

- Settings and the chat MCP drawer share the native inventory.
- Opening Settings forces a native reload before refreshing the list.
- Each server shows its config layer, enabled state, discovered tool count,
  and tool names.
- Project, managed, and plugin servers have disabled edit controls.
- OAuth-capable servers in `notLoggedIn` state expose an Authenticate action.
- Try is disabled until app-server reports at least one usable tool.

## Verification

Offline tests cover config/inventory merging, pagination shape validation,
secret redaction, user-layer ownership, write and reload sequences, transport
and credential validation, OAuth, authenticated HTTP routes, and frontend
syntax.

An isolated real `codex-cli 0.144.1` check verified add, read, hot reload,
status listing, and `null` deletion without reading or changing the developer's
real Codex configuration.

The deployed read-only check also covered a remote server whose inventory
request exceeded the bounded probe timeout. `/api/chat/mcp` returned the
sanitized config-only result with `inventory_error=unavailable`; a concurrent
model-list request on the shared runtime still returned successfully, proving
the stalled probe no longer blocks chat traffic.

## Deferred work

- richer startup-failure state from `mcpServer/status/updated` notifications;
- browser resource inspection through `mcpServer/resource/read`;
- an explicit diagnostic tool-call surface through `mcpServer/tool/call`;
- advanced per-server tool allow lists and approval policy editing.

These are optional management and diagnostic surfaces. They are not required
for MCP tools to be available inside normal Codex turns.
