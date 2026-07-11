# Linux／WSL2 安装

> [English](install-linux.md) · [← 文档索引](README_zh.md)

Linux 安装器把 muselab-codex 注册为当前用户的 systemd 服务。不要用 root 或 `sudo bash scripts/install-linux.sh` 运行。

## 前置条件

- 普通用户 shell；
- 可用的 `systemctl --user`；
- `uv`、Node.js／npm、Git；
- 已完成 `codex login`；
- 当前用户可读写的工作区目录。

验证：

```bash
systemctl --user is-system-running
uv --version
npm --version
codex login status
```

安装器会在缺少 Codex CLI 时安装 `scripts/versions.env` 固定的版本，但不会替你完成交互式登录。

## 安装

```bash
git clone https://github.com/hesorchen/muselab-codex
cd muselab-codex
bash scripts/install-linux.sh
```

流程如下：

1. 拒绝 root，并检查 systemd、`uv` 和 npm；
2. 安装或验证固定版本 Codex CLI；
3. 检查 `codex login status`；
4. 执行 `uv sync --frozen`；
5. 首次运行时创建工作区和随机 token 的 `.env`；
6. 根据模板生成 `~/.config/systemd/user/muselab.service`；
7. `enable --now` 启动服务。

已有 `.env` 不会被覆盖。需要修改工作区或端口时，先手工编辑，再重启服务。

## 服务单元

服务执行：

```text
WorkingDirectory=<repository>
EnvironmentFile=<repository>/.env
ExecStart=<uv> run python -m backend.main
```

它使用 `Restart=on-failure`，并设置进程数、文件描述符和内存上限。日志进入用户 journal。

常用命令：

```bash
systemctl --user status muselab
systemctl --user restart muselab
systemctl --user stop muselab
journalctl --user -u muselab -n 100
journalctl --user -u muselab -f
```

若短时间连续失败达到 systemd 限制：

```bash
systemctl --user reset-failed muselab
systemctl --user start muselab
```

## 注销后保持运行

部分服务器在用户退出登录后会停止用户服务。检查：

```bash
loginctl show-user "$USER" -p Linger
```

需要时启用：

```bash
sudo loginctl enable-linger "$USER"
```

这一步需要系统管理员权限，但安装器和应用本身仍应以普通用户运行。

## WSL2

若 `systemctl --user` 不可用，在 WSL 内创建或修改 `/etc/wsl.conf`：

```ini
[boot]
systemd=true
```

然后从 Windows PowerShell 执行：

```powershell
wsl --shutdown
```

重新打开 WSL，确认 systemd 用户实例正常后再安装。

## 安装后检查

```bash
bash scripts/doctor.sh
curl http://127.0.0.1:8765/api/health
```

再打开浏览器，输入 `.env` 里的 token，创建 thread 并执行一次文件读取。不要把 token 粘贴到 issue 或日志中。

## 更新与迁移

代码或配置更新后：

```bash
bash scripts/upgrade.sh
systemctl --user restart muselab
bash scripts/doctor.sh
```

从旧 muselab 只迁移最小部署设置和已验证 Provider key 时：

```bash
bash scripts/migrate-native-provider-keys.sh /path/to/old/.env
```

脚本不显示密钥，也不会迁移旧模型路由。

## 卸载

```bash
bash scripts/uninstall-linux.sh
```

卸载脚本移除用户服务，但应保留工作区、`.env` 和 Codex 数据。执行前仍建议按[数据与备份](data-and-backup_zh.md)完成备份。

## 远程访问

服务默认只监听回环地址。VPS 上可运行 `scripts/setup-https.sh` 配置 Caddy；仍应增加额外访问控制，并确保上游 8765 端口不对公网开放。详见[安全策略](../SECURITY.md)。
