# 快速入门

> [English](quickstart.md) · [← 文档索引](README_zh.md)

这份指南带你从空环境到完成一次 Codex 原生文件任务。正式长期运行推荐使用安装脚本；只想开发调试时再手动启动。

## 前置条件

| 组件 | 要求 | 用途 |
|---|---|---|
| 操作系统 | Linux、macOS；Windows 使用 WSL2 | 用户级服务与本地文件权限 |
| Python | 3.12+ | FastAPI 后端 |
| `uv` | 可执行 | Python 依赖和虚拟环境 |
| Node.js／npm | 可执行 | 安装 Codex CLI |
| Codex CLI | 测试基线 `0.144.1` | `codex app-server` runtime |
| Git | 可执行 | 克隆和升级仓库 |

先在宿主机完成 Codex 登录：

```bash
npm install -g @openai/codex@0.144.1
codex login
codex login status
```

登录态保存在 Codex 自己的 `CODEX_HOME` 中。muselab-codex 不接管 OAuth 文件。

## 方式一：一行安装

```bash
curl -fsSL https://raw.githubusercontent.com/hesorchen/muselab-codex/main/scripts/quick-install.sh | bash
```

引导脚本会：

1. 检测 Linux／macOS／WSL2；
2. 检查 Git、curl 和 systemd 条件；
3. 在缺少时安装 `uv`；
4. 克隆到 `~/muselab-codex`，或复用已有 checkout；
5. 转交平台安装脚本创建 `.env` 和用户服务。

它拒绝 root 运行。muselab-codex 应由普通用户启动，Codex 和 MCP 子进程也继承这个用户的权限。

## 方式二：从 checkout 安装

```bash
git clone https://github.com/hesorchen/muselab-codex
cd muselab-codex
codex login
bash scripts/install-linux.sh        # macOS：scripts/install-macos.sh
```

首次安装会询问：

- 工作区路径，默认 `~/muselab-workspace`；
- 本地端口，默认 `8765`。

安装器自动生成随机 token，并以 `MUSELAB_HOST=127.0.0.1` 创建权限为当前用户私有的 `.env`。

## 方式三：开发模式

```bash
git clone https://github.com/hesorchen/muselab-codex
cd muselab-codex
uv sync
cp .env.example .env
```

编辑 `.env`：

```dotenv
MUSELAB_TOKEN=replace-with-at-least-16-random-characters
MUSELAB_ROOT=/absolute/path/to/a-workspace-you-own
MUSELAB_PORT=8765
MUSELAB_HOST=127.0.0.1
```

工作区必须已经存在，不能指向 `/`、`/home`、`/etc` 等系统或跨用户根目录。

启动：

```bash
uv run python -m backend.main
```

开发时如需自动重载，可以使用仓库 `Makefile` 中的开发目标；正式服务不要开启 reload。

## 首次打开

1. 打开 `http://127.0.0.1:8765`；
2. 输入 `.env` 中的 `MUSELAB_TOKEN`；
3. 创建一个新会话；
4. 发送“列出这个工作区的顶层文件”；
5. 审批界面出现时，根据实际操作决定允许或拒绝；
6. 再让 Codex 创建一个中性的测试文件，确认写入和预览都正常。

如果工作区还没有 `AGENTS.md`，可以运行：

```bash
bash scripts/intake.sh
```

脚本会从 Codex 原生模板创建 `AGENTS.md` 和默认目录；已有文件在覆盖前会备份并要求确认。

## 健康检查

```bash
curl http://127.0.0.1:8765/api/health
```

正常响应示例：

```json
{
  "status": "ok",
  "version": "0.1.0a1",
  "runtime": {
    "state": "ready",
    "ready": true,
    "restart_count": 0
  }
}
```

`status: "starting"` 表示 Web 服务已启动，但 app-server 尚未完成初始化。若持续不变，运行：

```bash
bash scripts/doctor.sh
```

## 可选：启用国产模型

把对应密钥加入服务继承的私有环境：

```dotenv
MINIMAX_API_KEY=...
DASHSCOPE_API_KEY=...
XIAOMI_MIMO_API_KEY=...
```

重启服务，然后在“设置 → 模型”中打开所需 Provider。默认 Codex 登录模型无需在网页配置密钥。

## Windows／WSL2

Windows 用户在 WSL2 的 Linux 环境中安装。`install-linux.sh` 需要可访问的 systemd 用户实例；若未启用，在 `/etc/wsl.conf` 设置：

```ini
[boot]
systemd=true
```

然后在 Windows PowerShell 执行 `wsl --shutdown`，重新进入 WSL 再安装。

## 下一步

- 理解工作区、`CODEX_HOME` 和 Provider：[配置参考](configuration_zh.md)
- 日常服务管理：[Linux](install-linux_zh.md)／[macOS](install-macos_zh.md)
- 手机访问与通知：[移动端 PWA](mobile_zh.md)
- 故障定位：[排错](troubleshooting_zh.md)
