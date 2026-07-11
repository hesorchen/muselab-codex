# 定时任务

> [English](scheduler.md) · [← 文档索引](README_zh.md)

muselab 可以按计划运行一条保存好的 prompt —— 一个跑在后端 asyncio 循环里
的轻量 cron，无需外部调度器。每次触发都会走完整的 agent loop（工具 / MCP /
skills），和交互式回合一样，结果落到顶栏的小铃铛抽屉里。

典型用法：定期摘要（「总结 `notes/` 里的新内容并列出待办」）、周期性检查，
或任何你本来要按固定节奏重复敲的 prompt。

## 工作原理

- 任务存储在 `<workspace>/.muselab-codex/scheduler.json`，和其他 Codex-native sidecar
  元数据放在一起，重启后依然存在。
- 启动时会重算下次触发时间。服务停止期间错过的运行不会补跑；恢复后从下一个未来时间点继续。
- 一次运行完成会让**未读**计数 +1，显示为铃铛角标；打开抽屉即可清零。
- 运行完成会写入历史并增加未读数。当前版本尚未把计划任务完成事件自动连接到 Web Push。

## 调度类型

| 类型 | 触发时机 |
|------|---------|
| `daily` | 每天 `hh:mm`——也可通过 `times` 列表一天多次 |
| `weekly` | 选定的星期几（0 = 周一）的 `hh:mm` |
| `monthly` | 每月某天（1–31）的 `hh:mm` |
| `once` | 单次 `年 / 月 / 日` 的 `hh:mm`，触发后自动停用 |

浏览器会上报自己的 UTC 偏移（`tz_offset_minutes`），任务按**你的**本地时间
触发。未带偏移的旧任务回退到服务器本地时区。

## 会话模式

- **`fresh`**（默认）—— 每次运行都新建会话，运行之间互不影响。适合摘要、
  一次性报告。
- **`reuse`** —— 创建任务时预分配一个会话，每次运行都往里追加，上下文跨运行
  累积。

## API

所有端点都需要与其他应用 API 相同的 `X-Auth-Token` 鉴权。

| 方法与路径 | 用途 |
|---|---|
| `GET /api/scheduler/tasks` | 列出任务 + 当前未读数 |
| `POST /api/scheduler/tasks` | 创建任务 |
| `PATCH /api/scheduler/tasks/{id}` | 改名 / 改时间 / 启停 |
| `DELETE /api/scheduler/tasks/{id}` | 删除任务（**不会**删掉绑定的会话）|
| `POST /api/scheduler/tasks/{id}/run` | 计划外手动触发一次（重试 / 冒烟测试）|
| `GET /api/scheduler/history` | 运行日志，最新在前（`?limit=`，1–500）|
| `GET /api/scheduler/tasks/{id}/history` | 单个任务的运行日志 |
| `DELETE /api/scheduler/history` | 清空全部历史 |
| `DELETE /api/scheduler/history/{ts}` | 按时间戳删除单条历史 |
| `POST /api/scheduler/ack` | 未读角标归零 |

## 安全提示

定时运行是**无人值守**执行的，并使用 Codex `default` approval policy。没有浏览器在线回答审批或结构化提问时，需要交互的任务可能失败或超时。请像对待 cron 一样谨慎，只安排边界明确、无需人工判断的 prompt，并避免让不可信外部内容决定后续工具操作。
