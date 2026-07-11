# macOS installation

> [简体中文](install-macos_zh.md) · [← Documentation index](README.md)

The macOS installer creates the current user's launchd agent `com.muselab`. It does not require administrator privileges and does not copy Codex login state into the repository.

## Prerequisites

- `uv`, Node.js/npm, and Git;
- a successful `codex login`;
- a workspace writable by the current user.

The installer records the actual `uv` and `codex` directories in the plist PATH, covering common Apple Silicon and Intel Homebrew locations.

## Install

```bash
git clone https://github.com/hesorchen/muselab-codex
cd muselab-codex
codex login
bash scripts/install-macos.sh
```

It validates prerequisites, installs the pinned Codex CLI when missing, runs `uv sync --frozen`, creates a private `.env` on first use, writes `~/Library/LaunchAgents/com.muselab.plist`, and bootstraps it.

## Operate the agent

```bash
launchctl print gui/$(id -u)/com.muselab
launchctl kickstart -k gui/$(id -u)/com.muselab
launchctl bootout gui/$(id -u)/com.muselab
tail -f ~/Library/Logs/muselab/stderr.log
```

If launchd cannot find Codex, inspect `EnvironmentVariables/PATH` in the plist and bootstrap it again after correcting the path.

## Verify, upgrade, and uninstall

```bash
bash scripts/doctor.sh
curl http://127.0.0.1:8765/api/health

bash scripts/upgrade.sh
launchctl kickstart -k gui/$(id -u)/com.muselab
```

Use `bash scripts/uninstall-macos.sh` to remove the agent. Back up the workspace, `.env`, and `CODEX_HOME` before uninstalling or migrating.

Keep the default loopback binding. For mobile or remote access, use a controlled HTTPS tunnel or reverse proxy rather than exposing the upstream port directly.
