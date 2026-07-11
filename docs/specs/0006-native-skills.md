# Codex-native Skills

- **Status:** Implemented
- **Scope:** Phase 2
- **Protocol baseline:** `codex-cli 0.144.1` stable app-server API

## Goal

Replace the application-owned skill scanner and read-only UI with the
Skills state that Codex app-server actually uses for the active workspace.
muselab-codex must not parse or inject a second, divergent skill catalog.

## Protocol mapping

The FastAPI lifespan creates one `CodexSkillsService` beside the shared
app-server runtime. Its operations map directly to stable protocol methods:

| muselab-codex operation | app-server request |
|---|---|
| List or refresh the current workspace | `skills/list` with `cwds` and `forceReload` |
| Enable or disable a listed skill | `skills/config/write` with `path` and `enabled` |

The HTTP adapter exposes authenticated `GET /api/chat/skills` and
`PATCH /api/chat/skills`. It normalizes display name, short description,
scope, path, enabled state, and optional default prompt for the existing UI.
Discovery errors are returned separately instead of being represented as
usable skills.

Before writing configuration, the backend refreshes the current list and
requires an exact known path. This prevents the browser endpoint from being
used as an arbitrary configuration-path writer. After a write it force
reloads app-server state and returns the effective state.

## Discovery boundary

Codex app-server is the source of truth. Current user and workspace skills
normally live under `$CODEX_HOME/skills/` and `<workspace>/.codex/skills/`;
app-server may also report system or admin scopes. The UI shows the scope
reported by app-server rather than inferring it from paths.

The inherited `<repository>/skills/` collection is intentionally not passed
to `skills/extraRoots/set`. Those files target a different runtime and may
contain incompatible tools, commands, or assumptions. Individual
skills can be migrated after review into a Codex discovery location.

## Browser behavior

- Settings and the chat Skills drawer share the native catalog.
- Opening the drawer asks app-server to reload, so newly installed skills can
  appear without restarting muselab-codex.
- Disabled skills remain visible but cannot seed a prompt or appear in prompt
  suggestions.
- Enable and disable actions show localized success or failure feedback.
- A skill interface `defaultPrompt`, when present, is used by the Try action.

## Verification

Offline tests cover protocol normalization, discovery errors, exact-path
write validation, HTTP authentication and configuration, fake app-server
state changes, and frontend route/disabled-state contracts.

A real `codex-cli 0.144.1` check used isolated `CODEX_HOME` and workspace
directories. app-server discovered the temporary repo-scoped skill, disabled
it through `skills/config/write`, then restored it; forced reloads reflected
both changes without errors.

## Deferred work

- extra-root management for reviewed or user-selected skill collections;
- installing, authoring, or migrating Skills from the browser;
- richer surfacing of app-server discovery errors;
- scheduler and headless-turn integration.
