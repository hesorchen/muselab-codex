#!/usr/bin/env bash
# Install muselab-codex as a systemd user service on Linux or WSL2.
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

[[ $EUID -ne 0 ]] || die 'do not run the user-service installer with sudo'
command -v systemctl >/dev/null 2>&1 || die 'systemd user services are required'
if ! command -v uv >/dev/null 2>&1; then
  die 'uv is required; install it from https://docs.astral.sh/uv/'
fi
if ! command -v npm >/dev/null 2>&1; then
  die 'npm is required to install Codex; install a supported Node.js release first'
fi

printf 'muselab-codex Linux installer\n\n'
if ! command -v codex >/dev/null 2>&1; then
  printf 'Installing Codex CLI %s…\n' "$CODEX_CLI_VERSION"
  npm install -g "@openai/codex@$CODEX_CLI_VERSION"
fi
ok "codex: $(codex --version | head -1)"
if ! codex login status >/dev/null 2>&1; then
  warn 'Codex is not logged in. Run `codex login` in this terminal, then re-run this installer.'
  exit 1
fi
ok 'Codex login is active'

uv sync --frozen
ok 'Python dependencies installed'

if [[ ! -f .env ]]; then
  root="${MUSELAB_ROOT:-$(ask 'Workspace directory' "$HOME/muselab-workspace")}"; root="${root/#\~/$HOME}"
  mkdir -p "$root"
  port="${MUSELAB_PORT:-$(ask 'Local port' '8765')}"
  token="$(uv run python -c 'import secrets; print(secrets.token_hex(24))')"
  (umask 077; printf 'MUSELAB_TOKEN=%s\nMUSELAB_ROOT=%s\nMUSELAB_PORT=%s\nMUSELAB_HOST=127.0.0.1\n' "$token" "$root" "$port" > .env)
  ok '.env created with a local-only listener'
fi

unit_dir="$HOME/.config/systemd/user"
unit="$unit_dir/muselab.service"
mkdir -p "$unit_dir"
uv_path="$(command -v uv)"
sed -e "s|{{REPO_PATH}}|$REPO|g" -e "s|{{UV_PATH}}|$uv_path|g" \
  scripts/templates/muselab.service.tmpl > "$unit"
systemctl --user daemon-reload
systemctl --user enable --now muselab.service
ok "service started: http://127.0.0.1:$(grep '^MUSELAB_PORT=' .env | cut -d= -f2-)"
printf '\nNext: bash scripts/doctor.sh\n'
