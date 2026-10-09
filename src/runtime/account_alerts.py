"""Account recovery projection over the existing durable browser sessions."""

from __future__ import annotations

import re

from .browser_sessions import browser_sessions_summary
from .profile_registry import profile_registry_internal
from .xhs_account_identity import identity_for_profile

LABELS = {"xiaohongshu": "小红书", "xhs": "小红书", "zhihu": "知乎", "boss": "BOSS直聘", "liepin": "猎聘", "zhilian": "智联招聘"}
LABELS.update({"maimai": "脉脉", "cnki": "知网", "vip_oa": "维普开放获取", "coaj": "COAJ",
               "ucdrs": "全国图书馆参考咨询", "calis_thesis": "CALIS学位论文", "nstrs": "国家科技报告", "pubscholar": "PubScholar"})
PENDING_STATES = {"NEEDS_USER", "USER_INTERACTING", "USER_DONE_VERIFYING"}


def account_identity_fields(profile: dict, identity: dict | None = None) -> dict:
    identity = identity or {}
    label = str(identity.get("display_label") or profile.get("display_label") or profile.get("label") or profile.get("profile_id") or "未登记")
    hint = str(identity.get("masked_hint") or profile.get("masked_hint") or "")
    number = str(identity.get("account_number") or profile.get("account_number") or "")
    source = "registered_number" if number else ""
    registered_label = identity.get("display_label") or profile.get("display_label") or profile.get("label")
    if not number and registered_label:
        match = re.search(r"([0-9]{3,})$", label)
        if match:
            number, source = match.group(1), "registered_display_label"
    slot = str(profile.get("account_slot") or identity.get("account_slot") or "")
    return {"display_label": label, "masked_hint": hint, "account_number": number,
            "number_source": source, "account_slot": slot, "slot_label": slot.rsplit("_", 1)[-1].upper() if slot.startswith("xhs_account_") else ""}


def account_alert_snapshot() -> dict:
    registry = profile_registry_internal()
    profiles = (registry.get("raw") or {}).get("profiles") or registry.get("profiles") or []
    sessions = browser_sessions_summary(limit=100).get("sessions") or []
    by_profile = {}
    unresolved = {session.get("profile_id") for session in sessions if (session.get("metadata") or {}).get("account_recovery_pending")}
    for session in sessions:
        if session.get("profile_id") and session["profile_id"] not in by_profile:
            by_profile[session["profile_id"]] = session
    rows = []
    for profile in profiles:
        platform = str(profile.get("platform") or "")
        if platform not in LABELS or not profile.get("profile_id"):
            continue
        if platform in {"xiaohongshu", "xhs"} and profile.get("account_pool_member") is False:
            continue
        identity = identity_for_profile(profile["profile_id"]) if platform in {"xiaohongshu", "xhs"} else {}
        fields = account_identity_fields(profile, identity)
        session = by_profile.get(profile["profile_id"], {})
        state = str(session.get("state") or "UNOBSERVED")
        rows.append({
            **fields, "profile_id": profile["profile_id"], "platform": platform,
            "platform_label": LABELS[platform], "state": state,
            "needs_action": state in PENDING_STATES or profile["profile_id"] in unresolved or (state == "FAILED" and bool((session.get("metadata") or {}).get("manual_source"))),
            "reason": str(session.get("reason") or ""),
            "updated_at": str(session.get("updated_at_iso") or ""),
            "notification": (session.get("metadata") or {}).get("desktop_notification") or {},
        })
    return {"schema": "knowledgeradar-account-alerts/v1", "accounts": rows,
            "pending_count": sum(row["needs_action"] for row in rows)}


def recover_account(profile_id: str, action: str) -> dict:
    from .chrome_manager import complete_browser_interaction, request_user_login, probe_browser_auth

    profiles = (profile_registry_internal().get("raw") or {}).get("profiles") or []
    profile = next((row for row in profiles if row.get("profile_id") == profile_id and row.get("platform") in LABELS), None)
    if not profile:
        raise ValueError("未找到登记账号，请刷新后重试。")
    if profile.get("platform") in {"xiaohongshu", "xhs"} and profile.get("account_pool_member") is False:
        raise ValueError("实验通道不属于当前账号恢复流程。")
    platform = str(profile["platform"])
    resource = f"xhs:{profile_id}" if platform in {"xiaohongshu", "xhs"} else platform
    if action == "verify":
        probe_platform = "xhs" if platform in {"xiaohongshu", "xhs"} else platform
        probe = probe_browser_auth(probe_platform, target_profile_id=profile_id)
        if probe.get("status") != "ok" or probe.get("manual_action_required"):
            return {"status": "not_verified", "profile_id": profile_id,
                    "detail": probe.get("detail") or "当前账号尚未验证通过，请完成登录或稍后重试。", "probe": probe}
        result = complete_browser_interaction(probe_platform, probe_result=probe, profile_id=profile_id)
        result["detail"] = "该账号探针已通过，浏览器已恢复普通生命周期；实际任务仍会逐次核验。"
        return result
    if action == "open":
        return request_user_login(resource, "manual_login", target_profile_id=profile_id,
                                  trigger_evidence=["user_requested_account_recovery"], source="account_console")
    raise ValueError("不支持的账号操作。")
