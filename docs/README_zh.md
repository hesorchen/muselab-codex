# muselab-codex 文档

> [English](README.md) · [← 返回项目 README](../README.md)

muselab-codex 是围绕 `codex app-server` 构建的单用户、自托管工作台。下面的文档按“安装 → 使用 → 理解 → 运维”组织；若第一次接触项目，从[快速入门](quickstart_zh.md)开始。

## 安装与升级

- [快速入门](quickstart_zh.md) —— 三种运行方式、首次登录、健康检查和 WSL2 注意事项
- [Linux 安装](install-linux_zh.md) —— systemd 用户服务、日志、linger 与卸载
- [macOS 安装](install-macos_zh.md) —— launchd agent、PATH、日志与卸载
- [升级](upgrade_zh.md) —— 更新代码和依赖、核对 Codex CLI 基线、验证与回滚

## 使用与配置

- [配置参考](configuration_zh.md) —— `.env`、`CODEX_HOME`、`AGENTS.md`、Provider 与安全边界
- [Skills](skills_zh.md) —— Codex 原生发现、启停、作用域和自定义 Skill
- [定时任务](scheduler_zh.md) —— 保存 prompt、执行历史、时区和无人值守风险
- [移动端 PWA](mobile_zh.md) —— 加到主屏、HTTPS、通知与多设备使用
- [九位缪斯](muses_zh.md) —— 产品命名和会话入口的设计概念

## 架构与实现

- [架构](architecture_zh.md) —— 组件边界、请求链路、事件路由、持久化和安全边界
- [基础设施](infrastructure_zh.md) —— systemd、launchd、Docker、健康检查、CI 与发布门禁
- [原生实现规格](specs/) —— 每一阶段的 app-server 协议选择和验证记录
- [工具目录快照](tool-catalog.txt) —— 开发阶段记录的工具事件形状参考

## 运维

- [排错](troubleshooting_zh.md) —— 从 doctor 到日志、模型、浏览器缓存、MCP 和文件权限
- [数据与备份](data-and-backup_zh.md) —— 工作区、`.env`、`CODEX_HOME`、恢复演练和可丢弃数据
- [安全策略](../SECURITY.md) —— 威胁模型、部署基线和漏洞报告方式

## 项目协作

- [贡献指南](../CONTRIBUTING.md)
- [项目开发约束](../AGENTS.md)
- [第三方授权](../THIRD_PARTY_LICENSES.md)

## 文档约定

- 当前产品行为以代码、测试和 `codex app-server` 返回值为准；规格文档记录的是实现决策，不覆盖运行时事实。
- 面向用户的文档同时维护中文与英文版本。
- 示例只使用中性路径和占位数据；真实令牌、API Key、`CODEX_HOME` 与工作区内容不能进入公开制品。
