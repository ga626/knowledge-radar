---
name: research
description: 用 KnowledgeRadar 进行外部调研、事实核查、来源发现、最新信息检查、平台搜索和有证据的中文报告。
---

# KnowledgeRadar 调研

KnowledgeRadar 是 Codex 的外部研究感知层；用户级 `config.toml` 是唯一 MCP 注册源，本插件只提供工作流，不注册第二个 server。

## 工作流

1. 优先使用原生 `mcp__knowledgeradar.*`。未显示时，先发现 `knowledgeradar mcp health_check kr_research get_capabilities`；发现仍失败就报告 Codex 宿主工具面故障。原生明确 `Transport closed` 后，先用项目协议探针区分后端与当前 Desktop MCP session，不重启共享 KR 服务。若宿主暴露官方 `config/mcpServer/reload`，请求 reload，并仅在下一 user turn 的原生 `health_check(summary)` 与 `get_capabilities(summary=true)` 都成功后记录恢复；未暴露时，可提示用户完整退出并重新启动 Codex Desktop，再做同样的双调用验收。重启是宿主恢复选项，不是自动重连。只有任务必须在此之前继续时，才可调用版本化 `scripts\kr_mcp_continuity.py call`；输出必须标记 `access_path=continuity_fallback`，不得宣称原生 MCP 已恢复。
2. 非平凡或不确定任务先调用 `health_check(mode="summary")` 与 `get_capabilities(summary=true)`，再由 Agent 自主选择来源生态、轮次、工具、扩展和停止点。
3. 重度研究优先用 `kr_research`，后续按证据需要调用已注册的 Web、学术、GitHub、视频、平台、详情、任务状态和决策日志工具。
4. 内置 web/search 只能在 KR 路由后作为 `host_internal_web_wave` 使用，并记录 `wave_id`、`strategy_tree`、`reason` 和 `relationship_to_kr`。
5. 严肃报告记录证据登记：访问路径、工具、来源 URL/路径、事实、推理、强弱和缺口。搜索结果只是候选，需详情抽取或交叉来源确认后才支持强结论。
6. 一份报告对应一个 `research_task`，完成时使用同一任务 ID 调用 `finalize_research_task`；每个考虑过的来源生态都记录使用、跳过、阻断、不相关或未到达及原因。
7. 深度或治理报告须通过项目研究质量检查并完成最终化；sidecar 是声明，不等于每次工具调用的证明，关联不清时标为 partial。

## 失败边界

- 原生工具在发现后仍不可用：默认报告 Codex 宿主工具面故障；只有 L2 已明确断开且任务不能等待宿主恢复时，才使用带 `--reason`、任务 ID、回执和 handoff 的版本化 `continuity_fallback`。它只保证任务连续，不能让当前线程重新获得原生工具。用户完整重启 Desktop 后，必须重新发生两次真实 native call 才能写 `native_mcp_restored_after_desktop_restart`。不调用未版本化或缺失的 fallback。
- 缺少工具卡不等于 KnowledgeRadar 服务故障；区分宿主注入、stdio、HTTP 和源码配置。
- 登录、验证码和平台验证转为明确的人工交互状态，不盲目循环重试。
