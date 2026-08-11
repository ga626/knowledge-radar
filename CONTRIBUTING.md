# 贡献指南

感谢你帮助改进 KnowledgeRadar。项目的公开代码、模板和文档来自受控的公开源码投影；请不要把个人运行环境带入提交。

## 提交前

1. 从 `main` 新建清晰命名的分支，并说明改动解决的用户问题。
2. 不提交 `.env`、API Key、Token、Cookie、浏览器 profile、SQLite、日志、任务数据、媒体缓存或机器路径。
3. 改动公开边界、安装、打包、profile 或目录规则时，同时更新 `config/project-structure.manifest.json` 与 `config/public-source-manifest.json`。
4. 至少运行受影响的测试；提交前建议运行：

   ```powershell
   python -m ruff check src tests
   python -m pytest -q
   ```

5. 在 PR 中说明：改了什么、为什么、对用户有什么影响、如何验证，以及是否涉及本地隐私边界。

## 设计原则

- 默认本地优先：用户的凭据、账号和运行状态只留在用户机器。
- 默认安全降级：登录、验证码、额度或第三方平台限制必须如实报告。
- 不为“产品化”删除已有能力；把配置、边界与失败状态讲清楚。
- 新增平台或 provider 时，提供可验证的配置模板、最小验证方法和降级说明。

## 安全问题

请不要通过公开 Issue 提交漏洞、密钥或复现数据。参见 [SECURITY.md](SECURITY.md)。
