# 数据与备份

> [English](data-and-backup.md) · [← 文档索引](README_zh.md)

muselab-codex 不使用应用数据库。当前原生运行时的状态分布在三处：

1. `MUSELAB_ROOT` 指向的 workspace；
2. service `.env` 等部署配置；
3. `$CODEX_HOME`（通常是 `~/.codex`）下的 Codex CLI 状态。

## 需要备份的内容

| 路径 | 内容 | 为何重要 |
|---|---|---|
| `$MUSELAB_ROOT/` | 你的文件和 muselab-codex workspace 状态 | 这是用户拥有的工作目录 |
| `$MUSELAB_ROOT/.muselab-codex/attachments/threads/` | 已写入 Codex thread 的附件文件 | 恢复附件预览和 transcript 中本地路径所必需 |
| `$MUSELAB_ROOT/.muselab-codex/usage/` | 每个 thread 的脱敏数字 token-usage 快照 | 后端重启后继续显示 context meter 所必需 |
| `$MUSELAB_ROOT/.muselab-codex/scheduler.json` | 计划任务、运行历史和未读数 | 恢复自动任务所必需 |
| `$MUSELAB_ROOT/.muselab/` | VAPID 私钥和设备推送订阅 | 保持现有设备通知有效 |
| `$MUSELAB_ROOT/.muselab-dustbin/` | 文件回收站内容与 manifest | 恢复尚未永久删除的文件 |
| 当前 service 使用的 `.env` | `MUSELAB_ROOT`、token、端口和其他部署设置 | 含密钥；只能私密保存，不能提交 |
| `$CODEX_HOME/` | Codex 配置、登录状态和 Codex 管理的 thread／rollout 数据 | Codex 是 transcript 的真相源 |

`$CODEX_HOME` 可能含登录凭证，只能备份到私密、加密的位置。如果你有意不备份登录状态，恢复后重新登录 Codex 即可。

应用不使用独立会话数据库或 Provider 路由文件。thread 和配置都以 Codex 为唯一真相源。

## 可以丢弃的内容

| 路径 | 说明 |
|---|---|
| `$MUSELAB_ROOT/.muselab-codex/attachments/staged/` | 尚未发送的上传文件；服务停止时可以安全删除 |
| `<repo>/.venv/`、缓存和日志 | 由 `uv sync` 或运行时重新生成 |
| 临时 app-server schema 目录 | 需要时根据固定版本的 Codex CLI 重新生成 |

## 恢复步骤

1. 安装相同的受支持 Codex CLI 和 muselab-codex 版本。
2. 停止服务。
3. 恢复 `$MUSELAB_ROOT`、当前 service 使用的 `.env` 和 `$CODEX_HOME`。
4. 检查 `MUSELAB_ROOT`、`HOME` 与可选的 `CODEX_HOME` 是否指向恢复后的目录。
5. 确认 `codex login status`，然后启动 muselab-codex。
6. 打开一个近期 thread，分别验证 transcript 和一个附件。
7. 检查计划任务与推送订阅；发送测试通知验证 VAPID 恢复。

恢复实例时，请使用对应的服务管理与升级说明。
