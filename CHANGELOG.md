# Changelog

All notable user-facing changes are recorded here. This project follows an Alpha release channel; a version is delivered only when its immutable candidate artifact has passed its release checks.

## 0.1.0a20 (Unreleased)

- 修复从侧栏进入开发者中心后一直停在“读取中”的问题，进入页面即读取真实数据；失败仍保留可重试提示。
- 安装包与研究插件版本统一标记为 a20。研究和模型执行语义、依赖锁及权重保持 a19 的既定方案。

## 0.1.0a19 (maintainer-main)

- 研究记录沿用Codex任务账本，新增来源、耗时、问题、固定证据与成果导出；普通用户与本机维护者分层查看。
- 证据增加真实PDF页码、字幕切片、结构化资料定位、物理容量准入与SHA绑定的历史部分资料迁移；不将旧摘要标为完整原件。
- 新增显式Obsidian Inbox导出，保留ZIP独立留存；重复内容复用，用户修改后的文件拒绝覆盖，不自动同步。
- 控制台收敛为显式模板和原生TypeScript模块，移除11份旧页面片段；Windows开发/正式入口按角色隔离并恢复断连。
- 本地转写统一为原生 Transformers Qwen3-ASR 0.6B，扫描件用 PaddleOCR-VL 1.6，Office 用无模型 Docling；移除 Whisper/1.7B 与重复云端链。三项组件按固定依赖锁显式隔离安装，共用一个计算名额，超时/取消回收进程树。
- 云端图片和音视频统一百炼 Qwen3.8-Omni-Flash；评论保留原文并精确去重，由 Codex 判断相关性。升级迁移模型配置，回滚保留用户后续改动。
- 清除已退役爬虫的无效依赖元数据，持续评测沿用已有账本并增量收集故障候选；这些改动尚未发布。

## 0.1.0a18

- 转写缓存失效时创建下一代任务，保留已完成历史并重新生成可验证正文。
- B站转写失败或未就绪会返回结构化状态，不再把提示文字当作转写成功。
- 持久任务结果排除自身阻塞引用；真实子任务仍阻塞时禁止假完成。

## 0.1.0a17

- 单账号恢复与业务调用始终绑定登记的浏览器资料目录，避免登录失败后改用旧目录扫码。
- 统一大小写登录与验证失败的账号事实映射。
- 扫码窗口先恢复再移动到屏幕内，避免恢复动作重新带回屏幕外位置。
- 人工登录状态跨进程保护：等待扫码时不因空闲、超时或调用进程退出关闭窗口，未验证的完成请求不能提前收尾。

## 0.1.0a16

- 小红书缺少临时访问参数时，跟随同账号搜索页的可见目标卡取得参数，避免将有效笔记拼成不可浏览链接。
- 内容要求 App 查看时保留人工处理状态，停止将单条内容限制记成全部账号掉线或付费穷尽。

## 0.1.0a15

- 知乎公开安装版使用已登录浏览器读取搜索结果，移除对未分发签名脚本的运行依赖；详情优先读取已登录页面的正文区域。
- 修复小红书页面就绪、身份端点失败和受保护链接恢复；账号状态未确认时继续尝试其他账号，但不授权付费兜底。
- 补充从已安装产品发现的业务路由回归，保留空详情和真实验证请求的失败状态。

## 0.1.0a14

- 建立长期持续评测台账、按变更重测和分层验收；注册成功与真实业务成功分别记录。
- 统一证据语义、结构化失败、详情 URL 校验和敏感查询隔离，防止空正文、摘要或历史状态被误报为验证成功。
- 增加可恢复的有界任务、deadline、取消与跨进程 CAS；小红书按任务逐账号尝试，通知 outbox 支持精确账号深链和去重。
- 调整独立账号卡页面与紧凑布局，修复页面路由及共享 Profile 存储统计。
- 冻结 Python 3.12 离线 wheel、SBOM 与安装制品身份；安装与回退校验同一归档并保护用户数据。
- 公开源码和安装包排除历史受限爬虫及知乎签名脚本；精确引入来源仍待核实，不表示历史许可已清零。


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
