"""Agent-facing KnowledgeRadar capability descriptors."""

from __future__ import annotations

from typing import Any, Callable, Dict

from kr_core import PlatformRegistry, registry
from kr_core.strategy import ERROR_TAXONOMY, generic_web_strategy_tree
from capability_manifest import manifest_summary
from capability_manifest.ecologies import source_ecology_manifest, ecology_for_platform, joined_catalog
from mcp_tools import facade_manifest
from runtime.media_cache import DEFAULT_TTL_SECONDS, media_cache_root
from runtime.openclaw_native_adapter import openclaw_native_adapter_summary
from runtime.asr_lifecycle import asr_lifecycle_summary
from runtime.asr_policy import AsrPolicy
from runtime.asr_strategy import asr_strategy_manifest
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

from runtime.agent_contracts import TOOL_NAMES as ACTUAL_MCP_TOOLS


def actual_mcp_tools() -> list[str]:
    if not ACTUAL_MCP_TOOLS:
        import server  # register the actual tool surface; does not start the runtime
    return list(ACTUAL_MCP_TOOLS)


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
            "primary_discovery": sorted(academic_by_tier.get("primary_discovery", [])),
            "intent_domain_discovery": sorted(academic_by_tier.get("intent_domain_discovery", [])),
            "expected_degraded_boundaries": ["wanfang requires WFDATA_APP_KEY and collection entitlement", "semanticscholar auto supplement requires an API key and no cooldown", "cnki_authorized_browser requires explicit user-authorized workflow"],
        },
        "designed_fallbacks": {
            "bilibili_raw_cdn_native_media": "provider-side direct download is blocked by anti-hotlinking; this is not a failure when derived_text/ASR/sample-frame paths are available",
            "native_media_requires_provider_downloadable_url": "native_media is valid only when direct_media_probe.provider_downloadability.allow_native_media=true; provider-blocked URLs must use derived_text or sampled_media_with_text",
            "xiaohongshu_image_ocr_background": "Xiaohongshu OCR may exceed the sync window; scoped TaskStore fan-in plus get_task_status is the expected completion path",
            "semantic_scholar_without_key": "anonymous explicit calls may be rate limited; automatic supplement requires an API key and no cooldown",
            "ddgs_optional": "DDGS is optional; upstream refusal remains an explicit gap while other authorized discovery sources can continue",
        },
        "bilibili_multimodal_acceptance": {
            "native_video_url_provider_blocked": "EXPECTED_DEGRADED",
            "main_acceptance": "PASS when subtitle, local ASR, transcript cache, or sample-frame fallback completes.",
            "failure_boundary": "FAIL only when every declared derived_text/sample-frame fallback path is unavailable or the task crashes unexpectedly.",
        },
    }


def build_tool_surface() -> Dict[str, Any]:
    """Return the versioned MCP tool surface exposed by server.py."""
    tools = actual_mcp_tools()
    planning = planning_tools_manifest()
    return {
        "schema": "knowledgeradar-tool-surface/v2",
        "source_of_truth": "src/runtime/composition.py application contracts; MCP/CLI are projections",
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
            "Application contracts in runtime.composition are projected into MCP and CLI.",
            "kr_research is the high-level workflow entry for heavy research/perception tasks; low-level tools remain available for model-directed follow-up.",
            "YouTube search is exposed as the separate search_youtube MCP tool.",
            "GitHub repository search is exposed as the separate search_github_repositories MCP tool.",
            "微信公开文章由 search_wechat_articles 发现（公共索引、微信读书搜一搜），get_content_detail 读取正文；OpenCLI 为明确选择的只读浏览器通道。",
            "Source-ecology guidance is exposed by source_ecologies; MCP tool descriptions remain short execution contracts.",
            "kr_web_search(provider='youtube'/'github') remains only as a deprecated compatibility alias.",
            "The historic 12/13 tool-count wording is superseded by this versioned surface.",
            "expand_keywords and plan_research are legacy local helpers, not MCP registrations; Agents plan natively or use the registered research tools.",
        ],
    }




def capability_atlas_manifest(
    *,
    decision_log_summary: Dict[str, Any] | None = None,
    include_runtime_observations: bool = False,
) -> Dict[str, Any]:
    """分离声明与带定位的运行记录；不据汇总计数自动宣称真实验收。"""
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
        "global_observation": runtime_observations.get("global", {}),
        "unmapped_observation": runtime_observations.get("unmapped", {}),
    }


def _capability_runtime_observations(summary: Dict[str, Any]) -> Dict[str, Any]:
    observations: Dict[str, Any] = {}
    by_platform = summary.get("by_platform") or {}
    if isinstance(by_platform, dict):
        for platform, value in by_platform.items():
            ecology_id = ecology_for_platform(platform)
            item = observations.setdefault(ecology_id, {"platforms": {}, "failure_tags": {}, "latency": {}})
            item["platforms"][str(platform)] = value
    failure_tags = summary.get("failure_tags") or {}
    if failure_tags:
        observations["global"] = {"scope": "global", "failure_tags": failure_tags}
    latency = summary.get("latency_by_platform") or {}
    if isinstance(latency, dict):
        for platform, value in latency.items():
            ecology_id = ecology_for_platform(platform)
            observations.setdefault(ecology_id, {"platforms": {}, "failure_tags": {}, "latency": {}})["latency"][str(platform)] = value
    # 复用现有账本摘要的最近事件；保留时间、身份与日志定位，不能把成功次数当验收。
    for event in summary.get("recent_events") or []:
        if not isinstance(event, dict):
            continue
        platform = str(event.get("platform") or "unknown")
        trace = event.get("trace") if isinstance(event.get("trace"), dict) else {}
        item = observations.setdefault(ecology_for_platform(platform), {"platforms": {}, "failure_tags": {}, "latency": {}})
        item.setdefault("records", []).append({
            "platform": platform, "observed_at": event.get("timestamp", ""),
            "source_identity": {"code_generation": event.get("code_generation", ""),
                                "runtime_generation": event.get("runtime_generation", "")},
            "receipt_reference": {"log_path": summary.get("path", ""),
                                  "timestamp": event.get("timestamp", ""),
                                  "request_id": trace.get("request_id", "")},
            "scope": {"strategy": event.get("strategy", ""), "tool": trace.get("tool_name", ""),
                      "url": event.get("url", "")},
            "success": event.get("success"), "failure_tags": event.get("failure_tags", []),
            "verified": False, "verification_state": "runtime_record_not_acceptance",
        })
    return observations


def _capability_card(ecology_id: str, ecology: Dict[str, Any], runtime_observation: Dict[str, Any]) -> Dict[str, Any]:
    detail_tools = ecology.get("detail_tools", [])
    return {
        "id": ecology_id,
        "label": ecology.get("label"),
        "source_type": ecology.get("source_type"),
        "candidate_tools": ecology.get("candidate_tools", []),
        "declared_boundary": ecology.get("declared_boundary", ""),
        "availability": {"declared": True, "ready": "not_probed", "verified": False},
        "feature_policy": ecology.get("feature_policy", {}),
        "workflows": ecology.get("workflows", []),
        "minimum_stable_boundary": None,
        "maximum_observed_boundary": None,
        "unique_evidence_role": ecology.get("evidence_role"),
        "content_forms": ecology.get("content_forms", []),
        "query_archetypes": ecology.get("query_archetypes", []),
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
                "default_model": "qwen3.8-omni-flash",
                "reasoning_policy": {"ordinary_media": "none", "native_audio_video": "low"},
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
                "usage_fields": ["prompt_tokens", "completion_tokens", "total_tokens", "cached_tokens", "reasoning_tokens"],
                "main_chain_scope": ["sampled_media_with_text", "native_media", "video_text_analysis"],
                "known_boundary": "Bilibili raw CDN URLs are not provider-downloadable in current matrix; probe and fallback before native_media",
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
            "Model selections come only from runtime.model_roles; retired environment controls cannot override them.",
        ],
    }


def research_quality_contract_manifest() -> Dict[str, Any]:
    """Return the agent-readable contract for research quality checks."""
    return {
        "schema": "knowledgeradar-research-quality-contract/v1",
        "status": "diagnostic_research_guidance",
        "authority": "advisory_only_agent_decides_sufficiency",
        "evidence_sidecar_schema": "knowledgeradar-research-evidence/v1",
        "quality_check_schema": "knowledgeradar-research-quality-check/v1",
        "questioner_checkpoints": {
            "schema_field": "questioner_checkpoints",
            "suggested_when": "Agent requests a deep review",
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
                {"id": "kr_browser_link", "role": "borrowed_browser_primary",
                    "platforms": ["boss", "liepin_dynamic", "maimai_community", "zhihu"],
                    "description": "KR浏览器连接器：本人日常Chrome＋Content Script＋单一Native Messaging；HTTP路径不依赖它。",
                    "health_gate": "native protocol compatibility + explicit profile pairing; site authentication checked separately",
                    "configuration_surface": [],
                    "distribution": "store approval and extension identity pending; source edits do not install it"},
                {
                    "id": "chrome_cdp_persistent_profile",
                    "role": "primary",
                    "description": "Local Google Chrome with persistent profile and platform-specific CDP ports; never silently falls back to Edge.",
                    "health_gate": "chrome_runtime + chrome_cdp_<platform>",
                    "configuration_surface": [
                        "KR_CHROME_EXE",
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
                {"id": "opencli_daily_browser", "role": "xiaohongshu_primary",
                    "description": "借用日常Chrome扩展，不复制Cookie或管理用户进程。",
                    "health_gate": "opencli_connection; authentication requires separate UI probe",
                    "configuration_surface": ["KR_XHS_OPENCLI_ROOT", "KR_XHS_OPENCLI_CONTEXT_ID", "KR_XHS_OPENCLI_ADAPTER_SHA256"]},
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
            "小红书与微信借用日常Chrome的官方OpenCLI；知乎与BOSS借用KR Browser Link。其余来源按适配器合同，不能泛称全部CDP。",
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
        "principle": "默认后台最小化；明确扫码/验证码等人工交互信号，或已观察到最小化造成内容缺失/可见性阻断时才前置；解除后回后台。账号风险停止并交人工。",
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
            "小红书扫码等待最多300秒，释放任务worker；回读任务时确认登录后按阶段续跑。风控须人工确认解除。",
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
                "admission_target": "optional_github_authenticated_transport",
                "integration_mode": "readonly_api_transport",
                "risk_level": "low",
                "strategy": "github_readonly_api",
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
        "candidate_type": "readonly_api_transport",
        "platform": "GitHub",
        "risk_level": "low",
        "admission_state": "configured_not_business_verified" if status == "ok" else "blocked_or_degraded",
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
            "AUTH_INVALID", "AUTH_REQUIRED", "AUTH_CONTEXT_CHANGED",
            "PERMISSION_DENIED", "RATE_LIMITED", "NOT_FOUND_OR_INACCESSIBLE",
            "INVALID_QUERY_OR_OBJECT", "LEGAL_RESTRICTION", "TIMEOUT",
            "NETWORK_ERROR", "SCHEMA_CHANGED", "PROVIDER_DISABLED",
        ],
        "admission_gates": [
            "configured_transport_available",
            "request_identity_frozen",
            "json_schema_normalized",
            "shared_cooldown_and_leases",
            "no_plaintext_token_or_cookie_logs",
            "actual_mcp_tool_registered",
        ],
        "kill_switch": "KR_GITHUB_ENABLED=false (source); KR_GH_CLI_ENABLED=false (gh transport only)",
        "rollback": "KR_GITHUB_ENABLED=false disables GitHub search/detail without changing other sources",
        "notes": [
            "gh is an optional authenticated transport; public REST requires no gh installation",
            "configuration health is not account or business validation",
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
                "preferred": ["official_or_authorized_api", "aggregated_sources", "AnySearch/DDGS discovery"],
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
                "preferred": ["Tavily", "AnySearch backup", "Jina/trafilatura/readability/static fallback"],
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
    from runtime.comments_contract import platform_comment_scope
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
        "comment_contract": ({"schema":"knowledgeradar-comments/v2",
            **platform_comment_scope(platform),
            "request_schema":"get_content_detail.comments", "collection_is_not_live_acceptance":True,
            "coverage":"requested_window_and_source_exhaustion_are_separate",
            "media":"observed_attachments_with_public_download_and_explicit_ocr_asr",
            "view":"reversible_lexical_diversity; original_fixed_version_retained"} if cap.comments else None),
        "browser_resource_contract": ({"ownership":"borrowed_daily_profile",
            "presentation":"dedicated_work_window; never_append_to_user_window",
            "normal_work_focus":False,"manual_interaction":"explicit_handoff",
            "cleanup":"owned_tabs_only; user_takeover_relinquishes; no_browser_process_close",
            "live_acceptance":"not_inferred_from_capability_flag"}
            if platform in {'知乎','小红书','微信','BOSS直聘'} else None),
    }


def _manifest_defaults(platform: str) -> Dict[str, Any]:
    data = {
        "GitHub":{
            "default_route":"official_readonly_api", "risk_level":"medium",
            "transport":["gh_api","rest_api"],"health_layers":["auth","rate_limit","search","detail","comments"],
            "configuration_surface":["KR_GITHUB_TRANSPORT","GH_TOKEN","GITHUB_TOKEN"]},
        "微信":{
            "default_route":"public_http_with_explicit_opencli", "risk_level":"high",
            "transport":["public_http","opencli","shared_media"],
            "health_layers":["public_search","weread_session","article_body","reader_comments","media"],
            "configuration_surface":["opencli_daily_profile_binding","wechat_read_options"]},
        "B站": {
            "default_route": "platform_adapter",
            "risk_level": "medium",
            "transport": ["http_api", "background_task_queue"],
            "health_layers": ["search", "detail", "comments", "media"],
            "configuration_surface": ["BILI_HEADERS", "task_queue", "transcript_pipeline"],
        },
        "知乎": {
            "default_route": "browser_link_native",
            "risk_level": "high",
            "transport": ["content_script_native_messaging", "staged_shared_media"],
            "health_layers": ["login", "search", "detail"],
            "configuration_surface": ["browser_link_connection", "borrowed_profile_session"],
        },
        "小红书": {
            "default_route": "opencli_daily_browser",
            "risk_level": "high",
            "transport": ["opencli", "tikhub_paid_missing_stages_only"],
            "health_layers": ["extension_connection", "login", "search", "detail", "multimodal"],
            "configuration_surface": ["KR_XHS_OPENCLI_ROOT", "KR_XHS_OPENCLI_CONTEXT_ID",
                "KR_XHS_OPENCLI_ADAPTER_SHA256", "KR_PAID_POLICY_PATH"],
            "governance": {"single_account": True, "browser_site_concurrency": 1,
                "stop_on_risk": True, "automatic_browser_retry": False,
                "paid_default": "independent_provider_quota; no_shared_global_budget",
                "media_coverage": "full_observed_manifest; all_requested_images_or_explicit_gaps; validated_video",
                "comments_coverage": "bounded_top_level_with_embedded_replies"},
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
    asr_lifecycle = asr_lifecycle_summary()
    from runtime.model_roles import COMPONENTS, role_summary
    from runtime.optional_media import component_plan
    asr_lifecycle['component_install_status'] = {name: component_plan(name) for name in COMPONENTS}
    asr_lifecycle['roles'] = role_summary()
    strategy_tree_registry = build_strategy_tree_bundle(include_nodes=True)
    strategy_tree_consistency = validate_strategy_tree_bundle(strategy_tree_registry, actual_tools=ACTUAL_MCP_TOOLS)
    tool_surface = build_tool_surface()
    source_ecologies = joined_catalog(target_registry)
    research_quality = research_quality_contract_manifest()
    validation_semantics = validation_semantics_manifest(status, academic_status)
    result = {
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
        "architecture_standard": {"authority":"historical_design_only","verification":"not_runtime_proof"},
        "knowledge_asset_interface": {"authority":"fixed_assets_and_read_knowledge","legacy_schema":"historical_only"},
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
                    "ANYSEARCH_SEARCH_ENDPOINT",
                    "KR_HOST_SEARCH_CARD_DIR",
                    "KR_HOST_SEARCH_ENDPOINT",
                ],
                "fallback_policy": "auto mode runs configured free/host providers in parallel waves, aggregates and deduplicates results, and calls paid Tavily only when earlier waves are insufficient and its daily quota is available.",
            },
            "search_github_repositories": {
                "category": "collector.external_sidecar",
                "platform": "GitHub",
                "description": "搜索公开 GitHub 仓库，保留原查询与上游顺序，支持 page/refresh；通过 get_content_detail(github=...) 读取固定提交源码、README、目录和有界 Issue/PR 评论。",
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
                "actual_mcp_tool": True,
                "external_signature_stable": True,
                "health_check": "checks.youtube",
                "deprecated_alias": "kr_web_search(provider='youtube')",
            },
            "search_wechat_articles": {
                "category":"collector.platform", "platform":"微信",
                "description":"公开公众号文章发现：公共索引与微信读书搜一搜并列；候选交接get_content_detail，账号仅提示，验证不自动绕行。",
                "actual_mcp_tool":True,
                "integration_level":"wechat_readonly_v1", "strategy_tree":"web_search",
                "detail_followup":{"primary_tool":"get_content_detail","get_content_detail_platform_strategy":True},
                "cache_policy":"public configuration_generation and authority_scope; borrowed-account results are not cached across calls",
                "cost_profile":"bounded discovery only; article/OCR/ASR requested separately",
                "health_check":"public search status; OpenCLI optional and explicit",
                "external_signature_stable":False,
            },
            "search_academic": {
                "category": "collector.academic",
                "providers": [*academic_profiles.keys(), "auto"],
                "provider_capability_profiles": academic_profiles,
                "provider_status": academic_status,
                "default_order": "auto: PubScholar + OpenAlex for Chinese, OpenAlex for other queries; always complete matching domain/regional sources before stopping by count. DOI: Crossref then Unpaywall unless a readable fulltext link exists. Citation files: local import only. Optional Semantic Scholar/CORE/SerpAPI supplements respect auth, rate and budget. Wanfang requires explicit authorization. ar5iv is a read fallback, not a keyword-search wave.",
                "profile_schema": "knowledgeradar-academic-provider-profiles/v1",
                "result_schema_extensions": {
                    "source_database": "normalized database/provider id such as nssd, chinaxiv, pubscholar, sciengine, openalex, cnki, wanfang",
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
                    "WFDATA_APP_KEY",
                    "WFDATA_BASE_URL",
                    "SERPAPI_API_KEY",
                    "KR_ACADEMIC_ENABLE_SERPAPI",
                    "KR_ACADEMIC_SERPAPI_DAILY_LIMIT",
                    "KR_SERPAPI_MONTHLY_LIMIT",
                    "KR_ACADEMIC_IMPORT_MAX_BYTES",
                ],
                "open_full_text_policy": "Only public OA/full-text pages that expose accessible PDF/full-text bytes are auto-fetched. Browser-rendered public platforms such as PubScholar and SciEngine may be accessed through an anonymous Playwright path when the page itself exposes open/free-access filtering or PDF controls and text-extractable PDF file URLs. Professional licensed sources, token-gated APIs, anti-automation landing pages, unresolved WAF-sensitive routes, and document-delivery workflows are declared as degraded/unavailable unless the user supplies an authorized export or a later explicit browser probe proves stable direct full-text extraction.",
                "quota_policy": "Citation Import is local and quota-free; Semantic Scholar auto supplement requires key, cross-process pacing and server cooldown; CORE/SerpAPI retain their quota gates. Wanfang requires its own authorized app and quota.",
                "legal_boundary": "Citation imports are user-supplied. Wanfang uses its current official API with application authorization. Public OA pages may be read where permitted; login, captcha, payment, WAF and institutional gates are never bypassed. NSTRS full reports require approved professional registration and online viewing only.",
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
                    "order": ["transcript_cache", "bilibili_subtitle_api", "audio_download_plus_qwen3_asr_0_6b"],
                    "config": ["KR_ASR_DEVICE", "KR_ASR_LANGUAGE", "KR_LOCAL_MODEL_THREADS"],
                    "timing_fields": ["subtitle_probe_s", "download_s", "model_load_s", "warmup_s", "unload_to_cpu_s", "reload_from_cpu_s", "transcribe_s", "total_s"],
                    "transcript_cache_key_fields": asr_policy.compact()["transcript_cache_key_fields"],
                    "benchmark_tool": "scripts/kr_evaluate.py plan; scripts/kr_evaluate.py run --case model.local_chain",
                    "strategy_tree": "strategy_trees.asr",
                    "concurrency": "resource_concurrency",
                },
                "comment_filtering": {
                    "status": "deterministic_deduplication",
                    "assessment_owner": "research_agent",
                    "cloud_model": None,
                    "raw_comments_preserved": True,
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
            "tool": "scripts/kr_optional_media.py",
            "lifecycle_probe_tool": "get_capabilities:runtime_environment.asr_lifecycle",
            "engine_install_probe_tool": "scripts/kr_optional_media.py plan --component qwen3-asr-0.6b",
            "real_run_command":"scripts/kr_optional_media.py submit --component qwen3-asr-0.6b --payload-file <approved-input.json>",
            "selected_default":"local:qwen3-asr/0.6b-onnx-int8",
            "quality_boundary":"真实样本、人工金标和历史模型评测分别记录；旧结果不代表新模型质量",
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
    for tool_name, tool in result['tools'].items():
        tool['source_ecologies'] = [key for key, item in source_ecologies['ecologies'].items()
                                   if tool_name in item['candidate_tools']]
    return result
