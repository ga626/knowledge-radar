# 能力包与本地健康台

KnowledgeRadar 不会要求你在第一次安装时配置所有服务。核心网页研究、学术资料、视频理解和需要登录的平台是相互独立的能力包；未配置的能力会显示为“待按需配置”，不会把核心能力标记为故障。

## 两类配置能力与两类按需下载

| 能力包 | 何时配置 | 本机保存内容 | 重要边界 |
| --- | --- | --- | --- |
| 核心网页研究 | 希望搜索或提取公开网页时 | 你选择的搜索服务配置 | 至少填写一个搜索来源；额度、网络与来源可访问性仍会影响结果。 |
| 学术资料 | 希望扩展学术元数据来源时 | 可选 API 配置 | 机构授权、付费墙和 provider 额度不会被绕过。 |
| 视频与多模态理解 | 需要视频、图片或模型理解时 | 可选模型配置、按需媒体缓存 | 调用可能计费；媒体缓存只在本机。 |
| 登录平台与招聘 | 实际访问需要登录的平台时 | 你的浏览器 Profile 与本地状态 | 验证码、风控或平台策略会要求你手动完成操作，或返回明确降级。 |

下面两项不属于首次安装，也不会因打开控制台而下载。它们在控制台中都必须经过“生成计划 → 第二次确认安装”：

| 按需能力 | 下载到哪里 | 何时需要 | 不会做什么 |
| --- | --- | --- | --- |
| Playwright Chromium | 当前产品数据根的 `playwright/` | 动态网页或受支持的浏览器流程 | 不自动登录、不调用付费 API。 |
| 小红书诊断 bridge | 当前产品数据根的 `capabilities/xhs-bridge/` | 需要本地 bridge 诊断时 | 不绕过验证码、不自动变成生产兜底；完成后需刷新或重启 Codex。 |

仍可手动使用安装器：先运行 `capability-plan`，确认网络、登录、费用和重启边界，再将本次返回的令牌传给 `capability-apply`。令牌只对当前版本与数据根的当前计划有效。

```bat
python scripts\product_install.py capability-plan --capability browser
python scripts\product_install.py capability-apply --capability browser --confirmation <plan 中的 confirmation_token>
python scripts\product_install.py capability-plan --capability xhs_bridge
python scripts\product_install.py capability-apply --capability xhs_bridge --confirmation <plan 中的 confirmation_token>
```

## 本地健康台

Release 用户运行 `%LOCALAPPDATA%\KnowledgeRadar\configure.cmd` 后，浏览器中的本地页面会显示：

- 哪些能力包已具备最小配置；
- 哪些浏览器或 bridge 依赖已经按需安装，并在二次确认后安装它们；
- 产品数据根的分类空间摘要；
- 只针对已过期媒体缓存的可恢复隔离操作。

控制台还可以生成不含路径、文件名、账号或配置值的数据根迁移计划；真正迁移仍必须在命令行执行带令牌的 `data-move-apply`。脱敏诊断可在本机查看或导出，适合在提交 Issue 前人工核对；不要上传任何 Profile、Cookie、日志或原始配置文件。

页面只监听 `127.0.0.1`。密钥不会回显，空间摘要不显示文件名、路径或内容。隔离操作要求同源会话 token，且只会把 manifest 已登记且过期的媒体缓存移入可恢复隔离区；它**不会**删除 API Key、浏览器 Profile、Cookie、日志、模型或未知文件。

## 更新与排错

- 更新程序不会改写产品数据根；你已填写的配置和已登录的资料会保留。
- 若新版本无法运行，使用 `python scripts\product_install.py rollback` 恢复上一个 active 版本；不会删除数据。
- 安装后需要重启或按 Codex 支持的方式刷新，令 Codex 重新读取唯一的 MCP 配置块。详见 [产品安装与更新](PRODUCT_INSTALL.md)。
- `health_check` 与 `get_capabilities` 返回的是运行时的脱敏状态；“需要人工操作”或“预期降级”不等于核心服务崩溃。
