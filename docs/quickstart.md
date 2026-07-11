# Quick start

> [简体中文](quickstart_zh.md) · [← Documentation index](README.md)

This guide takes a new machine through one native Codex file task. Use the platform installer for a long-running service; use manual startup for development.

## Prerequisites

| Component | Requirement | Purpose |
|---|---|---|
| OS | Linux, macOS, or WSL2 | User service and local file permissions |
| Python | 3.12+ | FastAPI backend |
| `uv` | available on PATH | Python environment |
| Node.js/npm | available on PATH | Codex CLI installation |
| Codex CLI | tested baseline `0.144.1` | `codex app-server` runtime |
| Git | available on PATH | Clone and upgrade |

Authenticate Codex on the host first:

```bash
npm install -g @openai/codex@0.144.1
codex login
codex login status
```

Codex owns the login state in `CODEX_HOME`; muselab-codex does not manage OAuth files.

## One-line install

```bash
curl -fsSL https://raw.githubusercontent.com/hesorchen/muselab-codex/main/scripts/quick-install.sh | bash
```

The bootstrap detects the platform, checks Git/curl/systemd, installs `uv` when missing, clones or reuses a checkout, and hands off to the platform installer. It refuses root execution.

## Install from a checkout

```bash
git clone https://github.com/hesorchen/muselab-codex
cd muselab-codex
codex login
bash scripts/install-linux.sh        # use scripts/install-macos.sh on macOS
```

The first run asks for a workspace and local port, generates a random token, creates a private `.env`, and binds to `127.0.0.1`.

## Development mode

```bash
git clone https://github.com/hesorchen/muselab-codex
cd muselab-codex
uv sync
cp .env.example .env
```

Set at least:

```dotenv
MUSELAB_TOKEN=replace-with-at-least-16-random-characters
MUSELAB_ROOT=/absolute/path/to/a-workspace-you-own
MUSELAB_PORT=8765
MUSELAB_HOST=127.0.0.1
```

The workspace must already exist and cannot be a system or cross-user root. Start with:

```bash
uv run python -m backend.main
```

## First use

1. Open `http://127.0.0.1:8765`.
2. Enter `MUSELAB_TOKEN`.
3. Create a thread.
4. Ask Codex to list the workspace's top-level files.
5. Decide any approval request according to the actual operation.
6. Ask Codex to create a neutral test file and confirm that write and preview work.

Initialize optional workspace instructions and folders with:

```bash
bash scripts/intake.sh
```

## Health check

```bash
curl http://127.0.0.1:8765/api/health
```

`status: "ok"` and `runtime.ready: true` mean both FastAPI and app-server are ready. If the state remains `starting`, run `bash scripts/doctor.sh`.

## Optional native providers

Add the required key to the private service environment, restart, and enable the provider under **Settings → Models**:

```dotenv
MINIMAX_API_KEY=...
DASHSCOPE_API_KEY=...
XIAOMI_MIMO_API_KEY=...
```

The normal authenticated Codex models do not require browser-side key setup.

## WSL2

The Linux installer requires a working systemd user instance. Enable systemd in `/etc/wsl.conf`, run `wsl --shutdown` from Windows, reopen WSL, and install again.

Next, read [Configuration](configuration.md), the platform install guide, and [Troubleshooting](troubleshooting.md).
