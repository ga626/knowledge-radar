"""Agent-facing KnowledgeRadar capability descriptors."""

from __future__ import annotations

from typing import Any, Callable, Dict

from kr_core import PlatformRegistry, registry
from kr_core.strategy import ERROR_TAXONOMY, generic_web_strategy_tree
from capability_manifest import manifest_summary
from mcp_tools import facade_manifest
from runtime.architecture_standard import architecture_standard_summary
from runtime.knowledge_assets import knowledge_asset_schema_summary
from runtime.media_cache import DEFAULT_TTL_SECONDS, media_cache_root
from runtime.openclaw_native_adapter import openclaw_native_adapter_summary
from runtime.asr_lifecycle import asr_lifecycle_summary
from runtime.asr_policy import AsrPolicy
from runtime.asr_strategy import asr_strategy_manifest
from runtime.dependency_preflight import external_dependency_preflight_summary
from runtime.leases import runtime_lease_summary
from runtime.resource_concurrency import resource_concurrency_summary
from runtime.runtime_environment import planning_tools_manifest, runtime_environment_manifest
from runtime.project_state import project_governance_manifest
from runtime.status_schema import canonical_status_counts, validation_status_classes
from runtime.task_scope import SERVER_RUN_ID
from media_policy import MediaModelPolicy
from strategy_trees.registry import (
    build_strategy_tree_bundle,
    cache_registry_manifest,
    governance_registry_manifest,
    strategy_tree_summary,
    validate_strategy_tree_bundle,
)

ProviderStatus = Callable[[], Dict[str, Any]]

PLANNING_MCP_TOOLS = [
    "expand_keywords",
    "plan_research",
]

BASE_MCP_TOOLS = [
    "kr_research",
    "finalize_research_task",
    "analyze_decision_logs",
    "get_task_status",
    "kr_web_search",
    "search_github_repositories",
    "search_youtube",
    "search_wechat_articles",
    "search_academic",
    "extract_web_page",
    "extract_dynamic_page",
    "search_bilibili",
    "search_xiaohongshu",
    "search_zhihu",
    "search_recruitment",
    "get_capabilities",
    "health_check",
    "get_content_detail",
    "manage_xiaohongshu_accounts",
    "record_research_candidates_tool",
    "advance_research_candidate",
    "review_research_progress",
]


def actual_mcp_tools() -> list[str]:
    """Return only tools actually registered by ``server.mcp``.

    ``expand_keywords`` and ``plan_research`` remain local compatibility
    helpers.  They are deliberately not advertised here because neither has
    an ``@mcp.tool()`` registration in ``server.py``.
    """
    return list(BASE_MCP_TOOLS)


ACTUAL_MCP_TOOLS = actual_mcp_tools()


def validation_semantics_manifest(web_provider_status: Dict[str, Any], academic_status: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """Describe which degraded states are product boundaries instead of blockers."""
    optional_web = sorted(
        name
        for name, item in web_provider_status.items()
        if isinstance(item, dict) and item.get("degraded_ok")
    )
    exhausted_academic = sorted(
        name
        for name, item in (academic_status or {}).items()
        if isinstance(item, dict) and (item.get("daily_exhausted") or item.get("monthly_exhausted"))
    )
    academic_by_tier: dict[str, list[str]] = {}
    academic_by_status: dict[str, list[str]] = {}
    for name, item in (academic_status or {}).items():
        if not isinstance(item, dict):
            continue
        tier = str(item.get("provider_tier") or "unknown")
        academic_by_tier.setdefault(tier, []).append(name)
        status_class = str(item.get("validation_status") or item.get("status_class") or item.get("status") or "")
        academic_by_status.setdefault(status_class, []).append(name)
    default_chinese_fulltext = _academic_profile_role_ids(academic_status or {}, "default_chinese_fulltext")
    return {
        "schema": "knowledgeradar-validation-semantics/v1",
        "status_classes": validation_status_classes(),
        "status_order": ["PASS", "EXPECTED_DEGRADED", "NEEDS_INTERACTION", "FAIL"],
        "blocking_statuses": ["FAIL", "NEEDS_INTERACTION"],
        "non_blocking_statuses": ["PASS", "EXPECTED_DEGRADED"],
        "overall_pass_rule": "Only FAIL and NEEDS_INTERACTION block. EXPECTED_DEGRADED is a declared boundary, optional provider state, quota state, or designed fallback and does not fail the run.",
        "required_main_chain_examples": [
            "health_check(summary)",
            "get_capabilities(summary=true)",
            "kr_web_search(provider='auto')",
            "search_academic(provider='auto' or free configured providers)",
            "get_content_detail on supported platform URLs",
        ],
        "optional_expected_degraded": {
            "web_search_providers": optional_web,
            "academic_quota_exhausted": exhausted_academic,
            "academic_expected_degraded": sorted(academic_by_status.get("EXPECTED_DEGRADED", [])),
            "academic_login_or_entitlement_boundaries": sorted(
                name
                for name, item in (academic_status or {}).items()
                if isinstance(item, dict)
                and (item.get("requires_login") or item.get("requires_api_key") or str(item.get("provider_tier") or "").startswith("expected_degraded"))
                and str(item.get("validation_status") or item.get("status_class") or "") == "EXPECTED_DEGRADED"
            ),
            "legacy_planning_tools": ["expand_keywords", "plan_research"],
            "platform_candidates": ["xiaohongshu_detail_when_body_text_chain_is_not_admitted", "maimai_browser_when_login_or_security_gate_blocks"],
        },
        "academic_provider_matrix": {
            "schema": "knowledgeradar-academic-provider-validation/v1",
            "profile_schema": "knowledgeradar-academic-provider-profiles/v1",
            "status_counts": canonical_status_counts((academic_status or {}).values()),
            "by_validation_status": {key: sorted(value) for key, value in academic_by_status.items()},
            "by_tier": {key: sorted(value) for key, value in academic_by_tier.items()},
            "default_chinese_fulltext": default_chinese_fulltext,
            "p2_4_default_chinese_fulltext": default_chinese_fulltext,
            "expected_degraded_boundaries": ["baidu_scholar when key/entitlement/quota is missing", "wanfang automatic full-text provider is not registered", "cnki_authorized_browser requires explicit user-authorized workflow"],
        },
        "designed_fallbacks": {
            "bilibili_raw_cdn_native_media": "provider-side direct download is blocked by anti-hotlinking; this is not a failure when derived_text/ASR/sample-frame paths are available",
            "native_media_requires_provider_downloadable_url": "native_media is valid only when direct_media_probe.provider_downloadability.allow_native_media=true; provider-blocked URLs must use derived_text or sampled_media_with_text",
            "xiaohongshu_image_ocr_background": "Xiaohongshu OCR may exceed the sync window; scoped TaskStore fan-in plus get_task_status is the expected completion path",
            "semantic_scholar_without_key": "public unauthenticated API is valid but lower-rate; API key is optional for higher quota",
            "searxng_local": "self-hosted/local SearXNG is optional; absent daemon is expected degraded if Tavily/other auto providers pass",
            "baidu_qianfan_scholar": "official API only; unavailable endpoint/key/quota is expected degraded until the user provisions a working entitlement",
        },
        "bilibili_multimodal_acceptance": {
            "native_video_url_provider_blocked": "EXPECTED_DEGRADED",
            "main_acceptance": "PASS when subtitle, local ASR, transcript cache, or sample-frame fallback completes.",
            "failure_boundary": "FAIL only when every declared derived_text/sample-frame fallback path is unavailable or the task crashes unexpectedly.",
        },
    }


def _academic_profile_role_ids(academic_status: Dict[str, Any], role: str) -> list[str]:
    ids: list[str] = []
    for name, item in academic_status.items():
        if not isinstance(item, dict):
            continue
        profile = item.get("capability_profile") if isinstance(item.get("capability_profile"), dict) else {}
        if profile and profile.get("role") == role:
            ids.append(name)
            continue
        if not profile and role == "default_chinese_fulltext" and item.get("provider_tier") == "p2_4_default_chinese_fulltext":
            ids.append(name)
    return ids


def build_tool_surface() -> Dict[str, Any]:
    """Return the versioned MCP tool surface exposed by server.py."""
    tools = actual_mcp_tools()
    planning = planning_tools_manifest()
    return {
        "schema": "knowledgeradar-tool-surface/v2",
        "source_of_truth": "src/server.py @mcp.tool() registrations",
        "actual_mcp_tool_count": len(tools),
        "actual_mcp_tools": tools,
        "planning_tools": planning,
        "legacy_hidden_tools": list(PLANNING_MCP_TOOLS),
        "virtual_capabilities": [],
        "workflow_tools": {
            "kr_research": {
                "schema": "knowledgeradar-workflow-tool/v1",
                "role": "high_level_research_perception_entry",
                "modes": ["plan_only", "first_wave", "deep_route"],
                "relationship_to_low_level_tools": "Returns a route plan and optional first-wave evidence while leaving detailed follow-up tool choices to the Agent.",
                "side_effect_boundary": "plan_only is local-only; first_wave uses low-risk search tools; deep_route is a planning envelope and does not auto-run browser/login/media-heavy detail.",
                "host_internal_web_policy": {
                    "schema": "knowledgeradar-host-internal-web-policy/v1",
                    "principle": "Built-in host web/search is a finger inside the KnowledgeRadar hand, not an independent parallel path.",
                    "admission": "Call kr_research or otherwise obtain a KR web_search route plan before using built-in web/search for KR-eligible research.",
                    "wave_id": "host_internal_web_wave",
                    "strategy_tree": "web_search.provider_wave",
                    "required_trace_fields": ["wave_id", "strategy_tree", "relationship_to_kr", "reason"],
                },
            }
        },
        "deprecated_provider_aliases": {
            "kr_web_search(provider='youtube')": "search_youtube",
            "kr_web_search(provider='github')": "search_github_repositories",
        },
        "notes": [
            "Actual MCP tools are the functions registered with @mcp.tool() in src/server.py.",
            "kr_research is the high-level workflow entry for heavy research/perception tasks; low-level tools remain available for model-directed follow-up.",
            "YouTube search is exposed as the separate search_youtube MCP tool.",
            "GitHub repository search is exposed as the separate search_github_repositories MCP tool.",
            "WeChat public article search is exposed as the separate search_wechat_articles MCP tool and uses open Web index query templates.",
            "Source-ecology guidance is exposed by source_ecologies; MCP tool descriptions remain short execution contracts.",
            "kr_web_search(provider='youtube'/'github') remains only as a deprecated compatibility alias.",
            "The historic 12/13 tool-count wording is superseded by this versioned surface.",
            "expand_keywords and plan_research are legacy local helpers, not MCP registrations; Agents plan natively or use the registered research tools.",
        ],
    }


def source_ecology_manifest() -> Dict[str, Any]:
    """Describe searchable source ecologies without forcing a single tool route."""
    return {
        "schema": "knowledgeradar-source-ecologies/v1",
        "status": "implemented_p1_lightweight",
        "principles": [
            "Describe what each source ecology can reveal; do not reduce a platform to one domain label.",
            "Treat tool paths as candidates. The Agent should choose and cross-check based on the research question.",
            "Use open-ended examples to orient planning while leaving room for unexpected high-value sources.",
            "Distinguish discovery value from evidence strength; search hits are candidates until detail or cross-source validation.",
        ],
        "ecologies": {
            "wechat_public_article_ecology": {
                "label": "微信公众号公开文章生态",
                "source_type": "public_article_stream",
                "candidate_tools": ["search_wechat_articles", "kr_web_search", "extract_web_page", "extract_dynamic_page"],
                "content_forms": ["公众号文章", "机构推送", "作者专栏", "行业评论", "自媒体长文", "活动纪要"],
                "reveals": [
                    "中文公共传播中的早期观点、半正式表达、长文解释和机构对外沟通。",
                    "不一定进入官网、论文或媒体报道的议题材料、会议综述、政策解读和社群观点。",
                ],
                "not_limited_to": ["专业机构文章", "某一个研究领域", "官方账号"],
                "evidence_role": "discovery_and_context; detail extraction or cross-source confirmation required before strong claims",
                "known_limits": ["open web index coverage is uneven", "article body extraction can fail", "publication time and account identity may need verification"],
            },
            "bilibili_video_ecology": {
                "label": "B站视频与弹幕评论生态",
                "source_type": "public_video_community",
                "candidate_tools": ["search_bilibili", "get_content_detail", "get_task_status"],
                "content_forms": ["视频", "字幕/转写", "评论", "互动数据", "UP主频道内容"],
                "reveals": [
                    "中文视频社区中的讲解、现场记录、青年表达、二创传播和互动反馈。",
                    "文字来源难以覆盖的口语化叙事、视觉证据和评论区反应。",
                ],
                "not_limited_to": ["教程视频", "娱乐内容", "单一年龄群体"],
                "evidence_role": "media_context; transcript/comment quality and sampling limits must be visible",
                "known_limits": ["转写和评论可能产生后台任务", "媒体分析成本较高", "互动数据不等于代表性民意"],
            },
            "youtube_video_ecology": {
                "label": "YouTube 公开视频生态",
                "source_type": "public_video_global",
                "candidate_tools": ["search_youtube", "get_content_detail", "get_task_status"],
                "content_forms": ["公开视频", "频道元数据", "字幕/转写", "评论"],
                "reveals": [
                    "跨语种、海外社群、机构频道和长视频讨论。",
                    "国际议题、技术演示、访谈和公开讲座中的一手或准一手材料。",
                ],
                "not_limited_to": ["英文内容", "新闻视频", "教程"],
                "evidence_role": "media_context; transcripts and channel identity require validation",
                "known_limits": ["API key and network reachability affect availability", "会员/私密/地区受限内容不可访问"],
            },
            "zhihu_discussion_ecology": {
                "label": "知乎问答与专栏讨论生态",
                "source_type": "community_qa_and_article",
                "candidate_tools": ["search_zhihu", "get_content_detail"],
                "content_forms": ["问题", "回答", "文章", "作者信息", "赞同/评论线索"],
                "reveals": [
                    "围绕问题展开的观点谱系、专业用户解释、经验叙述和争议焦点。",
                    "比媒体报道更细的论证过程和用户对概念的理解方式。",
                ],
                "not_limited_to": ["专家回答", "高赞答案", "公共政策议题"],
                "evidence_role": "argument_map; use as viewpoint evidence unless identity and facts are independently verified",
                "known_limits": ["登录态和反爬健康影响详情", "高赞不等于事实正确"],
            },
            "xiaohongshu_experience_ecology": {
                "label": "小红书生活经验与图文视频生态",
                "source_type": "experience_note_and_social_media",
                "candidate_tools": ["search_xiaohongshu", "get_content_detail"],
                "content_forms": ["图文笔记", "短视频", "评论", "图片 OCR 线索"],
                "reveals": [
                    "消费、生活方式、城市体验、教育就业和身份表达中的个人经验材料。",
                    "图片、标签和评论中呈现的细粒度场景与情绪线索。",
                ],
                "not_limited_to": ["种草内容", "女性用户", "消费领域"],
                "evidence_role": "experience_signal; avoid treating sampled posts as population-level proof",
                "known_limits": ["登录/冷却/反爬边界明显", "样本偏差和营销内容需要标注"],
            },
            "github_repository_ecology": {
                "label": "GitHub 开源项目生态",
                "source_type": "code_repository_and_project_metadata",
                "candidate_tools": ["search_github_repositories", "kr_web_search"],
                "content_forms": ["仓库", "README", "issue 线索", "语言", "stars", "更新时间"],
                "reveals": [
                    "技术实现、项目活跃度、依赖关系、社区问题和替代方案。",
                    "普通网页摘要无法呈现的代码事实和维护状态。",
                ],
                "not_limited_to": ["AI 项目", "热门仓库", "英文项目"],
                "evidence_role": "implementation_evidence; inspect repository details before technical claims",
                "known_limits": ["搜索受 GitHub 可访问性和速率限制影响", "stars 不等于质量"],
            },
            "academic_literature_ecology": {
                "label": "开放学术文献与元数据生态",
                "source_type": "academic_metadata_and_open_fulltext",
                "candidate_tools": ["search_academic", "extract_web_page"],
                "content_forms": ["论文元数据", "DOI", "摘要", "开放全文线索", "引用格式"],
                "reveals": [
                    "学术研究问题、方法、数据、引用网络和同行评议成果。",
                    "需要严肃论证时的高权重证据入口。",
                ],
                "not_limited_to": ["英文论文", "开放全文", "期刊文章"],
                "evidence_role": "scholarly_evidence; metadata-only hits are weaker than verified full text",
                "known_limits": ["不绕过登录、验证码、付费墙或机构授权", "不同 provider 覆盖范围不同"],
            },
            "generic_web_ecology": {
                "label": "开放网页与新闻博客生态",
                "source_type": "open_web",
                "candidate_tools": ["kr_web_search", "extract_web_page", "extract_dynamic_page"],
                "content_forms": ["官网", "新闻", "博客", "文档", "报告页面", "网页文章"],
                "reveals": [
                    "广域发现、官方表述、新闻时间线、技术文档和跨站线索。",
                    "当问题来源不确定时的低成本入口。",
                ],
                "not_limited_to": ["通用搜索摘要", "英文网页", "官方站点"],
                "evidence_role": "broad_discovery; source authority and page extraction quality determine strength",
                "known_limits": ["搜索引擎索引不完整", "摘要不能替代正文", "动态页面可能需要升级抽取"],
            },
        },
    }


def capability_atlas_manifest(
    *,
    decision_log_summary: Dict[str, Any] | None = None,
    include_runtime_observations: bool = False,
) -> Dict[str, Any]:
    """Describe observed source-ecology boundaries without hard-routing agents."""
    ecologies = source_ecology_manifest()["ecologies"]
    runtime_observations = _capability_runtime_observations(decision_log_summary or {}) if include_runtime_observations else {}
    cards: Dict[str, Any] = {}
    for ecology_id, ecology in ecologies.items():
        cards[ecology_id] = _capability_card(ecology_id, ecology, runtime_observations.get(ecology_id, {}))
    return {
        "schema": "knowledgeradar-capability-atlas/v1",
        "status": "implemented_p2",
        "principles": [
            "Tool schemas stay executable and short; capability cards describe source-ecology boundaries.",
            "minimum_stable_boundary is conservative and should require repeated successful probes before widening.",
            "maximum_observed_boundary may record a single successful observation, but must remain provisional.",
            "Failure modes are capability evidence, not just errors; they guide future agent decisions and skips.",
        ],
        "upgrade_rules": {
            "minimum_stable_boundary": "widen only after repeated successes across query archetypes or time windows",
            "maximum_observed_boundary": "record single observed reach as provisional until it repeats",
            "failure_modes": "merge decision-log and probe evidence; keep user-action/login boundaries explicit",
            "agent_boundary": "cards advise source selection; they must not force a fixed tool sequence",
        },
        "cards": cards,
    }


def _capability_runtime_observations(summary: Dict[str, Any]) -> Dict[str, Any]:
    platform_to_ecology = {
        "微信公众号": "wechat_public_article_ecology",
        "wechat": "wechat_public_article_ecology",
        "B站": "bilibili_video_ecology",
        "bilibili": "bilibili_video_ecology",
        "YouTube": "youtube_video_ecology",
        "youtube": "youtube_video_ecology",
        "知乎": "zhihu_discussion_ecology",
        "zhihu": "zhihu_discussion_ecology",
        "小红书": "xiaohongshu_experience_ecology",
        "xiaohongshu": "xiaohongshu_experience_ecology",
        "GitHub": "github_repository_ecology",
        "github": "github_repository_ecology",
        "academic": "academic_literature_ecology",
        "学术": "academic_literature_ecology",
        "web": "generic_web_ecology",
        "unknown": "generic_web_ecology",
    }
    observations: Dict[str, Any] = {}
    by_platform = summary.get("by_platform") or {}
    if isinstance(by_platform, dict):
        for platform, value in by_platform.items():
            ecology_id = platform_to_ecology.get(str(platform), "generic_web_ecology")
            item = observations.setdefault(ecology_id, {"platforms": {}, "failure_tags": {}, "latency": {}})
            item["platforms"][str(platform)] = value
    failure_tags = summary.get("failure_tags") or {}
    if isinstance(failure_tags, dict):
        tags = failure_tags.get("by_tag") if isinstance(failure_tags.get("by_tag"), dict) else failure_tags
        for ecology_id in observations or {"generic_web_ecology": {}}:
            observations.setdefault(ecology_id, {"platforms": {}, "failure_tags": {}, "latency": {}})["failure_tags"] = tags
    latency = summary.get("latency_by_platform") or {}
    if isinstance(latency, dict):
        for platform, value in latency.items():
            ecology_id = platform_to_ecology.get(str(platform), "generic_web_ecology")
            observations.setdefault(ecology_id, {"platforms": {}, "failure_tags": {}, "latency": {}})["latency"][str(platform)] = value
    return observations


def _capability_card(ecology_id: str, ecology: Dict[str, Any], runtime_observation: Dict[str, Any]) -> Dict[str, Any]:
    stable_boundaries = {
        "wechat_public_article_ecology": "公开索引可发现候选文章 URL、标题、摘要和来源线索；正文抽取成功后才可升级证据强度。",
        "bilibili_video_ecology": "公开搜索可返回视频标题、URL、UP 主、简介和互动数据；详情、字幕、评论和多模态需按预算进入。",
        "youtube_video_ecology": "公开视频搜索可返回标题、URL、频道、摘要和发布时间线索；字幕、评论和媒体分析需详情链路验证。",
        "zhihu_discussion_ecology": "公开搜索可返回问题、回答、文章、作者和赞同线索；事实采信需要详情和跨源验证。",
        "xiaohongshu_experience_ecology": "公开/登录态可用时搜索图文或视频笔记候选；稳定事实声明需要标注样本偏差和访问边界。",
        "github_repository_ecology": "仓库搜索可返回 README 线索、语言、stars、更新时间和 issue 入口；技术结论需进入代码或文档详情。",
        "academic_literature_ecology": "公开元数据可返回题名、作者、年份、DOI、摘要和开放全文线索；全文证据强于元数据。",
        "generic_web_ecology": "开放网页搜索可发现官网、文档、新闻、博客和报告页面；摘要不能替代正文抽取。",
    }
    maximum_boundaries = {
        "wechat_public_article_ecology": "在可访问样本上抽取正文、识别转载链和传播语境；开放索引覆盖不稳定。",
        "bilibili_video_ecology": "在可访问样本上进入详情、字幕/转写、评论和视频理解后台任务。",
        "youtube_video_ecology": "在可访问样本上进入字幕/转写、频道上下文、评论线索和长视频理解任务。",
        "zhihu_discussion_ecology": "在登录/反爬状态允许时抽取正文、评论和作者上下文。",
        "xiaohongshu_experience_ecology": "在登录/冷却状态允许时抽取图文详情、图片 OCR 和视觉线索。",
        "github_repository_ecology": "可进一步检查 README、issue、release、目录结构和代码事实。",
        "academic_literature_ecology": "可进入开放全文、引用线索和 provider 专项能力；不绕过付费墙或机构授权。",
        "generic_web_ecology": "可升级到静态/动态正文抽取、跨站比对和官方来源追溯。",
    }
    query_archetypes = {
        "wechat_public_article_ecology": ["机构解读", "行业长文", "转载传播", "个人专栏"],
        "bilibili_video_ecology": ["教程演示", "项目复盘", "观点视频", "评论反馈"],
        "youtube_video_ecology": ["engineering talk", "demo walkthrough", "conference lecture", "creator review"],
        "zhihu_discussion_ecology": ["概念争议", "经验回答", "专业解释", "高赞反例"],
        "xiaohongshu_experience_ecology": ["体验笔记", "图文场景", "生活实践", "消费反馈"],
        "github_repository_ecology": ["implementation", "issue failure", "alternative project", "maintenance status"],
        "academic_literature_ecology": ["benchmark", "method paper", "survey", "process feedback"],
        "generic_web_ecology": ["official docs", "news timeline", "blog post", "public report"],
    }
    detail_tools = [tool for tool in ecology.get("candidate_tools", []) if tool in {"extract_web_page", "extract_dynamic_page", "get_content_detail", "get_task_status"}]
    return {
        "id": ecology_id,
        "label": ecology.get("label"),
        "source_type": ecology.get("source_type"),
        "candidate_tools": ecology.get("candidate_tools", []),
        "minimum_stable_boundary": stable_boundaries.get(ecology_id, "公开搜索或元数据发现候选来源；详情和强证据需另行验证。"),
        "maximum_observed_boundary": maximum_boundaries.get(ecology_id, "可在成功样本上进入详情和交叉验证，但需要记录退化边界。"),
        "unique_evidence_role": ecology.get("evidence_role"),
        "content_forms": ecology.get("content_forms", []),
        "query_archetypes": query_archetypes.get(ecology_id, ["broad", "domain_specific", "edge_case", "counterexample"]),
        "detail_affordances": detail_tools,
        "known_failure_modes": ecology.get("known_limits", []),
        "runtime_observation": runtime_observation,
        "evidence_strength_rule": "search results are candidates; detail extraction, source identity, or cross-source validation upgrades strength",
        "when_to_consider": ecology.get("reveals", [])[:2],
        "when_to_avoid": [
            "when the task needs a stronger source class than this ecology can provide",
            "when recent runtime observations show login, anti-bot, empty detail, or cost boundaries and alternatives exist",
        ],
        "last_probe": "",
        "stability_rule": "single success updates maximum_observed only; repeated diverse successes may widen minimum_stable_boundary",
    }


def media_policy_manifest() -> Dict[str, Any]:
    """Return the configured model policy for the four media source modes."""
    policy = MediaModelPolicy.from_env()
    return {
        "schema": "knowledgeradar-media-policy/v1",
        "source_modes": ["basic_text", "derived_text", "sampled_media_with_text", "native_media"],
        "storage_policy_status": "implemented_p0_4",
        "storage": {
            "cache_root": str(media_cache_root()),
            "configuration": ["KR_MEDIA_CACHE_DIR", "KR_RUNTIME_MEDIA_DIR"],
            "default_ttl_seconds": DEFAULT_TTL_SECONDS,
            "manifest": "manifest.jsonl",
            "cleanup": "cleanup_expired_media_cache",
        },
        "model_configuration": {
            "KR_BASIC_TEXT_MODELS": {
                "models": list(policy.basic_text_models),
                "required": False,
                "default_behavior": "basic_text does not call a model unless explicitly configured",
            },
            "KR_ASR_MODELS": {
                "models": list(policy.asr_models),
                "required": True,
                "used_for": ["derived_text"],
            },
            "KR_OCR_MODELS": {
                "models": list(policy.ocr_models),
                "required": True,
                "used_for": ["derived_text", "sampled_media_with_text"],
            },
            "KR_FRAME_VISION_MODELS": {
                "models": list(policy.frame_vision_models),
                "required": True,
                "used_for": ["sampled_media_with_text"],
            },
            "KR_NATIVE_VIDEO_MODELS": {
                "models": list(policy.native_video_models),
                "required": True,
                "used_for": ["native_media"],
            },
            "KR_NATIVE_AUDIO_VIDEO_MODELS": {
                "models": list(policy.native_audio_video_models),
                "required": False,
                "used_for": ["native_media"],
            },
            "KR_COMMENT_FILTER_MODELS": {
                "models": list(policy.comment_filter_models),
                "required": False,
                "used_for": ["comment_filtering", "text_analysis"],
            },
        },
        "capability_registry": policy.capability_registry(),
        "provider_preflight": {
            "schema": "knowledgeradar-media-provider-preflight/v1",
            "rule": "configured model or quota screenshot is not availability proof; provider endpoint, region and entitlement require a real canary receipt.",
            "failure_classes": ["MODEL_OR_REQUEST_CONTRACT", "AUTH_OR_PERMISSION", "QUOTA_OR_RATE_LIMIT", "PROVIDER_UNAVAILABLE", "UNKNOWN_PROVIDER_FAILURE"],
        },
        "native_auto_limits": {
            "max_duration_seconds": policy.native_auto_max_duration_seconds,
            "max_frames": policy.native_auto_max_frames,
            "fps": policy.native_auto_fps,
            "max_input_tokens": policy.native_auto_max_input_tokens,
            "timeout_seconds": policy.native_auto_timeout_seconds,
        },
        "provider_primitives": {
            "bailian": {
                "status": "implemented_main_provider_p1_1_to_p1_4",
                "default_model": "qwen3-vl-flash",
                "inputs": ["text", "image_url", "image_data_url", "video_url", "audio_url"],
                "schemas": ["DashScope official multimodal-generation", "OpenAI-compatible chat/completions for text"],
                "functions": [
                    "call_multimodal_generation",
                    "call_text_model",
                    "call_image_url_model",
                    "call_frame_images_model",
                    "call_video_url_model",
                    "smoke_video_url",
                ],
                "usage_tracking": True,
                "usage_fields": ["prompt_tokens", "completion_tokens", "total_tokens", "cached_tokens"],
                "main_chain_scope": ["sampled_media_with_text", "native_media", "comment_filtering", "video_text_analysis"],
                "known_boundary": "Bilibili raw CDN URLs are not provider-downloadable in current matrix; probe and fallback before native_media",
            },
            "siliconflow": {
                "status": "implemented_manual_fallback_p1_5",
                "inputs": ["image_base64", "video_url", "audio_url"],
                "functions": ["call_video_url_model", "call_audio_url_model", "smoke_video_url"],
                "usage_tracking": True,
                "fallback_scope": "manual fallback only; enable with KR_ENABLE_SILICONFLOW_FALLBACK=true or explicit legacy model envs",
            },
            "mimo": {
                "status": "explicit_fallback_only_p1_5",
                "inputs": ["text", "image_url", "video_url", "audio_url"],
                "default_usage": False,
                "fallback_scope": "not used by default for comments or vision; keep only when user explicitly configures MIMO/API147 fallback and has a cost reason",
            },
        },
        "direct_media_probe": {
            "status": "implemented_p1_2",
            "schema": "knowledgeradar-direct-media/v1",
            "generic_rule": {
                "native_media_gate": "allow only when provider_downloadability.allow_native_media=true",
                "fallback_order": ["derived_text", "sampled_media_with_text", "native_media_when_provider_downloadable"],
                "expected_degraded": "provider_blocked/not_direct are designed fallbacks, not main-chain failures",
                "validation_status": "provider_blocked native video_url is EXPECTED_DEGRADED; successful subtitle/ASR/sample-frame fallback is PASS",
            },
            "platforms": {
                "bilibili": {
                    "candidate_source": "yt-dlp extract_info(skip_download=True)",
                    "reachability": "HEAD then range GET with redacted URL",
                    "provider_downloadability": "provider_blocked for raw CDN candidates; never auto-select native_media even when local probe succeeds",
                    "downloads_media": False,
                },
                "youtube": {
                    "candidate_source": "watch_url status only; YouTube Data API does not expose direct playable URLs",
                    "reachability": "not probed for watch URLs",
                    "provider_downloadability": "not_direct; no native_media without an actual provider-downloadable media URL",
                    "downloads_media": False,
                    "current_environment": "code implemented but Google/YouTube reachability may be unavailable",
                },
            },
        },
        "notes": [
            "P0.1-P0.3 define policy and routing contracts; P0.4 adds the managed media cache and TTL cleanup.",
            "P0.5 validated Bilibili video_url reachability with an isolated spike; P1.2 productizes direct URL metadata/probe and P1.3 gates native_media on provider_downloadability.allow_native_media.",
            "Bilibili raw CDN direct URLs fall back to derived_text or sampled_media_with_text; native_media is reserved for provider-downloadable URLs within KR_NATIVE_AUTO_* limits.",
            "Legacy KR_IMAGE_MODELS and KR_VIDEO_MODELS remain compatibility fallbacks in provider code but do not override approach defaults.",
        ],
    }


def research_quality_contract_manifest() -> Dict[str, Any]:
    """Return the agent-readable contract for research quality checks."""
    return {
        "schema": "knowledgeradar-research-quality-contract/v1",
        "status": "implemented_p1_questioner_and_p2_to_p5_governance",
        "evidence_sidecar_schema": "knowledgeradar-research-evidence/v1",
        "quality_check_schema": "knowledgeradar-research-quality-check/v1",
        "questioner_checkpoints": {
            "schema_field": "questioner_checkpoints",
            "required_when": "budget.selected_profile == 'deep' or quality_contract.requires_questioner == true",
            "minimum_stages": ["after_local_archaeology", "after_first_external_round", "before_final_report"],
            "purpose": "过程级提问者检查研究是否足够，而不是固定工具路由或持续监控。",
        },
        "research_budget_profiles": [
            {
                "id": "fast",
                "label": "快速",
                "meaning": "低成本、低延迟；优先形成可用判断，保留明确的不确定性和待补证据。",
                "limits": {
                    "repair_rounds": 0,
                    "evidence_surfaces": "minimal",
                    "long_running_media": "avoid_unless_already_required",
                },
            },
            {
                "id": "balanced",
                "label": "标准",
                "meaning": "中等成本；覆盖关键证据面，允许一次检查反馈后的定向补证。",
                "limits": {
                    "repair_rounds": 1,
                    "evidence_surfaces": "required_plus_decisive",
                    "long_running_media": "allowed_when_evidence_depends_on_media",
                },
            },
            {
                "id": "deep",
                "label": "深入",
                "meaning": "较高成本；追求更完整的证据覆盖、源码考古和多轮补证，输出更强的可审计性。",
                "limits": {
                    "repair_rounds": 2,
                    "evidence_surfaces": "broad_with_explicit_skips",
                    "long_running_media": "allowed_with_budget_awareness",
                },
            },
        ],
        "evidence_surfaces": [
            "web",
            "platform_search",
            "source_ecology",
            "academic",
            "media_detail",
            "video_detail",
            "image_detail",
            "local_code",
            "upstream_code",
            "runtime_probe",
            "prior_report",
        ],
        "repair_request_contract": {
            "owner": "quality_check",
            "consumer": "agent",
            "agent_autonomy": "agent_decides_how_to_repair",
            "agent_autonomy_zh": "Agent 自己决定怎么补证据、用什么工具、补到什么程度。",
            "fields": ["id", "priority", "category", "problem", "acceptable_evidence_surfaces", "done_when", "budget_impact"],
        },
        "layer_boundaries": {
            "tool_schema": "声明稳定能力、边界、参数和成本开关。",
            "source_ecology": "说明来源生态能揭示什么、有哪些候选工具和局限；不绑定单一领域或强制调用路径。",
            "capability_atlas": "记录来源生态的最小稳定边界、最大观察边界、失败模式和探测规则。",
            "search_result_affordance": "说明搜索结果还能不能进入详情、媒体、评论或后台任务继续挖。",
            "quality_check": "检查机器可读结构、声明过的证据面和修复请求。",
            "agent": "判断是否值得继续补证，并负责执行修复。",
        },
        "detail_gap_governance": detail_gap_governance_manifest(),
    }


def detail_gap_governance_manifest() -> Dict[str, Any]:
    """Describe how detail failures should become auditable evidence gaps."""
    return {
        "schema": "knowledgeradar-detail-gap-governance/v1",
        "scope": ["xiaohongshu", "zhihu"],
        "principle": "详情失败不能静默吞掉；要转成可审计的降级、跳过原因或下一步动作。",
        "failure_classes": {
            "login_required": {
                "evidence_action": "在 evidence sidecar 的 skipped_surfaces 记录登录态边界。",
                "agent_next_step": "需要用户授权登录时再继续；不能把搜索摘要当强证据。",
            },
            "anti_bot": {
                "evidence_action": "记录 platform_search 或弱证据，并标注 detail surface 缺口。",
                "agent_next_step": "可换来源、稍后重试或请求人工验证。",
            },
            "empty_detail": {
                "evidence_action": "记录详情解析为空和候选 URL。",
                "agent_next_step": "尝试同平台其他结果或跨平台证据。",
            },
            "dead_link": {
                "evidence_action": "记录 dead_link，不计入有效详情证据。",
                "agent_next_step": "更换候选结果。",
            },
            "retryable_runtime_error": {
                "evidence_action": "记录 runtime_probe 和错误类型。",
                "agent_next_step": "预算允许时重试一次，否则写入不确定性。",
            },
        },
        "sidecar_fields": ["coverage.skipped_surfaces", "evidence_items[].strength", "evidence_items[].degradation"],
    }


def platform_onboarding_policy() -> Dict[str, Any]:
    """Agent-readable methodology for selecting a new platform collection path."""
    return {
        "schema": "knowledgeradar-platform-onboarding/v1",
        "principle": "Choose the lowest-risk path that satisfies the data need; escalate only with health gates and observable failure semantics.",
        "agent_judgement_principles": [
            "When a task depends on external systems, unknown platform limits, public capability claims, or irreversible changes, turn uncertainty into evidence before implementation.",
            "Do not declare search/detail follow-up affordances until the target tool path has been verified with the same class of real URL.",
            "For report-grade KR design decisions, combine local source archaeology, KR runtime probes, and external evidence; mark skipped evidence surfaces explicitly.",
        ],
        "decision_tree": [
            {
                "condition": "official_or_public_api_available",
                "recommended_path": "http_api",
                "notes": ["prefer official API, open metadata API, RSS or sitemap before browser automation"],
            },
            {
                "condition": "no_api_and_public_no_login",
                "recommended_path": "static_http_or_reader",
                "notes": ["curl/httpx + Jina/trafilatura/readability/static parser before dynamic browser"],
            },
            {
                "condition": "js_heavy_no_login",
                "recommended_path": "playwright_chromium_dynamic",
                "notes": ["use browser rendering only when static extraction is insufficient"],
            },
            {
                "condition": "login_required_anti_bot_weak",
                "recommended_path": "managed_chrome_cdp_plus_httpx_or_api_interception",
                "notes": ["persistent profile and login health are required before probes"],
            },
            {
                "condition": "login_required_anti_bot_strong",
                "recommended_path": "managed_chrome_cdp_persistent_profile_plus_dedicated_collector",
                "backup_path": "isolated_browser_base_candidate",
                "notes": ["require account/profile/proxy isolation, low-frequency workload and circuit breaker"],
            },
            {
                "condition": "external_cli_or_mcp_available",
                "recommended_path": "l7_sidecar_candidate",
                "notes": ["must pass wrapper, schema, error, dependency, account and health isolation before backup admission"],
            },
        ],
        "evaluation_variables": [
            "api_availability",
            "login_required",
            "anti_bot_strength",
            "account_risk",
            "session_persistence",
            "fingerprint_consistency",
            "frequency_and_concurrency",
            "data_need_depth",
            "output_schema_stability",
            "compliance_boundary",
        ],
        "high_risk_validation_flow": [
            "login_persistence_close_reopen_guest_false",
            "single_action_probe",
            "low_frequency_realistic_workload",
            "failure_classification",
            "strategy_tree_role_assignment",
        ],
        "strategy_roles": ["primary", "backup_candidate", "fallback", "experimental", "excluded"],
    }


def browser_strategy_manifest() -> Dict[str, Any]:
    """Declarative browser/CDP strategy policy; does not launch browsers."""
    return {
        "schema": "knowledgeradar-browser-strategy/v1",
        "default_base": "chrome_cdp_persistent_profile",
        "layers": {
            "base": [
                {
                    "id": "chrome_cdp_persistent_profile",
                    "role": "primary",
                    "description": "Local Google Chrome with persistent profile and platform-specific CDP ports; never silently falls back to Edge.",
                    "health_gate": "chrome_runtime + chrome_cdp_<platform>",
                    "configuration_surface": [
                        "KR_CHROME_EXE",
                        "KR_XHS_CHROME_DEBUG_PORT",
                        "KR_ZHIHU_CHROME_DEBUG_PORT",
                        "KR_XHS_CHROME_USER_DATA_DIR",
                        "KR_ZHIHU_CHROME_USER_DATA_DIR",
                        "KR_XHS_CHROME_PROFILE_DIRECTORY",
                        "KR_ZHIHU_CHROME_PROFILE_DIRECTORY",
                        "CHROME_PATH (compatibility alias)",
                    ],
                    "risk_level": "medium",
                    "cost_level": "low",
                },
                {
                    "id": "chrome_for_testing_isolated_profile",
                    "role": "candidate",
                    "description": "Version-controlled isolated Chrome profile for repeatable automation.",
                    "health_gate": "not_enabled_by_default",
                    "configuration_surface": ["KR_CHROME_FOR_TESTING_EXE", "KR_BROWSER_ISOLATION_ROOT"],
                    "risk_level": "low",
                    "cost_level": "medium",
                },
                {
                    "id": "camoufox_v2_isolated_profile",
                    "role": "backup_candidate",
                    "description": "Xiaohongshu-only isolated Camoufox v2 profile validated for login persistence and one-shot search; never default.",
                    "profile_dir_default": "browser_data/xhs_camoufox_profile_v2",
                    "launch_mode": "explicit Playwright DOM probe or manual recovery only",
                    "health_gate": "camoufox_v2 health ok + isolated_account + low_frequency_probe",
                    "configuration_surface": ["KR_CAMOUFOX_EXE", "KR_CAMOUFOX_PROFILE_DIR", "KR_CAMOUFOX_ENABLED"],
                    "risk_level": "high",
                    "cost_level": "medium",
                },
            ],
            "automation": [
                {
                    "id": "playwright",
                    "role": "default_dynamic_automation",
                    "description": "General dynamic page rendering, screenshots and DOM interaction.",
                    "risk_level": "low",
                    "cost_level": "medium",
                },
                {
                    "id": "raw_cdp",
                    "role": "diagnostic_read_only",
                    "description": "Read-only current page inspection and platform health diagnosis; not a search fallback.",
                    "risk_level": "medium",
                    "cost_level": "low",
                },
                {
                    "id": "scrapling_cdp",
                    "role": "platform_specific",
                    "description": "Keep inside Xiaohongshu adapter; do not promote as global default.",
                    "risk_level": "high",
                    "cost_level": "medium",
                },
                {
                    "id": "nodriver_experimental",
                    "role": "diagnostic_read_only_candidate",
                    "description": "Python/CDP current-page inspector candidate; not a search fallback or mainline framework.",
                    "risk_level": "high",
                    "cost_level": "medium",
                },
            ],
            "protocol": [
                {
                    "id": "chrome_manager",
                    "role": "primary_cdp_manager",
                    "description": "Owns CDP ports, persistent profiles, lifecycle and health summaries.",
                    "module": "runtime.chrome_manager",
                },
                {
                    "id": "webdriver_bidi",
                    "role": "watchlist",
                    "description": "Cross-browser protocol candidate; not a short-term replacement for Chrome CDP.",
                },
            ],
        },
        "selection_rules": [
            "Prefer chrome_cdp_persistent_profile for logged-in platform adapters.",
            "Use KR_CHROME_EXE as the canonical Google Chrome override; Windows App Paths and stable install paths are checked when process environment variables are absent, and Edge is never an implicit fallback.",
            "Use Playwright for ordinary dynamic open-web extraction.",
            "Use raw CDP when platform adapters need profile/session/tab control.",
            "Experimental anti-detection browsers require explicit enablement, account isolation and compliance review.",
        ],
    }


def manual_interaction_manifest() -> Dict[str, Any]:
    """Describe the cross-platform human-in-the-loop recovery contract."""
    return {
        "schema": "knowledgeradar-manual-interaction/v1",
        "principle": "Only explicit login, QR, captcha, security-verification, or account-risk evidence may trigger NEEDS_INTERACTION.",
        "request_payload": "runtime.browser_sessions.manual_action_request_from_session",
        "entrypoints": {
            "probe": "health_check(mode='probe_browser_auth:<platform>')",
            "request": "health_check(mode='request_browser_interaction:<platform>:<reason>')",
            "complete": "health_check(mode='complete_browser_interaction:<platform>')",
            "inspect": "health_check(mode='browser_sessions') or health_check(mode='summary')",
        },
        "platforms": [
            "xhs",
            "zhihu",
            "boss",
            "liepin",
            "maimai",
            "cnki",
            "vip_oa",
            "coaj",
            "ucdrs",
            "calis_thesis",
            "nstrs",
            "pubscholar",
        ],
        "manual_states": ["NEEDS_USER", "USER_INTERACTING", "USER_DONE_VERIFYING"],
        "non_manual_states": {
            "empty_results": "Do not ask the user to log in; retry, inspect selector/page state, or use fallback.",
            "empty_detail": "Do not ask the user to log in unless page classifier sees login/verification markers.",
            "provider_unavailable": "Expected degraded or provider failure; manual login is not a fix.",
            "collector_error": "Code/dependency/selector issue; manual login is not a fix.",
        },
        "scope_key": "platform + profile_id/account_slot/debug_port/profile_dir_hash",
        "agent_guidance": [
            "Surface pending_interactions to the user and do not continue retry loops blindly.",
            "Run probe_browser_auth before opening an academic/login browser; request_browser_interaction is only for proven user action.",
            "小红书已绑定 profile 的人工窗口会在扫码后以 CDP 事件唤醒、认证探针确认并自动收口；complete mode 保留给诊断或其他平台。",
            "After a non-Xiaohongshu browser action, call the complete mode and rerun the failed probe/tool.",
            "Treat NEEDS_INTERACTION as actionable, EXPECTED_DEGRADED as non-blocking product boundary, and FAIL as an unexpected defect.",
        ],
    }


def sidecar_governance_manifest() -> Dict[str, Any]:
    """L7 external-channel graduation policy."""
    return {
        "schema": "knowledgeradar-sidecar-governance/v1",
        "role": "L7 external candidate layer before mainline strategy-tree admission",
        "candidate_channels": {
            "miku_ai": {
                "priority": "medium",
                "admission_target": "generic_external_search_or_detail_backup",
                "integration_mode": "sidecar_adapter",
                "risk_level": "medium",
            },
            "gh_cli": {
                "priority": "high",
                "admission_target": "developer_research_backup",
                "integration_mode": "cli_sidecar",
                "risk_level": "low",
                "strategy": "gh_cli_sidecar_backup_candidate",
                "health_check": "checks.gh_cli_sidecar",
            },
            "twitter_cli": {
                "priority": "low_until_compliance_review",
                "admission_target": "none_by_default",
                "integration_mode": "sidecar_adapter",
                "risk_level": "high",
            },
        },
        "required_contract": {
            "output_schema": ["platform", "query_or_url", "items", "source_url", "retrieved_at", "account_id_hash", "risk_flags"],
            "error_codes": [
                "LOGIN_REQUIRED",
                "COOKIE_EXPIRED",
                "COOKIE_EXTRACTION_FAILED",
                "SIGNATURE_FAILED",
                "ANTI_BOT_BLOCKED",
                "CAPTCHA_REQUIRED",
                "RATE_LIMITED",
                "SCHEMA_CHANGED",
                "NETWORK_ERROR",
                "DEPENDENCY_CONFLICT",
                "UNKNOWN",
            ],
            "health_checks": ["adapter_health", "login_health", "search_probe", "detail_probe", "schema_probe"],
            "isolation": ["separate_process_or_venv", "per_account_cookie_store", "per_account_rate_limit", "no_secret_logs"],
            "audit_fields": ["called_at", "purpose", "account_id_hash", "request_count", "failure_code", "risk_flags"],
        },
        "graduation_gates": [
            "fixed_probe_set_success_rate_meets_threshold",
            "error_code_explainability_meets_threshold",
            "no_plaintext_cookie_or_token_leak",
            "health_check_passes_for_N_days",
            "manual_kill_switch_exists",
            "fallback_path_verified",
        ],
    }


def gh_cli_admission_record(health: Dict[str, Any] | None = None) -> Dict[str, Any]:
    health = health or {}
    status = health.get("status", "unknown")
    return {
        "schema": "knowledgeradar-candidate-admission/v1",
        "candidate_id": "gh_cli",
        "candidate_type": "cli_sidecar",
        "platform": "GitHub",
        "risk_level": "low",
        "admission_state": "admitted_as_backup_candidate" if status == "ok" else "blocked_or_degraded",
        "integration_mode": "search_github_repositories",
        "actual_mcp_tool": True,
        "tool_name": "search_github_repositories",
        "health": {
            "status": status,
            "available": bool(health.get("available")),
            "configured": bool(health.get("configured")),
            "version": health.get("version", ""),
            "account_id_hash": health.get("account_id_hash", ""),
            "failure_code": health.get("failure_code", ""),
        },
        "normalized_output_schema": [
            "title",
            "url",
            "snippet",
            "source_provider",
            "retrieved_at",
            "score",
            "raw.full_name",
            "raw.language",
            "raw.updated_at",
        ],
        "failure_codes": [
            "LOGIN_REQUIRED",
            "TIMEOUT",
            "SCHEMA_CHANGED",
            "PROVIDER_UNAVAILABLE",
            "DEPENDENCY_CONFLICT",
            "UNKNOWN",
        ],
        "admission_gates": [
            "cli_available",
            "auth_status_redacted",
            "json_schema_normalized",
            "degradation_breaker_present",
            "no_plaintext_token_or_cookie_logs",
            "actual_mcp_tool_registered",
        ],
        "kill_switch": "KR_GH_CLI_ENABLED=false",
        "rollback": "disable search_github_repositories or return EXPECTED_DEGRADED from the tool while preserving generic web search",
        "notes": [
            "gh CLI is the low-risk L7 sidecar admission template",
            "This record does not admit high-risk browser/API candidates",
        ],
    }


def route_policy_matrix() -> Dict[str, Any]:
    """Declarative source-routing policy for agents before adding collectors."""
    return {
        "schema": "knowledgeradar-route-policy/v1",
        "default_order": ["official_api", "open_metadata_api", "web_search", "generic_web_detail", "specialized_crawler"],
        "source_types": {
            "academic": {
                "default_route": "open_metadata_api",
                "preferred": ["OpenAlex", "Crossref", "Semantic Scholar", "PubMed", "arXiv"],
                "optional_enhancers": ["SerpAPI Google Scholar when SERPAPI_API_KEY is configured"],
                "avoid_by_default": ["Google Scholar specialized crawler", "CNKI full-text crawler"],
                "crawler_gate": "Only metadata or authorized access; no login/paywall/captcha bypass.",
            },
            "recruitment": {
                "default_route": "web_search",
                "preferred": ["official_or_authorized_api", "aggregated_sources", "Tavily/SearXNG discovery"],
                "avoid_by_default": ["Boss Zhipin specialized crawler"],
                "crawler_gate": "Requires robots/terms/privacy/frequency review and no personal-data harvesting.",
            },
            "community_qa": {
                "default_route": "specialized_crawler_when_login_state_is_healthy",
                "preferred": ["platform_adapter", "web_search_fallback"],
                "crawler_gate": "Use adapter only when login, signing and anti-bot health are observable.",
            },
            "video": {
                "default_route": "platform_adapter_or_official_api",
                "preferred": ["Bilibili adapter", "YouTube Data API", "transcript/detail fallback"],
                "crawler_gate": "Prefer official or public video APIs; keep transcript and L2 tasks observable.",
            },
            "generic_web": {
                "default_route": "web_search_then_generic_web_detail",
                "preferred": ["Tavily", "AnySearch backup", "SearXNG local fallback", "Jina/trafilatura/readability/static fallback"],
                "crawler_gate": "Use dynamic rendering only when static extraction is insufficient.",
            },
        },
        "source_need_to_ecologies": {
            "scholarly_claims": ["academic_literature_ecology", "generic_web_ecology"],
            "official_or_institutional_public_communication": ["generic_web_ecology", "wechat_public_article_ecology"],
            "public_discourse_and_viewpoint_mapping": [
                "zhihu_discussion_ecology",
                "wechat_public_article_ecology",
                "bilibili_video_ecology",
                "xiaohongshu_experience_ecology",
                "generic_web_ecology",
            ],
            "media_or_visual_evidence": ["bilibili_video_ecology", "youtube_video_ecology", "xiaohongshu_experience_ecology"],
            "lived_experience_or_scene_signal": ["xiaohongshu_experience_ecology", "zhihu_discussion_ecology", "bilibili_video_ecology"],
            "implementation_or_project_evidence": ["github_repository_ecology", "generic_web_ecology"],
            "unknown_or_broad_discovery": ["generic_web_ecology"],
        },
        "specialized_crawler_entry_criteria": [
            "robots and terms do not clearly block the target path",
            "content is publicly accessible without bypassing login, paywall, CAPTCHA or anti-bot controls",
            "no sensitive personal data collection is required",
            "stable page/API structure and clear business value",
            "rate limit, cache, deletion, source attribution and copyright boundaries are controllable",
            "no lower-risk API or web-search route satisfies the task",
        ],
        "generic_search_first_when": [
            "the task is discovery, news, documentation, official pages or paper landing pages",
            "platform compliance is unclear",
            "results can come from multiple sites",
            "only title, snippet, URL, date and source are needed",
            "the domain involves copyright, paywall, login, recruitment, academic or personal-data risk",
        ],
    }


def platform_capabilities_dict(target_registry: PlatformRegistry = registry) -> Dict[str, Dict[str, Any]]:
    capabilities: Dict[str, Dict[str, Any]] = {}
    for platform in target_registry.platforms():
        adapter = target_registry.get(platform)
        cap = adapter.capabilities
        capabilities[platform] = {
            "platform": cap.platform,
            "search": cap.search,
            "detail": cap.detail,
            "comments": cap.comments,
            "media_extract": cap.media_extract,
            "login_required": cap.login_required,
            "strategies": list(cap.strategies),
            "notes": cap.notes,
            "manifest": platform_manifest(cap.platform, cap),
        }
    return capabilities


def platform_manifest(platform: str, cap: Any) -> Dict[str, Any]:
    defaults = _manifest_defaults(platform)
    return {
        "schema": "knowledgeradar-platform-manifest/v1",
        "platform": platform,
        "capabilities": {
            "search": bool(cap.search),
            "detail": bool(cap.detail),
            "comments": bool(cap.comments),
            "media_extract": bool(cap.media_extract),
            "login_required": bool(cap.login_required),
        },
        "strategies": list(cap.strategies),
        "default_route": defaults["default_route"],
        "risk_level": defaults["risk_level"],
        "transport": defaults["transport"],
        "health_layers": defaults["health_layers"],
        "configuration_surface": defaults["configuration_surface"],
        "governance": defaults.get("governance", {}),
        "notes": cap.notes,
    }


def _manifest_defaults(platform: str) -> Dict[str, Any]:
    data = {
        "B站": {
            "default_route": "platform_adapter",
            "risk_level": "medium",
            "transport": ["http_api", "background_task_queue"],
            "health_layers": ["search", "detail", "comments", "media"],
            "configuration_surface": ["BILI_HEADERS", "task_queue", "transcript_pipeline"],
        },
        "知乎": {
            "default_route": "platform_adapter_when_login_healthy",
            "risk_level": "high",
            "transport": ["signed_api", "chrome_cdp"],
            "health_layers": ["login", "search", "detail"],
            "configuration_surface": ["ZHIHU_CHROME_DEBUG_PORT", "persistent_profile", "cookie_health"],
        },
        "小红书": {
            "default_route": "platform_adapter_when_login_healthy",
            "risk_level": "high",
            "transport": [
                "chrome_cdp",
                "scrapling_cdp_primary",
                "camoufox_v2_backup_candidate",
                "bridge_fallback_diagnostic_only",
                "force_probe_diagnostic",
                "raw_cdp_diagnostic",
                "nodriver_diagnostic_candidate",
                "tikhub_paid_break_glass_limited",
            ],
            "health_layers": ["login", "search", "detail", "multimodal"],
            "configuration_surface": [
                "XHS_CHROME_DEBUG_PORT",
                "XHS_BRIDGE_PATH",
                "persistent_profile",
                "KR_CAMOUFOX_EXE",
                "KR_CAMOUFOX_PROFILE_DIR",
                "KR_XHS_TIKHUB_DAILY_SEARCH_LIMIT",
                "KR_XHS_TIKHUB_DAILY_DETAIL_LIMIT",
            ],
            "governance": {
                "scheduled_patrol": False,
                "xhs_max_concurrent_operations_p0": 1,
                "selector_alert_threshold": "3 zero-hit selector samples in the latest 5 detail calls",
                "search_detail_daily_hard_limit": "none; existing rate/cooldown gates apply",
                "tikhub_paid_daily_hard_limit": "search=1, detail=1; failed paid calls count",
            },
        },
        "YouTube": {
            "default_route": "official_api",
            "risk_level": "medium",
            "transport": ["youtube_data_api_v3", "transcript_api", "background_task_queue"],
            "health_layers": ["api_key", "search", "detail", "media"],
            "configuration_surface": ["YOUTUBE_API_KEY", "youtube_transcript_api"],
        },
    }
    return data.get(
        platform,
        {
            "default_route": "unknown",
            "risk_level": "unknown",
            "transport": [],
            "health_layers": [],
            "configuration_surface": [],
        },
    )


def build_capabilities(
    *,
    decision_log_path: str,
    provider_status: ProviderStatus,
    target_registry: PlatformRegistry = registry,
) -> Dict[str, Any]:
    status = provider_status()
    try:
        from academic_providers.service import academic_provider_status
        from academic_providers.registry import academic_provider_profile_status

        academic_status = academic_provider_status()
        academic_profiles = academic_provider_profile_status()
    except Exception as exc:
        academic_status = {"error": {"type": "status_unavailable", "message": str(exc), "degraded": True}}
        academic_profiles = {}
    try:
        from search_providers.host import host_search_card_summary
        from search_providers.planner import auto_search_plan
        from search_providers.profile import provider_profiles
        from search_providers.quota import quota_summary

        web_host_cards = host_search_card_summary()
        web_profiles = provider_profiles()
        available_host_names = [
            str(card.get("id"))
            for card in web_host_cards.get("cards", [])
            if card.get("state") == "available" and card.get("enabled")
        ]
        web_search_plan = auto_search_plan(status, host_provider_names=available_host_names).to_dict()
        web_quota = quota_summary()
    except Exception as exc:
        web_host_cards = {"error": str(exc), "cards": []}
        web_profiles = {}
        web_search_plan = {"schema": "knowledgeradar-web-search-plan/v1", "waves": []}
        web_quota = {}
    asr_policy = AsrPolicy.from_env()
    dependency_external = external_dependency_preflight_summary()
    asr_lifecycle = asr_lifecycle_summary()
    asr_lifecycle["current_state"] = "policy_probe_only; live worker states are exposed by future ASR worker pool runtime"
    asr_lifecycle["candidate_engine_install_status"] = {
        "faster_whisper": dependency_external["tools"]["faster_whisper"],
        "funasr": dependency_external["tools"]["funasr"],
        "sherpa_onnx": dependency_external["tools"]["sherpa_onnx"],
    }
    strategy_tree_registry = build_strategy_tree_bundle(include_nodes=True)
    strategy_tree_consistency = validate_strategy_tree_bundle(strategy_tree_registry, actual_tools=ACTUAL_MCP_TOOLS)
    tool_surface = build_tool_surface()
    source_ecologies = source_ecology_manifest()
    research_quality = research_quality_contract_manifest()
    validation_semantics = validation_semantics_manifest(status, academic_status)
    return {
        "schema_version": "knowledgeradar-capabilities/v1",
        "server_run_id": SERVER_RUN_ID,
        "platforms": platform_capabilities_dict(target_registry),
        "mcp_facade": facade_manifest(ACTUAL_MCP_TOOLS),
        "capability_manifest": manifest_summary(
            tool_surface=tool_surface,
            source_ecologies=source_ecologies,
            validation_semantics=validation_semantics,
            research_quality=research_quality,
        ),
        "source_ecologies": source_ecologies,
        "capability_atlas": capability_atlas_manifest(include_runtime_observations=False),
        "platform_onboarding_policy": platform_onboarding_policy(),
        "route_policy": route_policy_matrix(),
        "browser_strategy": browser_strategy_manifest(),
        "manual_interaction": manual_interaction_manifest(),
        "sidecar_governance": sidecar_governance_manifest(),
        "media_policy": media_policy_manifest(),
        "research_quality_contract": research_quality,
        "runtime_environment": runtime_environment_manifest(),
        "project_governance": project_governance_manifest(),
        "architecture_standard": architecture_standard_summary(tool_surface=tool_surface),
        "knowledge_asset_interface": knowledge_asset_schema_summary(),
        "openclaw_native_adapter": openclaw_native_adapter_summary(),
        "candidate_admission_templates": {
            "gh_cli": gh_cli_admission_record(),
        },
        "strategy_tree_registry": strategy_tree_registry,
        "strategy_tree_summary": strategy_tree_summary(),
        "strategy_tree_consistency": strategy_tree_consistency,
        "governance_registry": governance_registry_manifest(),
        "cache_registry": cache_registry_manifest(),
        "strategy_trees": {
            "web_search": web_search_plan,
            "generic_web_extraction": generic_web_strategy_tree(),
            "asr": asr_strategy_manifest(),
        },
        "validation_semantics": validation_semantics,
        "failure_taxonomy": ERROR_TAXONOMY,
        "actual_mcp_tools": actual_mcp_tools(),
        "tool_surface": tool_surface,
        "virtual_capabilities": {},
        "deprecated_provider_aliases": tool_surface["deprecated_provider_aliases"],
        "tools": {
            "health_check": {"category": "runtime", "description": "运行状态和轻量探活"},
            "analyze_decision_logs": {
                "category": "observability",
                "description": "汇总最近详情提取决策日志，用于路由/证据质量校准",
                "schema": "knowledgeradar-decision-log-summary/v1",
            },
            "get_task_status": {
                "category": "observability",
                "description": "查询后台理解任务状态；支持 task_scope/source/content/task_id fan-in wait；research_session_id 仅为兼容别名；返回 compact task ref，避免重复传输转写/分析全文",
                "schema": "knowledgeradar-task-status/v2",
                "task_ref_schema": "knowledgeradar-runtime-task-ref/v1",
                "polling": {
                    "recommended_next_action": "poll_get_task_status",
                    "task_scope_fanin": "get_task_status(wait=true,task_scope_id=...,max_wait_s=...) waits blocking tasks before final reports",
                    "source_fanin": "get_task_status(wait=true,source_url=... or content_id=...) waits blocking tasks tied to a source",
                    "task_fanin": "get_task_status(wait=true,task_id=...) waits a specific blocking task",
                    "legacy_alias": "get_task_status(wait=true,research_session_id=...) remains available for P0/P1 compatibility",
                    "finalize_wait": {
                        "status": "implemented",
                        "default_max_wait_s": "KR_FINALIZE_MAX_WAIT_S or 120",
                        "poll_s": "KR_FINALIZE_POLL_S or 1",
                    },
                    "result_reread": "after terminal status, call result_reread_tool such as get_content_detail again to load transcript/result content",
                    "stale_cleanup": "queued/running stale tasks are marked cancelled with error_code and reason",
                    "large_payload_policy": "result content stays in result files; status responses expose result refs only",
                },
            },
            "kr_web_search": {
                "category": "collector.web_search",
                "providers": status,
                "provider_capability_profiles": web_profiles,
                "strategy_tree": "web_search",
                "default_waves": web_search_plan.get("waves", []),
                "quota": web_quota,
                "host_search_cards": web_host_cards,
                "host_search_readiness": web_host_cards.get("readiness", {}),
                "configuration_surface": [
                    "KR_WEB_SEARCH_PROVIDERS",
                    "TAVILY_API_KEY",
                    "BRAVE_SEARCH_API_KEY",
                    "EXA_API_KEY",
                    "SEARXNG_BASE_URL",
                    "ANYSEARCH_SEARCH_ENDPOINT",
                    "KR_HOST_SEARCH_CARD_DIR",
                    "KR_HOST_SEARCH_ENDPOINT",
                ],
                "fallback_policy": "auto mode runs configured free/host providers in parallel waves, aggregates and deduplicates results, and calls paid Tavily only when earlier waves are insufficient and its daily quota is available.",
            },
            "search_github_repositories": {
                "category": "collector.external_sidecar",
                "platform": "GitHub",
                "description": "搜索 GitHub 仓库，返回仓库标题、URL、简介、语言、stars、更新时间和可用的 README/issue 线索；受 GitHub 可访问性、认证状态和速率限制影响。",
                "source_ecologies": ["github_repository_ecology"],
                "sidecar": "gh_cli",
                "actual_mcp_tool": True,
                "external_signature_stable": True,
                "health_check": "checks.gh_cli_sidecar",
                "deprecated_alias": "kr_web_search(provider='github')",
            },
            "search_youtube": {
                "category": "collector.platform",
                "platform": "YouTube",
                "description": "搜索 YouTube 公开视频，返回标题、URL、频道、摘要、发布时间和平台元数据；需要 API 配置，不访问未公开、会员或地区受限内容。",
                "source_ecologies": ["youtube_video_ecology"],
                "actual_mcp_tool": True,
                "external_signature_stable": True,
                "health_check": "checks.youtube",
                "deprecated_alias": "kr_web_search(provider='youtube')",
            },
            "search_wechat_articles": {
                "category": "collector.web_search",
                "platform": "微信公众号",
                "description": "搜索公开微信公众号文章候选，返回 URL、标题、摘要、发布时间线索和来源线索；L1 查询模板发现，不使用登录态、Cookie、后台接口、Sogou 解析器或第三方数据代理。",
                "source_ecologies": ["wechat_public_article_ecology"],
                "actual_mcp_tool": True,
                "integration_level": "L1_query_templates",
                "strategy_tree": "web_search",
                "implementation_route": "generic web search provider auto mode with mp.weixin.qq.com query templates",
                "detail_followup": {
                    "primary_tool": "extract_web_page",
                    "fallback_tool": "extract_dynamic_page",
                    "get_content_detail_platform_strategy": False,
                },
                "configuration_surface": [
                    "KR_WEB_SEARCH_PROVIDERS",
                    "TAVILY_API_KEY",
                    "BRAVE_SEARCH_API_KEY",
                    "EXA_API_KEY",
                    "SEARXNG_BASE_URL",
                    "ANYSEARCH_SEARCH_ENDPOINT",
                    "KR_HOST_SEARCH_CARD_DIR",
                    "KR_HOST_SEARCH_ENDPOINT",
                ],
                "cache_policy": "inherits search cache under platform key wechat_articles",
                "cost_profile": "low to medium; one call may issue multiple configured web search requests and does not fetch article bodies",
                "health_check": "generic web search provider status via health_check/get_capabilities",
                "external_signature_stable": True,
            },
            "search_academic": {
                "category": "collector.academic",
                "providers": [*academic_profiles.keys(), "auto"],
                "provider_capability_profiles": academic_profiles,
                "provider_status": academic_status,
                "default_order": "auto uses Citation Import first for local citation files/text. For Chinese queries it then tries confirmed broad open full-text providers nssd -> chinaxiv -> hanspub -> oajrc -> sciopen -> pubscholar -> sciengine -> vip_oa before OpenAlex -> Crossref -> Semantic Scholar -> Baidu Qianfan Scholar -> SerpAPI Scholar. Explicit providers ivy_publisher and hkjo are available for narrower corpus coverage; socolar is a logged abstract/discovery supplement with external landing URLs; vip_oa uses the logged CQVIP main-site intelligent-reading route and extracts PDF text from the PDF.js preview file without invoking download controls; oalib is discovery-only because returned download landing pages are not verified PDF bytes.",
                "profile_schema": "knowledgeradar-academic-provider-profiles/v1",
                "result_schema_extensions": {
                    "source_database": "normalized database/provider id such as nssd, chinaxiv, hanspub, oajrc, sciopen, openalex, baidu_scholar, cnki, wanfang",
                    "access_mode": "open_full_text | public_api | official_api | serp_metadata | authorized_browser | document_delivery | registered_online_view | anonymous_open_access_pdf_viewer | logged_abstract_discovery | logged_online_reading_pdf_viewer | user_import",
                    "full_text_status": "metadata_only | direct_pdf | pdf_text_extractable | open_landing_page | open_access_article_detail | open_pdf_viewer_candidate | logged_online_reading_pdf_text_confirmed | abstract_with_external_landing_url | oa_available | licensed_visible | user_supplied | unavailable",
                    "provider_confidence": "0.0-1.0 provider-level confidence before cross-source verification",
                    "title_similarity": "0.0-1.0 similarity score when a citation verifier compares titles",
                    "verification_status": "unverified | title_matched | doi_matched | cross_provider_matched | user_verified",
                    "citation_export_formats": "supported or observed import/export formats such as ris, bibtex, endnote, refworks, noteexpress",
                    "license_scope": "open | institution | personal_purchase | unknown",
                    "degraded_reason": "explicit reason when a provider/result is partial, unauthorized, quota-limited, or unavailable",
                },
                "configuration_surface": [
                    "OPENALEX_API_KEY",
                    "SEMANTIC_SCHOLAR_API_KEY",
                    "BAIDU_QIANFAN_BEARER_TOKEN",
                    "KR_ACADEMIC_BAIDU_DAILY_LIMIT",
                    "SERPAPI_API_KEY",
                    "KR_ACADEMIC_ENABLE_SERPAPI",
                    "KR_ACADEMIC_SERPAPI_DAILY_LIMIT",
                    "KR_SERPAPI_MONTHLY_LIMIT",
                    "KR_ACADEMIC_IMPORT_MAX_BYTES",
                ],
                "open_full_text_policy": "Only public OA/full-text pages that expose accessible PDF/full-text bytes are auto-fetched. Browser-rendered public platforms such as PubScholar and SciEngine may be accessed through an anonymous Playwright path when the page itself exposes open/free-access filtering or PDF controls and text-extractable PDF file URLs. Professional licensed sources, token-gated APIs, anti-automation landing pages, unresolved WAF-sensitive routes, and document-delivery workflows are declared as degraded/unavailable unless the user supplies an authorized export or a later explicit browser probe proves stable direct full-text extraction.",
                "quota_policy": "Citation Import is local and quota-free; OpenAlex uses a free API key path; Semantic Scholar without academic/company email may run unauthenticated and degrade on 429; Baidu Qianfan Scholar free trial is treated as 50/day unless authorized; SerpAPI Free Plan is scarce (default 8/day, 250/month).",
                "legal_boundary": "Citation Import only parses user-supplied citation exports/text. Baidu Scholar is accessed only via the official Qianfan API. SerpAPI returns Google Scholar SERP metadata. KR may fetch public OA full-text links exposed by open platforms such as NSSD/ChinaXiv/Hans/OAJRC/SciOpen and explicit providers such as PubScholar/SciEngine/HKJO/IVY. It does not bypass login, captcha, payment, document delivery, token gates, WAF, or institutional authorization for CNKI/Wanfang/VIP/CALIS/NSTRS-style professional databases.",
                "coverage_reporting": "Responses expose source_database/access_mode/full_text_status so reports can say which databases were searched, which were unavailable, and which results are only open metadata.",
                "external_signature_stable": True,
            },
            "extract_web_page": {
                "category": "collector.generic_web",
                "collectors": ["jina_reader", "trafilatura", "readability", "static_html"],
                "strategy_tree": "generic_web_extraction",
            },
            "extract_dynamic_page": {"category": "collector.generic_web", "collectors": ["dynamic_playwright"], "strategy_tree": "generic_web_extraction"},
            "search_bilibili": {"category": "collector.platform", "platform": "B站"},
            "search_zhihu": {"category": "collector.platform", "platform": "知乎"},
            "search_xiaohongshu": {"category": "collector.platform", "platform": "小红书"},
            "get_content_detail": {
                "category": "detail",
                "input": "url + optional deep/comment/auto_multimodal/work_scope_id/task_scope_id/research_session_id legacy alias flags",
                "adds_evidence": True,
                "external_signature_stable": True,
                "task_scope_fanin": {
                    "status": "implemented",
                    "response_fields": ["server_run_id", "work_scope_id", "task_scope_id", "scope_binding", "pending_tasks", "blocking_tasks"],
                    "blocking_tasks": ["bilibili_transcribe", "bilibili_qwen_video_analysis", "youtube_qwen_video_analysis"],
                    "legacy_alias": "research_session_id remains returned for old callers but is not the worker lifecycle key",
                },
                "bilibili_asr_fast_path": {
                    "status": "implemented_p2_2_strategy_tree",
                    "order": ["transcript_cache", "bilibili_subtitle_api", "audio_download_plus_faster_whisper"],
                    "config": ["KR_ASR_MODELS", "KR_ASR_DEVICE", "KR_ASR_COMPUTE_TYPE", "KR_ASR_BEAM_SIZE", "KR_ASR_VAD", "KR_ASR_LANGUAGE", "KR_ASR_BATCH_SIZE", "KR_WHISPER_MODEL_DIR"],
                    "timing_fields": ["subtitle_probe_s", "download_s", "model_load_s", "warmup_s", "unload_to_cpu_s", "reload_from_cpu_s", "transcribe_s", "total_s"],
                    "transcript_cache_key_fields": asr_policy.compact()["transcript_cache_key_fields"],
                    "benchmark_tool": "tools/asr_benchmark.py --dry-run or --real-run --manifest <local-audio-manifest.json>",
                    "strategy_tree": "strategy_trees.asr",
                    "concurrency": "resource_concurrency",
                },
                "comment_filtering": {
                    "status": "implemented_non_blocking_p1_4",
                    "blocks_final_report": False,
                    "model_policy": "KR_COMMENT_FILTER_MODELS",
                    "default_models": list(MediaModelPolicy.from_env().comment_filter_models),
                    "default_provider": "bailian",
                    "fallback_scope": "MIMO/API147 only when explicitly configured; no key returns rule fallback with provider/model metadata",
                    "timeout_behavior": "returns raw comments or rule-filtered comments with explicit policy; background cache may complete later",
                },
                "direct_media": {
                    "status": "implemented_p1_2",
                    "response_field": "direct_media",
                    "schema": "knowledgeradar-direct-media/v1",
                    "bilibili": "auto_multimodal/deep analysis can expose redacted direct URL candidates and reachability without downloading media",
                    "youtube": "watch_url is exposed as non-direct status; code implemented but current environment may be unable to validate Google/YouTube live paths",
                    "provider_downloadability": "native_media is allowed only when probe.provider_downloadability.allow_native_media=true",
                    "native_switching": "implemented_p1_3; Bilibili raw CDN candidates remain sampled/derived fallback because provider-side downloads failed in validation",
                },
            },
        },
        "task_scope_fanin": {
            "schema": "knowledgeradar-task-scope-fanin/v1",
            "status": "implemented",
            "server_run_id": SERVER_RUN_ID,
            "scope_fields": ["work_scope_id", "task_scope_id", "source_url", "content_id", "task_id"],
            "wait_tool": "get_task_status(wait=true,task_scope_id=... or source_url/content_id/task_id=...,max_wait_s=...)",
            "blocking_metadata_key": "blocks_final_report",
            "terminal_statuses": ["completed", "failed", "skipped", "cancelled"],
            "legacy_alias": {
                "field": "research_session_id",
                "status": "compatibility_only",
            },
        },
        "proxy_rules": {
            "schema": "knowledgeradar-proxy-rules/v1",
            "status": "implemented_p1_8",
            "configuration_surface": ["KR_PROXY_RULE_DIRECT_URLS", "KR_PROXY_RULE_PROXY_URLS", "KR_PROXY_RULE_CACHE_DIR", "KR_PROXY_RULE_TTL_SECONDS"],
            "matching": ["exact_domain", "domain_suffix", "wildcard", "domain_keyword"],
            "clash_required": False,
            "runtime_manifest": "runtime_environment.proxy_rules",
        },
        "task_worker": {
            "schema": "knowledgeradar-task-worker/v1",
            "status": "implemented_p1_11",
            "mode_env": "KR_TASK_WORKER_MODE",
            "default_mode": "local_thread_sqlite",
            "adapter": "runtime.task_adapter.LocalTaskAdapter",
            "runtime_manifest": "runtime_environment.task_worker",
        },
        "resource_concurrency": resource_concurrency_summary(),
        "runtime_leases": runtime_lease_summary(limit=20),
        "asr_lifecycle": asr_lifecycle,
        "asr_benchmark": {
            "schema": "knowledgeradar-asr-benchmark/v2",
            "status": "implemented_p2_2_real_run_local_audio_manifest",
            "tool": "tools/asr_benchmark.py",
            "lifecycle_probe_tool": "tools/asr_lifecycle_probe.py --summary",
            "engine_install_probe_tool": "tools/asr_engine_install_probe.py",
            "real_run_command": "tools/asr_benchmark.py --real-run --manifest <local-audio-manifest.json> --include-engines faster-whisper",
            "engine_matrix_command": "tools/asr_engine_benchmark.py --manifest runtime/verification/p2_2_asr_benchmark_manifest.json --engines faster-whisper,funasr,sherpa-onnx",
            "baseline_fields": ["subtitle_probe_s", "download_s", "model_load_s", "warmup_s", "unload_to_cpu_s", "reload_from_cpu_s", "transcribe_s", "total_s"],
            "strategy_plan": "runtime.asr_strategy.build_asr_strategy_plan",
            "default_mode": "dry_run_schema_validation; engine real-run requires explicit local audio manifest and may download model weights but does not download media",
            "selected_default": "local:faster-whisper/base cpu/int8 after 2026-06-05 main-env benchmark",
            "not_selected": {
                "local:funasr/sensevoice-small": "benchmark evidence retained, but package is currently uninstalled from main .python312 and cold load was too high for KR default ASR",
                "local:sherpa-onnx/sensevoice-int8": "benchmark evidence retained, but package is currently uninstalled from main .python312 and 5m fixture was too slow and low quality on this host",
            },
        },
        "detail_response": {
            "external_compatibility": "legacy fields preserved",
            "standard_fields": ["platform", "title", "desc", "content", "transcript", "comments", "routing", "evidence"],
            "evidence_fields": [
                "source_url",
                "source_platform",
                "retrieved_at",
                "published_at",
                "summary",
                "credibility",
                "freshness",
                "verification_status",
            ],
        },
        "decision_logging": {
            "schema": "knowledgeradar-decision-log/v1",
            "path": decision_log_path,
            "records": ["detail_extract"],
            "purpose": "routing/evidence calibration and report-quality replay",
            "analysis_tool": "analyze_decision_logs",
        },
    }
