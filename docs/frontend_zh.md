# 前端内部机制

> [English](frontend.md)

本文介绍 muselab 浏览器端代码的结构与运行方式。
PWA 安装步骤请参阅 [mobile_zh.md](mobile_zh.md)。
SSE 流的服务端实现请参阅 [routing_zh.md](routing_zh.md)。
整体系统布局请参阅 [architecture_zh.md](architecture_zh.md)。

---

## 1. 刻意不设构建步骤

muselab 不附带任何打包工具——没有 Webpack、Vite 或 Rollup。
前端是纯 HTML + CSS + JavaScript，直接从 `frontend/` 目录提供服务。
对贡献者而言，这意味着：编辑文件，刷新浏览器即可。

**脚本加载顺序**（[`index.html:50–67`](../frontend/index.html#L50)）——全部带 `defer`，保持顺序：

```
i18n/index.js → data/constants.js → app.js
  → vendor/marked.min.js → vendor/purify.min.js → vendor/alpine.min.js
```

`i18n/index.js` 和 `data/constants.js` 必须在 `app.js` 之前加载，因为 `app.js` 在模块顶层就会读取 `window.MUSELAB_STRINGS`。

**按需延迟加载（lazy-load）的重型库**——在首次使用时动态注入为 `<script>` 标签
（[`_loadHljs`](../frontend/app.js#L10946)、[`_loadKatex`](../frontend/app.js#L10986) 等），启动时不预加载：

| 库 | 大致体积 | 触发时机 |
|---------|-------------|--------------|
| Mermaid | ~3.3 MB | 首次出现 `mermaid` 代码块 |
| CodeMirror（核心 + 语言模式） | ~308 KB | 预览面板首次点击「编辑」 |
| highlight.js | ~124 KB | 聊天中首次出现 `<pre><code>` |
| KaTeX（JS + 字体）| ~562 KB | 消息中首次出现 `$`、`$$`、`\(` 或 `\[` |

**缓存清除**——`/` 路由在每次请求时重新渲染 `index.html`，将所有 `/static/…` URL 改写为追加 `?v=<asset_version>` 的形式
（[`backend/main.py:396–430`](../backend/main.py#L396)）。
service worker（服务工作线程）从 `/sw.js` 提供服务（而非 `/static/sw.js`），使其作用域覆盖整个源站
（[`backend/main.py:486–494`](../backend/main.py#L486)）。

---

## 2. 单一 Alpine 组件

整个 UI 是一个以 [`x-data="portal()"`](../frontend/index.html#L231) 为根的单一组件。所有响应式状态——会话、消息、流式标志、文件树、标签页、设置、通知条——都作为
[`portal()`](../frontend/app.js#L178) 工厂函数返回对象的属性存在。
没有 Alpine store，没有子组件。
`x-init` 会在 Alpine 启动前立即移除预加载遮罩；`x-effect` 使 `<title>` 保持响应式，无需手动侦听器
（[`index.html:231–232`](../frontend/index.html#L231)）。

### 三栏布局

| 面板 | 元素 | index.html 行号 | 内容 |
|------|---------|---------------------|----------|
| 左侧——文件树 | `<aside class="pane files">` | [277–499](../frontend/index.html#L277) | 上传 / 新建目录按钮、搜索栏、已打开文件条、通过 `x-for` 生成的 `<ul class="filelist">`、拖拽删除目标区 |
| 中间——预览 | `<section class="pane preview">` | [504–938](../frontend/index.html#L504) | 标签栏、CodeMirror 编辑器（`x-ref="cmHost"`）、带 `x-show` 分支的预览主体（支持 md / text / html / img / pdf / xlsx / csv 模式）|
| 右侧——聊天 | `<aside class="pane chat">` | [943–末尾](../frontend/index.html#L943) | 会话标签条、消息体（`x-for` 遍历 `messages`）、带 @-提及自动补全的输入框、模型 / 权限 / effort 选择器 |

---

## 3. 渲染管线

### marked → DOMPurify → KaTeX → linkify

每条助手消息都经过
[`_mdRenderUncached()`](../frontend/app.js#L3177) 处理：

1. **预处理**——修复流式传输途中未关闭的 ` ``` ` / `~~~` 围栏
   （[`app.js:3188–3192`](../frontend/app.js#L3188)）；在传给 marked 之前，将数学表达式
   （`$$…$$`、`$…$`、`\(…\)`、`\[…\]`）替换为不透明占位符，防止 LaTeX 中的下划线和星号被当作 Markdown 强调语法消耗
   （[`app.js:3193–3201`](../frontend/app.js#L3193)）。
2. **解析**——`window.marked.parse(parseInput)`；解析异常时降级为 `<pre>`
   （[`app.js:3206–3210`](../frontend/app.js#L3206)）。
3. **净化**——`DOMPurify.sanitize(raw, { USE_PROFILES: { html:true, mathMl:true }, FORBID_TAGS: ["style","iframe","form","object","embed"], FORBID_ATTR: ["style","formaction"], ADD_ATTR: ["aria-hidden"] })`
   （[`app.js:3213–3218`](../frontend/app.js#L3213)）。
4. **数学还原 + KaTeX**——解除占位符掩码，若 KaTeX 已加载则调用 `renderMathInElement`
   （[`app.js:3261–3273`](../frontend/app.js#L3261)）。
5. **文件路径 linkify（链接化）**——以 `_linkifyFilePaths` 遍历离线 DOM
   （[`app.js:3275`](../frontend/app.js#L3275)）。

**流式传输的轻量路径 vs. 最终 `flushRender`**——以 `{ streaming: true }` 调用时，函数在第 3 步后返回，跳过 KaTeX 和 linkify
（[`app.js:3240`](../frontend/app.js#L3240)）。
这两个 DOM 遍历仅在 `done` 事件时的 `flushRender()` 中执行
（[`app.js:13735–13741`](../frontend/app.js#L13735)）。

**Mermaid**——`_renderMermaidBlock` 以 `securityLevel: "strict"` 延迟加载 `vendor/mermaid.min.js`，仅在源码哈希值变化时重新渲染。

**沙盒 iframe 中的 HTML 产物**——AI 生成的 HTML 块通过 `srcdoc`（而非 `src`）挂载，使用
[`app.js:11229`](../frontend/app.js#L11229) 中定义的沙盒属性：

```
sandbox="allow-scripts allow-popups allow-popups-to-escape-sandbox allow-forms"
```

`allow-same-origin` **刻意缺省**，使 iframe 获得空源站，因此无论 AI 生成的代码如何尝试，均无法访问 muselab 的 DOM、`localStorage`、cookie 或鉴权令牌。
预览面板中的 HTML 文件使用更严格的 `sandbox="allow-scripts"`
（[`index.html:780–782`](../frontend/index.html#L780)）。

**LRU 缓存**——[`mdRender()`](../frontend/app.js#L3154) 将 `_mdRenderUncached` 包裹在基于 Map 的 LRU 缓存中（容量上限 400 条）；正在流式传输的气泡绕过缓存。

---

## 4. 消费 SSE 流

每次对话回合会向 `/api/chat/stream?prompt=…&session_id=…&model=…&permission=…&token=…` 打开一个回合级
[`EventSource`](../frontend/app.js#L13582)
（[`app.js:13575–13582`](../frontend/app.js#L13575)）。
鉴权令牌作为查询参数传递，因为 `EventSource` 不支持自定义请求头。

### 已处理的事件类型

| 事件 | 处理函数 | 动作 |
|-------|---------|--------|
| `text` | [`app.js:13777`](../frontend/app.js#L13777) | 将 `d.text` 追加到累加器；调用 `scheduleRender()` |
| `thinking` | [`app.js:13802`](../frontend/app.js#L13802) | 合并到最近的思考气泡，或推入新气泡 |
| `tool_use` | [`app.js:13822`](../frontend/app.js#L13822) | 推入 `{role:"tool_use", name, id, …}`；可能触发预览刷新 |
| `tool_result` | [`app.js:13857`](../frontend/app.js#L13857) | 推入 `{role:"tool_result", id, tool_name, preview, …}` |
| `task_started` | [`app.js:13879`](../frontend/app.js#L13879) | 给匹配的 `tool_use` 打上 `{state:"running", …}` 戳 |
| `task_progress` | [`app.js:13889`](../frontend/app.js#L13889) | 更新匹配 `tool_use` 的运行状态与用量 |
| `task_notification` | [`app.js:13899`](../frontend/app.js#L13899) | 打上终态戳（`completed`/`failed`/`stopped`）|
| `rate_limit` | [`app.js:13915`](../frontend/app.js#L13915) | 合并限速窗口数据；重新计算 `rlBadge` |
| `ask_user_question` | [`app.js:13927`](../frontend/app.js#L13927) | 推入带预填 `pendingAnswers` 的交互式问题气泡 |
| `permission_request` | [`app.js:13956`](../frontend/app.js#L13956) | 推入带「允许 / 拒绝」控件的权限气泡 |
| `done` | [`app.js:14047`](../frontend/app.js#L14047) | `flushRender()`（含 KaTeX + linkify 的完整遍历）；解析成本 / 统计；关闭 `EventSource`；触发 `highlightCode` |
| `ping` / `cancelled` | [`app.js:13611–13614`](../frontend/app.js#L13611) | 仅更新 `_lastSseActivity` |

**渲染节流：80 ms → 1600 ms**——对每个 token 都重新解析完整累加器，在长回复时复杂度为 O(n²)。
`scheduleRender()` 随累加器大小拉长间隔
（[`app.js:13694–13707`](../frontend/app.js#L13694)）：
80 ms（< 2 KB）→ 160 ms（< 8 KB）→ 320 ms（< 20 KB）→ 600 ms（< 50 KB）→
1000 ms（< 120 KB）→ 1600 ms（≥ 120 KB）。
`done` 事件触发的 `flushRender()` 始终绘制完整的最终文本。

### 40 秒停滞看门狗 vs. 15 秒服务端心跳

服务端每 15 秒发送一次命名 `ping` 事件（见 [routing_zh.md](routing_zh.md)）。
一个 `setInterval` 看门狗每 10 秒触发一次；若超过 40 秒（≥ 2 次心跳缺失）无任何 SSE 活动，则合成一个传输层 `error` 事件以触发重连路径
（[`app.js:13615–13627`](../frontend/app.js#L13615)）。

---

## 5. 国际化（i18n）

[`frontend/i18n/index.js`](../frontend/i18n/index.js)（735 行）导出
`window.MUSELAB_STRINGS = { zh: {…}, en: {…} }`——每个语言区域包含约 200+ 个键的扁平字典。

默认语言区域为 `zh`；若 `navigator.language` 以 `"en"` 开头则自动检测为 `en`
（[`app.js:1946–1950`](../frontend/app.js#L1946)）。
`localStorage` 条目（`muselab_lang`）可覆盖自动检测并跨会话持久化；可在**设置 → 语言**中切换
（[`app.js:1952`](../frontend/app.js#L1952)）。

[`t(key)`](../frontend/app.js#L1960) 查找 `STRINGS[this.lang][key]`，回退至 `STRINGS.zh[key]`，再回退至原始键——缺失的翻译以自身键名呈现，而非静默显示空白。

Alpine 启动前的预加载遮罩也通过一段内联 `<script>` 实现了本地化，该脚本在 Alpine 初始化前检查 `navigator.language`
（[`index.html:84–95`](../frontend/index.html#L84)）。

---

## 6. 内置（Vendored）库

所有第三方代码均置于 `frontend/vendor/` 下。
**无 CDN**——安装完成后应用可完全离线运行。
许可证信息见 [`THIRD_PARTY_LICENSES.md`](../THIRD_PARTY_LICENSES.md)。

| 库 | 加载时机 | 用途 |
|---------|--------|---------|
| `alpine.min.js` | 启动时 | Alpine.js v3——响应式 UI 框架 |
| `marked.min.js` | 启动时 | Markdown 转 HTML 解析器 |
| `purify.min.js` | 启动时 | DOMPurify——在 marked 之后执行的 HTML 净化器 |
| `highlight-theme.css` / `highlight-theme-light.css` | 启动时 | highlight.js 主题 CSS（预加载以防止 FOUC 闪烁）|
| `highlight.min.js` + `hljs-langs/*.min.js` | 延迟加载 | 代码块语法高亮（~124 KB 核心 + 5 种额外语言）|
| `katex/katex.min.js` + 字体 + `auto-render.min.js` | 延迟加载 | 渲染 `$…$` / `$$…$$` / `\(…\)` / `\[…\]` 数学公式（含字体约 562 KB）|
| `mermaid.min.js` | 延迟加载 | 图表 / 流程图渲染器（~3.3 MB）|
| `cm/codemirror.min.js` + 语言模式 + 插件 | 延迟加载 | CodeMirror 5——预览面板编辑模式（含所有模式约 308 KB）|

---

## 7. Service worker：仅推送，不缓存

[`frontend/sw.js`](../frontend/sw.js)（94 行）刻意保持极简。

**不缓存**——service worker 刻意不缓存任何请求或资源
（[`sw.js:1–8`](../frontend/sw.js#L1)）。
静态资源已带 `?v=<mtime>` 版本戳，加入 stale-while-revalidate 层反而会在开发调试时造成困惑。

**安装 / 激活**——`skipWaiting()` + `clients.claim()`，使 service worker 立即激活并接管所有页面
（[`sw.js:10–15`](../frontend/sw.js#L10)）。

**Web Push 推送通知**是 service worker 唯一的功能。
它从 [`pushSubscribe()`](../frontend/app.js#L15455) 延迟注册，并从 `/sw.js`（而非 `/static/sw.js`）提供服务以覆盖整个源站作用域。
面向用户的推送设置请参阅 [mobile_zh.md](mobile_zh.md)。

**可见窗口抑制**——收到推送事件时，service worker 调用 `clients.matchAll()`，若同一设备上有任何 muselab 窗口的 `visibilityState === "visible"`，则丢弃该通知——用户已在应用内实时看到回复
（[`sw.js:47–60`](../frontend/sw.js#L47)）。

**过期 PWA 硬重载**——每次 `visibilitychange` 时，应用会请求 `/api/meta` 并将 `asset_version` 与嵌入 `<meta name="muselab-asset-version">` 中的值对比。若版本不同且当前无流式传输，则强制重载页面
（[`app.js:1685–1713`](../frontend/app.js#L1685)）。
这一机制处理了移动端 Safari 常见场景：恢复后台 PWA 标签页时内存中存有过期的 JavaScript。
