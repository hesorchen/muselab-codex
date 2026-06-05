# SDK Bump Checklist

> Maintainer-only. Run this every time you raise the `claude-agent-sdk` pin in
> `pyproject.toml`. muselab is an **adapter** over the SDK: it hand-maintains a
> few assumptions the SDK does not expose as a programmatic contract, so a minor
> SDK bump can silently break them with zero signal. The `<0.3` upper bound on
> the dependency exists to force this checklist to run instead of letting any
> `>=` upgrade slip through.

## Why a checklist (and not just `>=`)

Two classes of assumption have **no auto-diff** and must be eyeballed on each bump:

1. **Harness-tool denylist** — `backend/chat.py` `disallowed_tools` is a hand-kept
   blacklist. The SDK exposes no programmatic tool catalog (`builtin_types` is the
   stdlib `types` module, not a tool list), so a newly-added harness tool is
   **silently exposed** to the model until someone notices.
2. **CLI JSONL transcript format** — `backend/jsonl_cleanup.py`,
   `_full_session_msgs`, `_find_session_jsonl`, `_compact_summary_uuids` parse the
   CLI's private `*.jsonl` (`type` / `uuid` / `message` / `content[]` /
   `isCompactSummary`). A format change breaks parsing.

Two **other** drift classes are already auto-eliminated (don't re-check manually):

- **Project-dir path encoding** → `_cli_encode_cwd` delegates to the SDK's
  `project_key_for_directory()`. No drift possible.
- **Effort tiers** → `_VALID_EFFORT = get_args(EffortLevel)`. New tiers honored
  automatically.

## Checklist

Run from repo root in the project venv (`.venv/bin/python`).

- [ ] **1. Denylist vs current harness tools.** Diff the SDK's bundled CLI tool
  set against `disallowed_tools` in `backend/chat.py`. There's no programmatic
  catalog, so check the SDK release notes / CLI `--help` for newly-added tools and
  decide allow-vs-deny for each. A new tool defaults to **exposed** — treat any
  unrecognized one as deny-until-reviewed.

- [ ] **2. JSONL field assumptions still hold.** Confirm the CLI still writes
  `type` / `uuid` / `message.content[]` / `isCompactSummary` as parsed in
  `jsonl_cleanup.py` and `chat.py`. Spot-check a real transcript after one turn on
  the new SDK.

- [ ] **3. `Message` union members.** Re-run the probe below; confirm no message
  type muselab dispatches on was renamed/removed, and eyeball new members for ones
  worth surfacing (this is how `RateLimitEvent` got picked up).

- [ ] **4. `ClaudeAgentOptions` fields.** Diff the dataclass field set; note new
  fields for the capability-alignment backlog (`TODO.md`).

- [ ] **5. Symbols muselab imports still exist.** `project_key_for_directory`,
  `EffortLevel`, `RateLimitEvent`, `get_session_messages`, the `*_session` helpers,
  `ThinkingConfig*`, the `Task*Message` types.

- [ ] **6. Run the suite.** `ruff check`, `pytest`, `node --check frontend/app.js`.

### Probe snippet

```bash
.venv/bin/python - <<'PY'
import claude_agent_sdk as s
from typing import get_args
import dataclasses as dc
print("version:", getattr(s, "__version__", "?"))
print("EffortLevel:", get_args(s.EffortLevel))
print("ClaudeAgentOptions fields:",
      sorted(f.name for f in dc.fields(s.ClaudeAgentOptions)))
# Sanity: symbols muselab imports
for name in ("project_key_for_directory", "RateLimitEvent", "RateLimitInfo",
             "get_session_messages", "TaskStartedMessage"):
    print(f"  {name}:", hasattr(s, name))
PY
```

## After a clean bump

Raise both the lower pin **and** the `<0.3` upper bound in `pyproject.toml` only
if the new version crosses a minor line you've fully vetted; otherwise keep the
bound and just move the lower pin.
