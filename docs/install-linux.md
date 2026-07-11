# Linux / WSL2 installation

> [简体中文](install-linux_zh.md) · [← Documentation index](README.md)

The Linux installer registers muselab-codex as a systemd user service. Run it as a normal user, never through `sudo`.

## Prerequisites

- a working `systemctl --user` instance;
- `uv`, Node.js/npm, and Git;
- a successful `codex login`;
- a workspace writable by the current user.

```bash
systemctl --user is-system-running
uv --version
npm --version
codex login status
```

## Install

```bash
git clone https://github.com/hesorchen/muselab-codex
cd muselab-codex
bash scripts/install-linux.sh
```

The installer validates prerequisites, installs the pinned Codex CLI when missing, runs `uv sync --frozen`, creates `.env` on first use, renders `~/.config/systemd/user/muselab.service`, and enables it immediately. Existing `.env` files are preserved.

## Operate the service

```bash
systemctl --user status muselab
systemctl --user restart muselab
systemctl --user stop muselab
journalctl --user -u muselab -n 100
journalctl --user -u muselab -f
```

The unit uses `Restart=on-failure` with bounded restart frequency and resource limits. If systemd reaches the failure limit, run `systemctl --user reset-failed muselab` after fixing the cause.

For servers that must survive logout, check `loginctl show-user "$USER" -p Linger` and ask an administrator to run `loginctl enable-linger` when required.

## WSL2

Enable systemd in `/etc/wsl.conf`, run `wsl --shutdown` from Windows, reopen WSL, and confirm `systemctl --user` works before installing.

## Verify

```bash
bash scripts/doctor.sh
curl http://127.0.0.1:8765/api/health
```

Then authenticate in the browser and execute one file-read task.

## Upgrade and uninstall

```bash
bash scripts/upgrade.sh
systemctl --user restart muselab
bash scripts/doctor.sh
```

Use `bash scripts/uninstall-linux.sh` to remove the service. Back up the workspace, `.env`, and `CODEX_HOME` first. For remote access, keep the upstream on loopback and place HTTPS plus additional access control in front.
