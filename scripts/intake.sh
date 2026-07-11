#!/usr/bin/env bash
# Initialize a Codex-native private workspace with AGENTS.md and folders.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="$REPO/.env"
[[ -f "$ENV_FILE" ]] || { echo 'Run the platform installer first.' >&2; exit 1; }
ARCHIVE="$(grep '^MUSELAB_ROOT=' "$ENV_FILE" | head -1 | cut -d= -f2-)"
[[ -n "$ARCHIVE" ]] || { echo 'MUSELAB_ROOT is missing from .env.' >&2; exit 1; }
mkdir -p "$ARCHIVE"

case "${LANG:-}" in
  *zh*|*ZH*) template="$REPO/scripts/templates/default-AGENTS.md" ;;
  *) template="$REPO/scripts/templates/default-AGENTS.en.md" ;;
esac

if [[ -f "$ARCHIVE/AGENTS.md" ]]; then
  printf 'AGENTS.md already exists at %s. Overwrite? [y/N] ' "$ARCHIVE" >/dev/tty
  read -r answer </dev/tty
  [[ "$answer" =~ ^[Yy]([Ee][Ss])?$ ]] || exit 0
  cp "$ARCHIVE/AGENTS.md" "$ARCHIVE/AGENTS.md.bak"
fi
cp "$template" "$ARCHIVE/AGENTS.md"
for directory in health work money people notes archives; do mkdir -p "$ARCHIVE/$directory"; done
printf 'Created Codex workspace instructions: %s/AGENTS.md\n' "$ARCHIVE"
