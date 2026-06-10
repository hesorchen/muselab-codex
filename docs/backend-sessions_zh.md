# 会话内部机制

> [English](backend-sessions.md)

本页介绍 muselab 如何存储和管理对话会话：与 Claude CLI 共享的双层存储、会话索引与 sidecar 元数据文件、服务端消息队列（queue）、附件处理、会话分支（fork）以及重启恢复。

相关阅读：[架构](architecture_zh.md) · [数据与备份](data-and-backup_zh.md) · [定时任务](scheduler_zh.md)

---

## 1. 双层存储与所有权

每个会话由 **Claude CLI** 和 **muselab** 共同持有。两层之间没有交叉——各自负责不同维度的数据。

```
~/.claude/projects/<cwd-key>/          CLI 持有此目录树
└── <sid>.jsonl                        ← 对话记录（消息、工具调用、压缩边界）

muselab/sessions/                      muselab 持有此目录树
├── index.json                         ← 会话列表 + 展示元数据
├── <sid>.sidecar.json                 ← 逐条消息的标注信息 + 上下文量表
├── <sid>.queue.json                   ← 服务端消息队列（仅在非空时存在）
└── active_turns/<sid>.json            ← 进行中的回合哨兵文件（正常结束后删除）

$MUSELAB_ROOT/.muselab-attach/<sid>/   muselab 持有此目录树（位于归档目录下）
└── <original-filename>                ← 持久化的附件原文件
```

`<cwd-key>` 由 SDK 的 `project_key_for_directory(ROOT)` 派生——例如 `/home/alice/archive` 对应 `-home-alice-archive`。
（[`backend/chat.py:L99-L113`](../backend/chat.py#L99-L113)）

对于第三方厂商模型（DeepSeek、GLM、MiniMax、Kimi、Qwen、MiMo），CLI 会使用 `/tmp/muselab-vendor-cli-config-<uid>/` 下的隔离配置目录，而非 `~/.claude/`。（[`backend/chat.py:L69-L97`](../backend/chat.py#L69-L97)）

**各层存储内容一览：**

| 数据项 | 所有者 | 位置 |
|--------|-------|------|
| 对话记录（消息、工具调用、压缩边界） | CLI | `~/.claude/projects/<cwd-key>/<sid>.jsonl` |
| `custom_title` / `aiTitle`（每回合后由 Haiku 生成） | CLI | 同一 JSONL |
| `last_modified`、`created_at`、`first_prompt`、`tag` | CLI | 同一 JSONL |
| 会话显示名、`model`、`system_prompt`、`auto_named`、`pinned`、`effort`、`thinking` | muselab | `sessions/index.json` |
| 已在 UI 中创建但尚未发送消息的会话 | muselab | 仅 `sessions/index.json`（JSONL 尚不存在） |
| 每条消息的费用、模型标识、时间戳、附件引用 | muselab | `sessions/<sid>.sidecar.json` |
| 服务端待处理消息 | muselab | `sessions/<sid>.queue.json` |
| 附件原文件（全分辨率） | muselab | `$MUSELAB_ROOT/.muselab-attach/<sid>/` |

所有层使用同一个 UUID（`sid`）作为主键——不需要转换表。（[`backend/sessions.py:L80-L81`](../backend/sessions.py#L80-L81)，[`backend/chat.py:L116-L127`](../backend/chat.py#L116-L127)）

---

## 2. 会话索引

`sessions/index.json` 是一个 JSON 数组，每个条目代表一个会话，记录 CLI 不跟踪的元数据。
（[`backend/sessions.py:L75-L77`](../backend/sessions.py#L75-L77)）

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | string（UUID v4） | 主键；与 CLI JSONL 文件名一致 |
| `name` | string | 显示名；可以是首行片段或用户自定义标题 |
| `model` | string | 空字符串表示"使用已配置的默认值" |
| `system_prompt` | string | 会话级覆盖；空 = muselab 默认 |
| `created_at` | float（Unix 秒） | SDK 的 `created_at` 单位是毫秒，合并时除以 1000 |
| `updated_at` | float（Unix 秒） | 从 `last_modified`（毫秒→秒）派生；每次 `bump_session()` 调用时更新 |
| `message_count` | int | SDK 全部消息帧数量；每回合后更新 |
| `turn_count` | int | 仅统计用户输入的 prompt（不含工具调用结果的旁路帧） |
| `auto_named` | bool | 用户重命名前或第一条有意义的消息成为标题前为 `true` |
| `pinned` | bool | 置顶的会话排在最前；不存储在 CLI JSONL 中 |
| `effort` | string | `""` / `"low"` / `"medium"` / `"high"` / `"xhigh"` / `"max"`；空 = SDK 自适应 |
| `thinking` | bool | 扩展思考（extended thinking）开关；默认为 `true` |

（[`backend/sessions.py:L194-L238`](../backend/sessions.py#L194-L238)，[`backend/sessions.py:L342-L374`](../backend/sessions.py#L342-L374)）

`tag` 和 `first_prompt` **不**存储在 `index.json` 中，而是在读取时从 `SDKSessionInfo` 合并进来。
（[`backend/sessions.py:L228-L229`](../backend/sessions.py#L228-L229)）

`list_sessions()` 将 SDK 的 JSONL 扫描结果与 `index.json` 合并。仅存在于 `index.json` 中（尚无 JSONL）的会话会追加在末尾，确保它们在创建后立即出现在选择器中。
（[`backend/sessions.py:L318-L334`](../backend/sessions.py#L318-L334)）

列表会缓存 30 秒（`_LIST_CACHE_TTL_S = 30.0`）。muselab 内部的任何变更操作（创建、重命名、删除、置顶、更新）都会通过 `_save_index` 立即调用 `invalidate_sessions_cache()`，因此 UI 操作的效果立即可见；只有外部 `claude --resume` 的写入需要等待 TTL。（[`backend/sessions.py:L129-L163`](../backend/sessions.py#L129-L163)）

---

## 3. Sidecar 元数据文件

每个会话在 `sessions/` 下最多对应三个文件：

| 文件名 | 用途 |
|--------|------|
| `<sid>.sidecar.json` | 逐条消息标注：费用、模型标识、时间戳、附件引用、上下文量表 |
| `<sid>.queue.json` | 服务端消息队列（仅在非空或已暂停时存在） |
| `<sid>.json` | **遗留格式** —— 2026-05-17 之前的完整对话记录；现已不再写入 |

（[`backend/sessions.py:L80-L81`](../backend/sessions.py#L80-L81)，[`backend/sessions.py:L761-L762`](../backend/sessions.py#L761-L762)，[`backend/sessions.py:L26`](../backend/sessions.py#L26)）

### Sidecar 顶层结构

| 键 | 类型 | 描述 |
|----|------|------|
| `messages` | object（UUID → 标注信息） | 逐条消息的标注，键为与 CLI JSONL 对应的消息 UUID |
| `context_max_tokens` | int 或 null | SDK 测量的 `maxTokens`，用于上下文量表的分母；持久化后重启时无需等待新回合即可正确显示 |
| `pending_attachments` | array | 消息 UUID 确定前的临时上传队列（见第 5 节） |

（[`backend/sessions.py:L174-L183`](../backend/sessions.py#L174-L183)，[`backend/sessions.py:L576-L607`](../backend/sessions.py#L576-L607)）

### 逐条消息标注字段

`messages` 中每个条目以 CLI JSONL 里的**消息 UUID** 为键：

| 字段 | 归属 | 描述 |
|------|------|------|
| `cost` | 助手回合 | 根据 `ResultMessage` token 数量计算的本回合 USD 费用 |
| `model` | 助手回合 | 生成本次回复的模型 ID（在 UI 中显示为气泡标识） |
| `ts` | 助手回合 | 标注写入时的 Unix 秒时间戳 |
| `elapsed_s` | 助手回合 | 本回合流式输出所用的实际秒数 |
| `images` | 用户消息 | 本消息使用的已上传图片（缩略图 URL；base64 不存储在此） |
| `docs` | 用户消息 | 本消息使用的已上传文档 |

（[`backend/sessions.py:L556-L573`](../backend/sessions.py#L556-L573)）

sidecar 文件**只**负责存储标注——对话记录本身由 CLI JSONL 负责。每回合结束后，`chat.py` 会独立调用 `bump_session()`（更新索引）和 `set_message_annotation()`（更新 sidecar）。
（[`backend/sessions.py:L19-L23`](../backend/sessions.py#L19-L23)）

所有 sidecar 的读写操作都通过 `_SIDECAR_LOCK` 串行化，以防 FastAPI 线程池中的并发操作导致数据丢失。索引的读写操作同理通过 `_INDEX_LOCK` 串行化。所有写入均使用 `atomic_write_text()`（临时文件 + `os.replace()`），确保崩溃不会造成文件数据损坏。
（[`backend/sessions.py:L101-L123`](../backend/sessions.py#L101-L123)）

---

## 4. 消息队列（queue）

muselab 为每个会话维护一个**服务端 FIFO 消息队列**。当一条回合正在进行时，用户提交的新消息会进入队列而非被丢弃。队列排空循环（drain loop）会在当前回合结束后自动弹出队首并启动下一回合——即使此时没有任何浏览器连接。

（[`backend/sessions.py:L738-L757`](../backend/sessions.py#L738-L757)，[`backend/chat.py:L6633`](../backend/chat.py#L6633)，[`backend/chat.py:L6739-L6770`](../backend/chat.py#L6739-L6770)）

### `<sid>.queue.json` 结构

```json
{
  "items": [
    {
      "id": "q-<8-hex>",
      "text": "<用户消息文本>",
      "image_ids": "<逗号分隔的上传 ID>",
      "enqueued_at": 1718000000000
    }
  ],
  "paused": false
}
```

（[`backend/sessions.py:L747-L756`](../backend/sessions.py#L747-L756)，[`backend/sessions.py:L800-L816`](../backend/sessions.py#L800-L816)）

### 队列机制

- **最大深度：** 10 条（`_QUEUE_MAX = 10`）。第 11 条入队会返回 `{"ok": false, "error": "queue_full"}`。
  （[`backend/sessions.py:L758`](../backend/sessions.py#L758)，[`backend/sessions.py:L806-L807`](../backend/sessions.py#L806-L807)）
- **FIFO 排空：** `dequeue_message()` 弹出队首；`reorder_queue()` 允许在排空前调整顺序。
  （[`backend/sessions.py:L819-L880`](../backend/sessions.py#L819-L880)）
- **出错自动暂停：** 当一个排队中的回合发生错误、超时或被取消时，`paused` 标志会置为 `true`，自动排空停止，直到用户手动恢复。（[`backend/sessions.py:L858-L865`](../backend/sessions.py#L858-L865)）
- **文件生命周期：** 当 `items` 为空且 `paused` 为 `false` 时，队列文件会被删除，避免积累空文件。
  （[`backend/sessions.py:L783-L791`](../backend/sessions.py#L783-L791)）
- **竞争失败时重入队：** 若排空触发器在并发竞争中落败或启动回合失败，该条目会通过 `requeue_head` 重新插回队首，确保没有消息被静默丢弃。
  （[`backend/sessions.py:L831-L840`](../backend/sessions.py#L831-L840)）
- **附件 TTL 注意事项：** 队列条目中的 `image_ids` 引用的是内存中的 `_image_store`（10 分钟 TTL）。如果队列中的回合延迟超过该 TTL，其附件将以纯文本形式发送。
  （[`backend/sessions.py:L754-L757`](../backend/sessions.py#L754-L757)）

---

## 5. 附件

### 内存暂存

上传的文件在绑定到具体消息之前，先暂存于内存中：

1. `POST /api/chat/upload-image` 接收 multipart 文件。
2. 文件被分类为 `image`（png/jpg/gif/webp）、`pdf`、`text` 或 `xlsx`。
3. 以随机生成的 `aid` 存储到 `_image_store[aid]` 中。
4. TTL：**10 分钟**（`_IMAGE_TTL_S = 600`）。
   （[`backend/chat.py:L4647-L4648`](../backend/chat.py#L4647-L4648)）
5. 容量上限：最多 48 条或总计 256 MB，超限时按最旧条目优先驱逐。单文件上限：原始大小 10 MB；文本文件上限 200 KB。
   （[`backend/chat.py:L4649-L4680`](../backend/chat.py#L4649-L4680)）

**后端重启后所有暂存上传均会丢失** —— 内存存储不持久化，预期的恢复路径是重新上传附件。

### 持久化到归档目录

当某次上传被一个回合消费时，原始文件会保存到磁盘：

```
$MUSELAB_ROOT/.muselab-attach/<sid>/<original-filename>
```

此路径在重启后依然有效，可通过 `GET /api/chat/sessions/{sid}/attachment/{filename}?token=...` 提供灯箱（lightbox）展示。（[`backend/chat.py:L1479-L1493`](../backend/chat.py#L1479-L1493)）

### 待绑定附件队列（UUID 确定前）

SDK 异步写入用户消息的 JSONL 记录，因此上传时消息 UUID 尚不可知。muselab 在 sidecar 中使用 `pending_attachments` 列表作为暂存区：

1. 上传时，`append_pending_attachments(sid, images, docs)` 向其追加一个 `{"ts", "images", "docs"}` 捆包（bundle）。最多 50 条；每次调用时会裁剪掉超过 24 小时的旧条目。
   （[`backend/sessions.py:L622-L661`](../backend/sessions.py#L622-L661)）
2. 当 `GET /sessions/{sid}` 遇到含内嵌图片引用但没有标注的用户消息时，`consume_one_pending_attachments(sid, msg_uuid)` 会弹出最旧的捆包，将其提升为 `messages[msg_uuid]` 下的永久标注。
   （[`backend/sessions.py:L664-L686`](../backend/sessions.py#L664-L686)）

绑定完成后，用户消息标注的结构如下：

```json
{
  "images": [{"thumb": "<160px base64>", "url": "/api/chat/sessions/<sid>/attachment/<filename>?token=..."}],
  "docs":   [{"name": "<filename>", "text": "<content>"}]
}
```

---

## 6. 分支（fork）与消息编辑

`POST /api/chat/sessions/{sid}/fork` 可以从对话中的任意位置创建一个分支。（[`backend/chat.py:L3898-L3933`](../backend/chat.py#L3898-L3933)）

1. SDK 的 `fork_session()` 将 CLI JSONL 对话记录复制到指定的 `up_to_message_id` 为止，生成带有新 `new_sid` 和新消息 UUID 的全新 JSONL。
2. muselab 调用 `register_session(new_sid, ...)` 将该分支添加到 `index.json`。分支继承源会话的 `model` 和 `system_prompt`。
   （[`backend/sessions.py:L342-L374`](../backend/sessions.py#L342-L374)）
3. 分支立即出现在会话选择器中。

主要使用场景是**消息编辑**：当用户编辑某条历史消息时，UI 会在上一条助手消息处执行 fork，然后将修改后的文本发送到新分支。

---

## 7. 重启恢复

### 进行中回合的哨兵文件

回合启动时，会向 `sessions/active_turns/<sid>.json` 写入一个小型哨兵文件：

```json
{
  "sid": "<session-id>",
  "user_text": "<完整的用户 prompt>",
  "user_text_preview": "<首行，最多 200 字符>",
  "model": "<模型 ID>",
  "started_at": 1718000000.0
}
```

该文件在回合正常结束（成功、出错或超时）时删除。若 muselab 在回合进行中被强制终止，该文件会留存。

（[`backend/chat.py:L513-L606`](../backend/chat.py#L513-L606)）

### 启动扫描

进程启动时，`_scan_interrupted_turns_at_startup()` 会读取所有残留的 `active_turns/*.json` 文件，存入 `_interrupted_at_startup`。浏览器下次连接时，每个未完成的回合都会以 **toast 通知**形式弹出，显示 prompt 预览和模型信息，由用户决定是否重新发送。

**muselab 刻意不自动恢复。** 自动恢复会在用户可能已放弃或决定重新措辞的 prompt 上消耗 token。
（[`backend/chat.py:L582-L606`](../backend/chat.py#L582-L606)）

### 其他重启行为

| 状态 | 行为 |
|------|------|
| 会话列表 | 从 `index.json` + SDK JSONL 扫描重建；无需预热 |
| 上下文量表分母 | 持久化在每个 sidecar 的 `context_max_tokens` 中；重启后无需等待新回合即可立即正确显示（[`backend/sessions.py:L576-L607`](../backend/sessions.py#L576-L607)） |
| 暂存上传（`_image_store`） | **丢失** —— 仅在内存中；用户需重新上传附件 |
| 队列中待处理的消息 | 持久化在 `<sid>.queue.json` 中；发送一个回合后排空即恢复 |
| 列表缓存 | 重启后冷启动；首次请求需全量 JSONL 扫描（大型归档约 400 毫秒）；后续命中 30 秒 TTL 缓存 |

---

## 文件布局总览

```
muselab/sessions/
├── index.json                   会话列表（名称、模型、是否置顶……）
├── <sid>.sidecar.json           逐条消息的费用 / 模型 / 附件 + 上下文量表
├── <sid>.queue.json             服务端队列（空且未暂停时不存在）
└── active_turns/
    └── <sid>.json               进行中的回合哨兵文件（正常结束后删除）

~/.claude/projects/<cwd-key>/
└── <sid>.jsonl                  对话记录 —— CLI 独有

$MUSELAB_ROOT/.muselab-attach/
└── <sid>/
    └── <filename>               持久化的附件原文件
```

关于哪些路径需要纳入备份以及如何在新机器上还原，请参阅[数据与备份](data-and-backup_zh.md)。
