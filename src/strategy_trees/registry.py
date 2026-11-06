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
            owner_code_paths=["src/collectors/platform/gh_cli_sidecar.py", "src/collectors/platform/github_api.py", "src/collectors/platform/github_content.py", "src/platform_adapters/github_adapter.py"],
            tests=["tests/test_github_sidecar_rest_fallback.py", "tests/test_github_api.py", "tests/test_github_objects.py", "tests/test_strategy_tree_registry.py"],
            nodes=[
                _node("transport_configuration", kind="admission", module="collectors.platform.gh_cli_sidecar.health", decision_role="configuration only; authentication resolved once per request"),
                _node("repository_search", kind="executor", module="collectors.platform.gh_cli_sidecar.search_repositories", decision_role="one original query, explicit public scope and pagination"),
                _node("object_reader", kind="executor", module="collectors.platform.github_content.collect", decision_role="durable commit-pinned files and separately paginated Issue/PR streams"),
            ],
            invariants=[
                "GitHub remains an independent tool, not a generic web-search provider",
                "preserve query syntax and upstream ordering; no hidden expansion or identity fallback",
                "rate cooldown, cancellation and evidence use shared runtime infrastructure",
                "configuration health does not claim authentication or business verification",
            ],
        ),
        _tree(
            "xiaohongshu.risk_managed_browser",
            tool_name="search_xiaohongshu",
            platform="xiaohongshu",
            operation="search_and_detail",
            owner_code_paths=["src/platform_adapters/xiaohongshu_adapter.py", "src/runtime/xhs_opencli.py", "src/runtime/xhs_opencli_runner.mjs"],
            tests=["tests/test_strategy_tree_registry.py"],
            nodes=[
                _node("gate", kind="risk_gate", module="runtime.xhs_opencli", decision_role="login/captcha/manual-action boundary", risk="high"),
                _node("risk_stop", kind="manual_stop", module="runtime.xhs_opencli", decision_role="stop browser collection until explicit human confirmation", risk="high"),
                _node("negative_cache", kind="cache", module="runtime.xhs_opencli", decision_role="avoid repeated blocked detail attempts"),
                _node("ocr_chain", kind="fallback", module="runtime.xhs_opencli", decision_role="image OCR when text surface is weak", budget="cloud_ocr_service_quota; explicit_processing_authorization"),
            ],
            invariants=[
                "login, captcha, cooldown, and manual-action boundaries are not bypassed",
                "negative cache must distinguish platform block from genuine no-result",
                "OCR is a downstream evidence enhancer, not semantic routing",
            ],
            notes="Template candidate for risk_managed_browser_platform shared by other login-heavy browser surfaces.",
        ),
        _tree(
            "zhihu.borrowed_browser_evidence",
            tool_name="search_zhihu",
            platform="zhihu",
            operation="search_and_detail",
            owner_code_paths=["src/collectors/platform/zhihu.py", "src/detail_strategies/zhihu.py", "browser_link/extension/sites/zhihu.js"],
            tests=["tests/test_strategy_tree_registry.py"],
            nodes=[
                _node("browser_session", kind="admission", module="runtime.native_site", decision_role="borrowed profile identity; explicit risk and login gates", risk="medium"),
                _node("native_search", kind="executor", module="collectors.platform.zhihu", decision_role="packaged readonly DOM search when admitted", risk="medium"),
                _node("staged_evidence", kind="executor", module="detail_strategies.zhihu", decision_role="exact target, bounded directory/comments; shared OCR after browser release"),
            ],
            invariants=["no cookie export or managed Zhihu CDP", "requested identity and typed gaps must survive evidence storage"],
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
                _node("comments", kind="executor", module="collectors.platform.youtube.collect_comments", decision_role="paged roots/replies with native relation and coverage"),
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
            operation="discovery_then_explicit_fulltext_task",
            owner_code_paths=["src/academic_providers/service.py", "src/academic_providers/planner.py", "src/academic_providers/fulltext.py", "src/use_cases/academic_fulltext.py"],
            tests=["tests/test_academic_provider_profiles.py", "tests/test_strategy_tree_registry.py"],
            nodes=[
                _node("intent_planner", kind="planner", module="academic_providers.planner.plan_academic_search", decision_role="produce provider order and planner reason"),
                _node("metadata_wave", kind="executor", module="academic_providers.service.search_academic_metadata", decision_role="metadata-first provider chain"),
                _node("fulltext_request", kind="secondary_stage", module="use_cases.academic_fulltext.request_academic_fulltext", decision_role="explicit immutable bibliographic reference submits independent durable task"),
                _node("fulltext_result", kind="executor", module="use_cases.academic_fulltext.execute", decision_role="authorized fulltext only; shared PDF pipeline and fixed evidence"),
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
                _node("boss", kind="subtree", module="collectors.platform.boss.search", decision_role="BOSS-specific public/job-detail policy", risk="medium"),
                _node("liepin", kind="subtree", module="collectors.platform.liepin.legacy_search_liepin", decision_role="Liepin-specific public/job-detail policy", risk="medium"),
                _node("v2ex", kind="subtree", module="server.search_recruitment", decision_role="V2EX lightweight web path"),
                _node("maimai_community", kind="subtree", module="collectors.platform.maimai.legacy_search_maimai", decision_role="Managed authenticated community search; post detail and visible comments require current account access", risk="medium"),
            ],
            invariants=["each active recruitment source owns an independent subtree", "Maimai discussions are not structured job cards; Zhilian has no active collector"],
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
            "wechat.discovery_and_detail", tool_name="search_wechat_articles", platform="wechat",
            operation="discovery_then_detail", owner_code_paths=["src/use_cases/wechat_search.py", "src/detail_strategies/wechat.py"],
            tests=["tests/test_wechat_discovery.py", "tests/test_wechat_chain.py"],
            nodes=[_node("discovery", kind="executor", module="use_cases.wechat_search", decision_role="public search and explicitly selected WeRead discovery"),
                   _node("detail", kind="executor", module="detail_strategies.wechat.collect", decision_role="article text and shared media; reader comments paused")],
            invariants=["reader comment collection is paused; unavailable does not mean zero", "no author-management API used as reader permission"],
        ),
        _tree(
            "document.shared_pdf", tool_name="process_local_evidence", platform="document",
            operation="bounded_document_processing", owner_code_paths=["src/runtime/pdf_text.py", "src/runtime/document_contract.py"],
            tests=["tests/test_architecture_convergence.py"],
            nodes=[_node("shared_pdf", kind="executor", module="runtime.pdf_text.process_pdf", decision_role="requested physical pages; preserve selectable text; compensate only missing or explicitly suspicious pages"),
                   _node("evidence", kind="executor", module="kr_core.evidence_store.EvidenceStore", decision_role="immutable evidence with page locators and explicit gaps")],
            invariants=["no automatic OCR of every page when one is empty", "OCR authorization and unknown-request no-replay survive resume"],
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
                _node("action_plan", kind="planner", module="media_router.plan_media_action", decision_role="choose operations; each configured provider keeps its own quota and authorization"),
            ],
            invariants=["cost admission remains independent per provider; no global total budget", "native media is admitted only by explicit direct-media proof", "models do not infer media availability from watch/page URLs"],
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
    root = Path(repo_root or _ROOT)
    installed = (root / "SBOM.json").exists()
    return validation_report(bundle or build_strategy_tree_bundle(include_nodes=True), actual_tools=actual_tools,
                             repo_root=root, check_test_paths=not installed)


def governance_registry_manifest() -> dict[str, Any]:
    return {
        "schema": "knowledgeradar-governance-registry/v1",
        "policy_version": POLICY_VERSION,
        "entries": {
            "github.sidecar_admission": {"owner": "github.search_sidecar", "state_type": "request_auth_identity_and_shared_cooldown", "mode": "shadow"},
            "youtube.quota": {"owner": "youtube.quota_and_media", "state_type": "daily_quota_and_api_key", "mode": "shadow"},
            "zhihu.browser_session": {"owner": "zhihu.borrowed_browser_evidence", "state_type": "borrowed_session_identity", "mode": "declared"},
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
