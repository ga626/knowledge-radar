# KnowledgeRadar

[简体中文](README.md) | [English](README.en.md)

**把网页、学术资料、视频与中文平台搜索能力统一接入本地 Agent 的 MCP 感知层。**

KnowledgeRadar 是一个 Windows 优先的本地 MCP 服务。它让 Codex 等 Agent 通过一组统一工具完成网页搜索与提取、学术元数据检索、Bilibili、小红书、知乎、招聘信息与运行状态检查；所有 API Key、登录态、浏览器资料、任务数据库和日志都只保留在你的电脑上。

> 当前为 `0.1.0` beta。各平台登录、验证码、额度和第三方 API 的可用性由你的本地账号与服务商决定；KnowledgeRadar 会报告需要人工操作或预期降级的边界，不会伪装成已成功。

## 适合谁

- 你在 Windows 上使用 Codex、其他 MCP 客户端或本地 Agent，需要一层可审计的搜索与感知能力。
- 你希望自己填写 API Key、完成必要的平台登录，并让资料留在本机。
- 你需要把一般网页、学术、中文内容平台和招聘来源放到同一 MCP 工作流中。

不适合：你需要云端托管服务、绕过验证码/付费墙，或希望项目代替你管理第三方账号与额度。

## 最快开始

1. 安装 Python 3.12，或使用本地已有的 `.python312` 运行时。
2. 将 `.env.example` 复制为仓库根目录的 `.env`，只填写你准备启用的 provider。
3. 双击 `scripts\\install.bat`，再运行 `start.cmd`。
4. 在 Agent 中注册 `http://127.0.0.1:18765/mcp`，或参考 `config\\mcp-config-template.json`。
5. 执行安全验证：

   ```bat
   .python312\\python.exe scripts\\verify_api_keys.py
   .python312\\python.exe scripts\\verify_all_capabilities.py --safe
   ```

详细步骤见 [安装与首用](docs/INSTALL.md)、[API 与配置](docs/API_KEYS.md) 和 [MCP 接入](docs/MCP_SETUP.md)。

## 能力与边界

| 方向 | 提供的能力 | 重要边界 |
| --- | --- | --- |
| 网页与研究 | 搜索、网页提取、研究路线、任务状态与健康检查 | 搜索结果不是事实结论；重要结论需要回到来源核验。 |
| 中文内容平台 | Bilibili、小红书、知乎及招聘信息的搜索/详情能力 | 登录、验证码、反爬和平台策略会要求人工操作或触发降级。 |
| 学术资料 | 多来源学术元数据与开放获取线索 | 授权、额度和机构访问权仍由使用者负责。 |
| 本地运行 | HTTP / stdio MCP、可读状态摘要与验证脚本 | 不上传你的 `.env`、Cookie、浏览器 profile、数据库或日志。 |

完整公开工具面以 `src/server.py` 中的 `@mcp.tool()` 注册为准；`get_capabilities` 与 `health_check` 会返回当前机器实际可用情况。

## 隐私：会提交什么，不会提交什么

会提交：源码、测试、公开配置模板、安装脚本和公开文档。

绝不会提交：真实 API Key、Token、Cookie、账户/浏览器 profile、SQLite 数据库、运行日志、任务记录、媒体缓存和本机路径。请只在仓库根目录 `.env` 中保存私有值；它已被 Git 忽略。详见 [隐私与本地状态](docs/PRIVACY_AND_LOCAL_STATE.md)。

## 参与和安全

- 想提交改进：阅读 [贡献指南](CONTRIBUTING.md)。
- 发现安全或隐私问题：不要公开 Issue，按 [安全政策](SECURITY.md) 私下报告。
- 使用问题和功能建议将通过 GitHub Issue 模板收集。

## 开源许可

本项目采用 [MIT License](LICENSE)。使用任何第三方平台能力前，请同时遵守该平台的条款、robots 规则及适用法律。
