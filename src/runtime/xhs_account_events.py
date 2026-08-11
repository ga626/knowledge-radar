"""Xiaohongshu account-pool event classification and switch controls."""

from __future__ import annotations

from typing import Any, Dict, List

from .profile_registry import profile_registry_internal, record_profile_state
from .xhs_account_pool import DEFAULT_PURPOSES, select_account, xhs_account_pool_summary


EVENT_RULES: Dict[str, Dict[str, Any]] = {
    "OK": {"state": "healthy", "cooldown_seconds": 0, "manual": False},
    "PROFILE_START_FAILED": {"state": "degraded", "cooldown_seconds": 0, "manual": False},
    "CDP_PORT_UNAVAILABLE": {"state": "degraded", "cooldown_seconds": 0, "manual": False},
    "NO_CLICKABLE_CANDIDATES": {"state": "degraded", "cooldown_seconds": 0, "manual": False},
    "PAGE_LOAD_DEGRADED": {"state": "degraded", "cooldown_seconds": 0, "manual": False},
    "SEARCH_PAGE_DEGRADED": {"state": "degraded", "cooldown_seconds": 0, "manual": False},
    "DETAIL_WEAK": {"state": "degraded", "cooldown_seconds": 0, "manual": False},
    "COOKIE_MISSING": {"state": "degraded", "cooldown_seconds": 0, "manual": True},
    "LOGIN_REQUIRED": {"state": "degraded", "cooldown_seconds": 0, "manual": True},
    "HTTP_429": {"state": "cooldown", "cooldown_seconds": 1800, "manual": False},
    "FREQUENCY_LIMIT": {"state": "cooldown", "cooldown_seconds": 1800, "manual": False},
    "OPERATION_FREQUENT": {"state": "cooldown", "cooldown_seconds": 1800, "manual": False},
    "SEARCH_FREQUENT": {"state": "cooldown", "cooldown_seconds": 1800, "manual": False},
    "CAPTCHA_REQUIRED": {"state": "blocked", "cooldown_seconds": 7200, "manual": True},
    "SECURITY_VERIFICATION": {"state": "blocked", "cooldown_seconds": 7200, "manual": True},
    "ANTI_BOT_BLOCKED": {"state": "blocked", "cooldown_seconds": 7200, "manual": True},
    "ACCOUNT_LOCKED": {"state": "locked", "cooldown_seconds": 86400, "manual": True},
    "ACCOUNT_RISK": {"state": "blocked", "cooldown_seconds": 21600, "manual": True},
    "IP_OR_DEVICE_RISK": {"state": "blocked", "cooldown_seconds": 21600, "manual": True},
}


def classify_account_event(reason_code: str) -> Dict[str, Any]:
    """Classify an account/profile observation into runtime-state actions."""
    code = str(reason_code or "").strip().upper() or "UNKNOWN"
    rule = dict(EVENT_RULES.get(code) or {"state": "degraded", "cooldown_seconds": 0, "manual": True})
    rule.update(
        {
            "schema": "xhs-account-event-classification/v1",
            "reason_code": code,
            "safe_to_switch_account": _safe_to_switch_hint(code, rule),
        }
    )
    return rule


def record_xhs_account_event(
    profile_id: str,
    reason_code: str,
    *,
    last_tool: str = "",
    notes: List[str] | None = None,
) -> Dict[str, Any]:
    """Record a sanitized runtime observation for a profile.

    The function updates runtime state only. It never launches a browser and
    never switches accounts.
    """
    event = classify_account_event(reason_code)
    return record_profile_state(
        profile_id,
        platform="xiaohongshu",
        state=str(event.get("state") or "degraded"),
        reason_code=str(event.get("reason_code") or ""),
        cooldown_seconds=int(event.get("cooldown_seconds") or 0),
        manual_action_required=bool(event.get("manual")),
        safe_to_switch_account=bool(event.get("safe_to_switch_account")),
        last_tool=last_tool,
        notes=notes or [f"event:{event.get('reason_code')}"],
    )


def xhs_account_admission(
    purpose: str,
    *,
    reason_code: str = "",
    registry: Dict[str, Any] | None = None,
    mode: str | None = None,
    switches_used: int = 0,
    allow_manual_recovery_followup: bool = False,
) -> Dict[str, Any]:
    """Return advisory admission for an Xiaohongshu chain purpose.

    This is a read-only gate. It does not switch accounts and does not launch a
    browser. Callers can use it to decide whether a workflow should proceed,
    require manual confirmation, or stay blocked.
    """
    registry = registry or profile_registry_internal()
    pool = xhs_account_pool_summary(registry)
    selected = select_account(
        purpose,
        registry=registry,
        account_rows=pool.get("accounts", []),
        mode=mode,
        reason_code=reason_code,
        switches_used=switches_used,
        allow_manual_recovery_followup=allow_manual_recovery_followup,
    )
    decision = selected.get("switch_decision") or {}
    purpose = str(purpose or "").strip().lower() or "diagnostic"
    if decision.get("allowed"):
        action = "allowed"
    elif decision.get("manual_confirm_required"):
        action = "manual_confirm"
    else:
        action = "blocked"
    return {
        "schema": "xhs-account-admission/v1",
        "purpose": purpose,
        "action": action,
        "recommended_account_slot": selected.get("recommended_account_slot", ""),
        "recommended_profile_id": selected.get("recommended_profile_id", ""),
        "recommended_channel_id": selected.get("recommended_channel_id", ""),
        "risk_score": selected.get("risk_score"),
        "risk_level": selected.get("risk_level"),
        "reason": decision.get("reason", ""),
        "decision": decision,
        "notes": [
            "admission is executable by the account switcher when safe_auto allows it",
            "no browser launch, no account switch",
        ],
    }


def xhs_account_control_summary(registry: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """Return event policy and switch dry-runs for health summaries."""
    registry = registry or profile_registry_internal()
    pool = xhs_account_pool_summary(registry)
    policy = pool.get("policy", {})
    dry_runs = []
    for purpose in DEFAULT_PURPOSES:
        dry_runs.append(
            {
                "purpose": purpose,
                "default": select_account(purpose, registry=registry, account_rows=pool.get("accounts", [])),
                "safe_auto_probe": select_account(
                    purpose,
                    registry=registry,
                    account_rows=pool.get("accounts", []),
                    mode="safe_auto",
                    reason_code="PROFILE_START_FAILED",
                    switches_used=0,
                ),
                "admission": xhs_account_admission(purpose, registry=registry, reason_code="PROFILE_START_FAILED"),
            }
        )
    what_if_policy = dict(policy)
    what_if_policy["default_mode"] = "safe_auto"
    what_if_policy["max_switches_per_task"] = 1
    what_if_registry = _with_policy(registry, what_if_policy)
    what_if_pool = xhs_account_pool_summary(what_if_registry)
    what_if = {
        purpose: xhs_account_admission(
            purpose,
            registry=what_if_registry,
            reason_code="PROFILE_START_FAILED",
            mode="safe_auto",
            switches_used=0,
        )
        for purpose in DEFAULT_PURPOSES
    }
    return {
        "schema": "knowledgeradar-xhs-account-control/v1",
        "status": "ok",
        "auto_switch": str(policy.get("default_mode") or policy.get("auto_switch_default") or "safe_auto"),
        "policy": policy,
        "event_rules": {
            code: {
                "state": rule.get("state"),
                "cooldown_seconds": rule.get("cooldown_seconds"),
                "manual": rule.get("manual"),
            }
            for code, rule in EVENT_RULES.items()
        },
        "dry_runs": dry_runs,
        "safe_auto_what_if": {
            "schema": "xhs-account-safe-auto-what-if/v1",
            "enabled_for_simulation": True,
            "policy": what_if_pool.get("policy", {}),
            "admission": what_if,
            "notes": [
                "registry safe_auto policy is now executable for readonly search/detail/main_chain",
                "readonly search/detail/main_chain follow registry policy; interactive challenges remain manual only",
            ],
        },
        "notes": [
            "record_xhs_account_event updates runtime state",
            "xhs_account_switcher can switch readonly search/detail/main_chain when registry policy allows it",
        ],
    }


def _safe_to_switch_hint(code: str, rule: Dict[str, Any]) -> bool:
    if code == "OK":
        return False
    return str(rule.get("state") or "") in {"degraded", "cooldown", "blocked", "locked"}


def _with_policy(registry: Dict[str, Any], policy: Dict[str, Any]) -> Dict[str, Any]:
    cloned = dict(registry)
    raw = dict(cloned.get("raw") or {})
    raw["policy"] = dict(policy)
    cloned["raw"] = raw
    cloned["policy"] = dict(policy)
    return cloned
