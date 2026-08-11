"""TikHub Xiaohongshu API fallback plan and gated execution.

TikHub is the paid final fallback for Xiaohongshu. Browser/account routes stay
first, and the live executor still enforces the hard daily paid-call counters.
"""

from __future__ import annotations

import os
import time
import uuid
from typing import Any, Dict

import httpx

from .chrome_manager import XHS_CHROME_DEBUG_PORT
from .env_loader import load_runtime_env
from .xhs_tikhub_adapter import normalize_tikhub_xhs_detail_response, normalize_tikhub_xhs_search_response
from .xhs_tikhub_usage import (
    check_tikhub_daily_limit,
    record_tikhub_reservation_outcome,
    reserve_tikhub_daily_limit,
    tikhub_usage_summary,
)


load_runtime_env()

TIKHUB_XHS_APP_V2_SEARCH_ENDPOINT = "/api/v1/xiaohongshu/app_v2/search_notes"
TIKHUB_XHS_APP_V2_DETAIL_ENDPOINT = "/api/v1/xiaohongshu/app_v2/get_image_note_detail"
CONFIRMATION_PHRASE = "CONFIRM_TIKHUB_PAID_SEARCH_LIMIT_2"
BREAK_GLASS_ENV = "KR_XHS_TIKHUB_BREAK_GLASS_AUTO"
BREAK_GLASS_DRY_RUN_ENV = "KR_XHS_TIKHUB_BREAK_GLASS_DRY_RUN"
BREAK_GLASS_DAILY_BUDGET_ENV = "KR_XHS_TIKHUB_DAILY_BUDGET_USD"
BREAK_GLASS_MAX_CALLS_ENV = "KR_XHS_TIKHUB_MAX_CALLS_PER_TASK"


def plan_tikhub_xhs_search_fallback(keyword: str, *, limit: int = 1) -> Dict[str, Any]:
    """Return a spend-aware execution plan without calling TikHub."""
    effective_limit = max(1, min(int(limit or 1), 1))
    return {
        "schema": "knowledgeradar-tikhub-xhs-fallback-plan/v1",
        "status": "ready_for_manual_confirm" if _api_key_configured() else "awaiting_api_key",
        "provider": "tikhub",
        "platform": "xiaohongshu",
        "purpose": "search_discovery_fallback",
        "endpoint": TIKHUB_XHS_APP_V2_SEARCH_ENDPOINT,
        "keyword": str(keyword or ""),
        "requested_limit": limit,
        "effective_limit": effective_limit,
        "mode": "manual_confirm",
        "confirmation_required": True,
        "confirmation_phrase": CONFIRMATION_PHRASE,
        "side_effects_if_executed": {
            "api_call": True,
            "billing_possible": True,
            "browser_launch": False,
            "station_search": False,
            "account_switch": False,
            "main_chain_admission": False,
        },
        "guards": {
            "max_calls_per_execution": 1,
            "daily_paid_call_limit": tikhub_usage_summary().get("search", {}),
            "do_not_retry": True,
            "do_not_expand_page": True,
            "do_not_promote_main_chain": True,
            "search_safe_auto": "denied",
            "detail_safe_auto": "denied",
        },
    }


def plan_tikhub_break_glass_fallback(
    keyword: str,
    *,
    limit: int = 1,
    browser_availability: Dict[str, Any] | None = None,
    route_scoring: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Plan TikHub paid search when every browser route is unavailable.

    This function is plan-only. It does not call TikHub and it does not mutate
    route state. The live executor below still enforces the same guards.
    """
    manual_plan = plan_tikhub_xhs_search_fallback(keyword, limit=limit)
    browser_availability = browser_availability or {}
    route_scoring = route_scoring or {}
    browser_unavailable = _browser_pool_unavailable(browser_availability, route_scoring)
    enabled = _break_glass_enabled()
    dry_run = _break_glass_dry_run()
    budget = _break_glass_daily_budget()
    max_calls = _break_glass_max_calls()
    status = "ready_for_break_glass" if manual_plan["status"] == "ready_for_manual_confirm" and enabled and browser_unavailable else "not_active"
    blockers = []
    if manual_plan["status"] != "ready_for_manual_confirm":
        blockers.append("tikhub_api_key_missing")
    if not enabled:
        blockers.append(f"{BREAK_GLASS_ENV}_not_enabled")
    if not browser_unavailable:
        blockers.append("browser_pool_still_has_candidate")
    if budget <= 0:
        blockers.append("daily_budget_zero")
    if max_calls <= 0:
        blockers.append("max_calls_zero")
    daily = check_tikhub_daily_limit("search")
    if not daily.get("allowed"):
        blockers.append(str(daily.get("reason_code") or "daily_limit_reached"))
    return {
        **manual_plan,
        "schema": "knowledgeradar-tikhub-xhs-break-glass-plan/v1",
        "status": status if not blockers else "blocked",
        "mode": "break_glass_auto" if enabled else "manual_confirm",
        "confirmation_required": False if enabled and browser_unavailable else True,
        "break_glass": {
            "enabled": enabled,
            "dry_run": dry_run,
            "browser_pool_unavailable": browser_unavailable,
            "trigger_condition": "healthy_browser_count_zero_or_no_observed_search_candidate",
            "daily_budget_usd": budget,
            "max_calls_per_task": max_calls,
            "daily_usage": daily.get("summary", {}),
            "blockers": blockers,
        },
        "guards": {
            **manual_plan["guards"],
            "break_glass_only_when_browser_pool_unavailable": True,
            "dry_run_env": BREAK_GLASS_DRY_RUN_ENV,
            "enable_env": BREAK_GLASS_ENV,
        },
    }


def execute_tikhub_break_glass_fallback(
    keyword: str,
    *,
    limit: int = 1,
    browser_availability: Dict[str, Any] | None = None,
    route_scoring: Dict[str, Any] | None = None,
    timeout_s: float = 20.0,
    task_failover_id: str = "",
) -> Dict[str, Any]:
    """Execute one TikHub break-glass search when policy permits it."""
    plan = plan_tikhub_break_glass_fallback(
        keyword,
        limit=limit,
        browser_availability=browser_availability,
        route_scoring=route_scoring,
    )
    if plan.get("status") != "ready_for_break_glass":
        return {**plan, "status": "blocked", "reason_code": "BREAK_GLASS_GUARD_BLOCKED", "api_call_count": 0}
    if _break_glass_dry_run():
        return {**plan, "status": "dry_run", "reason_code": "BREAK_GLASS_DRY_RUN", "api_call_count": 0}
    return execute_tikhub_xhs_search_fallback(
        keyword,
        limit=limit,
        confirmation=CONFIRMATION_PHRASE,
        timeout_s=timeout_s,
        task_failover_id=task_failover_id,
    )


def execute_tikhub_xhs_search_fallback(
    keyword: str,
    *,
    limit: int = 1,
    confirmation: str = "",
    timeout_s: float = 20.0,
    task_failover_id: str = "",
) -> Dict[str, Any]:
    """Execute exactly one paid TikHub search when explicitly confirmed."""
    plan = plan_tikhub_xhs_search_fallback(keyword, limit=limit)
    if confirmation != CONFIRMATION_PHRASE:
        return {
            **plan,
            "status": "blocked",
            "reason_code": "MANUAL_CONFIRM_REQUIRED",
            "api_call_count": 0,
        }
    api_key = _api_key()
    if not api_key:
        return {
            **plan,
            "status": "blocked",
            "reason_code": "TIKHUB_API_KEY_MISSING",
            "api_call_count": 0,
        }
    reservation_id = str(task_failover_id or f"xhs-search:{uuid.uuid4().hex}")
    reservation = reserve_tikhub_daily_limit("search", reservation_id=reservation_id)
    if not reservation.get("reserved"):
        return {
            **plan,
            "status": "blocked",
            "reason_code": reservation.get("reason_code") or "TIKHUB_DAILY_SEARCH_LIMIT_REACHED",
            "api_call_count": 0,
            "usage": reservation.get("summary", {}),
        }
    url = _base_url().rstrip("/") + TIKHUB_XHS_APP_V2_SEARCH_ENDPOINT
    started = time.perf_counter()
    try:
        with httpx.Client(timeout=timeout_s, follow_redirects=True) as client:
            response = client.get(
                url,
                params={"keyword": str(keyword or ""), "limit": plan["effective_limit"]},
                headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
            )
        elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
        try:
            payload = response.json()
        except Exception:
            payload = {}
        if not (200 <= response.status_code < 300):
            usage = record_tikhub_reservation_outcome(reservation_id, status="degraded", reason_code=f"HTTP_{response.status_code}", billed=True)
            return {
                **plan,
                "status": "degraded",
                "reason_code": f"HTTP_{response.status_code}",
                "http_status": response.status_code,
                "elapsed_ms": elapsed_ms,
                "api_call_count": 1,
                "response_keys": list(payload.keys())[:30] if isinstance(payload, dict) else [],
                "secret_exposed": False,
                "usage": usage, "reservation_id": reservation_id,
            }
        normalized = normalize_tikhub_xhs_search_response(
            payload if isinstance(payload, dict) else {},
            keyword=str(keyword or ""),
            limit=plan["effective_limit"],
        ).to_mcp_dict()
        usage = record_tikhub_reservation_outcome(reservation_id, status="ok", reason_code="OK_PAID_SEARCH_DISCOVERY", billed=True)
        return {
            **plan,
            "status": "ok",
            "reason_code": "OK_PAID_SEARCH_DISCOVERY",
            "http_status": response.status_code,
            "elapsed_ms": elapsed_ms,
            "api_call_count": 1,
            "result": normalized,
            "raw_response_stored": False,
            "secret_exposed": False,
            "usage": usage, "reservation_id": reservation_id,
        }
    except Exception as exc:
        usage = record_tikhub_reservation_outcome(reservation_id, status="failed", reason_code=exc.__class__.__name__, billed=None)
        return {
            **plan,
            "status": "failed",
            "reason_code": exc.__class__.__name__,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
            "api_call_count": 1,
            "error": str(exc)[:300],
            "secret_exposed": False,
            "usage": usage, "reservation_id": reservation_id,
        }


def plan_tikhub_xhs_detail_fallback(note_id: str, *, share_text: str = "") -> Dict[str, Any]:
    daily = check_tikhub_daily_limit("detail")
    return {
        "schema": "knowledgeradar-tikhub-xhs-detail-fallback-plan/v1",
        "status": "ready_for_manual_confirm" if _api_key_configured() else "awaiting_api_key",
        "provider": "tikhub",
        "platform": "xiaohongshu",
        "purpose": "detail_break_glass_fallback",
        "endpoint": TIKHUB_XHS_APP_V2_DETAIL_ENDPOINT,
        "note_id": str(note_id or ""),
        "share_text_present": bool(str(share_text or "").strip()),
        "mode": "manual_confirm",
        "confirmation_required": True,
        "confirmation_phrase": CONFIRMATION_PHRASE,
        "guards": {
            "max_calls_per_execution": 1,
            "daily_paid_call_limit": daily.get("summary", {}).get("detail", {}),
            "do_not_retry": True,
            "do_not_promote_main_chain": True,
        },
    }


def execute_tikhub_xhs_detail_fallback(
    note_id: str,
    *,
    xsec_token: str = "",
    xsec_source: str = "pc_search",
    share_text: str = "",
    confirmation: str = "",
    timeout_s: float = 20.0,
    task_failover_id: str = "",
) -> Dict[str, Any]:
    plan = plan_tikhub_xhs_detail_fallback(note_id, share_text=share_text)
    auto_allowed = _break_glass_enabled() and not _break_glass_dry_run()
    if confirmation != CONFIRMATION_PHRASE and not auto_allowed:
        return {**plan, "status": "blocked", "reason_code": "MANUAL_CONFIRM_REQUIRED", "api_call_count": 0}
    if _break_glass_dry_run() and confirmation != CONFIRMATION_PHRASE:
        return {**plan, "status": "dry_run", "reason_code": "BREAK_GLASS_DRY_RUN", "api_call_count": 0}
    api_key = _api_key()
    if not api_key:
        return {**plan, "status": "blocked", "reason_code": "TIKHUB_API_KEY_MISSING", "api_call_count": 0}
    reservation_id = str(task_failover_id or f"xhs-detail:{uuid.uuid4().hex}")
    reservation = reserve_tikhub_daily_limit("detail", reservation_id=reservation_id)
    if not reservation.get("reserved"):
        return {
            **plan,
            "status": "blocked",
            "reason_code": reservation.get("reason_code") or "TIKHUB_DAILY_DETAIL_LIMIT_REACHED",
            "api_call_count": 0,
            "usage": reservation.get("summary", {}),
        }
    params = {"note_id": str(note_id or "")}
    if share_text:
        params["share_text"] = share_text
    elif note_id:
        share = f"https://www.xiaohongshu.com/explore/{note_id}"
        if xsec_token:
            share += f"?xsec_token={xsec_token}&xsec_source={xsec_source or 'pc_search'}"
        params["share_text"] = share
    url = _base_url().rstrip("/") + TIKHUB_XHS_APP_V2_DETAIL_ENDPOINT
    started = time.perf_counter()
    try:
        with httpx.Client(timeout=timeout_s, follow_redirects=True) as client:
            response = client.get(
                url,
                params=params,
                headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
            )
        elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
        try:
            payload = response.json()
        except Exception:
            payload = {}
        if not (200 <= response.status_code < 300):
            usage = record_tikhub_reservation_outcome(reservation_id, status="degraded", reason_code=f"HTTP_{response.status_code}", billed=True)
            return {
                **plan,
                "status": "degraded",
                "reason_code": f"HTTP_{response.status_code}",
                "http_status": response.status_code,
                "elapsed_ms": elapsed_ms,
                "api_call_count": 1,
                "response_keys": list(payload.keys())[:30] if isinstance(payload, dict) else [],
                "secret_exposed": False,
                "usage": usage, "reservation_id": reservation_id,
            }
        note_data = normalize_tikhub_xhs_detail_response(payload if isinstance(payload, dict) else {}, note_id=note_id)
        status = "ok" if note_data else "empty"
        reason = "OK_PAID_DETAIL" if note_data else "EMPTY_PAID_DETAIL"
        usage = record_tikhub_reservation_outcome(reservation_id, status=status, reason_code=reason, billed=True)
        return {
            **plan,
            "status": status,
            "reason_code": reason,
            "http_status": response.status_code,
            "elapsed_ms": elapsed_ms,
            "api_call_count": 1,
            "noteData": note_data,
            "raw_response_stored": False,
            "secret_exposed": False,
            "usage": usage, "reservation_id": reservation_id,
        }
    except Exception as exc:
        usage = record_tikhub_reservation_outcome(reservation_id, status="failed", reason_code=exc.__class__.__name__, billed=None)
        return {
            **plan,
            "status": "failed",
            "reason_code": exc.__class__.__name__,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
            "api_call_count": 1,
            "error": str(exc)[:300],
            "secret_exposed": False,
            "usage": usage, "reservation_id": reservation_id,
        }


def _base_url() -> str:
    return os.environ.get("TIKHUB_BASE_URL", "").strip() or "https://api.tikhub.dev"


def _api_key() -> str:
    return os.environ.get("TIKHUB_API_KEY", "").strip()


def _api_key_configured() -> bool:
    return bool(_api_key())


def _break_glass_enabled() -> bool:
    return os.environ.get(BREAK_GLASS_ENV, "1").strip().lower() in {"1", "true", "yes", "on"}


def _break_glass_dry_run() -> bool:
    return os.environ.get(BREAK_GLASS_DRY_RUN_ENV, "0").strip().lower() not in {"0", "false", "no", "off"}


def _break_glass_daily_budget() -> float:
    try:
        return max(0.0, float(os.environ.get(BREAK_GLASS_DAILY_BUDGET_ENV, "0.20") or 0.20))
    except Exception:
        return 0.20


def _break_glass_max_calls() -> int:
    try:
        return max(0, min(int(os.environ.get(BREAK_GLASS_MAX_CALLS_ENV, "1") or 1), 1))
    except Exception:
        return 1


def _browser_pool_unavailable(browser_availability: Dict[str, Any], route_scoring: Dict[str, Any]) -> bool:
    try:
        healthy_count = int(browser_availability.get("healthy_count") or 0)
    except Exception:
        healthy_count = 0
    if healthy_count > 0:
        return False
    top = route_scoring.get("top_recommendation") or {}
    if str(top.get("browser_base") or "") == f"chrome_{XHS_CHROME_DEBUG_PORT}" and str(top.get("channel_id") or "") == f"chrome_{XHS_CHROME_DEBUG_PORT}_scrapling_cdp":
        if str(top.get("recommendation") or "") == "best_observed_candidate":
            return False
    return True
