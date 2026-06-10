# Skills

> [简体中文](skills_zh.md)

Skills are SKILL.md instruction packs that the Claude Agent SDK loads at
startup and makes available to Muse. When a task matches a skill's trigger,
the model reads the skill's body and follows its protocol — no extra wiring
required on your end. Skills work the same way in interactive chat,
[scheduled tasks](scheduler.md), and any other context that runs the full
agent loop.

**Example.** A skill called `changelog-formatter` might have a
`description` starting with `"USE WHEN the user asks to format or generate
a CHANGELOG entry"`. Whenever you ask Muse to write a changelog, the SDK
surfaces that skill and the model adopts its output conventions
automatically.

---

## Bundled skills

Muselab ships 11 skills out of the box. The first seven are muselab-native
(MIT); the last four are community-contributed and included with
attribution — see [`THIRD_PARTY_LICENSES.md`](../THIRD_PARTY_LICENSES.md#L47-L73) for
upstream URLs and license details.

| Skill | What it does | Origin | External deps |
|---|---|---|---|
| `web-search` | Translates vague queries into targeted searches, opens at least one source to confirm recency, returns a cited answer with dates | muselab-native | `WebSearch` / `WebFetch` tool or `mcp__fetch__fetch` |
| `markdown-formatter` | Normalises heading hierarchy, lists, tables, code fences, math delimiters, and Chinese full-width punctuation; returns the rewritten doc only | muselab-native | none |
| `mermaid-helper` | Picks the right Mermaid diagram type, writes validated syntax, returns a fenced block plus a short explanation | muselab-native | none |
| `code-reviewer` | Reviews code by severity order (bugs → security → correctness → performance → maintainability), with line references and fix snippets | muselab-native | none |
| `citation-formatter` | Converts DOIs, arXiv IDs, PubMed IDs, and raw text into APA 7 / IEEE / GB/T 7714 / BibTeX; fetches authoritative metadata when possible | muselab-native | `WebFetch` or `mcp__fetch__fetch` (optional) |
| `task-decomposer` | Turns a vague goal into an ordered task list with size estimates, a Definition of Done, critical-path steps, and flagged unknowns | muselab-native | none |
| `summary-distiller` | Picks the right summary shape (TL;DR, key points, structured, action items) based on source type; preserves numbers, names, and dates verbatim | muselab-native | none |
| `pptx` | Generates PowerPoint files by writing and running inline Python with `python-pptx` via the Bash tool | [community](../THIRD_PARTY_LICENSES.md#L63) | `python-pptx` (`pip install python-pptx`) |
| `csv-analyzer` | Loads a CSV with `pandas`, profiles column types, generates conditional charts (PNG), outputs a complete analysis in one response | [community](../THIRD_PARTY_LICENSES.md#L64) | `pandas`; `matplotlib`/`seaborn` optional |
| `translate` | Three-stage internal pipeline (literal → issue identification → polished reinterpretation); outputs final Chinese text only, preserving technical terms | [community](../THIRD_PARTY_LICENSES.md#L65) | none |
| `meeting-notes` | Extracts decisions, action items (with owners and due dates), and next steps from raw notes or transcripts using four ready-made templates | [community](../THIRD_PARTY_LICENSES.md#L66) | none |

---

## How discovery works

Skill discovery is controlled by two parameters passed to
`ClaudeAgentOptions` in [`backend/chat.py`](../backend/chat.py):

**`setting_sources`** ([`chat.py:L944`](../backend/chat.py#L944)):

```python
setting_sources=["user", "project", "local"]
```

This tells the SDK to load CLAUDE.md, memory files, and skills from three
scopes:

| Scope | Resolves to |
|---|---|
| `user` | `~/.claude/` — user-global config shared with Claude Code CLI |
| `project` | the archive `cwd` (see below) |
| `local` | `.claude/` inside `cwd` |

**`cwd` is the archive root** ([`chat.py:L902`](../backend/chat.py#L902),
[`backend/settings.py:L188-L194`](../backend/settings.py#L188-L194)):

```python
cwd=str(ROOT)   # ROOT comes from MUSELAB_ROOT in .env
```

The SDK's `local` scope therefore resolves the bundled `skills/` directory
from the muselab repo (which is the checkout that contains your `.env`).
Output files produced by skills such as `pptx` or `csv-analyzer` land in
the archive root unless you specify an explicit path.

**`skills="all"`** ([`chat.py:L961`](../backend/chat.py#L961)):

```python
if not is_third_party and not skills_off:
    opts_kwargs["skills"] = "all"
```

When this flag is set, the SDK loads every discoverable `SKILL.md` and
makes it available to the model. There is no copy or symlink step — the
bundled `skills/` directory is served directly from the repo checkout.

**UI listing.** The `GET /api/settings/skills` endpoint
([`api_settings.py:L1129-L1143`](../backend/api_settings.py#L1129-L1143))
independently enumerates skills for the frontend from three paths:
the repo's `skills/` (project scope), `~/.claude/skills/` (user scope),
and `~/.claude/plugins/marketplaces/*/plugins/*/skills/` (plugin scope).
Both `SKILL.md` and `skill.md` filenames are accepted
([`api_settings.py:L1077`](../backend/api_settings.py#L1077)). This listing
is read-only — it has no effect on what the model actually uses at runtime.

---

## Adding your own skill

### Where to put it

| Location | Scope | Visible to |
|---|---|---|
| `<muselab-repo>/skills/your-skill/SKILL.md` | project | muselab only |
| `~/.claude/skills/your-skill/SKILL.md` | user | muselab + all Claude Code projects |

When two skills share the same name, the project-scope skill takes
precedence over the user-scope one at runtime.

### Required structure

```
skills/your-skill/
└── SKILL.md          ← must contain YAML frontmatter
```

The frontmatter block must include at minimum `name` and `description`:

```yaml
---
name: your-skill
description: "USE WHEN ... — one sentence describing the trigger and capability"
---
```

The body is free-form Markdown that the model reads on every invocation —
keep it concise. Recommended practices (from [`skills/README.md`](../skills/README.md)):

- Start `description` with `"USE WHEN ..."` — this is the primary signal
  the model uses to select a skill.
- Use a table to map scenarios to actions.
- Include a `NOT use when` section to prevent overuse.
- Optional: place reference scripts (`*.py`) or config (`config.yaml`) in
  the same subdirectory and reference them from the SKILL.md body.

### Restart required

Skills are loaded during SDK initialisation. After adding or editing a
skill, restart the muselab service:

**Linux (systemd):**
```bash
systemctl --user restart muselab
```

**macOS (launchd):**
```bash
launchctl kickstart -k "gui/$(id -u)/com.muselab"
```

---

## Caveats

### Skills are disabled on third-party providers

When a session uses a third-party model (DeepSeek, GLM / ZhipuAI, MiniMax,
and others detected by `endpoints.is_third_party()`), muselab omits
`skills="all"` from the SDK options entirely
([`chat.py:L958-L961`](../backend/chat.py#L958-L961)). The code comment
explains the reason directly:

> "third-party vendors (DeepSeek / GLM / MiniMax) often time out or 400 on
> the bigger payload"

Skills are injected into the system prompt as additional content; the
enlarged payload reliably triggers timeouts or HTTP 400 responses from
several vendors. Rather than failing silently mid-conversation, muselab
disables skills for all third-party sessions. See [routing.md](routing.md)
and [providers.md](providers.md) for more on the third-party environment.

### Kill switch

To disable skills even for Claude models, set the following in your `.env`:

```
MUSELAB_DISABLE_SKILLS=1
```

Accepted values: `1`, `true`, `yes` (case-insensitive)
([`chat.py:L959`](../backend/chat.py#L959)).

---

*Related: [architecture.md](architecture.md) · [routing.md](routing.md) · [providers.md](providers.md)*
