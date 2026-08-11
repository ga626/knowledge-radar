"""Threshold-gated Xiaohongshu autonomous candidate admission.

This module is intentionally read-only. It turns existing route-event evidence
into a candidate fallback pool decision, but never launches browsers, switches
accounts, performs station search, or mutates the profile registry.
"""

from __future__ import annotations

import math
from typing import Any, Dict, Iterable, List, Tuple


DEFAULT_THRESHOLDS = {
    "success_rate_min": 0.70,
    "parse_failed_rate_max": 0.15,
    "p95_latency_s_max": 10.0,
    "min_limit1_canary_passes": 2,
}

PRODUCTION_CANDIDATE_CHANNELS = {
    "chrome_12733_playwright_cdp_attach": {
        "family": "playwright_search_page",
        "route_role": "search_page_fallback_candidate",
    },
    "playwright_chromium_isolated_probe": {
        "family": "playwright_search_page",
        "route_role": "search_page_fallback_candidate",
    },
    "camoufox_sdk_persistent_context": {
        "family": "camoufox",
        "route_role": "browser_fallback_candidate",
    },
    "camoufox_v2_dom_probe": {
        "family": "camoufox",
        "route_role": "browser_fallback_candidate",
    },
}

SEARCH_CANARY_CAPABILITIES = {
    "search_canary_limit_1",
    "search_page_candidates",
    "search_page_candidates_strict",
    "search_page_probe",
}

PARSE_FAILED_REASONS = {
    "parse_failed",
    "selector_parse_failed",
    "empty_after_parse",
    "no_clickable_candidates",
    "candidate_parse_failed",
}

ANTI_BOT_REASONS = {
    "anti_bot_blocked",
    "captcha_required",
    "platform_verification_required",
    "app_scan_required",
    "http_403",
    "http_429",
}


def xhs_autonomous_candidate_admission_summary(
    *,
    profile_registry: Dict[str, Any],
    route_matrix: Dict[str, Any],
    thresholds: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Return candidate-pool admission decisions from evidence only."""
    effective_thresholds = _thresholds(profile_registry, thresholds)
    profiles = _candidate_profiles(profile_registry)
    events = [row for row in route_matrix.get("recent_events", []) or [] if isinstance(row, dict)]
    rows = [_score_candidate(profile, events, effective_thresholds) for profile in profiles]
    static_rows = _static_candidates(events, effective_thresholds, rows)
    all_rows = rows + static_rows
    admitted = [row for row in all_rows if row.get("admission") == "admitted_to_autonomous_candidate_pool"]
    pending = [row for row in all_rows if row.get("admission") != "admitted_to_autonomous_candidate_pool"]
    return {
        "schema": "knowledgeradar-xhs-autonomous-candidate-admission/v1",
        "status": "ok",
        "mode": "threshold_gated_readonly",
        "thresholds": effective_thresholds,
        "production_scope": {
            "main_chain": "unchanged",
            "candidate_fallback_pool": "enabled_when_thresholds_pass",
            "allowed_channel_families": sorted({item["family"] for item in PRODUCTION_CANDIDATE_CHANNELS.values()}),
        },
        "side_effects": {
            "browser_launch": False,
            "station_search": False,
            "account_switch": False,
            "registry_write": False,
            "api_call": False,
        },
        "pool": {
            "admitted_count": len(admitted),
            "pending_count": len(pending),
            "admitted": admitted,
            "pending": pending,
        },
        "notes": [
            "Candidates enter production fallback only after the metric gate passes.",
            "The gate requires at least two successful limit=1 canaries.",
            "Main-chain admission remains separate from candidate fallback admission.",
        ],
    }


def candidate_admission_by_key(candidate_admission: Dict[str, Any] | None) -> Dict[str, Dict[str, Any]]:
    rows: Dict[str, Dict[str, Any]] = {}
    if not isinstance(candidate_admission, dict):
        return rows
    pool = candidate_admission.get("pool") or {}
    for group in ("admitted", "pending"):
        for row in pool.get(group, []) or []:
            if not isinstance(row, dict):
                continue
            for key_name in ("profile_id", "channel_id"):
                key = str(row.get(key_name) or "")
                if key:
                    rows[key] = row
    return rows


def _thresholds(profile_registry: Dict[str, Any], explicit: Dict[str, Any] | None) -> Dict[str, Any]:
    merged = dict(DEFAULT_THRESHOLDS)
    policy = profile_registry.get("policy") if isinstance(profile_registry, dict) else {}
    if isinstance(policy, dict):
        configured = policy.get("xhs_autonomous_candidate_admission") or policy.get("candidate_channel_admission")
        if isinstance(configured, dict):
            merged.update({key: configured[key] for key in merged if key in configured})
    if explicit:
        merged.update({key: explicit[key] for key in merged if key in explicit})
    merged["success_rate_min"] = _float(merged.get("success_rate_min"), DEFAULT_THRESHOLDS["success_rate_min"])
    merged["parse_failed_rate_max"] = _float(merged.get("parse_failed_rate_max"), DEFAULT_THRESHOLDS["parse_failed_rate_max"])
    merged["p95_latency_s_max"] = _float(merged.get("p95_latency_s_max"), DEFAULT_THRESHOLDS["p95_latency_s_max"])
    merged["min_limit1_canary_passes"] = int(_float(merged.get("min_limit1_canary_passes"), DEFAULT_THRESHOLDS["min_limit1_canary_passes"]))
    return merged


def _candidate_profiles(profile_registry: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for profile in profile_registry.get("profiles", []) or []:
        if not isinstance(profile, dict) or str(profile.get("platform") or "") != "xiaohongshu":
            continue
        channel_id = str(profile.get("channel_id") or "")
        browser_base = str(profile.get("browser_base") or "").lower()
        if channel_id in PRODUCTION_CANDIDATE_CHANNELS or "playwright" in browser_base or "camoufox" in browser_base:
            rows.append(profile)
    return rows


def _static_candidates(
    events: List[Dict[str, Any]],
    thresholds: Dict[str, Any],
    registry_rows: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    registry_keys = {str(row.get("profile_id") or "") for row in registry_rows} | {str(row.get("channel_id") or "") for row in registry_rows}
    static = [
        {
            "profile_id": "xhs-playwright-chromium-p7",
            "account_slot": "",
            "browser_base": "playwright_chromium",
            "channel_id": "playwright_chromium_isolated_probe",
            "role": "static_candidate",
            "launch_policy": "safe_auto_candidate_after_admission",
        },
        {
            "profile_id": "xhs-camoufox-v2",
            "account_slot": "",
            "browser_base": "camoufox_v2",
            "channel_id": "camoufox_v2_dom_probe",
            "role": "static_candidate",
            "launch_policy": "safe_auto_candidate_after_admission",
        },
    ]
    rows: List[Dict[str, Any]] = []
    for row in static:
        if row["profile_id"] in registry_keys or row["channel_id"] in registry_keys:
            continue
        rows.append(_score_candidate(row, events, thresholds))
    return rows


def _score_candidate(profile: Dict[str, Any], events: List[Dict[str, Any]], thresholds: Dict[str, Any]) -> Dict[str, Any]:
    profile_id = str(profile.get("profile_id") or "")
    channel_id = str(profile.get("channel_id") or "")
    matching = [_event for _event in events if _matches_candidate(_event, profile_id, channel_id)]
    relevant = [_event for _event in matching if _is_search_canary_related(_event)]
    if not relevant and matching:
        relevant = matching
    metric_events, recovery_note = _metric_events_after_manual_recovery(relevant, thresholds)

    ok_events = [event for event in metric_events if str(event.get("result") or "") == "ok"]
    parse_failed = [event for event in metric_events if _is_parse_failed(event)]
    anti_bot_events = [event for event in metric_events if _is_anti_bot_blocked(event)]
    latencies = [_latency_s(event) for event in metric_events if _latency_s(event) > 0]
    canary_passes = [event for event in metric_events if _is_limit1_canary_pass(event)]

    total = len(metric_events)
    success_rate = len(ok_events) / total if total else 0.0
    parse_failed_rate = len(parse_failed) / total if total else 0.0
    p95_latency_s = _percentile95(latencies) if latencies else None
    blockers = _blockers(
        total=total,
        success_rate=success_rate,
        parse_failed_rate=parse_failed_rate,
        p95_latency_s=p95_latency_s,
        canary_pass_count=len(canary_passes),
        anti_bot_count=len(anti_bot_events),
        thresholds=thresholds,
        events=relevant,
        metric_events=metric_events,
    )
    admitted = not blockers
    channel_meta = PRODUCTION_CANDIDATE_CHANNELS.get(channel_id) or {}
    return {
        "profile_id": profile_id,
        "account_slot": str(profile.get("account_slot") or ""),
        "browser_base": str(profile.get("browser_base") or ""),
        "channel_id": channel_id,
        "channel_family": channel_meta.get("family", _family_from_profile(profile)),
        "route_role": channel_meta.get("route_role", "browser_fallback_candidate"),
        "launch_policy": str(profile.get("launch_policy") or "safe_auto_candidate_after_admission"),
        "main_chain_allowed": False,
        "admission": "admitted_to_autonomous_candidate_pool" if admitted else "shadow_pending_threshold_gate",
        "candidate_fallback_allowed": admitted,
        "metrics": {
            "event_count": total,
            "raw_event_count": len(relevant),
            "success_rate": round(success_rate, 4),
            "parse_failed_rate": round(parse_failed_rate, 4),
            "anti_bot_event_count": len(anti_bot_events),
            "p95_latency_s": round(p95_latency_s, 4) if p95_latency_s is not None else None,
            "limit1_canary_passes": len(canary_passes),
            "recovery_window": recovery_note,
        },
        "blockers": blockers,
        "next_action": "production_candidate_pool" if admitted else _next_action(blockers),
    }


def _matches_candidate(event: Dict[str, Any], profile_id: str, channel_id: str) -> bool:
    event_profile = str(event.get("profile_id") or "")
    event_channel = str(event.get("channel_id") or "")
    if event_profile:
        return bool(profile_id and event_profile == profile_id)
    if profile_id and event_profile == profile_id:
        return True
    if channel_id and event_channel == channel_id:
        return True
    return False


def _is_search_canary_related(event: Dict[str, Any]) -> bool:
    capability = str(event.get("capability") or "")
    metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
    if capability in SEARCH_CANARY_CAPABILITIES:
        return True
    return str(metadata.get("limit") or metadata.get("canary_limit") or "") == "1"


def _is_limit1_canary_pass(event: Dict[str, Any]) -> bool:
    if str(event.get("result") or "") != "ok":
        return False
    metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
    capability = str(event.get("capability") or "")
    return capability == "search_canary_limit_1" or str(metadata.get("limit") or metadata.get("canary_limit") or "") == "1"


def _is_parse_failed(event: Dict[str, Any]) -> bool:
    reason = str(event.get("reason_code") or "").lower()
    result = str(event.get("result") or "").lower()
    metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
    if reason in PARSE_FAILED_REASONS or result in PARSE_FAILED_REASONS:
        return True
    return str(metadata.get("parse_failed") or "").lower() in {"1", "true", "yes"}


def _is_anti_bot_blocked(event: Dict[str, Any]) -> bool:
    reason = str(event.get("reason_code") or "").lower()
    metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
    if reason in ANTI_BOT_REASONS:
        return True
    network_block_statuses = str(metadata.get("network_block_statuses") or "")
    return any(status in {"403", "429", "461", "471"} for status in network_block_statuses.split(","))


def _blockers(
    *,
    total: int,
    success_rate: float,
    parse_failed_rate: float,
    p95_latency_s: float | None,
    canary_pass_count: int,
    anti_bot_count: int,
    thresholds: Dict[str, Any],
    events: List[Dict[str, Any]],
    metric_events: List[Dict[str, Any]],
) -> List[str]:
    blockers: List[str] = []
    if total <= 0:
        blockers.append("no_route_events")
    if canary_pass_count < int(thresholds["min_limit1_canary_passes"]):
        blockers.append("limit1_canary_passes_below_threshold")
    if success_rate < float(thresholds["success_rate_min"]):
        blockers.append("success_rate_below_threshold")
    if parse_failed_rate > float(thresholds["parse_failed_rate_max"]):
        blockers.append("parse_failed_rate_above_threshold")
    if anti_bot_count > 0:
        blockers.append("anti_bot_blocked_event_present")
    if p95_latency_s is None:
        blockers.append("p95_latency_missing")
    elif p95_latency_s > float(thresholds["p95_latency_s_max"]):
        blockers.append("p95_latency_above_threshold")
    if _has_unresolved_manual_action(events, metric_events):
        blockers.append("manual_action_required_event_present")
    return blockers


def _metric_events_after_manual_recovery(
    events: List[Dict[str, Any]],
    thresholds: Dict[str, Any],
) -> Tuple[List[Dict[str, Any]], str]:
    """Use the post-login recovery window once enough fresh canaries pass.

    A candidate should not be permanently blocked by a historical
    login_required/manual-action event after the user has completed login and
    fresh canaries prove the route is healthy again.
    """
    last_manual_index = -1
    for index, event in enumerate(events):
        if event.get("manual_action_required"):
            last_manual_index = index
    if last_manual_index < 0:
        return events, "full_window"

    post_recovery = events[last_manual_index + 1 :]
    min_passes = int(thresholds["min_limit1_canary_passes"])
    post_canary_passes = [event for event in post_recovery if _is_limit1_canary_pass(event)]
    if len(post_canary_passes) >= min_passes:
        return post_recovery, "post_manual_recovery"
    return events, "manual_action_unresolved"


def _has_unresolved_manual_action(
    events: List[Dict[str, Any]],
    metric_events: List[Dict[str, Any]],
) -> bool:
    if not any(event.get("manual_action_required") for event in events):
        return False
    if metric_events is events:
        return True
    if not metric_events:
        return True
    return metric_events[0] in events and events.index(metric_events[0]) <= max(
        index for index, event in enumerate(events) if event.get("manual_action_required")
    )


def _next_action(blockers: List[str]) -> str:
    if "anti_bot_blocked_event_present" in blockers:
        return "repair_antibot_signature_or_fingerprint_before_admission"
    if "no_route_events" in blockers or "limit1_canary_passes_below_threshold" in blockers:
        return "run_two_limit1_canaries_before_admission"
    if "p95_latency_above_threshold" in blockers:
        return "reduce_latency_or_timeout_before_admission"
    if "parse_failed_rate_above_threshold" in blockers:
        return "repair_parser_before_admission"
    if "success_rate_below_threshold" in blockers:
        return "observe_more_successful_canaries_before_admission"
    if "manual_action_required_event_present" in blockers:
        return "resolve_manual_action_before_admission"
    return "observe_more_evidence"


def _percentile95(values: List[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, math.ceil(len(ordered) * 0.95) - 1)
    return ordered[index]


def _latency_s(event: Dict[str, Any]) -> float:
    value = event.get("latency_ms")
    if value in (None, ""):
        return 0.0
    return _float(value, 0.0) / 1000.0


def _family_from_profile(profile: Dict[str, Any]) -> str:
    text = " ".join([str(profile.get("browser_base") or ""), str(profile.get("channel_id") or "")]).lower()
    if "camoufox" in text:
        return "camoufox"
    if "playwright" in text:
        return "playwright_search_page"
    return "unknown"


def _float(value: Any, default: float) -> float:
    try:
        return float(value)
    except Exception:
        return default
