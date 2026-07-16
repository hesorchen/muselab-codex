# 架构

> [English](architecture.md) · [← 文档索引](README_zh.md)

muselab-codex 是 `codex app-server` 的本地 Web 工作台。架构的核心不是“把模型 API 包成聊天页面”，而是保持 Codex 原生 thread、工具和权限语义，同时增加浏览器、文件工作区和长期运行能力。

```text
┌──────────────┐    HTTP／SSE + token    ┌──────────────────┐
│ Browser／PWA │ ──────────────────────→ │ FastAPI backend  │
└──────────────┘                         └────────┬─────────┘
                                                │ Unix WebSocket
                                                ▼
                                       ┌──────────────────┐
                                       │ codex app-server │
                                       └────────┬─────────┘
                                                │ Codex auth／config
                                                ▼
                                              Codex
```

## 所有权边界

| 领域 | 权威组件 | muselab-codex 的角色 |
|---|---|---|
| thread、turn、transcript | Codex app-server | 映射为 HTTP 资源和浏览器视图 |
| 模型、推理参数与服务档位 | Codex 配置和 thread | 展示模型目录，随会话提交 Effort、摘要与 catalog 驱动的 Fast 选择 |
| 工具、sandbox、审批 | Codex app-server | 展示请求并回传用户决定 |
| Skills、MCP、Memory | Codex app-server／`CODEX_HOME` | 提供管理界面，不重新实现发现规则 |
| 文件浏览与预览 | FastAPI／浏览器 | 在当前已登记工作目录下安全读写和渲染 |
| 登录与远程访问 | 部署环境 | 应用只提供单用户 token 鉴权 |
| 附件和用量 UI 元数据 | FastAPI sidecar | 保存浏览器恢复所需的最小数据 |

只要 Codex 已经提供原生能力，后端就适配协议而不复制业务语义。这条原则避免两个 transcript、两套工具循环或两套配置优先级长期漂移。

## 进程生命周期

应用启动时创建一个 `CodexRuntime`，由它监督单个
`codex app-server --listen unix://PATH` 子进程，并通过关闭压缩协商和应用层
keepalive 的 Unix WebSocket 连接（由内核 socket 关闭作为存活信号）：

1. 创建私有 socket 目录并启动 listener；
2. 发送 `initialize`，校验返回对象；
3. 发送 `initialized` notification；
4. 启动唯一的事件读取器、计划任务和外围服务；
5. FastAPI 开始接受会话和流式请求；
6. 关闭时依次停止 turn、终端、调度器、事件路由和 app-server。

同一个 listener 也接受 `codex --remote unix://PATH`，因此浏览器和终端共享
同一份内存 thread 状态；普通 `codex` 命令按 Codex 设计仍是独立 runtime。

当前协议测试基线为 `codex-cli 0.144.1`。升级基线时需要重新生成匹配的 app-server schema、更新离线 fixture，并执行显式的真实登录验证。

## 一次消息的完整链路

```text
POST /api/chat/stream/start
  → 校验 token、thread、模型和附件
  → 必要时 thread/resume
  → 订阅 thread 事件
  → turn/start
  → 返回一次性 stream ticket

GET /api/chat/stream?ticket=…
  → 回放当前 turn 已缓存事件
  → 持续发送 SSE 文本、工具、审批、用量和完成事件
  → turn 完成后触发队列 drain
```

stream ticket 避免长期把主 token 放在 SSE URL 中；下载和 SSE 等无法设置自定义 header 的浏览器通道使用受限查询参数，并配合访问日志脱敏与 `Referrer-Policy`。

## 为什么需要单一事件读取器

app-server notification 是一条共享流。如果每个浏览器 SSE 连接都直接调用 `next_notification()`，两个并发 thread 会互相消费事件。

`CodexEventRouter` 因此成为唯一读取者：

- 从 app-server 顺序读取 notification；
- 从 `threadId` 提取归属；
- 分发给对应 thread subscription；
- 将终端等 connection-scoped 事件发给连接级订阅；
- app-server 重启时关闭旧订阅，避免把两代进程的事件混在一起。

## thread、turn 与浏览器会话

- Codex thread ID 是会话主键，transcript 以 Codex 原生历史为准。
- 浏览器可以打开多个 thread 标签页；每个标签页维护自己的消息和 stream 状态，但会话区只显示当前 thread。应用级工作目录切换会同时切换可见会话标签、新 thread 的 cwd、文件树和预览；非默认目录必须先由后端登记并校验。
- 同一个 thread 同时只允许一个活动 turn；用户在运行期间继续发送的消息进入应用队列。
- fork 使用 Codex 原生 thread 分支；compact 在同一个 thread 内压缩活动上下文，并在 transcript 中记录压缩边界。
- app-server 进程重启后，持久 thread 会按 runtime generation 执行一次 `thread/resume`，再启动新 turn。

## 持久化与目录

```text
MUSELAB_ROOT/
├── AGENTS.md                         # workspace instructions
├── .codex/                           # optional workspace Codex config／Skills
├── .muselab-codex/
│   ├── attachments/
│   │   ├── staged/                   # 尚未发送的上传
│   │   └── threads/<thread-id>/      # 已归属 thread 的附件
│   └── usage/<thread-id>.json        # 仅数值型 token 用量快照
└── …                                 # 用户文件

CODEX_HOME/                            # 通常为 ~/.codex
├── config.toml
├── skills/
└── Codex 登录态、Memory 与原生 thread 历史
```

`.muselab-codex` 不是第二套会话数据库。它只保存 app-server transcript 未覆盖、但浏览器重启恢复需要的附件归属和脱敏用量数字。

## 文件系统边界

文件 API 默认以 `MUSELAB_ROOT` 为根；切换后则以当前后端已登记并校验的工作目录为根：

- 拒绝 `..` 穿越和解析后逃逸的符号链接；
- 屏蔽凭证形状和内部敏感文件；
- 写入使用原子替换；
- 删除默认进入工作区回收站；
- 上传、附件和预览都有大小与类型限制。

这是一道应用级边界，不等同于多租户隔离。持有 `MUSELAB_TOKEN` 的人仍可驱动 Codex 在其 sandbox／审批策略允许的范围内执行工具。

## 故障模型

| 故障 | 行为 |
|---|---|
| app-server 启动失败 | FastAPI lifespan 失败，健康检查不会报告 ready |
| app-server 运行中退出 | runtime 标记 failed；下一次显式请求会创建新进程，不自动重放可能已生效的变更请求 |
| SSE 断开 | turn 继续运行；重新连接可回放进程内事件缓存 |
| 浏览器刷新 | thread transcript 从 Codex 历史恢复，附件和用量从 workspace sidecar 恢复 |
| MCP／推送外围能力失败 | 核心聊天尽量继续运行，并在对应界面显示降级状态 |

不对变更型 app-server 请求做自动重试，是为了避免“服务端已执行、客户端未收到响应”时重复写文件或重复启动 turn。

## 相关实现

- `backend/codex/process.py`：WebSocket JSON-RPC 双向协议和 server-initiated request
- `backend/codex/runtime.py`：进程生命周期和 generation
- `backend/codex/event_router.py`：单读取者事件分发
- `backend/codex/threads.py`、`turns.py`：thread／turn 生命周期
- `backend/codex/approvals.py`、`user_input.py`：审批和结构化提问
- `backend/files.py`：工作区文件边界

阶段性协议选择和验证证据见[原生实现规格](specs/)。
