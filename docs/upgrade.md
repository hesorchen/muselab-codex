# Upgrade

> [简体中文](upgrade_zh.md) · [← Documentation index](README.md)

There are two upgrade classes: routine application updates and Codex CLI protocol-baseline changes. The latter requires protocol validation, not just an npm update.

## Before upgrading

Back up `MUSELAB_ROOT`, repository `.env`, and `CODEX_HOME`. Record the current Git revision and `codex --version`, and preserve any local code changes.

## Routine update

```bash
git pull --ff-only
bash scripts/upgrade.sh
```

The script checks Codex CLI, updates/synchronizes the lock, runs pytest, Ruff, project lint, and frontend syntax checks, then prints platform restart commands. It does not restart the service or alter user data.

After restart, run doctor and verify health, a new thread, history, and one file-tool call.

## Codex CLI baseline

A change to `scripts/versions.env` also requires matching generated schemas, a recorded digest, updated protocol fixtures, an ephemeral live test, synchronized Docker/install pins, and architecture documentation. Experimental APIs require a concrete need, protocol coverage, and fallback.

## Rollback

Switch to a known-good revision, run `uv sync --frozen`, restore the matching Codex CLI when necessary, and restart. Avoid destructive Git commands that discard local work. Codex history and workspace data are independent of the Python virtual environment.

For Docker, rebuild and recreate the container while preserving the workspace and `CODEX_HOME` mounts.

See [Data and backup](data-and-backup.md) for the restore boundary.
