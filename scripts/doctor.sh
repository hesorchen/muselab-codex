#!/usr/bin/env bash
# Diagnose a local muselab-codex installation without reading credentials.
set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"
FAIL=0
WARN=0

ok() { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; WARN=$((WARN + 1)); }
err() { printf '  \033[31m✗\033[0m %s\n' "$*" >&2; FAIL=$((FAIL + 1)); }
value() { grep -E "^$1=" .env 2>/dev/null | head -1 | cut -d= -f2- | tr -d '[:space:]'; }

printf 'muselab-codex doctor\n\n'
printf '1. Runtime\n'
if command -v uv >/dev/null 2>&1; then ok "uv: $(uv --version 2>&1)"; else err "uv not found"; fi
if command -v codex >/dev/null 2>&1; then
  ok "codex: $(codex --version 2>&1 | head -1)"
  if codex login status >/dev/null 2>&1; then ok "Codex login is active"; else warn "Codex is not logged in; run: codex login"; fi
else
  err "codex CLI not found; install: npm install -g @openai/codex@0.144.1"
fi

printf '\n2. Configuration\n'
TOKEN=""
ROOT=""
PORT="8765"
if [[ -f .env ]]; then
  TOKEN="$(value MUSELAB_TOKEN)"
  ROOT="$(value MUSELAB_ROOT)"
  PORT="$(value MUSELAB_PORT)"; PORT="${PORT:-8765}"
  if [[ ${#TOKEN} -ge 16 ]]; then ok "MUSELAB_TOKEN is configured"; else err "MUSELAB_TOKEN is missing or too short"; fi
  if [[ -n "$ROOT" && -d "$ROOT" ]]; then ok "workspace: $ROOT"; else err "MUSELAB_ROOT is missing or unavailable"; fi
else
  err ".env not found; run scripts/install-linux.sh or scripts/install-macos.sh"
fi

printf '\n3. Dependencies\n'
if uv sync --frozen --no-progress >/dev/null 2>&1; then ok "uv.lock is reproducible"; else err "uv sync --frozen failed"; fi

printf '\n4. Service\n'
if command -v systemctl >/dev/null 2>&1; then
  if systemctl --user is-active --quiet muselab.service; then ok "systemd user service is active"; else warn "systemd user service is not active"; fi
elif [[ "$(uname -s)" == "Darwin" ]]; then
  if launchctl print "gui/$(id -u)/com.muselab" >/dev/null 2>&1; then ok "launchd agent is active"; else warn "launchd agent is not active"; fi
else
  warn "no supported service manager detected"
fi

printf '\n5. HTTP\n'
if curl -fsS --max-time 3 "http://127.0.0.1:${PORT}/api/health" | grep -q '"status":"ok"'; then
  ok "health endpoint responds"
elif [[ -n "$TOKEN" ]] && curl -fsS --max-time 3 -H "X-Auth-Token: $TOKEN" "http://127.0.0.1:${PORT}/api/meta" >/dev/null; then
  ok "authenticated API responds"
else
  warn "service is not responding on 127.0.0.1:${PORT}"
fi

printf '\nSummary: %s blocking issue(s), %s warning(s)\n' "$FAIL" "$WARN"
(( FAIL == 0 ))
