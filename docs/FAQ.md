# FAQ

Release 用户先看 [首次成功](FIRST_SUCCESS.md)。日常配置运行安装器生成的 `configure.cmd`；它打开只监听 `127.0.0.1` 的本地页面，密钥不会回显。源码 checkout 的 `scripts\…` 仅供开发者使用。

## `/mcp` returns 406 in a browser

That is normal for streamable HTTP MCP. Use an MCP client or Agent, not a normal browser page.

## `health_check` is degraded

先区分核心链路与可选能力。缺少可选 Provider、SearXNG、Chrome 或登录 Profile 是待配置状态，不等于核心服务失败。Release 用户在本地配置台补齐需要的项，按 Codex 支持方式刷新后调用 `health_check`；源码开发者才使用验证脚本。

## Xiaohongshu, BOSS, Liepin, or Maimai do not pass on first install

在你自己的浏览器 Profile 中完成交互式登录。验证码、账号风控和失效会话都需要你手动处理；产品不会代填 Cookie 或绕过验证。完成后重新调用对应工具；仍失败再提交脱敏诊断。

## SearXNG is not running

在本地配置台配置其他网页 Provider，或参阅 [SearXNG](SEARXNG.md)。

## Node.js or Chrome is missing

核心低风险工具仍可用；浏览器辅助能力需要本机 Chrome，部分兼容桥接需要 Node.js。安装或配置后重启/刷新 Codex，再做一次脱敏状态检查。

## C 盘空间变大怎么办

先在 [产品安装与更新](PRODUCT_INSTALL.md) 中生成数据迁移计划。计划会检查空间、浏览器锁和目标目录，并给出确认令牌；只有确认后才会复制、校验和切换。旧数据根会保留用于回滚，产品不会自动删除登录资料、模型或备份。
