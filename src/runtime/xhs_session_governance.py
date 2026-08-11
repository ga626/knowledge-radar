"""Read-only Xiaohongshu session governance helpers."""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict

from .chrome_manager import XHS_CHROME_DEBUG_PORT


def xhs_session_governance_summary(profile_registry: Dict[str, Any]) -> Dict[str, Any]:
    """Summarize account/browser-base binding risks without side effects."""
    profiles = [
        row
        for row in (profile_registry.get("profiles") or [])
        if isinstance(row, dict) and str(row.get("platform") or "") == "xiaohongshu"
    ]
    by_slot: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in profiles:
        slot = str(row.get("account_slot") or "")
        if slot:
            by_slot[slot].append(row)

    rows = []
    multi_base_slots = []
    for slot, slot_profiles in sorted(by_slot.items()):
        browser_bases = sorted({str(row.get("browser_base") or "") for row in slot_profiles if row.get("browser_base")})
        production_like = [
            row
            for row in slot_profiles
            if str(row.get("channel_id") or "") in {f"chrome_{XHS_CHROME_DEBUG_PORT}_scrapling_cdp", f"chrome_{XHS_CHROME_DEBUG_PORT}_playwright_cdp_attach"}
        ]
        camoufox = [row for row in slot_profiles if "camoufox" in str(row.get("browser_base") or "").lower()]
        risk = "medium" if len(browser_bases) > 1 and production_like and camoufox else "low"
        if risk != "low":
            multi_base_slots.append(slot)
        rows.append(
            {
                "account_slot": slot,
                "browser_bases": browser_bases,
                "profile_ids": [str(row.get("profile_id") or "") for row in slot_profiles],
                "production_search_bases": sorted({str(row.get("browser_base") or "") for row in production_like}),
                "diagnostic_or_candidate_bases": sorted({str(row.get("browser_base") or "") for row in camoufox}),
                "session_collision_risk": risk,
                "recommendation": (
                    "Keep one production browser base per account; use other bases for diagnostics only or assign a separate account."
                    if risk != "low"
                    else "No multi-base session collision risk detected for this account."
                ),
            }
        )

    return {
        "schema": "knowledgeradar-xhs-session-governance/v1",
        "status": "recommendation_only" if multi_base_slots else "ok",
        "multi_base_slots": multi_base_slots,
        "accounts": rows,
        "policy": {
            "normal_search_uses": f"healthy Chrome {XHS_CHROME_DEBUG_PORT} account pool only",
            "camoufox_role": "diagnostic_or_detail_candidate_until_assigned_dedicated_account",
            "search_canary_requires": "login_preflight_ok_and_manual_probe",
            "main_chain_allowed_changed": False,
        },
        "notes": [
            "Cookie expiry is advisory; server-side login state is the admission source.",
            "Avoid using the same account as production login on multiple browser bases.",
        ],
    }
