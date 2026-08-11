"""Read-only Xiaohongshu governance summaries for P6-P9.

This module deliberately does not launch browsers, switch accounts, perform
searches, or call external APIs. It only makes already-known governance state
visible in one compact place.
"""

from __future__ import annotations

from typing import Any, Dict, List

from .xhs_candidate_admission import candidate_admission_by_key


def xhs_p6_p9_governance_summary(
    *,
    profile_registry: Dict[str, Any],
    browser_channels: Dict[str, Any],
    channel_admission: Dict[str, Any],
    account_pool: Dict[str, Any],
    candidate_admission: Dict[str, Any] | None = None,
    web_providers: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Return the P6-P9 governance state without side effects."""
    web_providers = web_providers or {}
    return {
        "schema": "knowledgeradar-xhs-p6-p9-governance/v1",
        "status": "ok",
        "side_effects": {
            "browser_launch": False,
            "station_search": False,
            "account_switch": False,
            "api_call": False,
        },
        "p6_candidate_channels": _candidate_channels(browser_channels, channel_admission, candidate_admission),
        "p7_account_risk_state_machine": _account_risk_state(account_pool),
        "p8_external_discovery_and_api": _external_discovery_and_api(web_providers),
        "p9_light_patrol": _light_patrol(profile_registry, account_pool, channel_admission),
        "notes": [
            "P6-P9 summary is read-only and safe for health_check(mode='summary').",
            "C account low-frequency search probe is not part of this summary and must be explicit.",
        ],
    }


def _candidate_channels(
    browser_channels: Dict[str, Any],
    channel_admission: Dict[str, Any],
    candidate_admission: Dict[str, Any] | None,
) -> Dict[str, Any]:
    channels = list(browser_channels.get("channels") or [])
    candidate_by_key = candidate_admission_by_key(candidate_admission)
    admission_by_profile = {
        str(row.get("profile_id") or ""): row
        for row in channel_admission.get("channels", []) or []
        if isinstance(row, dict)
    }
    rows: List[Dict[str, Any]] = []
    for channel in channels:
        if not isinstance(channel, dict):
            continue
        profile_id = str(channel.get("profile_id") or "")
        channel_id = str(channel.get("channel_id") or "")
        admission = admission_by_profile.get(profile_id, {})
        candidate_gate = candidate_by_key.get(profile_id) or candidate_by_key.get(channel_id) or {}
        rows.append(
            {
                "profile_id": profile_id,
                "channel_id": channel_id,
                "browser_base": str(channel.get("browser_base") or ""),
                "automation": str(channel.get("automation") or ""),
                "launch_policy": str(channel.get("launch_policy") or ""),
                "gate": str(admission.get("admission_gate") or "candidate_observe"),
                "main_chain_admission": str(admission.get("main_chain_admission") or "denied"),
                "main_chain_allowed": bool(channel.get("main_chain_allowed", False)),
                "autonomous_candidate_admission": str(candidate_gate.get("admission") or "not_candidate"),
                "candidate_fallback_allowed": bool(candidate_gate.get("candidate_fallback_allowed", False)),
                "candidate_gate_metrics": candidate_gate.get("metrics", {}),
                "candidate_gate_blockers": candidate_gate.get("blockers", []),
            }
        )
    static_candidates = [
        {
            "profile_id": "xhs-playwright-chromium-p7",
            "channel_id": "playwright_chromium_isolated_probe",
            "browser_base": "playwright_chromium",
            "automation": "playwright_dom",
            "launch_policy": "safe_auto_candidate_after_admission",
            "gate": "threshold_gated_candidate_pool",
            "main_chain_admission": "denied",
            "main_chain_allowed": False,
        },
        {
            "profile_id": "xhs-camoufox-v2",
            "channel_id": "camoufox_v2_dom_probe",
            "browser_base": "camoufox_v2",
            "automation": "playwright_dom",
            "launch_policy": "safe_auto_candidate_after_admission",
            "gate": "threshold_gated_candidate_pool",
            "main_chain_admission": "denied",
            "main_chain_allowed": False,
        },
        {
            "profile_id": "xhs-bridge-fallback",
            "channel_id": "bridge_fallback",
            "browser_base": "node_bridge",
            "automation": "diagnostic_bridge",
            "launch_policy": "breaker_controlled",
            "gate": "diagnostic_only",
            "main_chain_admission": "denied",
            "main_chain_allowed": False,
        },
    ]
    static_candidates = [_with_candidate_gate(row, candidate_by_key) for row in static_candidates]
    admitted_count = sum(1 for row in rows + static_candidates if row.get("candidate_fallback_allowed"))
    return {
        "schema": "xhs-candidate-channel-matrix/v1",
        "status": "ok",
        "production_routing": "candidate_fallback_pool_threshold_gated",
        "auto_admission": "enabled_for_candidate_pool_only",
        "registry_channels": rows,
        "static_candidates": static_candidates,
        "counts": {
            "registry_channels": len(rows),
            "static_candidates": len(static_candidates),
            "main_chain_allowed": sum(1 for row in rows if row.get("main_chain_allowed")),
            "candidate_fallback_allowed": admitted_count,
        },
    }


def _with_candidate_gate(row: Dict[str, Any], candidate_by_key: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    candidate_gate = candidate_by_key.get(str(row.get("profile_id") or "")) or candidate_by_key.get(str(row.get("channel_id") or "")) or {}
    return {
        **row,
        "autonomous_candidate_admission": str(candidate_gate.get("admission") or "shadow_pending_threshold_gate"),
        "candidate_fallback_allowed": bool(candidate_gate.get("candidate_fallback_allowed", False)),
        "candidate_gate_metrics": candidate_gate.get("metrics", {}),
        "candidate_gate_blockers": candidate_gate.get("blockers", []),
    }


def _account_risk_state(account_pool: Dict[str, Any]) -> Dict[str, Any]:
    accounts = list(account_pool.get("accounts") or [])
    return {
        "schema": "xhs-account-risk-state-machine-summary/v1",
        "status": "ok",
        "counts": account_pool.get("counts", {}),
        "ordered_accounts": [
            {
                "account_slot": row.get("account_slot", ""),
                "profile_id": row.get("profile_id", ""),
                "runtime_state": row.get("runtime_state", ""),
                "cooldown_active": bool(row.get("cooldown_active", False)),
                "manual_action_required": bool(row.get("manual_action_required", False)),
                "risk_score": (row.get("risk") or {}).get("risk_score"),
                "recommended": bool((row.get("risk") or {}).get("recommended", False)),
            }
            for row in accounts
            if isinstance(row, dict)
        ],
        "policy": account_pool.get("policy", {}),
        "recommendations": {
            purpose: {
                "recommended_profile_id": (account_pool.get("recommendations") or {}).get(purpose, {}).get("recommended_profile_id", ""),
                "requires_manual_confirm": bool((account_pool.get("recommendations") or {}).get(purpose, {}).get("requires_manual_confirm", False)),
                "switch_reason": ((account_pool.get("recommendations") or {}).get(purpose, {}).get("switch_decision") or {}).get("reason", ""),
            }
            for purpose in ["diagnostic", "patrol", "search", "detail", "main_chain"]
        },
    }


def _external_discovery_and_api(web_providers: Dict[str, Any]) -> Dict[str, Any]:
    provider_names = ["tavily", "brave", "exa", "searxng"]
    return {
        "schema": "xhs-external-discovery-api-summary/v1",
        "status": "ok",
        "external_search_then_detail": {
            "role": "fallback_candidate_when_station_search_blocked",
            "default_main_chain": False,
            "queries": [
                "site:xiaohongshu.com/discovery/item <keyword>",
                "site:xiaohongshu.com/explore <keyword>",
            ],
            "providers": [
                {
                    "provider": name,
                    "configured": bool((web_providers.get(name) or {}).get("configured", False)),
                    "available": bool((web_providers.get(name) or {}).get("available", False)),
                    "status": (web_providers.get(name) or {}).get("status", "unknown"),
                }
                for name in provider_names
            ],
        },
        "api_candidates": [
            {
                "id": "tikhub",
                "role": "high-risk-platform-paid-fallback-candidate",
                "default_enabled": False,
                "requires_key": True,
                "known_positioning": "multi-platform API candidate; evaluate price, failed-call billing, fields, quota, compliance",
            },
            {
                "id": "apify_rednote",
                "role": "managed-actor-candidate",
                "default_enabled": False,
                "requires_key": True,
                "known_positioning": "managed scraping/API candidate; evaluate actor quality, proxy cost, compliance, schema",
            },
        ],
        "stable_platform_policy": {
            "bilibili": "prefer_existing_free_chain",
            "zhihu": "prefer_existing_chain_when_login_healthy",
            "youtube": "prefer_existing_youtube_provider",
        },
    }


def _light_patrol(profile_registry: Dict[str, Any], account_pool: Dict[str, Any], channel_admission: Dict[str, Any]) -> Dict[str, Any]:
    profiles = list(profile_registry.get("profiles") or [])
    risky = [
        row
        for row in profiles
        if isinstance(row, dict) and (row.get("manual_action_required") or row.get("cooldown_active"))
    ]
    return {
        "schema": "xhs-light-patrol-dry-run/v1",
        "status": "ok",
        "dry_run_only": True,
        "checks": [
            "profile_registry",
            "runtime_state",
            "account_pool_risk_order",
            "channel_admission",
            "policy_gate",
            "cooldown_and_manual_action",
        ],
        "forbidden_actions": ["browser_launch", "station_search", "account_switch", "api_call"],
        "profile_count": len(profiles),
        "cooldown_or_manual_profiles": [
            {
                "profile_id": row.get("profile_id", ""),
                "runtime_state": row.get("runtime_state", ""),
                "cooldown_active": bool(row.get("cooldown_active", False)),
                "manual_action_required": bool(row.get("manual_action_required", False)),
            }
            for row in risky
        ],
        "account_pool_counts": account_pool.get("counts", {}),
        "channel_admission_status": channel_admission.get("status", "unknown"),
    }
