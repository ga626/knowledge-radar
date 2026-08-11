"""Read-only Xiaohongshu route scoring.

The scorer recommends candidates from recorded evidence only. It never launches
browsers, calls APIs, switches accounts, or mutates production traffic. Candidate
fallback admission is surfaced only after the autonomous admission gate passes.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable


CAPABILITY_WEIGHTS = {
    "search_page_candidates": 20,
    "search_page_candidates_strict": 24,
    "login_persistence_explore_candidates": 18,
    "contextual_detail_probe": 24,
    "single_url_detail": 10,
    "search_page_probe": 8,
    "api_candidate_research": 5,
    "api_search_minimal_probe": 18,
    "search_canary_limit_1": 32,
}

RESULT_WEIGHTS = {"ok": 30, "degraded": -12, "blocked": -30, "failed": -24, "skipped": 0}


def xhs_route_scoring_summary(
    route_matrix: Dict[str, Any],
    candidate_admission: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    events = [row for row in route_matrix.get("recent_events", []) or [] if isinstance(row, dict)]
    scores = _score_events(events)
    ranked = sorted(scores.values(), key=lambda row: row["score"], reverse=True)
    pool = (candidate_admission or {}).get("pool") or {}
    admitted = [row for row in pool.get("admitted", []) or [] if isinstance(row, dict)]
    pending = [row for row in pool.get("pending", []) or [] if isinstance(row, dict)]
    return {
        "schema": "knowledgeradar-xhs-route-scoring/v1",
        "status": "candidate_pool_enabled" if admitted else "candidate_pool_waiting_for_thresholds",
        "mode": "threshold_gated_candidate_pool",
        "side_effects": {
            "account_switch": False,
            "browser_launch": False,
            "station_search": False,
            "api_call": False,
            "main_chain_admission": False,
        },
        "ranked_candidates": ranked,
        "top_recommendation": ranked[0] if ranked else {},
        "admission": {
            "main_chain_allowed": False,
            "safe_auto_search": "candidate_pool_allowed" if admitted else "denied_until_candidate_thresholds_pass",
            "safe_auto_detail": "denied",
            "requires_manual_confirm_for_canary_or_admission": False,
            "candidate_pool": {
                "admitted_count": len(admitted),
                "pending_count": len(pending),
                "thresholds": (candidate_admission or {}).get("thresholds", {}),
                "admitted": admitted,
            },
        },
        "notes": [
            "Scores are advisory and based on recent route events only.",
            "Candidate fallback requires success_rate>=0.70, parse_failed<=0.15, p95_latency<=10s, and two limit=1 canary passes.",
            "This module does not mutate profile_registry or launch fallback traffic.",
        ],
    }


def _score_events(events: Iterable[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    rows: Dict[str, Dict[str, Any]] = {}
    for event in events:
        key = str(event.get("profile_id") or event.get("channel_id") or "unknown")
        row = rows.setdefault(
            key,
            {
                "profile_id": str(event.get("profile_id") or ""),
                "account_slot": str(event.get("account_slot") or ""),
                "browser_base": str(event.get("browser_base") or ""),
                "channel_id": str(event.get("channel_id") or ""),
                "score": 50,
                "ok_events": 0,
                "degraded_events": 0,
                "manual_action_events": 0,
                "capabilities": [],
                "last_reason_code": "",
            },
        )
        capability = str(event.get("capability") or "")
        result = str(event.get("result") or "")
        row["score"] += CAPABILITY_WEIGHTS.get(capability, 4)
        row["score"] += RESULT_WEIGHTS.get(result, 0)
        if result == "ok":
            row["ok_events"] += 1
        elif result == "degraded":
            row["degraded_events"] += 1
        if event.get("manual_action_required"):
            row["manual_action_events"] += 1
            row["score"] -= 40
        if capability and capability not in row["capabilities"]:
            row["capabilities"].append(capability)
        row["last_reason_code"] = str(event.get("reason_code") or "")
    for row in rows.values():
        row["score"] = max(0, min(100, int(row["score"])))
        if row["manual_action_events"]:
            row["recommendation"] = "blocked_until_manual_action_resolved"
        elif row["score"] >= 80 and row["degraded_events"] == 0:
            row["recommendation"] = "best_observed_candidate"
        elif row["score"] >= 60:
            row["recommendation"] = "candidate_observe"
        else:
            row["recommendation"] = "hold_or_repair"
    return rows
