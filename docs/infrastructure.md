# Infrastructure

> [简体中文](infrastructure_zh.md) · [← Documentation index](README.md)

This page describes how installers, user services, Docker, health checks, and CI support one Codex-native process tree.

```text
service manager
  └── uv run python -m backend.main
        ├── FastAPI/Uvicorn
        └── codex app-server --listen unix://PATH
              └── Codex, MCP, and tool subprocesses
```

## Version baseline

`scripts/versions.env` is the source of truth for the tested external Codex CLI. Platform installers consume it, while Docker and CI mirror and verify the same value. A baseline upgrade also requires matching schemas, protocol fixtures, offline tests, an ephemeral live check, and documentation updates.

## Script map

| Script | Responsibility |
|---|---|
| `quick-install.sh` | Detect platform, install `uv`, clone, and hand off |
| `install-linux.sh` | Create `.env` and a systemd user service |
| `install-macos.sh` | Create `.env` and a launchd agent |
| `doctor.sh` | Check runtime, login, configuration, dependencies, service, and HTTP |
| `upgrade.sh` | Synchronize the lock, run gates, and print restart commands |
| `intake.sh` | Create native `AGENTS.md` and a workspace skeleton |
| `migrate-native-provider-keys.sh` | Move minimum deployment fields and verified provider keys |
| `setup-https.sh` | Configure Caddy, TLS, SSE flushing, and base headers on Linux |
| `uninstall-*.sh` | Remove the user service while preserving data |

## User services

The systemd unit reads the repository `.env`, restarts on failure, applies bounded restart frequency and resource limits, enables `NoNewPrivileges`, and writes to the user journal. It must still access the workspace and `CODEX_HOME`, so operators should use an unprivileged account and a narrow `MUSELAB_ROOT`.

The launchd plist records the repository, `uv`, home, and Codex PATH. Logs live below `~/Library/Logs/muselab/`. Re-run the installer after moving tool binaries.

## Docker

The multi-stage image creates a locked production environment, installs the pinned Codex CLI, and runs as the unprivileged `muse` user. Compose binds to loopback, mounts `/data` for the workspace and `/home/muse/.codex` for Codex state, restarts unless stopped, and applies memory/PID limits.

`CODEX_HOME` must be private and writable. Never log in during image build or copy credentials into an image layer.

## Health, assets, and streaming

`/api/health` is unauthenticated and exposes only application version, runtime readiness, and restart count. `/api/meta` is authenticated and includes detailed diagnostics plus the static asset version.

Static files are build-free and carry mtime-based version stamps. Large responses use gzip; SSE keeps identity encoding so proxies can flush each event immediately.

## CI and tests

```bash
uv run pytest tests/
uv run ruff check backend/ tests/
bash scripts/lint.sh
node --check frontend/app.js
```

Protocol tests use an offline fake app-server and throwaway workspaces. Playwright and real Codex checks are explicit. CI artifacts must exclude `.env`, `CODEX_HOME`, transcripts, and workspace content.

## Release and rollback

Keep the Git revision, `uv.lock`, Codex CLI baseline, and documentation aligned. Roll back to a known revision, run `uv sync --frozen`, and restart the service. User data remains reconstructible when the workspace, `.env`, and `CODEX_HOME` are backed up.
