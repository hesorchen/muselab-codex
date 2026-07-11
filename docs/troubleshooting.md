# Troubleshooting

> [简体中文](troubleshooting_zh.md) · [← Documentation index](README.md)

Start with `bash scripts/doctor.sh`. It checks runtime, Codex login, `.env`, dependencies, service manager, and HTTP without printing credential values.

## Service is unreachable

```bash
curl -v http://127.0.0.1:8765/api/health
systemctl --user status muselab
journalctl --user -u muselab -n 100
```

Check for an invalid `.env`, missing Codex CLI in the service PATH, or another listener on port 8765. Do not run a manual process beside the managed service; the browser may connect to stale code.

If health remains `starting`, verify `codex --version`, `codex login status`, and the service log.

## Authentication returns 401

The browser token does not match the `.env` used by the running process. Re-enter the current token. Scripts use `X-Auth-Token`, not `Authorization: Bearer`.

## Thread and streaming errors

- `turn/start ... -32600` usually means a thread was not loaded in the current app-server generation. The backend resumes persisted threads before starting a new turn; verify that the service runs the latest revision.
- Large histories may exceed `MUSELAB_CODEX_HISTORY_READ_TIMEOUT_SECONDS`. Prefer compact or a summary thread before increasing the timeout.
- An SSE disconnect does not cancel the Codex turn. Reopen the thread to attach/replay, or use Stop to send an interrupt.

## Providers

If **Settings → Models** is empty, hard-refresh and query `/api/settings/providers` with `X-Auth-Token`. A 404 usually means an old process still owns the port. Configuration reads can briefly warm after restart, so the frontend retries.

If an enabled provider fails authentication, ensure its key is in the environment inherited by the managed service, then restart. The UI stores provider definitions, never key values.

Web Search is intentionally disabled for MiniMax, Qwen, and MiMo. Local file, terminal, Skill, and MCP tools remain available.

## MCP and Skills

For MCP, refresh inventory, inspect enabled/auth state, and ensure STDIO commands exist in the service PATH. Never paste bearer tokens into logs.

Install Skills in native Codex locations such as `$CODEX_HOME/skills/` or workspace `.codex/skills/`, then reopen the drawer to force reload.

## Files and attachments

File errors commonly indicate traversal, an escaping symlink, a blocked sensitive path, or an unsupported preview. Attachments are limited to 10 MiB each, text to 200 KiB, and eight items per send; text must be UTF-8.

## Browser and PWA

Hard-refresh after deployment if the tab still uses old assets. Mobile installation and notifications require HTTPS on iOS and most browsers.

## Before reporting an issue

Include the Git revision, `codex --version`, OS/install mode, sanitized `/api/health` output, error code, and minimal reproduction. Never include `.env`, credentials, prompts, transcripts, workspace files, or unsanitized logs.
