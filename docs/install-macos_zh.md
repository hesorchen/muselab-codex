# macOS 安装

> [English](install-macos.md) · [← 文档索引](README_zh.md)

macOS 安装器创建当前用户的 launchd agent `com.muselab`。它不需要管理员权限，也不会把 Codex 登录态复制进仓库。

## 前置条件

- macOS 用户 shell；
- `uv`、Node.js／npm、Git；
- 已完成 `codex login`；
- 当前用户可读写的工作区。

Apple Silicon 常用 Homebrew 路径是 `/opt/homebrew/bin`，Intel 通常是 `/usr/local/bin`。安装器会把当前 `uv` 和 `codex` 所在目录写入 plist 的 PATH。

## 安装

```bash
git clone https://github.com/hesorchen/muselab-codex
cd muselab-codex
codex login
bash scripts/install-macos.sh
```

安装器会：

1. 验证 macOS、`uv` 和 npm；
2. 安装或检查固定版本 Codex CLI；
3. 验证登录状态；
4. 执行 `uv sync --frozen`；
5. 首次运行时创建工作区和私有 `.env`；
6. 生成 `~/Library/LaunchAgents/com.muselab.plist`；
7. 使用 `launchctl bootstrap` 启动 agent。

## 服务管理

```bash
launchctl print gui/$(id -u)/com.muselab
launchctl kickstart -k gui/$(id -u)/com.muselab
launchctl bootout gui/$(id -u)/com.muselab
```

日志位置：

```text
~/Library/Logs/muselab/stdout.log
~/Library/Logs/muselab/stderr.log
```

查看：

```bash
tail -f ~/Library/Logs/muselab/stderr.log
```

如果 agent 能启动但找不到 Codex，检查 plist 的 `EnvironmentVariables/PATH` 是否包含实际的 `uv` 和 `codex` 目录，修改后重新 bootstrap。

## 安装后验证

```bash
bash scripts/doctor.sh
curl http://127.0.0.1:8765/api/health
```

健康检查 ready 后，打开浏览器并完成一次文件读取与预览。

## 更新

```bash
bash scripts/upgrade.sh
launchctl kickstart -k gui/$(id -u)/com.muselab
bash scripts/doctor.sh
```

## 卸载

```bash
bash scripts/uninstall-macos.sh
```

卸载前备份工作区、`.env` 和 `CODEX_HOME`。launchd agent 与应用数据是分开的；移除 agent 不应等同于删除用户数据。

## 局域网与远程访问

默认 `127.0.0.1` 只能从本机访问。移动端推荐使用带 HTTPS 的受控隧道或反向代理；不要为了图方便直接改成公网可访问的 `0.0.0.0`。参见[移动端](mobile_zh.md)和[安全策略](../SECURITY.md)。
