"""
FastMCP Server — 全网知识搜索系统
=====================================
MCP Server，默认以 streamable-http 常驻模式运行，供 OpenClaw 远程连接。

启动:   python server.py
stdio:  KR_MCP_TRANSPORT=stdio python server.py
调试:   python -m mcp dev server.py          # MCP Inspector
Cursor: python -m mcp install server.py       # 注册到 Cursor

工具清单以本文件中的 @mcp.tool() 注册集合为准，并通过 capabilities.build_tool_surface()
暴露给 Agent、health_check 和文档校验。
"""
import json
import hashlib
import logging
import os
import re
import sys
import threading
import time
import importlib.util
import uuid
from logging.handlers import RotatingFileHandler
from typing import Annotated, Any, Dict, List, Literal

from pydantic import Field

from mcp.server.fastmcp import FastMCP

os.environ.setdefault("PYTHONUTF8", "1")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ── 项目根路径 ──────────────────────────────────────────────────────────
SRC_ROOT = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SRC_ROOT)
PROJECT_ROOT = SRC_ROOT
sys.path.insert(0, SRC_ROOT)
from runtime.env_loader import load_runtime_env  # noqa: E402
from runtime.paths import runtime_log_dir, runtime_state_dir  # noqa: E402
from runtime.research_ledger import (  # noqa: E402
    close_task as close_research_task,
    open_task as open_research_task,
    record_candidates as record_research_candidates,
    record_event as record_research_event,
    review_task as review_research_task,
    update_candidate_stage as update_research_candidate_stage,
)
from runtime.platform_risk import build_manual_interaction_envelope, compute_platform_cooldown, normalize_platform_risk_event  # noqa: E402
from runtime.mcp_observability import record_fallback_server_stopped, record_server_started, record_tool_list, snapshot as mcp_observability_snapshot  # noqa: E402
from runtime.mcp_runtime import source_fingerprint  # noqa: E402
from runtime.redaction import RedactingLogFilter, redact_url  # noqa: E402


load_runtime_env()
os.environ.setdefault("KR_STATE_DIR", str(runtime_state_dir()))
os.environ.setdefault("KR_LOG_DIR", str(runtime_log_dir()))

from capabilities import ACTUAL_MCP_TOOLS, build_capabilities, build_tool_surface, manual_interaction_manifest, media_policy_manifest, platform_capabilities_dict
from capabilities import capability_atlas_manifest
from capabilities import gh_cli_admission_record
from capabilities import research_quality_contract_manifest
from capabilities import source_ecology_manifest
from capabilities import validation_semantics_manifest
from academic_providers import AcademicSearchRequest
from academic_providers.service import academic_provider_status, search_academic_metadata
from collectors.platform import (
    extract_bvid,
    get_bilibili_info,
    legacy_search_bilibili,
    recover_xhs_xsec_token,
    detail_needs_fallback as xhs_detail_needs_fallback,
    extract_xhs_detail_via_cdp,
    xiaohongshu_account_state,
)
from collectors.platform import zhihu as zhihu_collectors
from collectors.platform import xiaohongshu as xhs_collectors
from collectors.platform import gh_cli_sidecar
from collectors.platform import youtube as youtube_collectors
from collectors.platform import boss as boss_collectors
from collectors.platform import liepin as liepin_collectors
from collectors.platform import maimai as maimai_collectors
from collectors.platform import v2ex as v2ex_collectors
from collectors.platform import zhilian as zhilian_collectors
from kr_core import (
    DecisionLogEvent,
    DecisionLogger,
    DetailRequest,
    DetailResponse,
    SearchRequest,
    registry,
)
from kr_core.collection import format_search_response as format_collection_search_response
from detail_strategies import (
    BilibiliDetailDeps,
    BilibiliDetailStrategy,
    RecruitmentDetailStrategy,
    XiaohongshuDetailDeps,
    XiaohongshuDetailStrategy,
    YouTubeDetailDeps,
    YouTubeDetailStrategy,
    ZhihuDetailDeps,
    ZhihuDetailStrategy,
)
from generic_web import GenericWebRequest, collect_dynamic_url, collect_url
from platform_adapters import register_default_adapters
from research_planner import build_research_plan
from routing import attach_routing_metadata, build_agent_native_fields, routing_recommends_l2, routing_snapshot
from routing.calibration import build_calibration_report
from runtime.chrome_manager import (
    XHS_CHROME_DEBUG_PORT,
    background_chrome,
    bring_chrome_to_front,
    chrome_active_operation,
    chrome_runtime_summary,
    chrome_runtime_quick_summary,
    cancel_browser_interaction,
    complete_browser_interaction,
    finish_chrome_automation,
    managed_browser_platforms,
    probe_browser_auth,
    request_user_login,
    request_browser_interaction,
    reconcile_stale_xhs_manual_interactions,
    restore_chrome_idle_cleanups,
    _chrome_debug_port,
    _chrome_debug_url,
    _cleanup_managed_chrome_platform,
    _ensure_chrome_debugging,
    _find_chrome_exe,
    _managed_chrome_profile_dir,
)
from runtime.browser_sessions import browser_sessions_summary, compact_terminal_browser_sessions, stable_hash as browser_profile_hash
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
from runtime.executables import find_node_exe
from runtime.browser_health import (
    camoufox_v2_health,
    playwright_chromium_isolated_health,
    probe_camoufox_v2_login,
    probe_camoufox_v2_search_page,
    probe_camoufox_sdk_xhs_search_page,
    probe_playwright_cdp_xhs_search_page,
    probe_playwright_chromium_launch_only,
    probe_playwright_chromium_xhs_page_load,
    probe_playwright_chromium_xhs_search_page,
    probe_playwright_chromium_xhs_detail,
    probe_playwright_chromium_xhs_login,
)
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
from runtime.architecture_standard import architecture_standard_summary, architecture_completion_summary
from runtime.knowledge_assets import build_evidence_pack_summary, knowledge_asset_schema_summary
from runtime.native_runner import (
    build_low_risk_execution_command,
    compact_patrol_contract,
    governed_capability_plan_summary,
    l2_multimodal_task_contract,
    low_risk_execution_summary,
    run_low_risk_execution_command,
    run_readonly_patrol,
    runtime_contract_summary,
    xhs_policy_gate_matrix,
)
from runtime.openclaw_native_adapter import openclaw_native_adapter_summary
from runtime.profile_registry import account_pool_selection_summary, profile_registry_internal, profile_registry_summary, raw_registry_for_platform
from runtime.xhs_account_identity import claim_xhs_account_identity, xhs_account_identity_summary
from runtime.project_state import project_governance_manifest
from runtime.runtime_environment import planning_tools_enabled
from runtime.summary_helpers import (
    decision_logs_compact_from_summary,
    low_risk_execution_probe_declaration,
    overall_status_from_summary,
    task_status_compact_from_summary,
)
from runtime.status_schema import aggregate_validation_status, canonical_status_counts, classify_runtime_payload, legacy_health_status
from runtime.tool_trace import current_tool_trace, get_tool_trace_recorder, set_current_tool_trace, traced_tool
from runtime.xhs_account_events import xhs_account_control_summary
from runtime.xhs_account_patrol import xhs_account_patrol_summary
from runtime.xhs_account_pool import xhs_account_pool_summary
from runtime.xhs_account_switcher import xhs_account_switcher_summary
from runtime.xhs_api_candidates import xhs_api_candidate_config_summary
from runtime.xhs_candidate_admission import xhs_autonomous_candidate_admission_summary
from runtime.xhs_governance import xhs_p6_p9_governance_summary
from runtime.xhs_session_governance import xhs_session_governance_summary
from runtime.xhs_route_events import xhs_route_event_summary
from runtime.xhs_route_scoring import xhs_route_scoring_summary
from runtime.xhs_stability_observer import xhs_stability_observer_summary
from runtime.xhs_tikhub_fallback import plan_tikhub_break_glass_fallback, plan_tikhub_xhs_search_fallback
from runtime.xhs_auth_watcher import restore_pending_xhs_auth_watchers
from runtime.xhs_multimodal_acceptance import xhs_multimodal_acceptance_summary
from runtime.xhs_health import get_xhs_detail_health_tracker
from runtime.xhs_health import get_xhs_chain_health_tracker
from runtime.tasks import compact_task_ref, compact_task_refs, get_task_store
from runtime.task_scope import SERVER_RUN_ID, make_task_scope
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
    extract_zhihu_article_from_html,
    extract_zhihu_article_via_cdp,
    filter_bilibili_comments,
    get_bilibili_comments,
    looks_like_zhihu_not_found as understanding_looks_like_zhihu_not_found,
    ocr_first_xhs_image,
    strip_html_text as understanding_strip_html_text,
    transcribe_bilibili,
)
from collectors.platform.bilibili import deep_analyze_youtube


def _load_xhs_scrapling_adapter():
    scrapling_path = os.path.join(PROJECT_ROOT, "media_platform", "xhs", "scrapling_adapter.py")
    spec = importlib.util.spec_from_file_location("knowledgeradar_xhs_scrapling_adapter", scrapling_path)
    if not spec or not spec.loader:
        raise ImportError(f"无法加载小红书 scrapling adapter: {scrapling_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


xhs_scrapling_adapter = _load_xhs_scrapling_adapter()

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
XHS_SEARCH_GATE_PATH = os.path.join(RUNTIME_LOG_DIR, "knowledgeradar-xhs-search-gate.jsonl")
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
    if "zhipin.com" in url:
        return "BOSS直聘"
    if "liepin.com" in url:
        return "猎聘"
    if "xiaohongshu.com" in url:
        return "小红书"
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

mcp = FastMCP(
    "全网知识搜索",
    log_level="INFO",
    host=MCP_HOST,
    port=MCP_PORT,
    streamable_http_path=MCP_STREAMABLE_HTTP_PATH,
    sse_path=MCP_SSE_PATH,
    message_path=MCP_MESSAGE_PATH,
)


def _install_mcp_observability() -> None:
    """Declare tool-list change support and observe real list requests.

    FastMCP defaults ``tools_changed`` to false.  Codex can only refresh a
    changed tool list when the server advertises the capability and emits a
    valid list response.  The wrapper records the response without exposing
    session identifiers or host-private thread state.
    """
    low_level = getattr(mcp, "_mcp_server", None)
    if low_level is None or getattr(low_level, "_kr_observability_installed", False):
        return
    from mcp.server.lowlevel.server import NotificationOptions
    from mcp.types import ListToolsRequest

    create_options = low_level.create_initialization_options

    def create_observed_options(notification_options=None, experimental_capabilities=None):
        options = notification_options or NotificationOptions()
        options.tools_changed = True
        return create_options(options, experimental_capabilities)

    low_level.create_initialization_options = create_observed_options
    original_handler = low_level.request_handlers.get(ListToolsRequest)
    if original_handler is not None:
        async def observed_list_tools(request):
            result = await original_handler(request)
            names = []
            payload = getattr(result, "root", None)
            for item in getattr(payload, "tools", []) or []:
                name = getattr(item, "name", None)
                if name:
                    names.append(str(name))
            try:
                session = low_level.request_context.session
                session_id = id(session)
            except LookupError:
                session_id = ""
            record_tool_list(
                session_id=session_id,
                tool_names=names,
                transport=MCP_TRANSPORT,
                invocation_kind="continuity_fallback" if os.environ.get("KR_CONTINUITY_FALLBACK") == "1" else "native_server",
                invocation_id=os.environ.get("KR_CONTINUITY_INVOCATION_ID", ""),
            )
            return result

        low_level.request_handlers[ListToolsRequest] = observed_list_tools
    low_level._kr_observability_installed = True

# ── API 配置 ────────────────────────────────────────────────────────────
BILI_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://search.bilibili.com/",
}
ZHIHU_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://www.zhihu.com/",
}
VENV_PYTHON = os.path.join(REPO_ROOT, ".python312", "python.exe")
_NODE_PATH = None


def _record_search_evidence(platform: str, query: str, response: Dict) -> None:
    try:
        evidence_store.append_search(platform=platform, query=query, response=response)
    except Exception as exc:
        log.debug(f"搜索证据仓库写入失败: platform={platform}, error={exc}")


def _record_detail_evidence(platform: str, url: str, response: Dict) -> None:
    try:
        evidence_store.append_detail(platform=platform, url=url, response=response)
    except Exception as exc:
        log.debug(f"详情证据仓库写入失败: platform={platform}, error={exc}")


def _record_academic_evidence(query: str, response: Dict) -> None:
    try:
        evidence_store.append_academic_search(query=query, response=response)
    except Exception as exc:
        log.debug(f"学术证据仓库写入失败: error={exc}")


def _xhs_search_cooldown_seconds() -> int:
    return int(os.environ.get("KR_XHS_SEARCH_COOLDOWN_SECONDS", "1800"))


def _xhs_search_gate_read() -> Dict:
    if not os.path.isfile(XHS_SEARCH_GATE_PATH):
        return {}
    try:
        with open(XHS_SEARCH_GATE_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()
        for line in reversed(lines[-200:]):
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if isinstance(row, dict):
                return row
    except Exception as exc:
        log.debug(f"小红书搜索门控读取失败: {exc}")
    return {}


def _xhs_search_gate_write(entry: Dict) -> None:
    try:
        os.makedirs(os.path.dirname(XHS_SEARCH_GATE_PATH), exist_ok=True)
        with open(XHS_SEARCH_GATE_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n")
    except Exception as exc:
        log.debug(f"小红书搜索门控写入失败: {exc}")


def _xhs_search_gate_state() -> Dict:
    row = _xhs_search_gate_read()
    now = time.time()
    cooldown_until = float(row.get("cooldown_until") or 0.0)
    active = bool(cooldown_until and now < cooldown_until)
    return {
        "active": active,
        "cooldown_until": cooldown_until,
        "next_retry_at": float(row.get("next_retry_at") or cooldown_until or 0.0),
        "cooldown_remaining_s": round(max(0.0, cooldown_until - now), 2) if active else 0,
        "last_outcome": str(row.get("outcome") or ""),
        "last_reason": str(row.get("reason") or ""),
        "last_search_type": str(row.get("search_type") or ""),
        "last_probe_mode": bool(row.get("probe_mode")),
        "metadata": row.get("metadata") if isinstance(row.get("metadata"), dict) else {},
        "updated_at": float(row.get("updated_at") or 0.0),
    }


def _xhs_route_receipt(result: Dict, *, run_id: str, entry_gate: Dict) -> Dict:
    """Attach a compact, redacted account/fallback receipt to every XHS search.

    The receipt is deliberately descriptive, not a second router.  It makes a
    zero-result response auditable: an Agent can see which collector stages
    actually ran, which ones were skipped, and whether a prior gate was only
    historical context rather than the reason the current request stopped.
    """
    metadata = dict(result.get("metadata") or {}) if isinstance(result, dict) else {}
    collection = metadata.get("collection") if isinstance(metadata.get("collection"), dict) else {}
    attempts = collection.get("attempts") if isinstance(collection.get("attempts"), list) else []
    observed = []
    for item in attempts:
        if not isinstance(item, dict):
            continue
        observed.append(
            {
                "stage": str(item.get("name") or ""),
                "status": str(item.get("status") or "unknown"),
                "reason": str(item.get("error_type") or item.get("detail") or "")[:160],
            }
        )
    attempted = {item["stage"] for item in observed}
    known = [
        "login_preflight",
        "xhs_account_auto_switch",
        "scrapling_cdp",
        "external_search_then_detail",
        "tikhub_break_glass",
    ]
    metadata["xhs_route_receipt"] = {
        "schema": "knowledgeradar-xhs-search-route-receipt/v1",
        "run_id": run_id,
        "entry_gate_observed": bool(entry_gate.get("active")),
        "entry_gate_reason": str(entry_gate.get("last_reason") or "")[:160],
        "attempts": observed,
        "not_attempted_routes": [name for name in known if name not in attempted],
        "terminal_status": "ok" if not result.get("error") else "degraded_or_blocked",
    }
    result["metadata"] = metadata
    return result


def _record_xhs_search_gate(*, outcome: str, reason: str, search_type: str, probe_mode: bool, cooldown_seconds: int | None = None, metadata: Dict | None = None) -> Dict:
    now = time.time()
    metadata = metadata or {}
    base_cooldown = int(cooldown_seconds or _xhs_search_cooldown_seconds())
    event = normalize_platform_risk_event(
        platform="xiaohongshu",
        operation="search",
        reason_code=reason,
        outcome=outcome,
        scope={"search_type": search_type, "probe_mode": probe_mode},
        manual_action_required=bool(metadata.get("manual_action_required")),
    )
    cooldown_policy = compute_platform_cooldown(
        event,
        base_s=base_cooldown,
        maximum_s=int(os.environ.get("KR_XHS_SEARCH_COOLDOWN_MAX_SECONDS", "7200")),
        previous_cooldown_s=int((_xhs_search_gate_read() or {}).get("cooldown_seconds") or 0),
        jitter_ratio=0.0,
        now=now,
    )
    should_cooldown = outcome in {"blocked", "failed", "degraded"} and not probe_mode
    cooldown = int(cooldown_policy["cooldown_seconds"]) if should_cooldown else 0
    entry = {
        "platform": "小红书",
        "outcome": outcome,
        "reason": reason,
        "search_type": search_type,
        "probe_mode": bool(probe_mode),
        "cooldown_seconds": cooldown,
        "cooldown_until": now + cooldown if should_cooldown else 0,
        "next_retry_at": now + cooldown if should_cooldown and cooldown > 0 else 0,
        "updated_at": now,
        "metadata": {
            **metadata,
            "risk_event": event.to_dict(),
            "cooldown_policy": cooldown_policy,
        },
    }
    _xhs_search_gate_write(entry)
    return entry


def _xhs_try_clear_login_cooldown(gate: Dict) -> Dict:
    """Clear stale XHS login cooldown after the user fixes the visible profile."""
    if not gate.get("active"):
        return gate
    reason = str(gate.get("last_reason") or "").lower()
    if not any(token in reason for token in ("登录", "login", "auth", "cookie", "cdp", "chrome")):
        return gate
    try:
        if not _ensure_chrome_debugging("xhs"):
            return gate
        state = xhs_collectors.xiaohongshu_account_state(_chrome_debug_url)
        if xhs_collectors._xhs_login_state_ok(state):
            _record_xhs_search_gate(
                outcome="ok",
                reason="login_recovered_after_manual_action",
                search_type=str(gate.get("last_search_type") or "all"),
                probe_mode=False,
                cooldown_seconds=0,
                metadata={
                    "cleared_stale_gate": gate,
                    "account_state": {
                        "code": state.get("code"),
                        "guest": state.get("guest"),
                        "confirmed": state.get("confirmed"),
                        "has_login_prompt": state.get("has_login_prompt"),
                        "has_verify_prompt": state.get("has_verify_prompt"),
                    },
                },
            )
            return _xhs_search_gate_state()
    except Exception as exc:
        log.debug(f"小红书登录冷却恢复探针失败: {exc}")
    return gate


def _xhs_manual_gate_error(gate: Dict) -> Dict:
    metadata = gate.get("metadata") if isinstance(gate.get("metadata"), dict) else {}
    risk_event = metadata.get("risk_event") if isinstance(metadata.get("risk_event"), dict) else {}
    platform_state = str(
        metadata.get("platform_state")
        or risk_event.get("reason_code")
        or metadata.get("failure_type")
        or "platform_verification_required"
    )
    failure_type = str(metadata.get("failure_type") or "anti_bot_verification")
    return {
        "error": "小红书搜索上次触发登录或安全验证，已暂停重复探测并等待人工处理",
        "type": failure_type,
        "failure_type": failure_type,
        "platform": "小红书",
        "stage": "search",
        "retryable": False,
        "manual_action_required": True,
        "platform_state": platform_state,
        "cooldown_seconds_remaining": gate.get("cooldown_remaining_s", 0),
        "next_retry_at": gate.get("next_retry_at") or gate.get("cooldown_until") or 0,
        "last_reason": gate.get("last_reason", ""),
        "last_search_type": gate.get("last_search_type", ""),
        "diagnostic_evidence": [
            f"cooldown_until={gate.get('cooldown_until')}",
            f"next_retry_at={gate.get('next_retry_at') or gate.get('cooldown_until')}",
            f"last_outcome={gate.get('last_outcome')}",
            "cooldown_preserved_manual_interaction=true",
        ],
        "recommended_action": "请先完成小红书登录或安全验证：health_check(mode='request_browser_interaction:xhs:platform_verification_required')；完成后调用 health_check(mode='complete_browser_interaction:xhs') 再重试。",
    }


def _xhs_budget_state() -> Dict:
    detail = get_xhs_detail_health_tracker().summary(recent_limit=5)
    gate = _xhs_search_gate_state()
    success_rate = detail.get("success_rate")
    detail_read_enabled = True
    if isinstance(success_rate, (int, float)) and detail.get("total", 0) >= 3:
        detail_read_enabled = success_rate >= XHS_DETAIL_SUCCESS_RATE_DISABLE_THRESHOLD
    return {
        "status": "degraded" if gate.get("active") or not detail_read_enabled else "ok",
        "search": {
            "max_limit": XHS_DEFAULT_SEARCH_LIMIT,
            "probe_limit": XHS_PROBE_SEARCH_LIMIT,
            "cooldown_active": bool(gate.get("active")),
            "cooldown_remaining_s": gate.get("cooldown_remaining_s", 0),
            "last_outcome": gate.get("last_outcome", ""),
            "last_reason": gate.get("last_reason", ""),
        },
        "detail": {
            "enabled": detail_read_enabled,
            "disable_success_rate_threshold": XHS_DETAIL_SUCCESS_RATE_DISABLE_THRESHOLD,
            "recent_success_rate": success_rate,
            "recent_total": detail.get("total"),
        },
        "multimodal": {
            "default_auto_multimodal": False,
            "ocr_rule": "auto_multimodal_true_and_images_present",
            "ocr_trigger_policy": os.environ.get("KR_XHS_OCR_TRIGGER_POLICY", "image_presence"),
        },
    }


def _xhs_diagnostic_control_state() -> Dict:
    policy = get_degradation_policy()
    return {
        "schema_version": "xhs-diagnostic-control/v1",
        "status": "ok",
        "production_collection": {
            "primary": f"chrome_{XHS_CHROME_DEBUG_PORT}_scrapling_cdp",
            "force_probe": "diagnostic_light_action",
            "raw_cdp": "diagnostic_read_only",
            "nodriver": "diagnostic_read_only_candidate",
            "playwright_chromium_isolated": "isolated_backup_candidate",
        },
        "isolated_candidates": {
            "playwright_chromium": playwright_chromium_isolated_health(),
            "camoufox_v2": camoufox_v2_health(),
        },
        "bridge": {
            "search_fallback": {
                "status": "diagnostic_only",
                "production_enabled": os.environ.get("KR_XHS_BRIDGE_PRODUCTION_ENABLED", "0").strip().lower() in {"1", "true", "yes", "on"},
                "breaker_key": "collector:xhs.search_bridge_fallback",
                "breaker": policy.is_open("collector:xhs.search_bridge_fallback"),
            },
            "detail_bridge": {
                "status": "guarded_by_breaker",
                "breaker_key": "collector:xhs.detail_bridge",
                "breaker": policy.is_open("collector:xhs.detail_bridge"),
            },
        },
        "acceptance_gate": {
            "bridge_rejoin_requires": [
                "success_rate>=0.70 on acceptance set",
                "bridge_parse_failed<=0.15",
                "p95_latency_s<=10",
                "unique_recovery_count>=3",
            ]
        },
    }


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
    """Attach a manual-action advisory; ordinary searches never open Chrome."""

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
    if platform_id not in set(managed_browser_platforms()):
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
    interaction = {
        "status": "action_required_not_opened",
        "platform": platform_id,
        "reason": reason,
        "original_tool": original_tool,
        "manual_open_mode": f"health_check(mode='request_browser_interaction:{platform_id}:{reason}')",
        "detail": "普通搜索遇到登录、验证码或风控时不会自动打开 Chrome；只有明确请求人工交互才会打开受管窗口。",
    }

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
    cached = search_cache.get(key)
    if cached is not None:
        return cached
    started = time.perf_counter()
    result = fn()
    if isinstance(result, dict) and not result.get("error"):
        result.setdefault("metadata", {})
        result["metadata"]["search_quality_snapshot"] = snapshot_from_response(
            query,
            result,
            elapsed_ms=float((result.get("metadata") or {}).get("elapsed_ms") or (time.perf_counter() - started) * 1000),
        )
        result = search_cache.set(key, result)
    return result


def _find_xhs_bridge() -> str:
    """自动查找 xhs_mcp_bridge.cjs：
    1. 环境变量 XHS_BRIDGE_PATH
    2. repo-local bridge/xhs_mcp_bridge.cjs
    """
    # 环境变量优先
    env_path = os.environ.get("XHS_BRIDGE_PATH", "")
    if env_path and os.path.isfile(env_path):
        return env_path

    # Keep the bridge inside this repo so the MCP server is deployable.
    local_bridge = os.path.join(REPO_ROOT, "bridge", "xhs_mcp_bridge.cjs")
    if os.path.isfile(local_bridge):
        return local_bridge

    if os.environ.get("KR_ALLOW_LEGACY_XHS_BRIDGE", "").strip().lower() in {"1", "true", "yes"}:
        legacy = os.environ.get("KR_LEGACY_XHS_BRIDGE", "")
        if os.path.isfile(legacy):
            log.warning("使用 legacy XHS bridge 回退；建议改用 XHS_BRIDGE_PATH 或 repo-local bridge")
            return legacy

    raise FileNotFoundError("xhs_mcp_bridge.cjs 未找到；请设置 XHS_BRIDGE_PATH 或放置 repo-local bridge/xhs_mcp_bridge.cjs")


XHS_BRIDGE_PATH = _find_xhs_bridge()


# ═══════════════════════════════════════════════════════════════════════
# 工具 1: expand_keywords
# ═══════════════════════════════════════════════════════════════════════

@traced_tool("expand_keywords", strategy="llm_keyword_expansion")
def expand_keywords(topic: Annotated[str, Field(description="待拆解的主题文本。返回若干可直接提交给搜索工具的短查询词。")]) -> List[str]:
    """生成多组搜索查询词。

    返回围绕同一主题的短查询词列表，不访问外部网页，不读取详情页。
    这是 legacy planning helper；可通过环境开关从 MCP 工具面隐藏。
    """
    log.info(f"expand_keywords: {topic}")
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
    mcp.tool()(expand_keywords)
    mcp.tool()(plan_research)


# ═══════════════════════════════════════════════════════════════════════
# 工具 1.6: analyze_decision_logs
# ═══════════════════════════════════════════════════════════════════════

@mcp.tool()
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


@mcp.tool()
@traced_tool("get_task_status", strategy="runtime_task_store")
def get_task_status(
    task_id: Annotated[str, Field(description="任务 ID。传 cancel:<task_id> 可取消 queued/running 任务；传 summary 或 compact 返回轻量摘要。")] = "",
    limit: Annotated[int, Field(description="返回最近任务数量。", ge=1, le=100)] = 20,
    compact: Annotated[bool, Field(description="为 true 时返回聚合摘要，不返回完整最近任务列表。")] = False,
    wait: Annotated[bool, Field(description="为 true 时等待指定 task/scope/source 的阻塞任务进入终态；可能变慢。")] = False,
    max_wait_s: Annotated[float, Field(description="wait=true 时最长等待秒数；0 表示使用运行时默认值。", ge=0, le=600)] = 0.0,
    research_session_id: Annotated[str, Field(description="旧研究会话别名。普通调用留空；保留兼容。")] = "",
    work_scope_id: Annotated[str, Field(description="工作作用域 ID。由上游详情调用返回；普通调用留空。")] = "",
    task_scope_id: Annotated[str, Field(description="任务作用域 ID。由上游详情调用返回；普通调用留空。")] = "",
    source_url: Annotated[str, Field(description="源内容 URL。可用于等待绑定到该来源的后台任务。")] = "",
    content_id: Annotated[str, Field(description="平台内容 ID。可用于等待绑定到该内容的后台任务。")] = "",
) -> Dict:
    """查询后台理解任务状态。

    不传 task_id 时返回最近任务列表；传入 task_id 时返回单个任务详情。
    可取消 queued/running 任务；可等待绑定范围内的阻塞任务进入终态。
    wait=true 可能阻塞到 max_wait_s。
    """
    store = get_task_store()
    limit = max(1, min(int(limit or 20), 100))
    cleanup = store.cleanup_stale_tasks(limit=limit)
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
        return {
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
    if str(task_id or "").strip().lower() in {"summary", "compact"}:
        compact = True
        task_id = ""
    if task_id:
        if task_id.startswith("cancel:"):
            real_task_id = task_id.split(":", 1)[1].strip()
            if not real_task_id:
                return {"status": "bad_request", "error": "cancel requires task_id"}
            task = store.cancel_task(real_task_id)
            return {
                "schema_version": "knowledgeradar-task-status/v2",
                "status": "ok",
                "task": compact_task_ref(task),
                "cancelled": True,
                "stale_cleanup": {
                    "cleaned_count": cleanup.get("cleaned_count", 0),
                    "cleaned": compact_task_refs(cleanup.get("cleaned") or [], limit=3),
                    "reason": cleanup.get("reason", ""),
                },
            }
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
    cleanup = store.cleanup_stale_tasks(limit=limit)
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
_ECOLOGY_TOOL_MAP = {
    "generic_web_ecology": ["kr_web_search", "extract_web_page", "extract_dynamic_page"],
    "academic_literature_ecology": ["search_academic", "extract_web_page"],
    "github_repository_ecology": ["search_github_repositories", "kr_web_search"],
    "youtube_video_ecology": ["search_youtube", "get_content_detail", "get_task_status"],
    "wechat_public_article_ecology": ["search_wechat_articles", "kr_web_search", "extract_web_page", "extract_dynamic_page"],
    "bilibili_video_ecology": ["search_bilibili", "get_content_detail", "get_task_status"],
    "zhihu_discussion_ecology": ["search_zhihu", "get_content_detail"],
    "xiaohongshu_experience_ecology": ["search_xiaohongshu", "get_content_detail"],
}
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
    "admission": "requires_kr_research_route_plan_first",
    "status": "host_agent_invoked",
    "relationship_to_kr": "finger_of_kr_hand; not an independent fallback path",
    "usage_rule": "If the Agent uses built-in web/search, it must treat each call as execution of this KR-authorized wave and record the wave_id in trace/evidence.",
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
    keyword_rules = {
        "academic_literature_ecology": ["paper", "论文", "学术", "doi", "arxiv", "survey", "benchmark", "文献"],
        "github_repository_ecology": ["github", "repo", "repository", "开源", "代码", "实现"],
        "youtube_video_ecology": ["youtube", "海外视频", "公开视频"],
        "bilibili_video_ecology": ["b站", "bilibili", "视频", "弹幕", "up主"],
        "wechat_public_article_ecology": ["微信", "公众号", "公开文章"],
        "zhihu_discussion_ecology": ["知乎", "问答", "观点谱系"],
        "xiaohongshu_experience_ecology": ["小红书", "图文", "生活经验", "体验"],
    }
    considered: list[Dict] = []
    for ecology_id, ecology in ecologies.items():
        reasons: list[str] = []
        if ecology_id == "generic_web_ecology":
            reasons.append("broad_discovery_baseline")
        if ecology_id in hints:
            reasons.append("explicit_hint")
        if any(token.lower() in haystack for token in keyword_rules.get(ecology_id, [])):
            reasons.append("task_or_evidence_need_signal")
        tools = list(ecology.get("candidate_tools") or [])
        detail_tools = [tool for tool in tools if tool in {"extract_web_page", "extract_dynamic_page", "get_content_detail", "get_task_status"}]
        considered.append(
            {
                "source_ecology": ecology_id,
                "status": "initial_candidate",
                "relevance_signals": reasons or ["available_for_model_consideration"],
                "candidate_tools": tools,
                "detail_affordance": detail_tools,
                "unique_information_value": list(ecology.get("reveals") or [])[:2],
                "known_limits": list(ecology.get("known_limits") or []),
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
                "tools": _ECOLOGY_TOOL_MAP.get(ecology_id, []),
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
            "Built-in web/search is not a parallel bypass. Use it only after this KR route plan as host_internal_web_wave and record wave_id plus relationship_to_kr.",
            "Do not treat shell import server.py as natural MCP tool use in user-facing research tasks.",
        ],
    }


def _research_first_wave(plan: Dict, limit: int, research_task_id: str = "") -> list[Dict]:
    query = plan.get("query") or ""
    results: list[Dict] = []
    if not query:
        return results
    per_tool_limit = max(1, min(int(limit or 5), 8))
    for ecology_id in plan.get("selected_source_ecologies") or []:
        if ecology_id not in _FIRST_WAVE_SAFE_ECOLOGIES:
            results.append(
                {
                    "source_ecology": ecology_id,
                    "status": "skipped",
                    "candidate_stage": "degraded_or_blocked",
                    "reason": "not_first_wave_safe; use explicit low-level tool if the Agent decides this source is worth the risk/cost",
                    "candidate_tools": _ECOLOGY_TOOL_MAP.get(ecology_id, []),
                    "next_candidate_action": "Keep as a candidate; decide later from evidence value, runtime state, cost, and manual boundaries.",
                }
            )
            continue
        try:
            if ecology_id == "academic_literature_ecology":
                payload = search_academic(query=query, limit=min(per_tool_limit, 5), provider="auto")
                tool_name = "search_academic"
            elif ecology_id == "github_repository_ecology":
                payload = search_github_repositories(query=query, limit=per_tool_limit)
                tool_name = "search_github_repositories"
            elif ecology_id == "youtube_video_ecology":
                payload = search_youtube(keyword=query, limit=per_tool_limit)
                tool_name = "search_youtube"
            elif ecology_id == "wechat_public_article_ecology":
                payload = search_wechat_articles(query=query, limit=per_tool_limit)
                tool_name = "search_wechat_articles"
            else:
                payload = kr_web_search(query=query, limit=per_tool_limit, provider="auto")
                tool_name = "kr_web_search"
            candidate_items = (payload.get("items") or [])[:per_tool_limit]
            candidate_set = _research_candidate_records(ecology_id, candidate_items)
            if research_task_id and candidate_items:
                record_research_candidates(
                    task_id=research_task_id,
                    source_ecology=ecology_id,
                    tool=tool_name,
                    items=candidate_items,
                    query=query,
                    language="zh" if re.search(r"[\u4e00-\u9fff]", query) else "non_zh",
                    intent_label="first_wave_discovery",
                )
            results.append(
                {
                    "source_ecology": ecology_id,
                    "tool": tool_name,
                    "status": "ok" if not payload.get("error") else "degraded",
                    "candidate_stage": "discovered_candidate" if not payload.get("error") else "degraded_or_blocked",
                    "total": payload.get("total", len(payload.get("items") or [])),
                    "items": candidate_items,
                    "candidate_set": candidate_set,
                    "metadata": payload.get("metadata", {}),
                    "error": payload.get("error", ""),
                    "next_candidate_action": "Inspect selected candidate details, verify source identity, cross-check important claims, and seek counterevidence where it can change the conclusion.",
                }
            )
        except Exception as exc:
            results.append(
                {
                    "source_ecology": ecology_id,
                    "status": "error",
                    "candidate_stage": "degraded_or_blocked",
                    "error": str(exc),
                    "candidate_tools": _ECOLOGY_TOOL_MAP.get(ecology_id, []),
                    "next_candidate_action": "Record the boundary and let the Agent choose an alternative source or a later retry; do not treat it as evidence absence.",
                }
            )
    return results


@mcp.tool()
@traced_tool("kr_research", strategy="workflow_research_perception")
def kr_research(
    task: Annotated[str, Field(description="研究/查证/感知任务目标。返回路线、来源生态和可选第一波证据。")],
    mode: Annotated[Literal["plan_only", "first_wave", "deep_route"], Field(description="plan_only 只规划；first_wave 执行低风险首轮搜索；deep_route 返回深度路线但不自动执行高风险详情。")] = "plan_only",
    budget: Annotated[Literal["fast", "balanced", "deep", "diagnostic"], Field(description="预算档位；控制建议深度和首轮数量，不固定工具序列。")] = "balanced",
    source_ecology_hints: Annotated[str, Field(description="可选来源生态提示，逗号分隔，如 academic_literature_ecology,github_repository_ecology。")] = "",
    evidence_needs: Annotated[str, Field(description="可选证据需求，例如官方来源、学术、平台讨论、实现证据、近期性。")] = "",
    limit: Annotated[int, Field(description="first_wave 每个低风险来源返回候选数量。", ge=1, le=10)] = 5,
    research_task_id: Annotated[str, Field(description="可选研究任务 ID。用于持久记录本次路线与后续完成状态；不是 host call-id 或 trace_context。")]= "",
) -> Dict:
    """高层研究感知入口。

    用于重度研究、查证、治理和跨来源证据任务的第一步。返回 KR 能力握手、
    来源生态路线和可选第一波低风险证据；不会自动启动登录态浏览器、
    不绕过验证码/付费墙，也不会把路线固定成强制工具序列。
    """
    clean_mode = mode if mode in _RESEARCH_MODE_VALUES else "plan_only"
    clean_budget = budget if budget in _RESEARCH_BUDGET_VALUES else "balanced"
    hints = _split_hint_list(source_ecology_hints)
    log.info("kr_research: mode=%s budget=%s task=%r", clean_mode, clean_budget, task)
    plan = _research_route_plan(
        task=task,
        mode=clean_mode,
        budget=clean_budget,
        source_ecology_hints=hints,
        evidence_needs=evidence_needs,
    )
    ledger = open_research_task(
        objective=task,
        budget=clean_budget,
        considered=list(plan.get("considered_source_ecologies") or []),
        task_id=research_task_id,
    )
    record_research_event(
        task_id=ledger["research_task_id"],
        kind="query_family_created",
        source_ecology="research_route",
        tool="kr_research",
        language="zh" if re.search(r"[\u4e00-\u9fff]", _research_query(task, evidence_needs)) else "non_zh",
        intent_label="route_admission",
        query=_research_query(task, evidence_needs),
        metadata={"mode": clean_mode, "budget": clean_budget},
    )
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
            "status": ledger["status"],
            "ledger_schema": ledger["schema"],
            "completion_rule": "Use finalize_research_task after autonomous research. Every considered ecology must be recorded as used, skipped, blocked, not_relevant, or not_reached; this is an outcome record, not a tool route.",
        },
        "first_wave": [],
        "fallback_policy": {
            "builtin_web": "Allowed only as host_internal_web_wave after KR route admission; record wave_id and relationship_to_kr in reports.",
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


@mcp.tool()
@traced_tool("record_research_candidates", strategy="research_candidate_ledger")
def record_research_candidates_tool(
    research_task_id: Annotated[str, Field(description="kr_research 返回的研究任务 ID。")],
    source_ecology: Annotated[str, Field(description="候选来自的来源生态。")],
    tool: Annotated[str, Field(description="本次发现候选所用工具。")],
    candidates: Annotated[List[Dict], Field(description="本次已获得的候选对象。系统只保存不可逆候选 ID、阶段和低基数元数据，不保存 URL、标题、原查询或正文。")],
    query: Annotated[str, Field(description="本次实际查询；只即时计算 HMAC 指纹，绝不写入账本。")]= "",
    language: Annotated[str, Field(description="本次查询语言标签，例如 zh、en 或 mixed。")]= "",
    intent_label: Annotated[str, Field(description="查询意图标签，例如 implementation、counterexample、user_experience。")]= "",
    receipt_id: Annotated[str, Field(description="刚完成的显式 research_task_id 工具调用返回的 research_receipt.receipt_id；缺失时仅记录为未核验候选。")]= "",
) -> Dict:
    """把 Agent 已决定执行的低层发现纳入同一候选池；不指定后续工具或轮次。"""
    return record_research_candidates(
        task_id=research_task_id, source_ecology=source_ecology, tool=tool, items=candidates,
        query=query, language=language, intent_label=intent_label, receipt_id=receipt_id,
    )


@mcp.tool()
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


@mcp.tool()
@traced_tool("review_research_progress", strategy="research_gap_review")
def review_research_progress(
    research_task_id: Annotated[str, Field(description="kr_research 返回的研究任务 ID。")],
    phase: Annotated[Literal["after_archaeology", "after_first_candidates", "before_delivery"], Field(description="低频审阅阶段。")],
) -> Dict:
    """指出尚未被记录的证据缺口；这是停止审阅，不命令 Agent 使用任何平台、语言或工具。"""
    return review_research_task(task_id=research_task_id, phase=phase)


@mcp.tool()
@traced_tool("finalize_research_task", strategy="research_delivery_closeout")
def finalize_research_task(
    research_task_id: Annotated[str, Field(description="kr_research 返回的研究任务 ID。")],
    ecology_outcomes: Annotated[List[Dict], Field(description="每个 considered source ecology 的 outcome（used/strategic_skip/blocked/not_relevant/not_reached）、原因、receipt_ids；战略跳过/不相关还须 claim_gap_ids 和 reopen_condition。")],
    stop_rationale: Annotated[str, Field(description="模型自主停止的证据边际收益/边界理由。")],
    key_claims: Annotated[List[Dict], Field(description="关键主张及 supporting_evidence_ids、supporting_receipt_ids；高重要性主张缺少真实回执支持将返回 needs_repair。")],
    quality_status: Annotated[str, Field(description="check_research_quality 的结果，深研应为 pass。")]= "",
    transcript_status: Annotated[str, Field(description="Codex transcript 可见性，如 available、partial 或 unavailable。")]= "unavailable",
    report_path: Annotated[str, Field(description="深研 Markdown 报告的绝对路径；deep 必填。")]= "",
    evidence_path: Annotated[str, Field(description="报告 evidence sidecar 的绝对路径；deep 必填。")]= "",
    quality_receipt_path: Annotated[str, Field(description="check_research_quality --receipt 生成的哈希绑定回执；deep 必填。")]= "",
) -> Dict:
    """记录深研完成状态；不规定模型应该用哪些工具或来源。"""
    result = close_research_task(
        task_id=research_task_id,
        ecology_outcomes=ecology_outcomes,
        stop_rationale=stop_rationale,
        key_claims=key_claims,
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
        "next_action": "Repair missing outcomes or unsupported critical claims before describing a deep research delivery as complete."
        if result.get("status") == "needs_repair"
        else "Research state is accepted for decision, subject to explicit host observability boundaries.",
    }


# ═══════════════════════════════════════════════════════════════════════
# 工具 2: kr_web_search
# ═══════════════════════════════════════════════════════════════════════

@mcp.tool()
@traced_tool("kr_web_search", strategy="provider_fallback")
def kr_web_search(
    query: Annotated[str, Field(description="开放 Web 搜索查询词。返回候选网页、新闻、博客、文档、项目主页等 URL 和摘要。")],
    limit: Annotated[int, Field(description="返回候选结果数量。", ge=1, le=20)] = 5,
    freshness: Annotated[Literal["", "day", "week", "month", "year"], Field(description="时间过滤范围。空字符串表示不限制时间。")] = "",
    provider: Annotated[Literal["auto", "tavily", "anysearch", "brave", "exa", "searxng", "codex_web_search", "codex_builtin_web_search", "youtube", "yt", "github", "gh"], Field(description="搜索后端。auto 使用已配置波次；codex_web_search/codex_builtin_web_search 仅在对应 host capability card 有已验证调用契约时可用；youtube/yt/github/gh 为兼容旧调用的别名。")] = "auto",
    include_raw_content: Annotated[bool, Field(description="是否请求后端返回原始正文。可能增加响应体积和耗时。")] = False,
    research_task_id: Annotated[str, Field(description="可选的 kr_research 任务 ID；提供后返回可供账本绑定的 research_receipt。")]= "",
) -> Dict:
    """开放 Web 搜索。

    返回普通网页、新闻、博客、技术文档、论文页面、项目主页等候选 URL 和摘要。
    不返回封闭平台登录内容。include_raw_content 可能增加耗时和响应体积。
    """
    log.info(f"kr_web_search: query={query!r}, limit={limit}, freshness={freshness}, provider={provider}")
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
    request = WebSearchRequest(
        query=query,
        limit=limit,
        freshness=freshness,
        provider=provider,
        include_raw_content=include_raw_content,
    )
    result = _cached_search(
        "web",
        query,
        limit,
        lambda: search_web(request).to_mcp_dict(),
        search_type=freshness,
        provider=provider,
        freshness=freshness,
        include_raw_content=include_raw_content,
        options=request.options,
    )
    log.info(
        "  -> provider=%s total=%s fallback=%s attempted=%s",
        result.get("provider"),
        result.get("total"),
        result.get("fallback_used"),
        result.get("attempted_providers"),
    )
    _record_search_evidence("web", query, result)
    return result


def _wechat_article_queries(query: str, account_hint: str = "") -> List[str]:
    clean_query = " ".join(str(query or "").split())
    clean_account = " ".join(str(account_hint or "").split())
    base_terms = " ".join(term for term in (clean_account, clean_query) if term).strip()
    if not base_terms:
        return []
    candidates = [
        f"site:mp.weixin.qq.com/s {base_terms}",
        f"site:mp.weixin.qq.com {base_terms}",
    ]
    if clean_account and clean_query:
        candidates.append(f"site:mp.weixin.qq.com/s {clean_query}")
    seen: set[str] = set()
    queries: List[str] = []
    for candidate in candidates:
        if candidate not in seen:
            seen.add(candidate)
            queries.append(candidate)
    return queries


def _wechat_article_url(url: str) -> bool:
    lowered = str(url or "").lower()
    return "mp.weixin.qq.com" in lowered


def _wechat_annotated_item(item: Dict, discovery_query: str) -> Dict:
    annotated = dict(item or {})
    annotated["source_type"] = "wechat_public_account_article"
    annotated["discovery_query"] = discovery_query
    annotated["recommended_extract_tool"] = "extract_web_page"
    annotated["dynamic_fallback_tool"] = "extract_dynamic_page"
    annotated["detail_tool_supported"] = False
    return annotated


@mcp.tool()
@traced_tool("search_github_repositories", strategy="platform_sidecar")
def search_github_repositories(
    query: Annotated[str, Field(description="GitHub 仓库搜索关键词或 GitHub search 语法。返回仓库、README/issue 线索和元数据。")],
    limit: Annotated[int, Field(description="返回仓库数量。", ge=1, le=20)] = 10,
    research_task_id: Annotated[str, Field(description="可选的 kr_research 任务 ID；提供后返回 research_receipt。")]= "",
) -> Dict:
    """GitHub 仓库搜索。

    返回仓库标题、URL、简介、语言、stars、更新时间和可用的 README/issue 线索。
    依赖 GitHub 可访问性和速率限制；认证不可用时会降级或返回错误。
    """
    clean_limit = max(1, min(int(limit or 10), 20))
    log.info("search_github_repositories: query=%r, limit=%s", query, clean_limit)
    try:
        result = gh_cli_sidecar.search_repositories(query, limit=clean_limit)
    except Exception as exc:
        failure_code = getattr(exc, "metadata", {}).get("failure_code", "UNKNOWN")
        result = {
            "query": query,
            "provider": "github",
            "platform": "GitHub",
            "items": [],
            "total": 0,
            "fallback_used": False,
            "attempted_providers": ["github"],
            "error": {
                "type": failure_code,
                "message": str(exc),
                "expected_degraded": failure_code in {"LOGIN_REQUIRED", "DEPENDENCY_CONFLICT", "PROVIDER_UNAVAILABLE"},
            },
            "metadata": {
                "sidecar": "gh_cli",
                "strategy": "gh_cli_sidecar",
                "status_class": "EXPECTED_DEGRADED" if failure_code in {"LOGIN_REQUIRED", "DEPENDENCY_CONFLICT", "PROVIDER_UNAVAILABLE"} else "FAIL",
            },
        }
    result.setdefault("platform", "GitHub")
    result.setdefault("metadata", {})
    result["metadata"].setdefault("actual_mcp_tool", "search_github_repositories")
    _record_search_evidence("GitHub", query, result)
    return result


@mcp.tool()
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
    log.info("search_youtube: keyword=%r, limit=%s", keyword, clean_limit)
    result = youtube_collectors.search_youtube(keyword, limit=clean_limit)
    result.setdefault("platform", "YouTube")
    result.setdefault("metadata", {})
    result["metadata"].setdefault("actual_mcp_tool", "search_youtube")
    _record_search_evidence("YouTube", keyword, result)
    return result


@mcp.tool()
@traced_tool("search_wechat_articles", strategy="generic_web_wechat_l1")
def search_wechat_articles(
    query: Annotated[str, Field(description="微信公众号文章搜索关键词。返回公开文章候选 URL、标题、摘要、发布时间线索和来源线索。")],
    limit: Annotated[int, Field(description="返回文章候选数量。", ge=1, le=20)] = 10,
    freshness: Annotated[Literal["", "day", "week", "month", "year"], Field(description="时间过滤范围。空字符串表示不限制时间。")] = "",
    account_hint: Annotated[str, Field(description="公众号名称或账号线索；为空时按主题检索。")] = "",
    research_task_id: Annotated[str, Field(description="可选的 kr_research 任务 ID；提供后返回 research_receipt。")]= "",
) -> Dict:
    """微信公众号公开文章搜索。

    返回公开文章候选 URL、标题、摘要、发布时间线索和来源线索。
    通过开放 Web 检索发现公开页面；不使用登录态、Cookie、后台接口或第三方代理。
    """
    clean_limit = max(1, min(int(limit or 10), 20))
    queries = _wechat_article_queries(query, account_hint)
    log.info("search_wechat_articles: query=%r, limit=%s, freshness=%s, query_count=%s", query, clean_limit, freshness, len(queries))
    if not queries:
        result = {
            "query": query,
            "platform": "微信公众号",
            "provider": "generic_web_wechat_l1",
            "items": [],
            "total": 0,
            "fallback_used": False,
            "attempted_providers": [],
            "error": {"type": "bad_request", "message": "query is required"},
            "metadata": {
                "actual_mcp_tool": "search_wechat_articles",
                "integration_level": "L1_query_templates",
                "source_boundary": "open_web_index_only",
            },
        }
        _record_search_evidence("微信公众号", str(query or ""), result)
        return result

    per_query_limit = max(3, min(clean_limit, 10))
    items: List[Dict] = []
    seen_urls: set[str] = set()
    attempted_providers: set[str] = set()
    fallback_used = False
    filtered_non_wechat_count = 0
    errors: List[Dict] = []

    for discovery_query in queries:
        request = WebSearchRequest(
            query=discovery_query,
            limit=per_query_limit,
            freshness=freshness,
            provider="auto",
            include_raw_content=False,
        )
        try:
            provider_result = _cached_search(
                "wechat_articles",
                discovery_query,
                per_query_limit,
                lambda request=request: search_web(request).to_mcp_dict(),
                search_type=freshness,
                provider="auto",
            )
        except Exception as exc:
            errors.append({"query": discovery_query, "type": type(exc).__name__, "message": str(exc)[:300]})
            continue
        for provider_name in provider_result.get("attempted_providers") or []:
            attempted_providers.add(str(provider_name))
        if provider_result.get("provider"):
            attempted_providers.add(str(provider_result.get("provider")))
        fallback_used = fallback_used or bool(provider_result.get("fallback_used"))
        if provider_result.get("error"):
            errors.append({"query": discovery_query, "error": provider_result.get("error")})
        for item in provider_result.get("items") or []:
            url = str((item or {}).get("url") or "")
            if not _wechat_article_url(url):
                filtered_non_wechat_count += 1
                continue
            if url in seen_urls:
                continue
            seen_urls.add(url)
            items.append(_wechat_annotated_item(item, discovery_query))
            if len(items) >= clean_limit:
                break
        if len(items) >= clean_limit:
            break

    result = {
        "query": query,
        "platform": "微信公众号",
        "provider": "generic_web_wechat_l1",
        "items": items[:clean_limit],
        "total": len(items[:clean_limit]),
        "fallback_used": fallback_used,
        "attempted_providers": sorted(attempted_providers),
        "metadata": {
            "actual_mcp_tool": "search_wechat_articles",
            "integration_level": "L1_query_templates",
            "strategy": "open_web_index_query_templates",
            "queries": queries,
            "source_boundary": "open_web_index_only",
            "does_not_use": ["wechat_login", "wechat_cookie", "wechat_official_api", "sogou_parser", "third_party_data_api"],
            "recommended_detail_tool": "extract_web_page",
            "dynamic_fallback_tool": "extract_dynamic_page",
            "detail_tool_supported": False,
            "l2_candidate_provider": "sogou_weixin_parser",
            "filtered_non_wechat_count": filtered_non_wechat_count,
        },
    }
    if errors:
        result["metadata"]["errors"] = errors[:3]
    _record_search_evidence("微信公众号", str(query or ""), result)
    return result


# ═══════════════════════════════════════════════════════════════════════
# 工具 2.5: search_academic
# ═══════════════════════════════════════════════════════════════════════

@mcp.tool()
@traced_tool("search_academic", strategy="academic_api_metadata")
def search_academic(
    query: Annotated[str, Field(description="学术检索查询、DOI、arXiv 线索或用户提供的引用文本/文件线索。返回题名、作者、年份、DOI、摘要、开放全文状态等元数据。")],
    limit: Annotated[int, Field(description="返回文献数量。", ge=1, le=20)] = 5,
    provider: Annotated[str, Field(description="学术数据源名称。auto 使用已配置的公开元数据和开放全文链；显式值用于指定单个数据源。")] = "openalex",
    research_task_id: Annotated[str, Field(description="可选的 kr_research 任务 ID；提供后返回 research_receipt。")]= "",
) -> Dict:
    """开放学术资料和元数据搜索。

    返回 title/authors/year/doi/url/source/oa_status/abstract/full_text_status/access_mode 等字段。
    只获取公开元数据和开放全文线索，不绕过登录、验证码、付费墙、文献传递或机构授权。
    """
    log.info(f"search_academic: query={query!r}, limit={limit}, provider={provider}")
    request = AcademicSearchRequest(query=query, limit=limit, provider=provider)
    result = search_academic_metadata(request).to_mcp_dict()
    _record_academic_evidence(query, result)
    return result


# ═══════════════════════════════════════════════════════════════════════
# 工具 3: extract_web_page
# ═══════════════════════════════════════════════════════════════════════

@mcp.tool()
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

@mcp.tool()
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

@mcp.tool()
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

@mcp.tool()
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
    gate = _xhs_search_gate_state()
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
        options={"probe_mode": probe_mode, "cache_buster": "force_probe" if probe_mode else ""},
    )
    cache_search_type = f"{search_type}|probe" if probe_mode else search_type
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
        result = _xhs_route_receipt(result, run_id=run_id, entry_gate=gate)
    _record_search_evidence("小红书", keyword, result)
    return result


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
    if not isinstance(result, dict) or not result.get("error"):
        return result
    error = result.get("error") if isinstance(result.get("error"), dict) else {"error": str(result.get("error"))}
    enriched = dict(error)
    embedded_metadata = enriched.get("metadata") if isinstance(enriched.get("metadata"), dict) else {}
    for key in ("probe_mode", "platform_state", "manual_action_required", "recommended_action", "failure_type"):
        if key in embedded_metadata:
            enriched[key] = embedded_metadata[key]
    enriched.setdefault("platform", "小红书")
    enriched.setdefault("stage", "search")
    admission = classify_admission_state(enriched)
    status_class = admission.get("status_class")
    is_manual = status_class == "NEEDS_INTERACTION"
    enriched["manual_action_required"] = bool(admission.get("manual_action_required"))
    enriched["status_class"] = status_class
    enriched["admission_state"] = admission
    enriched["expected_degraded"] = False if is_manual else bool(enriched.get("expected_degraded", True))
    enriched["retryable"] = False if is_manual else bool(enriched.get("retryable", True))
    if enriched.get("probe_mode"):
        enriched.setdefault(
            "recommended_action",
            "请先处理浏览器验证或等待冷却；轻量诊断不再自动进入外部搜索详情兜底。",
        )
    else:
        enriched.setdefault("recommended_action", "检查登录态、浏览器会话和平台验证后再重试；巡检场景不要重复调用小红书")
    enriched.setdefault("diagnostic_evidence", [])
    if not enriched["diagnostic_evidence"]:
        health_error = _xiaohongshu_expected_degraded()
        enriched["diagnostic_evidence"] = health_error.get("diagnostic_evidence", [])
        enriched.setdefault("root_causes", health_error.get("root_causes", []))
    if bool(enriched.get("manual_action_required")):
        enriched.setdefault(
            "manual_interaction_envelope",
            build_manual_interaction_envelope(
                platform="xhs",
                reason_code=str(enriched.get("platform_state") or enriched.get("failure_type") or "manual_action_required"),
                original_tool="search_xiaohongshu",
                original_args={
                    "stage": "search",
                    "platform_state": enriched.get("platform_state", ""),
                    "failure_type": enriched.get("failure_type", ""),
                },
            ),
        )
    next_result = dict(result)
    next_result["error"] = enriched
    metadata = dict(next_result.get("metadata") or {})
    metadata["expected_degraded"] = False if is_manual else True
    metadata["status_class"] = status_class
    metadata["admission_state"] = admission
    if enriched.get("probe_mode"):
        metadata["probe_mode"] = True
    next_result["metadata"] = metadata
    if is_manual:
        return _request_manual_interaction_for_result(
            next_result,
            platform="xhs",
            original_tool="search_xiaohongshu",
            original_args={
                "stage": "search",
                "platform_state": enriched.get("platform_state", ""),
                "failure_type": enriched.get("failure_type", ""),
            },
        )
    return next_result


# ═══════════════════════════════════════════════════════════════════════
# 工具 6: search_zhihu (httpx 直调知乎 API，1-2s 完成)
# ═══════════════════════════════════════════════════════════════════════

_strip_html_text = understanding_strip_html_text
@mcp.tool()
@traced_tool("search_zhihu", strategy="platform_adapter")
def search_zhihu(
    keyword: Annotated[str, Field(description="知乎搜索关键词。返回回答、文章、问题或视频条目的标题、URL、摘要、作者、赞同数和类型。")],
    limit: Annotated[int, Field(description="返回条目数量。", ge=1, le=20)] = 10,
    research_task_id: Annotated[str, Field(description="可选的 kr_research 任务 ID；提供后返回 research_receipt。")]= "",
) -> Dict:
    """知乎内容搜索。

    返回回答、文章、问题或视频条目的标题、URL、摘要、作者、赞同数、类型和平台元数据。
    依赖可用登录态和平台接口；登录失效、权限限制或接口变化可能导致降级或结果为空。
    """
    request = SearchRequest(keyword=keyword, limit=limit, platform="知乎")
    try:
        result = _cached_search("知乎", keyword, limit, lambda: registry.get("知乎").search(request).to_mcp_dict())
    except Exception as exc:
        message = str(exc)
        lower = message.lower()
        failure_type = "login_or_cookie_unavailable" if any(token in lower for token in ("cookie", "login", "登录", "auth")) else "collector_error"
        result = {
            "items": [],
            "total": 0,
            "platform": "知乎",
            "error": {
                "type": failure_type,
                "message": message,
                "expected_degraded": failure_type == "login_or_cookie_unavailable",
                "retryable": failure_type != "login_or_cookie_unavailable",
                "manual_action_required": failure_type == "login_or_cookie_unavailable",
                "recommended_action": "检查知乎登录态/cookie 后重试。" if failure_type == "login_or_cookie_unavailable" else "查看采集器错误并按 fallback 链路排查。",
            },
            "metadata": {
                "strategy": "zhihu_cookie_governed_fallback",
                "fallback_used": True,
                "failure_type": failure_type,
            },
        }
    result = _promote_manual_interaction_result(
        result,
        platform="zhihu",
        original_tool="search_zhihu",
        original_args={"keyword": keyword, "limit": limit},
    )
    _record_search_evidence("知乎", keyword, result)
    return result


def _legacy_bilibili_from_request(request: SearchRequest) -> Dict:
    return legacy_search_bilibili(request.keyword, page_size=request.limit)


def _legacy_zhihu_from_request(request: SearchRequest) -> Dict:
    return zhihu_collectors.legacy_search_zhihu(request.keyword, limit=request.limit)


def _legacy_xiaohongshu_from_request(request: SearchRequest) -> Dict:
    search_type = request.search_type or "all"
    probe_mode = bool((request.options or {}).get("probe_mode")) or search_type in {
        "force",
        "probe",
        "force_probe",
    }
    return xhs_collectors.legacy_search_xiaohongshu(
        request.keyword,
        limit=request.limit,
        search_type=search_type,
        probe_mode=probe_mode,
    )


def _youtube_from_request(request: SearchRequest) -> Dict:
    return youtube_collectors.search_youtube(request.keyword, limit=request.limit)


def _legacy_boss_from_request(request: SearchRequest) -> Dict:
    city = (request.options or {}).get("city", "")
    return boss_collectors.legacy_search_boss(request.keyword, city=city, limit=request.limit)


def _legacy_liepin_from_request(request: SearchRequest) -> Dict:
    city = (request.options or {}).get("city", "")
    return liepin_collectors.legacy_search_liepin(request.keyword, city=city, limit=request.limit)


def _legacy_maimai_from_request(request: SearchRequest) -> Dict:
    return maimai_collectors.legacy_search_maimai(request.keyword, limit=request.limit)


def _maimai_web_search_from_request(request: SearchRequest) -> Dict:
    """Use open-web discovery for Maimai after the web search page was retired."""
    city = (request.options or {}).get("city", "")
    query_parts = [request.keyword.strip(), city.strip(), "脉脉 招聘 OR 职位 site:maimai.cn"]
    web_query = " ".join(part for part in query_parts if part)
    fallback = search_web(WebSearchRequest(query=web_query, limit=request.limit, provider="auto")).to_mcp_dict()
    items = []
    for item in fallback.get("items") or []:
        next_item = dict(item)
        next_item.setdefault("platform", "脉脉")
        next_item.setdefault("source", "web_search_fallback")
        items.append(next_item)
    return format_collection_search_response(
        "脉脉",
        items[: request.limit],
        metadata={
            "primary_status": "retired",
            "strategy": "web_search_fallback",
            "fallback_from": "maimai_web_search_page_retired",
            "provider": fallback.get("provider"),
            "query": web_query,
            "note": "脉脉网页版职位搜索入口已失效，不再通过 Chrome CDP 启动或采集。",
        },
    )


register_default_adapters(
    bilibili_search=_legacy_bilibili_from_request,
    zhihu_search=_legacy_zhihu_from_request,
    xiaohongshu_search=_legacy_xiaohongshu_from_request,
    boss_search=_legacy_boss_from_request,
    liepin_search=_legacy_liepin_from_request,
    maimai_search=_maimai_web_search_from_request,
    youtube_search=_youtube_from_request,
)


# ═══════════════════════════════════════════════════════════════════════
# 招聘平台统一搜索工具
# ═══════════════════════════════════════════════════════════════════════

_RECRUITMENT_ADAPTERS = {
    "boss": _legacy_boss_from_request,
    "liepin": _legacy_liepin_from_request,
    "maimai": _maimai_web_search_from_request,
    "zhilian": None,  # special handling
    "v2ex": None,  # special handling
}


def _legacy_v2ex_from_request(request: SearchRequest) -> Dict:
    city = (request.options or {}).get("city", "")
    return v2ex_collectors.legacy_search_v2ex(request.keyword, limit=request.limit, city=city)


def _legacy_zhilian_from_request(request: SearchRequest) -> Dict:
    city = (request.options or {}).get("city", "")
    return zhilian_collectors.legacy_search_zhilian(request.keyword, city=city, limit=request.limit)


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
    "zhilian": "zhaopin.com/jobs",
}

_RECRUITMENT_NATIVE_FAILURES_WITH_WEB_FALLBACK = {
    "tool_failure_needs_repair",
    "selector_miss",
    "parse_failed",
    "request_failed",
    "cdp_unavailable",
    "cdp_runtime_error",
    "cdp_no_output",
    "cdp_output_parse_error",
    "runtime_evaluate_exception",
    "runtime_evaluate_no_value",
}


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
    admission = (
        check_platform_admission(platform, keyword=keyword, city=city)
        if platform in LEASED_BROWSER_PLATFORMS
        else {
            "schema": "knowledgeradar-recruitment-admission/v1",
            "platform": platform,
            "admission": "open",
            "allowed": True,
            "reason_code": "non_browser_source",
            "reason": "non_browser_source",
            "retry_after_s": 0,
            "manual_action_required": False,
        }
    )
    if not admission.get("allowed"):
        extra = {"admission": admission}
        if admission.get("manual_interaction"):
            extra["manual_interaction"] = admission.get("manual_interaction")
        return _recruitment_blocked_result(
            platform,
            reason=str(admission.get("reason") or admission.get("reason_code") or "recruitment_admission_blocked"),
            reason_code=str(admission.get("reason_code") or "recruitment_admission_blocked"),
            retry_after_s=float(admission.get("retry_after_s") or 0),
            manual_action_required=bool(admission.get("manual_action_required")),
            extra=extra,
        )

    lease = None
    if platform in LEASED_BROWSER_PLATFORMS:
        lease = acquire_platform_lease(platform, keyword=keyword, city=city)
        if not lease.acquired:
            return _recruitment_blocked_result(
                platform,
                reason=f"平台 {platform} 正在执行另一个浏览器型招聘请求，请稍后重试。",
                reason_code="platform_lease_busy",
                retry_after_s=float(lease.retry_after_s or 0),
                manual_action_required=False,
                extra={"lease": lease.to_dict()},
            )
    try:
        result = call()
        if isinstance(result, dict):
            metadata = dict(result.get("metadata") or {})
            metadata.setdefault("admission", admission)
            if lease is not None:
                metadata.setdefault("platform_lease", lease.to_dict())
            result = dict(result)
            result["metadata"] = metadata
        return result
    finally:
        if lease is not None and lease.acquired:
            release_platform_lease(lease.lease_id)


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
    items = []
    seen_urls = set()
    city_norm = str(city or "").strip().removesuffix("市")
    for item in fallback.get("items") or []:
        candidate = dict(item)
        url = str(candidate.get("url") or "")
        if not url or url in seen_urls:
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


@mcp.tool()
@traced_tool("search_recruitment", strategy="platform_adapter")
def search_recruitment(
    platform: Annotated[Literal["boss", "liepin", "maimai", "v2ex", "zhilian"], Field(description="招聘来源。取值为 boss、liepin、maimai、v2ex、zhilian。")],
    keyword: Annotated[str, Field(description="职位关键词。返回职位标题、URL、公司或社区信息、摘要和平台元数据。")],
    city: Annotated[str, Field(description="城市中文名；不限城市时留空。")] = "",
    limit: Annotated[int, Field(description="返回职位数量。", ge=1, le=20)] = 10,
) -> Dict:
    """搜索国内招聘平台职位。

    返回国内招聘平台职位标题、URL、公司或社区信息、摘要、城市过滤结果和平台元数据。
    登录态、平台页面变化、反爬或公开入口下线可能导致降级、回退或结果为空。
    """
    # V2EX 和智联招聘需要特殊处理
    if platform == "v2ex":
        try:
            result = _with_recruitment_runtime_guards(
                platform,
                keyword,
                city,
                lambda: _legacy_v2ex_from_request(SearchRequest(
                    keyword=keyword,
                    limit=limit,
                    platform="V2EX",
                    options={"city": city} if city else {},
                )),
            )
            if result.get("error"):
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
        try:
            result = _with_recruitment_runtime_guards(
                platform,
                keyword,
                city,
                lambda: _legacy_zhilian_from_request(SearchRequest(
                    keyword=keyword, limit=limit, platform="智联招聘",
                    options={"city": city} if city else {},
                )),
            )
            return _promote_manual_interaction_result(
                _recruitment_open_web_fallback_after_route_failure(
                    platform=platform,
                    keyword=keyword,
                    city=city,
                    limit=limit,
                    direct_result=result,
                ),
                platform=platform,
                original_tool="search_recruitment",
                original_args={"platform": platform, "keyword": keyword, "city": city, "limit": limit},
            )
        except Exception as e:
            log.error(f"智联招聘搜索异常: {e}")
            return {"error": str(e), "platform": "智联招聘"}

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
            original_args={"platform": platform, "keyword": keyword, "city": city, "limit": limit},
        )
        return _recruitment_open_web_fallback_after_route_failure(
            platform=platform,
            keyword=keyword,
            city=city,
            limit=limit,
            direct_result=promoted,
        )
    except Exception as e:
        log.error(f"招聘搜索异常 [{platform}]: {e}")
        return {"error": str(e), "platform": platform}


# ═══════════════════════════════════════════════════════════════════════
# 工具 7: get_capabilities
# ═══════════════════════════════════════════════════════════════════════

@mcp.tool()
@traced_tool("get_capabilities", strategy="capability_discovery")
def get_capabilities(
    summary: Annotated[bool, Field(description="为 true 时返回压缩摘要；为 false 时返回完整能力、状态和策略 manifest，响应更大。")] = False,
) -> Dict:
    """返回 KnowledgeRadar 当前 MCP 工具和平台能力声明。

    返回已注册工具、平台能力、运行时状态、provider 状态和策略摘要。
    完整结果较大；summary=true 返回压缩版。
    """
    if summary:
        with governed_call("get_capabilities", "capabilities.agent_summary") as gov:
            cache = get_ttl_cache("capabilities.agent_summary", ttl_s=60, max_items=8)
            cold_static = os.environ.get("KR_SUMMARY_COLD_STATIC", "true").strip().lower() not in {"0", "false", "no", "off"}
            cache_key = stable_key("capabilities.agent_summary", len(ACTUAL_MCP_TOOLS), cold_static)
            cached = cache.get(cache_key, allow_stale=True)
            if cached:
                return cached
            compute_started = time.time()
            result = _capabilities_summary()
            cache_meta = cache.set(cache_key, result, compute_elapsed_s=time.time() - compute_started)
            return attach_runtime_metadata(
                result,
                tool_name="get_capabilities",
                capability_id="capabilities.agent_summary",
                started=gov["started"],
                budget=gov["budget"],
                cache=cache_meta,
            )
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
    tool_surface = build_tool_surface()
    platforms = {}
    for name, item in platform_capabilities_dict().items():
        platforms[name] = {
            "search": item.get("search"),
            "detail": item.get("detail"),
            "comments": item.get("comments"),
            "media_extract": item.get("media_extract"),
            "login_required": item.get("login_required"),
            "strategies": item.get("strategies", []),
            "risk_level": (item.get("manifest") or {}).get("risk_level"),
        }
    cold_static = os.environ.get("KR_SUMMARY_COLD_STATIC", "true").strip().lower() not in {"0", "false", "no", "off"}
    raw_provider_status = {"_quota": {}, "_host_search_cards": {}}
    providers = {}
    academic_status = {}
    if not cold_static:
        raw_provider_status = provider_status()
        for name, item in raw_provider_status.items():
            if name.startswith("_"):
                continue
            providers[name] = {
                "configured": item.get("configured"),
                "available": item.get("available"),
                "status": item.get("status"),
                "enabled": item.get("enabled"),
                "role": item.get("role"),
                "strategy": item.get("strategy"),
                "detail": item.get("detail"),
                "degraded_ok": item.get("degraded_ok"),
                "anonymous_access": item.get("anonymous_access"),
                "degraded_reason": item.get("degraded_reason"),
                "capability_profile": item.get("capability_profile"),
            }
        try:
            academic_status = academic_provider_status()
        except Exception:
            academic_status = {}
    return {
        "schema_version": "knowledgeradar-capabilities-summary/v1",
        "summary": True,
        "agent_facing": True,
        "tool_surface": tool_surface,
        "mcp_observability": mcp_observability_snapshot(
            transport=MCP_TRANSPORT,
            tool_names=ACTUAL_MCP_TOOLS,
        ),
        "platforms": platforms,
        "source_ecologies": source_ecology_manifest(),
        "capability_atlas": capability_atlas_manifest(include_runtime_observations=False),
        "web_search_providers": providers,
        "provider_status_policy": {
            "mode": "deferred_static_summary" if cold_static else "live_summary",
            "reason": "summary=true 默认不做慢探活；需要实时 provider 状态时调用 health_check(mode='diagnostic_summary') 或 get_capabilities(summary=false)。",
        },
        "web_search_quota": raw_provider_status.get("_quota", {}),
        "host_search_cards": raw_provider_status.get("_host_search_cards", {}),
        "capability_cost_profiles": capability_cost_profiles(),
        "request_budget": budget_manifest(),
        "cache_registry": cache_registry_summary(),
        "validation_semantics": validation_semantics_manifest(raw_provider_status, academic_status),
        "deprecated_provider_aliases": tool_surface["deprecated_provider_aliases"],
        "runtime_contract": runtime_contract_summary(),
        "manual_interaction": manual_interaction_manifest(),
        "media_policy": media_policy_manifest(),
        "research_quality_contract": research_quality_contract_manifest(),
        "l2_multimodal_task_contract": l2_multimodal_task_contract(),
        "compact_patrol_contract": compact_patrol_contract(),
        "diagnostic_sections": {
            "runtime_environment": "get_capabilities(summary=false)",
            "project_governance": "health_check(mode='diagnostic_summary')",
            "native_execution": "get_capabilities(summary=false)",
            "governed_capability_plan": "get_capabilities(summary=false)",
            "architecture_standard": "get_capabilities(summary=false)",
            "knowledge_asset_interface": "get_capabilities(summary=false)",
            "openclaw_native_adapter": "get_capabilities(summary=false)",
            "candidate_admission": "health_check(mode='diagnostic_summary')",
        },
    }


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

def _xhs_profile_for_identity(profile_id: str) -> Dict:
    target = str(profile_id or "").strip()
    for row in raw_registry_for_platform("xiaohongshu").get("profiles", []) or []:
        if isinstance(row, dict) and str(row.get("profile_id") or "") == target:
            return dict(row)
    return {}


@mcp.tool()
@traced_tool("manage_xiaohongshu_accounts", strategy="account_identity_lifecycle")
def manage_xiaohongshu_accounts(
    action: Annotated[str, Field(description="操作：summary 查看已认领账号；begin_claim 打开指定 profile 等待扫码；confirm_claim 在已登录后保存用户确认的账号名称；recover 对指定账号做一次最小恢复确认。")],
    profile_id: Annotated[str, Field(description="小红书 profile ID。summary 可留空；其余操作必填。")] = "",
    display_label: Annotated[str, Field(description="用户确认的人类名称，例如“工作主号”。仅 confirm_claim 使用，不保存密码或完整账号 ID。")] = "",
    masked_hint: Annotated[str, Field(description="可选脱敏提示，例如“尾号 1234”。仅 confirm_claim 使用。")] = "",
) -> Dict:
    """Manage explicit Xiaohongshu account claiming and low-cost recovery."""
    normalized = str(action or "summary").strip().lower()
    if normalized == "summary":
        return {
            "schema_version": "knowledgeradar-xhs-account-management/v1",
            "status": "ok",
            "action": "summary",
            "identities": xhs_account_identity_summary(),
            "browser_sessions": browser_sessions_summary(limit=20),
        }

    profile = _xhs_profile_for_identity(profile_id)
    if not profile:
        return {
            "schema_version": "knowledgeradar-xhs-account-management/v1",
            "status": "error",
            "reason_code": "UNKNOWN_XHS_PROFILE",
            "profile_id": str(profile_id or ""),
        }
    profile_id = str(profile.get("profile_id") or "")
    profile_dir = str(profile.get("profile_dir") or "")
    if normalized == "begin_claim":
        interaction = request_browser_interaction("xhs", "account_claim", target_profile_id=profile_id)
        return {
            "schema_version": "knowledgeradar-xhs-account-management/v1",
            "status": interaction.get("status", "unknown"),
            "action": normalized,
            "profile_id": profile_id,
            "account_slot": str(profile.get("account_slot") or ""),
            "manual_action": interaction.get("manual_action") or interaction,
            "next_step": "在该固定窗口完成扫码；页面显示已登录身份后，调用 confirm_claim 并填写你确认过的账号名称。",
        }
    if normalized not in {"confirm_claim", "recover"}:
        return {
            "schema_version": "knowledgeradar-xhs-account-management/v1",
            "status": "error",
            "reason_code": "UNKNOWN_ACTION",
            "supported_actions": ["summary", "begin_claim", "confirm_claim", "recover"],
        }

    auth_probe = probe_browser_auth("xhs", target_profile_id=profile_id, _include_platform_state=True)
    state = auth_probe.pop("_platform_state", {})
    if auth_probe.get("status") != "ok":
        if (
            normalized == "confirm_claim"
            and auth_probe.get("auth_state") == "platform_state_unconfirmed"
            and str(display_label or "").strip()
        ):
            claimed = claim_xhs_account_identity(
                profile_id=profile_id,
                account_slot=str(profile.get("account_slot") or ""),
                profile_dir_hash=browser_profile_hash(_managed_chrome_profile_dir("xhs", target_profile_id=profile_id)),
                display_label=display_label,
                masked_hint=masked_hint,
                nickname="",
                user_id="",
                allow_user_confirmed_without_identity=True,
            )
            return {
                "schema_version": "knowledgeradar-xhs-account-management/v1",
                "status": claimed.get("status", "unknown"),
                "action": normalized,
                "profile_id": profile_id,
                "account_slot": str(profile.get("account_slot") or ""),
                "claim": claimed,
                "detail": "已保存用户确认的账号名称；平台身份指纹待小红书登录探针可确认后补充。",
            }
        if not auth_probe.get("manual_action_required"):
            return {
                "schema_version": "knowledgeradar-xhs-account-management/v1",
                "status": auth_probe.get("status", "unknown"),
                "reason_code": str(auth_probe.get("auth_state") or "XHS_AUTH_UNCONFIRMED").upper(),
                "profile_id": profile_id,
                "account_slot": str(profile.get("account_slot") or ""),
                "detail": auth_probe.get("detail", "小红书登录态未确认。"),
                "retryable": bool(auth_probe.get("retryable")),
            }
        interaction = request_browser_interaction("xhs", "account_claim_login_required", target_profile_id=profile_id)
        return {
            "schema_version": "knowledgeradar-xhs-account-management/v1",
            "status": "needs_interaction",
            "action": normalized,
            "profile_id": profile_id,
            "account_slot": str(profile.get("account_slot") or ""),
            "manual_action": interaction.get("manual_action") or interaction,
            "state": {key: state.get(key) for key in ("code", "has_login_prompt", "has_verify_prompt", "detail", "msg") if key in state},
        }
    probe = {
        "status": "ok",
        "platform": "xhs",
        "platform_state": "authenticated",
        "manual_action_required": False,
    }
    if normalized == "recover":
        completed = complete_browser_interaction("xhs", probe_result=probe, profile_id=profile_id, profile_dir=profile_dir)
        return {
            "schema_version": "knowledgeradar-xhs-account-management/v1",
            "status": "ok",
            "action": normalized,
            "profile_id": profile_id,
            "account_slot": str(profile.get("account_slot") or ""),
            "completed": completed,
        }
    claimed = claim_xhs_account_identity(
        profile_id=profile_id,
        account_slot=str(profile.get("account_slot") or ""),
        profile_dir_hash=browser_profile_hash(_managed_chrome_profile_dir("xhs", target_profile_id=profile_id)),
        display_label=display_label,
        masked_hint=masked_hint,
        nickname=str(state.get("nickname") or ""),
        user_id=str(state.get("user_id") or ""),
    )
    if claimed.get("status") == "ok":
        claimed["completed"] = complete_browser_interaction("xhs", probe_result=probe, profile_id=profile_id, profile_dir=profile_dir)
    return {
        "schema_version": "knowledgeradar-xhs-account-management/v1",
        "status": claimed.get("status", "unknown"),
        "action": normalized,
        "profile_id": profile_id,
        "account_slot": str(profile.get("account_slot") or ""),
        "claim": claimed,
    }


@mcp.tool()
@traced_tool("health_check", strategy="layered_runtime_diagnostics")
def health_check(
    mode: Annotated[str, Field(description="检查模式。full 执行完整探活；summary/light/lite 返回模型决策用轻量摘要；diagnostic_summary 返回诊断摘要；browser_sessions 等诊断模式返回浏览器交互状态。")] = "full",
) -> Dict:
    """检查 KnowledgeRadar 的关键平台和运行依赖状态。

    返回整体状态、平台探活、登录态、浏览器会话、任务队列、provider 和降级摘要。
    full 可能较慢；summary/light/lite 返回轻量摘要。
    """
    normalized_mode = str(mode or "full").strip().lower()
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
        if platform not in set(managed_browser_platforms()):
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
        if platform not in set(managed_browser_platforms()):
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
        if platform not in set(managed_browser_platforms()):
            return {
                "schema_version": "knowledgeradar-health-browser-interaction/v1",
                "summary": False,
                "status": "invalid_request",
                "checks": {"browser_interaction": {"status": "invalid_request", "detail": "unsupported platform", "platform": platform}},
            }
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
    if normalized_mode in {"camoufox_probe", "camoufox_v2_probe", "xhs_camoufox_probe"}:
        return {
            "schema_version": "knowledgeradar-health-camoufox-probe/v1",
            "summary": False,
            "status": "ok",
            "checks": {
                "camoufox_v2": camoufox_v2_health(),
                "camoufox_v2_login_probe": probe_camoufox_v2_login(),
            },
        }
    if normalized_mode in {
        "camoufox_search_page_probe",
        "camoufox_v2_search_page_probe",
        "xhs_camoufox_search_page_probe",
    }:
        probe = probe_camoufox_v2_search_page()
        return {
            "schema_version": "knowledgeradar-health-camoufox-search-page-probe/v1",
            "summary": False,
            "status": "ok" if probe.get("status") == "ok" else "degraded",
            "checks": {
                "camoufox_v2": camoufox_v2_health(),
                "camoufox_v2_search_page_probe": probe,
            },
        }
    if normalized_mode in {
        "camoufox_sdk_search_page_probe",
        "camoufox_sdk_xhs_search_page_probe",
        "xhs_camoufox_sdk_search_page_probe",
    }:
        probe = probe_camoufox_sdk_xhs_search_page()
        return {
            "schema_version": "knowledgeradar-health-camoufox-sdk-search-page-probe/v1",
            "summary": False,
            "status": "ok" if probe.get("status") == "ok" else "degraded",
            "checks": {
                "camoufox_sdk_search_page_probe": probe,
            },
        }
    if normalized_mode in {
        "playwright_chromium_probe",
        "xhs_playwright_chromium_probe",
        "playwright_chromium_xhs_probe",
    }:
        probe = probe_playwright_chromium_xhs_login()
        return {
            "schema_version": "knowledgeradar-health-playwright-chromium-xhs-probe/v1",
            "summary": False,
            "status": "ok" if probe.get("status") == "ok" else "degraded",
            "checks": {
                "playwright_chromium_isolated": playwright_chromium_isolated_health(),
                "playwright_chromium_xhs_login_probe": probe,
            },
        }
    if normalized_mode in {
        "playwright_chromium_launch_probe",
        "xhs_playwright_chromium_launch_probe",
        "playwright_chromium_xhs_launch_probe",
    }:
        probe = probe_playwright_chromium_launch_only()
        return {
            "schema_version": "knowledgeradar-health-playwright-chromium-launch-probe/v1",
            "summary": False,
            "status": "ok" if probe.get("status") == "ok" else "degraded",
            "checks": {
                "playwright_chromium_isolated": playwright_chromium_isolated_health(),
                "playwright_chromium_launch_probe": probe,
            },
        }
    if normalized_mode in {
        "playwright_chromium_page_load_probe",
        "xhs_playwright_chromium_page_load_probe",
        "playwright_chromium_xhs_page_load_probe",
    }:
        probe = probe_playwright_chromium_xhs_page_load()
        return {
            "schema_version": "knowledgeradar-health-playwright-chromium-xhs-page-load-probe/v1",
            "summary": False,
            "status": "ok" if probe.get("status") == "ok" else "degraded",
            "checks": {
                "playwright_chromium_isolated": playwright_chromium_isolated_health(),
                "playwright_chromium_xhs_page_load_probe": probe,
            },
        }
    if normalized_mode in {
        "playwright_chromium_search_page_probe",
        "xhs_playwright_chromium_search_page_probe",
        "playwright_chromium_xhs_search_page_probe",
        "playwright_cdp_search_page_probe",
        "xhs_playwright_cdp_search_page_probe",
        "playwright_cdp_xhs_search_page_probe",
    }:
        if "cdp" in normalized_mode:
            probe = probe_playwright_cdp_xhs_search_page()
            schema = "knowledgeradar-health-playwright-cdp-xhs-search-page-probe/v1"
            check_key = "playwright_cdp_xhs_search_page_probe"
        else:
            probe = probe_playwright_chromium_xhs_search_page()
            schema = "knowledgeradar-health-playwright-chromium-xhs-search-page-probe/v1"
            check_key = "playwright_chromium_xhs_search_page_probe"
        return {
            "schema_version": schema,
            "summary": False,
            "status": "ok" if probe.get("status") == "ok" else "degraded",
            "checks": {
                "playwright_chromium_isolated": playwright_chromium_isolated_health(),
                check_key: probe,
            },
        }
    if normalized_mode in {
        "playwright_chromium_detail_probe",
        "xhs_playwright_chromium_detail_probe",
        "playwright_chromium_xhs_detail_probe",
    }:
        probe = probe_playwright_chromium_xhs_detail()
        return {
            "schema_version": "knowledgeradar-health-playwright-chromium-xhs-detail-probe/v1",
            "summary": False,
            "status": "ok" if probe.get("status") == "ok" else "degraded",
            "checks": {
                "playwright_chromium_isolated": playwright_chromium_isolated_health(),
                "playwright_chromium_xhs_detail_probe": probe,
            },
        }

    global _LAST_HEALTH_LAYERS
    result = HealthCheckService(
        HealthCheckDeps(
            bili_headers=BILI_HEADERS,
            xhs_bridge_path=XHS_BRIDGE_PATH,
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
            read_zhihu_cookies_from_cdp=zhihu_collectors.read_zhihu_cookies_from_cdp,
            read_zhihu_cookies_from_profile=zhihu_collectors.read_zhihu_cookies_from_profile,
            zhihu_sign=zhihu_collectors.zhihu_sign,
            zhihu_search_api=zhihu_collectors.zhihu_search_api,
            inspect_zhihu_cookie_health=zhihu_collectors.inspect_zhihu_cookie_health,
            inspect_xhs_login_health=lambda cdp_url, warn_within_hours=72: xhs_scrapling_adapter.inspect_xhs_login_health(cdp_url, warn_within_hours),
            probe_xhs_page_state=lambda cdp_url: xhs_scrapling_adapter.probe_login_state(cdp_url),
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
        force_zhihu_login_probe=normalized_mode in {
            "full",
            "zhihu_login_probe",
            "zhihu-login-probe",
            "real_zhihu_login_probe",
            "real-zhihu-login-probe",
        },
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
    xhs_budget = _xhs_budget_state()
    degradation = get_degradation_policy().summary(recent_limit=3)
    profile_registry = profile_registry_summary()
    profile_registry_runtime = profile_registry_internal()
    browser_channels = browser_channel_summary(profile_registry.get("profiles", []))
    channel_admission = build_channel_admission_summary(profile_registry.get("profiles", []))
    xhs_account_pool_v3 = xhs_account_pool_summary(profile_registry_runtime)
    xhs_account_patrol = xhs_account_patrol_summary(profile_registry_runtime)
    xhs_account_control = xhs_account_control_summary(profile_registry_runtime)
    xhs_account_switcher = xhs_account_switcher_summary(profile_registry_runtime)
    xhs_policy_matrix = xhs_policy_gate_matrix(xhs_account_switcher)
    xhs_route_matrix = xhs_route_event_summary()
    xhs_candidate_admission = xhs_autonomous_candidate_admission_summary(
        profile_registry=profile_registry_runtime,
        route_matrix=xhs_route_matrix,
    )
    xhs_route_scoring = xhs_route_scoring_summary(xhs_route_matrix, candidate_admission=xhs_candidate_admission)
    xhs_api_candidates = xhs_api_candidate_config_summary()
    xhs_api_fallback_plan = {
        "schema": "knowledgeradar-xhs-api-fallback-health-plan/v1",
        "status": "ok",
        "manual_plan": plan_tikhub_xhs_search_fallback("<keyword>", limit=1),
        "break_glass_plan": plan_tikhub_break_glass_fallback(
            "<keyword>",
            limit=1,
            browser_availability=(xhs_account_pool_v3.get("availability") or {}),
            route_scoring=xhs_route_scoring,
        ),
        "side_effects": {
            "api_call": False,
            "billing": False,
            "browser_launch": False,
            "station_search": False,
            "account_switch": False,
        },
    }
    xhs_stability_observer = xhs_stability_observer_summary(
        profile_registry=profile_registry,
        route_matrix=xhs_route_matrix,
        channel_admission=channel_admission,
        candidate_admission=xhs_candidate_admission,
    )
    decision_log_compact = _decision_logs_compact(limit=120)
    xhs_multimodal_acceptance = xhs_multimodal_acceptance_summary(decision_log_compact)
    xhs_session_governance = xhs_session_governance_summary(profile_registry_runtime)
    chrome_runtime = chrome_runtime_summary()
    xhs_p6_p9_governance = xhs_p6_p9_governance_summary(
        profile_registry=profile_registry_runtime,
        browser_channels=browser_channels,
        channel_admission=channel_admission,
        account_pool=xhs_account_pool_v3,
        candidate_admission=xhs_candidate_admission,
        web_providers=providers,
    )
    governed_capability_plan = governed_capability_plan_summary()
    low_risk_execution = _low_risk_execution_probe_declaration()
    gh_cli_admission = gh_cli_admission_record(gh_cli_sidecar.health())
    architecture_standard = architecture_standard_summary(tool_surface=build_tool_surface())
    knowledge_assets = knowledge_asset_schema_summary()
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
                "capability_id": "account_pool.summary",
                "tool_name": "xhs_account_pool_summary",
                "fn": lambda: xhs_account_pool_v3,
            },
            {
                "capability_id": "account_patrol.summary",
                "tool_name": "xhs_account_patrol_summary",
                "fn": lambda: xhs_account_patrol,
            },
            {
                "capability_id": "account_switcher.summary",
                "tool_name": "xhs_account_switcher_summary",
                "fn": lambda: xhs_account_switcher,
            },
            {
                "capability_id": "xhs_route_matrix.summary",
                "tool_name": "xhs_route_event_summary",
                "fn": lambda: xhs_route_matrix,
            },
            {
                "capability_id": "xhs_candidate_admission.summary",
                "tool_name": "xhs_autonomous_candidate_admission_summary",
                "fn": lambda: xhs_candidate_admission,
            },
            {
                "capability_id": "xhs_stability_observer.summary",
                "tool_name": "xhs_stability_observer_summary",
                "fn": lambda: xhs_stability_observer,
            },
            {
                "capability_id": "xhs_api_candidates.summary",
                "tool_name": "xhs_api_candidate_config_summary",
                "fn": lambda: xhs_api_candidates,
            },
            {
                "capability_id": "xhs_route_scoring.summary",
                "tool_name": "xhs_route_scoring_summary",
                "fn": lambda: xhs_route_scoring,
            },
            {
                "capability_id": "xhs_session_governance.summary",
                "tool_name": "xhs_session_governance_summary",
                "fn": lambda: xhs_session_governance,
            },
            {
                "capability_id": "xhs_p6_p9_governance.summary",
                "tool_name": "xhs_p6_p9_governance_summary",
                "fn": lambda: xhs_p6_p9_governance,
            },
            {
                "capability_id": "architecture.standard",
                "tool_name": "architecture_standard_summary",
                "fn": lambda: architecture_standard,
            },
            {
                "capability_id": "knowledge_assets.summary",
                "tool_name": "knowledge_asset_schema_summary",
                "fn": lambda: knowledge_assets,
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
    architecture_completion = architecture_completion_summary(
        [
            {"id": "capability_registry", "status": "ok"},
            {"id": "route_policy", "status": "ok"},
            {"id": "runtime_contract", "status": (runtime_contract_summary() or {}).get("status", "ok")},
            {"id": "task_durable_runtime", "status": task_summary.get("status", "ok")},
            {"id": "trace_evidence_ledger", "status": "ok"},
            {"id": "external_candidate_admission", "status": gh_cli_admission.get("admission_state", "ok")},
            {"id": "knowledge_asset_interface", "status": knowledge_assets.get("status", "ok")},
            {"id": "openclaw_native_adapter", "status": openclaw_adapter.get("status", "design_ready")},
        ]
    )
    knowledge_asset_pack_sample = build_evidence_pack_summary(
        topic="architecture_governance",
        scope="summary_only",
        evidence_rows=evidence_store.recent(10),
        claim="KnowledgeRadar architecture contracts are represented by runtime summaries.",
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
            "status": xhs_detail.get("status"),
            "success_rate": xhs_detail.get("success_rate"),
            "total": xhs_detail.get("total"),
            "detail": xhs_detail.get("detail"),
        },
        "xiaohongshu_chain_health": {
            "status": xhs_chain.get("status"),
            "detail": xhs_chain.get("detail"),
            "layers": xhs_chain.get("layers", {}),
        },
        "xiaohongshu_budget": xhs_budget,
        "xiaohongshu_diagnostic_control": _xhs_diagnostic_control_state(),
        "profile_registry": profile_registry,
        "xiaohongshu_account_pool": account_pool_selection_summary(
            platform="xiaohongshu",
            registry=profile_registry_runtime,
        ),
        "xiaohongshu_account_pool_v3": xhs_account_pool_v3,
        "xiaohongshu_account_patrol": xhs_account_patrol,
        "xiaohongshu_account_control": xhs_account_control,
        "xiaohongshu_account_switcher": xhs_account_switcher,
        "xiaohongshu_policy_gate_matrix": xhs_policy_matrix,
        "xiaohongshu_route_matrix": xhs_route_matrix,
        "xiaohongshu_candidate_admission": xhs_candidate_admission,
        "xiaohongshu_route_scoring": xhs_route_scoring,
        "xiaohongshu_api_candidates": xhs_api_candidates,
        "xiaohongshu_api_fallback_plan": xhs_api_fallback_plan,
        "xiaohongshu_stability_observer": xhs_stability_observer,
        "xiaohongshu_multimodal_acceptance": xhs_multimodal_acceptance,
        "xiaohongshu_session_governance": xhs_session_governance,
        "chrome_runtime": _compact_chrome_runtime_summary(chrome_runtime),
        "xiaohongshu_p6_p9_governance": xhs_p6_p9_governance,
        "runtime_contract": runtime_contract_summary(),
        "l2_multimodal_task_contract": l2_multimodal_task_contract(),
        "compact_patrol_contract": compact_patrol_contract(),
        "architecture_standard": architecture_standard,
        "architecture_completion": architecture_completion,
        "knowledge_asset_interface": knowledge_assets,
        "knowledge_asset_pack_sample": {
            "schema": knowledge_asset_pack_sample.get("schema"),
            "pack_id": knowledge_asset_pack_sample.get("pack_id"),
            "topic": knowledge_asset_pack_sample.get("topic"),
            "source_count": len(knowledge_asset_pack_sample.get("source_records", []) or []),
            "claim_count": len(knowledge_asset_pack_sample.get("claim_records", []) or []),
            "privacy_level": knowledge_asset_pack_sample.get("privacy_level"),
        },
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
        cache_key = stable_key("health.summary", len(ACTUAL_MCP_TOOLS), cold_static)
        cached = cache.get(cache_key, allow_stale=True)
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
    cold_static = os.environ.get("KR_HEALTH_SUMMARY_COLD_STATIC", "true").strip().lower() not in {"0", "false", "no", "off"}
    providers = {"_quota": {}}
    academic_status = {}
    if not cold_static:
        providers = provider_status()
        try:
            academic_status = academic_provider_status()
        except Exception as exc:
            academic_status = {
                "status_unavailable": {
                    "available": False,
                    "status": "degraded",
                    "degraded_reason": str(exc)[:160],
                }
            }
    task_summary = get_task_store().summary(recent_limit=3)
    web_available = [
        str(name)
        for name, item in providers.items()
        if isinstance(item, dict) and not str(name).startswith("_") and item.get("available")
    ]
    academic_available = [
        str(name)
        for name, item in academic_status.items()
        if isinstance(item, dict) and item.get("available")
    ]
    gh_health = {"status": "deferred", "available": None, "retryable": False, "failure_code": "deferred_summary_probe"}
    if not cold_static:
        gh_health = gh_cli_sidecar.health(stale_ok=True)
    chrome_runtime = _compact_chrome_runtime_summary(chrome_runtime_quick_summary())
    checks = {
        "tool_surface": {
            "status": "ok",
            "tool_count": len(build_tool_surface().get("actual_mcp_tools") or []),
        },
        "mcp_observability": mcp_observability_snapshot(
            transport=MCP_TRANSPORT,
            tool_names=ACTUAL_MCP_TOOLS,
        ),
        "summary_probe_policy": {
            "status": "ok",
            "mode": "deferred_static_summary" if cold_static else "live_summary",
            "diagnostic_mode": "health_check(mode='diagnostic_summary')",
        },
        "web_search": {
            "status": "deferred" if cold_static else ("ok" if web_available else "degraded"),
            "available_providers": web_available,
            "quota": providers.get("_quota", {}),
        },
        "academic_search": {
            "status": "deferred" if cold_static else ("ok" if academic_available else "degraded"),
            "available_providers": academic_available,
        },
        "github_search": {
            "status": gh_health.get("status", "unknown"),
            "available": bool(gh_health.get("available")),
            "retryable": bool(gh_health.get("retryable")),
            "failure_code": gh_health.get("failure_code", ""),
        },
        "task_queue": {
            "status": task_summary.get("status", "ok"),
            "active": task_summary.get("active"),
            "stale_count": task_summary.get("stale_count"),
            "counts": task_summary.get("counts", {}),
        },
        "browser_runtime": {
            "status": chrome_runtime.get("status", "unknown"),
            "platforms": {
                name: {
                    "cdp_status": item.get("cdp_status"),
                    "managed": item.get("managed"),
                    "blocked_reason": item.get("blocked_reason"),
                }
                for name, item in (chrome_runtime.get("platforms") or {}).items()
                if isinstance(item, dict)
            },
        },
    }
    status = _overall_status_from_summary(checks)
    return {
        "schema_version": "knowledgeradar-health-agent-summary/v1",
        "summary": True,
        "status": status,
        "checks": checks,
        "diagnostic_mode": "health_check(mode='diagnostic_summary')",
    }


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

@mcp.tool()
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
) -> Dict:
    """详情页正文抽取。

    返回正文、标题、作者、发布时间、评论、字幕/转写线索、图片/视频处理结果、
    证据字段、错误字段和后台任务引用。登录态、验证码、付费墙、反爬、
    音视频处理和多媒体处理可能导致失败、降级或耗时增加。
    """
    log.info(f"get_content_detail: {redact_url(url)}, deep={enable_deep_analysis}, filter={enable_comment_filtering}, auto_mm={auto_multimodal}")
    platform = _infer_detail_platform(url)
    scope = make_task_scope(
        source_url=url,
        platform=platform,
        work_scope_id=work_scope_id,
        task_scope_id=task_scope_id,
        research_session_id=research_session_id,
    )
    session_id = scope.research_session_id_alias
    request = DetailRequest(
        url=url,
        enable_deep_analysis=enable_deep_analysis,
        enable_comment_filtering=enable_comment_filtering,
        auto_multimodal=auto_multimodal,
        platform=platform,
        research_session_id=session_id,
        work_scope_id=scope.work_scope_id,
        task_scope_id=scope.task_scope_id,
        scope_kind=scope.scope_kind,
        options={"task_scope": scope.compact()},
    )
    failure_cache_key = detail_failure_cache.key(platform=request.platform, url=request.url)
    cached_failure = detail_failure_cache.get(failure_cache_key)
    if cached_failure is not None:
        cached_failure = _attach_research_session_tasks(cached_failure, session_id, scope.compact())
        _record_detail_evidence(str(cached_failure.get("platform") or request.platform), request.url, cached_failure)
        return cached_failure
    if request.platform == "小红书":
        with chrome_active_operation("xhs"):
            result = _handle_detail_request(request).to_legacy_dict()
    else:
        result = _handle_detail_request(request).to_legacy_dict()
    result = _attach_research_session_tasks(result, session_id, scope.compact())
    if isinstance(result, dict) and result.get("error"):
        failure_type = str(result.get("failure_type") or result.get("error_type") or "")
        cacheable_failures = {"empty_detail", "dead_link", "unsupported_url", "anti_bot_verification", "login_required"}
        if failure_type in cacheable_failures or bool(result.get("manual_action_required")):
            typed_key = detail_failure_cache.key(platform=request.platform, url=request.url, failure_type=failure_type)
            detail_failure_cache.set(failure_cache_key, result)
            if typed_key != failure_cache_key:
                detail_failure_cache.set(typed_key, result)
    _record_detail_evidence(str(result.get("platform") or request.platform), request.url, result)
    return result


def _attach_research_session_tasks(result: Dict, research_session_id: str, scope: Dict | None = None) -> Dict:
    data = dict(result)
    scope_data = dict(scope or {})
    data["server_run_id"] = SERVER_RUN_ID
    if scope_data:
        data["work_scope_id"] = scope_data.get("work_scope_id", "")
        data["task_scope_id"] = scope_data.get("task_scope_id", "")
        data["scope_binding"] = scope_data
    data["research_session_id"] = research_session_id
    if not (research_session_id or scope_data):
        return data
    try:
        store = get_task_store()
        tasks = store.tasks_for_scope(
            task_scope_id=str(scope_data.get("task_scope_id") or ""),
            work_scope_id=str(scope_data.get("work_scope_id") or ""),
            blocking_only=False,
            include_terminal=True,
            limit=20,
        )
        if not tasks and research_session_id:
            tasks = store.tasks_for_session(research_session_id, blocking_only=False, include_terminal=True, limit=20)
        pending = [task for task in tasks if str(task.get("status") or "") in {"queued", "running"}]
        blocking = [
            task
            for task in tasks
            if bool((task.get("metadata") or {}).get("blocks_final_report"))
            and str(task.get("status") or "") in {"queued", "running"}
        ]
        data["pending_tasks"] = compact_task_refs(pending, limit=20)
        data["blocking_tasks"] = compact_task_refs(blocking, limit=20)
    except Exception as exc:
        data["pending_tasks_error"] = str(exc)[:160]
    return data

def _handle_detail_request(request: DetailRequest) -> DetailResponse:
    started = time.time()
    platform = request.platform or _infer_detail_platform(request.url) or "unknown"
    strategy_builders = {
        "B站": _bilibili_detail_strategy,
        "知乎": _zhihu_detail_strategy,
        "小红书": _xiaohongshu_detail_strategy,
        "YouTube": _youtube_detail_strategy,
        "猎聘": _liepin_detail_strategy,
        "BOSS直聘": _boss_detail_strategy,
    }
    strategy_builder = strategy_builders.get(platform)
    strategy = strategy_builder() if strategy_builder else None
    if strategy is None:
        alternative_tools = [
            {"tool": "kr_web_search", "when": "Use for open-web discovery before detail extraction."},
            {"tool": "extract_web_page", "when": "Use for ordinary static article pages."},
            {"tool": "extract_dynamic_page", "when": "Use when a public page requires browser rendering."},
        ]
        result = {
            "error": f"不支持的 URL: {request.url}",
            "failure_type": "unsupported_url",
            "supported": ["B站视频 (BV...)", "YouTube 视频 (youtube.com/watch 或 youtu.be)", "知乎 (zhihu.com)", "小红书 (xiaohongshu.com)", "猎聘职位 (liepin.com/job 或 /a/)", "BOSS直聘职位 (zhipin.com/job_detail/)"],
            "alternative_tools": alternative_tools,
            "recommended_next_action": "Use kr_web_search for discovery or extract_web_page/extract_dynamic_page for generic public pages.",
            "normalized_status": {
                "status": "unsupported_url",
                "retryable": False,
                "expected_degraded": True,
            },
            "platform": platform,
            "url": request.url,
        }
        result = attach_detail_evidence(request.url, result, infer_platform=_infer_detail_platform)
        response = DetailResponse.from_legacy(
            platform,
            request.url,
            result,
            evidence=build_detail_evidence(request.url, platform, result),
            metadata=_attach_detail_degradation(platform, request.url, result, {"strategy": "unsupported"}),
        )
        _record_detail_decision(request, response, time.time() - started)
        return response
    else:
        trace_hint = {
            "tool_name": "get_content_detail",
            "strategy": type(strategy).__name__,
            "status": "running",
            "elapsed_s": 0.0,
            "retry_count": 0,
            "failure_code": "",
            "failure_tags": [],
        }
        set_current_tool_trace(trace_hint)
        response = strategy.extract(request)
        data = attach_detail_evidence(request.url, response.data, infer_platform=_infer_detail_platform)
        data.update(build_agent_native_fields(data, strategy=str((response.metadata or {}).get("strategy") or "")))
        normalized = DetailResponse.from_legacy(
            response.platform,
            request.url,
            data,
            evidence=response.evidence or build_detail_evidence(request.url, response.platform, data),
            metadata=_attach_detail_degradation(response.platform, request.url, data, dict(response.metadata)),
        )
        _record_detail_decision(request, normalized, time.time() - started)
        return normalized


def _bilibili_detail_strategy() -> BilibiliDetailStrategy:
    return BilibiliDetailStrategy(
        BilibiliDetailDeps(
            extract_bvid=extract_bvid,
            get_info=get_bilibili_info,
            transcribe=lambda bvid, session_id="", options=None: transcribe_bilibili(
                bvid,
                research_session_id=session_id,
                scope_metadata=(options or {}).get("task_scope") if isinstance(options, dict) else None,
            ),
            get_comments=get_bilibili_comments,
            filter_comments=filter_bilibili_comments,
            attach_routing=attach_routing_metadata,
            routing_recommends_l2=routing_recommends_l2,
            deep_analyze=deep_analyze_bilibili,
            direct_media_probe=_probe_bilibili_direct_media,
            evidence_builder=build_detail_evidence,
            data_dir=REPO_ROOT,
        )
    )


def _zhihu_detail_strategy() -> ZhihuDetailStrategy:
    return ZhihuDetailStrategy(
        ZhihuDetailDeps(
            headers=ZHIHU_HEADERS,
            profile_dir=_managed_chrome_profile_dir,
            ensure_chrome_debugging=_ensure_chrome_debugging,
            finish_chrome_automation=finish_chrome_automation,
            read_cookies_from_cdp=zhihu_collectors.read_zhihu_cookies_from_cdp,
            read_cookies_from_profile=zhihu_collectors.read_zhihu_cookies_from_profile,
            strip_html=understanding_strip_html_text,
            looks_not_found=understanding_looks_like_zhihu_not_found,
            article_from_html=lambda html, url: extract_zhihu_article_from_html(html, url=url),
            article_via_cdp=extract_zhihu_article_via_cdp,
            attach_routing=attach_routing_metadata,
            evidence_builder=build_detail_evidence,
            log_info=log.info,
            log_debug=log.debug,
            log_error=log.error,
        )
    )


def _xiaohongshu_detail_strategy() -> XiaohongshuDetailStrategy:
    return XiaohongshuDetailStrategy(
        XiaohongshuDetailDeps(
            bridge_path=XHS_BRIDGE_PATH,
            node_exe=find_node_exe(),
            recover_xsec_token=recover_xhs_xsec_token,
            detail_needs_fallback=xhs_detail_needs_fallback,
            extract_via_cdp=extract_xhs_detail_via_cdp,
            ocr_first_image=ocr_first_xhs_image,
            attach_routing=attach_routing_metadata,
            evidence_builder=build_detail_evidence,
            log_info=log.info,
            log_warning=log.warning,
            log_error=log.error,
            auto_switch_account=xhs_collectors._auto_switch_xhs_account,
            request_user_login=request_user_login,
            selected_profile_id=xhs_collectors._selected_xhs_profile_id,
        )
    )


def _youtube_detail_strategy() -> YouTubeDetailStrategy:
    return YouTubeDetailStrategy(
        YouTubeDetailDeps(
            extract_video_id=youtube_collectors.extract_youtube_video_id,
            get_detail=youtube_collectors.get_youtube_detail,
            attach_routing=attach_routing_metadata,
            routing_recommends_l2=routing_recommends_l2,
            deep_analyze=deep_analyze_youtube,
            direct_media_probe=_probe_youtube_direct_media,
            evidence_builder=build_detail_evidence,
        )
    )


def _probe_bilibili_direct_media(bvid: str, enabled: bool = True) -> Dict:
    if not enabled:
        return {"schema": "knowledgeradar-direct-media/v1", "status": "skipped", "reason": "disabled"}
    return build_direct_media_probe(
        bilibili_direct_candidate_with_ytdlp(bvid),
        headers=BILIBILI_HEADERS,
        probe_reachability=True,
    )


def _probe_youtube_direct_media(video_id: str, enabled: bool = True) -> Dict:
    if not enabled:
        return {"schema": "knowledgeradar-direct-media/v1", "status": "skipped", "reason": "disabled"}
    return build_direct_media_probe(
        youtube_watch_url_candidate(video_id),
        probe_reachability=False,
    )


def _liepin_detail_strategy() -> RecruitmentDetailStrategy:
    return RecruitmentDetailStrategy(
        platform="猎聘",
        extractor=liepin_collectors.liepin_detail_via_cdp,
        evidence_builder=build_detail_evidence,
    )


def _boss_detail_strategy() -> RecruitmentDetailStrategy:
    return RecruitmentDetailStrategy(
        platform="BOSS直聘",
        extractor=boss_collectors.boss_detail_via_cdp,
        evidence_builder=build_detail_evidence,
    )

# ═══════════════════════════════════════════════════════════════════════
# 启动
# ═══════════════════════════════════════════════════════════════════════

def _bootstrap_persistent_runtime() -> None:
    """Warm up long-lived browser dependencies for HTTP/SSE server mode."""
    reconciled = reconcile_stale_xhs_manual_interactions()
    if reconciled["rejected"]:
        log.warning("已撤销 %s 个账号绑定不一致的小红书人工交互窗口", reconciled["rejected"])
    restored = restore_chrome_idle_cleanups()
    if restored["restored"]:
        log.info("已恢复 %s 个受管 Chrome 空闲回收计时器，其中过期 %s 个", restored["restored"], restored["overdue"])
    watcher_restore = restore_pending_xhs_auth_watchers()
    if watcher_restore.get("restored_profile_ids"):
        log.info("已恢复 %s 个小红书扫码自动收口观察器", len(watcher_restore["restored_profile_ids"]))
    if os.environ.get("KR_BROWSER_SESSION_MAINTENANCE_ON_STARTUP", "1").strip().lower() in {"1", "true", "yes", "on"}:
        try:
            compacted = compact_terminal_browser_sessions(retain_closed=100, retain_failed=20)
            log.info("已完成浏览器会话终态维护：压缩 %s 条，保留活动会话 %s 条", compacted.get("removed_count", 0), compacted.get("protected_active_count", 0))
        except Exception as exc:
            log.warning("浏览器会话终态维护失败（不阻断启动）: %s", exc)
    if os.environ.get("KR_CHROME_PREWARM", "").strip().lower() not in {"1", "true", "yes"}:
        log.info("  Chrome 启动预热已关闭；小红书/知乎工具调用时按需启动")
        return

    def _warmup() -> None:
        for platform in ("xhs", "zhihu"):
            try:
                ok = _ensure_chrome_debugging(platform)
                log.info(f"启动预热 Chrome: platform={platform}, ok={ok}, port={_chrome_debug_port(platform)}")
            except Exception as e:
                log.warning(f"启动预热 Chrome 失败: platform={platform}, error={e}")

    threading.Thread(target=_warmup, name="kr-runtime-warmup", daemon=True).start()


def main() -> None:
    fallback_invocation_id = os.environ.get("KR_CONTINUITY_INVOCATION_ID", "")
    is_continuity_fallback = os.environ.get("KR_CONTINUITY_FALLBACK") == "1"
    _install_mcp_observability()
    record_server_started(
        transport=MCP_TRANSPORT,
        tool_names=ACTUAL_MCP_TOOLS,
        source_fingerprint=source_fingerprint(REPO_ROOT),
        invocation_kind="continuity_fallback" if is_continuity_fallback else "native_server",
        invocation_id=fallback_invocation_id,
    )
    log.info("=" * 50)
    log.info(f"  全网知识搜索 MCP Server 启动 ({MCP_TRANSPORT} 模式)")
    if MCP_TRANSPORT in {"sse", "streamable-http"}:
        log.info(f"  HTTP: http://{MCP_HOST}:{MCP_PORT}{MCP_STREAMABLE_HTTP_PATH}")
        log.info(f"  SSE:  http://{MCP_HOST}:{MCP_PORT}{MCP_SSE_PATH}")
    log.info(f"  工具({len(ACTUAL_MCP_TOOLS)}): {', '.join(ACTUAL_MCP_TOOLS)}")
    log.info("  ✨ 小红书/知乎支持 Chrome 调试模式按需自动启动（保留持久登录态）")
    log.info("=" * 50)
    if MCP_TRANSPORT in {"sse", "streamable-http"}:
        _bootstrap_persistent_runtime()
    try:
        mcp.run(transport=MCP_TRANSPORT)
    finally:
        if is_continuity_fallback:
            record_fallback_server_stopped(invocation_id=fallback_invocation_id)


if __name__ == "__main__":
    main()
