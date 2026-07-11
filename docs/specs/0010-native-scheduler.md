# Codex-native scheduler and headless turns

- **Status:** Implemented
- **Scope:** Phase 3

Scheduled prompts now persist only task metadata and run history under
`.muselab-codex/scheduler.json`. They create or resume a Codex thread, then
start a normal app-server turn; there is no second SDK client, CLI subprocess,
or legacy session JSONL involved.

The scheduler supports fresh and reusable thread modes, daily (including
multiple daily times), weekly, monthly, and one-time schedules. Each completed
or failed run creates a bounded history row and increments the existing browser
notification badge.

Headless runs use the normal approval policy instead of bypassing it. This is
intentional: an unattended task must not silently gain permission to execute
commands or edit files. Prompts that need approvals can be run interactively
from their associated thread.
