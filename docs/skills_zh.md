# Skills（技能包）

> [English](skills.md)

Skills 是 SKILL.md 指令包，由 Claude Agent SDK 在启动时加载并提供给 Muse 使用。当任务与某个 skill 的触发条件匹配时，模型会读取该 skill 的正文并遵循其协议——你无需在端上做任何额外配置。Skills 在交互式聊天、[定时任务](scheduler_zh.md)以及其他运行完整 agent 循环的上下文中均以相同方式工作。

**示例。** 一个名为 `changelog-formatter` 的 skill，其 `description` 字段可能以 `"USE WHEN the user asks to format or generate a CHANGELOG entry"` 开头。每当你让 Muse 编写 changelog 时，SDK 就会浮现该 skill，模型将自动采用其输出规范。

---

## 内置 Skills

Muselab 开箱即附 11 个 skill。前七个是 muselab 原生 skill（MIT 许可）；后四个来自社区贡献并注明了出处——上游 URL 和许可证详情见 [`THIRD_PARTY_LICENSES.md`](../THIRD_PARTY_LICENSES.md#L47-L73)。

| Skill | 功能 | 来源 | 外部依赖 |
|---|---|---|---|
| `web-search` | 将模糊查询转化为精准搜索，至少打开一个来源确认时效性，返回带日期的引用答案 | muselab 原生 | `WebSearch` / `WebFetch` 工具或 `mcp__fetch__fetch` |
| `markdown-formatter` | 规范化标题层级、列表、表格、代码围栏、数学分隔符以及中文全角标点；仅返回改写后的文档 | muselab 原生 | 无 |
| `mermaid-helper` | 选择合适的 Mermaid 图表类型，编写经验证的语法，返回带简短说明的围栏代码块 | muselab 原生 | 无 |
| `code-reviewer` | 按严重程度顺序（Bug → 安全 → 正确性 → 性能 → 可维护性）审查代码，提供行号引用和修复片段 | muselab 原生 | 无 |
| `citation-formatter` | 将 DOI、arXiv ID、PubMed ID 和原始文本转换为 APA 7 / IEEE / GB/T 7714 / BibTeX 格式；尽可能获取权威元数据 | muselab 原生 | `WebFetch` 或 `mcp__fetch__fetch`（可选）|
| `task-decomposer` | 将模糊目标拆解为有序任务列表，附带规模估算、完成标准、关键路径步骤和已标记的未知项 | muselab 原生 | 无 |
| `summary-distiller` | 根据来源类型选择合适的摘要形式（TL;DR、要点、结构化、行动项）；逐字保留数字、人名和日期 | muselab 原生 | 无 |
| `pptx` | 通过 Bash 工具编写并运行内联 Python（`python-pptx`）生成 PowerPoint 文件 | [社区](../THIRD_PARTY_LICENSES.md#L63) | `python-pptx`（`pip install python-pptx`）|
| `csv-analyzer` | 用 `pandas` 加载 CSV，分析列类型，生成条件图表（PNG），在单次响应中输出完整分析 | [社区](../THIRD_PARTY_LICENSES.md#L64) | `pandas`；`matplotlib` / `seaborn` 可选 |
| `translate` | 三阶段内部流水线（直译 → 问题识别 → 润色再诠释）；仅输出最终中文文本，保留技术术语 | [社区](../THIRD_PARTY_LICENSES.md#L65) | 无 |
| `meeting-notes` | 使用四个预置模板，从原始笔记或会议记录中提取决策、行动项（含负责人和截止日期）及后续步骤 | [社区](../THIRD_PARTY_LICENSES.md#L66) | 无 |

---

## 发现机制

Skill 发现由传给 [`backend/chat.py`](../backend/chat.py) 中 `ClaudeAgentOptions` 的两个参数控制：

**`setting_sources`**（[`chat.py:L944`](../backend/chat.py#L944)）：

```python
setting_sources=["user", "project", "local"]
```

该配置告诉 SDK 从三个作用域加载 CLAUDE.md、memory 文件和 skill：

| 作用域 | 解析路径 |
|---|---|
| `user` | `~/.claude/`——与 Claude Code CLI 共享的用户全局配置 |
| `project` | 归档根目录 `cwd`（见下文）|
| `local` | `cwd` 内的 `.claude/` |

**`cwd` 即归档根目录**（[`chat.py:L902`](../backend/chat.py#L902)，
[`backend/settings.py:L188-L194`](../backend/settings.py#L188-L194)）：

```python
cwd=str(ROOT)   # ROOT 来自 .env 中的 MUSELAB_ROOT
```

因此，SDK 的 `local` 作用域会从 muselab 仓库（即包含你的 `.env` 的那个 checkout）中解析内置 `skills/` 目录。`pptx` 或 `csv-analyzer` 等 skill 产生的输出文件，若未指定路径则落在归档根目录中。

**`skills="all"`**（[`chat.py:L961`](../backend/chat.py#L961)）：

```python
if not is_third_party and not skills_off:
    opts_kwargs["skills"] = "all"
```

设置该标志后，SDK 会加载所有可发现的 `SKILL.md` 并提供给模型使用。无需复制或创建符号链接——内置 `skills/` 目录直接从仓库 checkout 中提供服务。

**UI 列表。** `GET /api/settings/skills` 接口
（[`api_settings.py:L1129-L1143`](../backend/api_settings.py#L1129-L1143)）
独立地为前端从三个路径枚举 skill：仓库的 `skills/`（project 作用域）、`~/.claude/skills/`（user 作用域）和 `~/.claude/plugins/marketplaces/*/plugins/*/skills/`（plugin 作用域）。
`SKILL.md` 和 `skill.md` 两种文件名均被接受
（[`api_settings.py:L1077`](../backend/api_settings.py#L1077)）。该列表为只读——不影响模型在运行时实际使用的内容。

---

## 添加自定义 Skill

### 存放位置

| 位置 | 作用域 | 对谁可见 |
|---|---|---|
| `<muselab-repo>/skills/your-skill/SKILL.md` | project | 仅 muselab |
| `~/.claude/skills/your-skill/SKILL.md` | user | muselab + 所有 Claude Code 项目 |

当两个 skill 同名时，project 作用域的 skill 在运行时优先于 user 作用域的 skill。

### 必需结构

```
skills/your-skill/
└── SKILL.md          ← 必须包含 YAML frontmatter
```

frontmatter 块至少须包含 `name` 和 `description`：

```yaml
---
name: your-skill
description: "USE WHEN ... — 一句话描述触发条件和功能"
---
```

正文是自由格式的 Markdown，模型每次调用时都会读取——保持简洁。推荐实践（参考 [`skills/README.md`](../skills/README.md)）：

- `description` 以 `"USE WHEN ..."` 开头——这是模型选择 skill 时最主要的信号。
- 用表格将场景映射到动作。
- 添加 `NOT use when` 节以防止过度触发。
- 可选：在同一子目录中放置参考脚本（`*.py`）或配置文件（`config.yaml`），并在 SKILL.md 正文中引用。

### 需要重启

Skills 在 SDK 初始化期间加载。添加或编辑 skill 后，须重启 muselab 服务：

**Linux（systemd）：**
```bash
systemctl --user restart muselab
```

**macOS（launchd）：**
```bash
launchctl kickstart -k "gui/$(id -u)/com.muselab"
```

---

## 注意事项

### Skills 在第三方提供商上被禁用

当会话使用第三方模型（DeepSeek、GLM / ZhipuAI、MiniMax 及其他由 `endpoints.is_third_party()` 检测到的模型）时，muselab 会完全从 SDK 选项中省略 `skills="all"`
（[`chat.py:L958-L961`](../backend/chat.py#L958-L961)）。代码注释直接说明了原因：

> "third-party vendors (DeepSeek / GLM / MiniMax) often time out or 400 on the bigger payload"
>（第三方提供商（DeepSeek / GLM / MiniMax）在面对更大的 payload 时经常超时或返回 400 错误）

Skills 作为额外内容注入系统提示中；扩大后的 payload 会可靠地触发多个提供商的超时或 HTTP 400 响应。为避免对话中途静默失败，muselab 对所有第三方会话禁用 skill。更多关于第三方环境的内容请参阅 [routing_zh.md](routing_zh.md) 和 [providers_zh.md](providers_zh.md)。

### 终止开关

若要对 Claude 模型也禁用 skill，请在 `.env` 中设置：

```
MUSELAB_DISABLE_SKILLS=1
```

可接受的值：`1`、`true`、`yes`（不区分大小写）
（[`chat.py:L959`](../backend/chat.py#L959)）。

---

*相关文档：[architecture_zh.md](architecture_zh.md) · [routing_zh.md](routing_zh.md) · [providers_zh.md](providers_zh.md)*
