# 排错

> [English](troubleshooting.md) · [← 文档索引](README_zh.md)

先运行：

```bash
bash scripts/doctor.sh
```

doctor 不输出凭证值，会按 runtime、Codex 登录、`.env`、依赖、服务管理器和 HTTP 逐层检查。下面按症状继续定位。

## 服务无法访问

### 连接被拒绝

```bash
curl -v http://127.0.0.1:8765/api/health
systemctl --user status muselab             # Linux
journalctl --user -u muselab -n 100         # Linux
launchctl print gui/$(id -u)/com.muselab    # macOS
```

常见原因：服务未启动、端口改变、`.env` 校验失败、Codex CLI 不在服务 PATH，或端口已被另一进程占用。

查端口：

```bash
ss -ltnp 'sport = :8765'                    # Linux
lsof -nP -iTCP:8765 -sTCP:LISTEN            # macOS
```

不要同时手工启动和运行用户服务；这会让浏览器连到旧进程，出现“代码已经更新但接口仍 404”。

### 健康检查一直是 starting

`status: "starting"` 表示 FastAPI 已响应，但 app-server 还未 ready。检查：

```bash
codex --version
codex login status
journalctl --user -u muselab -f
```

若 Codex CLI 更新过，确认版本与 `scripts/versions.env` 基线兼容。

## 登录后所有 API 返回 401

浏览器本地保存的 token 与当前 `.env` 不一致。确认你使用的是正在运行服务所读取的 `.env`，然后在登录页重新输入。

脚本调用使用：

```bash
curl -H "X-Auth-Token: <token>" http://127.0.0.1:8765/api/meta
```

不要使用 `Authorization: Bearer`，也不要把真实 token 放进 shell history、截图或 issue。

## Codex 会话错误

### `turn/start failed ... -32600`

通常表示对一个尚未在当前 app-server generation 中加载的 thread 启动 turn。当前实现会在进程重启后先执行一次 `thread/resume`。如果仍出现：

1. 确认服务已经运行最新代码；
2. 查看 app-server 是否在 turn 前发生过重启；
3. 新建 thread 判断是否只影响旧历史；
4. 保存脱敏错误码和版本信息，避免提交 prompt 或 transcript。

### thread 历史过大或读取超时

大型 transcript 读取受 `MUSELAB_CODEX_HISTORY_READ_TIMEOUT_SECONDS` 限制。优先 compact 或创建摘要 thread；只有确认磁盘和 Codex 历史读取正常后才提高超时。

### SSE 中断但模型仍在运行

浏览器断线不会自动取消 Codex turn。重新打开 thread 会尝试附着或回放当前进程内事件。需要真正停止时使用停止按钮，它会发送 interrupt。

## 模型列表与 Provider

### “模型”为空

1. 强制刷新浏览器，排除旧静态资源；
2. 查询原生接口：

   ```bash
   curl -H "X-Auth-Token: <token>" \
     http://127.0.0.1:8765/api/settings/providers
   ```

3. 若返回 404，端口上通常仍是旧服务；重启受管理的服务；
4. 服务刚启动时 app-server 配置读取可能短暂变慢，前端会自动重试。

正常列表包含 `minimax`、`qwen`、`mimo`。

### Provider 已启用但请求鉴权失败

确认 key 在服务进程环境中，而不仅是当前交互式 shell：

- systemd 读取仓库 `.env`；
- launchd 由 plist 启动应用，应用再读取仓库 `.env`；
- Docker 通过 compose `env_file` 注入。

修改后重启。网页开关只写 Provider 定义，不保存 key。

### 模型存在但 Web Search 不工作

MiniMax、Qwen 和 MiMo 当前会显式关闭 Codex Web Search，以保持 Responses 兼容。文件、终端、Skills 和 MCP 工具仍可用。这是已知能力边界，不是密钥故障。

## MCP 与 Skills

### MCP server 不出现或没有工具

- 在设置页执行刷新；
- 检查 app-server 返回的 enabled／auth 状态；
- STDIO server 必须能在服务用户 PATH 中启动；
- remote server 的 OAuth 需要浏览器可访问 authorization URL；
- 查看服务日志，但不要输出 bearer token 或完整环境。

### 新 Skill 没出现

把 Skill 放到 Codex 原生发现位置，如 `$CODEX_HOME/skills/` 或工作区 `.codex/skills/`，然后重新打开 Skills 抽屉触发 `forceReload`。仓库根目录不再内置另一套 Skills 目录。

## 文件与附件

### 文件 API 返回 400／403

检查路径是否：

- 逃逸 `MUSELAB_ROOT`；
- 通过符号链接指向工作区外；
- 命中敏感文件屏蔽；
- 指向不支持或过大的预览类型。

### 附件上传失败

单个附件默认上限 10 MiB，文本附件上限 200 KiB，一次最多 8 个。文本必须是 UTF-8。附件在发送前位于 `.muselab-codex/attachments/staged/`，发送时才归属具体 thread。

## 浏览器与 PWA

### 页面仍是旧版本

先强制刷新。前端通过 `/api/meta.asset_version` 检测新静态资源，但长期后台标签页或 PWA 仍可能保留旧页面。必要时关闭所有标签页后重新打开。

### 手机端不能安装或收不到通知

iOS 和多数移动浏览器要求 HTTPS。先通过受控 HTTPS 地址访问并加到主屏，再开启通知。查看[移动端 PWA](mobile_zh.md)。

## 提交问题前

请提供：

- Git revision；
- `codex --version`；
- 操作系统和安装方式；
- `/api/health` 的非敏感输出；
- 最小复现步骤和错误码。

不要提供 `.env`、token、API Key、OAuth 文件、完整 prompt、transcript、工作区文件或未经清理的日志。
