# Skills（技能包）

> [English](skills.md) · [← 文档索引](README_zh.md)

muselab-codex 使用 `codex app-server` 暴露的 Codex Skills 目录，浏览器不会另行扫描技能目录。

## 发现机制

后端针对当前 `MUSELAB_ROOT` workspace 调用 `skills/list`，并在设置页和聊天输入框的
Skills 抽屉中展示 app-server 返回的结果。目录可能包含以下 app-server 作用域：

| 作用域 | 含义 |
|---|---|
| `user` | 来自用户 Codex 配置、对该用户可用的技能 |
| `repo` | 针对当前 workspace 发现的技能 |
| `system` | Codex 提供的系统技能 |
| `admin` | 由管理员管理的技能 |

用户和 workspace 技能通常放在：

```text
$CODEX_HOME/skills/your-skill/SKILL.md
<MUSELAB_ROOT>/.codex/skills/your-skill/SKILL.md
```

未显式覆盖时，`CODEX_HOME` 默认为 `~/.codex`。最终作用域、优先级、元数据和启用
状态都以 app-server 为准。


## 浏览器控制

`GET /api/chat/skills` 返回权威目录。每次打开 Skills 抽屉都会请求 app-server 强制
刷新，因此新安装的技能无需重启 muselab-codex 就能出现。

设置页通过 `PATCH /api/chat/skills` 启用或禁用已发现的技能。后端只接受当前
app-server 列表中的精确路径，并把持久化交给 `skills/config/write`。禁用后的技能
仍然可见，但「试用」按钮和输入建议不会使用它。

如果技能声明了 UI `defaultPrompt`，「试用」会把它填入聊天输入框；否则
muselab-codex 会根据技能名生成一条简短的通用提示。

## 添加技能

在 Codex 发现位置创建一个包含 `SKILL.md` 的目录。最小文件由 YAML frontmatter 和
Markdown 指令组成：

```markdown
---
name: your-skill
description: Use when the user asks for this capability.
---

# Workflow

在这里说明执行步骤、约束和引用的资源。
```

描述应足够具体，让 Codex 只在合适的任务中选择该技能。脚本、参考资料和素材可以与
`SKILL.md` 放在同一目录，并用相对路径引用。重新打开 Skills 抽屉即可刷新发现结果。

实现合同和验证记录见 [Codex-native Skills](specs/0006-native-skills.md)。
