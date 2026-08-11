"""Xiaohongshu collection helpers migrated out of server.py."""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import random
import re
import time
from typing import Dict, List

import httpx

from kr_core.collection import CollectionTrace, format_search_error, format_search_response
from kr_core.decision_log import DecisionLogger
from collectors.url_manager import expand_xhs_short_url, normalize_xhs_note_url, preflight_xhs_url
from runtime.chrome_manager import (
    XHS_CHROME_DEBUG_PORT,
    chrome_active_operation,
    complete_browser_interaction,
    request_user_login,
    _browser_resource_key,
    _chrome_debug_url,
    _chrome_user_data_dir_for_pid,
    _ensure_chrome_debugging,
    _find_chrome_with_debug_port,
)
from runtime.browser_sessions import browser_sessions_summary, stable_hash as browser_profile_hash
from runtime.degradation import get_degradation_policy
from runtime.platform_diagnostics import build_platform_health_probe, probe_from_error
from runtime.xhs_candidates import normalize_xhs_detail_snapshot, xhs_detail_content_snapshot_js
from runtime.xhs_account_events import record_xhs_account_event
from runtime.profile_registry import profile_registry_internal, raw_registry_for_platform, select_main_chain_profile
from runtime.xhs_account_pool import xhs_account_pool_summary
from runtime.xhs_account_switcher import execute_xhs_account_switch
from runtime.xhs_route_events import record_xhs_route_event, xhs_route_event_summary
from runtime.xhs_route_scoring import xhs_route_scoring_summary
from runtime.xhs_tikhub_fallback import execute_tikhub_break_glass_fallback

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
RUNTIME_LOG_DIR = os.environ.get("KR_LOG_DIR") or os.path.join(
    os.environ.get("OPENCLAW_STATE_DIR") or os.environ.get("OPENCLAW_HOME") or os.path.join(os.path.expanduser("~"), ".openclaw"),
    "logs",
    "runtime",
)
DECISION_LOG_PATH = os.path.join(RUNTIME_LOG_DIR, "knowledgeradar-decisions.jsonl")
_decision_logger = DecisionLogger(DECISION_LOG_PATH)
log = logging.getLogger("mcp-server")
XHS_NEGATIVE_CACHE_PATH = os.path.join(RUNTIME_LOG_DIR, "knowledgeradar-xhs-negative-cache.jsonl")
XHS_SEARCH_GATE_PATH = os.path.join(RUNTIME_LOG_DIR, "knowledgeradar-xhs-search-gate.jsonl")
XHS_NOTE_URL_RE = re.compile(
    r"https?://(?:www\.)?xiaohongshu\.com/(?:explore|discovery/item)/[A-Za-z0-9_-]+(?:\?[^\s\"'<>]*)?",
    re.IGNORECASE,
)
XHS_SEARCH_BRIDGE_BREAKER_KEY = "collector:xhs.search_bridge_fallback"


def _resolve_xhs_cdp_url(chrome_debug_url) -> str:
    """Resolve legacy helper or current concrete XHS CDP endpoint safely."""
    resource_key = _browser_resource_key("xhs")
    return str(chrome_debug_url(resource_key) if callable(chrome_debug_url) else (chrome_debug_url or ""))


def xiaohongshu_account_state(chrome_debug_url) -> Dict:
    """Return the current XHS account/login state via the Scrapling adapter.

    Callers now pass the concrete CDP endpoint for the selected account. Keep
    the callable form for older internal callers, but never try to call an
    endpoint string as if it were the legacy ``_chrome_debug_url`` helper.
    """
    try:
        scrapling_path = os.path.join(PROJECT_ROOT, "media_platform", "xhs", "scrapling_adapter.py")
        spec = importlib.util.spec_from_file_location("xhs_scrapling_adapter_account_state", scrapling_path)
        scrapling_adapter = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(scrapling_adapter)
        resolved_cdp_url = _resolve_xhs_cdp_url(chrome_debug_url)
        if not resolved_cdp_url:
            return {"status": "unknown", "detail": "小红书账号状态探针缺少 CDP 地址"}
        state = scrapling_adapter.probe_login_state(resolved_cdp_url)
        if not isinstance(state, dict):
            return {"status": "unknown", "detail": "小红书账号状态探针未返回结构化结果"}
        return state
    except Exception as exc:
        return {"status": "unknown", "detail": f"小红书账号状态探针失败: {exc}"}


def _xhs_login_state_ok(state: Dict) -> bool:
    if not isinstance(state, dict):
        return False
    if state.get("guest") is True:
        return False
    # API 确认已登录时，优先信任 API，忽略页面文字误判
    if state.get("ok") is True:
        return True
    if state.get("confirmed") is True:
        return True
    if state.get("code") == 0 and state.get("user_id"):
        return True
    # The authenticated browser page is an operational proof only.  It is used
    # when XHS rejects the optional identity endpoint (currently observed as
    # HTTP 406), so a valid session is not mislabeled as logged out.  It never
    # writes a raw user id or upgrades the user-confirmed account label to a
    # platform-identity fingerprint.
    if state.get("ui_authenticated") is True:
        return True
    # 仅在 API 未确认时，才检查页面文字
    if state.get("has_login_prompt"):
        return False
    return False


def _xhs_manual_auth_evidence(state: Dict) -> tuple[str, List[str]]:
    """Return an explicit user-action reason only for platform-proven states."""
    if not isinstance(state, dict):
        return "", []
    if state.get("has_verify_prompt"):
        return "security_verification", ["xhs_page_has_verify_prompt=true"]
    if state.get("has_login_prompt"):
        return "login_required", ["xhs_page_has_login_prompt=true"]
    if state.get("guest") is True:
        return "login_required", ["xhs_api_guest=true"]
    if state.get("code") in {-100, -101}:
        return "login_required", [f"xhs_api_code={state.get('code')}"]
    return "", []


def _record_xhs_login_preflight(reason_code: str, state: Dict) -> None:
    try:
        selected = select_main_chain_profile("xiaohongshu")
        profile_id = str(selected.get("profile_id") or "")
        if profile_id:
            record_xhs_account_event(
                profile_id,
                reason_code,
                last_tool="search_xiaohongshu_login_preflight",
                notes=[
                    f"code={state.get('code')}",
                    str(state.get("msg") or state.get("detail") or "")[:80],
                ],
            )
    except Exception as exc:
        log.debug(f"小红书登录态前置门状态记录失败: {exc}")


def _selected_xhs_profile_id() -> str:
    try:
        pid = _find_chrome_with_debug_port("xhs")
        current_dir = os.path.normcase(os.path.abspath(_chrome_user_data_dir_for_pid(pid))).lower() if pid else ""
        if current_dir:
            for profile in raw_registry_for_platform("xiaohongshu").get("profiles", []) or []:
                if not isinstance(profile, dict):
                    continue
                configured = str(profile.get("profile_dir") or "")
                if not configured:
                    continue
                configured_dir = os.path.normcase(
                    os.path.abspath(os.path.expandvars(os.path.expanduser(configured)))
                ).lower()
                if configured_dir == current_dir:
                    return str(profile.get("profile_id") or "")
    except Exception as exc:
        log.debug(f"小红书当前 Chrome profile 反查失败: {exc}")
    try:
        selected = select_main_chain_profile("xiaohongshu")
        return str(selected.get("profile_id") or "")
    except Exception:
        return ""


def _auto_switch_xhs_account(
    *,
    purpose: str,
    reason_code: str,
    trace: CollectionTrace | None = None,
    last_tool: str = "",
    notes: List[str] | None = None,
    current_profile_id: str = "",
    switches_used: int = 0,
    allow_manual_recovery_followup: bool = False,
) -> Dict:
    """Record the failure and execute readonly account switching if admitted."""
    current_profile_id = current_profile_id or _selected_xhs_profile_id()
    record_result: Dict = {}
    if current_profile_id:
        try:
            record_result = record_xhs_account_event(
                current_profile_id,
                reason_code,
                last_tool=last_tool or f"xhs_{purpose}",
                notes=notes or [],
            )
        except Exception as exc:
            record_result = {"status": "error", "error": str(exc)}
    try:
        switch_result = execute_xhs_account_switch(
            purpose,
            reason_code=reason_code,
            current_profile_id=current_profile_id,
            switches_used=switches_used,
            allow_manual_recovery_followup=allow_manual_recovery_followup,
        )
    except Exception as exc:
        switch_result = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
    payload = {
        "current_profile_id": current_profile_id,
        "reason_code": reason_code,
        "record": record_result,
        "switch": switch_result,
    }
    if trace:
        trace.add(
            "xhs_account_auto_switch",
            "ok" if switch_result.get("status") == "ok" else "skipped",
            detail=str(switch_result.get("denial_reason") or switch_result.get("reason_code") or reason_code),
            metadata=payload,
        )
    return payload


def _xhs_login_preflight_result(
    trace: CollectionTrace,
    state: Dict,
    *,
    account_switch: Dict | None = None,
    profile_id_override: str = "",
) -> Dict:
    code = state.get("code") if isinstance(state, dict) else None
    msg = str(state.get("msg") or state.get("detail") or "小红书登录态未确认") if isinstance(state, dict) else "小红书登录态未确认"
    reason, trigger_evidence = _xhs_manual_auth_evidence(state)
    profile_id = profile_id_override or _selected_xhs_profile_id()
    manual_interaction = (
        request_user_login(
            "xhs",
            reason,
            target_profile_id=profile_id,
            trigger_evidence=trigger_evidence,
            source="search_xiaohongshu.login_preflight",
        )
        if reason and profile_id
        else {
            "status": "deferred",
            "validation_status": "EXPECTED_DEGRADED",
            "reason_code": "AUTH_STATE_UNCONFIRMED",
            "detail": "小红书登录态暂未确认，已保持后台，不会弹出浏览器。",
        }
    )
    trace.add(
        "login_preflight",
        "failed",
        detail=f"login_state_not_confirmed: {msg}",
        error_type="login_required",
        retryable=False,
        metadata={
            "code": code,
            "has_login_prompt": bool(isinstance(state, dict) and state.get("has_login_prompt")),
            "has_verify_prompt": bool(isinstance(state, dict) and state.get("has_verify_prompt")),
            "profile_id": profile_id,
            "trigger_evidence": trigger_evidence,
        },
    )
    result = _format_search_error(
        "小红书",
        {
            "error": "小红书登录态未确认，已在搜索前停止",
            "type": "login_required_before_search",
            "failure_type": "login_required_before_search",
            "message": "小红书登录态未确认，未触发站内搜索。",
            "retryable": False,
            "platform_state": "login_required" if reason else "auth_state_unconfirmed",
            "manual_action_required": bool(reason and profile_id),
            "login_required": bool(reason),
            "diagnostic_evidence": [
                f"account_state_code={code}",
                f"account_state_msg={msg}",
            ],
            "recommended_action": "登录页或验证页证据齐全时才会弹出对应账号；暂未确认时保持后台并稍后重试。",
            "manual_interaction": manual_interaction,
        },
        trace=trace,
        strategy="login_preflight",
    )
    metadata = dict(result.get("metadata") or {})
    metadata["platform_health_probe"] = _xhs_probe_payload(
        status="manual_action",
        reason_code=reason.upper() if reason else "AUTH_STATE_UNCONFIRMED",
        mode="read_only",
        evidence={
            "account_state_code": code,
            "account_state_msg": msg,
            "has_verify_prompt": bool(isinstance(state, dict) and state.get("has_verify_prompt")),
            "account_auto_switch": account_switch or {},
        },
        manual=bool(reason and profile_id),
    )
    metadata["account_auto_switch"] = account_switch or {}
    metadata["manual_interaction"] = manual_interaction
    metadata["search_not_triggered"] = True
    result["metadata"] = metadata
    if isinstance(result.get("error"), dict):
        result["error"]["retryable"] = False
        result["error"]["type"] = "login_required_before_search"
    return result


def _try_tikhub_break_glass_search(keyword: str, limit: int, *, trace: CollectionTrace, trigger_reason: str) -> Dict | None:
    """Try paid TikHub search only when break-glass guards allow it."""
    try:
        registry = profile_registry_internal()
        account_pool = xhs_account_pool_summary(registry)
        route_matrix = xhs_route_event_summary(recent_limit=80)
        route_scoring = xhs_route_scoring_summary(route_matrix)
        result = execute_tikhub_break_glass_fallback(
            keyword,
            limit=limit,
            browser_availability=account_pool.get("availability") or {},
            route_scoring=route_scoring,
        )
        status = str(result.get("status") or "")
        reason_code = str(result.get("reason_code") or status or "unknown")
        api_call_count = int(result.get("api_call_count") or 0)
        record_xhs_route_event(
            actor="knowledgeradar",
            account_slot="",
            profile_id="xhs-api-tikhub",
            browser_base="api",
            channel_id="tikhub_api",
            capability="api_search_break_glass",
            action_type="api_break_glass_auto",
            result="ok" if status == "ok" else ("skipped" if status in {"blocked", "dry_run"} else "degraded"),
            reason_code=reason_code,
            manual_action_required=status == "blocked",
            evidence_ref=f"tikhub_break_glass:{trigger_reason}",
            metadata={
                "trigger_reason": trigger_reason,
                "api_call_count": api_call_count,
                "dry_run": status == "dry_run",
                "browser_launch": False,
                "station_search": False,
                "account_switch": False,
            },
        )
        trace.add(
            "tikhub_break_glass",
            "ok" if status == "ok" else "skipped",
            detail=reason_code,
            metadata={
                "status": status,
                "reason_code": reason_code,
                "api_call_count": api_call_count,
                "trigger_reason": trigger_reason,
            },
        )
        if status != "ok":
            return None
        payload = result.get("result") if isinstance(result.get("result"), dict) else {}
        items = payload.get("items") or []
        metadata = dict(payload.get("metadata") or {})
        metadata["tikhub_break_glass"] = {
            "status": status,
            "reason_code": reason_code,
            "trigger_reason": trigger_reason,
            "api_call_count": api_call_count,
            "main_chain_admission": False,
        }
        return _format_search_response("小红书", items[:limit], trace=trace, metadata=metadata)
    except Exception as exc:
        trace.add("tikhub_break_glass", "failed", detail=str(exc)[:160], error_type="api_fallback_failed", retryable=False)
        return None


def _format_search_response(
    platform: str,
    items: List[Dict],
    *,
    trace: CollectionTrace | None = None,
    metadata: Dict | None = None,
) -> Dict:
    return format_search_response(platform, items, trace=trace, metadata=metadata)


def _format_search_error(
    platform: str,
    error_item: Dict,
    *,
    trace: CollectionTrace | None = None,
    strategy: str = "",
) -> Dict:
    return format_search_error(platform, error_item, trace=trace, strategy=strategy)


def _bridge_production_enabled() -> bool:
    return os.environ.get("KR_XHS_BRIDGE_PRODUCTION_ENABLED", "0").strip().lower() in {"1", "true", "yes", "on"}


def _bridge_diagnostic_metadata(*, reason: str, breaker: Dict | None = None) -> Dict:
    return {
        "bridge_fallback": {
            "status": "diagnostic_only",
            "production_enabled": _bridge_production_enabled(),
            "breaker_key": XHS_SEARCH_BRIDGE_BREAKER_KEY,
            "breaker": breaker or get_degradation_policy().is_open(XHS_SEARCH_BRIDGE_BREAKER_KEY),
            "reason": reason,
            "activation_condition": "set KR_XHS_BRIDGE_PRODUCTION_ENABLED=1 and pass bridge acceptance gate",
        }
    }


def _xhs_probe_payload(*, status: str, reason_code: str, mode: str, evidence: Dict | None = None, manual: bool = False) -> Dict:
    return build_platform_health_probe(
        platform="xiaohongshu",
        tool="search_xiaohongshu",
        mode=mode,
        status=status,
        reason_code=reason_code,
        profile_id=f"xhs_chrome_{XHS_CHROME_DEBUG_PORT}",
        browser_base=f"chrome_{XHS_CHROME_DEBUG_PORT}",
        confidence=0.8,
        risk_scope="platform" if status in {"cooldown", "manual_action"} else "unknown",
        risk_level="high" if manual else ("medium" if status == "fail" else "none"),
        safe_to_retry=status == "fail",
        safe_to_switch_account=False,
        manual_action_required=manual,
        evidence=evidence or {},
    )


def _detail_health_status() -> Dict:
    try:
        from runtime.xhs_health import get_xhs_detail_health_tracker

        return get_xhs_detail_health_tracker().summary(recent_limit=12)
    except Exception as exc:
        return {"status": "degraded", "detail": str(exc), "total": 0, "success_rate": None, "avg_latency_s": None}


def _read_xhs_negative_cache() -> List[Dict]:
    if not os.path.isfile(XHS_NEGATIVE_CACHE_PATH):
        return []
    rows: List[Dict] = []
    try:
        with open(XHS_NEGATIVE_CACHE_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict):
                    rows.append(row)
    except Exception:
        return []
    return rows


def _record_xhs_negative_cache(url: str, reason: str, *, ttl_s: int = 86400) -> None:
    try:
        os.makedirs(os.path.dirname(XHS_NEGATIVE_CACHE_PATH), exist_ok=True)
        entry = {
            "url": url,
            "reason": reason,
            "ttl_s": int(ttl_s),
            "created_at": time.time(),
        }
        with open(XHS_NEGATIVE_CACHE_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n")
    except Exception as exc:
        log.debug(f"小红书负缓存记录失败: {exc}")


def _is_xhs_negative_cached(url: str) -> bool:
    now = time.time()
    for row in reversed(_read_xhs_negative_cache()[-200:]):
        cached_url = str(row.get("url") or "")
        if cached_url != url:
            continue
        ttl_s = int(row.get("ttl_s") or 0)
        created_at = float(row.get("created_at") or 0.0)
        if ttl_s <= 0:
            return True
        if now - created_at <= ttl_s:
            return True
    return False


def _read_xhs_search_gate() -> Dict:
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
    except Exception:
        return {}
    return {}


def _xhs_search_gate_active() -> Dict:
    row = _read_xhs_search_gate()
    now = time.time()
    cooldown_until = float(row.get("cooldown_until") or 0.0)
    active = bool(cooldown_until and now < cooldown_until)
    return {
        "active": active,
        "cooldown_until": cooldown_until,
        "cooldown_remaining_s": round(max(0.0, cooldown_until - now), 2) if active else 0,
        "last_outcome": str(row.get("outcome") or ""),
        "last_reason": str(row.get("reason") or ""),
        "last_search_type": str(row.get("search_type") or ""),
        "last_probe_mode": bool(row.get("probe_mode")),
    }


def _record_xhs_search_gate(*, outcome: str, reason: str, search_type: str, probe_mode: bool, cooldown_seconds: int = 1800, metadata: Dict | None = None) -> None:
    try:
        now = time.time()
        entry = {
            "platform": "小红书",
            "outcome": outcome,
            "reason": reason,
            "search_type": search_type,
            "probe_mode": bool(probe_mode),
            "cooldown_seconds": int(cooldown_seconds),
            "cooldown_until": now + int(cooldown_seconds) if outcome in {"blocked", "failed", "degraded"} and not probe_mode else 0,
            "updated_at": now,
            "metadata": metadata or {},
        }
        os.makedirs(os.path.dirname(XHS_SEARCH_GATE_PATH), exist_ok=True)
        with open(XHS_SEARCH_GATE_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n")
    except Exception as exc:
        log.debug(f"小红书搜索门控记录失败: {exc}")


def _xhs_safe_int_env(name: str, default: int, minimum: int = 0) -> int:
    try:
        return max(minimum, int(os.environ.get(name, str(default)).strip()))
    except Exception:
        return default


def _xhs_low_frequency_guard(keyword: str, *, search_type: str, probe_mode: bool) -> Dict:
    if probe_mode:
        return {"guard_enabled": False, "guard_reason": "probe_mode"}
    now = time.time()
    gate = _read_xhs_search_gate()
    last_at = float(gate.get("updated_at") or 0.0)
    min_interval_s = _xhs_safe_int_env("KR_XHS_MIN_INTERVAL_S", 3, 0)
    jitter_min_ms = _xhs_safe_int_env("KR_XHS_RANDOM_DELAY_MIN_MS", 600, 0)
    jitter_max_ms = _xhs_safe_int_env("KR_XHS_RANDOM_DELAY_MAX_MS", 1800, jitter_min_ms)
    dedupe_ttl_s = _xhs_safe_int_env("KR_XHS_KEYWORD_DEDUPE_TTL_S", 180, 0)
    backoff_max_s = _xhs_safe_int_env("KR_XHS_BACKOFF_MAX_S", 60, 1)
    delays: List[float] = []
    if last_at and min_interval_s:
        delays.append(max(0.0, min_interval_s - (now - last_at)))
    cooldown_until = float(gate.get("cooldown_until") or 0.0)
    if cooldown_until and now < cooldown_until:
        delays.append(cooldown_until - now)
    keyword_norm = re.sub(r"\s+", " ", (keyword or "").strip()).lower()
    last_keyword = str((gate.get("metadata") or {}).get("keyword_norm") or "")
    if keyword_norm and keyword_norm == last_keyword and last_at and now - last_at < dedupe_ttl_s:
        delays.append(min(2.0, max(0.0, dedupe_ttl_s - (now - last_at))))
    if jitter_max_ms > 0:
        delays.append(random.uniform(jitter_min_ms, jitter_max_ms) / 1000.0)
    delay_s = min(float(backoff_max_s), max(delays or [0.0]))
    if delay_s > 0:
        log.info(f"小红书低频护栏等待 {delay_s:.2f}s: type={search_type}")
        time.sleep(delay_s)
    return {
        "guard_enabled": True,
        "guard_delay_s": round(delay_s, 3),
        "keyword_norm": keyword_norm,
        "min_interval_s": min_interval_s,
        "dedupe_ttl_s": dedupe_ttl_s,
    }


def _xhs_failure_cooldown_seconds() -> int:
    gate = _read_xhs_search_gate()
    base = _xhs_safe_int_env("KR_XHS_BACKOFF_BASE_S", 5, 1)
    maximum = _xhs_safe_int_env("KR_XHS_BACKOFF_MAX_S", 60, base)
    if str(gate.get("outcome") or "") in {"blocked", "failed", "degraded"}:
        return min(maximum, max(base, int(gate.get("cooldown_seconds") or base) * 2))
    return base


def _recent_xhs_search_verified(limit: int = 20) -> bool:
    try:
        events = _decision_logger.read_recent(limit)
    except Exception:
        return False
    for event in reversed(events):
        if str(event.get("platform") or "") != "小红书":
            continue
        if str(event.get("event_type") or "") != "search":
            continue
        metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
        if str(metadata.get("strategy") or "") != "external_search_then_detail":
            continue
        if str(metadata.get("failure_type") or "") == "anti_bot_verification":
            return True
        if bool(metadata.get("manual_action_required")) and str(metadata.get("platform_state") or "") == "platform_verification_required":
            return True
    return False


def _iter_text_fragments(value) -> List[str]:
    fragments: List[str] = []
    if value is None:
        return fragments
    if isinstance(value, str):
        if value.strip():
            fragments.append(value)
        return fragments
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"raw_content", "content", "snippet", "description", "text", "title", "url"}:
                fragments.extend(_iter_text_fragments(item))
            else:
                fragments.extend(_iter_text_fragments(item))
        return fragments
    if isinstance(value, (list, tuple, set)):
        for item in value:
            fragments.extend(_iter_text_fragments(item))
    return fragments


def _extract_xhs_urls_from_result(item) -> List[str]:
    fragments = _iter_text_fragments(getattr(item, "raw", None))
    fragments.extend(_iter_text_fragments(getattr(item, "title", "")))
    fragments.extend(_iter_text_fragments(getattr(item, "snippet", "")))
    fragments.extend(_iter_text_fragments(getattr(item, "url", "")))
    urls: List[str] = []
    seen = set()
    for fragment in fragments:
        for match in XHS_NOTE_URL_RE.findall(fragment):
            url = match.split("#", 1)[0]
            if url not in seen:
                seen.add(url)
                urls.append(url)
    return urls


def _xhs_result_looks_dead(item) -> bool:
    text = " ".join(
        str(value or "")
        for value in (
            getattr(item, "title", ""),
            getattr(item, "snippet", ""),
        )
    )
    return "页面不见了" in text or "not found" in text.lower()


def _normalize_xhs_note_url(url: str) -> str:
    return normalize_xhs_note_url(url)


def _expand_xhs_short_url(url: str) -> str:
    return expand_xhs_short_url(url)


def _xhs_url_looks_available(url: str) -> bool:
    result = preflight_xhs_url(
        url,
        negative_cache_lookup=_is_xhs_negative_cached,
        negative_cache_record=lambda cached_url, reason: _record_xhs_negative_cache(cached_url, reason, ttl_s=86400),
    )
    return result.available


def _xhs_external_search_queries(keyword: str) -> List[str]:
    keyword = (keyword or "").strip()
    if not keyword:
        return []
    queries = [
        f"site:xiaohongshu.com/discovery/item {keyword}",
        f"site:xiaohongshu.com/discovery/item {keyword} 小红书 笔记",
        f"小红书 {keyword} 笔记 site:xiaohongshu.com/discovery/item",
        f"小红书 {keyword} 小红书 笔记",
        f"site:xiaohongshu.com/explore {keyword}",
        f"site:xiaohongshu.com/explore/ {keyword}",
        f"xiaohongshu.com/explore {keyword}",
        f"xiaohongshu {keyword}",
    ]
    seen = set()
    unique_queries: List[str] = []
    for query in queries:
        if query not in seen:
            seen.add(query)
            unique_queries.append(query)
    return unique_queries


def _external_search_then_detail(keyword: str, limit: int, *, trace: CollectionTrace, search_type: str = "all") -> Dict:
    providers = ["tavily", "brave", "exa", "searxng"]
    seen = set()
    urls: List[str] = []
    dead_urls: List[str] = []
    provider_used = ""
    query_used = ""
    fallback_errors = []

    queries = _xhs_external_search_queries(keyword)
    for query in queries:
        for provider in providers:
            try:
                from search_providers import search_web, WebSearchRequest

                response = search_web(
                    WebSearchRequest(
                        query=query,
                        limit=max(10, limit * 5),
                        provider=provider,
                        include_raw_content=True,
                        options={},
                    )
                )
                provider_used = response.provider or provider
                query_used = query
                items = response.items or []
                for item in items:
                    looks_dead = _xhs_result_looks_dead(item)
                    candidates = []
                    if getattr(item, "url", ""):
                        candidates.append(str(item.url))
                    candidates.extend(_extract_xhs_urls_from_result(item))
                    for candidate in candidates:
                        expanded = _expand_xhs_short_url(candidate)
                        url = _normalize_xhs_note_url(expanded)
                        if not url:
                            continue
                        if looks_dead or not _xhs_url_looks_available(url):
                            if url not in dead_urls:
                                dead_urls.append(url)
                            _record_xhs_negative_cache(url, "search_dead_or_unavailable", ttl_s=86400)
                            continue
                        if url not in seen:
                            seen.add(url)
                            urls.append(url)
                if len(urls) >= max(limit * 3, 3):
                    break
                if response.error:
                    fallback_errors.append({"query": query, "provider": provider, "error": response.error})
            except Exception as exc:
                fallback_errors.append({"query": query, "provider": provider, "error": str(exc)})
                continue
        if len(urls) >= max(limit * 3, 3):
            break

    if not urls:
        trace.add(
            "external_search_then_detail",
            "failed",
            detail="no_explore_urls",
            error_type="anti_bot_verification",
            retryable=True,
            metadata={"errors": fallback_errors[:8], "queries": queries, "dead_urls": dead_urls[:5]},
        )
        return _format_search_error(
            "小红书",
            {
                "error": "搜索入口触发账号安全验证；详情页链路可能仍可用",
                "type": "anti_bot_verification",
                "retryable": True,
                "failure_type": "anti_bot_verification",
                "message": "搜索入口触发账号安全验证；详情页链路可能仍可用",
                "recommended_fallback": "external_search_then_detail",
                "hint": "请改用外部搜索引擎发现 xiaohongshu.com/explore URL，再进入详情页抽取。",
                "platform_state": "platform_verification_required",
                "manual_action_required": True,
                "login_required": False,
            },
            trace=trace,
            strategy="external_search_then_detail",
        )

    detail_items = []
    attempted_urls: List[str] = []
    for url in urls:
        if len([item for item in detail_items if item.get("title")]) >= limit:
            break
        attempted_urls.append(url)
        try:
            from detail_strategies.xiaohongshu import XiaohongshuDetailDeps, XiaohongshuDetailStrategy
            from routing import attach_routing_metadata
            from runtime.executables import find_node_exe
            from understanding import build_detail_evidence
            from kr_core import DetailRequest

            detail_strategy = XiaohongshuDetailStrategy(
                XiaohongshuDetailDeps(
                    bridge_path=os.path.join(PROJECT_ROOT, "bridge", "xhs_mcp_bridge.cjs"),
                    node_exe=find_node_exe(),
                    recover_xsec_token=recover_xhs_xsec_token,
                    detail_needs_fallback=_xhs_detail_needs_fallback,
                    extract_via_cdp=_extract_xhs_detail_via_cdp,
                    ocr_first_image=_ocr_first_xhs_image,
                    attach_routing=attach_routing_metadata,
                    evidence_builder=build_detail_evidence,
                    log_info=log.info,
                    log_warning=log.warning,
                    log_error=log.error,
                    auto_switch_account=_auto_switch_xhs_account,
                    request_user_login=request_user_login,
                    selected_profile_id=_selected_xhs_profile_id,
                )
            )
            detail = detail_strategy.extract(
                DetailRequest(
                    url=url,
                    enable_deep_analysis=False,
                    enable_comment_filtering=False,
                    auto_multimodal=False,
                    platform="小红书",
                )
            ).to_legacy_dict()
        except Exception as exc:
            detail_items.append({"url": url, "error": str(exc)})
            continue
        if isinstance(detail, dict) and not detail.get("error"):
            detail_items.append(
                {
                    "title": detail.get("title", ""),
                    "url": detail.get("url", url),
                    "author": detail.get("author", ""),
                    "desc": detail.get("desc", "")[:200],
                    "platform": "小红书",
                    "metadata": {
                        "source": "external_search_then_detail",
                        "search_provider": provider_used,
                        "detail_metadata": detail.get("detail_metadata", {}),
                    },
                }
            )
        else:
            failure_type = str(detail.get("failure_type") or "detail_failed") if isinstance(detail, dict) else "detail_failed"
            _record_xhs_negative_cache(url, failure_type, ttl_s=86400 if failure_type in {"dead_link", "empty_detail"} else 3600)
            detail_items.append({"url": url, "error": detail.get("error") if isinstance(detail, dict) else "detail_failed", "failure_type": failure_type})

    trace.add(
        "external_search_then_detail",
        "ok" if any(item.get("title") for item in detail_items) else "failed",
        item_count=len([item for item in detail_items if item.get("title")]),
        metadata={"provider": provider_used, "query": query_used, "urls": attempted_urls[: max(limit * 3, 3)]},
    )
    if any(item.get("title") for item in detail_items):
        return _format_search_response(
            "小红书",
            [item for item in detail_items if item.get("title")],
            trace=trace,
        )
    return _format_search_error(
        "小红书",
        {
            "error": "搜索入口触发账号安全验证；详情页链路可能仍可用",
            "type": "anti_bot_verification",
            "retryable": True,
            "failure_type": "anti_bot_verification",
            "message": "搜索入口触发账号安全验证；详情页链路可能仍可用",
            "recommended_fallback": "external_search_then_detail",
            "platform_state": "platform_verification_required",
            "manual_action_required": True,
            "login_required": False,
            "detail": "外部搜索找到了 URL，但详情抽取未返回有效内容",
            "detail_failure_type": "detail_empty",
        },
        trace=trace,
        strategy="external_search_then_detail",
    )


def _xhs_detail_needs_fallback(note_data: Dict) -> bool:
    if not isinstance(note_data, dict) or not note_data:
        return True
    title = str(note_data.get("title") or "")
    content = str(note_data.get("content") or note_data.get("desc") or "")
    images = note_data.get("images") or []
    has_images = isinstance(images, list) and any(str(image or "").strip() for image in images)
    return (
        "页面不见了" in title
        or "你访问的页面不见了" in content
        or (not title.strip() and not content.strip() and not has_images)
    )


def _recover_xhs_xsec_token(note_id: str) -> str:
    """Recover xsec_token from all open XHS search/detail tabs."""
    try:
        resource_key = _browser_resource_key("xhs")
        if not _ensure_chrome_debugging(resource_key):
            return ""
        scrapling_path = os.path.join(PROJECT_ROOT, "media_platform", "xhs", "scrapling_adapter.py")
        spec = importlib.util.spec_from_file_location("xhs_scrapling_adapter", scrapling_path)
        scrapling_adapter = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(scrapling_adapter)
        req = __import__("urllib.request").request.Request(
            f"{_chrome_debug_url(resource_key)}/json",
            headers={"User-Agent": "Mozilla/5.0"},
            method="GET",
        )
        with __import__("urllib.request").request.urlopen(req, timeout=5) as resp:
            tabs = json.loads(resp.read().decode("utf-8"))
        page_ws_urls = [
            tab.get("webSocketDebuggerUrl", "")
            for tab in tabs
            if tab.get("type") == "page"
            and "xiaohongshu.com" in (tab.get("url") or "")
            and tab.get("webSocketDebuggerUrl")
        ]
        if not page_ws_urls:
            page_ws_urls = [scrapling_adapter._ensure_xhs_page_ws_url(_chrome_debug_url(resource_key))]
        expression = f"""
        (() => {{
          const noteId = {json.dumps(note_id)};
          const links = Array.from(document.querySelectorAll('a[href*="' + noteId + '"]'));
          for (const link of links) {{
            const href = link.href || link.getAttribute('href') || '';
            const match = href.match(/[?&]xsec_token=([^&#]+)/);
            if (match) return JSON.stringify({{ token: decodeURIComponent(match[1]) }});
          }}
          const html = document.documentElement ? document.documentElement.innerHTML || '' : '';
          const escaped = noteId.replace(/[.*+?^${{}}()|[\\]\\\\]/g, '\\\\$&');
          const re = new RegExp(escaped + '[^"\\\\']{{0,500}}[?&]xsec_token=([^&#"\\\\']+)');
          const match = html.match(re);
          return JSON.stringify({{ token: match ? decodeURIComponent(match[1]) : '' }});
        }})()
        """
        for page_ws in page_ws_urls:
            data = scrapling_adapter._cdp_json(page_ws, expression, timeout=8)
            token = str(data.get("token") or "")
            if token:
                return token
    except Exception as e:
        log.debug(f"小红书 xsec_token 恢复失败: {e}")
    return ""


def _recover_pending_xhs_interaction_if_authenticated(state: Dict, *, profile_id_override: str = "") -> Dict:
    """Clear one matching stale manual session only when this profile is now authenticated."""
    if not _xhs_login_state_ok(state):
        return {"recovered": False, "reason": "not_authenticated"}
    profile_id = profile_id_override or _selected_xhs_profile_id()
    resource_key = f"xhs:{profile_id}" if profile_id else "xhs"
    pid = _find_chrome_with_debug_port(resource_key)
    profile_dir = _chrome_user_data_dir_for_pid(pid) if pid else ""
    profile_hash = browser_profile_hash(profile_dir)
    try:
        pending = browser_sessions_summary(limit=30).get("pending_interactions") or []
        match = next(
            (
                item
                for item in pending
                if str(item.get("platform") or "") == "xhs"
                and (
                    (profile_id and str(item.get("profile_id") or "") == profile_id)
                    or (profile_hash and str(item.get("profile_dir_hash") or "") == profile_hash)
                )
            ),
            None,
        )
        if not match:
            return {"recovered": False, "reason": "no_matching_pending_interaction"}
        complete = complete_browser_interaction(
            "xhs",
            probe_result={"status": "ok", "platform_state": "authenticated", "manual_action_required": False},
            profile_id=profile_id,
            profile_dir=profile_dir,
        )
        return {"recovered": True, "profile_id": profile_id, "complete": {"status": complete.get("status")}}
    except Exception as exc:
        log.debug(f"小红书待登录会话自动恢复失败: {exc}")
        return {"recovered": False, "reason": "recovery_error"}


def _extract_xhs_detail_via_cdp(note_id: str, xsec_token: str = "", xsec_source: str = "pc_search") -> Dict | None:
    """Use the existing XHS CDP page as a fast detail fallback."""
    try:
        resource_key = _browser_resource_key("xhs")
        if not _ensure_chrome_debugging(resource_key):
            return None
        scrapling_path = os.path.join(PROJECT_ROOT, "media_platform", "xhs", "scrapling_adapter.py")
        spec = importlib.util.spec_from_file_location("xhs_scrapling_adapter", scrapling_path)
        scrapling_adapter = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(scrapling_adapter)

        page_ws = scrapling_adapter._ensure_xhs_page_ws_url(_chrome_debug_url(resource_key))
        target_url = f"https://www.xiaohongshu.com/explore/{note_id}"
        if xsec_token:
            target_url += f"?xsec_token={xsec_token}&xsec_source={xsec_source or 'pc_search'}"

        snapshot_js = xhs_detail_content_snapshot_js(max_chars=2400)
        snapshot_js_json = json.dumps(snapshot_js)
        js_code = r"""
        (async (url) => {
          const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
          location.href = url;
          await sleep(4500);
          const snapshotFactory = eval(%s);
          let data = await snapshotFactory();
          if (!data.selectorTexts || (!data.selectorTexts['#detail-title'] && !data.selectorTexts['#detail-desc'])) {
            await sleep(3500);
            data = await snapshotFactory();
          }
          return data;
        })
        """ % snapshot_js_json
        snapshot = scrapling_adapter._cdp_json(page_ws, f"({js_code})({json.dumps(target_url)})", timeout=15)
        normalized = normalize_xhs_detail_snapshot(snapshot if isinstance(snapshot, dict) else {})
        data = {
            "title": normalized.get("title", ""),
            "desc": normalized.get("body", ""),
            "content": normalized.get("body", ""),
            "author": "",
            "images": normalized.get("images", []),
            "textSample": str((snapshot or {}).get("textSample") or "")[:300] if isinstance(snapshot, dict) else "",
            "snapshot_status": normalized.get("status", ""),
            "text_len": normalized.get("text_len", 0),
            "url": normalized.get("url") or target_url,
            "selector_keys": normalized.get("selector_keys", []),
            "selector_bundle_version": normalized.get("selector_bundle_version", ""),
            "selector_hits_by_field": normalized.get("selector_hits_by_field", {}),
            "selector_hit_count": normalized.get("selector_hit_count", 0),
            "image_count": normalized.get("image_count", 0),
            "captcha_element_count": normalized.get("captcha_element_count", 0),
            "loading_state": normalized.get("loading_state", ""),
        }
        return data
    except Exception as e:
        log.warning(f"小红书 CDP 详情兜底失败: {e}")
        return None


def _ocr_first_xhs_image(images: List[str]) -> Dict:
    """Run a one-image RapidOCR trial for Xiaohongshu detail extraction."""
    started = time.time()
    if not images:
        return {"status": "skipped", "reason": "no_images", "elapsed_s": 0}

    image_url = str(images[0] or "")
    if not image_url:
        return {"status": "skipped", "reason": "empty_image_url", "elapsed_s": 0}

    try:
        from rapidocr_onnxruntime import RapidOCR

        resp = httpx.get(
            image_url,
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.xiaohongshu.com/"},
            timeout=20,
            follow_redirects=True,
        )
        resp.raise_for_status()

        engine = RapidOCR()
        ocr_result, runtime = engine(resp.content)
        items = []
        for item in ocr_result or []:
            box, text, score = item
            items.append({
                "text": str(text),
                "score": float(score),
                "box": box,
            })
        text = "\n".join(item["text"] for item in items if item.get("text")).strip()
        elapsed = time.time() - started
        log.info(f"小红书首图 OCR 完成: chars={len(text)}, items={len(items)}, elapsed={elapsed:.2f}s")
        return {
            "status": "ok",
            "engine": "rapidocr_onnxruntime",
            "image_url": image_url,
            "text": text,
            "items": items,
            "elapsed_s": round(elapsed, 2),
        }
    except Exception as e:
        elapsed = time.time() - started
        log.warning(f"小红书首图 OCR 失败: {e}")
        return {"status": "degraded", "error": str(e), "elapsed_s": round(elapsed, 2)}


def legacy_search_xiaohongshu(keyword: str, limit: int = 10, search_type: str = "all", probe_mode: bool = False) -> Dict:
    """小红书搜索入口（外层包装），确保搜索完成后触发 Chrome 空闲清理。"""
    from runtime.xhs_operation_coordinator import xhs_operation

    with xhs_operation("search", keyword=keyword):
        with chrome_active_operation("xhs"):
            return _legacy_search_xiaohongshu_impl(keyword, limit, search_type, probe_mode)


def _legacy_search_xiaohongshu_impl(keyword: str, limit: int = 10, search_type: str = "all", probe_mode: bool = False) -> Dict:
    log.info(f"search_xiaohongshu: keyword={keyword!r}, limit={limit}, type={search_type}, probe_mode={probe_mode}")
    limit = 1 if probe_mode else min(limit, 20)
    # A gate is a historical safety signal, not a global verdict on every
    # isolated profile or on the non-browser fallbacks.  It changes the first
    # low-risk action to external discovery, then the current account still
    # gets one serialized page-level preflight if discovery produces no usable
    # evidence.  This must never fan a verified risk event out to A/B/C.
    gate = _xhs_search_gate_active()
    prefer_external = False if probe_mode else bool(gate.get("active") or _recent_xhs_search_verified())
    if prefer_external:
        strategy_tree = ["previous_global_gate_observed", "external_search_then_detail", "chrome_cdp_preflight", "login_preflight", "scrapling_cdp", "tikhub_break_glass", "bridge_fallback_diagnostic_only"]
    else:
        strategy_tree = ["chrome_cdp_preflight", "login_preflight", "scrapling_cdp", "tikhub_break_glass", "bridge_fallback_diagnostic_only"]
    trace = CollectionTrace("小红书", strategy_tree)
    effective_type = "image" if probe_mode and search_type in ("all", "normal", "") else ("all" if search_type in ("all", "normal", "") else search_type)
    guard_state = _xhs_low_frequency_guard(keyword, search_type=effective_type, probe_mode=probe_mode)

    if gate.get("active") and not probe_mode:
        trace.add(
            "previous_global_gate_observed",
            "ok",
            detail="historical_gate_changes_first_action_only",
            metadata={
                "last_outcome": str(gate.get("last_outcome") or ""),
                "cooldown_remaining_s": gate.get("cooldown_remaining_s", 0),
                "last_reason": str(gate.get("last_reason") or "")[:120],
            },
        )

    if prefer_external:
        log.info("  策略: 历史失败信号存在，先以站外发现 + 详情验证获取低风险证据")
        fallback_result = _external_search_then_detail(keyword, limit, trace=trace, search_type=effective_type)
        if fallback_result.get("items"):
            _record_xhs_search_gate(
                outcome="ok",
                reason="external_search_then_detail_success",
                search_type=effective_type,
                probe_mode=probe_mode,
                metadata={"strategy": "external_search_then_detail", "historical_gate_observed": bool(gate.get("active"))},
            )
            return fallback_result

    active_profile_id = _selected_xhs_profile_id()
    active_resource = f"xhs:{active_profile_id}" if active_profile_id else "xhs"
    if not _ensure_chrome_debugging(active_resource, target_profile_id=active_profile_id):
        log.warning("Chrome 调试模式不可用，终止小红书搜索")
        trace.add("chrome_cdp_preflight", "failed", detail="cdp_unavailable", error_type="cdp_unavailable", retryable=True)
        switch = _auto_switch_xhs_account(
            purpose="search",
            reason_code="CDP_PORT_UNAVAILABLE",
            trace=trace,
            last_tool="search_xiaohongshu_cdp_preflight",
        )
        api_fallback = _try_tikhub_break_glass_search(keyword, limit, trace=trace, trigger_reason="cdp_unavailable")
        if api_fallback:
            return api_fallback
        result = _format_search_error("小红书", {
            "error": "Chrome/CDP 不可用，无法执行小红书搜索",
            "retryable": True,
            "hint": f"确认 Chrome 可启动、{XHS_CHROME_DEBUG_PORT} 调试端口未被占用，并保留 xhs_user_data_dir 登录态",
        }, trace=trace, strategy="chrome_cdp_preflight")
        metadata = dict(result.get("metadata") or {})
        metadata["platform_health_probe"] = _xhs_probe_payload(
            status="fail",
            reason_code="CDP_UNAVAILABLE",
            mode="read_only",
            evidence={"cdp_url": _chrome_debug_url(active_resource)},
        )
        metadata["account_auto_switch"] = switch
        result["metadata"] = metadata
        return result
    trace.add("chrome_cdp_preflight", "ok", detail="debug_port_ready")
    account_state = xiaohongshu_account_state(_chrome_debug_url(active_resource))
    pending_recovery: Dict = {}
    if not _xhs_login_state_ok(account_state):
        account_switches: List[Dict] = []
        # A confirmed login or verification gate on any profile is immediately
        # surfaced for that exact profile. Search probing then continues in the
        # fixed B -> A -> C account-pool order; it never retries the gated
        # profile and it never hides later account prompts behind the first.
        for switches_used in range(3):
            reason, trigger_evidence = _xhs_manual_auth_evidence(account_state)
            if reason and active_profile_id:
                interaction = request_user_login(
                    "xhs",
                    reason,
                    target_profile_id=active_profile_id,
                    trigger_evidence=trigger_evidence,
                    source="search_xiaohongshu.login_failover",
                )
                trace.add("manual_interaction", "ok", detail=reason, metadata={"profile_id": active_profile_id, "manual_interaction": interaction})
            if switches_used >= 2:
                break
            account_switch = _auto_switch_xhs_account(
                purpose="search",
                reason_code="SECURITY_VERIFICATION" if bool(account_state.get("has_verify_prompt")) else "LOGIN_REQUIRED",
                trace=trace,
                last_tool="search_xiaohongshu_login_preflight",
                notes=[str(account_state.get("msg") or account_state.get("detail") or "")[:120]],
                current_profile_id=active_profile_id,
                switches_used=switches_used,
                allow_manual_recovery_followup=True,
            )
            account_switches.append(account_switch)
            switch = account_switch.get("switch") or {}
            next_profile_id = str(switch.get("target_profile_id") or "")
            if str(switch.get("status") or "") != "ok" or not next_profile_id:
                break
            active_profile_id = next_profile_id
            active_resource = f"xhs:{active_profile_id}"
            if not _ensure_chrome_debugging(active_resource, target_profile_id=active_profile_id):
                continue
            account_state = xiaohongshu_account_state(_chrome_debug_url(active_resource))
            if _xhs_login_state_ok(account_state):
                trace.add("login_preflight_after_account_switch", "ok", detail="alternate_profile_authenticated", metadata={"account_auto_switches": account_switches})
                break
        if _xhs_login_state_ok(account_state):
            trace.add(
                "login_preflight",
                "ok",
                detail="account_state_confirmed_after_failover",
                metadata={"code": account_state.get("code"), "account_auto_switches": account_switches, "active_profile_id": active_profile_id},
            )
        else:
            api_fallback = _try_tikhub_break_glass_search(keyword, limit, trace=trace, trigger_reason="login_preflight_failed")
            if api_fallback:
                return api_fallback
            return _xhs_login_preflight_result(trace, account_state, account_switch={"attempts": account_switches}, profile_id_override=active_profile_id)
    pending_recovery = _recover_pending_xhs_interaction_if_authenticated(account_state, profile_id_override=active_profile_id)
    trace.add(
        "login_preflight",
        "ok",
        detail="account_state_confirmed",
        metadata={
            "code": account_state.get("code"),
            "guest": bool(account_state.get("guest")),
            "has_login_prompt": bool(account_state.get("has_login_prompt")),
            "has_verify_prompt": bool(account_state.get("has_verify_prompt")),
            "pending_manual_recovery": pending_recovery,
        },
    )

    def _map_item(item: dict, forced_type: str = "") -> dict:
        note_id = item.get("noteId") or item.get("note_id") or ""
        xsec_token = item.get("xsecToken") or item.get("xsec_token", "")
        note_url = item.get("url", "")
        if note_id and xsec_token:
            note_url = f"https://www.xiaohongshu.com/explore/{note_id}?xsec_token={xsec_token}&xsec_source=pc_search"
        elif not note_url and note_id:
            note_url = f"https://www.xiaohongshu.com/explore/{note_id}"
        note_type = forced_type or item.get("noteType", "")
        return {
            "title": item.get("title", ""),
            "author": item.get("author", ""),
            "desc": (item.get("desc") or "")[:200],
            "url": note_url,
            "likes": item.get("likes", 0),
            "note_id": note_id,
            "xsec_token": xsec_token,
            "platform": "小红书",
            "type": note_type,
        }

    def _bridge_search() -> List[Dict]:
        mcp_client_path = os.path.join(PROJECT_ROOT, "media_platform", "xhs", "mcp_client.py")
        spec = importlib.util.spec_from_file_location("xhs_mcp_client", mcp_client_path)
        mcp_client = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(mcp_client)
        McpXhsClient = mcp_client.McpXhsClient
        mcp_client.CHROME_DEBUG_PORT = str(_chrome_debug_url(active_resource).rsplit(":", 1)[-1])
        mcp_client.CHROME_PORT_URL = _chrome_debug_url(active_resource)
        client = McpXhsClient()

        if effective_type == "all":
            log.info("  策略: 双搜（图文 + 视频）")
            image_items = client.search(keyword, feed_type="image")
            video_items = client.search(keyword, feed_type="video")
            log.info(f"  MCP 原始返回: 图文={len(image_items)}, 视频={len(video_items)}")

            valid_image_items = [item for item in image_items if not item.get("login_required")]
            valid_video_items = [item for item in video_items if not item.get("login_required")]
            if any(item.get("login_required") for item in image_items + video_items) and not (valid_image_items or valid_video_items):
                trace.add("bridge_fallback", "failed", detail="login_required_or_verification", error_type="anti_bot", retryable=True)
                return _format_search_error("小红书", {
                    "error": "小红书搜索被平台验证拦截",
                    "hint": "当前登录态可能仍在，但搜索链路被平台验证阻断；请手工处理验证页后重试。",
                    "platform_state": "platform_verification_required",
                    "manual_action_required": True,
                    "login_required": False,
                }, trace=trace, strategy="bridge_fallback")

            raw_items = []
            seen = set()
            for item in valid_image_items:
                nid = item.get("noteId") or item.get("note_id", "")
                if nid and nid not in seen:
                    seen.add(nid)
                    item["noteType"] = "image"
                    raw_items.append(item)
            for item in valid_video_items:
                nid = item.get("noteId") or item.get("note_id", "")
                if nid and nid not in seen:
                    seen.add(nid)
                    item["noteType"] = "video"
                    raw_items.append(item)
            return [_map_item(item) for item in raw_items[:limit]]

        raw_items = client.search(keyword, feed_type=effective_type)
        log.info(f"  MCP 原始返回: {effective_type}={len(raw_items)} 条")
        if any(item.get("login_required") for item in raw_items):
            trace.add("bridge_fallback", "failed", detail="login_required_or_verification", error_type="anti_bot", retryable=True)
            return _format_search_error("小红书", {
                "error": "小红书搜索被平台验证拦截",
                "hint": "当前登录态可能仍在，但搜索链路被平台验证阻断；请手工处理验证页后重试。",
                "platform_state": "platform_verification_required",
                "manual_action_required": True,
                "login_required": False,
            }, trace=trace, strategy="bridge_fallback")
        return [_map_item(item, forced_type=effective_type) for item in raw_items[:limit]]

    try:
        scrapling_path = os.path.join(PROJECT_ROOT, "media_platform", "xhs", "scrapling_adapter.py")
        spec = importlib.util.spec_from_file_location("xhs_scrapling_adapter", scrapling_path)
        scrapling_adapter = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(scrapling_adapter)

        if effective_type == "all":
            log.info("  策略: Scrapling 双搜（图文 + 视频）")
            raw_items = []
            seen = set()
            for feed_type in ("image", "video"):
                for item in scrapling_adapter.search(keyword, limit=limit, feed_type=feed_type, cdp_url=_chrome_debug_url(active_resource)):
                    nid = item.get("note_id") or item.get("noteId", "")
                    if nid and nid not in seen:
                        seen.add(nid)
                        raw_items.append(item)
            mapped = raw_items[:limit]
        else:
            log.info(f"  策略: Scrapling 单类型搜索 ({effective_type})")
            mapped = scrapling_adapter.search(
                keyword,
                limit=limit,
                feed_type=effective_type,
                cdp_url=_chrome_debug_url(active_resource),
                timeout_ms=9000 if probe_mode else 45000,
                probe_mode=probe_mode,
            )

        log.info(f"  Scrapling -> 返回 {len(mapped)} 条")
        trace.add("scrapling_cdp", "ok", item_count=len(mapped))
        _record_xhs_search_gate(
            outcome="ok",
            reason="scrapling_cdp_success",
            search_type=effective_type,
            probe_mode=probe_mode,
            metadata={"strategy": "scrapling_cdp", **guard_state},
        )
        return _format_search_response("小红书", mapped, trace=trace)
    except Exception as e:
        error_data = e.to_dict() if hasattr(e, "to_dict") else {"error": str(e), "type": "scrapling_error"}
        log.warning(f"Scrapling 小红书搜索失败，将回退 bridge: {error_data}")
        verification_hit = error_data.get("login_required") or error_data.get("type") == "verification_required"
        error_type = "anti_bot_verification" if verification_hit else str(error_data.get("type") or "request_failed")
        trace.add("scrapling_cdp", "failed", detail=str(error_data.get("error") or ""), error_type=error_type, retryable=True)
        if verification_hit:
            profile_id = _selected_xhs_profile_id()
            manual_interaction = (
                request_user_login(
                    "xhs",
                    "platform_verification_required",
                    target_profile_id=profile_id,
                    trigger_evidence=["xhs_search_response_verification_required=true"],
                    source="search_xiaohongshu.scrapling_cdp",
                )
                if profile_id
                else {
                    "status": "deferred",
                    "validation_status": "EXPECTED_DEGRADED",
                    "reason_code": "PROFILE_BINDING_REQUIRED",
                    "detail": "未能绑定小红书实际账号，已保持后台，不会弹出浏览器。",
                }
            )
            trace.add(
                "manual_interaction",
                "ok",
                detail="platform_verification_required",
                metadata={"manual_interaction": manual_interaction},
            )
        else:
            _auto_switch_xhs_account(
                purpose="search",
                reason_code="SEARCH_PAGE_DEGRADED",
                trace=trace,
                last_tool="search_xiaohongshu_scrapling_cdp",
                notes=[str(error_data.get("error") or "")[:120]],
            )

        if verification_hit:
            api_fallback = _try_tikhub_break_glass_search(
                keyword,
                limit,
                trace=trace,
                trigger_reason="scrapling_security_verification",
            )
            if api_fallback:
                return api_fallback
            _record_xhs_search_gate(
                outcome="blocked",
                reason="platform_verification_required",
                search_type=effective_type,
                probe_mode=probe_mode,
                cooldown_seconds=_xhs_failure_cooldown_seconds(),
                metadata={
                    "strategy": "scrapling_cdp",
                    "probe_mode": probe_mode,
                    "manual_action_required": True,
                    "platform_state": "platform_verification_required",
                    "failure_type": "anti_bot_verification",
                    "manual_interaction": manual_interaction,
                    **guard_state,
                },
            )
            if probe_mode:
                result = _format_search_error(
                    "小红书",
                    {
                        "error": "小红书轻量探测被平台验证拦截",
                        "type": "anti_bot_verification",
                        "failure_type": "anti_bot_verification",
                        "message": "小红书轻量探测被平台验证拦截",
                        "retryable": False,
                        "platform_state": "platform_verification_required",
                        "manual_action_required": True,
                        "login_required": False,
                        "probe_mode": True,
                        "recommended_action": "请先在弹出的 Chrome 窗口完成小红书安全验证；完成后调用 health_check(mode='complete_browser_interaction:xhs') 再重试。",
                        "manual_interaction": manual_interaction,
                    },
                    trace=trace,
                    strategy="scrapling_cdp_probe",
                )
                metadata = dict(result.get("metadata") or {})
                metadata["platform_health_probe"] = probe_from_error(
                    platform="xiaohongshu",
                    tool="force_probe",
                    mode="light_action",
                    error=result.get("error") or {},
                    profile_id=f"xhs_chrome_{XHS_CHROME_DEBUG_PORT}",
                    browser_base=f"chrome_{XHS_CHROME_DEBUG_PORT}",
                )
                result["metadata"] = metadata
                return result
            return _format_search_error(
                "小红书",
                {
                    "error": "搜索入口触发账号安全验证，已请求人工处理",
                    "type": "anti_bot_verification",
                    "failure_type": "anti_bot_verification",
                    "message": "搜索入口触发账号安全验证，已请求人工处理",
                    "retryable": False,
                    "platform_state": "platform_verification_required",
                    "manual_action_required": True,
                    "login_required": False,
                    "recommended_action": "请先在弹出的 Chrome 窗口完成小红书安全验证；完成后调用 health_check(mode='complete_browser_interaction:xhs') 再重试。",
                    "manual_interaction": manual_interaction,
                },
                trace=trace,
                strategy="manual_interaction",
            )

        if probe_mode:
            _record_xhs_search_gate(
                outcome="failed",
                reason=str(error_data.get("error") or error_data)[:240],
                search_type=effective_type,
                probe_mode=True,
                metadata={"strategy": "scrapling_cdp_probe", "error_type": error_type},
            )
            result = _format_search_error("小红书", {
                **error_data,
                "error": str(error_data.get("error") or "小红书轻量探测失败"),
                "type": error_type,
                "retryable": True,
                "probe_mode": True,
                "recommended_action": "轻量 probe 只验证 Chrome/CDP + Scrapling 主链路，不进入 sidecar/bridge/external fallback。",
            }, trace=trace, strategy="scrapling_cdp_probe")
            metadata = dict(result.get("metadata") or {})
            metadata["platform_health_probe"] = probe_from_error(
                platform="xiaohongshu",
                tool="force_probe",
                mode="light_action",
                error=result.get("error") or {},
                profile_id=f"xhs_chrome_{XHS_CHROME_DEBUG_PORT}",
                browser_base=f"chrome_{XHS_CHROME_DEBUG_PORT}",
            )
            result["metadata"] = metadata
            return result

        bridge_breaker = get_degradation_policy().is_open(XHS_SEARCH_BRIDGE_BREAKER_KEY)
        if not _bridge_production_enabled() or bridge_breaker.get("open"):
            reason = "bridge_fallback_diagnostic_only"
            if bridge_breaker.get("open"):
                reason = f"bridge breaker open: {bridge_breaker.get('last_reason') or 'recent failures'}"
            trace.add(
                "bridge_fallback_diagnostic_only",
                "skipped",
                detail=reason,
                error_type="PROVIDER_UNAVAILABLE",
                retryable=False,
                metadata=_bridge_diagnostic_metadata(reason=reason, breaker=bridge_breaker),
            )
            result = _format_search_error(
                "小红书",
                {
                    **error_data,
                    "error": "小红书 Scrapling 主链路失败；bridge_fallback 当前为 diagnostic-only，未进入生产兜底",
                    "type": error_type,
                    "retryable": True,
                    "bridge_fallback_status": "diagnostic_only",
                    "recommended_action": "使用诊断验收集验证 bridge 独立恢复能力；通过前不要恢复生产 fallback。",
                },
                trace=trace,
                strategy="scrapling_cdp",
            )
            metadata = dict(result.get("metadata") or {})
            metadata.update(_bridge_diagnostic_metadata(reason=reason, breaker=bridge_breaker))
            metadata["platform_health_probe"] = probe_from_error(
                platform="xiaohongshu",
                tool="search_xiaohongshu",
                mode="active_action",
                error=result.get("error") or {},
                profile_id=f"xhs_chrome_{XHS_CHROME_DEBUG_PORT}",
                browser_base=f"chrome_{XHS_CHROME_DEBUG_PORT}",
            )
            result["metadata"] = metadata
            _record_xhs_search_gate(
                outcome="failed",
                reason=str((result.get("error") or {}).get("error") or "scrapling_failed_bridge_diagnostic_only")[:240],
                search_type=effective_type,
                probe_mode=probe_mode,
                cooldown_seconds=_xhs_failure_cooldown_seconds(),
                metadata={"strategy": "scrapling_cdp", "bridge": "diagnostic_only", **guard_state},
            )
            return result

        try:
            mapped = _bridge_search()
            if isinstance(mapped, dict):
                if mapped.get("error"):
                    mapped["metadata"] = {**(mapped.get("metadata") or {}), **trace.to_metadata()}
                    get_degradation_policy().mark_failure(
                        XHS_SEARCH_BRIDGE_BREAKER_KEY,
                        "xhs_search_bridge_fallback",
                        str((mapped.get("error") or {}).get("error") or mapped.get("error") or "bridge_failed")[:240],
                        metadata={"search_type": effective_type},
                        failure_threshold=3,
                        cooldown_seconds=21600,
                    )
                    _record_xhs_search_gate(
                        outcome="failed",
                        reason=str((mapped.get("error") or {}).get("error") or mapped.get("error") or "bridge_failed")[:240],
                        search_type=effective_type,
                        probe_mode=probe_mode,
                        metadata={"strategy": "bridge_fallback"},
                    )
                return mapped
            log.info(f"  -> 返回 {len(mapped)} 条（含 type 标记）")
            trace.add("bridge_fallback", "ok", item_count=len(mapped))
            get_degradation_policy().mark_success(
                XHS_SEARCH_BRIDGE_BREAKER_KEY,
                "xhs_search_bridge_fallback",
                {"search_type": effective_type, "item_count": len(mapped)},
            )
            _record_xhs_search_gate(
                outcome="ok",
                reason="bridge_fallback_success",
                search_type=effective_type,
                probe_mode=probe_mode,
                metadata={"strategy": "bridge_fallback"},
            )
            return _format_search_response("小红书", mapped, trace=trace)
        except FileNotFoundError as bridge_e:
            log.error(f"桥接脚本未找到: {bridge_e}")
            trace.add("bridge_fallback", "failed", detail=str(bridge_e), error_type="request_failed", retryable=True)
            return _format_search_error("小红书", {
                **error_data,
                "fallback_error": "xhs_mcp_bridge.cjs 未找到，请确认文件存在",
                "fallback_detail": str(bridge_e),
            }, trace=trace, strategy="bridge_fallback")
        except Exception as bridge_e:
            log.error(f"小红书 bridge fallback 异常: {bridge_e}")
            import traceback
            trace.add("bridge_fallback", "failed", detail=str(bridge_e), error_type="request_failed", retryable=True)
            return _format_search_error("小红书", {
                **error_data,
                "fallback_error": f"小红书 bridge fallback 异常: {str(bridge_e)}",
                "fallback_detail": traceback.format_exc()[-300:],
                "hint": "小红书页面出现平台验证/APP扫码查看提示；请在当前 Chrome 窗口完成手动验证后重试。",
                "platform_state": "platform_verification_required",
                "manual_action_required": True,
                "login_required": False,
            }, trace=trace, strategy="bridge_fallback")


detail_needs_fallback = _xhs_detail_needs_fallback
recover_xhs_xsec_token = _recover_xhs_xsec_token
extract_xhs_detail_via_cdp = _extract_xhs_detail_via_cdp
ocr_first_xhs_image = _ocr_first_xhs_image
