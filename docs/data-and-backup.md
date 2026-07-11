# Data & backup

> [简体中文](data-and-backup_zh.md) · [← Documentation index](README.md)

muselab-codex has no application database. Its current native state lives in
three places:

1. the workspace selected by `MUSELAB_ROOT`;
2. deployment configuration such as the service `.env`;
3. Codex CLI state under `$CODEX_HOME` (normally `~/.codex`).

## What to back up

| Path | Contains | Why it matters |
|---|---|---|
| `$MUSELAB_ROOT/` | Your files and muselab-codex workspace state | This is the user-owned workspace |
| `$MUSELAB_ROOT/.muselab-codex/attachments/threads/` | Files attached to materialized Codex threads | Required for attachment previews and the local paths stored in transcripts |
| `$MUSELAB_ROOT/.muselab-codex/usage/` | Sanitized per-thread numeric token-usage snapshots | Keeps the context meter populated after a backend restart |
| `$MUSELAB_ROOT/.muselab-codex/scheduler.json` | Scheduled tasks, run history, and unread state | Restores automation |
| `$MUSELAB_ROOT/.muselab/` | VAPID private key and push subscriptions | Keeps existing device notifications valid |
| `$MUSELAB_ROOT/.muselab-dustbin/` | Trash payloads and manifests | Restores files that were not permanently deleted |
| The active service `.env` | `MUSELAB_ROOT`, token, port, and other deployment settings | Contains secrets; store it privately and never commit it |
| `$CODEX_HOME/` | Codex configuration, authentication state, and Codex-managed thread/rollout data | Codex is the transcript source of truth |

Back up `$CODEX_HOME` only to a private encrypted location. It may contain
authentication material. If you intentionally omit authentication state, sign
in to Codex again after restoring.

The application does not use a separate session database or provider-routing
file. Codex remains the source of truth for threads and configuration.

## What can be discarded

| Path | Note |
|---|---|
| `$MUSELAB_ROOT/.muselab-codex/attachments/staged/` | Unsent uploads; safe to remove while the service is stopped |
| `<repo>/.venv/`, caches, and logs | Recreated by `uv sync` or at runtime |
| Temporary app-server schema directories | Generated from the pinned Codex CLI when needed |

## Restore outline

1. Install the same supported Codex CLI and muselab-codex revision.
2. Stop the service.
3. Restore `$MUSELAB_ROOT`, the active service `.env`, and `$CODEX_HOME`.
4. Check that `MUSELAB_ROOT`, `HOME`, and optional `CODEX_HOME` point to the restored locations.
5. Confirm `codex login status`, then start muselab-codex.
6. Open a recent thread and verify both its transcript and one attachment.
7. Check scheduled tasks and send a test notification to verify VAPID state.

Use the matching service and upgrade instructions when restoring an instance.
