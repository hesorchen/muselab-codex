# 升级

> [English](upgrade.md) · [← 文档索引](README_zh.md)

升级分为两类：普通应用更新，以及提升 Codex CLI 协议基线。前者是日常运维；后者需要协议验证，不能只执行 npm update。

## 升级前

1. 备份 `MUSELAB_ROOT`、仓库 `.env` 和 `CODEX_HOME`；
2. 确认工作区和 Codex 登录态可恢复；
3. 记录当前 Git revision 与 `codex --version`；
4. 确认仓库没有需要保留但未提交的代码修改。

## 普通应用更新

```bash
git pull --ff-only
bash scripts/upgrade.sh
```

脚本会：

- 检查已安装 Codex CLI；
- 执行 `uv lock` 和 `uv sync --frozen`；
- 运行 pytest、Ruff、项目 lint 和前端语法检查；
- 输出 Linux／macOS 重启命令。

脚本不会自动提交、修改工作区文件或重启服务。

验证通过后：

```bash
systemctl --user restart muselab                 # Linux
launchctl kickstart -k gui/$(id -u)/com.muselab  # macOS
bash scripts/doctor.sh
```

再确认 `/api/health`、新建 thread、历史读取和一次文件工具调用。

## Codex CLI 基线升级

当 `scripts/versions.env` 中的版本变化时，还需要：

1. 使用目标 CLI 生成稳定 app-server schema；
2. 记录版本和 schema digest；
3. 对照变更更新 fake app-server fixture 与协议测试；
4. 在临时工作区执行 live thread、turn、工具、审批、Skills 和 MCP 检查；
5. 同步 Docker build arg、安装测试和架构文档。

不要默认启用 experimental API。只有明确需求、协议测试和 fallback 同时存在时才使用。

## 回滚

应用代码回滚：

```bash
git switch --detach <known-good-revision>
uv sync --frozen
systemctl --user restart muselab                 # Linux
```

不要使用会丢失本地工作的 `git reset --hard`。若升级修改了 Codex CLI，也要恢复与该 revision 匹配的版本。

回滚后运行 doctor，并检查旧 thread 是否能 resume。Codex 原生历史和工作区不应依赖 Python 虚拟环境，因此正常回滚不需要迁移用户文件。

## Docker 更新

```bash
git pull --ff-only
docker compose build --pull
docker compose up -d
docker compose ps
```

确认工作区和 `CODEX_HOME` 仍挂载到原卷。删除旧容器前不要删除宿主数据目录。

## 数据兼容

muselab-codex 没有应用数据库 schema。需要重点保护的是：

- Codex 管理的 thread／rollout；
- workspace 附件路径；
- `.muselab-codex/usage` 数值快照；
- Provider、Skills 和 MCP 的 Codex 配置。

完整恢复边界见[数据与备份](data-and-backup_zh.md)。
