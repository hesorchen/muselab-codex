#!/usr/bin/env bash
# Install muselab-codex as a launchd user agent on macOS.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"
source scripts/versions.env

ok() { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
die() { printf '  \033[31m✗\033[0m %s\n' "$*" >&2; exit 1; }
ask() {
  local prompt="$1" default="$2" answer=""
  if [[ "${MUSELAB_NONINTERACTIVE:-0}" == "1" ]]; then printf '%s\n' "$default"; return; fi
  read -r -p "  $prompt [$default] " answer </dev/tty
  printf '%s\n' "${answer:-$default}"
}

[[ "$(uname -s)" == "Darwin" ]] || die 'this installer is for macOS'
command -v uv >/dev/null 2>&1 || die 'uv is required; install it from https://docs.astral.sh/uv/'
command -v npm >/dev/null 2>&1 || die 'npm is required to install Codex; install Node.js first'

printf 'muselab-codex macOS installer\n\n'
if ! command -v codex >/dev/null 2>&1; then npm install -g "@openai/codex@$CODEX_CLI_VERSION"; fi
ok "codex: $(codex --version | head -1)"
if ! codex login status >/dev/null 2>&1; then
  warn 'Codex is not logged in. Run `codex login` in this terminal, then re-run this installer.'
  exit 1
fi

uv sync --frozen
if [[ ! -f .env ]]; then
  root="${MUSELAB_ROOT:-$(ask 'Workspace directory' "$HOME/muselab-workspace")}"; root="${root/#\~/$HOME}"
  mkdir -p "$root"
  port="${MUSELAB_PORT:-$(ask 'Local port' '8765')}"
  token="$(uv run python -c 'import secrets; print(secrets.token_hex(24))')"
  (umask 077; printf 'MUSELAB_TOKEN=%s\nMUSELAB_ROOT=%s\nMUSELAB_PORT=%s\nMUSELAB_HOST=127.0.0.1\n' "$token" "$root" "$port" > .env)
  ok '.env created with a local-only listener'
fi

plist_dir="$HOME/Library/LaunchAgents"
plist="$plist_dir/com.muselab.plist"
log_dir="$HOME/Library/Logs/muselab"
mkdir -p "$plist_dir" "$log_dir"
path_dirs="$(dirname "$(command -v uv)"):$(dirname "$(command -v codex)"):/usr/local/bin:/usr/bin:/bin"
sed -e "s|{{UV_PATH}}|$(command -v uv)|g" -e "s|{{REPO_PATH}}|$REPO|g" \
    -e "s|{{PATH_DIRS}}|$path_dirs|g" -e "s|{{HOME_DIR}}|$HOME|g" \
    scripts/templates/com.muselab.plist.tmpl > "$plist"
launchctl bootout "gui/$(id -u)/com.muselab" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$plist"
ok "agent started: http://127.0.0.1:$(grep '^MUSELAB_PORT=' .env | cut -d= -f2-)"
printf '\nNext: bash scripts/doctor.sh\n'
