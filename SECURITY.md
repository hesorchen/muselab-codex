# Security policy

muselab-codex is a single-user, self-hosted Codex workspace. It is not a multi-tenant service and should not be treated as a hardened public shell gateway.

## Threat model

Anyone holding `MUSELAB_TOKEN` can use the authenticated file APIs below `MUSELAB_ROOT`, create Codex threads, and ask Codex to execute tools under the active sandbox and approval policy. Treat the token as a password with authority over the workspace.

The main protected assets are:

- files under `MUSELAB_ROOT`;
- `.env`, including the application token and optional provider keys;
- `CODEX_HOME`, including login state, configuration, Memory, Skills, MCP, and thread history;
- prompts, transcripts, attachments, and tool outputs;
- the operating-system account running Codex and MCP subprocesses.

## Supported deployment baseline

- run as a dedicated, unprivileged operating-system user;
- keep `MUSELAB_HOST=127.0.0.1` unless a trusted network boundary exists;
- use a long random `MUSELAB_TOKEN` and rotate it after suspected exposure;
- use HTTPS plus an additional access-control layer for remote access;
- keep the upstream port unreachable from the public internet;
- keep `.env`, `CODEX_HOME`, and workspace backups private and encrypted;
- mount only the intended workspace into a container, never an entire host home directory;
- review MCP servers and Skills as executable extensions with the service user's authority.

## Security invariants

### Authentication

Meaningful browser APIs require `MUSELAB_TOKEN`. Standard requests use `X-Auth-Token`. Browser channels that cannot set custom headers, such as SSE and raw downloads, use constrained query credentials; the application redacts them from its access log and sends a restrictive referrer policy.

`/api/health` is intentionally unauthenticated and returns only application version and runtime readiness. Workspace paths and detailed diagnostics remain authenticated.

### Filesystem containment

File operations resolve below `MUSELAB_ROOT`. The backend rejects path traversal, escaping symlinks, dangerous root workspaces, and credential-shaped internal files. User-visible text writes use atomic replacement, and normal deletion uses a workspace trash area.

Containment does not make hostile files safe to execute. Codex tools, terminal commands, previews, and MCP servers must still be governed by an appropriate sandbox and approval policy.

### Codex process boundary

`codex app-server` runs as a private stdio child, not a network service. Initialization follows the app-server handshake, stdout remains protocol-only JSONL, and stderr is consumed separately without retaining sensitive lines.

The process supervisor terminates only the app-server process it created. Mutating requests are not automatically retried after an ambiguous transport failure.

### Credentials

The browser never receives native Provider API key values. Keys are inherited from the private service environment, while provider definitions are written through app-server to Codex configuration.

The default app-server child environment removes `OPENAI_API_KEY`; the primary path uses the explicitly authenticated Codex CLI state. `CODEX_HOME` remains Codex-owned and must never be copied into a repository, image layer, issue attachment, or public log.

### Logging and public artifacts

Application logs must not include tokens, OAuth material, API keys, raw prompts, file contents, or full protocol payloads. Tests use throwaway workspaces and fake app-server peers by default. Live checks must use ephemeral threads and must not read a developer's private archive.

## Remote access

Binding directly to `0.0.0.0` makes token authentication the only application barrier. Prefer a private overlay network, authenticated tunnel, or reverse proxy with TLS and independent access control. Configure proxies to disable SSE buffering and redact query strings from access logs.

`scripts/setup-https.sh` can install a Caddy reverse proxy on supported Linux hosts, but operators remain responsible for DNS, firewall rules, upstream isolation, and access policy.

## Provider and MCP risk

Enabling a native model provider sends prompts and relevant tool context to that provider according to Codex behavior. Review the provider's data policy before use.

MCP servers and Skills can expand tool authority. Only install sources you trust, inspect commands and environment-variable references, and disable entries that are not required.

## Reporting a vulnerability

Email **hesorchen@gmail.com** with the subject `muselab-codex security`. Include the affected revision, impact, reproduction steps, and a minimal sanitized proof of concept. Do not open a public issue for a vulnerability involving file access, credentials, authentication, sandbox escape, or command execution.
