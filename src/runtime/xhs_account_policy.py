"""Purpose-aware account-pool switch policy for Xiaohongshu."""

from __future__ import annotations

from typing import Any, Dict


DEFAULT_SAFE_AUTO_ALLOWED = {"diagnostic", "patrol", "search", "detail", "ocr", "main_chain"}
DEFAULT_SAFE_AUTO_DENIED = {"batch_detail", "multimodal", "interactive_post", "comment_dm_group", "maintenance"}
HIGH_RISK_REASON_CODES = {
    "CAPTCHA_REQUIRED",
    "SECURITY_VERIFICATION",
    "ANTI_BOT_BLOCKED",
    "ACCOUNT_LOCKED",
    "ACCOUNT_RISK",
    "IP_OR_DEVICE_RISK",
    "MULTI_ACCOUNT_FAILURE",
    "UNKNOWN_HIGH_RISK",
}
COOLDOWN_FIRST_REASON_CODES = {"HTTP_429", "FREQUENCY_LIMIT", "OPERATION_FREQUENT", "SEARCH_FREQUENT"}
DEFAULT_SAFE_AUTO_REASON_CODES = {"PROFILE_START_FAILED", "CDP_PORT_UNAVAILABLE", "COOKIE_MISSING", "COOLDOWN_ACTIVE", "LOGIN_REQUIRED"}


def switch_policy_decision(
    *,
    purpose: str,
    mode: str,
    reason_code: str = "",
    risk_score: int = 100,
    policy: Dict[str, Any] | None = None,
    switches_used: int = 0,
    allow_manual_recovery_followup: bool = False,
) -> Dict[str, Any]:
    """Return whether account switching is allowed for a purpose."""
    policy = policy or {}
    purpose = str(purpose or "").strip().lower() or "diagnostic"
    mode = str(mode or policy.get("default_mode") or "disabled").strip().lower()
    reason_code = str(reason_code or "").strip().upper()
    allowed_purposes = set(policy.get("safe_auto_allowed_purposes") or DEFAULT_SAFE_AUTO_ALLOWED)
    denied_purposes = set(policy.get("safe_auto_denied_purposes") or DEFAULT_SAFE_AUTO_DENIED)
    max_switches = int(policy.get("max_switches_per_task") or 0)

    if mode == "disabled":
        return _decision(False, "MODE_DISABLED", purpose, mode)
    if mode == "manual_confirm":
        return _decision(False, "MANUAL_CONFIRM_REQUIRED", purpose, mode, manual_confirm=True)
    if mode != "safe_auto":
        return _decision(False, "UNKNOWN_MODE", purpose, mode)
    if purpose in denied_purposes:
        return _decision(False, f"PURPOSE_DENIED:{purpose}", purpose, mode)
    if purpose not in allowed_purposes:
        return _decision(False, f"PURPOSE_NOT_ALLOWED:{purpose}", purpose, mode)
    if max_switches <= 0:
        return _decision(False, "MAX_SWITCHES_ZERO", purpose, mode)
    if switches_used >= max_switches:
        return _decision(False, "MAX_SWITCHES_REACHED", purpose, mode)
    if reason_code in HIGH_RISK_REASON_CODES:
        if allow_manual_recovery_followup:
            return _decision(
                True,
                f"MANUAL_RECOVERY_FOLLOWUP_ALLOWED:{reason_code}",
                purpose,
                mode,
                reason_code=reason_code,
            )
        return _decision(False, f"HIGH_RISK_REASON:{reason_code}", purpose, mode)
    if reason_code in COOLDOWN_FIRST_REASON_CODES:
        return _decision(False, f"COOLDOWN_FIRST:{reason_code}", purpose, mode)
    allowed_reasons = set(policy.get("allowed_auto_switch_reasons") or DEFAULT_SAFE_AUTO_REASON_CODES)
    # Login expiry is a safe failover condition only after explicit verification/
    # captcha signals have already been excluded above.
    allowed_reasons.add("LOGIN_REQUIRED")
    if reason_code and reason_code not in allowed_reasons:
        return _decision(False, f"REASON_NOT_ALLOWED:{reason_code}", purpose, mode)
    if risk_score >= _risk_score_limit(policy):
        return _decision(False, "RISK_SCORE_TOO_HIGH", purpose, mode)
    return _decision(True, "SAFE_AUTO_ALLOWED", purpose, mode, reason_code=reason_code)


def purpose_mode_for(purpose: str, policy: Dict[str, Any] | None = None) -> str:
    """Return the most permissive initial mode for a purpose without enabling switching."""
    policy = policy or {}
    purpose = str(purpose or "").strip().lower()
    if purpose in set(policy.get("safe_auto_allowed_purposes") or DEFAULT_SAFE_AUTO_ALLOWED):
        return "safe_auto_enabled"
    return "disabled"


def _risk_score_limit(policy: Dict[str, Any]) -> int:
    try:
        return max(0, min(100, int(policy.get("max_auto_switch_risk_score", 100))))
    except Exception:
        return 100


def _decision(allowed: bool, reason: str, purpose: str, mode: str, *, manual_confirm: bool = False, reason_code: str = "") -> Dict[str, Any]:
    return {
        "schema": "xhs-account-switch-policy/v1",
        "allowed": bool(allowed),
        "reason": reason,
        "trigger_reason_code": reason_code,
        "purpose": purpose,
        "mode": mode,
        "manual_confirm_required": bool(manual_confirm),
    }
