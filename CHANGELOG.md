# Changelog

All notable user-facing changes are recorded here. This project follows an Alpha release channel; a version is delivered only when its immutable candidate artifact has passed its release checks.

## Unreleased

## 0.1.0a13

- 限定本地转写的 PyAV 兼容范围，避免可导入但无法解码音频的运行时被安装或随升级保留。

- 修复网站与 MCP 跨进程会话缓存：账号事件随磁盘更新刷新，写入使用进程锁和原子替换；关闭未验证窗口仍保留恢复待办。

- 小红书搜索和详情统一使用任务内账号执行器：当前账号优先、每个允许账号最多尝试一次、切号后重跑完整流程；公共组件错误不会污染账号或授权付费兜底。自动付费需要本任务的穷尽回执，旧健康快照不能替代它。
- 账号失效由同一浏览器会话事件驱动桌面通知和控制台待办；小红书显示 A/B/C、登记全名和登记编号。恢复按钮先运行指定账号探针，再恢复浏览器普通生命周期；关闭窗口不会宣称登录成功。
- 修复小红书正文读取在页面导航中丢失执行上下文的问题，以及猎聘详情脚本的正则转义和跨上下文变量引用。
- 微信搜索排除入口页；学术搜索取消固定样本补位、按查询相关性决定后续来源与真实空结果；本地限频不会再冒充验证码。
- 产品组件检查改查实际程序文件，统一 FFmpeg 解析与 PubScholar Chromium 模式；升级恢复用户已选择的浏览器、下载器和转写运行时，不自动新增模型下载。
- 状态摘要不再接受过期缓存；当前响应进程与共享历史进程记录分别显示；产品健康不要求携带开发测试文件。
- 城市表缺失保留失败记录，不再误触发平台风险冷却或耗尽账号失败预算；既有配置错误冷却可由新分类自动忽略，真实平台风控仍受保护。
- 修复小红书原生页面 CDP 搜索被可选 Scrapling 导入提前阻断的问题；真正使用 DynamicFetcher 的路径仍明确报告依赖缺失。
- 公开并打包已有招聘城市参数表，恢复 BOSS、猎聘等平台的城市过滤；未知城市仍拒绝猜测代码。
- 浏览器登记的相对 Profile 路径按产品数据根解析，避免升级后读取程序目录中的空账号。

## 0.1.0a11

- Strengthen the Codex MCP continuity path without registering a second MCP server: the fallback now refuses an incomplete or unexpected tool catalog, binds its receipt to the selected active artifact, and records the catalog fingerprint and call attempts.
- Retry only the read-only readiness checks (`health_check` and `get_capabilities`) once in a fresh fallback process after a transient transport failure; research, account, browser, and other potentially stateful operations are never replayed automatically.
- Make native-recovery acceptance explicit: a host refresh is recorded as pending, and recovery is confirmed only after both real native checks succeed against the same installed artifact in the next Codex turn.

- Keep the local control console at one fixed loopback address (`127.0.0.1:18882`): reuse a known running instance, refuse foreign port conflicts, and create a visible per-user startup entry so the console returns after Windows sign-in. The active product version remains the only data/configuration authority.
- Deliver the redesigned workspace console with a research dashboard, capability status, local components, data/recovery, and settings/help views instead of the original single long configuration form.
- Add explicit local maintenance plans to the control console: path-free data-root migration plans, copyable diagnostic exports, and protected retention guidance.
- Make Playwright Chromium and the Xiaohongshu diagnostic bridge opt-in capability downloads.  Each requires a fresh plan and a second confirmation; neither logs in, calls paid APIs, or silently enables a production fallback.
- Keep optional browser/Node dependencies under the active data root and preserve the single active MCP identity after an explicit capability installation.
- Make the local configuration console fast on large data roots: first open now reads only sanitized installation/capability state, and category storage scanning is user-initiated.
- Keep the redistributable ZIP focused on product operation: retain the product installer, plugin, local console, runtime, and user help; exclude source-only installers, launchers, test/verification utilities, and development setup scripts.
- Clarify the Windows/Codex support boundary, optional-provider cost/login behavior, and safe data-root migration in Chinese-first user guidance.
- Add safe product data-root relocation: plan, explicit confirmation, browser-lock refusal, copy-and-SHA-256 verification, active MCP switch, and rollback while preserving the old data root.
- Keep browser state, Profile registry, media cache, and product runtime under the active product data root; add a public storage-ownership policy and reversible quarantine for manifest-known expired media cache.
- Repair Windows stdio verifier cleanup so it waits for the child process and closes all stdio handles before removing its temporary state.
- Repair the Release first-use contract: one stable `configure.cmd`, public stdio verification, data-root diagnostics, and separated user/developer documentation.
- Improve the repository entry points, support guidance, contributor conduct, and bilingual README accuracy.

## 0.1.0a4

- First public Alpha Release with a local product installer, versioned app/runtime, separate user data root, rollback, and Codex MCP registration.
