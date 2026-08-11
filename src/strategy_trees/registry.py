"""Unified strategy-tree registry.

This module describes platform-specific decision trees in a shared envelope.
It does not replace the existing model-native tool selection contract: the
model still chooses tools from schema and affordances, while these trees expose
how each selected tool governs admission, fallbacks, cache reuse, and evidence.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .schema import compact_tree, validation_report


POLICY_VERSION = "strategy-tree-v2.0-shadow"
SCHEMA = "knowledgeradar-strategy-tree-registry/v2"
_ROOT = Path(__file__).resolve().parents[2]


def _node(
    node_id: str,
    *,
    kind: str,
    module: str,
    decision_role: str,
    admission: dict[str, Any] | None = None,
    fallback: str = "",
    cache: str = "",
    budget: str = "",
    risk: str = "low",
    evidence: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "node_id": node_id,
        "kind": kind,
        "module": module,
        "decision_role": decision_role,
        "admission": dict(admission or {}),
        "fallback": fallback,
        "cache": cache,
        "budget": budget,
        "risk": risk,
        "evidence_fields": list(evidence or []),
    }


def _tree(
    tree_id: str,
    *,
    tool_name: str,
    platform: str,
    operation: str,
    owner_code_paths: list[str],
    tests: list[str],
    nodes: list[dict[str, Any]],
    invariants: list[str],
    notes: str = "",
    mode: str = "shadow",
) -> dict[str, Any]:
    return {
        "tree_id": tree_id,
        "version": POLICY_VERSION,
        "tool_name": tool_name,
        "platform": platform,
        "operation": operation,
        "mode": mode,
        "owner_code_paths": owner_code_paths,
        "tests": tests,
        "nodes": nodes,
        "invariants": invariants,
        "notes": notes,
    }


def _trees() -> list[dict[str, Any]]:
    return [
        _tree(
            "web_search.provider_wave",
            tool_name="kr_web_search",
            platform="generic_web",
            operation="search",
            owner_code_paths=["src/search_providers/service.py", "src/search_providers/planner.py", "src/search_providers/concurrency.py"],
            tests=["tests/test_search_provider_order.py", "tests/test_strategy_tree_registry.py"],
            nodes=[
                _node("plan_auto", kind="planner", module="search_providers.planner.auto_search_plan", decision_role="build provider waves from status/profile affordances"),
                _node("adaptive_wave_concurrency", kind="admission", module="search_providers.concurrency.compute_wave_concurrency", decision_role="choose bounded parallelism per wave"),
                _node("provider_attempt", kind="executor", module="search_providers.service._search_provider", decision_role="run configured provider with quota/circuit handling"),
                _node("coverage_supplement", kind="fallback", module="search_providers.aggregation.coverage_decision", decision_role="call paid Tavily only when earlier coverage is insufficient"),
            ],
            invariants=[
                "provider=github/youtube aliases stay deprecated and point to dedicated MCP tools",
                "free or host providers run before paid Tavily supplement in auto mode",
                "concurrency is metadata-visible and capped by KR_WEB_SEARCH_MAX_WORKERS",
            ],
        ),
        _tree(
            "github.search_sidecar",
            tool_name="search_github_repositories",
            platform="github",
            operation="repository_search",
            owner_code_paths=["src/collectors/platform/gh_cli_sidecar.py"],
            tests=["tests/test_github_sidecar_rest_fallback.py", "tests/test_strategy_tree_registry.py"],
            nodes=[
                _node("sidecar_admission", kind="admission", module="collectors.platform.gh_cli_sidecar.health", decision_role="classify gh CLI availability, auth, and dependency conflicts"),
                _node("gh_cli_search", kind="executor", module="collectors.platform.gh_cli_sidecar.search_repositories", decision_role="authenticated gh search path", risk="medium"),
                _node("github_rest_fallback", kind="fallback", module="collectors.platform.gh_cli_sidecar._github_rest_search", decision_role="unauthenticated REST fallback after empty or failed CLI"),
            ],
            invariants=[
                "GitHub remains an independent tool, not a generic web-search provider",
                "REST fallback keeps semantic query expansion and relevance reranking",
                "CLI admission must be observable before enforcement can replace trial-and-error",
            ],
        ),
        _tree(
            "xiaohongshu.risk_managed_browser",
            tool_name="search_xiaohongshu",
            platform="xiaohongshu",
            operation="search_and_detail",
            owner_code_paths=["src/collectors/platform/xiaohongshu.py", "src/server.py"],
            tests=["tests/test_strategy_tree_registry.py"],
            nodes=[
                _node("gate", kind="risk_gate", module="collectors.platform.xiaohongshu", decision_role="login/captcha/manual-action boundary", risk="high"),
                _node("cooldown", kind="cooldown", module="collectors.platform.xiaohongshu", decision_role="protect browser session after platform risk signals", risk="high"),
                _node("negative_cache", kind="cache", module="collectors.platform.xiaohongshu", decision_role="avoid repeated blocked detail attempts"),
                _node("ocr_chain", kind="fallback", module="collectors.platform.xiaohongshu", decision_role="image OCR when text surface is weak", budget="shared_multimodal_budget"),
            ],
            invariants=[
                "login, captcha, cooldown, and manual-action boundaries are not bypassed",
                "negative cache must distinguish platform block from genuine no-result",
                "OCR is a downstream evidence enhancer, not semantic routing",
            ],
            notes="Template candidate for risk_managed_browser_platform shared by other login-heavy browser surfaces.",
        ),
        _tree(
            "zhihu.cookie_governed_fallback",
            tool_name="search_zhihu",
            platform="zhihu",
            operation="search_and_detail",
            owner_code_paths=["src/collectors/platform/zhihu.py", "src/server.py"],
            tests=["tests/test_strategy_tree_registry.py"],
            nodes=[
                _node("cookie_health", kind="admission", module="collectors.platform.zhihu", decision_role="declare cookie/login health in governance registry", risk="medium"),
                _node("native_search", kind="executor", module="collectors.platform.zhihu", decision_role="platform API/browser search when admitted", risk="medium"),
                _node("fallback_chain", kind="fallback", module="collectors.platform.zhihu", decision_role="explicit fallback ordering with evidence tags"),
            ],
            invariants=["cookie health is governance state, not hidden control flow", "fallback paths must preserve source and error taxonomy"],
        ),
        _tree(
            "bilibili.search_detail_transcript_media",
            tool_name="search_bilibili",
            platform="bilibili",
            operation="search_detail_transcript_media",
            owner_code_paths=["src/collectors/platform/bilibili.py", "src/detail_strategies/bilibili.py", "src/media_router.py", "src/routing/router.py"],
            tests=["tests/test_media_router_planning.py", "tests/test_strategy_tree_registry.py"],
            nodes=[
                _node("search", kind="executor", module="collectors.platform.bilibili.legacy_search_bilibili", decision_role="public search result discovery"),
                _node("detail", kind="executor", module="detail_strategies.bilibili.BilibiliDetailStrategy", decision_role="detail, comments, routing signals"),
                _node("transcript_cache", kind="cache", module="collectors.platform.bilibili.transcribe_bilibili", decision_role="reuse subtitle/ASR transcript by cache key"),
                _node("media_router", kind="router", module="media_router.plan_media_action", decision_role="decide derived text, sampled media, or native media"),
            ],
            invariants=[
                "search/detail/transcript/media are separately observable subtrees",
                "deep analysis requires explicit routing signal, user flag, or transcript weakness",
                "subtitle and ASR cache keys belong in the unified cache registry",
            ],
        ),
        _tree(
            "youtube.quota_and_media",
            tool_name="search_youtube",
            platform="youtube",
            operation="search_detail_transcript_media",
            owner_code_paths=["src/collectors/platform/youtube.py", "src/detail_strategies/youtube.py", "src/media_router.py"],
            tests=["tests/test_media_router_planning.py", "tests/test_strategy_tree_registry.py"],
            nodes=[
                _node("quota_admission", kind="admission", module="collectors.platform.youtube.youtube_configured", decision_role="API key and quota governance"),
                _node("search_list", kind="executor", module="collectors.platform.youtube.search_youtube", decision_role="YouTube Data API search.list"),
                _node("transcript", kind="fallback", module="detail_strategies.youtube", decision_role="caption/transcript before ASR or visual analysis"),
                _node("watch_url_boundary", kind="media_policy", module="media_router.plan_media_action", decision_role="watch URLs are page URLs and never native downloadable media"),
            ],
            invariants=[
                "quota is shared governance state",
                "watch URL cannot satisfy native media admission",
                "member-only, private, or geo-restricted content remains unsupported/degraded",
            ],
        ),
        _tree(
            "academic.metadata_then_fulltext",
            tool_name="search_academic",
            platform="academic",
            operation="metadata_and_fulltext",
            owner_code_paths=["src/academic_providers/service.py", "src/academic_providers/planner.py", "src/academic_providers/fulltext.py"],
            tests=["tests/test_academic_provider_profiles.py", "tests/test_strategy_tree_registry.py"],
            nodes=[
                _node("intent_planner", kind="planner", module="academic_providers.planner.plan_academic_search", decision_role="produce provider order and planner reason"),
                _node("metadata_wave", kind="executor", module="academic_providers.service.search_academic_metadata", decision_role="metadata-first provider chain"),
                _node("fulltext_resolution", kind="secondary_stage", module="academic_providers.fulltext.extract_academic_fulltext", decision_role="resolve OA/fulltext candidates after ranking"),
            ],
            invariants=[
                "metadata collection and fulltext resolution are separate stages",
                "licensed/login/document-delivery boundaries remain explicit degraded states",
                "responses expose planner reason for audit without semantic rerouting",
            ],
        ),
        _tree(
            "recruitment.platform_specific",
            tool_name="search_recruitment",
            platform="recruitment",
            operation="job_search",
            owner_code_paths=["src/collectors/platform/boss.py", "src/collectors/platform/liepin.py", "src/server.py"],
            tests=["tests/test_strategy_tree_registry.py"],
            nodes=[
                _node("boss", kind="subtree", module="collectors.platform.boss.legacy_search_boss", decision_role="BOSS-specific public/job-detail policy", risk="medium"),
                _node("liepin", kind="subtree", module="collectors.platform.liepin.legacy_search_liepin", decision_role="Liepin-specific public/job-detail policy", risk="medium"),
                _node("v2ex", kind="subtree", module="server.search_recruitment", decision_role="V2EX lightweight web path"),
                _node("maimai_retired", kind="retired", module="server._maimai_web_search_from_request", decision_role="Maimai is retired and only reports degraded web fallback"),
            ],
            invariants=["each active recruitment source owns an independent subtree", "Maimai must not masquerade as an active native platform"],
        ),
        _tree(
            "detail.dispatch_and_unsupported",
            tool_name="get_content_detail",
            platform="detail",
            operation="dispatch",
            owner_code_paths=["src/server.py", "src/kr_core/models.py"],
            tests=["tests/test_strategy_tree_registry.py"],
            nodes=[
                _node("infer_platform", kind="dispatcher", module="server._infer_detail_platform", decision_role="URL/platform mapping without semantic routing"),
                _node("platform_strategy", kind="executor", module="server._handle_detail_request", decision_role="delegate to platform detail strategy"),
                _node("unsupported_url", kind="terminal", module="server._handle_detail_request", decision_role="return normalized error and alternative suggestions"),
            ],
            invariants=["unsupported URL responses include supported platforms and alternative tool suggestions", "legacy fields and evidence remain present"],
        ),
        _tree(
            "generic_web.extraction",
            tool_name="extract_web_page",
            platform="generic_web",
            operation="static_and_dynamic_extract",
            owner_code_paths=["src/generic_web/collector.py", "src/kr_core/strategy.py"],
            tests=["tests/test_strategy_tree_registry.py"],
            nodes=[
                _node("static_extractors", kind="executor", module="generic_web.collector.collect_url", decision_role="reader/trafilatura/readability/static HTML"),
                _node("dynamic_hint", kind="fallback", module="generic_web.dynamic.collect_dynamic_url", decision_role="browser-rendered extraction when static path is weak"),
            ],
            invariants=["dynamic browser extraction is explicit and not silently invoked by static extraction"],
        ),
        _tree(
            "multimodal.media_router",
            tool_name="virtual.media_router",
            platform="multimodal",
            operation="media_action_planning",
            owner_code_paths=["src/media_router.py", "src/routing/router.py", "src/media_bundle.py"],
            tests=["tests/test_media_router_planning.py", "tests/test_strategy_tree_registry.py"],
            nodes=[
                _node("bundle", kind="input_model", module="media_bundle.MediaBundle", decision_role="normalize text/image/audio/video evidence"),
                _node("route_decision", kind="signal", module="routing.router.decide_route", decision_role="use L1 signals to decide whether media understanding is needed"),
                _node("action_plan", kind="planner", module="media_router.plan_media_action", decision_role="allocate shared budget and choose transcript/OCR/frame/native operations"),
            ],
            invariants=["deep analysis shares media budget", "native media is admitted only by explicit direct-media proof", "models do not infer media availability from watch/page URLs"],
        ),
    ]


def build_strategy_tree_bundle(*, include_nodes: bool = True) -> dict[str, Any]:
    trees = _trees()
    if not include_nodes:
        trees = [compact_tree(tree) for tree in trees]
    return {
        "schema": SCHEMA,
        "policy_version": POLICY_VERSION,
        "default_mode": "shadow",
        "model_routing_contract": "Natural-language tool choice stays model-native; strategy trees expose per-tool execution policy, admission, fallback, cache, evidence, and observability.",
        "trees": trees,
    }


def strategy_tree_summary() -> dict[str, Any]:
    bundle = build_strategy_tree_bundle(include_nodes=False)
    validation = validate_strategy_tree_bundle(build_strategy_tree_bundle(include_nodes=True))
    return {**bundle, "validation_status": validation["status"], "validation_error_count": len(validation["errors"])}


def validate_strategy_tree_bundle(
    bundle: dict[str, Any] | None = None,
    *,
    actual_tools: list[str] | tuple[str, ...] | set[str] | None = None,
    repo_root: str | Path | None = None,
) -> dict[str, Any]:
    return validation_report(bundle or build_strategy_tree_bundle(include_nodes=True), actual_tools=actual_tools, repo_root=repo_root or _ROOT)


def governance_registry_manifest() -> dict[str, Any]:
    return {
        "schema": "knowledgeradar-governance-registry/v1",
        "policy_version": POLICY_VERSION,
        "entries": {
            "github.sidecar_admission": {"owner": "github.search_sidecar", "state_type": "auth_and_cli_health", "mode": "shadow"},
            "youtube.quota": {"owner": "youtube.quota_and_media", "state_type": "daily_quota_and_api_key", "mode": "shadow"},
            "zhihu.cookie_health": {"owner": "zhihu.cookie_governed_fallback", "state_type": "login_cookie_health", "mode": "declared"},
            "xiaohongshu.risk_gate": {"owner": "xiaohongshu.risk_managed_browser", "state_type": "cooldown_manual_boundary_negative_cache", "mode": "declared"},
            "media.shared_budget": {"owner": "multimodal.media_router", "state_type": "deep_analysis_budget", "mode": "declared"},
        },
    }


def cache_registry_manifest() -> dict[str, Any]:
    return {
        "schema": "knowledgeradar-cache-registry/v1",
        "policy_version": POLICY_VERSION,
        "entries": {
            "web_search.provider_cache": {"owner": "web_search.provider_wave", "ttl_s": "provider-specific", "negative_cache": True},
            "github.search_cache": {"owner": "github.search_sidecar", "ttl_s": 300, "negative_cache": False},
            "bilibili.transcript_cache": {"owner": "bilibili.search_detail_transcript_media", "key_fields": ["platform", "content_id", "asr_policy"]},
            "youtube.transcript_cache": {"owner": "youtube.quota_and_media", "key_fields": ["platform", "video_id", "language"]},
            "xiaohongshu.negative_cache": {"owner": "xiaohongshu.risk_managed_browser", "key_fields": ["url", "failure_type", "cooldown_epoch"]},
            "academic.metadata_cache": {"owner": "academic.metadata_then_fulltext", "ttl_s": 300},
        },
    }
