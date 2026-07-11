# muselab-codex documentation

> [简体中文](README_zh.md) · [← Back to project README](../README_en.md)

muselab-codex is a single-user, self-hosted workspace built around `codex app-server`. The documentation follows the path from installation to daily use, architecture, and operations. New users should begin with [Quick start](quickstart.md).

## Install and upgrade

- [Quick start](quickstart.md) — supported run modes, first login, health checks, and WSL2 notes
- [Linux installation](install-linux.md) — systemd user service, logs, linger, and uninstall
- [macOS installation](install-macos.md) — launchd agent, PATH, logs, and uninstall
- [Upgrade](upgrade.md) — update code and dependencies, check the Codex CLI baseline, verify, and roll back

## Use and configure

- [Configuration](configuration.md) — `.env`, `CODEX_HOME`, `AGENTS.md`, providers, and security boundaries
- [Skills](skills.md) — native discovery, enablement, scopes, and custom Skills
- [Scheduler](scheduler.md) — saved prompts, execution history, time zones, and unattended risk
- [Mobile PWA](mobile.md) — home-screen installation, HTTPS, notifications, and multi-device use
- [The nine Muses](muses.md) — product naming and conversation-entry concepts

## Architecture and implementation

- [Architecture](architecture.md) — ownership boundaries, request flow, event routing, persistence, and security
- [Infrastructure](infrastructure.md) — systemd, launchd, Docker, health checks, CI, and release gates
- [Native implementation specs](specs/) — protocol choices and verification records for each implementation stage
- [Tool catalog snapshot](tool-catalog.txt) — a development reference for observed tool-event shapes

## Operations

- [Troubleshooting](troubleshooting.md) — doctor, logs, models, browser cache, MCP, and file permissions
- [Data and backup](data-and-backup.md) — workspace, `.env`, `CODEX_HOME`, restore drills, and disposable state
- [Security policy](../SECURITY.md) — threat model, deployment baseline, and vulnerability reporting

## Project collaboration

- [Contributing](../CONTRIBUTING.md)
- [Development constraints](../AGENTS.md)
- [Third-party licenses](../THIRD_PARTY_LICENSES.md)

## Documentation conventions

- Current behavior is defined by code, tests, and `codex app-server` responses. Specs record implementation decisions; they do not override runtime facts.
- User-facing documents are maintained in both English and Chinese.
- Examples use neutral paths and placeholder data. Real tokens, API keys, `CODEX_HOME`, and workspace contents must never enter public artifacts.
