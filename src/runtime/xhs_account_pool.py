"""Xiaohongshu account-pool summary and purpose-aware selection."""

from __future__ import annotations

import time
from typing import Any, Dict, List

from .platform_diagnostics import stable_hash
from .profile_registry import profile_registry_internal
from .xhs_account_policy import purpose_mode_for, switch_policy_decision
from .xhs_account_risk import score_account_risk


DEFAULT_PURPOSES = ["diagnostic", "patrol", "detail", "search", "main_chain"]


def xhs_account_pool_summary(registry: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """Return v3 account-pool governance summary without launching browsers."""
    registry = registry or profile_registry_internal()
    raw = registry.get("raw") if isinstance(registry, dict) else {}
    policy = dict((raw or {}).get("policy") or registry.get("policy") or {})
    profiles = _xhs_profiles(list(registry.get("profiles", [])))
    accounts = _xhs_accounts(list((raw or {}).get("accounts") or _accounts_from_profiles(profiles)))
    bindings = _xhs_bindings(list((raw or {}).get("bindings") or _bindings_from_profiles(profiles)))
    state = registry.get("runtime_state") or {}
    state_by_profile = {
        str(row.get("profile_id") or ""): row
        for row in state.get("profiles", [])
        if isinstance(row, dict)
    }
    profile_by_slot = _profile_by_slot(profiles)

    account_rows: List[Dict[str, Any]] = []
    for account in accounts:
        if not isinstance(account, dict):
            continue
        slot = str(account.get("account_slot") or "")
        profile = profile_by_slot.get(slot, {})
        runtime = state_by_profile.get(str(profile.get("profile_id") or ""), {})
        risk = score_account_risk(
            account,
            profile=profile,
            runtime_state=runtime,
            events=runtime.get("recent_events", []),
        )
        account_rows.append(
            {
                "account_slot": slot,
                "platform": "xiaohongshu",
                "account_id_hash": _sanitize_hash(account),
                "status": str(account.get("status") or "unknown"),
                "profile_id": str(profile.get("profile_id") or ""),
                "channel_id": str(profile.get("channel_id") or ""),
                "runtime_state": runtime.get("state", "unobserved") if runtime else "unobserved",
                "manual_action_required": bool(runtime.get("manual_action_required", False)) if runtime else False,
                "cooldown_active": bool(runtime.get("cooldown_active", False)) if runtime else _cooldown_active(account),
                "risk": risk,
                "priority": int(account.get("priority") or 100),
                "capability_matrix": _compact_capability_matrix(account.get("capability_matrix")),
            }
        )
    account_rows.sort(key=lambda row: (row.get("priority", 100), row["risk"]["risk_score"], row.get("account_slot", "")))

    purpose_recommendations = {
        purpose: select_account(purpose, registry=registry, account_rows=account_rows)
        for purpose in DEFAULT_PURPOSES
    }
    auto_switch_mode = str(policy.get("default_mode") or policy.get("auto_switch_default") or "safe_auto")
    available_rows = [
        row
        for row in account_rows
        if not row.get("manual_action_required")
        and not row.get("cooldown_active")
        and str(row.get("runtime_state") or "") not in {"blocked", "locked"}
        and int((row.get("risk") or {}).get("risk_score") or 100) <= _max_auto_switch_risk(policy)
    ]
    return {
        "schema": "knowledgeradar-xhs-account-pool/v3",
        "status": "ok",
        "mode": auto_switch_mode,
        "auto_switch": auto_switch_mode,
        "policy": {
            "default_mode": auto_switch_mode,
            "max_switches_per_task": int(policy.get("max_switches_per_task") or 1),
            "safe_auto_allowed_purposes": list(policy.get("safe_auto_allowed_purposes") or []),
            "safe_auto_denied_purposes": list(policy.get("safe_auto_denied_purposes") or []),
            "manual_confirm_purposes": list(policy.get("manual_confirm_purposes") or []),
        },
        "counts": {
            "accounts": len(account_rows),
            "profiles": len(profiles),
            "bindings": len(bindings),
            "healthy": sum(1 for row in account_rows if row.get("runtime_state") in {"healthy", "available"} or row.get("status") == "healthy"),
            "cooldown": sum(1 for row in account_rows if row.get("cooldown_active")),
            "manual_action": sum(1 for row in account_rows if row.get("manual_action_required")),
        },
        "accounts": account_rows,
        "availability": {
            "observed_available_account_count": len(available_rows),
            "observed_available_profile_ids": [str(row.get("profile_id") or "") for row in available_rows],
            "all_browser_accounts_unavailable": not bool(available_rows),
        },
        "bindings": bindings,
        "recommendations": purpose_recommendations,
        "notes": [
            "raw account ids are not stored",
            "readonly search/detail/main_chain follow registry policy",
            "selection is executable for readonly purposes when safe_auto policy allows it",
        ],
    }


def select_account(
    purpose: str,
    *,
    registry: Dict[str, Any] | None = None,
    account_rows: List[Dict[str, Any]] | None = None,
    mode: str | None = None,
    reason_code: str = "",
    switches_used: int = 0,
    allow_manual_recovery_followup: bool = False,
) -> Dict[str, Any]:
    """Select the lowest-risk account for a purpose and return policy outcome."""
    registry = registry or profile_registry_internal()
    raw = registry.get("raw") if isinstance(registry, dict) else {}
    policy = dict((raw or {}).get("policy") or registry.get("policy") or {})
    purpose = str(purpose or "diagnostic").strip().lower()
    if account_rows is None:
        account_rows = xhs_account_pool_summary(registry).get("accounts", [])
    candidates = []
    for row in account_rows:
        if str(row.get("platform") or "xiaohongshu").lower() not in {"xiaohongshu", "xhs"}:
            continue
        risk_score = int((row.get("risk") or {}).get("risk_score") or 100)
        runtime_state = str(row.get("runtime_state") or "")
        if row.get("manual_action_required") or runtime_state in {"blocked", "locked"}:
            continue
        if risk_score > _max_auto_switch_risk(policy):
            continue
        candidates.append(row)
    recommended = candidates[0] if candidates else {}
    policy_mode = mode or str(policy.get("default_mode") or policy.get("auto_switch_default") or "safe_auto")
    decision = switch_policy_decision(
        purpose=purpose,
        mode=policy_mode,
        reason_code=reason_code,
        risk_score=int((recommended.get("risk") or {}).get("risk_score") or 100),
        policy=policy,
        switches_used=switches_used,
        allow_manual_recovery_followup=allow_manual_recovery_followup,
    )
    return {
        "schema": "xhs-account-selection/v1",
        "purpose": purpose,
        "recommended_account_slot": recommended.get("account_slot", ""),
        "recommended_profile_id": recommended.get("profile_id", ""),
        "recommended_channel_id": recommended.get("channel_id", ""),
        "risk_score": (recommended.get("risk") or {}).get("risk_score"),
        "risk_level": (recommended.get("risk") or {}).get("risk_level"),
        "candidate_count": len(candidates),
        "mode": policy_mode,
        "purpose_mode": purpose_mode_for(purpose, policy),
        "switch_decision": decision,
        "requires_manual_confirm": bool(decision.get("manual_confirm_required")),
    }


def _max_auto_switch_risk(policy: Dict[str, Any]) -> int:
    try:
        return max(0, min(100, int(policy.get("max_auto_switch_risk_score", 100))))
    except Exception:
        return 100


def _profile_by_slot(profiles: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    result: Dict[str, Dict[str, Any]] = {}
    for profile in profiles:
        if not isinstance(profile, dict):
            continue
        slot = str(profile.get("account_slot") or "")
        if slot and slot not in result:
            result[slot] = profile
    return result


def _xhs_profiles(profiles: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [
        profile
        for profile in profiles
        if isinstance(profile, dict)
        and str(profile.get("platform") or "").strip().lower() in {"xiaohongshu", "xhs"}
    ]


def _xhs_accounts(accounts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [
        account
        for account in accounts
        if isinstance(account, dict)
        and str(account.get("platform") or "").strip().lower() in {"xiaohongshu", "xhs"}
    ]


def _xhs_bindings(bindings: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [
        binding
        for binding in bindings
        if isinstance(binding, dict)
        and str(binding.get("platform") or "").strip().lower() in {"xiaohongshu", "xhs"}
    ]


def _accounts_from_profiles(profiles: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    accounts = []
    for profile in profiles:
        if not isinstance(profile, dict):
            continue
        if str(profile.get("platform") or "").strip().lower() not in {"xiaohongshu", "xhs"}:
            continue
        slot = str(profile.get("account_slot") or "")
        if not slot:
            continue
        accounts.append(
            {
                "platform": str(profile.get("platform") or "xiaohongshu"),
                "account_slot": slot,
                "account_id_hash": stable_hash(slot, length=16),
                "status": "healthy" if profile.get("account_pool_member") else "unknown",
                "risk_score": 50,
                "priority": 100,
            }
        )
    return accounts


def _bindings_from_profiles(profiles: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [
        {
            "binding_id": f"{profile.get('profile_id', '')}-binding",
            "platform": profile.get("platform", ""),
            "account_slot": profile.get("account_slot", ""),
            "profile_id": profile.get("profile_id", ""),
            "channel_id": profile.get("channel_id", ""),
            "main_chain_allowed": str(profile.get("role") or "") == "primary",
        }
        for profile in profiles
        if isinstance(profile, dict)
    ]


def _sanitize_hash(account: Dict[str, Any]) -> str:
    value = str(account.get("account_id_hash") or "")
    return value[:24] if value else stable_hash(account.get("account_slot"), length=16)


def _compact_capability_matrix(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    result: Dict[str, Any] = {}
    for name in ("search_readonly", "detail_readonly", "ocr_readonly", "interactive"):
        item = value.get(name)
        if not isinstance(item, dict):
            continue
        result[name] = {
            "state": str(item.get("state") or ""),
            "cooldown_until": str(item.get("cooldown_until") or ""),
            "canary_result": str(item.get("canary_result") or ""),
            "scope": list(item.get("scope") or [])[:8] if isinstance(item.get("scope"), list) else [],
        }
    return result


def _cooldown_active(account: Dict[str, Any]) -> bool:
    try:
        return float(account.get("cooldown_until") or 0) > time.time()
    except Exception:
        return False
