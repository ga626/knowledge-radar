"""Safe Xiaohongshu account switch decision and execution wrapper."""

from __future__ import annotations

from typing import Any, Dict

from .profile_registry import profile_registry_internal
from .xhs_account_events import xhs_account_admission


def plan_xhs_account_switch(
    purpose: str,
    *,
    reason_code: str = "PROFILE_START_FAILED",
    switches_used: int = 0,
    registry: Dict[str, Any] | None = None,
    allow_manual_recovery_followup: bool = False,
) -> Dict[str, Any]:
    """Return a switch plan without executing it."""
    registry = registry or profile_registry_internal()
    admission = xhs_account_admission(
        purpose,
        reason_code=reason_code,
        registry=registry,
        switches_used=switches_used,
        allow_manual_recovery_followup=allow_manual_recovery_followup,
    )
    executable = admission.get("action") == "allowed"
    return {
        "schema": "xhs-account-switch-plan/v1",
        "status": "ok",
        "purpose": purpose,
        "reason_code": reason_code,
        "executable": bool(executable),
        "execution_mode": "executable" if executable else "plan_only",
        "recommended_account_slot": admission.get("recommended_account_slot", ""),
        "recommended_profile_id": admission.get("recommended_profile_id", ""),
        "recommended_channel_id": admission.get("recommended_channel_id", ""),
        "risk_score": admission.get("risk_score"),
        "risk_level": admission.get("risk_level"),
        "denial_reason": "" if executable else admission.get("reason", ""),
        "admission": admission,
        "notes": [
            "readonly search/detail/main_chain may be admitted by registry policy",
            "callers must record an explicit event before using this plan",
        ],
    }


def execute_xhs_account_switch(
    purpose: str,
    *,
    reason_code: str = "PROFILE_START_FAILED",
    current_profile_id: str = "",
    switches_used: int = 0,
    registry: Dict[str, Any] | None = None,
    allow_manual_recovery_followup: bool = False,
) -> Dict[str, Any]:
    """Switch the managed XHS Chrome profile when safe_auto policy permits it.

    The actual browser transition is delegated to chrome_manager. It already
    compares actual/expected user-data-dir and performs a same-platform safe
    profile switch under the platform lock.
    """
    registry = registry or profile_registry_internal()
    plan = plan_xhs_account_switch(
        purpose,
        reason_code=reason_code,
        switches_used=switches_used,
        registry=registry,
        allow_manual_recovery_followup=allow_manual_recovery_followup,
    )
    if not plan.get("executable"):
        return {**plan, "status": "blocked", "execution_mode": "not_executed"}
    recommended = str(plan.get("recommended_profile_id") or "")
    if not recommended:
        return {**plan, "status": "blocked", "execution_mode": "not_executed", "denial_reason": "NO_RECOMMENDED_PROFILE"}
    if current_profile_id and current_profile_id == recommended:
        return {**plan, "status": "skipped", "execution_mode": "not_executed", "denial_reason": "RECOMMENDED_PROFILE_IS_CURRENT"}
    try:
        from .chrome_manager import _ensure_chrome_debugging

        ok = _ensure_chrome_debugging("xhs", target_profile_id=recommended)
    except Exception as exc:
        return {
            **plan,
            "status": "failed",
            "execution_mode": "attempted",
            "error": f"{type(exc).__name__}: {exc}",
        }
    return {
        **plan,
        "status": "ok" if ok else "failed",
        "execution_mode": "executed",
        "browser_ready": bool(ok),
        "target_profile_id": recommended,
        "target_account_slot": str(plan.get("recommended_account_slot") or ""),
    }


def xhs_account_switcher_summary(registry: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """Return purpose matrix for safe account switch planning."""
    registry = registry or profile_registry_internal()
    purposes = ["diagnostic", "patrol", "search", "detail", "main_chain"]
    plans = {
        purpose: plan_xhs_account_switch(
            purpose,
            registry=registry,
            reason_code="PROFILE_START_FAILED",
            switches_used=0,
        )
        for purpose in purposes
    }
    return {
        "schema": "knowledgeradar-xhs-account-switcher/v1",
        "status": "ok",
        "execution_mode": "executable",
        "plans": plans,
        "notes": [
            "diagnostic/patrol and readonly search/detail/main_chain are executable under safe_auto policy",
            "interactive maintenance flows remain non-executable",
        ],
    }
