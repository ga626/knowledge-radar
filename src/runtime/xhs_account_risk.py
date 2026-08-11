"""Risk scoring helpers for Xiaohongshu account-pool governance."""

from __future__ import annotations

import time
from typing import Any, Dict, Iterable, List


HIGH_RISK_REASONS = {
    "CAPTCHA_REQUIRED",
    "SECURITY_VERIFICATION",
    "ANTI_BOT_BLOCKED",
    "ACCOUNT_RISK",
    "ACCOUNT_LOCKED",
    "IP_OR_DEVICE_RISK",
}

COOLDOWN_REASONS = {
    "HTTP_429",
    "FREQUENCY_LIMIT",
    "OPERATION_FREQUENT",
    "SEARCH_FREQUENT",
}


def score_account_risk(
    account: Dict[str, Any],
    *,
    profile: Dict[str, Any] | None = None,
    runtime_state: Dict[str, Any] | None = None,
    events: Iterable[Dict[str, Any]] | None = None,
) -> Dict[str, Any]:
    """Return a deterministic, explainable risk score for one account."""
    score = _base_score(str(account.get("status") or "unknown"))
    reasons: List[str] = [f"base:{score}"]
    profile = profile or {}
    runtime_state = runtime_state or {}
    events = list(events or [])

    text = " ".join(
        [
            str(account.get("status") or ""),
            str(profile.get("status") or ""),
            " ".join(str(item) for item in account.get("notes", []) or []),
            " ".join(str(item) for item in profile.get("notes", []) or []),
            str(runtime_state.get("reason_code") or ""),
            str(runtime_state.get("state") or ""),
        ]
    ).lower()

    if "login_persistence_ok" in text or "required cookies present" in text:
        score -= 10
        reasons.append("login_persistence_ok:-10")
    if "search_minimum_ok" in text or "search/detail selector probe passed" in text:
        score -= 3
        reasons.append("search_minimum_ok:-3")
    if "detail_selectors_ok" in text or "detail selector" in text:
        score -= 3
        reasons.append("detail_selectors_ok:-3")
    if "body_text_weak" in text or "body text quality weak" in text:
        score += 5
        reasons.append("body_text_weak:+5")
    if runtime_state.get("state") in {"healthy", "available"}:
        score -= 5
        reasons.append("runtime_healthy:-5")
    if runtime_state.get("manual_action_required"):
        score += 45
        reasons.append("manual_action:+45")
    if runtime_state.get("cooldown_active") or _cooldown_active(account):
        score += 35
        reasons.append("cooldown_active:+35")

    # A verified recovery writes an OK observation for this exact profile.
    # Earlier login/captcha events remain in the audit trail, but must not keep
    # poisoning the account's current admission indefinitely.  Only incidents
    # after the newest successful recovery describe the active risk epoch.
    active_events = events[-20:]
    latest_ok_index = max(
        (index for index, event in enumerate(active_events) if str(event.get("reason_code") or "").upper() == "OK"),
        default=-1,
    )
    if latest_ok_index >= 0:
        active_events = active_events[latest_ok_index:]
        reasons.append("recovery_epoch_after_latest_ok")

    for event in active_events:
        code = str(event.get("reason_code") or "").upper()
        if code in HIGH_RISK_REASONS:
            score += 50
            reasons.append(f"{code}:+50")
        elif code in COOLDOWN_REASONS:
            score += 40
            reasons.append(f"{code}:+40")
        elif code in {"LOGIN_REQUIRED", "COOKIE_MISSING"}:
            score += 25
            reasons.append(f"{code}:+25")
        elif code == "OK":
            score -= 2
            reasons.append("OK:-2")

    configured = _int(account.get("risk_score"))
    if configured is not None:
        score = round((score + configured) / 2)
        reasons.append(f"configured_blend:{configured}")

    score = max(0, min(100, int(score)))
    return {
        "schema": "xhs-account-risk/v1",
        "risk_score": score,
        "risk_level": _risk_level(score),
        "reasons": reasons[:12],
        "recommended": score < 60 and not runtime_state.get("manual_action_required") and not runtime_state.get("cooldown_active"),
    }


def _base_score(status: str) -> int:
    return {
        "healthy": 20,
        "available": 20,
        "unknown": 50,
        "degraded": 65,
        "blocked": 90,
        "locked": 95,
    }.get(status, 50)


def _risk_level(score: int) -> str:
    if score < 35:
        return "low"
    if score < 65:
        return "medium"
    if score < 85:
        return "high"
    return "critical"


def _cooldown_active(account: Dict[str, Any]) -> bool:
    try:
        return float(account.get("cooldown_until") or 0) > time.time()
    except Exception:
        return False


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except Exception:
        return None
