# Skills

> [简体中文](skills_zh.md) · [← Documentation index](README.md)

muselab-codex uses the Codex Skills catalog exposed by `codex app-server`.
The browser does not scan skill directories independently.

## Discovery

For the active `MUSELAB_ROOT` workspace, the backend calls `skills/list` and
shows the app-server result in Settings and the chat Skills drawer. The catalog
can include these app-server scopes:

| Scope | Meaning |
|---|---|
| `user` | A skill available from the user's Codex configuration |
| `repo` | A skill discovered for the current workspace |
| `system` | A Codex-provided system skill |
| `admin` | A skill managed by an administrator |

User and workspace skills normally use these locations:

```text
$CODEX_HOME/skills/your-skill/SKILL.md
<MUSELAB_ROOT>/.codex/skills/your-skill/SKILL.md
```

`CODEX_HOME` defaults to `~/.codex` when it is not overridden. app-server is
the authority for the final scope, precedence, metadata, and enabled state.


## Browser controls

`GET /api/chat/skills` returns the authoritative catalog. Opening the Skills
drawer requests a forced app-server reload, so a newly installed skill can
appear without restarting muselab-codex.

Settings can enable or disable a listed skill through
`PATCH /api/chat/skills`. The backend only accepts an exact path from the
current app-server list and delegates persistence to `skills/config/write`.
Disabled skills stay visible, but the Try action and prompt suggestions do not
use them.

If a skill declares a UI `defaultPrompt`, Try places that prompt in the chat
composer. Otherwise muselab-codex creates a short generic prompt using the
skill name.

## Adding a skill

Create a directory containing `SKILL.md` in a Codex discovery location. A
minimal file uses YAML frontmatter followed by Markdown instructions:

```markdown
---
name: your-skill
description: Use when the user asks for this capability.
---

# Workflow

Describe the steps, constraints, and any referenced resources here.
```

Keep the description specific enough for Codex to select the skill at the
right time. Keep scripts, references, and assets beside `SKILL.md` and refer to
them with relative paths. Reopen the Skills drawer to refresh discovery.

For the implementation contract and verification record, see
[Codex-native Skills](specs/0006-native-skills.md).
