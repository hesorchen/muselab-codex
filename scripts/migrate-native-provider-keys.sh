#!/usr/bin/env bash
# Copy the minimum deployment settings and verified native-provider keys from
# an existing private env file.
# Values never reach stdout, git, or the browser.
set -euo pipefail

SOURCE_ENV="${1:-}"
TARGET_ENV="${2:-.env}"
REQUIRED_KEYS=(
  MUSELAB_ROOT MUSELAB_TOKEN MUSELAB_PORT
  MINIMAX_API_KEY DASHSCOPE_API_KEY XIAOMI_MIMO_API_KEY
)
OPTIONAL_KEYS=(MUSELAB_HOST)
KEYS=("${REQUIRED_KEYS[@]}" "${OPTIONAL_KEYS[@]}")

if [[ -z "$SOURCE_ENV" || ! -f "$SOURCE_ENV" ]]; then
  echo "usage: $0 /path/to/legacy.env [target.env]" >&2
  exit 2
fi

umask 077
tmp="$(mktemp "${TARGET_ENV}.XXXXXX")"
trap 'rm -f "$tmp"' EXIT

if [[ -f "$TARGET_ENV" ]]; then
  pattern="^($(IFS='|'; echo "${KEYS[*]}"))="
  grep -Ev "$pattern" "$TARGET_ENV" >"$tmp" || true
fi

for key in "${REQUIRED_KEYS[@]}"; do
  line="$(grep -m1 -E "^${key}=" "$SOURCE_ENV" || true)"
  if [[ -z "$line" ]]; then
    echo "missing required native-provider key: $key" >&2
    exit 1
  fi
  printf '%s\n' "$line" >>"$tmp"
done

for key in "${OPTIONAL_KEYS[@]}"; do
  line="$(grep -m1 -E "^${key}=" "$SOURCE_ENV" || true)"
  [[ -z "$line" ]] || printf '%s\n' "$line" >>"$tmp"
done

chmod 600 "$tmp"
mv "$tmp" "$TARGET_ENV"
trap - EXIT
echo "migrated deployment settings and MiniMax, Qwen, MiMo key references into $TARGET_ENV"
