# 配置参考

> [English](configuration.md) · [← 文档索引](README_zh.md)

muselab-codex 把配置分成三层，每层只有一个权威来源：

| 层 | 权威来源 | 示例 |
|---|---|---|
| 部署与工作区 | 仓库 `.env`／进程环境 | host、port、token、`MUSELAB_ROOT` |
| Codex 用户配置 | `CODEX_HOME` | 登录态、`config.toml`、Memory、用户 Skills、MCP |
| 工作区配置 | `MUSELAB_ROOT` | `AGENTS.md`、`.codex/`、工作区 Skills |

网页设置不会成为第四套配置系统。模型、Skills 和 MCP 的变更通过 app-server 写回 Codex 原生配置。

## 应用环境变量

| 变量 | 必需 | 默认值 | 说明 |
|---|:---:|---|---|
| `MUSELAB_TOKEN` | 是 | 无 | 至少 16 字符；保护所有有意义的 HTTP／SSE 操作 |
| `MUSELAB_ROOT` | 是 | 无 | 已存在且由当前用户拥有的绝对工作区路径 |
| `MUSELAB_HOST` | 否 | `127.0.0.1` | Web 监听地址；除非有受控网络边界，否则不要改为 `0.0.0.0` |
| `MUSELAB_PORT` | 否 | `8765` | Web 监听端口，非法值会回退并限制为正整数 |
| `CODEX_BIN` | 否 | 自动查找 `codex` | 指定 Codex CLI 绝对路径或命令名 |
| `MUSELAB_CODEX_HISTORY_READ_TIMEOUT_SECONDS` | 否 | `8` | 读取大型 thread 历史的客户端超时 |
| `MUSELAB_CODEX_COMPACT_TIMEOUT_SECONDS` | 否 | `600` | compact 摘要 turn 的最长等待时间 |
| `MUSELAB_VAPID_SUBJECT` | 否 | `mailto:noreply@muselab.dev` | Web Push VAPID subject，必须是 `mailto:` 地址 |

最小 `.env`：

```dotenv
MUSELAB_TOKEN=replace-with-a-long-random-token
MUSELAB_ROOT=/absolute/path/to/workspace
MUSELAB_PORT=8765
MUSELAB_HOST=127.0.0.1
```

`.env` 含 token 和可能的 Provider key，权限应限制为当前用户，且不能提交到 Git。

## `MUSELAB_ROOT`

`MUSELAB_ROOT` 是默认工作目录。后端启动时会解析并校验它：

- 路径必须存在；
- 不能省略并回退到整个 `$HOME`；
- 拒绝 `/`、`/etc`、`/root`、`/home`、`/var`、`/usr`、`/boot` 等危险根目录；
- 文件 API 会再次阻止路径穿越、符号链接逃逸和敏感文件访问。

还可以从工作目录选择器登记其他已存在目录；它们必须通过相同的危险根目录校验，登记后文件树、预览、会话标签和新 thread 的 cwd 会一起切换。推荐只登记明确的项目子目录，不要把整个 home 目录暴露给文件工作台。

## `AGENTS.md` 与上下文

Codex 原生 instruction 文件是工作区根目录的 `AGENTS.md`。可以用 `scripts/intake.sh` 创建模板；项目内 `.codex/` 可承载工作区 Codex 配置和 Skills。

上下文优先级、Memory 加载和 instruction 合并规则最终以 Codex 为准。muselab-codex 只在设置和上下文界面显示来源是否存在，不把文件内容复制进应用数据库。

## `CODEX_HOME`

未显式设置时通常是 `~/.codex`，其中可能包含：

- `config.toml` 和模型 Provider 配置；
- Codex 登录凭证；
- 用户级 Skills、Memory 和 MCP 设置；
- Codex 管理的 thread／rollout 数据。

它必须可由服务用户读写。Docker 使用时应挂载为私有持久卷，不要复制进镜像。

## Codex 原生 Provider

应用内置三项经过真实 Responses API 和工具调用验证的配置：

| ID | 模型 | Base URL | 环境变量 |
|---|---|---|---|
| `minimax` | `minimax-m2.7` | `https://api.minimaxi.com/v1` | `MINIMAX_API_KEY` |
| `qwen` | `qwen3.7-plus` | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `DASHSCOPE_API_KEY` |
| `mimo` | `mimo-v2.5-pro` | `https://api.xiaomimimo.com/v1` | `XIAOMI_MIMO_API_KEY` |

启用流程：

1. 把 key 放进服务进程可继承的私有环境；
2. 重启服务，让 app-server 继承变量；
3. 打开“设置 → 模型”；
4. 打开对应开关；
5. 在新会话的模型菜单中选择模型。

开关通过 `config/value/write` 写入 Codex `model_providers.<id>`，使用 `wire_api = "responses"`。网页只看到环境变量名和启用状态，不读取 key 值。

为兼容性，这三项 Provider 的 thread 配置会关闭 Codex Web Search；文件、终端、Skills 和 MCP 等本地工具仍由 Codex 原生执行。

## 设置界面能修改什么

| 设置 | 持久化位置 |
|---|---|
| 模型 Provider 开关 | Codex `config.toml` 的 `model_providers` |
| Skills 启用状态 | app-server `skills/config/write` |
| MCP server | app-server MCP 配置接口 |
| 会话模型、审批策略、effort、Fast 档位 | Codex thread／turn 参数；稳定读取暂不回传的显式选择保存在最小兼容 sidecar |
| 主题、布局和部分 UI 偏好 | 浏览器本地存储 |

网页不会修改 `MUSELAB_ROOT`、监听地址、主 token 或 Provider key。部署级配置需要编辑私有环境并重启。

Fast 是 Codex 原生 `serviceTier`，不是 Effort 的一个等级。界面只在当前模型的
`model/list.serviceTiers` 发布名为 Fast 的档位时展示；原生档位 ID 以目录为准
（Codex 0.144.1 当前为 `priority`），不是前端写死的值。开启后生成通常更快，
但会消耗更多账户额度。Standard／Fast 按会话保存，并从下一轮开始生效。

## Docker 配置

`docker-compose.yml` 会把宿主 `.env` 注入容器，并覆盖容器内：

```text
MUSELAB_ROOT=/data
CODEX_HOME=/home/muse/.codex
```

需要挂载：

- `ARCHIVE_DIR` → `/data`；
- 宿主 `CODEX_HOME` → `/home/muse/.codex`。

容器默认端口映射只绑定 `127.0.0.1`。Docker 中的登录态和配置卷同样属于敏感数据。

## 变更后何时生效

| 变更 | 是否重启 |
|---|:---:|
| 修改 `.env`、Provider key、`CODEX_BIN` | 是 |
| 网页启停原生 Provider | 通常无需重启；新 thread 生效 |
| 修改 `AGENTS.md` | 后续 Codex thread／turn 按原生规则加载 |
| 增删 Skill | 重新打开 Skills 抽屉强制刷新 |
| 修改 MCP | 通过设置页刷新或重载 |
| 修改前端静态文件 | 开发模式刷新；受管理服务重启后强刷浏览器 |
