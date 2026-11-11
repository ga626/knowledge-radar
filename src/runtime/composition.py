"""入口中立的业务装配；平台实现沿用，MCP/CLI仅投影同一应用合同。"""
import json
import hashlib
import logging
import os
import re
import sys
import time
import uuid
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler
from typing import Annotated, Any, Dict, List, Literal

# Embedded Python may ignore PYTHONPATH and script directories. Pin this checkout
# before the first runtime import; never load an editable development copy.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pydantic import Field
from runtime.application import application_tool
from runtime.agent_contracts import DetailResult, TaskStatusResult, ResearchWorkflowResult
from runtime.read_options import DocumentReadOptions, CommentReadOptions
from runtime.research_queries import ResearchQuery, ResearchQuestion
from collectors.platform.github_content import GitHubReadOptions
from runtime.xhs_opencli import XhsReadOptions
from collectors.platform.wechat import WechatReadOptions


os.environ.setdefault("PYTHONUTF8", "1")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ── 项目根路径 ──────────────────────────────────────────────────────────
SRC_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_ROOT = os.path.dirname(SRC_ROOT)
PROJECT_ROOT = SRC_ROOT
sys.path.insert(0, SRC_ROOT)
from runtime.env_loader import load_runtime_env  # noqa: E402
from runtime.paths import runtime_log_dir, runtime_state_dir  # noqa: E402
from runtime.research_ledger import (  # noqa: E402
    close_task as close_research_task,
    open_task as open_research_task,
    project_context as research_project_context,
    record_candidates as record_research_candidates,
    record_event as record_research_event,
    review_task as review_research_task,
    update_candidate_stage as update_research_candidate_stage,
)
from runtime.platform_risk import build_manual_interaction_envelope  # noqa: E402
from runtime.mcp_observability import record_fallback_server_stopped, record_server_started, record_server_stopped, record_tool_list, snapshot as mcp_observability_snapshot  # noqa: E402
from runtime.mcp_runtime import source_fingerprint  # noqa: E402
from runtime.redaction import RedactingLogFilter, redact_url  # noqa: E402


load_runtime_env()
os.environ.setdefault("KR_STATE_DIR", str(runtime_state_dir()))
os.environ.setdefault("KR_LOG_DIR", str(runtime_log_dir()))

from runtime.agent_contracts import ResearchReadPayload, EcologyOutcome, ResearchClaim
from runtime.knowledge import ReadRequest, ReadResult
from runtime.knowledge_delivery import ArtifactRequest

from capabilities import ACTUAL_MCP_TOOLS, build_capabilities, build_tool_surface, manual_interaction_manifest, media_policy_manifest, platform_capabilities_dict
from capabilities import capability_atlas_manifest
from capabilities import gh_cli_admission_record
from capabilities import research_quality_contract_manifest
from capabilities import source_ecology_manifest
from capability_manifest.ecologies import candidate_tools as ecology_candidate_tools
from capabilities import validation_semantics_manifest
from academic_providers import AcademicSearchRequest
from academic_providers.service import academic_provider_status, search_academic_metadata
from collectors.platform import (
    extract_bvid,
    get_bilibili_info,
    legacy_search_bilibili,
    xiaohongshu_account_state,
)
from collectors.platform import zhihu as zhihu_collectors
from collectors.platform import xiaohongshu as xhs_collectors
from collectors.platform import gh_cli_sidecar
from collectors.platform import youtube as youtube_collectors
from kr_core import (
    DecisionLogEvent,
    DecisionLogger,
    DetailRequest,
    DetailResponse,
    SearchRequest,
    registry,
)
from runtime.recruitment_collection import format_search_response as format_collection_search_response
from generic_web import GenericWebRequest, collect_dynamic_url, collect_url
from search_providers.catalog import SearchProviderName
from platform_adapters.boss_adapter import search_from_request as _boss_search
from platform_adapters.liepin_adapter import search_from_request as _liepin_search
from platform_adapters.maimai_adapter import search_from_request as _maimai_search
from platform_adapters.v2ex_adapter import search_from_request as _v2ex_search
from runtime.platform_execution import ensure_bindings
from research_planner import build_research_plan
from routing import attach_routing_metadata, routing_recommends_l2, routing_snapshot
from routing.calibration import build_calibration_report
from runtime.chrome_manager import (
    background_chrome,
    bring_chrome_to_front,
    chrome_runtime_summary,
    chrome_runtime_quick_summary,
    cancel_browser_interaction,
    complete_browser_interaction,
    finish_chrome_automation,
    managed_browser_platforms,
    probe_browser_auth,
    request_browser_interaction,
    restore_chrome_idle_cleanups,
    _chrome_debug_port,
    _chrome_debug_url,
    _cleanup_managed_chrome_platform,
    _ensure_chrome_debugging,
    _find_chrome_exe,
)
from runtime.browser_sessions import browser_sessions_summary, compact_terminal_browser_sessions
from runtime.recruitment_governance import (
    LEASED_BROWSER_PLATFORMS,
    acquire_platform_lease,
    check_platform_admission,
    release_platform_lease,
)
from kr_core.evidence_store import EvidenceStore
from kr_core.search_cache import SearchCache
from search_providers.baseline import snapshot_from_response
from runtime.degradation import get_degradation_policy
from runtime.admission import classify_admission_state
from runtime.browser_channel import browser_channel_summary
from runtime.channel_admission import build_channel_admission_summary
from runtime.cost_latency import (
    attach_runtime_metadata,
    budget_manifest,
    cache_registry_summary,
    capability_cost_profiles,
    get_ttl_cache,
    governed_call,
    stable_key,
)
from runtime.health_checks import HealthCheckDeps, HealthCheckService
from runtime.monitor import get_monitor_tracker, sample_runtime_snapshot
from runtime.failure_tags import detect_failure_tags
from runtime.failure_cache import detail_failure_cache
from runtime.native_runner import (
    build_low_risk_execution_command,
    compact_patrol_contract,
    governed_capability_plan_summary,
    l2_multimodal_task_contract,
    low_risk_execution_summary,
    run_low_risk_execution_command,
    run_readonly_patrol,
    runtime_contract_summary,
)
from runtime.openclaw_native_adapter import openclaw_native_adapter_summary
from runtime.profile_registry import profile_registry_summary
from runtime.project_state import project_governance_manifest
from runtime.runtime_environment import planning_tools_enabled
from runtime.summary_helpers import (
    decision_logs_compact_from_summary,
    low_risk_execution_probe_declaration,
    overall_status_from_summary,
    task_status_compact_from_summary,
)
from runtime.status_schema import aggregate_validation_status, canonical_status_counts, classify_runtime_payload, legacy_health_status
from runtime.tool_trace import current_tool_trace, get_tool_trace_recorder, set_current_tool_trace, traced_tool, privacy_fingerprint
from runtime.xhs_health import get_xhs_detail_health_tracker
from runtime.xhs_health import get_xhs_chain_health_tracker
from runtime.tasks import compact_task_ref, compact_task_refs, get_task_store
from runtime.task_scope import SERVER_RUN_ID
from runtime.usage_tracker import get_usage_tracker
from search_providers import WebSearchRequest, provider_status, search_web
from media_direct_url import (
    BILIBILI_HEADERS,
    bilibili_direct_candidate_with_ytdlp,
    build_direct_media_probe,
    youtube_watch_url_candidate,
)
from understanding import attach_detail_evidence, build_detail_evidence
from understanding import (
    deep_analyze_bilibili,
    filter_bilibili_comments,
    get_bilibili_comments,
    strip_html_text as understanding_strip_html_text,
    transcribe_bilibili,
)
from collectors.platform.bilibili import get_bilibili_transcript_metadata
from collectors.platform.youtube import deep_analyze_youtube






# ── 日志 ────────────────────────────────────────────────────────────────
def _runtime_log_dir() -> str:
    openclaw_home = os.environ.get("OPENCLAW_STATE_DIR") or os.environ.get("OPENCLAW_HOME")
    if not openclaw_home:
        candidate = os.path.join(os.path.expanduser("~"), ".openclaw")
        if os.path.isdir(candidate):
            openclaw_home = candidate
    return (
        os.environ.get("KR_LOG_DIR")
        or os.path.join(openclaw_home or REPO_ROOT, "logs", "runtime")
    )


RUNTIME_LOG_DIR = _runtime_log_dir()
MCP_SERVER_LOG_PATH = os.path.join(RUNTIME_LOG_DIR, "knowledgeradar-mcp-server.log")
DECISION_LOG_PATH = os.path.join(RUNTIME_LOG_DIR, "knowledgeradar-decisions.jsonl")
XHS_DEFAULT_SEARCH_LIMIT = int(os.environ.get("KR_XHS_DEFAULT_SEARCH_LIMIT", "10"))
XHS_PROBE_SEARCH_LIMIT = int(os.environ.get("KR_XHS_PROBE_SEARCH_LIMIT", "1"))
XHS_DETAIL_SUCCESS_RATE_DISABLE_THRESHOLD = float(os.environ.get("KR_XHS_DETAIL_DISABLE_SUCCESS_RATE", "0.3"))
decision_logger = DecisionLogger(DECISION_LOG_PATH)
evidence_store = EvidenceStore()
search_cache = SearchCache(ttl_s=int(os.environ.get("KR_SEARCH_CACHE_TTL_S", "180")))


def _configure_logging() -> logging.Logger:
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(level=logging.INFO, format="[mcp-server] %(message)s")
    else:
        root.setLevel(logging.INFO)
    # httpx/httpcore INFO records include full request URLs. Some provider APIs
    # carry credentials in query parameters, so keep transport logs quiet.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    for handler in root.handlers:
        if not any(isinstance(item, RedactingLogFilter) for item in handler.filters):
            handler.addFilter(RedactingLogFilter())

    try:
        os.makedirs(RUNTIME_LOG_DIR, exist_ok=True)
        if not any(getattr(handler, "_kr_runtime_file", False) for handler in root.handlers):
            file_handler = RotatingFileHandler(
                MCP_SERVER_LOG_PATH,
                maxBytes=5 * 1024 * 1024,
                backupCount=5,
                encoding="utf-8",
            )
            file_handler.setFormatter(logging.Formatter("[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s"))
            file_handler.addFilter(RedactingLogFilter())
            file_handler._kr_runtime_file = True  # type: ignore[attr-defined]
            root.addHandler(file_handler)
    except Exception as e:
        logging.getLogger("mcp-server").warning(f"运行日志归档初始化失败: {e}")

    logger = logging.getLogger("mcp-server")
    logger.info(f"运行日志归档: {MCP_SERVER_LOG_PATH}")
    return logger


log = _configure_logging()

_LAST_HEALTH_LAYERS: Dict[str, Dict] = {}


def _record_detail_decision(request: DetailRequest, response: DetailResponse, elapsed_s: float) -> None:
    """Best-effort detail decision audit trail; never changes tool behavior."""
    try:
        data = response.data if isinstance(response.data, dict) else {}
        evidence = response.evidence.to_mcp_dict() if response.evidence else data.get("evidence") or {}
        success = not bool(data.get("error") or response.error)
        trace = current_tool_trace()
        if trace:
            trace = {
                **trace,
                "elapsed_s": round(elapsed_s, 3),
                "status": "ok" if success else "failed",
                "failure_code": trace.get("failure_code") or ("error" if not success else ""),
            }
        failure_tags = detect_failure_tags(data.get("error"), response.error, response.metadata if not success else {})
        decision_logger.record(
            DecisionLogEvent(
                event_type="detail_extract",
                platform=response.platform,
                url=request.url,
                success=success,
                strategy=str((response.metadata or {}).get("strategy") or ""),
                routing=routing_snapshot(data),
                evidence=evidence if isinstance(evidence, dict) else {},
                error=str(data.get("error") or response.error or ""),
                elapsed_s=round(elapsed_s, 3),
                trace=trace,
                failure_tags=failure_tags,
                health_layers=_LAST_HEALTH_LAYERS.get(response.platform, {}),
                metadata={
                    "auto_multimodal": request.auto_multimodal,
                    "enable_deep_analysis": request.enable_deep_analysis,
                    "enable_comment_filtering": request.enable_comment_filtering,
                    "content_chars": len(str(data.get("content") or "")),
                    "transcript_chars": len(str(data.get("transcript") or "")),
                    "image_count": len(data.get("images") or []) if isinstance(data.get("images"), list) else 0,
                    "comment_count": len(data.get("comments") or []) if isinstance(data.get("comments"), list) else 0,
                },
            )
        )
    except Exception as exc:
        log.debug(f"详情决策日志写入失败: {exc}")


def _infer_detail_platform(url: str) -> str:
    registered = registry.platform_for_url(url)
    if registered:
        return registered
    if "zhipin.com" in url:
        return "BOSS直聘"
    if "liepin.com" in url:
        return "猎聘"
    if "zhaopin.com" in url:
        return "智联招聘"
    if "maimai.cn" in url:
        return "脉脉"
    if "v2ex.com" in url:
        return "V2EX"
    if "zhihu.com" in url or "zhuanlan.zhihu.com" in url:
        return "知乎"
    if "bilibili.com" in url or re.search(r"\b(?:BV[a-zA-Z0-9]{10,12}|av\d+)\b", url, flags=re.IGNORECASE):
        if extract_bvid(url):
            return "B站"
    if "youtube.com" in url or "youtu.be" in url or youtube_collectors.extract_youtube_video_id(url):
        return "YouTube"
    return ""


# ── MCP 服务器实例 ─────────────────────────────────────────────────────
MCP_HOST = os.environ.get("KR_MCP_HOST", "127.0.0.1")
MCP_PORT = int(os.environ.get("KR_MCP_PORT", "18765"))
MCP_TRANSPORT = os.environ.get("KR_MCP_TRANSPORT", "streamable-http").strip().lower()
MCP_STREAMABLE_HTTP_PATH = os.environ.get("KR_MCP_PATH", "/mcp")
MCP_SSE_PATH = os.environ.get("KR_MCP_SSE_PATH", "/sse")
MCP_MESSAGE_PATH = os.environ.get("KR_MCP_MESSAGE_PATH", "/messages/")


# ── API 配置 ────────────────────────────────────────────────────────────
BILI_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://search.bilibili.com/",
}

VENV_PYTHON = os.path.join(REPO_ROOT, ".python312", "python.exe")
_NODE_PATH = None


def _record_search_evidence(platform: str, query: str, response: Dict) -> None:
    try:
        evidence_store.append_search(platform=platform, query=query, response=response)
    except Exception as exc:
        response.setdefault('evidence_storage_gaps', []).append({'part':'repository','reason':type(exc).__name__,'retry':'resubmit_fixed_result_without_search'})
        log.debug(f"搜索证据仓库写入失败: platform={platform}, error={type(exc).__name__}")


def _record_detail_evidence(platform: str, url: str, response: Dict) -> None:
    try:
        evidence_store.record_detail(platform=platform, url=url, response=response)
    except Exception as exc:
        response.setdefault('evidence_storage_gaps', []).append({'part':'repository','reason':type(exc).__name__,
            'retry':'resubmit_fixed_result_without_fetch'})


def _record_academic_evidence(query: str, response: Dict) -> None:
    try:
        evidence_store.append_academic_search(query=query, response=response)
    except Exception as exc:
        response.setdefault('evidence_storage_gaps', []).append({'part': 'repository', 'reason': type(exc).__name__})
        log.debug(f"学术证据仓库写入失败: error={exc}")














def _xhs_route_receipt(result: Dict, *, run_id: str) -> Dict:
    metadata = result.setdefault("metadata", {})
    metadata["xhs_route_receipt"] = {"schema": "knowledgeradar-xhs-search-route-receipt/v2",
        "run_id": run_id, "transport": "opencli", "ownership": "borrowed",
        "fallback_used": bool(metadata.get("fallback_used")),
        "terminal_status": "failed" if result.get("error") else "ok"}
    return result











def _xhs_budget_state() -> Dict:
    from runtime.xhs_tikhub_usage import tikhub_usage_summary
    return {"search": {"max_limit": XHS_DEFAULT_SEARCH_LIMIT},
        "paid_fallback": tikhub_usage_summary(), "multimodal": {"default_auto_multimodal": False,
        "ocr": "siliconflow_paddleocr_vl_1.5", "asr": "local_qwen3_0.6b"}}


def _xhs_diagnostic_control_state() -> Dict:
    from runtime.xhs_opencli import connection_status
    return connection_status()


def _detail_degradation_metadata(platform: str, data: Dict) -> Dict:
    if not isinstance(data, dict) or not data.get("error"):
        return {}
    failure_type = str(data.get("failure_type") or data.get("error_type") or "detail_error")
    manual_action_required = bool(data.get("manual_action_required"))
    retryable = failure_type not in {"dead_link", "unsupported_url", "not_found"}
    admission = classify_admission_state(data)
    return {
        "status": "degraded",
        "platform": platform,
        "failure_type": failure_type,
        "platform_state": data.get("platform_state") or "",
        "manual_action_required": manual_action_required,
        "retryable": retryable and not manual_action_required,
        "admission_state": admission,
        "reason": str(data.get("error") or "")[:240],
        "hint": str(data.get("hint") or "")[:240],
    }


def _attach_detail_degradation(platform: str, url: str, data: Dict, metadata: Dict) -> Dict:
    degradation = _detail_degradation_metadata(platform, data)
    if not degradation:
        return metadata
    next_metadata = dict(metadata or {})
    next_metadata["degradation"] = degradation
    next_metadata["admission_state"] = degradation.get("admission_state") or {}
    try:
        get_degradation_policy().record_degradation(
            "detail_strategy",
            f"{platform}:{degradation.get('failure_type')}",
            degradation.get("reason") or "detail degraded",
            {"url": url, **degradation},
        )
    except Exception as exc:
        log.debug(f"详情降级事件写入失败: platform={platform}, error={exc}")
    return next_metadata


_MANUAL_PLATFORM_ALIASES = {
    "boss": "boss",
    "BOSS直聘": "boss",
    "liepin": "liepin",
    "猎聘": "liepin",
    "maimai": "maimai",
    "脉脉": "maimai",
    "zhihu": "zhihu",
    "知乎": "zhihu",
    "xhs": "xhs",
    "小红书": "xhs",
}


def _promote_manual_interaction_result(
    result: Dict,
    *,
    platform: str,
    original_tool: str,
    original_args: Dict | None = None,
) -> Dict:
    """Expose uniform NEEDS_INTERACTION metadata when a tool already declares manual action."""

    if not isinstance(result, dict):
        return result
    error = result.get("error")
    if not isinstance(error, dict):
        return result
    admission = classify_admission_state(error)
    if admission.get("status_class") != "NEEDS_INTERACTION":
        return result
    platform_id = _MANUAL_PLATFORM_ALIASES.get(platform, platform)
    manual_confidence = str(error.get("manual_confidence") or "").lower()
    if platform_id == "liepin" and manual_confidence and manual_confidence != "confirmed":
        downgraded = dict(error)
        downgraded["status_class"] = "EXPECTED_DEGRADED"
        downgraded["manual_action_required"] = False
        downgraded["expected_degraded"] = True
        downgraded["retryable"] = True
        downgraded["admission_state"] = {
            **admission,
            "status_class": "EXPECTED_DEGRADED",
            "manual_action_required": False,
            "retryable": True,
            "expected_degraded": True,
            "reason_key": str(downgraded.get("failure_type") or downgraded.get("platform_state") or "ambiguous_page_state"),
        }
        next_result = dict(result)
        next_result["error"] = downgraded
        metadata = dict(next_result.get("metadata") or {})
        metadata["status_class"] = "EXPECTED_DEGRADED"
        metadata["admission_state"] = downgraded["admission_state"]
        metadata["expected_degraded"] = True
        next_result["metadata"] = metadata
        return next_result
    enriched = dict(error)
    enriched["status_class"] = "NEEDS_INTERACTION"
    enriched["admission_state"] = admission
    enriched["manual_action_required"] = True
    enriched["expected_degraded"] = False
    enriched["retryable"] = False
    enriched.setdefault(
        "manual_interaction_envelope",
        build_manual_interaction_envelope(
            platform=platform_id,
            reason_code=str(enriched.get("platform_state") or enriched.get("failure_type") or enriched.get("type") or "manual_action_required"),
            original_tool=original_tool,
            original_args=original_args or {},
        ),
    )
    next_result = dict(result)
    next_result["error"] = enriched
    metadata = dict(next_result.get("metadata") or {})
    metadata["status_class"] = "NEEDS_INTERACTION"
    metadata["admission_state"] = admission
    metadata["expected_degraded"] = False
    next_result["metadata"] = metadata
    if platform_id == 'zhihu':
        # 风控页不自动导航到登录页；由Agent显式请求人工处理。
        return next_result
    return _request_manual_interaction_for_result(
        next_result,
        platform=platform_id,
        original_tool=original_tool,
        original_args=original_args or {},
    )


def _request_manual_interaction_for_result(
    result: Dict,
    *,
    platform: str,
    original_tool: str,
    original_args: Dict | None = None,
) -> Dict:
    """Route an observed login failure through the single account recovery owner."""

    if not isinstance(result, dict):
        return result
    error = result.get("error")
    if not isinstance(error, dict):
        return result
    if str(error.get("status_class") or "") != "NEEDS_INTERACTION":
        return result
    metadata = dict(result.get("metadata") or {})
    if metadata.get("manual_interaction_request") or error.get("manual_interaction"):
        return result

    platform_id = _MANUAL_PLATFORM_ALIASES.get(platform, platform)
    manual_confidence = str(error.get("manual_confidence") or "").lower()
    if platform_id == "liepin" and manual_confidence and manual_confidence != "confirmed":
        skipped = {
            "status": "skipped",
            "reason": "manual_confidence_not_confirmed",
            "platform": platform_id,
            "manual_confidence": manual_confidence,
            "original_tool": original_tool,
        }
        metadata["manual_interaction_request"] = skipped
        result["metadata"] = metadata
        return result
    reason = str(
        error.get("platform_state")
        or error.get("failure_type")
        or error.get("type")
        or "manual_action_required"
    )
    if platform_id not in set(managed_browser_platforms()) | {'boss', 'liepin', 'maimai', 'zhihu'}:
        skipped = {
            "status": "skipped",
            "reason": "platform_not_managed",
            "platform": platform_id,
            "original_tool": original_tool,
        }
        metadata["manual_interaction_request"] = skipped
        result["metadata"] = metadata
        return result

    target_profile_id = str(error.get("profile_id") or "")
    if platform_id == "xhs" and not target_profile_id:
        skipped = {
            "status": "skipped",
            "reason": "xhs_profile_binding_required",
            "platform": platform_id,
            "original_tool": original_tool,
            "detail": "旧风险记录没有指定账号，已保留待办提示但不会自动弹出浏览器。",
        }
        metadata["manual_interaction_request"] = skipped
        result["metadata"] = metadata
        return result
    if error.get("gate_status") or error.get("cooldown_seconds_remaining") or error.get("last_reason"):
        interaction = {"status": "skipped", "reason": "historical_gate_requires_current_account_evidence", "platform": platform_id}
    else:
        interaction = request_browser_interaction(
            platform_id, reason, target_profile_id=target_profile_id,
            trigger_evidence=[f"observed_tool={original_tool}", f"observed_failure={reason}"],
            source=original_tool,
        )
        interaction["original_tool"] = original_tool
        interaction["manual_open_mode"] = f"health_check(mode='request_browser_interaction:{platform_id}:{reason}')"

    enriched = dict(error)
    enriched["manual_interaction"] = interaction
    metadata["manual_interaction_request"] = interaction
    next_result = dict(result)
    next_result["error"] = enriched
    next_result["metadata"] = metadata
    return next_result


def _cached_search(
    platform: str,
    query: str,
    limit: int,
    fn,
    *,
    search_type: str = "",
    provider: str = "",
    freshness: str = "",
    include_raw_content: bool = False,
    language: str = "",
    options: Dict | None = None,
) -> Dict:
    key = search_cache.key(
        platform=platform,
        query=query,
        limit=limit,
        search_type=search_type,
        provider=provider,
        freshness=freshness,
        include_raw_content=include_raw_content,
        language=language,
        options=options or {},
    )
    def execute():
        started = time.perf_counter()
        result = fn()
        if isinstance(result, dict) and not result.get("error"):
            result.setdefault("metadata", {})["search_quality_snapshot"] = snapshot_from_response(
                query, result, elapsed_ms=float((result.get("metadata") or {}).get("elapsed_ms") or
                                               (time.perf_counter()-started)*1000))
        return result
    return search_cache.run(key, execute)







# ═══════════════════════════════════════════════════════════════════════
# 工具 1: expand_keywords
# ═══════════════════════════════════════════════════════════════════════

@traced_tool("expand_keywords", strategy="deterministic_keyword_templates")
def expand_keywords(topic: Annotated[str, Field(description="待拆解的主题文本。返回若干可直接提交给搜索工具的短查询词。")]) -> List[str]:
    """生成多组搜索查询词。

    返回围绕同一主题的短查询词列表，不访问外部网页，不读取详情页。
    这是 legacy planning helper；可通过环境开关从 MCP 工具面隐藏。
    """
    log.info("expand_keywords: topic_fingerprint=%s", privacy_fingerprint(topic))
    from expand_keywords import expand_keywords as _expand
    plan_keywords = build_research_plan(topic).keywords
    legacy_keywords = _expand(topic)
    keywords = []
    seen = set()
    for keyword in plan_keywords + legacy_keywords:
        if keyword and keyword not in seen:
            seen.add(keyword)
            keywords.append(keyword)
        if len(keywords) >= 8:
            break
    log.info(f"  -> {len(keywords)} 个搜索词")
    return keywords


# ═══════════════════════════════════════════════════════════════════════
# 工具 1.5: plan_research
# ═══════════════════════════════════════════════════════════════════════

@traced_tool("plan_research", strategy="rule_based_planner")
def plan_research(topic: Annotated[str, Field(description="待规划的主题文本。返回搜索动作草案，不执行搜索。")]) -> Dict:
    """生成结构化检索计划。

    返回查询词、候选信息形态和详情抽取提示，不访问外部网页，不读取详情页。
    这是 legacy planning helper；可通过环境开关从 MCP 工具面隐藏。
    """
    log.info(f"plan_research: {topic}")
    plan = build_research_plan(topic).to_dict()
    log.info(f"  -> {len(plan.get('searches') or [])} 个计划搜索动作")
    return plan


if planning_tools_enabled():
    application_tool()(expand_keywords)
    application_tool()(plan_research)


# ═══════════════════════════════════════════════════════════════════════
# 工具 1.6: analyze_decision_logs
# ═══════════════════════════════════════════════════════════════════════

@application_tool()
@traced_tool("analyze_decision_logs", strategy="decision_log_summary")
def analyze_decision_logs(
    limit: Annotated[int, Field(description="读取最近决策日志条数。", ge=1, le=500)] = 50,
    compact: Annotated[bool, Field(description="为 true 时只返回聚合摘要和少量错误样例。")] = False,
) -> Dict:
    """汇总最近详情提取决策日志。

    返回路由、证据、错误、平台稳定性和少量失败样例。
    只读；不修改阈值、不重跑抓取，也不触发多模态处理。
    """
    limit = max(1, min(int(limit or 50), 500))
    log.info(f"analyze_decision_logs: limit={limit}, compact={compact}")
    summary = decision_logger.summarize(limit)
    if not compact:
        return summary
    return _decision_logs_compact_from_summary(summary)


def _decision_logs_compact(limit: int = 50) -> Dict:
    limit = max(1, min(int(limit or 50), 500))
    return _decision_logs_compact_from_summary(decision_logger.summarize(limit))


def _decision_logs_compact_from_summary(summary: Dict) -> Dict:
    return decision_logs_compact_from_summary(summary)


@application_tool( group="runtime", read_only=True)
@traced_tool("get_task_status", strategy="runtime_task_store")
def get_task_status(
    task_id: Annotated[str, Field(description="任务 ID。仅查询；取消或恢复用 control_task。summary/compact返回摘要。")] = "",
    limit: Annotated[int, Field(description="返回最近任务数量。", ge=1, le=100)] = 20,
    compact: Annotated[bool, Field(description="为 true 时返回聚合摘要，不返回完整最近任务列表。")] = False,
    wait: Annotated[bool, Field(description="为 true 时等待指定 task/scope/source 的阻塞任务进入终态；可能变慢。")] = False,
    max_wait_s: Annotated[float, Field(description="wait=true 时最长等待秒数；0 表示使用运行时默认值。", ge=0, le=600)] = 0.0,
    research_session_id: Annotated[str, Field(description="旧研究会话别名。普通调用留空；保留兼容。")] = "",
    work_scope_id: Annotated[str, Field(description="工作作用域 ID。由上游详情调用返回；普通调用留空。")] = "",
    task_scope_id: Annotated[str, Field(description="任务作用域 ID。由上游详情调用返回；普通调用留空。")] = "",
    source_url: Annotated[str, Field(description="源内容 URL。可用于等待绑定到该来源的后台任务。")] = "",
    content_id: Annotated[str, Field(description="平台内容 ID。可用于等待绑定到该内容的后台任务。")] = "",
    research_action: Annotated[Literal["", "list", "inspect", "files", "evidence_search", "evidence_fetch"], Field(description="可选只读查询：list/inspect/files读取kr_research同一账本的任务、详情或产物；evidence_search/evidence_fetch读取已保存固定证据。空值沿用后台任务查询，不负责启动或完成研究。")] = "",
    research_payload: Annotated[ResearchReadPayload | None, Field(description="只读查询参数：query/limit或version_id/start/max_chars；inspect/files使用task_id。")] = None,
    receipt_request_id: Annotated[str, Field(description="只读查询公共执行回执；recent列出最近动作，否则传原request_id。", max_length=128)] = '',
) -> TaskStatusResult:
    """查询后台理解任务状态。

    不传 task_id 时返回最近任务列表；传入 task_id 时返回单个任务详情。
    只读；可等待绑定范围内的阻塞任务进入终态。
    wait=true 可能阻塞到 max_wait_s。
    """
    if receipt_request_id:
        from runtime.execution_owner import OwnerClient
        client = OwnerClient()
        return client.receipts(limit) if receipt_request_id == 'recent' else client.receipt(receipt_request_id)
    if research_action:
        from runtime.research_workspace import ResearchRecords, research_action as read_research
        data = research_payload.model_dump() if isinstance(research_payload, ResearchReadPayload) else research_payload
        return read_research(ResearchRecords(), research_action, task_id, payload=data)
    store = get_task_store()
    limit = max(1, min(int(limit or 20), 100))
    task_id = str(task_id or "").strip()
    if task_id.startswith(("retry:", "cancel:")):
        raise ValueError("task_command_required_use_control_task")
    cleanup = {"cleaned_count": 0, "cleaned": [], "reason": "read_only_runtime_maintenance"}
    session_id = str(research_session_id or "").strip()
    scope_wait_requested = any(str(value or "").strip() for value in (task_scope_id, work_scope_id, source_url, content_id, task_id))
    if wait and (scope_wait_requested or session_id):
        wait_kwargs = {
            "max_wait_s": float(max_wait_s or os.environ.get("KR_FINALIZE_MAX_WAIT_S", "120")),
            "poll_s": float(os.environ.get("KR_FINALIZE_POLL_S", "1")),
            "blocking_only": True,
        }
        if scope_wait_requested:
            wait_result = store.wait_for_scope(
                task_scope_id=str(task_scope_id or "").strip(),
                work_scope_id=str(work_scope_id or "").strip(),
                source_url=str(source_url or "").strip(),
                content_id=str(content_id or "").strip(),
                task_id=str(task_id or "").strip(),
                **wait_kwargs,
            )
            wait_binding = {
                "schema_version": wait_result.get("schema_version"),
                "status": wait_result.get("status"),
                "task_scope_id": str(task_scope_id or "").strip(),
                "work_scope_id": str(work_scope_id or "").strip(),
                "source_url": str(source_url or "").strip(),
                "content_id": str(content_id or "").strip(),
                "task_id": str(task_id or "").strip(),
                "waited_s": wait_result.get("waited_s"),
                "blocking_only": True,
                "binding": "task_scope_fanin",
            }
        else:
            wait_result = store.wait_for_session(
                session_id,
                max_wait_s=wait_kwargs["max_wait_s"],
                poll_s=wait_kwargs["poll_s"],
                blocking_only=True,
            )
            wait_binding = {
                "schema_version": wait_result.get("schema_version"),
                "status": wait_result.get("status"),
                "research_session_id": session_id,
                "legacy_alias": True,
                "waited_s": wait_result.get("waited_s"),
                "blocking_only": True,
                "binding": "legacy_research_session_fanin",
            }
        response = {
            "schema_version": "knowledgeradar-task-status/v2",
            "status": "ok",
            "server_run_id": SERVER_RUN_ID,
            "wait": wait_binding,
            "tasks": compact_task_refs(wait_result.get("tasks") or [], limit=limit),
            "pending": compact_task_refs(wait_result.get("pending") or [], limit=limit),
            "terminal": compact_task_refs(wait_result.get("terminal") or [], limit=limit),
            "stale_cleanup": {
                "cleaned_count": cleanup.get("cleaned_count", 0),
                "cleaned": compact_task_refs(cleanup.get("cleaned") or [], limit=3),
                "reason": cleanup.get("reason", ""),
            },
        }
        if task_id.startswith("durable-"):
            from runtime.task_service import TaskService
            response["durable_result"] = TaskService(store).result(task_id)
        return response
    if str(task_id or "").strip().lower() in {"summary", "compact"}:
        compact = True
        task_id = ""
    if task_id:
        if task_id.startswith("durable-"):
            from runtime.task_service import TaskService
            return TaskService(store).result(task_id)
        task = store.get_task(task_id)
        if not task:
            return {"status": "not_found", "task_id": task_id}
        return {
            "schema_version": "knowledgeradar-task-status/v2",
            "status": "ok",
            "task": compact_task_ref(task),
            "stale_cleanup": {
                "cleaned_count": cleanup.get("cleaned_count", 0),
                "cleaned": compact_task_refs(cleanup.get("cleaned") or [], limit=3),
                "reason": cleanup.get("reason", ""),
            },
        }
    summary = store.summary(recent_limit=limit)
    stale = store.stale_tasks(limit=limit)
    if compact:
        result = _task_status_compact_from_summary(summary, stale)
        result["stale_cleanup"] = {
            "cleaned_count": cleanup.get("cleaned_count", 0),
            "cleaned": compact_task_refs(cleanup.get("cleaned") or [], limit=3),
            "reason": cleanup.get("reason", ""),
        }
        return result
    return {
        "schema_version": "knowledgeradar-task-status/v2",
        "status": "ok",
        "tasks": compact_task_refs(store.recent_tasks(limit)),
        "summary": {
            "total": summary.get("total"),
            "counts": summary.get("counts", {}),
            "active": summary.get("active"),
            "active_oldest_age_s": summary.get("active_oldest_age_s"),
            "stale_count": summary.get("stale_count"),
            "by_platform": summary.get("by_platform", []),
            "by_task_type": summary.get("by_task_type", []),
            "by_error_code": summary.get("by_error_code", []),
            "unknown_error_count": summary.get("unknown_error_count", 0),
        },
        "recent_failed": compact_task_refs(summary.get("recent_failed") or [], limit=5),
        "stale": compact_task_refs(stale, limit=5),
        "stale_cleanup": {
            "cleaned_count": cleanup.get("cleaned_count", 0),
            "cleaned": compact_task_refs(cleanup.get("cleaned") or [], limit=3),
            "reason": cleanup.get("reason", ""),
        },
    }


def _task_status_compact(limit: int = 20) -> Dict:
    store = get_task_store()
    limit = max(1, min(int(limit or 20), 100))
    cleanup = {"cleaned_count": 0, "cleaned": [], "reason": "read_only_runtime_maintenance"}
    result = _task_status_compact_from_summary(store.summary(recent_limit=limit), store.stale_tasks(limit=limit))
    result["stale_cleanup"] = {
        "cleaned_count": cleanup.get("cleaned_count", 0),
        "cleaned": compact_task_refs(cleanup.get("cleaned") or [], limit=3),
        "reason": cleanup.get("reason", ""),
    }
    return result


def _task_status_compact_from_summary(summary: Dict, stale: List[Dict]) -> Dict:
    return task_status_compact_from_summary(summary, stale)


# ═══════════════════════════════════════════════════════════════════════
# 工具 1.7: kr_research
# ═══════════════════════════════════════════════════════════════════════

_RESEARCH_MODE_VALUES = {"plan_only", "first_wave", "deep_route"}
_RESEARCH_BUDGET_VALUES = {"fast", "balanced", "deep", "diagnostic"}
_FIRST_WAVE_SAFE_ECOLOGIES = {
    "generic_web_ecology",
    "academic_literature_ecology",
    "github_repository_ecology",
    "youtube_video_ecology",
    "wechat_public_article_ecology",
}
_HOST_INTERNAL_WEB_WAVE = {
    "schema": "knowledgeradar-host-internal-web-wave/v1",
    "wave_id": "host_internal_web_wave",
    "strategy_tree": "web_search.provider_wave",
    "source_ecology": "generic_web_ecology",
    "candidate_provider_ids": ["codex_builtin_web_search", "codex_builtin_web_fetch"],
    "admission": "agent_selected_no_prior_route_required",
    "status": "host_agent_invoked",
    "relationship_to_kr": "host discovery complements KR acquisition and fixed evidence",
    "usage_rule": "The Agent may search directly, then optionally return selected results with real host references; no KR pre-registration is needed.",
    "evidence_record_fields": ["wave_id", "strategy_tree", "source_ecology", "relationship_to_kr", "reason"],
}


def _split_hint_list(value: str) -> list[str]:
    tokens = re.split(r"[,，;\s]+", str(value or "").strip())
    return [token for token in tokens if token]


def _research_ecology_cards(ecology_ids: list[str]) -> list[Dict]:
    ecologies = source_ecology_manifest().get("ecologies") or {}
    cards = []
    for ecology_id in ecology_ids:
        item = ecologies.get(ecology_id) or {}
        if not item:
            continue
        cards.append(
            {
                "id": ecology_id,
                "label": item.get("label"),
                "candidate_tools": item.get("candidate_tools", []),
                "evidence_role": item.get("evidence_role"),
                "known_limits": item.get("known_limits", []),
                "reveals": item.get("reveals", [])[:2],
                **{key: item.get(key, []) for key in ('not_limited_to', 'content_forms', 'query_archetypes')},
                "declared_boundary": item.get('declared_boundary', ''),
            }
        )
    return cards


def _research_ecology_consideration_map(task: str, hints: list[str], evidence_needs: str) -> list[Dict]:
    """Expose every known ecology as a candidate without prescribing a route.

    Keyword relevance and explicit hints only explain why an ecology may be
    useful in the first wave.  They must never erase the rest of the ecology
    map: an Agent can widen, narrow, or revisit candidates after it sees real
    evidence.  This is deliberately a planning aid, not a tool router.
    """
    ecologies = source_ecology_manifest().get("ecologies") or {}
    haystack = f"{task} {evidence_needs}".lower()
    considered: list[Dict] = []
    for ecology_id, ecology in ecologies.items():
        reasons: list[str] = []
        if ecology_id == "generic_web_ecology":
            reasons.append("broad_discovery_baseline")
        if ecology_id in hints:
            reasons.append("explicit_hint")
        if any(token.lower() in haystack for token in ecology.get("discovery_signals", [])):
            reasons.append("task_or_evidence_need_signal")
        tools = list(ecology.get("candidate_tools") or [])
        detail_tools = list(ecology.get("detail_tools", []))
        considered.append(
            {
                "source_ecology": ecology_id,
                "status": "initial_candidate",
                "relevance_signals": reasons or ["available_for_model_consideration"],
                "candidate_tools": tools,
                "detail_affordance": detail_tools,
                "unique_information_value": list(ecology.get("reveals") or [])[:2],
                "known_limits": list(ecology.get("known_limits") or []),
                **{key: ecology.get(key, []) for key in ('not_limited_to', 'content_forms', 'query_archetypes')},
                "declared_boundary": ecology.get('declared_boundary', ''),
                "next_candidate_action": "Agent decides whether to discover, extract detail, cross-check, seek counterevidence, or record a justified skip.",
            }
        )
    return considered


def _infer_research_ecologies(task: str, hints: list[str], evidence_needs: str) -> list[str]:
    considered = _research_ecology_consideration_map(task, hints, evidence_needs)
    # This short list controls only the optional low-risk first wave.  The full
    # consideration map remains available to native Agent reasoning.
    selected = [item["source_ecology"] for item in considered if item["source_ecology"] == "generic_web_ecology" or item["relevance_signals"] != ["available_for_model_consideration"]]
    return selected or ["generic_web_ecology"]


def _research_query(task: str, evidence_needs: str) -> str:
    query = " ".join(str(part or "").strip() for part in (task, evidence_needs) if str(part or "").strip())
    return query[:240] if len(query) > 240 else query


def _research_candidate_records(ecology_id: str, items: list[Any]) -> list[Dict]:
    """Give low-cost discovery hits stable upgrade semantics without routing."""
    ecology = (source_ecology_manifest().get("ecologies") or {}).get(ecology_id) or {}
    detail_tools = [
        tool
        for tool in (ecology.get("candidate_tools") or [])
        if tool in {"extract_web_page", "extract_dynamic_page", "get_content_detail", "get_task_status"}
    ]
    records: list[Dict] = []
    for index, item in enumerate(items, start=1):
        value = item if isinstance(item, dict) else {"title": str(item or "")}
        identity_value = str(value.get("content_id") or value.get("note_id") or value.get("id") or value.get("url") or value.get("title") or "")
        records.append(
            {
                "candidate_id": f"candidate-{hashlib.sha256(f'{ecology_id}|{identity_value}'.encode('utf-8', errors='ignore')).hexdigest()[:16]}",
                "source_ecology": ecology_id,
                "stage": "discovered_candidate",
                "detail_affordance": detail_tools,
                "next_candidate_action": "Agent decides whether this candidate merits detail extraction, identity checking, cross-checking, counterevidence, or a justified skip.",
            }
        )
    return records


def _research_route_plan(
    *,
    task: str,
    mode: str,
    budget: str,
    source_ecology_hints: list[str],
    evidence_needs: str,
) -> Dict:
    ecologies = _infer_research_ecologies(task, source_ecology_hints, evidence_needs)
    consideration_map = _research_ecology_consideration_map(task, source_ecology_hints, evidence_needs)
    route_steps = [
        {
            "step": "capability_handshake",
            "tools": ["health_check", "get_capabilities"],
            "purpose": "Confirm KR runtime and source ecology map before spending evidence budget.",
            "status": "recommended",
        }
    ]
    for ecology_id in ecologies:
        route_steps.append(
            {
                "step": f"discover:{ecology_id}",
                "source_ecology": ecology_id,
                "tools": ecology_candidate_tools(ecology_id),
                "purpose": "Find candidate evidence; upgrade strength only after detail extraction or cross-source validation.",
                "status": "candidate",
                "first_wave_allowed": ecology_id in _FIRST_WAVE_SAFE_ECOLOGIES,
            }
        )
        if ecology_id == "generic_web_ecology":
            route_steps.append(
                {
                    "step": "host_internal_web_wave:generic_web_ecology",
                    "source_ecology": "generic_web_ecology",
                    "tools": ["builtin_web_search", "builtin_web_fetch"],
                    "purpose": "Allow the host Agent's built-in web/search as one web-search provider wave under KR governance when official docs or host-native retrieval is the best finger to use.",
                    "status": "candidate_host_wave",
                    "first_wave_allowed": False,
                    "admission": _HOST_INTERNAL_WEB_WAVE["admission"],
                    "wave_id": _HOST_INTERNAL_WEB_WAVE["wave_id"],
                    "relationship_to_kr": _HOST_INTERNAL_WEB_WAVE["relationship_to_kr"],
                }
            )
    route_steps.append(
        {
            "step": "evidence_closeout",
            "tools": ["get_task_status", "analyze_decision_logs"],
            "purpose": "Check background task completion and runtime degradation before final claims.",
            "status": "recommended_for_deep_reports",
        }
    )
    return {
        "schema": "knowledgeradar-research-route-plan/v1",
        "mode": mode,
        "budget": budget,
        "query": _research_query(task, evidence_needs),
        "selected_source_ecologies": ecologies,
        "source_ecology_cards": _research_ecology_cards(ecologies),
        "considered_source_ecologies": consideration_map,
        "candidate_lifecycle": [
            "discovered_candidate",
            "detail_extracted",
            "identity_checked",
            "cross_checked",
            "counterevidence",
            "degraded_or_blocked",
        ],
        "host_internal_web_wave": _HOST_INTERNAL_WEB_WAVE if "generic_web_ecology" in ecologies else {},
        "route_steps": route_steps,
        "agent_guidance": [
            "Use this as a perception entry, not as a fixed script.",
            "source_ecology_hints and relevance signals only prioritize optional first-wave candidates; they never exclude another ecology from model consideration.",
            "Search results are candidate evidence until detail extraction or cross-source validation.",
            "Official host search may start directly without this route plan. Use KR for special acquisition, durable execution and fixed evidence when useful.",
            "Do not treat shell import server.py as natural MCP tool use in user-facing research tasks.",
        ],
    }


def _research_first_wave(plan: Dict, limit: int, research_task_id: str = "") -> list[Dict]:
    from runtime.research_queries import query_families
    from runtime.research_index import ResearchIndex
    from runtime.task_service import TaskService
    families = plan.get("query_families") or query_families(plan.get("query") or "")
    index, service = ResearchIndex(), TaskService()
    results = []
    selected = plan.get('selected_source_ecologies') or []
    for family in families:
        if family.get('ecology_resolution') == 'unsupported' or family['source_ecology'] not in ['*', *selected]:
            reason = 'unsupported_ecology' if family.get('ecology_resolution') == 'unsupported' else 'ecology_not_selected'
            index.query(research_task_id, {**family, 'reason': reason}, status='skipped')
            results.append({'family_id': family['family_id'], 'source_ecology': family['source_ecology'], 'status':'skipped', 'reason':reason})
    for ecology in plan.get("selected_source_ecologies") or []:
        for family in families:
            if family["source_ecology"] not in {"*", ecology}:
                continue
            family = {**family, "source_ecology": ecology, 'parent_family_id':family['family_id'], "family_id": family["family_id"] + ":" + ecology}
            if ecology not in _FIRST_WAVE_SAFE_ECOLOGIES:
                index.query(research_task_id, family, status="skipped")
                results.append({"source_ecology":ecology,"status":"skipped","reason":"explicit_platform_action_required","family_id":family["family_id"]})
                continue
            index.query(research_task_id,family)
            try:
                task = service.submit("research_query", {"family":family,"limit":max(1,min(limit,8)),"research_task_id":research_task_id}, scope={"research_task_id":research_task_id})
                results.append({"source_ecology":ecology,"status":task["status"],"family_id":family["family_id"],"task":compact_task_ref(task)})
            except Exception as exc:
                index.query(research_task_id,family,status="failed",result={"error":type(exc).__name__})
                results.append({"source_ecology":ecology,"status":"failed","family_id":family["family_id"],"error":type(exc).__name__})
    return results


def execute_research_query(payload):
    """由既有持久任务执行器调用；查询只执行一次，不创建嵌套工作池。"""
    from runtime.research_index import ResearchIndex
    from runtime.execution_context import check_execution
    family, tid = payload["family"], payload["research_task_id"]
    from runtime.research_queries import CURRENT_RESEARCH_ACTION
    import uuid
    family = {**family, 'action_id':family.get('action_id') or family['family_id'], 'attempt_id':uuid.uuid4().hex}
    ecology, query, limit = family["source_ecology"], family["query"], payload["limit"]
    index = ResearchIndex()
    index.query(tid,family,status="running")
    token = CURRENT_RESEARCH_ACTION.set({'_task_id':tid,**{k:family.get(k, '') for k in ('family_id','action_id','attempt_id','question_id','parent_event_id')}})
    try:
        check_execution()
        if ecology == "academic_literature_ecology":
            result = search_academic(query=query,limit=min(limit,5),provider="auto",research_task_id=tid)
        elif ecology == "github_repository_ecology":
            result = search_github_repositories(query=query,limit=limit,research_task_id=tid)
        elif ecology == "youtube_video_ecology":
            result = search_youtube(keyword=query,limit=limit,research_task_id=tid)
        elif ecology == "wechat_public_article_ecology":
            result = search_wechat_articles(query=query,limit=limit,research_task_id=tid)
        elif ecology == "generic_web_ecology":
            result = kr_web_search(query=query,limit=limit,provider="auto",language='' if family.get('language') == 'unknown' else family.get('language', ''),research_task_id=tid)
        else:
            raise ValueError("unsupported_research_query_ecology")
        check_execution()
    except BaseException as exc:
        index.query(tid, family, status='failed', result={'error': {'type': type(exc).__name__}})
        raise
    finally:
        CURRENT_RESEARCH_ACTION.reset(token)
    from runtime.research_index import query_outcome
    index.query(tid,family,status=query_outcome(result),result=result)
    return result


@application_tool()
@traced_tool("kr_research", strategy="workflow_research_perception")
def kr_research(
    task: Annotated[str, Field(description="研究/查证/感知任务目标。返回路线、来源生态和可选第一波证据。")],
    mode: Annotated[Literal["plan_only", "first_wave", "deep_route"], Field(description="plan_only 只规划；first_wave 提交可取消的低风险后台搜索，返回任务句柄；deep_route 返回深度路线但不自动执行高风险详情。")] = "plan_only",
    budget: Annotated[Literal["fast", "balanced", "deep", "diagnostic"], Field(description="预算档位；控制建议深度和首轮数量，不固定工具序列。")] = "balanced",
    source_ecology_hints: Annotated[str, Field(description="可选来源生态提示，逗号分隔，如 academic_literature_ecology,github_repository_ecology。")] = "",
    evidence_needs: Annotated[str, Field(description="可选证据需求，例如官方来源、学术、平台讨论、实现证据、近期性。")] = "",
    limit: Annotated[int, Field(description="first_wave 每个低风险来源返回候选数量。", ge=1, le=10)] = 5,
    research_task_id: Annotated[str, Field(description="可选研究任务 ID。用于持久记录本次路线与后续完成状态；不是 host call-id 或 trace_context。")]= "",
    queries: Annotated[list[ResearchQuery] | None, Field(min_length=1, max_length=40, description="执行者给出的类型化查询族；intent使用公开枚举。默认仅原语种草案，不伪造翻译。")] = None,
    questions: Annotated[list[ResearchQuestion] | None, Field(min_length=1, max_length=20, description="Codex显式选择的必要问题与范围；先登记后搜索，不提供时保留既有范围或标记未知。")] = None,
    display_title: Annotated[str, Field(description="Codex 根据用户目标提供的简短中文任务名，建议12至24字，保留必要产品名；不放工具名、任务ID或执行限制。省略时从原始目标提取。", max_length=80)] = "",
    project_name: Annotated[str, Field(description="可选项目名称，用于研究历史按项目归类；不提供时归入未分类项目。", max_length=80)] = "",
    project_id: Annotated[str, Field(description="Codex明确选择的稳定项目ID，与显示名分离；不得按KR安装目录猜测。", max_length=200)] = "",
) -> ResearchWorkflowResult:
    """高层研究感知入口。

    用于重度研究、查证、治理和跨来源证据任务的第一步。返回 KR 能力握手、
    来源生态路线和可选第一波低风险证据；不会自动启动登录态浏览器、
    不绕过验证码/付费墙，也不会把路线固定成强制工具序列。
    """
    clean_mode = mode if mode in _RESEARCH_MODE_VALUES else "plan_only"
    clean_budget = budget if budget in _RESEARCH_BUDGET_VALUES else "balanced"
    hints = _split_hint_list(source_ecology_hints)
    from runtime.research_ledger import query_fingerprint
    log.info("kr_research: mode=%s budget=%s objective_fingerprint=%s", clean_mode, clean_budget, query_fingerprint(task))
    plan = _research_route_plan(
        task=task,
        mode=clean_mode,
        budget=clean_budget,
        source_ecology_hints=hints,
        evidence_needs=evidence_needs,
    )
    from runtime.research_queries import query_families
    plan["query_families"] = query_families(task, queries)
    ledger = open_research_task(
        objective=task, budget=clean_budget,
        considered=list(plan.get("considered_source_ecologies") or []), task_id=research_task_id,
        display_title=display_title, project_name=project_name, project_id=project_id)
    from runtime.research_index import ResearchIndex, ResearchWrite
    scope_index = ResearchIndex()
    declared_questions = [q if isinstance(q, ResearchQuestion) else ResearchQuestion.model_validate(q) for q in (questions or [])]
    ids = [q.question_id for q in declared_questions]
    if len(ids) != len(set(ids)):
        raise ValueError('duplicate_question_identity')
    for question in declared_questions:
        scope_index.annotate(ResearchWrite(action='question', task_id=ledger['research_task_id'], **question.model_dump()))
    scope_registration = scope_index.read(ledger['research_task_id'], view='feedback')['scope_registration']
    route_receipt_id = ""
    route_trace = current_tool_trace()
    if route_trace.get("trace_id"):
        from runtime.research_ledger import record_tool_receipt
        route_receipt = record_tool_receipt(
            task_id=ledger["research_task_id"],
            trace_id=str(route_trace["trace_id"]),
            tool="kr_research",
            status="ok",
            source_ecology="research_route",
            association="explicit_task_scope",
            result={"route_plan": plan},
        )
        route_receipt_id = str((route_receipt.get("receipt") or {}).get("receipt_id") or "")
    for family in plan["query_families"]:
        from runtime.research_index import ResearchIndex
        ResearchIndex().query(ledger["research_task_id"],family)
        record_research_event(
            task_id=ledger["research_task_id"], kind="query_family_created",
            source_ecology=family["source_ecology"], tool="kr_research",
            language=family["language"], intent_label=family["intent"], query=family["query"],
            parent_event_id=family["parent_event_id"],
            metadata={"mode": clean_mode, "budget": clean_budget, "gap_id": family["gap_id"]},
        )
    completion_rule = "Use finalize_research_task after autonomous research. Every considered ecology requires an explicit outcome. The optional console reads this same ledger; no webpage, claim, lease or reservation is required."
    result: Dict = {
        "schema": "knowledgeradar-research-perception/v1",
        "status": "ok",
        "mode": clean_mode,
        "budget": clean_budget,
        "task": task,
        "capability_handshake": {
            "health_check": {"recommended_first": True, "mode": "summary"},
            "get_capabilities": {"recommended_second": True, "summary": True},
        },
        "route_plan": plan,
        "research_task": {
            "research_task_id": ledger["research_task_id"],
            "display_title": ledger.get("display_title", ""),
            "project_name": ledger.get("project_name", ""),
            "project_id": ledger.get("project_id", ""),
            "project_context": research_project_context(ledger),
            "scope_registration": scope_registration,
            "archive": {"status":"context_pending", "next_action":"manage_research_artifacts.bind_context with explicit local/Vault targets; default summary required"},
            "status": ledger["status"],
            "ledger_schema": ledger["schema"],
            "route_receipt_id": route_receipt_id,
            "completion_rule": completion_rule,
        },
        "first_wave": [],
        "fallback_policy": {
            "builtin_web": "Agent-selected host search needs no prior KR admission. Results may be returned after search with actual references; do not claim KR executed the host search.",
            "server_import": "Use only for local development/tests; user-facing research should prefer MCP tool calls.",
        },
    }
    if clean_mode == "first_wave":
        result["first_wave"] = _research_first_wave(plan, limit=limit, research_task_id=ledger["research_task_id"])
        result["candidate_set"] = [
            candidate
            for wave in result["first_wave"]
            if isinstance(wave, dict)
            for candidate in (wave.get("candidate_set") or [])
            if isinstance(candidate, dict)
        ]
    elif clean_mode == "deep_route":
        result["deep_route"] = {
            "recommended_closeout": ["get_task_status(compact=true)", "analyze_decision_logs(compact=true)"],
            "report_artifacts": ["Evidence Register", "claim evidence chains", "roadmap", "fallback/bypass record"],
            "manual_boundaries": "Login, captcha, account risk, paid walls and high-cost media detail require explicit Agent/user decision.",
        }
    return result


@application_tool()
@traced_tool("record_research_candidates", strategy="research_candidate_ledger")
def record_research_candidates_tool(
    research_task_id: Annotated[str, Field(description="kr_research 返回的研究任务 ID。")],
    source_ecology: Annotated[str, Field(description="候选来自的来源生态。")],
    tool: Annotated[str, Field(description="本次发现候选所用工具。")],
    candidates: Annotated[List[Dict], Field(description="本次已获得的候选对象。普通登记保存不可逆候选ID；宿主批次另保留公开URL、标题与真实host_reference，供后续正文获取；不保存凭据。")],
    query: Annotated[str, Field(description="本次实际查询。普通候选仅存HMAC指纹；宿主结果无预登记批次时必填，显式批次保存该查询和公开引用。勿传凭据或个人敏感信息。")]= "",
    language: Annotated[str, Field(description="本次查询语言标签，例如 zh、en 或 mixed。")]= "",
    intent_label: Annotated[str, Field(description="查询意图标签，例如 implementation、counterexample、user_experience。")]= "",
    receipt_id: Annotated[str, Field(description="刚完成的显式 research_task_id 工具调用返回的 research_receipt.receipt_id；缺失时仅记录为未核验候选。")]= "",
    host_batch_id: Annotated[str, Field(description="可选既有宿主批次ID；无须搜索前登记。宿主工具交回时可留空并提供实际query，candidates保留真实host_reference。宿主声明不冒充KR原生取证。")]= "",
    host_outcome: Annotated[Literal['', 'results', 'no_results', 'unavailable'], Field(description="宿主结果语义。非空候选默认results；空候选必须显式no_results（已确认零命中）或unavailable（工具没有可用载荷/故障），后者不会记作搜索成功。")]= '',
) -> Dict:
    """把 Agent 已决定执行的低层发现纳入同一候选池；不指定后续工具或轮次。"""
    if host_batch_id or tool in {'web.run','codex_web_search','codex_builtin_web_search','mcp__codex_apps__search_service_web_run'}:
        from runtime.host_search_handoff import submit
        return submit(research_task_id, host_batch_id, candidates, host_tool=tool, host_outcome=host_outcome, query=query)
    if host_outcome:
        raise ValueError('host_outcome_requires_registered_host_batch')
    return record_research_candidates(
        task_id=research_task_id, source_ecology=source_ecology, tool=tool, items=candidates,
        query=query, language=language, intent_label=intent_label, receipt_id=receipt_id,
    )


@application_tool()
@traced_tool("advance_research_candidate", strategy="research_candidate_ledger")
def advance_research_candidate(
    research_task_id: Annotated[str, Field(description="kr_research 返回的研究任务 ID。")],
    candidate_id: Annotated[str, Field(description="候选池返回的 candidate_id。")],
    stage: Annotated[Literal["selected", "deferred", "detail_extracted", "identity_checked", "cross_checked", "counterevidence", "degraded_or_blocked"], Field(description="候选当前证据阶段。")],
    tool: Annotated[str, Field(description="产生该阶段的工具或方法。")]= "",
    outcome: Annotated[str, Field(description="低基数结果标签；不要填写 URL、原文或账号信息。")]= "",
    evidence_receipt_ids: Annotated[List[str], Field(description="支撑本次升阶的已返回 research_receipt IDs。详情、身份、交叉核验、反例和退化阶段必须提供。")]= [],
    independence_rationale: Annotated[str, Field(description="单一权威来源足以支持 cross_checked 时的低基数独立性理由；否则 cross_checked 需要至少两个回执。")]= "",
) -> Dict:
    """回写已选择候选的详情、核验、反例或退化阶段。"""
    return update_research_candidate_stage(task_id=research_task_id, candidate_id=candidate_id, stage=stage, tool=tool, outcome=outcome, evidence_receipt_ids=evidence_receipt_ids, independence_rationale=independence_rationale)


@application_tool()
@traced_tool("review_research_progress", strategy="research_gap_review")
def review_research_progress(
    research_task_id: Annotated[str, Field(description="kr_research 返回的研究任务 ID。")],
    phase: Annotated[Literal["after_archaeology", "after_first_candidates", "before_delivery"], Field(description="低频审阅阶段。")],
) -> Dict:
    """指出尚未被记录的证据缺口；这是停止审阅，不命令 Agent 使用任何平台、语言或工具。"""
    from runtime.research_index import ResearchIndex
    result = review_research_task(task_id=research_task_id, phase=phase)
    feedback = ResearchIndex().read(research_task_id,view="feedback")
    review = ResearchIndex().read(research_task_id,view='review')
    semantic_gaps = any(feedback.get(k+'_count',len(feedback.get(k,[]))) for k in (
        'questions_without_reviewed_support','scope_gaps','assessments_requiring_review','unresolved_counterevidence','requirement_gaps'))
    semantic_gaps = semantic_gaps or review.get('review_status')!='recorded'
    return {**result, 'stop_assessment':'not_ready' if semantic_gaps else result.get('stop_assessment'),
            'claim_review':review,
            'review_summary':{'recording':result.get('stop_assessment'), 'semantic_review':('unknown' if feedback.get('scope_registration') != 'recorded' else 'gaps' if semantic_gaps else 'agent_declared_only'),
                              'coverage':'registered_items_only','unregistered_coverage':'unknown','stop_authority':'agent'},
            "evidence_feedback":feedback}


@application_tool()
@traced_tool("finalize_research_task", strategy="research_delivery_closeout")
def finalize_research_task(
    research_task_id: Annotated[str, Field(description="kr_research 返回的研究任务 ID。")],
    ecology_outcomes: Annotated[List[EcologyOutcome], Field(description="每个 considered source ecology 的 outcome（used/strategic_skip/blocked/not_relevant/not_reached）、原因、receipt_ids；战略跳过/不相关还须 claim_gap_ids 和 reopen_condition。")],
    stop_rationale: Annotated[str, Field(description="模型自主停止的证据边际收益/边界理由。")],
    key_claims: Annotated[List[ResearchClaim], Field(description="关键主张及 supporting_evidence_ids、supporting_receipt_ids；高重要性主张缺少真实回执支持将返回 needs_repair。")],
    quality_status: Annotated[str, Field(description="可选报告检查观察，与Agent停止决定分开；未检查保持unknown。")]= "",
    transcript_status: Annotated[str, Field(description="Codex transcript 可见性，如 available、partial 或 unavailable。")]= "unavailable",
    report_path: Annotated[str, Field(description="可选报告路径；固定版本请另用manage_research_artifacts登记。")]= "",
    evidence_path: Annotated[str, Field(description="可选证据sidecar路径。")]= "",
    quality_receipt_path: Annotated[str, Field(description="可选哈希绑定质量回执，不作为交卷许可。")]= "",
) -> Dict:
    """记录深研完成状态；不规定模型应该用哪些工具或来源。"""
    result = close_research_task(
        task_id=research_task_id,
        ecology_outcomes=[x.model_dump() if isinstance(x,EcologyOutcome) else x for x in ecology_outcomes],
        stop_rationale=stop_rationale,
        key_claims=[x.model_dump() if isinstance(x,ResearchClaim) else x for x in key_claims],
        quality_status=quality_status,
        transcript_status=transcript_status,
        report_path=report_path,
        evidence_path=evidence_path,
        quality_receipt_path=quality_receipt_path,
    )
    return {
        "schema": "knowledgeradar-research-delivery-closeout/v1",
        "status": result.get("status", "unknown_task"),
        "research_task_id": result.get("research_task_id", research_task_id),
        "closeout": result.get("closeout", {}),
        "next_action": "Review recorded gaps; the Agent decides whether to continue or deliver with disclosed limitations.",
    }


# ═══════════════════════════════════════════════════════════════════════
# 工具 2: kr_web_search
# ═══════════════════════════════════════════════════════════════════════

@application_tool()
@traced_tool("kr_web_search", strategy="provider_fallback")
def kr_web_search(
    query: Annotated[str, Field(description="开放 Web 搜索查询词。返回候选网页、新闻、博客、文档、项目主页等 URL 和摘要。")],
    limit: Annotated[int, Field(description="返回候选结果数量。", ge=1, le=20)] = 5,
    freshness: Annotated[Literal["", "day", "week", "month", "year"], Field(description="时间过滤范围。空字符串表示不限制时间。")] = "",
    provider: Annotated[SearchProviderName, Field(description="搜索后端。auto 优先 AnySearch，按缺口使用已授权补源；DDGS 无需独立服务；codex_web_search/codex_builtin_web_search 仅在对应 host capability card 有已验证调用契约时可用；youtube/yt/github/gh 为兼容旧调用的别名。")] = "auto",
    include_raw_content: Annotated[bool, Field(description="是否请求后端返回原始正文。可能增加响应体积和耗时。")] = False,
    research_task_id: Annotated[str, Field(description="可选的 kr_research 任务 ID；提供后返回可供账本绑定的 research_receipt。")]= "",
    language: Annotated[str, Field(description='搜索语言提示；支持情况随回执返回。')] = '',
    include_domains: Annotated[List[str], Field(description='允许的域名；返回结果强制后过滤，不承诺上游召回完整。')] = [],
    exclude_domains: Annotated[List[str], Field(description='排除的域名。')] = [],
    require_constraints: Annotated[bool, Field(description='为真时，不支持严格约束的来源在发送前跳过。')] = False,
    timeout_s: Annotated[float, Field(description='整个查询作业的期限秒数。', ge=1, le=120)] = 30,
    complementary_providers: Annotated[List[str], Field(description='Agent明确选择的互补来源，最多六个已配置来源或实例；为空时按缺口顺序补查，不全源扇出。')] = [],
) -> Dict:
    """开放 Web 搜索。

    返回普通网页、新闻、博客、技术文档、论文页面、项目主页等候选 URL 和摘要。
    不返回封闭平台登录内容。include_raw_content 可能增加耗时和响应体积。
    """
    log.info("kr_web_search: query_fingerprint=%s limit=%s provider=%s", privacy_fingerprint(query), limit, provider)
    if str(provider or "").lower() in {"youtube", "yt"}:
        result = search_youtube(query, limit=limit)
        result.setdefault("metadata", {})
        result["metadata"]["deprecated_alias"] = "kr_web_search(provider='youtube')"
        result["metadata"]["preferred_tool"] = "search_youtube"
        return result
    if str(provider or "").lower() in {"github", "gh"}:
        result = search_github_repositories(query, limit=limit)
        result.setdefault("metadata", {})
        result["metadata"]["deprecated_alias"] = "kr_web_search(provider='github')"
        result["metadata"]["preferred_tool"] = "search_github_repositories"
        return result
    from runtime.search_configuration import current_configuration
    config = current_configuration(fresh=True)
    request = WebSearchRequest(query=query, limit=limit, freshness=freshness, provider=provider,
        include_raw_content=include_raw_content, language=language,
        options={'include_domains':include_domains, 'exclude_domains':exclude_domains,
                 'require_constraints':require_constraints,'timeout_s':timeout_s,
                 'complementary_providers':complementary_providers,
                 'configuration_generation':config.generation})
    from use_cases.web_search import execute
    result = execute(request, cache=search_cache, record_evidence=_record_search_evidence)
    log.info(
        "  -> provider=%s total=%s fallback=%s attempted=%s",
        result.get("provider"),
        result.get("total"),
        result.get("fallback_used"),
        result.get("attempted_providers"),
    )
    return result


# 兼容内部引用；微信发现逻辑只维护在平台用例中。
from use_cases.wechat_search import queries as _wechat_article_queries, relevance as _wechat_metadata_relevance
from collectors.platform.wechat import article_url as _wechat_article_url


@application_tool()
@traced_tool("search_github_repositories", strategy="platform_sidecar")
def search_github_repositories(
    query: Annotated[str, Field(description="GitHub仓库原生查询；保留限定词与上游排序，仅显式加公开范围，不自动扩词。返回元数据与README读取入口，不能当源码证据。")],
    limit: Annotated[int, Field(description="返回仓库数量。", ge=1, le=20)] = 10,
    research_task_id: Annotated[str, Field(description="可选的 kr_research 任务 ID；提供后返回 research_receipt。")]= "",
    page: Annotated[int, Field(description="搜索页码；按next_page继续；仅GitHub前1000条窗口，不代表证据饱和。", ge=1, le=1000)] = 1,
    refresh: Annotated[bool, Field(description="显式重新联网；默认仅复用同身份、同查询的5分钟内结果。")]=False,
) -> Dict:
    """GitHub 仓库搜索。

    返回公开仓库候选、元数据、README读取入口、覆盖和下一页；不获取正文。
    gh与REST是同一只读API的传输。身份在请求前冻结，错误/空结果不切账号。
    """
    clean_limit = max(1, min(int(limit or 10), 20))
    log.info("search_github_repositories: query_fingerprint=%s limit=%s", privacy_fingerprint(query), clean_limit)
    try:
        result = gh_cli_sidecar.search_repositories(query, limit=clean_limit,
            **({'page':page} if page != 1 else {}), **({'refresh':True} if refresh else {}))
    except Exception as exc:
        from runtime.execution_context import ExecutionStopped
        from runtime.resource_concurrency import ResourceDeferred
        if isinstance(exc, (ExecutionStopped, ResourceDeferred)):
            raise
        failure_code = getattr(exc, "metadata", {}).get("failure_code", "UNKNOWN")
        result = {
            "query": query,
            "provider": "github",
            "platform": "GitHub",
            "items": [],
            "total": 0,
            "status": "failed",
            "fallback_used": False,
            "attempted_providers": ["github"],
            "error": {
                "type": failure_code,
                "message": str(exc),
                "retryable": bool(getattr(exc, 'retryable', False)),
                **getattr(exc, 'details', {}),
                "expected_degraded": failure_code in {"LOGIN_REQUIRED", "DEPENDENCY_CONFLICT", "PROVIDER_UNAVAILABLE"},
            },
            "metadata": {
                "strategy": "github_readonly_api",
                "status_class": "EXPECTED_DEGRADED" if failure_code in {"LOGIN_REQUIRED", "DEPENDENCY_CONFLICT", "PROVIDER_UNAVAILABLE"} else "FAIL",
            },
        }
    result.setdefault("platform", "GitHub")
    result.setdefault("metadata", {})
    result["metadata"].setdefault("actual_mcp_tool", "search_github_repositories")
    _record_search_evidence("GitHub", query, result)
    return result


@application_tool()
@traced_tool("search_youtube", strategy="platform_adapter")
def search_youtube(
    keyword: Annotated[str, Field(description="YouTube 视频搜索关键词。返回公开视频标题、URL、频道、摘要、发布时间等元数据。")],
    limit: Annotated[int, Field(description="返回视频数量。", ge=1, le=25)] = 10,
    research_task_id: Annotated[str, Field(description="可选的 kr_research 任务 ID；提供后返回 research_receipt。")]= "",
) -> Dict:
    """YouTube 公开视频搜索。

    返回视频标题、URL、频道、摘要、发布时间和平台元数据。
    需要 YouTube API 配置；不访问未公开、会员或地区受限内容。
    """
    clean_limit = max(1, min(int(limit or 10), 25))
    log.info("search_youtube: query_fingerprint=%s limit=%s", privacy_fingerprint(keyword), clean_limit)
    result = youtube_collectors.search_youtube(keyword, limit=clean_limit)
    result.setdefault("platform", "YouTube")
    result.setdefault("metadata", {})
    result["metadata"].setdefault("actual_mcp_tool", "search_youtube")
    _record_search_evidence("YouTube", keyword, result)
    return result


@application_tool()
@traced_tool("search_wechat_articles", strategy="wechat_readonly_discovery")
def search_wechat_articles(
    query: Annotated[str, Field(description="公众号文章主题；发现只返回候选，正文需get_content_detail。")],
    limit: Annotated[int, Field(description="展示候选上限；其余候选继续登记公共目录。", ge=1, le=20)] = 10,
    freshness: Annotated[Literal["", "day", "week", "month", "year"], Field(description="公共搜索时间约束；微信读书不支持时明确标未应用。")]= "",
    account_hint: Annotated[str, Field(description="公众号名称线索，不是严格账号限定；不会暗中去掉该线索扩搜。")]= "",
    research_task_id: Annotated[str, Field(description="可选研究任务ID，关联候选及证据。")]= "",
    source: Annotated[Literal['auto','web','weread'], Field(description="auto=公共搜索与微信读书并行；web只公共索引，weread只搜公众号文章；未登录仅阻断该来源，不自动扫码。")]='auto',
    page: Annotated[int, Field(description="兼容参数固定1；来源接续建议见metadata.continuations，不代表全微信。",ge=1,le=1)]=1,
    timeout_s: Annotated[float, Field(description="发现整体时限；已取得候选保留，未执行来源显式说明。",ge=1,le=120)]=40,
) -> Dict:
    """微信公开文章发现。公共索引与微信读书关联搜一搜并列；候选统一登记，来源错误独立保留。

    确认原文URL后由get_content_detail读取；未解析卡片不是正文。账号名仅为线索。
    微信读书借用日常Chrome的现有会话，不导出Cookie，不承诺全历史，正文/OCR另行请求。
    """
    from use_cases.wechat_search import execute
    return execute(query,cache=search_cache,record_evidence=_record_search_evidence,limit=limit,
        freshness=freshness,account_hint=account_hint,source=source,page=page,timeout_s=timeout_s)


# ═══════════════════════════════════════════════════════════════════════
# 工具 2.5: search_academic
# ═══════════════════════════════════════════════════════════════════════

@application_tool()
@traced_tool("search_academic", strategy="academic_api_metadata")
def search_academic(
    query: Annotated[str, Field(description="学术检索查询、DOI、arXiv 线索或用户提供的引用文本/文件线索。返回题名、作者、年份、DOI、摘要、开放全文状态等元数据。")],
    limit: Annotated[int, Field(description="返回文献数量。", ge=1, le=20)] = 5,
    provider: Annotated[str, Field(description="学术数据源名称。auto 按语言、学科和文献类型选择主入口与专业来源；显式值指定单个数据源。")] = "auto",
    research_task_id: Annotated[str, Field(description="可选的 kr_research 任务 ID；提供后返回 research_receipt。")]= "",
    options: Annotated[Dict | None, Field(description="默认只查书目摘要和raw.fulltext_ref固定文献卡；把version_id传给get_content_detail.academic_reference取全文。PubScholar/NSTRS返回pending_discoveries任务，完成后从get_task_status读取卡片；metadata.filtered_candidates/additional_candidates可回查词面筛选与批次溢出。provider_outcomes.continuation给源内接续参数，source_offset翻页须保持limit不变，source_cursor可直接使用。file导入须approved_input_root及input_sha256。resolve_fulltext=true（兼容 need_fulltext）提交独立正文任务，发现立即返回；按任务ID查询完成后读取证据，fulltext_top_k 上限5；has_readable_fulltext 避免重复读取。api_parallelism 控制公开API小批并行（默认3，上限4），有费用来源不预发；provider_status仅本次来源摘要，完整目录按需查get_capabilities。max_provider_attempts、deadline_s 控制来源尝试与协作式时间预算，provider_offset 接续未执行来源；取消在来源间、浏览器等待和公共下载/处理检查；单次阻塞请求受超时约束。全文fulltext_deadline_s独立于发现deadline_s，默认600，上限3600。万方默认停用，保存 Key 或指定 provider 不会启用；本人核实费用后明确开启 WFDATA_ENABLED=true 且 authorized_sources 包含 wanfang 才自动补查。collections 指定授权集合，document_id 与 collection 定点读取元数据。")]=None,
) -> Dict:
    """开放学术资料和元数据搜索。

    返回 title/authors/year/doi/url/source/oa_status/abstract/full_text_status/access_mode 等字段。
    只获取公开元数据和开放全文线索，不绕过登录、验证码、付费墙、文献传递或机构授权。
    """
    log.info("search_academic: query_fingerprint=%s limit=%s provider=%s", privacy_fingerprint(query), limit, provider)
    request = AcademicSearchRequest(query=query, limit=limit, provider=provider, options=options or {})
    result = search_academic_metadata(request).to_mcp_dict()
    _record_academic_evidence(query, result)
    return result


# ═══════════════════════════════════════════════════════════════════════
# 工具 3: extract_web_page
# ═══════════════════════════════════════════════════════════════════════

@application_tool()
@traced_tool("extract_web_page", strategy="generic_web_fallback")
def extract_web_page(
    url: Annotated[str, Field(description="http/https URL。返回清洗后的 title/content/content_format/collector 等字段。", json_schema_extra={"format": "uri"})],
    use_jina: Annotated[bool, Field(description="为 true 时允许外部 Reader 服务参与抽取；为 false 时只用本地静态抽取。")] = True,
    timeout: Annotated[float, Field(description="单次请求超时秒数。", ge=1, le=60)] = 20.0,
    research_task_id: Annotated[str, Field(description="可选的 kr_research 任务 ID；提供后返回 research_receipt。")]= "",
) -> Dict:
    """开放网页正文抽取。

    返回普通开放网页的干净 Markdown 正文、标题、抽取器、时间和错误字段。
    仅支持 http/https；登录、验证码、付费墙或强动态页面可能失败。
    """
    log.info(f"extract_web_page: url={redact_url(url)!r}, use_jina={use_jina}, timeout={timeout}")
    result = collect_url(GenericWebRequest(url=url, timeout=timeout, use_jina=use_jina)).to_mcp_dict()
    wechat_data = (result.get('metadata') or {}).get('wechat')
    if wechat_data:
        result = {**result, **wechat_data}
        result['metadata'] = {k:v for k,v in result.get('metadata',{}).items() if k != 'wechat'}
    _record_detail_evidence("微信" if wechat_data else "web", url, result)
    log.info(
        "  -> collector=%s chars=%s error=%s elapsed=%s",
        result.get("collector"),
        len(result.get("content") or ""),
        bool(result.get("error")),
        result.get("elapsed_s"),
    )
    return result


# ═══════════════════════════════════════════════════════════════════════
# 工具 4: extract_dynamic_page
# ═══════════════════════════════════════════════════════════════════════

@application_tool()
@traced_tool('process_local_evidence', strategy='isolated_optional_media')
def process_local_evidence(
    input_path: Annotated[str, Field(description='新建处理时必填：用户明确提供的本地文件路径；不扫描目录。')]='',
    approved_input_root: Annotated[str, Field(description='新建处理时必填：用户授权读取的本地目录，输入必须位于其内。')]='',
    input_sha256: Annotated[str, Field(description='新建处理时必填：输入文件 SHA-256，绑定本次解析身份。', pattern='^$|^[a-f0-9]{64}$')]='',
    component: Annotated[str, Field(description='auto：文字 PDF 本地解析，Office 用 docling，扫描/图片上传硅基流动 PaddleOCR-VL 1.5（需 SILICONFLOW_API_KEY），音频用本地 qwen3-asr-0.6b。云失败明确报错，不转本地 OCR。')]='auto',
    language: Annotated[str, Field(description='语言代码或 auto。')]='auto',
    research_task_id: Annotated[str, Field(description='所属 kr_research 任务；不创建新研究生命周期。')]='',
    task_id: Annotated[str, Field(description='已有后台任务 ID；提供时只读取该任务结果，不重复执行。')]='',
    document: DocumentReadOptions | None = None,
    timeout_s: Annotated[float,Field(ge=1,le=3600)] = 600,
) -> DetailResult:
    """解析授权文件并保留固定版本；图片/扫描件上传云端 OCR，文字PDF本地解析。"""
    from use_cases.local_evidence import request_local_evidence
    return request_local_evidence(
        input_path=input_path, approved_input_root=approved_input_root,
        input_sha256=input_sha256, component=component, language=language,
        research_task_id=research_task_id, task_id=task_id, document=document, timeout_s=timeout_s,
    )


@application_tool()
@traced_tool("extract_dynamic_page", strategy="dynamic_playwright")
def extract_dynamic_page(
    url: Annotated[str, Field(description="http/https URL。返回浏览器渲染后的正文抽取结果。", json_schema_extra={"format": "uri"})],
    wait_ms: Annotated[int, Field(description="DOM 加载后的额外等待毫秒数。", ge=0, le=15000)] = 3000,
    timeout: Annotated[float, Field(description="页面加载和抽取超时秒数。", ge=1, le=120)] = 25.0,
    research_task_id: Annotated[str, Field(description="可选的 kr_research 任务 ID；提供后返回 research_receipt。")]= "",
) -> Dict:
    """浏览器渲染网页正文抽取。

    返回普通开放网页在浏览器渲染后的正文、标题、抽取器、时间和错误字段。
    启动浏览器成本较高；登录、验证码和付费墙内容可能失败。
    """
    log.info(f"extract_dynamic_page: url={redact_url(url)!r}, wait_ms={wait_ms}, timeout={timeout}")
    result = collect_dynamic_url(
        GenericWebRequest(url=url, timeout=timeout, use_jina=False),
        wait_ms=wait_ms,
    ).to_mcp_dict()
    wechat_data = (result.get('metadata') or {}).get('wechat')
    if wechat_data:
        result = {**result, **wechat_data}
        result['metadata'] = {k:v for k,v in result.get('metadata',{}).items() if k != 'wechat'}
    _record_detail_evidence("微信" if wechat_data else "dynamic_web", url, result)
    log.info(
        "  -> collector=%s chars=%s error=%s elapsed=%s",
        result.get("collector"),
        len(result.get("content") or ""),
        bool(result.get("error")),
        result.get("elapsed_s"),
    )
    return result


# ═══════════════════════════════════════════════════════════════════════
# 工具 5: search_bilibili
# ═══════════════════════════════════════════════════════════════════════

@application_tool()
@traced_tool("search_bilibili", strategy="platform_adapter")
def search_bilibili(
    keyword: Annotated[str, Field(description="B站搜索关键词。返回视频标题、URL、UP 主、简介、播放/互动数据和发布时间等元数据。")],
    page_size: Annotated[int, Field(description="返回视频数量。", ge=1, le=30)] = 10,
    research_task_id: Annotated[str, Field(description="可选的 kr_research 任务 ID；提供后返回 research_receipt。")]= "",
) -> Dict:
    """B站视频搜索。

    返回视频标题、URL、UP 主、简介、播放/互动数据、发布时间和平台元数据。
    登录态、平台验证或页面结构变化可能导致结果为空或降级。
    """
    request = SearchRequest(keyword=keyword, limit=page_size, platform="B站")
    result = _cached_search("B站", keyword, page_size, lambda: registry.get("B站").search(request).to_mcp_dict())
    _record_search_evidence("B站", keyword, result)
    return result


# ═══════════════════════════════════════════════════════════════════════
# 工具 5: search_xiaohongshu（图文/视频分流 + 自动双搜）
# ═══════════════════════════════════════════════════════════════════════

@application_tool()
@traced_tool("search_xiaohongshu", strategy="platform_adapter")
def search_xiaohongshu(
    keyword: Annotated[str, Field(description="小红书搜索关键词。返回笔记标题、URL、作者、摘要、图文/视频类型和平台元数据。")],
    limit: Annotated[int, Field(description="返回笔记数量。", ge=1, le=20)] = 10,
    search_type: Annotated[Literal["all", "image", "video", "normal", "force_probe"], Field(description="内容类型过滤。all/normal 为综合，image 为图文，video 为视频，force_probe 为低频诊断探测。")] = "all",
    research_task_id: Annotated[str, Field(description="可选的 kr_research 任务 ID；提供后返回 research_receipt。")]= "",
) -> Dict:
    """小红书笔记搜索。

    返回笔记标题、URL、作者、摘要、图文/视频类型和平台元数据。
    依赖可用登录态和平台页面状态；验证、冷却或反爬会导致降级、跳过或结果为空。
    """
    probe_mode = search_type in {"force", "probe", "force_probe"}
    requested_limit = limit
    limit = max(1, min(int(limit or 1), XHS_PROBE_SEARCH_LIMIT if probe_mode else XHS_DEFAULT_SEARCH_LIMIT))
    # A historic global cooldown is evidence, not permission to silently skip
    # every currently admitted account and fallback.  The collector classifies
    # it with current page/account state under the single-platform operation
    # lock.  This preserves anti-bot protections without conflating profiles.
    run_id = f"xhs-search-{uuid.uuid4().hex[:12]}"
    request = SearchRequest(
        keyword=keyword,
        limit=limit,
        platform="小红书",
        search_type="all" if probe_mode else search_type,
        options={"probe_mode": probe_mode, "research_task_id": research_task_id, "cache_buster": "force_probe" if probe_mode else ""},
    )
    cache_search_type = f"{search_type}|probe" if probe_mode else search_type
    cache_search_type += "|adapter=" + json.dumps(registry.runtime_identity("小红书"), sort_keys=True)
    result = _cached_search("小红书", keyword, limit, lambda: registry.get("小红书").search(request).to_mcp_dict(), search_type=cache_search_type)
    result = _attach_xiaohongshu_error_context(result)
    if isinstance(result, dict):
        metadata = dict(result.get("metadata") or {})
        metadata.setdefault("budget", _xhs_budget_state())
        metadata["requested_limit"] = requested_limit
        metadata["effective_limit"] = limit
        metadata["xhs_search_run_id"] = run_id
        result["metadata"] = metadata
    if isinstance(result, dict):
        result = _xhs_route_receipt(result, run_id=run_id)
    _record_search_evidence("小红书", keyword, result)
    return _persist_discovery_cooldown(result, tool='search_xiaohongshu', platform='小红书',
        arguments={'keyword': keyword, 'limit': requested_limit, 'search_type': search_type, 'research_task_id': research_task_id})


def _persist_discovery_cooldown(result, *, tool, platform, arguments):
    """首次已确证风险转为持久等待；worker重探沿同一只读调用，禁止嵌套提交。"""
    from runtime.execution_context import execution_value
    from runtime.task_cooldown import cooldown_plan
    if execution_value('KR_DURABLE_WORKER') == '1' or cooldown_plan(result, operation='platform_discovery') is None:
        return result
    from runtime.task_service import TaskService
    from runtime.tasks import compact_task_ref
    service = TaskService()
    task = service.submit('platform_discovery', {'tool': tool, 'platform': platform,
        'arguments': arguments, 'initial_result': result}, timeout_s=600,
        scope={'research_task_id': arguments.get('research_task_id', '')})
    return {**result, 'accepted': True, 'task_id': task['task_id'], 'task': compact_task_ref(task),
            'result_tool': 'get_task_status', 'resume_policy': 'bounded_read_only_probe',
            'manual_action_required': False}


def execute_platform_discovery(payload):
    functions = {'search_xiaohongshu': search_xiaohongshu, 'search_zhihu': search_zhihu,
                 'search_recruitment': search_recruitment}
    function = functions.get(payload['tool'])
    if function is None: raise ValueError('unsupported_platform_discovery')
    # 同一应用合同；绕过根工具trace，由TaskService写唯一的后台完成回执。
    return function.__wrapped__(**payload['arguments'])


def _xiaohongshu_expected_degraded() -> Dict:
    detail = get_xhs_detail_health_tracker().summary(recent_limit=5)
    chain = get_xhs_chain_health_tracker().summary(recent_limit=5)
    if detail.get("status") != "degraded" and chain.get("status") != "degraded":
        return {}
    detail_layer = chain.get("detail_layer") if isinstance(chain.get("detail_layer"), dict) else {}
    discovery = chain.get("discovery") if isinstance(chain.get("discovery"), dict) else {}
    reason_hints = [
        detail.get("detail") or "",
        f"detail_success_rate={detail.get('success_rate')}",
        f"anti_bot_count={detail.get('anti_bot_count')}",
        f"empty_detail_count={detail.get('empty_detail_count')}",
        f"bridge_timeout_count={detail_layer.get('bridge_timeout_count')}",
        f"bridge_parse_failed_count={detail_layer.get('bridge_parse_failed_count')}",
        f"dead_link_count={discovery.get('dead_link_count')}",
    ]
    root_causes = []
    if (detail.get("anti_bot_count") or 0) > 0:
        root_causes.append("anti_bot_or_platform_verification")
    if (detail_layer.get("bridge_timeout_count") or 0) > 0:
        root_causes.append("bridge_timeout")
    if (detail_layer.get("bridge_parse_failed_count") or 0) > 0 or (detail.get("empty_detail_count") or 0) > 0:
        root_causes.append("parse_or_empty_detail")
    if (discovery.get("dead_link_count") or 0) > 0:
        root_causes.append("dead_link_or_stale_url")
    if not root_causes:
        root_causes.append("historical_detail_health_degraded")
    return {
        "error": "小红书当前处于 expected_degraded，已根据健康摘要跳过搜索以避免长时间 timeout",
        "type": "expected_degraded",
        "platform": "小红书",
        "stage": "search",
        "retryable": False,
        "manual_action_required": True,
        "platform_state": "degraded_by_health_gate",
        "root_causes": root_causes[:4],
        "diagnostic_evidence": [hint for hint in reason_hints if hint and "None" not in hint][:8],
        "recommended_action": "如需恢复小红书硬验收，请先检查登录态、浏览器会话和平台验证；如需强制探测，可用 search_type='force_probe'",
    }


def _attach_xiaohongshu_error_context(result: Dict) -> Dict:
    # Adapter owns login/risk recovery. Never launch a second browser here.
    if isinstance(result, dict) and isinstance(result.get("error"), dict):
        result["error"].setdefault("platform", "小红书")
        result["error"].setdefault("stage", "search")
    return result


# ═══════════════════════════════════════════════════════════════════════
# 工具 6: search_zhihu（借用日常浏览器的只读站点适配器）
# ═══════════════════════════════════════════════════════════════════════

_strip_html_text = understanding_strip_html_text
@application_tool()
@traced_tool("search_zhihu", strategy="platform_adapter")
def search_zhihu(
    keyword: Annotated[str, Field(description="知乎搜索关键词。返回可见回答、文章、问题或视频条目的标题、URL、摘要、作者和类型；正文需按URL取详情。")],
    limit: Annotated[int, Field(description="返回条目数量。", ge=1, le=20)] = 10,
    research_task_id: Annotated[str, Field(description="可选的 kr_research 任务 ID；提供后返回 research_receipt。")]= "",
) -> Dict:
    """知乎内容搜索。

    返回可见回答、文章、问题或视频条目的标题、URL、摘要、作者、类型和平台元数据。
    通过Browser Link读取本人获准可见的搜索页；未登录、访问限制、结构变化与确证空结果分别返回。
    """
    request = SearchRequest(keyword=keyword, limit=limit, platform="知乎")
    identity = registry.runtime_identity("知乎")
    try:
        result = _cached_search("知乎", keyword, limit, lambda: registry.get("知乎").search(request).to_mcp_dict(), options={"runtime_identity": identity})
    except Exception as exc:
        from runtime.execution_context import ExecutionStopped
        from runtime.resource_concurrency import ResourceDeferred
        if isinstance(exc, (ExecutionStopped, ResourceDeferred)): raise
        result = zhihu_collectors.failure('collector_error', message='知乎采集异常：' + type(exc).__name__)
    result = _promote_manual_interaction_result(
        result,
        platform="zhihu",
        original_tool="search_zhihu",
        original_args={"keyword": keyword, "limit": limit},
    )
    _record_search_evidence("知乎", keyword, result)
    return _persist_discovery_cooldown(result, tool='search_zhihu', platform='知乎',
        arguments={'keyword': keyword, 'limit': limit, 'research_task_id': research_task_id})


ensure_bindings()


# ═══════════════════════════════════════════════════════════════════════
# 招聘平台统一搜索工具
# ═══════════════════════════════════════════════════════════════════════

# 原始招聘结果仍由现有路由守卫消费；采集函数只在平台适配器维护。
_RECRUITMENT_ADAPTERS = {
    "boss": _boss_search,
    "liepin": _liepin_search,
    "maimai": _maimai_search,
    "v2ex": None,  # special handling
}


_RECRUITMENT_DISPLAY_PLATFORM = {
    "boss": "BOSS直聘",
    "liepin": "猎聘",
    "maimai": "脉脉",
    "v2ex": "V2EX",
    "zhilian": "智联招聘",
}

_RECRUITMENT_WEB_FALLBACK_DOMAINS = {
    "boss": "zhipin.com/job_detail",
    "liepin": "liepin.com/job",
    "zhilian": "jobs.zhaopin.com",
}

_RECRUITMENT_NATIVE_FAILURES_WITH_WEB_FALLBACK = {
    "tool_failure_needs_repair",
    "selector_miss",
    "schema_changed",
    "parse_failed",
    "request_failed",
    "cdp_unavailable",
    "cdp_runtime_error",
    "page_structure_or_not_ready",
    "search_query_not_applied",
    "opencli_page_failed",
    "opencli_dependency_or_output_invalid",
    "opencli_extension_unavailable",
    "opencli_daily_browser_binding_required",
    "cdp_no_output",
    "cdp_output_parse_error",
    "runtime_evaluate_exception",
    "runtime_evaluate_no_value",
}


def _recruitment_candidate_url(platform: str, url: str) -> bool:
    from urllib.parse import urlsplit
    parsed = urlsplit(url)
    if parsed.scheme not in {'http', 'https'}:
        return False
    host = parsed.hostname or ''
    if platform == 'zhilian':
        from collectors.platform.zhilian_parser import normalize_jobs
        return bool(normalize_jobs([{'url': url, 'title': 'candidate'}]))
    domain, pattern = {
        'boss': ('zhipin.com', r'^/job_detail/[^/]+\.html$'),
        'liepin': ('liepin.com', r'^/job/\d+\.shtml$'),
        'v2ex': ('v2ex.com', r'^/t/\d+/?$'),
    }.get(platform, ('', r'(?!)'))
    return bool(domain and (host == domain or host.endswith('.' + domain)) and re.search(pattern, parsed.path))


def _recruitment_blocked_result(platform: str, *, reason: str, reason_code: str, retry_after_s: float = 0, manual_action_required: bool = False, extra: Dict | None = None) -> Dict:
    display = _RECRUITMENT_DISPLAY_PLATFORM.get(platform, platform)
    error = {
        "error": reason,
        "type": reason_code,
        "reason_code": reason_code,
        "failure_type": reason_code,
        "platform_state": reason_code,
        "manual_action_required": bool(manual_action_required),
        "retryable": not manual_action_required,
        "retry_after_s": round(max(0.0, float(retry_after_s or 0)), 3),
        "failure_class": "blocked_no_claim",
        "evidence_strength": "blocked_no_claim",
        "market_claim_allowed": False,
        "salary_claim_allowed": False,
        "recommended_action": "complete_existing_browser_interaction" if manual_action_required else "retry_after_backoff",
    }
    if extra:
        error.update(extra)
    return {
        "items": [],
        "total": 0,
        "platform": display,
        "error": error,
        "failure_class": error["failure_class"],
        "evidence_strength": error["evidence_strength"],
        "market_claim_allowed": False,
        "salary_claim_allowed": False,
        "metadata": {
            "admission": {
                "allowed": False,
                "reason_code": reason_code,
                "retry_after_s": error["retry_after_s"],
                "manual_action_required": bool(manual_action_required),
            }
        },
    }


def _with_recruitment_runtime_guards(platform: str, keyword: str, city: str, call) -> Dict:
    from runtime.recruitment_governance import execute_operation
    return execute_operation(platform, 'search', call, keyword=keyword, city=city)


def _recruitment_open_web_fallback_after_route_failure(
    *,
    platform: str,
    keyword: str,
    city: str,
    limit: int,
    direct_result: Dict,
) -> Dict:
    domain = _RECRUITMENT_WEB_FALLBACK_DOMAINS.get(platform)
    if not domain:
        return direct_result
    error = direct_result.get("error") if isinstance(direct_result.get("error"), dict) else {}
    failure_type = str(error.get("failure_type") or direct_result.get("failure_type") or "")
    manual = bool(error.get("manual_action_required") or direct_result.get("manual_action_required"))
    if failure_type not in _RECRUITMENT_NATIVE_FAILURES_WITH_WEB_FALLBACK or manual:
        return direct_result

    display = _RECRUITMENT_DISPLAY_PLATFORM.get(platform, platform)
    query_parts = [f"site:{domain}", keyword, "招聘"]
    if city:
        query_parts.append(city)
    fallback = search_web(WebSearchRequest(query=" ".join(part for part in query_parts if part), limit=limit, provider="auto")).to_mcp_dict()
    if platform == 'zhilian':
        from collectors.platform.zhilian_parser import normalize_jobs
        fallback['items'] = normalize_jobs(fallback.get('items'), limit)
    items = []
    seen_urls = set()
    city_norm = str(city or "").strip().removesuffix("市")
    for item in fallback.get("items") or []:
        candidate = dict(item)
        url = str(candidate.get("url") or "")
        if not _recruitment_candidate_url(platform, url) or url in seen_urls:
            continue
        seen_urls.add(url)
        text = "\n".join(str(candidate.get(key) or "") for key in ("title", "snippet", "desc", "content"))
        if city_norm and city_norm not in text and "远程" not in text and "remote" not in text.lower():
            continue
        candidate.setdefault("platform", display)
        candidate["source"] = "web_search_fallback_after_native_route_failure"
        candidate["evidence_strength"] = "weak_open_index"
        candidate["market_claim_allowed"] = False
        candidate["salary_claim_allowed"] = False
        if candidate.get("snippet") and not candidate.get("desc"):
            candidate["desc"] = candidate.get("snippet")
        items.append(candidate)
        if len(items) >= limit:
            break
    if not items:
        return direct_result

    metadata = {
        "strategy": "web_search_fallback_after_native_route_failure",
        "fallback_from": platform,
        "provider": fallback.get("provider"),
        "native_route_failure": error or {
            "failure_type": failure_type,
            "platform_state": direct_result.get("platform_state") or "",
        },
        "evidence_strength": "weak_open_index",
        "market_claim_allowed": False,
        "salary_claim_allowed": False,
        "city_filter": {"applied": bool(city), "city": city},
    }
    return format_collection_search_response(display, items[:limit], metadata=metadata)


@application_tool()
@traced_tool("search_recruitment", strategy="platform_adapter")
def search_recruitment(
    platform: Annotated[Literal["boss", "liepin", "maimai", "v2ex"], Field(description="招聘/社区来源：boss、liepin、maimai、v2ex。脉脉返回社区讨论。")],
    keyword: Annotated[str, Field(description="职位关键词。返回职位标题、URL、公司或社区信息、摘要和平台元数据。")],
    city: Annotated[str, Field(description="城市中文名；不限城市时留空。")] = "",
    limit: Annotated[int, Field(description="返回职位数量。", ge=1, le=20)] = 10,
    research_task_id: Annotated[str, Field(description="所属 kr_research 任务 ID；提供后记录本次招聘调用与结果回执。")]= "",
) -> Dict:
    """搜索国内招聘平台职位。

    返回国内招聘平台职位标题、URL、公司或社区信息、摘要、城市过滤结果和平台元数据。
    脉脉搜索本人登录后可见的社区讨论；用 get_content_detail 读正文，include_comments=true 按需读可见评论。它不是职位列表，搜索翻页不作承诺。
    智联已退出主动采集入口，暂不投入反复真人验证；职位来源为 BOSS 和猎聘。
    登录态、平台页面变化、反爬或公开入口下线可能导致降级、回退或结果为空。
    """
    # V2EX 社区来源单独处理。
    if platform == "v2ex":
        try:
            result = _with_recruitment_runtime_guards(
                platform,
                keyword,
                city,
                lambda: _v2ex_search(SearchRequest(
                    keyword=keyword,
                    limit=limit,
                    platform="V2EX",
                    options={"city": city} if city else {},
                )),
            )
            if result.get("error") and not result.get("metadata", {}).get("open_index_eligible"):
                return result
            if result.get("items"):
                return result
            web_query = "site:v2ex.com/t/ 酷工作"
            if keyword:
                web_query = f"site:v2ex.com/t/ {keyword} 招聘 OR 酷工作"
            if city:
                web_query = f"{web_query} {city}"
            fallback = search_web(WebSearchRequest(query=web_query, limit=limit, provider="auto")).to_mcp_dict()
            items = []
            seen_urls = set()
            city_norm = str(city or "").strip().removesuffix("市")
            for item in fallback.get("items") or []:
                item = dict(item)
                if not _recruitment_candidate_url('v2ex', str(item.get('url') or '')):
                    continue
                if city_norm:
                    text = "\n".join(str(item.get(key) or "") for key in ("title", "snippet", "desc", "content"))
                    if city_norm not in text and "远程" not in text and "remote" not in text.lower():
                        continue
                canonical_url = str(item.get("url") or "").replace("https://www.v2ex.com/", "https://v2ex.com/")
                if canonical_url and canonical_url in seen_urls:
                    continue
                if canonical_url:
                    seen_urls.add(canonical_url)
                    item["url"] = canonical_url
                item.setdefault("platform", "V2EX")
                item.setdefault("source", "web_search_fallback")
                item.update(content_type="community_discussion_candidate", evidence_strength="weak_open_index", market_claim_allowed=False, salary_claim_allowed=False)
                if item.get("snippet") and not item.get("desc"):
                    item["desc"] = item.get("snippet")
                items.append(item)
            if items:
                return format_collection_search_response(
                    "V2EX",
                    items[:limit],
                    metadata={
                        "strategy": "web_search_fallback",
                        "fallback_from": "v2ex_direct",
                        "native_discovery": result.get("metadata", {}),
                        "evidence_strength": "weak_open_index",
                        "market_claim_allowed": False, "salary_claim_allowed": False,
                        "direct": (result.get("metadata") or {}),
                        "provider": fallback.get("provider"),
                        "city_filter": {"applied": bool(city), "city": city},
                    },
                )
            return result
        except Exception as e:
            log.error(f"V2EX 搜索异常: {e}")
            return {"error": str(e), "platform": "V2EX"}

    if platform == "zhilian":
        return {"platform": "智联招聘", "status": "unsupported", "items": [],
                "error": "智联已退出主动采集范围，优先使用 BOSS 或猎聘。已观察到真人验证墙，但未证明每次必验；不再维持自动探测和弹窗。",
                "failure_type": "source_disabled", "manual_action_required": False}

    func = _RECRUITMENT_ADAPTERS.get(platform)
    if not func:
        return {
            "error": f"不支持的平台: {platform}",
            "supported": list(_RECRUITMENT_ADAPTERS.keys()),
            "platform": platform,
        }
    request = SearchRequest(
        keyword=keyword,
        limit=limit,
        platform=platform,
        options={"city": city} if city else {},
    )
    try:
        result = _with_recruitment_runtime_guards(platform, keyword, city, lambda: func(request))
        promoted = _promote_manual_interaction_result(
            result,
            platform=platform,
            original_tool="search_recruitment",
            original_args={"platform": platform, "keyword": keyword, "city": city, "limit": limit, "research_task_id": research_task_id},
        )
        final = _recruitment_open_web_fallback_after_route_failure(
            platform=platform,
            keyword=keyword,
            city=city,
            limit=limit,
            direct_result=promoted,
        )
        return _persist_discovery_cooldown(final, tool='search_recruitment',
            platform=_RECRUITMENT_DISPLAY_PLATFORM.get(platform, platform),
            arguments={'platform': platform, 'keyword': keyword, 'city': city, 'limit': limit, 'research_task_id': research_task_id})
    except Exception as e:
        log.error(f"招聘搜索异常 [{platform}]: {e}")
        return {"error": str(e), "platform": platform}


# ═══════════════════════════════════════════════════════════════════════
# 工具 7: get_capabilities
# ═══════════════════════════════════════════════════════════════════════

@application_tool( group="runtime", read_only=True)
@traced_tool("get_capabilities", strategy="capability_discovery")
def get_capabilities(
    summary: Annotated[bool, Field(description="默认短目录；false只用于完整诊断。")] = True,
    tool_name: Annotated[str, Field(description="按工具名展开当前注册的输入/输出schema与用途。")] = "",
) -> Dict:
    """返回 KnowledgeRadar 当前 MCP 工具和平台能力声明。

    返回已注册工具、平台能力、运行时状态、provider 状态和策略摘要。
    完整结果较大；summary=true 返回压缩版。
    """
    from runtime.agent_contracts import catalog
    if tool_name or summary:
        return catalog(tool_name)
    result = build_capabilities(
        decision_log_path=DECISION_LOG_PATH,
        provider_status=provider_status,
    )
    result["mcp_observability"] = mcp_observability_snapshot(
        transport=MCP_TRANSPORT,
        tool_names=ACTUAL_MCP_TOOLS,
    )
    return result


def _capabilities_summary() -> Dict:
    from runtime.agent_contracts import catalog
    return catalog()


def _compact_chrome_runtime_summary(runtime: Dict) -> Dict:
    platforms = {}
    for name, item in (runtime.get("platforms") or {}).items():
        if not isinstance(item, dict):
            continue
        platforms[name] = {
            "port": item.get("port"),
            "cdp_status": item.get("cdp_status"),
            "managed": item.get("managed"),
            "external_profile_pid": item.get("external_profile_pid"),
            "external_profile_without_cdp": item.get("external_profile_without_cdp"),
            "blocked_reason": item.get("blocked_reason"),
            "manual_probe": item.get("manual_probe", {}),
            "main_chain_gate": item.get("main_chain_gate", {}),
            "profile_dir_hash": _stable_text_hash(item.get("profile_dir")),
            "actual_profile_dir_hash": _stable_text_hash(item.get("actual_profile_dir")),
        }
    return {
        "status": runtime.get("status", "unknown"),
        "transport": runtime.get("transport"),
        "on_demand": runtime.get("on_demand"),
        "platforms": platforms,
    }


def _stable_text_hash(value: object) -> str:
    import hashlib

    text = str(value or "")
    if not text:
        return ""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


# ═══════════════════════════════════════════════════════════════════════
# 工具 8: health_check
# ═══════════════════════════════════════════════════════════════════════




@application_tool()
@traced_tool("manage_xiaohongshu_accounts", strategy="account_identity_lifecycle")
def manage_xiaohongshu_accounts(
    action: Annotated[str, Field(description="summary 查看连接；begin_claim 打开日常浏览器扫码；confirm_claim/recover 确认登录。")],
    profile_id: Annotated[str, Field(description="兼容参数；只允许当前绑定的单一日常浏览器。")]= "",
    display_label: Annotated[str, Field(description="兼容参数，不保存账号池名称。")]= "",
    masked_hint: Annotated[str, Field(description="兼容参数，不保存账号信息。")]= "",
) -> Dict:
    """查看小红书连接状态，或协助用户登录已绑定的日常浏览器。

    begin_claim 打开登录页；用户完成扫码后调用 confirm_claim 或 recover 确认。
    仅使用当前单账号；安全验证需要用户处理，不自动重试。
    """
    from runtime.xhs_opencli import connection_status, request_login, complete_login
    from runtime.xhs_opencli import setting
    current = setting("KR_XHS_OPENCLI_CONTEXT_ID")
    if profile_id and profile_id != current:
        return {"status": "error", "reason_code": "SINGLE_DAILY_BROWSER_ONLY"}
    normalized = str(action or "summary").strip().lower()
    if normalized == "summary":
        return {"status": "ok", "connection": connection_status(), "single_account": True}
    if normalized == "begin_claim":
        return request_login()
    if normalized in {"confirm_claim", "recover"}:
        return complete_login()
    return {"status": "error", "reason_code": "UNKNOWN_ACTION"}


@application_tool()
@traced_tool("health_check", strategy="layered_runtime_diagnostics")
def health_check(
    mode: Annotated[str, Field(description="runtime：内核外只读状态，不启动、不探平台，可有界等待恢复；默认summary为只读声明/任务摘要；诊断用diagnostic_summary，完整探活用full。未知浏览器执行只查原回执：reconcile_browser_operation:<platform>；显式接受确定结果或放弃原操作：resolve_browser_operation:<platform>:<accept_result|abandon>:<原operation_id>，均不重放；仅支持zhihu/boss/liepin/maimai。微信读书认证检查probe_browser_auth:weread，显式登录交接request_browser_interaction:weread:login，结束自有登录页release_browser_interaction:weread。")] = "summary",
    wait_seconds: Annotated[float, Field(ge=0, le=30, description="仅mode=runtime：有界等待现有恢复，最长30秒；不启动内核、不重试业务。")] = 0,
) -> Dict:
    """检查 KnowledgeRadar 的关键平台和运行依赖状态。

    返回整体状态、平台探活、登录态、浏览器会话、任务队列、provider 和降级摘要。
    full 可能较慢；summary/light/lite 返回轻量摘要。
    """
    normalized_mode = str(mode or "full").strip().lower()
    if normalized_mode == 'runtime':
        from runtime.runtime_control import runtime_snapshot
        return runtime_snapshot(wait_seconds=wait_seconds)
    if normalized_mode.startswith(('reconcile_browser_operation:', 'resolve_browser_operation:')):
        parts = str(mode).strip().split(':')
        platform = parts[1].lower() if len(parts)>1 else ''
        if platform not in {'zhihu','boss','liepin','maimai'}:
            return {'status':'invalid_request','failure_type':'unsupported_native_platform'}
        from collectors.platform import zhihu, boss, liepin, maimai
        site = {'zhihu':zhihu,'boss':boss,'liepin':liepin,'maimai':maimai}[platform]._site()
        if normalized_mode.startswith('resolve_browser_operation:'):
            if len(parts)!=4:
                return {'status':'invalid_request','failure_type':'resolution_and_original_operation_id_required'}
            interaction = site.reconcile_outcome(resolution=parts[2],operation_id=parts[3])
        else:
            interaction = site.reconcile_outcome()
        return {'status':interaction.get('status','unknown'),'platform':platform,'interaction':interaction,'automatic_replay':False}
    if normalized_mode in {"probe_browser_auth:weread", "request_browser_interaction:weread:login", "release_browser_interaction:weread"}:
        from collectors.platform import weread
        if normalized_mode == "probe_browser_auth:weread":
            interaction = weread.readiness()
            status = "ok" if interaction.get("ready") else interaction.get("status", "unknown")
        elif normalized_mode == "request_browser_interaction:weread:login":
            interaction = weread.begin_login()
            status = "waiting_for_user" if interaction.get("ok") else "failed"
        else:
            from runtime.opencli_bridge import invoke_page
            interaction = invoke_page(session="wechat-weread", operation="release_login", passive_manual_login=True)
            status = "ok" if interaction.get("ok") else "failed"
        return {"status": status, "platform": "微信", "discovery_source": "weread_sosou", "interaction": interaction}
    if normalized_mode in {"summary", "light", "lite"}:
        return _health_check_agent_summary()
    if normalized_mode in {"diagnostic_summary", "diagnostic-summary", "summary_diagnostic", "debug_summary"}:
        return _health_check_summary()
    if normalized_mode in {"browser_sessions", "browser-session", "browser_sessions_summary"}:
        sessions = browser_sessions_summary(limit=50)
        return {
            "schema_version": "knowledgeradar-health-browser-sessions/v1",
            "summary": False,
            "status": "needs_interaction" if sessions.get("pending_human_action") else "ok",
            "checks": {
                "browser_sessions": sessions,
                "chrome_runtime": _compact_chrome_runtime_summary(chrome_runtime_summary()),
            },
        }
    if normalized_mode.startswith("probe_browser_auth:"):
        parts = normalized_mode.split(":")
        platform = parts[1] if len(parts) >= 2 else ""
        if platform == "xhs":
            from runtime.xhs_opencli import invoke
            interaction = invoke("session_probe")
        else:
            interaction = probe_browser_auth(platform)
        return {
            "schema_version": "knowledgeradar-health-browser-auth-probe/v1",
            "summary": False,
            "status": interaction.get("status", "unknown"),
            "checks": {
                "browser_auth_probe": interaction,
                "browser_sessions": browser_sessions_summary(limit=20),
                "chrome_runtime": _compact_chrome_runtime_summary(chrome_runtime_summary()),
            },
        }
    if normalized_mode.startswith("request_browser_interaction:"):
        parts = normalized_mode.split(":")
        platform = parts[1] if len(parts) >= 2 else ""
        reason = parts[2] if len(parts) >= 3 and parts[2] else "manual_action_required"
        if platform not in set(managed_browser_platforms()) | {'boss', 'liepin', 'maimai', 'zhihu', 'xhs'}:
            return {
                "schema_version": "knowledgeradar-health-browser-interaction/v1",
                "summary": False,
                "status": "error",
                "checks": {
                    "browser_interaction": {
                        "status": "error",
                        "detail": "unsupported platform",
                        "platform": platform,
                    }
                },
            }
        if platform == "xhs":
            from runtime.xhs_opencli import request_login
            interaction = request_login()
        else:
            interaction = request_browser_interaction(platform, reason)
        return {
            "schema_version": "knowledgeradar-health-browser-interaction/v1",
            "summary": False,
            "status": interaction.get("status", "unknown"),
            "checks": {
                "browser_interaction": interaction,
                "browser_sessions": browser_sessions_summary(limit=20),
                "chrome_runtime": _compact_chrome_runtime_summary(chrome_runtime_summary()),
            },
        }
    if normalized_mode.startswith("complete_browser_interaction:"):
        parts = normalized_mode.split(":")
        platform = parts[1] if len(parts) >= 2 else ""
        if platform not in set(managed_browser_platforms()) | {'boss', 'liepin', 'maimai', 'zhihu', 'xhs'}:
            return {
                "schema_version": "knowledgeradar-health-browser-interaction/v1",
                "summary": False,
                "status": "error",
                "checks": {
                    "browser_interaction": {
                        "status": "error",
                        "detail": "unsupported platform",
                        "platform": platform,
                    }
                },
            }
        if platform == "xhs":
            from runtime.xhs_opencli import complete_login
            interaction = complete_login()
        else:
            interaction = complete_browser_interaction(platform)
        return {
            "schema_version": "knowledgeradar-health-browser-interaction/v1",
            "summary": False,
            "status": interaction.get("status", "unknown"),
            "checks": {
                "browser_interaction": interaction,
                "browser_sessions": browser_sessions_summary(limit=20),
                "chrome_runtime": _compact_chrome_runtime_summary(chrome_runtime_summary()),
            },
        }
    if normalized_mode.startswith("cancel_browser_interaction:"):
        parts = normalized_mode.split(":")
        platform = parts[1] if len(parts) >= 2 else ""
        if platform not in set(managed_browser_platforms()) | {'boss', 'liepin', 'maimai', 'zhihu', 'xhs'}:
            return {
                "schema_version": "knowledgeradar-health-browser-interaction/v1",
                "summary": False,
                "status": "invalid_request",
                "checks": {"browser_interaction": {"status": "invalid_request", "detail": "unsupported platform", "platform": platform}},
            }
        if platform == "xhs":
            from runtime.xhs_opencli import cancel_login
            interaction = cancel_login()
        else:
            interaction = cancel_browser_interaction(platform)
        return {
            "schema_version": "knowledgeradar-health-browser-interaction/v1",
            "summary": False,
            "status": interaction.get("status", "unknown"),
            "checks": {
                "browser_interaction": interaction,
                "browser_sessions": browser_sessions_summary(limit=20),
                "chrome_runtime": _compact_chrome_runtime_summary(chrome_runtime_summary()),
            },
        }
    if normalized_mode in {
        "low_risk_execution_probe",
        "low-risk-execution-probe",
        "native_low_risk_probe",
        "native-low-risk-probe",
        "runtime_contract_probe",
        "runtime-contract-probe",
    }:
        probe = _low_risk_execution_probe()
        return {
            "schema_version": "knowledgeradar-health-low-risk-execution-probe/v1",
            "summary": False,
            "status": probe.get("status", "ok"),
            "checks": {
                "runtime_contract": runtime_contract_summary(),
                "low_risk_execution": probe,
                "trace_evidence_ledger": probe.get("ledger", {}),
                "gh_cli_admission": gh_cli_admission_record(gh_cli_sidecar.health()),
            },
        }
    global _LAST_HEALTH_LAYERS
    result = HealthCheckService(
        HealthCheckDeps(
            bili_headers=BILI_HEADERS,
            project_root=PROJECT_ROOT,
            runtime_log_dir=RUNTIME_LOG_DIR,
            mcp_server_log_path=MCP_SERVER_LOG_PATH,
            decision_log_path=DECISION_LOG_PATH,
            decision_logger=decision_logger,
            provider_status=provider_status,
            academic_provider_status=academic_provider_status,
            chrome_debug_port=_chrome_debug_port,
            chrome_debug_url=_chrome_debug_url,
            chrome_runtime_summary=chrome_runtime_summary,
            ensure_chrome_debugging=_ensure_chrome_debugging,
            finish_chrome_automation=finish_chrome_automation,
            find_chrome_exe=_find_chrome_exe,
            cleanup_chrome_platform=_cleanup_managed_chrome_platform,
            bring_chrome_to_front=bring_chrome_to_front,
            background_chrome=background_chrome,
            legacy_search_bilibili=legacy_search_bilibili,
            legacy_search_zhihu=zhihu_collectors.legacy_search_zhihu,
            legacy_search_xiaohongshu=xhs_collectors.legacy_search_xiaohongshu,
            youtube_configured=youtube_collectors.youtube_configured,
            platform_capabilities=platform_capabilities_dict,
            task_queue_summary=lambda: get_task_store().summary(recent_limit=10),
            usage_summary=lambda: get_usage_tracker().summary(recent_limit=10),
            monitor_summary=lambda: get_monitor_tracker().summary(recent_limit=10),
            degradation_summary=lambda: get_degradation_policy().summary(recent_limit=10),
            xhs_detail_health_summary=lambda: get_xhs_detail_health_tracker().summary(recent_limit=12),
            xhs_chain_health_summary=lambda: get_xhs_chain_health_tracker().summary(recent_limit=12),
            evidence_store_health=evidence_store.health,
            academic_evidence_summary=lambda: evidence_store.academic_recent_summary(limit=10),
            search_cache_summary=search_cache.summary,
            gh_cli_sidecar_health=gh_cli_sidecar.health,
        ),
    ).run()
    result["usage_summary"] = get_usage_tracker().summary(recent_limit=10)
    result["monitor_summary"] = get_monitor_tracker().summary(recent_limit=10)
    result["degradation_summary"] = get_degradation_policy().summary(recent_limit=10)
    result["tool_trace_summary"] = get_tool_trace_recorder().summary(recent_limit=10)
    result["calibration"] = build_calibration_report(
        evidence_rows=evidence_store.recent(100),
        decision_summary=decision_logger.summarize(100),
    )
    result["xiaohongshu_account_state"] = xiaohongshu_account_state(_chrome_debug_url)
    _LAST_HEALTH_LAYERS = result.get("platform_health_layers", {}) if isinstance(result.get("platform_health_layers"), dict) else {}
    sample_runtime_snapshot(scope="health_check", run_health_check=lambda: result)
    return result


def _low_risk_execution_probe() -> Dict:
    low_risk_executions = []
    low_risk_executions.append(
        run_low_risk_execution_command(
            build_low_risk_execution_command(
                capability_id="academic.search.execute.sample",
                tool_name="search_academic",
                purpose="search",
                input_data={"query": "Model Context Protocol", "limit": 1, "provider": "openalex"},
            ),
            lambda: search_academic("Model Context Protocol", limit=1, provider="openalex"),
        )
    )
    low_risk_executions.append(
        run_low_risk_execution_command(
            build_low_risk_execution_command(
                capability_id="web.extract.execute.sample",
                tool_name="extract_web_page",
                purpose="extract",
                input_data={
                    "url": "https://modelcontextprotocol.io/specification/2025-11-25/server/resources",
                    "timeout": 12,
                    "use_jina": True,
                },
            ),
            lambda: extract_web_page(
                "https://modelcontextprotocol.io/specification/2025-11-25/server/resources",
                use_jina=True,
                timeout=12.0,
            ),
        )
    )
    return low_risk_execution_summary(low_risk_executions)


def _low_risk_execution_probe_declaration() -> Dict:
    return low_risk_execution_probe_declaration()


def _health_check_summary() -> Dict:
    providers = provider_status()
    try:
        academic_status = academic_provider_status()
    except Exception as exc:
        academic_status = {
            "status_unavailable": {
                "status": "degraded",
                "validation_status": "EXPECTED_DEGRADED",
                "status_class": "EXPECTED_DEGRADED",
                "degraded_reason": str(exc)[:240],
            }
        }
    task_summary = get_task_store().summary(recent_limit=3)
    xhs_detail = get_xhs_detail_health_tracker().summary(recent_limit=5)
    xhs_chain = get_xhs_chain_health_tracker().summary(recent_limit=5)
    from runtime.xhs_opencli import connection_status as _xhs_opencli_status
    xhs_opencli = _xhs_opencli_status()
    xhs_budget = _xhs_budget_state()
    degradation = get_degradation_policy().summary(recent_limit=3)
    profile_registry = profile_registry_summary()
    from runtime.browser_link import BrowserLink
    from runtime.channel_admission import current_browser_diagnostics
    profile_registry, channel_admission = current_browser_diagnostics(
        profile_registry, opencli=xhs_opencli, native=BrowserLink().health())
    browser_channels = browser_channel_summary(profile_registry.get("profiles", []))
    browser_channels['borrowed_channels'] = channel_admission['channels']
    browser_channels['production_routing'] = 'platform_adapter_owned'
    chrome_runtime = chrome_runtime_summary()
    governed_capability_plan = governed_capability_plan_summary()
    low_risk_execution = _low_risk_execution_probe_declaration()
    gh_cli_admission = gh_cli_admission_record(gh_cli_sidecar.health())
    openclaw_adapter = openclaw_native_adapter_summary()
    project_governance = project_governance_manifest()
    native_readonly_runner = run_readonly_patrol(
        [
            {
                "capability_id": "capabilities.summary",
                "tool_name": "capabilities_summary",
                "fn": _capabilities_summary,
            },
            {
                "capability_id": "tasks.summary",
                "tool_name": "task_store_summary",
                "fn": lambda: _task_status_compact(limit=5),
            },
            {
                "capability_id": "decision_logs.compact",
                "tool_name": "decision_log_summary",
                "fn": lambda: _decision_logs_compact(limit=30),
            },
            {
                "capability_id": "profile_registry.summary",
                "tool_name": "profile_registry_summary",
                "fn": lambda: profile_registry,
            },
            {
                "capability_id": "openclaw_native_adapter.summary",
                "tool_name": "openclaw_native_adapter_summary",
                "fn": lambda: openclaw_adapter,
            },
            {
                "capability_id": "project_governance.summary",
                "tool_name": "project_governance_manifest",
                "fn": lambda: project_governance,
            },
        ]
    )
    web_available = any(
        item.get("available")
        for name, item in providers.items()
        if isinstance(item, dict) and not str(name).startswith("_")
    )
    checks = {
        "tool_surface": {"status": "ok", **build_tool_surface()},
        "mcp_observability": mcp_observability_snapshot(
            transport=MCP_TRANSPORT,
            tool_names=ACTUAL_MCP_TOOLS,
        ),
        "web_search_provider": {
            "status": "ok" if web_available else "degraded",
            "providers": {
                name: {
                    "configured": item.get("configured"),
                    "available": item.get("available"),
                    "status": item.get("status"),
                    "enabled": item.get("enabled"),
                    "role": item.get("role"),
                    "degraded_ok": item.get("degraded_ok"),
                    "anonymous_access": item.get("anonymous_access"),
                    "degraded_reason": item.get("degraded_reason"),
                }
                for name, item in providers.items()
                if not str(name).startswith("_")
            },
            "quota": providers.get("_quota", {}),
            "host_search_cards": providers.get("_host_search_cards", {}),
            "validation_status": "PASS" if web_available else "EXPECTED_DEGRADED",
            "status_class": "PASS" if web_available else "EXPECTED_DEGRADED",
            "validation_semantics": validation_semantics_manifest(providers, academic_status),
        },
        "academic_provider": {
            "status": "ok" if any(item.get("available") for item in academic_status.values() if isinstance(item, dict)) else "degraded",
            "provider_status": academic_status,
            "status_counts": canonical_status_counts(academic_status.values()),
            "validation_status": "PASS" if any(item.get("available") for item in academic_status.values() if isinstance(item, dict)) else "EXPECTED_DEGRADED",
            "status_class": "PASS" if any(item.get("available") for item in academic_status.values() if isinstance(item, dict)) else "EXPECTED_DEGRADED",
        },
        "task_queue": {
            "status": task_summary.get("status", "ok"),
            "total": task_summary.get("total"),
            "active": task_summary.get("active"),
            "stale_count": task_summary.get("stale_count"),
            "counts": task_summary.get("counts", {}),
        },
        "xiaohongshu_detail_health": {
            "status": xhs_opencli.get("status") if not xhs_opencli.get("connected") else xhs_detail.get("status"),
            "success_rate": xhs_detail.get("success_rate"),
            "total": xhs_detail.get("total"),
            "detail": xhs_opencli.get("detail") if not xhs_opencli.get("connected") else xhs_detail.get("detail"),
        },
        "xiaohongshu_chain_health": {
            "status": xhs_opencli.get("status") if not xhs_opencli.get("connected") else xhs_chain.get("status"),
            "detail": xhs_opencli.get("detail") if not xhs_opencli.get("connected") else xhs_chain.get("detail"),
            "layers": xhs_chain.get("layers", {}),
        },
        "xiaohongshu_budget": xhs_budget,
        "xiaohongshu_diagnostic_control": _xhs_diagnostic_control_state(),
        "profile_registry": profile_registry,
        "chrome_runtime": _compact_chrome_runtime_summary(chrome_runtime),
        "runtime_contract": runtime_contract_summary(),
        "l2_multimodal_task_contract": l2_multimodal_task_contract(),
        "compact_patrol_contract": compact_patrol_contract(),
        "openclaw_native_adapter": openclaw_adapter,
        "project_governance": project_governance,
        "native_readonly_runner": native_readonly_runner,
        "governed_capability_plan": governed_capability_plan,
        "low_risk_execution": low_risk_execution,
        "trace_evidence_ledger": {
            "schema": "knowledgeradar-trace-evidence-ledger-summary/v1",
            "status": "ok",
            "sources": ["native_readonly_runner", "governed_capability_plan"],
            "readonly": native_readonly_runner.get("ledger", {}),
            "plan_only": governed_capability_plan.get("ledger", {}),
            "low_risk_execution": {"status": "not_executed", "probe_mode": "health_check(mode='low_risk_execution_probe')"},
        },
        "gh_cli_admission": gh_cli_admission,
        "browser_channels": browser_channels,
        "xiaohongshu_channel_admission": channel_admission,
        "degradation_summary": {
            "status": degradation.get("status", "ok"),
            "recent_count": len(degradation.get("recent", []) or []),
        },
    }
    validation_rollup = _summary_validation_rollup(checks)
    return {
        "schema_version": "knowledgeradar-health-summary/v1",
        "summary": True,
        "status": legacy_health_status(validation_rollup["status_class"]),
        "legacy_status": _overall_status_from_summary(checks),
        "checks": checks,
        "validation_rollup": validation_rollup,
        "capabilities": {"tool_surface": build_tool_surface()},
    }


def _health_check_agent_summary() -> Dict:
    with governed_call("health_check", "health.summary") as gov:
        cache = get_ttl_cache("health.summary", ttl_s=float(os.environ.get("KR_HEALTH_SUMMARY_TTL_S", "30")), max_items=8)
        cold_static = os.environ.get("KR_HEALTH_SUMMARY_COLD_STATIC", "true").strip().lower() not in {"0", "false", "no", "off"}
        cache_key = stable_key("health.summary", os.getpid(), len(ACTUAL_MCP_TOOLS), cold_static)
        cached = cache.get(cache_key, allow_stale=False)
        if cached:
            return cached
        compute_started = time.time()
        result = _health_check_agent_summary_uncached()
        cache_meta = cache.set(cache_key, result, compute_elapsed_s=time.time() - compute_started)
        return attach_runtime_metadata(
            result,
            tool_name="health_check",
            capability_id="health.summary",
            started=gov["started"],
            budget=gov["budget"],
            cache=cache_meta,
        )


def _health_check_agent_summary_uncached() -> Dict:
    from runtime.agent_contracts import catalog
    info=catalog()
    summary=get_task_store().summary(recent_limit=1)
    return {"schema_version":"knowledgeradar-health-agent-summary/v2", "status":"ok", "summary":True,
            "tool_count":info["tool_count"], "contract_hash":info["contract_hash"],
            "task_counts":summary.get("counts",{}), "providers":"not_probed", "browser":"not_probed",
            "diagnostic_mode":"health_check(mode='diagnostic_summary')"}


def _overall_status_from_summary(checks: Dict[str, Dict]) -> str:
    return overall_status_from_summary(checks)


def _summary_validation_rollup(checks: Dict[str, Dict]) -> Dict:
    required = {"tool_surface", "web_search_provider", "academic_provider", "task_queue", "project_governance", "runtime_contract"}
    rows = []
    for name, check in (checks or {}).items():
        if not isinstance(check, dict):
            continue
        is_required = name in required and not check.get("skipped")
        classification = classify_runtime_payload(
            check,
            required=is_required,
            main_chain=is_required,
            configured=True,
            has_declared_reason=bool(check.get("detail") or check.get("reason") or check.get("degraded_reason") or check.get("validation_reason")),
            optional=not is_required or bool(check.get("retryable")) or bool(check.get("skipped")),
        )
        rows.append({"name": name, **classification})
    return {
        "schema": "knowledgeradar-validation-rollup/v1",
        "status_class": aggregate_validation_status(rows),
        "counts": canonical_status_counts(rows),
        "status_policy": "PASS and EXPECTED_DEGRADED are non-blocking; FAIL and NEEDS_INTERACTION block.",
        "checks": {
            row["name"]: {
                "status_class": row["status_class"],
                "raw_status": row["raw_status"],
                "reason": row.get("reason") or "",
                "blocks_overall_pass": row["blocks_overall_pass"],
            }
            for row in rows
        },
    }


# ═══════════════════════════════════════════════════════════════════════
# 工具 8: get_content_detail
# ═══════════════════════════════════════════════════════════════════════

@application_tool()
@traced_tool("get_content_detail", strategy="detail_strategy")
def get_content_detail(
    url: Annotated[str, Field(description="内容 URL。支持开放网页和已实现的平台详情 URL；返回正文、元数据、评论、字幕或转写线索、图片或视频处理结果、证据字段、错误和后台任务引用。", json_schema_extra={"format": "uri"})],
    enable_deep_analysis: Annotated[bool, Field(description="为 true 时允许更深的图片、视频、音频或内容分析；成本更高，可能较慢或产生后台任务。")] = False,
    enable_comment_filtering: Annotated[bool, Field(description="为 true 时允许评论筛选或评论分析；可能增加耗时，失败时应保留原始评论或退化说明。")] = False,
    auto_multimodal: Annotated[bool, Field(description="为 true 时允许图片 OCR、视频或音频转写、视觉理解等多媒体处理；可能较慢，可能产生后台任务，完成后可按返回字段等待并重读详情。")] = False,
    research_session_id: Annotated[str, Field(description="旧研究会话别名。普通调用留空；保留兼容。")] = "",
    work_scope_id: Annotated[str, Field(description="工作范围 ID。普通调用留空；用于把后台任务绑定到同一工作范围。")] = "",
    task_scope_id: Annotated[str, Field(description="任务范围 ID。普通调用留空；用于等待或聚合同一任务范围的后台任务。")] = "",
    research_task_id: Annotated[str, Field(description="可选的 kr_research 任务 ID；提供后返回 research_receipt。")]= "",
    include_transcript: Annotated[bool | None, Field(description="明确允许或禁止字幕/ASR；未指定时跟随增强开关。允许时由有界持久任务执行。")]= None,
    include_comments: Annotated[bool | None, Field(description="明确允许或禁止评论采集；未指定时跟随评论分析开关。")]= None,
    include_ocr: Annotated[bool | None, Field(description="明确允许或禁止图片OCR；未指定时跟随多模态开关。")]= None,
    language: Annotated[str | None, Field(description="本次字幕/ASR语言，ISO639如zh/en或auto；不修改进程默认语言。")] = None,
    deadline_s: Annotated[float, Field(description="增强任务总期限秒数，含排队；长视频可能超过180秒。到期任务停止接收结果，原生计算返回前仍保留资源租约，不自动延长或重试。", ge=1, le=3600)] = 600,
    academic_reference: Annotated[str, Field(description="search_academic返回的fulltext_ref.version_id。提供时按固定文献卡提交独立学术全文任务，url须与该卡片一致；用get_task_status和read_knowledge读取结果。", pattern='^(?:[a-f0-9]{64})?$')] = '',
    academic_refresh: Annotated[bool, Field(description="学术全文显式要求重新核验来源；默认允许复用一天内同文献卡、同处理版本的固定证据，并说明未重新联网核验。")]=False,
    github: Annotated[GitHubReadOptions | None, Field(description="GitHub对象范围；普通URL可不填。主页默认目录概览，kind=readme读说明，file/tree配path；非默认分支URL用ref消歧。评论默认每流一页，按next_page续读。异步任务沿固定commit和已完成页恢复。")]=None,
    xhs: Annotated[XhsReadOptions | None, Field(description="小红书请求窗口：OCR默认全部内容图片，image_indexes可选原始零基索引；执行上限导致未完成时返回缺口。评论按根窗口和总条数预算读取。只要正文请显式关闭OCR和转写，不下载媒体。")]=None,
    wechat: Annotated[WechatReadOptions | None, Field(description="微信读取窗口：默认HTTP；明确transport=opencli借用获准日常浏览器，不自动绕过验证。image_indexes为零基图片范围，include_attachments取公开PDF；增强在持久任务中执行。")]=None,
    document: DocumentReadOptions | None = None,
    comments: CommentReadOptions | None = None,
) -> DetailResult:
    """详情页正文抽取。

    返回正文、标题、作者、发布时间、评论、字幕/转写线索、图片/视频处理结果、
    证据字段、错误字段和后台任务引用。登录态、验证码、付费墙、反爬、
    音视频处理和多媒体处理可能导致失败、降级或耗时增加。
    """
    if academic_reference:
        if github is not None or xhs is not None or wechat is not None:
            raise ValueError('academic_reference_and_platform_options_are_mutually_exclusive')
        from use_cases.academic_fulltext import request_academic_fulltext
        return request_academic_fulltext(academic_reference, research_task_id=research_task_id,
            timeout_s=deadline_s, include_ocr=include_ocr, url=url, refresh=academic_refresh, document=document)
    log.info(f"get_content_detail: {redact_url(url)}, deep={enable_deep_analysis}, filter={enable_comment_filtering}, auto_mm={auto_multimodal}")
    from use_cases.content_detail import request_content_detail
    return request_content_detail(
        url, infer_platform=_infer_detail_platform, execute_sync=_execute_detail_request,
        enable_deep_analysis=enable_deep_analysis, enable_comment_filtering=enable_comment_filtering,
        auto_multimodal=auto_multimodal, research_session_id=research_session_id,
        work_scope_id=work_scope_id, task_scope_id=task_scope_id, research_task_id=research_task_id,
        include_transcript=include_transcript, include_comments=include_comments,
        include_ocr=include_ocr, language=language, deadline_s=deadline_s, github=github, xhs=xhs, wechat=wechat, document=document, comments=comments,
    )


def _execute_detail_request(request: DetailRequest) -> Dict:
    from use_cases.content_detail import execute_detail_request
    request = registry.prepare_detail(request)
    return execute_detail_request(request, failure_cache=detail_failure_cache, handle=_handle_detail_request,
        bind_tasks=_attach_research_session_tasks, record_evidence=_record_detail_evidence)


def _attach_research_session_tasks(result: Dict, research_session_id: str, scope: Dict | None = None) -> Dict:
    from runtime.task_scope import attach_detail_task_scope
    return attach_detail_task_scope(result, research_session_id, scope, store_factory=get_task_store)

def _handle_detail_request(request: DetailRequest) -> DetailResponse:
    started = time.time()
    platform = request.platform or _infer_detail_platform(request.url) or "unknown"
    from runtime.detail_composition import resolve_strategy
    strategy = resolve_strategy(platform)
    from use_cases.content_detail import resolve_detail_response
    if strategy is None:
        def unsupported(req):
            result = {
                "error": f"不支持的 URL: {req.url}", "failure_type": "unsupported_url",
                "supported": ["B站视频 (BV...)", "YouTube 视频 (youtube.com/watch 或 youtu.be)", "知乎 (zhihu.com)", "小红书 (xiaohongshu.com)", "猎聘职位 (liepin.com/job 或 /a/)", "BOSS直聘职位 (zhipin.com/job_detail/)", "脉脉社区帖子与公开分享 (maimai.cn/community/feed-detail 或 gossip-detail)"],
                "alternative_tools": [
                    {"tool": "kr_web_search", "when": "Use for open-web discovery before detail extraction."},
                    {"tool": "extract_web_page", "when": "Use for ordinary static article pages."},
                    {"tool": "extract_dynamic_page", "when": "Use when a public page requires browser rendering."},
                ],
                "recommended_next_action": "Use kr_web_search for discovery or extract_web_page/extract_dynamic_page for generic public pages.",
                "normalized_status": {"status": "unsupported_url", "retryable": False, "expected_degraded": True},
                "platform": platform, "url": req.url,
            }
            return attach_detail_evidence(req.url, result, infer_platform=_infer_detail_platform)
        response = resolve_detail_response(request, strategy=None, unsupported=unsupported,
            decorate=lambda value: _attach_detail_degradation(platform, request.url, value.data, value.metadata))
        _record_detail_decision(request, response, time.time() - started)
        return response
    else:
        trace_hint = {
            **current_tool_trace(),
            "tool_name": "get_content_detail",
            "strategy": type(strategy).__name__,
            "status": "running",
            "elapsed_s": 0.0,
            "retry_count": 0,
            "failure_code": "",
            "failure_tags": [],
        }
        set_current_tool_trace(trace_hint)
        normalized = resolve_detail_response(request, strategy=strategy,
            decorate=lambda value: _attach_detail_degradation(
                platform, request.url, value.data, value.metadata))
        _record_detail_decision(request, normalized, time.time() - started)
        return normalized



# ═══════════════════════════════════════════════════════════════════════
# 启动
# ═══════════════════════════════════════════════════════════════════════

from runtime.research_index import ResearchRequest

@application_tool(group="research")
def update_research(request: ResearchRequest) -> Dict:
    """记录问题、需求/尝试/覆盖、动作/探索及关键命题。action=evaluation记录评测题卡、影子提案、候选处置、可逆合并和用户反馈补偿；request_id幂等、expected_revision防并发，语义判断由Agent负责。先task_purpose声明同任务目标，再claim声明语义判断与固定依赖；按read_knowledge research/review与withdrawal_scenarios处置替代及默认撤证，report_review绑定报告正文SHA256与当前baseline。submit_report自动附中文审阅表。旧记录未声明保持未审；程序核身份、定位、版本和缺口，不判真假或饱和。"""
    from runtime.research_index import ResearchIndex
    return ResearchIndex().annotate(request)


@application_tool( group="knowledge", read_only=True)
def read_knowledge(request: ReadRequest) -> ReadResult:
    """检索本机固定证据和报告；按命中版本/位置渐进读取，不扫描私人目录。action=evaluation读取评测观察、候选、维护谱系、等待Agent队列和题卡样本片段；只读，不启动评测。"""
    from runtime.knowledge import read
    return read(request)


@application_tool( group="delivery")
def manage_research_artifacts(request: ArtifactRequest) -> Dict:
    """授权目标、交付固定报告及全部有效来源正文。新任务按动态项目/年份/首次日期时间任务归档，最终报告命名总报告.md；未引用和反证同样保留，报告与来源完成分别回读。晚绑定补交历史固定版本；旧布局用plan_archive_layout检查归属/身份/哈希，再以同plan_id调用apply_archive_layout。选取只生成链接并复用正文；人改、移动或删除受保护，命名不调用模型。"""
    from runtime.knowledge_delivery import execute
    return execute(request)


@application_tool( group="runtime")
def control_task(task_id: Annotated[str, Field(description="后台任务返回的固定ID。")],
                 action: Literal["cancel", "resume"]) -> TaskStatusResult:
    """显式取消或恢复已接受任务；不与状态查询混用，恢复沿原费用幂等范围。"""
    from runtime.task_service import TaskService
    service=TaskService()
    task=service.cancel(task_id) if action=="cancel" else service.retry(task_id)
    return {"status":"ok" if task else "not_found","task":compact_task_ref(task),"action":action}


def _bootstrap_persistent_runtime():
    """Restore durable work and browser ownership for every MCP transport."""
    from runtime.task_service import TaskService
    service = TaskService()
    service.recover()
    restored = restore_chrome_idle_cleanups()
    if restored["restored"]:
        log.info("已恢复 %s 个受管 Chrome 空闲回收计时器，其中过期 %s 个", restored["restored"], restored["overdue"])
    if os.environ.get("KR_BROWSER_SESSION_MAINTENANCE_ON_STARTUP", "1").strip().lower() in {"1", "true", "yes", "on"}:
        try:
            compacted = compact_terminal_browser_sessions(retain_closed=100, retain_failed=20)
            log.info("已完成浏览器会话终态维护：压缩 %s 条，保留活动会话 %s 条", compacted.get("removed_count", 0), compacted.get("protected_active_count", 0))
        except Exception as exc:
            log.warning("浏览器会话终态维护失败（不阻断启动）: %s", exc)
    return service.start_maintenance(keep_alive=True)
