---
name: research
description: 用 KnowledgeRadar 进行外部调研、事实核查、来源发现、最新信息检查、平台搜索和有证据的中文报告。
---

# KnowledgeRadar 调研

KnowledgeRadar 是 Codex 的外部研究感知层；用户级 `config.toml` 是唯一 MCP 注册源，本插件只提供工作流，不注册第二个 server。

## 工作流

1. 优先使用原生 `mcp__knowledgeradar.*`。未显示时，先发现 `knowledgeradar mcp health_check kr_research get_capabilities`；发现仍失败就记录“本任务原生工具面未附着”，而不是把服务健康误报为可用。原生明确 `Transport closed` 后，先用项目协议探针区分后端与当前 Desktop MCP session，不重启共享 KR 服务。若宿主暴露官方 `config/mcpServer/reload`，先以 `mark-host-refresh-pending` 记录请求，再在下一 user turn 只用真实原生 `health_check(summary)` 与 `get_capabilities(summary=true)` 两项共同验收；缺一项均不是恢复。若宿主未暴露刷新入口，完整退出并重新启动 Codex Desktop 是可重复的宿主恢复动作，但同样必须完成原生双调用验收。任务需要继续时，直接使用版本化 `scripts\kr_mcp_continuity.py call`；它绑定当前 `active.json` 安装身份，对只读就绪调用最多新建一次重试会话，并输出 `access_path=continuity_fallback`、artifact identity、工具目录 fingerprint 与 retry receipt。不得宣称原生 MCP 已恢复，也不得自动重放写入、账号或浏览器操作。
2. 非平凡或不确定任务先调用 `health_check(mode="summary")` 与 `get_capabilities(summary=true)`，再由 Agent 自主选择来源生态、轮次、工具、扩展和停止点。
3. 重度研究优先用 `kr_research`，后续按证据需要调用已注册的 Web、学术、GitHub、视频、平台、详情、任务状态和决策日志工具。
4. 内置 web/search 只能在 KR 路由后作为 `host_internal_web_wave` 使用，并记录 `wave_id`、`strategy_tree`、`reason` 和 `relationship_to_kr`。
5. 严肃报告记录证据登记：访问路径、工具、来源 URL/路径、事实、推理、强弱和缺口。搜索结果只是候选，需详情抽取或交叉来源确认后才支持强结论。
6. 一份报告对应一个 `research_task`，完成时使用同一任务 ID 调用 `finalize_research_task`；每个考虑过的来源生态都记录使用、跳过、阻断、不相关或未到达及原因。
7. 深度或治理报告须通过项目研究质量检查并完成最终化；sidecar 是声明，不等于每次工具调用的证明，关联不清时标为 partial。

## 同一研究记录与完整证据

- 用户只需在Codex对话里提出目标。Agent直接调用kr_research和普通MCP工具，沿返回的research_task_id记录回执，仍由finalize_research_task完成检查；不要求网页建单、交接、认领、租约或逐次预留。
- 当前工具 schema 支持 `display_title` 时，由 Codex 在首次调用 `kr_research` 时按用户目标提供 12 至 24 字中文短标题，保留必要产品名，不塞入工具名、任务 ID 和执行限制；详细范围仍写 `task`。续查复用同一任务 ID 和原名称，独立交付问题新建任务。旧 schema 不传此参数，由控制台从原始目标提取标题；不额外调用模型或要求用户配置命名 API。
- 控制台“研究记录”按任务回顾成果与调用；已保存原文从对应任务的成果进入回读。页面不是开展调研的前置步骤，也不自动同步到 Obsidian。
- 详情返回evidence_assets时，按version_id固定回读：get_task_status(research_action="evidence_fetch",research_payload={"version_id":"...","start":0,"max_chars":8000})；next_start不为空时可继续读取。evidence_search按query搜索已保存资产，list/inspect只读同一研究账本。仅在当前工具schema支持这些操作时使用。
- 资产SHA和unicode_code_point定位用于复核原文；已保存不等于覆盖完整，旧摘要不能恢复已经丢失正文。未记费用保持未知，账号、付费和媒体权限仍按既有边界执行。
- 支持当前 schema 时，普通搜索自动登记候选；详情按唯一来源身份关联候选。同一任务显式传 `research_task_id`，不依靠全局“当前任务”猜测归属。已结案任务出现新回执后必须重新检查完成条件。
- 用户明确提供本地文件并需要可选解析时，调用 `process_local_evidence`，传文件、允许根目录、SHA、组件和任务 ID；之后以返回的后台任务 ID 取回结果。未安装并批准的隔离组件明确拒绝，不自动下载模型。语音首选 Qwen3-ASR-1.7B 原生 Transformers，0.6B 为显式资源备选；时间定位目前是实际输入切片范围，不能称为词级对齐。
- 任务成果支持选择固定证据和报告后下载 ZIP，或显式保存到用户指定的 Obsidian Vault 专用 Inbox；不扫描笔记、不覆盖既有文件、不自动同步。没有 Obsidian 或 MensLink 也能保存、回读和导出证据。

## 失败边界

- 原生工具在发现后仍不可用：带 `--reason`、任务 ID、回执和 handoff 的版本化 `continuity_fallback` 是正式连续性路径；它只保证任务连续，不能让当前线程重新获得原生工具。用户完整重启 Desktop 后，只有在同一 artifact 上重新发生 `health_check` 与 `get_capabilities` 两次真实 native call，才能写 `native_mcp_restored_after_desktop_restart`。不调用未版本化、身份不明或工具目录不完整的 fallback。
- 缺少工具卡不等于 KnowledgeRadar 服务故障；区分宿主注入、stdio、HTTP 和源码配置。
- 登录、验证码和平台验证转为明确的人工交互状态，不盲目循环重试。
