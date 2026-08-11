"""Read-only Xiaohongshu stability observation planner.

This module does not launch browsers, switch profiles, call APIs, or perform
station search. It turns the current matrix registry plus route-event ledger
into a compact P3 readiness summary.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List


SEARCH_CAPABILITIES = {
    "search_page_candidates",
    "search_page_candidates_strict",
    "login_persistence_explore_candidates",
}

DETAIL_CAPABILITIES = {"single_url_detail", "detail_probe", "detail_extract"}
API_CAPABILITIES = {"api_candidate_research", "api_search", "api_detail"}


def xhs_stability_observer_summary(
    *,
    profile_registry: Dict[str, Any],
    route_matrix: Dict[str, Any],
    channel_admission: Dict[str, Any],
    candidate_admission: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Return P3 stability-readiness summary with no side effects."""
    profiles = [row for row in profile_registry.get("profiles", []) or [] if isinstance(row, dict)]
    recent_events = [row for row in route_matrix.get("recent_events", []) or [] if isinstance(row, dict)]
    admission_by_profile = {
        str(row.get("profile_id") or ""): row
        for row in channel_admission.get("channels", []) or []
        if isinstance(row, dict)
    }
    browser_rows = _browser_observation_rows(profiles, recent_events, admission_by_profile)
    api_rows = _api_observation_rows(recent_events)
    blockers = _blockers(browser_rows, api_rows)
    candidate_pool = (candidate_admission or {}).get("pool") or {}
    return {
        "schema": "knowledgeradar-xhs-stability-observer/v1",
        "status": "ready_for_readonly_observation" if browser_rows or api_rows else "not_ready",
        "side_effects": {
            "browser_launch": False,
            "station_search": False,
            "account_switch": False,
            "api_call": False,
            "registry_write": False,
        },
        "observation_window": {
            "recommended_days": "7-14",
            "readonly_patrol": "allowed",
            "detail_probe": "manual_confirm",
            "search_canary": "autonomous_limit_1_threshold_gate",
            "api_minimal_probe": "manual_confirm_key_required",
        },
        "candidate_admission": {
            "status": (candidate_admission or {}).get("status", "not_configured"),
            "admitted_count": len(candidate_pool.get("admitted", []) or []),
            "pending_count": len(candidate_pool.get("pending", []) or []),
            "thresholds": (candidate_admission or {}).get("thresholds", {}),
        },
        "browser_candidates": browser_rows,
        "api_candidates": api_rows,
        "counts": {
            "browser_candidates": len(browser_rows),
            "browser_ready_for_readonly_patrol": sum(1 for row in browser_rows if row.get("readonly_patrol_ready")),
            "api_candidates": len(api_rows),
            "api_ready_for_keyed_probe": sum(1 for row in api_rows if row.get("minimal_probe_ready")),
            "main_chain_allowed": sum(1 for row in profiles if bool(row.get("main_chain_allowed", False))),
        },
        "route_scoring": {
            "status": "candidate_threshold_gate_enabled",
            "reason": "Candidates promote only after metric thresholds and two limit=1 canary passes.",
        },
        "blockers": blockers,
        "notes": [
            "P3 summary is a plan/readiness layer only.",
            "Search canaries are low-frequency limit=1 evidence for threshold admission.",
            "API candidates require user-provided key or trial authorization before any live request.",
        ],
    }


def _browser_observation_rows(
    profiles: Iterable[Dict[str, Any]],
    recent_events: List[Dict[str, Any]],
    admission_by_profile: Dict[str, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for profile in profiles:
        platform = str(profile.get("platform") or "")
        if platform != "xiaohongshu":
            continue
        profile_id = str(profile.get("profile_id") or "")
        status = str(profile.get("status") or "")
        channel_id = str(profile.get("channel_id") or "")
        browser_base = str(profile.get("browser_base") or "")
        matching_events = [event for event in recent_events if str(event.get("profile_id") or "") == profile_id]
        ok_capabilities = sorted(
            {
                str(event.get("capability") or "")
                for event in matching_events
                if str(event.get("result") or "") == "ok"
            }
        )
        has_login = "login_persistence_ok" in status or any("login" in cap for cap in ok_capabilities)
        has_search_page = _has_capability(ok_capabilities, SEARCH_CAPABILITIES) or "search_page_minimum_ok" in status
        admission = admission_by_profile.get(profile_id, {})
        rows.append(
            {
                "profile_id": profile_id,
                "account_slot": str(profile.get("account_slot") or ""),
                "browser_base": browser_base,
                "channel_id": channel_id,
                "role": str(profile.get("role") or ""),
                "readonly_patrol_ready": bool(has_login),
                "search_page_observed": bool(has_search_page),
                "detail_quality": "weak" if "body_text_weak" in status else "unknown",
                "main_chain_allowed": bool(profile.get("main_chain_allowed", False)),
                "admission_gate": str(admission.get("admission_gate") or ""),
                "next_allowed_step": _next_browser_step(status, ok_capabilities),
                "ok_capabilities": ok_capabilities,
            }
        )
    return rows


def _api_observation_rows(recent_events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for event in recent_events:
        if str(event.get("capability") or "") not in API_CAPABILITIES:
            continue
        profile_id = str(event.get("profile_id") or "")
        if not profile_id or profile_id in seen:
            continue
        seen.add(profile_id)
        result = str(event.get("result") or "")
        reason = str(event.get("reason_code") or "")
        rows.append(
            {
                "profile_id": profile_id,
                "channel_id": str(event.get("channel_id") or ""),
                "documentation_status": result,
                "reason_code": reason,
                "minimal_probe_ready": result == "ok",
                "next_allowed_step": "manual_confirm_api_key_minimal_probe" if result == "ok" else "watch_or_research_more",
            }
        )
    return rows


def _blockers(browser_rows: List[Dict[str, Any]], api_rows: List[Dict[str, Any]]) -> List[str]:
    blockers: List[str] = []
    if not any(row.get("search_page_observed") for row in browser_rows):
        blockers.append("no browser candidate has search-page observation")
    if not any(row.get("main_chain_allowed") for row in browser_rows):
        blockers.append("no profile has main_chain_allowed=true")
    if any(row.get("next_allowed_step") == "autonomous_limit1_canary_threshold_gate" for row in browser_rows):
        blockers.append("candidate admission waits for two successful limit=1 canaries")
    if any(row.get("minimal_probe_ready") for row in api_rows):
        blockers.append("API minimal probes require user key/trial authorization")
    return blockers


def _has_capability(caps: Iterable[str], expected: set[str]) -> bool:
    return bool(set(caps) & expected)


def _next_browser_step(status: str, ok_capabilities: List[str]) -> str:
    if "camoufox_detail_context_required" in status:
        return "manual_confirm_contextual_detail_probe_with_xsec_token"
    if "body_text_weak" in status:
        return "manual_confirm_single_url_detail_quality_probe"
    if _has_capability(ok_capabilities, SEARCH_CAPABILITIES):
        return "autonomous_limit1_canary_threshold_gate"
    if "login_persistence_ok" in status:
        return "readonly_patrol_continue"
    return "login_or_profile_recovery_required"
