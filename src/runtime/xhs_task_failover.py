"""Task-local account exhaustion; shared failures never authorize paid fallback."""

from __future__ import annotations

import uuid
from contextvars import ContextVar
from contextlib import contextmanager
from typing import Callable

from .xhs_account_policy import switch_policy_decision
from .xhs_account_pool import select_account

ACCOUNT_FAILURES = {
    "cdp_unavailable", "login_required", "anti_bot_verification",
    "platform_verification_required", "verification_required", "cookie_missing",
    "page_load_degraded", "search_page_degraded", "empty_search_page",
}
CURRENT_XHS_PROFILE = ContextVar("xhs_task_profile", default="")


@contextmanager
def task_profile(profile_id: str):
    token = CURRENT_XHS_PROFILE.set(profile_id)
    try:
        yield
    finally:
        CURRENT_XHS_PROFILE.reset(token)


def run_browser_accounts(
    registry: dict, pool: dict, current_profile_id: str, attempt: Callable[[str], dict],
    record_failure: Callable[[str, str], None],
    *, purpose: str = "search",
) -> tuple[dict, dict]:
    """Try each eligible profile once, including a full search after every change."""
    rows = pool.get("accounts") or []
    policy = (registry.get("raw") or {}).get("policy") or registry.get("policy") or {}
    policy = dict(policy)
    if purpose in {"search", "detail", "main_chain", "diagnostic", "patrol"}:
        # 只读逐账号轮换由本任务穷尽集合限次；旧全局单次切换上限不再截断C。
        policy["max_switches_per_task"] = max(1, len(rows) - 1)
    selection = select_account(purpose, registry=registry, account_rows=rows)
    eligible = [str(row["profile_id"]) for row in rows if select_account(
        purpose, registry=registry, account_rows=[row],
    ).get("recommended_profile_id")]
    registered = list(dict.fromkeys(str(row.get("profile_id")) for row in rows if row.get("profile_id")))
    order = list(dict.fromkeys([current_profile_id, selection.get("recommended_profile_id")] + eligible))
    order = [profile for profile in order if profile in eligible]
    receipt = {
        "schema": "xhs-task-browser-exhaustion/v1", "task_id": uuid.uuid4().hex,
        "registered_profile_ids": registered, "failed_profile_ids": [],
        "policy_excluded_profile_ids": [profile for profile in registered if profile not in eligible],
        "attempts": [], "exhausted": False, "common_failure": False,
        "profiles": [{"profile_id": str(row.get("profile_id") or ""), "account_slot": row.get("account_slot", ""),
                      "attempted": False, "eligible": str(row.get("profile_id") or "") in eligible,
                      "reason": "eligible_not_attempted" if str(row.get("profile_id") or "") in eligible else
                                str((row.get("observation") or {}).get("reason") or row.get("runtime_state") or "risk_or_policy_excluded"),
                      "expires_at": (row.get("observation") or {}).get("expires_at")}
                     for row in rows if row.get("profile_id")],
    }
    result = {"error": {"type": "browser_accounts_unavailable", "message": "账号均处于登录或风控等待状态"}}
    reason = "PROFILE_START_FAILED"
    for profile in order:
        if receipt["attempts"]:
            decision = switch_policy_decision(
                purpose=purpose, mode=policy.get("default_mode", "safe_auto"),
                reason_code=reason, risk_score=0, policy=policy,
                switches_used=len(receipt["attempts"]) - 1,
                allow_manual_recovery_followup=reason in {"LOGIN_REQUIRED", "SECURITY_VERIFICATION"},
            )
            if not decision.get("allowed"):
                receipt["stopped_by_policy"] = decision.get("reason")
                return result, receipt
        from .account_observations import account_observation_attempt
        with account_observation_attempt(profile) as marker:
            result = attempt(profile)
        profile_receipt = next(row for row in receipt["profiles"] if row["profile_id"] == profile)
        profile_receipt["attempted"] = True
        error = result.get("error") or {}
        if not isinstance(error, dict):
            error = {"type": result.get("failure_type") or "unknown"}
        failure = str(result.get("failure_type") or error.get("failure_type") or error.get("type") or "unknown").lower()
        page_state = result.get("page_state") or error.get("page_state") or {}
        failed = bool(error or result.get("failure_type") or result.get("status") in {"failed", "error", "empty", "needs_interaction"} or result.get("manual_action_required") or page_state.get("manual_action_required"))
        if page_state.get("manual_action_required"):
            subtype = str(page_state.get("failure_subtype") or "")
            observed_state = str(page_state.get("platform_state") or "")
            if subtype in {"frequency_limit", "environment_risk", "ip_or_device_risk"}:
                failure = "global_platform_risk"
            elif observed_state == "login_required":
                failure = "login_required"
            elif observed_state in {"platform_verification_required", "app_scan_required"}:
                failure = "platform_verification_required"
        receipt["attempts"].append({"profile_id": profile, "status": "failed" if failed else "ok", "failure_type": failure if failed else ""})
        profile_receipt["reason"] = failure if failed else "business_success"
        if not failed:
            from .account_observations import observe_account
            observe_account(profile, "xiaohongshu", "business_verified", source=f"{purpose}_business_result", expected_revision=marker["revision"])
            for row in receipt["profiles"]:
                if row["eligible"] and not row["attempted"]:
                    row["reason"] = "not_needed_after_success"
            return result, receipt
        if failure not in ACCOUNT_FAILURES:
            receipt["common_failure"] = True
            return result, receipt
        receipt["failed_profile_ids"].append(profile)
        reason = ("SECURITY_VERIFICATION" if "verification" in failure else
                  "LOGIN_REQUIRED" if failure in {"login_required", "cookie_missing"} else "CDP_PORT_UNAVAILABLE")
        with account_observation_attempt(profile, revision=marker["revision"]):
            record_failure(profile, reason)
    # 跳过不等于试过且失败；有任一政策排除时不授权付费兜底。
    receipt["exhausted"] = bool(registered) and set(registered) == set(receipt["failed_profile_ids"])
    return result, receipt
