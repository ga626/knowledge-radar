"""Normalize platform admission and degradation states."""

from __future__ import annotations

from typing import Any, Dict


PASS = "PASS"
EXPECTED_DEGRADED = "EXPECTED_DEGRADED"
NEEDS_INTERACTION = "NEEDS_INTERACTION"
RETRY_LATER = "RETRY_LATER"
FAIL = "FAIL"

MANUAL_FAILURE_TYPES = {
    "login_required",
    "anti_bot_verification",
    "platform_verification_required",
    "manual_action_required",
    "captcha_required",
    "account_risk",
}
EXPECTED_DEGRADED_FAILURE_TYPES = {
    "auth_state_unconfirmed",
    "ambiguous_page_state",
    "empty_detail",
    "dead_link",
    "not_found",
    "unsupported_url",
    "not_configured_optional",
    "quota_exhausted",
    "provider_blocked",
}
RETRY_LATER_FAILURE_TYPES = {
    "cdp_unavailable",
    "timeout",
    "network_timeout",
    "rate_limited",
    "cooldown",
    "temporary_provider_error",
}


def classify_admission_state(payload: Dict[str, Any] | None) -> Dict[str, Any]:
    data = payload or {}
    failure_type = str(
        data.get("failure_type")
        or data.get("failure_subtype")
        or data.get("error_type")
        or data.get("type")
        or ""
    )
    platform_state = str(data.get("platform_state") or "")
    manual = bool(data.get("manual_action_required")) or bool(data.get("login_required"))
    key = failure_type or platform_state
    if manual or failure_type in MANUAL_FAILURE_TYPES or platform_state in MANUAL_FAILURE_TYPES:
        status = NEEDS_INTERACTION
        retryable = False
    elif failure_type in RETRY_LATER_FAILURE_TYPES or platform_state in RETRY_LATER_FAILURE_TYPES:
        status = RETRY_LATER
        retryable = True
    elif failure_type in EXPECTED_DEGRADED_FAILURE_TYPES:
        status = EXPECTED_DEGRADED
        retryable = False
    elif data.get("error"):
        status = FAIL
        retryable = bool(data.get("retryable", True))
    else:
        status = PASS
        retryable = False
    return {
        "schema": "knowledgeradar-admission-state/v1",
        "status_class": status,
        "failure_type": failure_type,
        "platform_state": platform_state,
        "manual_action_required": status == NEEDS_INTERACTION,
        "retryable": retryable,
        "expected_degraded": status == EXPECTED_DEGRADED,
        "reason_key": key,
    }
