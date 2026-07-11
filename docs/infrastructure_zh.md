# 基础设施

> [English](infrastructure.md) · [← 文档索引](README_zh.md)

本页面向维护者和部署者，说明安装脚本、用户服务、Docker、健康检查和 CI 如何围绕同一条 Codex Native 进程链路工作。

```text
service manager
  └── uv run python -m backend.main
        ├── FastAPI／Uvicorn
        └── codex app-server --listen unix://PATH
              └── Codex／MCP／工具子进程
```

## 版本基线

`scripts/versions.env` 是外部 Codex CLI 测试版本的源码真相源。Linux／macOS 安装器读取它；Dockerfile 的 build arg 和 CI 会检查是否一致。

提升版本时不能只修改 npm 包号，还要：

1. 生成匹配的 app-server schema；
2. 更新协议 fixture；
3. 运行离线单元测试；
4. 在临时工作区执行显式 live check；
5. 更新架构和升级文档。

## 脚本目录

| 脚本 | 职责 |
|---|---|
| `quick-install.sh` | 检测平台、安装 `uv`、克隆仓库并转交平台安装器 |
| `install-linux.sh` | 安装依赖、创建 `.env`、注册 systemd 用户服务 |
| `install-macos.sh` | 安装依赖、创建 `.env`、注册 launchd agent |
| `doctor.sh` | 检查 runtime、登录、配置、依赖、服务和 HTTP 状态 |
| `upgrade.sh` | 同步 lock、运行门禁并提示重启命令 |
| `intake.sh` | 在工作区创建 Codex 原生 `AGENTS.md` 与目录骨架 |
| `migrate-native-provider-keys.sh` | 从旧私有 `.env` 迁移最小部署项和三家 Provider key |
| `setup-https.sh` | Linux 上配置 Caddy、TLS、SSE 刷新和基础安全 header |
| `uninstall-*.sh` | 移除用户服务，保留用户数据 |
| `lint.sh` | 文件编码、CSS 冲突、PII 与维护者身份泄漏检查 |

## Linux：systemd 用户服务

模板为 `scripts/templates/muselab.service.tmpl`。关键设置：

- 从仓库 `.env` 读取环境；
- `Restart=on-failure`，10 秒后重启；
- 5 分钟内最多连续重启 5 次；
- `NoNewPrivileges=true`；
- `LimitNOFILE=8192`、`TasksMax=4096`；
- `MemoryHigh=2G`、`MemoryMax=4G`；
- stdout／stderr 写入用户 journal。

服务需要访问工作区和 `CODEX_HOME`，因此不会启用会阻断 home 的 `ProtectHome`。这也是为什么必须使用专用低权限用户和明确的 `MUSELAB_ROOT`。

## macOS：launchd

模板为 `scripts/templates/com.muselab.plist.tmpl`。安装器写入仓库路径、`uv` 路径、home 和包含 Codex CLI 的 PATH。日志进入 `~/Library/Logs/muselab/`。

launchd 的环境比交互式 shell 更小；若手工移动 `uv` 或 Codex CLI，需要重新运行安装器或更新 plist。

## Docker

镜像分两阶段构建：

1. builder 使用锁文件创建生产 `.venv`；
2. runtime 安装固定 Codex CLI，复制后端和前端，并切换到 uid／gid 1000 的 `muse` 用户。

Compose 默认：

| 项目 | 默认值 |
|---|---|
| 端口 | `127.0.0.1:8765:8765` |
| 工作区 | `${ARCHIVE_DIR:-./data}:/data` |
| Codex 状态 | `${CODEX_HOME:-${HOME}/.codex}:/home/muse/.codex` |
| 重启策略 | `unless-stopped` |
| 内存 | 预留 1 GiB，上限 4 GiB |
| PID 上限 | 4096 |

容器中的 `CODEX_HOME` 挂载必须可写，以便登录刷新、配置更新和 thread 持久化。不要在 Dockerfile 中执行登录，也不要把凭证 COPY 进镜像层。

## 健康与版本

`GET /api/health` 不需要 token，供 systemd 外部监控、Docker HEALTHCHECK、Caddy 和容器平台使用。它只返回：

- 应用版本；
- runtime state／ready；
- app-server 重启次数。

`GET /api/meta` 需要 token，可返回资源版本、工作区路径和诊断版本。前端用资源版本检测旧标签页，避免升级后继续运行缓存的 JS。

健康检查是 readiness，不执行文件写入或模型请求，以免短暂外部故障触发服务重启风暴。

## 静态资源与 SSE

前端无构建步骤，HTML、CSS、JavaScript 和 vendored 库直接由 FastAPI 提供。资源 URL 带基于 mtime 的版本戳；较大响应启用 gzip。

SSE 明确保持 identity encoding，避免压缩中间件和反向代理缓冲 token。代理层必须允许长连接并即时 flush。

## CI 与测试

本地和 CI 的阻塞门禁：

```bash
uv run pytest tests/
uv run ruff check backend/ tests/
bash scripts/lint.sh
node --check frontend/app.js
```

协议测试默认启动 `tests/fixtures/fake_codex_app_server.py`，不读取真实登录态和私人工作区。Playwright E2E 由显式环境开关控制；真实 Codex live check 不属于普通单元测试。

安装测试会核对 CLI pin、构建 Docker 镜像并验证容器内 schema 命令。任何故障产物都必须排除 `.env`、`CODEX_HOME`、transcript 和工作区内容。

## 发布与回滚

升级应保持 Git revision、`uv.lock`、Codex CLI baseline 和文档一致。发生问题时优先回到已知 Git revision，执行 `uv sync --frozen`，再重启服务；不要用 hard reset 覆盖仍需保留的本地配置或工作区。

用户数据不在镜像层和 Python 虚拟环境中。只要工作区、`.env` 和 `CODEX_HOME` 已备份，应用 checkout 可以重建。
