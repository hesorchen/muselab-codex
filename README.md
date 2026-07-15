<h1 align="center">muselab-codex</h1>

<p align="center">
  <a href="https://github.com/hesorchen/muselab-codex/actions/workflows/ci.yml"><img src="https://github.com/hesorchen/muselab-codex/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-blue.svg" alt="License: MIT"></a>
  <a href="docs/quickstart_zh.md"><img src="https://img.shields.io/badge/deploy-self--hosted-orange.svg" alt="Self-hosted"></a>
  <a href="README_en.md"><img src="https://img.shields.io/badge/lang-English-red" alt="English"></a>
</p>

<p align="center"><strong>基于 <code>codex app-server</code> 的自托管 AI 工作台</strong></p>

<p align="center"><em>让 Codex 在浏览器里持续理解一个真实的本地工作区。</em></p>

<table align="center">
<tr>
<td align="center"><img src="promo/media/screenshot-mobile-files.jpeg" width="100" alt="移动端文件区"></td>
<td align="center"><img src="promo/media/screenshot-mobile-preview.png" width="100" alt="移动端预览区"></td>
<td align="center"><img src="promo/media/screenshot-mobile-chat.png" width="100" alt="移动端对话区"></td>
<td align="center"><img src="promo/media/screenshot-desktop.png" width="360" alt="桌面端工作台"></td>
</tr>
<tr>
<td align="center">移动端 · 文件</td>
<td align="center">移动端 · 预览</td>
<td align="center">移动端 · 对话</td>
<td align="center">桌面端 · 三栏工作台</td>
</tr>
</table>

<p align="center"><sub>点击图片可查看原图；muselab-codex 与 muselab 保持一致的三栏工作台体验。</sub></p>

muselab-codex 把本机已登录的 Codex 变成一个适合长期使用的文件与对话工作台：资料留在本地，Codex 持续理解真实工作区，浏览器提供文件管理、内容预览、多会话、流式交互和移动端访问。

```text
Browser → FastAPI HTTP／SSE → codex app-server Unix WebSocket → Codex
```

项目只有一套 agent runtime。muselab-codex 不维护第二套模型循环，也不把 Codex 降级为普通聊天接口。

muselab-codex 会监督一个本地 Unix socket listener。需要从终端进入同一套实时 thread
状态时，在「设置 → 关于」复制 `codex resume --remote unix:///.../app-server.sock`；
普通 `codex` 命令仍会创建独立 runtime。

## 核心特性

| 能力 | 说明 |
|---|---|
| **Codex 原生 Agent Harness** | thread、turn、工具、审批、sandbox、Skills、MCP 和账户限额均由 `codex app-server` 管理 |
| **可持续的本地上下文** | `MUSELAB_ROOT` 是默认工作目录，并可登记更多本地目录；`AGENTS.md`、Memory 和工作区文件共同形成可检查、可维护的上下文 |
| **完整文件工作台** | 文件树、全文搜索、上传、编辑、回收站，以及 Markdown、代码、图片、PDF、CSV、XLSX、HTML 预览 |
| **多会话工作流** | 流式回复、历史回放、消息队列、fork、compact、子 agent 会话、原生 Fast 模式，以及按工作目录隔离的会话标签 |
| **原生扩展能力** | 浏览器直接展示和管理 Codex Skills、MCP server、OAuth 状态、审批和结构化用户提问 |
| **计划任务与终端** | 保存 prompt 定时执行；后台终端进程可查询、输入和终止 |
| **自托管与移动端** | 默认仅监听本机，支持 systemd、launchd、Docker、PWA、HTTPS 反向代理和 Web Push |
| **国产 Responses Provider** | 已验证 MiniMax M2.7、Qwen 3.7 Plus、MiMo V2.5 Pro，通过 Codex 原生 `model_providers` 接入 |

## 快速开始

### 一行安装

支持 Linux、macOS 和启用了 systemd 的 WSL2：

```bash
curl -fsSL https://raw.githubusercontent.com/hesorchen/muselab-codex/main/scripts/quick-install.sh | bash
```

安装器会克隆仓库、检查 `uv`／Node.js／Codex CLI、验证 `codex login`、创建私有 `.env`，并注册用户级服务。

### 手动安装

```bash
git clone https://github.com/hesorchen/muselab-codex
cd muselab-codex
codex login
bash scripts/install-linux.sh        # macOS 使用 install-macos.sh
```

### 安装后验证

1. 浏览器打开 `http://127.0.0.1:8765`；
2. 输入安装器生成的 `MUSELAB_TOKEN`；
3. 新建会话并发送“你好”；
4. 让 Codex 读取或生成一个工作区文件，确认工具链路正常。

出问题？运行 `bash scripts/doctor.sh` 逐层诊断，或查看[排错文档](docs/troubleshooting_zh.md)。健康接口中 `runtime.ready: true` 表示 FastAPI 与 Codex app-server 均已就绪。

> **Windows 用户：** 请通过启用了 systemd 的 WSL2 安装，详见[快速入门](docs/quickstart_zh.md#windowswsl2)。

## 会话实践

> “扫描这个目录，说明 Markdown、PDF 和表格之间的关系，再把结论整理成一份新的 Markdown 概览。”

Muse 会在同一个 Codex thread 中读取真实文件、使用终端工具、按需请求审批，并把结果写回工作区。你可以继续让它生成单文件 HTML 报告，在中间预览区直接查看；长对话接近上下文上限时，可使用 Codex 原生 compact 压缩当前 thread 的上下文后继续工作。

这里没有预先切块或应用自建的 RAG 索引。所有文件变更都可见、可编辑、可备份。

## 为什么是 Codex-native？

| 方案 | 常见局限 | muselab-codex 的选择 |
|---|---|---|
| 普通网页聊天 | 文件临时上传，工具与上下文由应用重新实现 | 直接使用本地工作区和 Codex 原生工具循环 |
| 独立终端会话 | 适合命令行，但缺少文件预览、移动端和多标签工作区 | 浏览器与终端可接入同一个 app-server runtime |
| 应用自建 Agent 层 | thread、审批、Skills、MCP 容易与上游语义分叉 | 由 Codex 管理权威状态，应用只做界面与协议适配 |

## 实用细节

- **三栏工作台** —— 文件树、内容预览和对话区协同工作，Markdown、代码、图片、PDF、CSV、XLSX、HTML 可直接预览。
- **多工作目录** —— 可登记并切换多个本地目录；文件树、预览、会话标签和新会话 cwd 会作为一个整体切换，每个目录保留自己的展开层级与预览标签。
- **原生会话参数** —— Effort、思考摘要和 Fast 速度档彼此独立；Fast 只对 `model/list` 声明支持的模型展示，并按会话保留。
- **中英双语与多主题** —— 页面语言即时切换，适配亮色、暗色、护眼主题和移动端 PWA。
- **同一原生 runtime** —— 在“设置 → 关于”复制 remote 命令，即可从 Codex CLI 进入同一套实时 thread 状态。
- **可观测运行状态** —— 健康检查、账户用量、上下文用量、工具过程、审批与 MCP 提问均直接呈现在浏览器中。

## Codex 原生架构

| muselab-codex 负责 | Codex app-server 负责 |
|---|---|
| 浏览器 UI、PWA、令牌鉴权 | thread、turn 与 transcript |
| HTTP／SSE 适配与进程监督 | 模型调用、流式事件与工具循环 |
| 安全的工作区文件 API | sandbox、审批与用户提问 |
| 附件落盘和数值型用量 sidecar | Skills、MCP、Memory 与配置优先级 |
| systemd／launchd／Docker 集成 | 登录态、账户限额和原生历史 |

这个边界是项目的维护原则：只要 Codex 已经定义了权威语义，muselab-codex 就适配它，而不在应用层复制一套实现。

## 国产模型

当前只内置经过真实 Responses API 与工具调用验证的 Provider：

| Provider | 模型 | 环境变量 | Web Search |
|---|---|---|---|
| MiniMax | `minimax-m2.7` | `MINIMAX_API_KEY` | 为兼容性关闭 |
| Qwen | `qwen3.7-plus` | `DASHSCOPE_API_KEY` | 为兼容性关闭 |
| Xiaomi MiMo | `mimo-v2.5-pro` | `XIAOMI_MIMO_API_KEY` | 为兼容性关闭 |

把密钥放入服务进程可继承的私有环境，重启服务，然后在“设置 → 模型”中启用。网页不会读取、显示或提交密钥；开关只通过 app-server 写入 Codex `model_providers` 配置。详见[配置参考](docs/configuration_zh.md)。

## 本地开发

要求 Python 3.12+、[`uv`](https://docs.astral.sh/uv/)、Node.js 和已登录的 Codex CLI。当前协议测试基线为 `codex-cli 0.144.1`。

```bash
git clone https://github.com/hesorchen/muselab-codex
cd muselab-codex
uv sync
cp .env.example .env
# 编辑 .env：至少设置 MUSELAB_TOKEN 和 MUSELAB_ROOT
uv run python -m backend.main
```

质量门禁：

```bash
uv run pytest tests/
uv run ruff check backend/ tests/
bash scripts/lint.sh
node --check frontend/app.js
```

## 文档

**[📚 完整中文文档索引](docs/README_zh.md)** · **[English documentation](docs/README.md)**

- **上手：** [快速入门](docs/quickstart_zh.md) · [Linux](docs/install-linux_zh.md) · [macOS](docs/install-macos_zh.md) · [升级](docs/upgrade_zh.md)
- **配置：** [环境与 Provider](docs/configuration_zh.md) · [Skills](docs/skills_zh.md) · [定时任务](docs/scheduler_zh.md) · [移动端](docs/mobile_zh.md)
- **原理：** [架构](docs/architecture_zh.md) · [基础设施](docs/infrastructure_zh.md) · [原生实现规格](docs/specs/)
- **运维：** [排错](docs/troubleshooting_zh.md) · [数据与备份](docs/data-and-backup_zh.md) · [安全策略](SECURITY.md)
- **项目：** [贡献指南](CONTRIBUTING.md) · [第三方授权](THIRD_PARTY_LICENSES.md)

## 安全提示

持有 `MUSELAB_TOKEN` 的人可以操作 `MUSELAB_ROOT` 及所有已登记工作目录内的文件，并驱动 Codex 执行获准的工具。默认保持 `MUSELAB_HOST=127.0.0.1`；远程访问时使用 HTTPS 和额外访问控制，不要把 `.env`、`CODEX_HOME` 或真实工作区提交到仓库或镜像。

## 项目状态

当前版本为 `0.1.0a1`。核心 Codex Native 路径已经可用，协议兼容基线仍会随 Codex CLI 演进。升级前请查看[升级说明](docs/upgrade_zh.md)。

本仓库从 muselab 的历史基线独立演进，保留 MIT 许可证，但不是 GitHub Fork。

[MIT](LICENSE)
