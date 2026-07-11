#!/usr/bin/env bash
# Upgrade the project dependencies and report the pinned Codex CLI baseline.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"
source scripts/versions.env

printf 'muselab-codex upgrade\n\n'
if command -v codex >/dev/null 2>&1; then
  printf 'Installed Codex: %s\n' "$(codex --version | head -1)"
else
  printf 'Codex CLI is missing. Install the tested baseline:\n  npm install -g @openai/codex@%s\n' "$CODEX_CLI_VERSION" >&2
  exit 1
fi

printf 'Updating Python lockfile…\n'
uv lock
uv sync --frozen
printf 'Running checks…\n'
uv run pytest tests/ -q
uv run ruff check backend/ tests/
bash scripts/lint.sh
node --check frontend/app.js

printf '\nComplete. The tested Codex CLI baseline is %s.\n' "$CODEX_CLI_VERSION"
printf 'Restart Linux service: systemctl --user restart muselab\n'
printf 'Restart macOS agent: launchctl kickstart -k gui/%s/com.muselab\n' "$(id -u)"
