from __future__ import annotations

import json
from pathlib import Path
import urllib.request

from runtime.browser_sessions import BrowserSessionStore, manual_action_request_from_session
from runtime.xhs_account_identity import claim_xhs_account_identity, xhs_account_identity_summary
from runtime.xhs_account_policy import switch_policy_decision
from runtime.xhs_account_risk import score_account_risk
from runtime.xhs_account_switcher import execute_xhs_account_switch


def test_claim_keeps_only_label_and_irreversible_identity_fingerprint(tmp_path: Path) -> None:
    state_path = tmp_path / "identities.json"
    key_path = tmp_path / "identity.key"

    claimed = claim_xhs_account_identity(
        profile_id="xhs-b",
        account_slot="xhs_account_b",
        profile_dir_hash="profile-b-hash",
        display_label="工作主号",
        masked_hint="尾号 1234",
        nickname="可见昵称",
        user_id="raw-platform-user-id-must-not-persist",
        path=state_path,
        key_path=key_path,
    )

    assert claimed["status"] == "ok"
    assert claimed["display_label"] == "工作主号"
    assert claimed["observed_nickname"] == "可见昵称"
    serialized = state_path.read_text(encoding="utf-8")
    assert "raw-platform-user-id-must-not-persist" not in serialized
    assert "可见昵称" not in serialized
    assert xhs_account_identity_summary(path=state_path)["profiles"][0]["display_label"] == "工作主号"


def test_browser_session_idle_deadline_is_persisted_without_refreshing_activity(tmp_path: Path) -> None:
    store = BrowserSessionStore(
        state_path=tmp_path / "browser-sessions.json",
        event_path=tmp_path / "browser-session-events.jsonl",
    )
    created = store.upsert(
        platform="xhs",
        profile_id="xhs-c",
        account_slot="xhs_account_c",
        profile_dir="D:/profiles/xhs/c",
        state="READY_SILENT",
    )

    updated = store.set_deadline(
        "xhs",
        1234.5,
        profile_id="xhs-c",
        profile_dir="D:/profiles/xhs/c",
        event="idle_cleanup_scheduled",
    )
    reloaded = BrowserSessionStore(
        state_path=tmp_path / "browser-sessions.json",
        event_path=tmp_path / "browser-session-events.jsonl",
    ).summary()["sessions"][0]

    assert updated["session_id"] == created["session_id"]
    assert updated["deadline_at"] == 1234.5
    assert reloaded["deadline_at"] == 1234.5


def test_claim_rejects_same_platform_identity_on_two_profiles(tmp_path: Path) -> None:
    state_path = tmp_path / "identities.json"
    key_path = tmp_path / "identity.key"
    first = claim_xhs_account_identity(
        profile_id="xhs-a",
        account_slot="xhs_account_a",
        profile_dir_hash="a",
        display_label="备用号 1",
        nickname="",
        user_id="same-user",
        path=state_path,
        key_path=key_path,
    )
    second = claim_xhs_account_identity(
        profile_id="xhs-b",
        account_slot="xhs_account_b",
        profile_dir_hash="b",
        display_label="工作主号",
        nickname="",
        user_id="same-user",
        path=state_path,
        key_path=key_path,
    )

    assert first["status"] == "ok"
    assert second["reason_code"] == "IDENTITY_ALREADY_CLAIMED_BY_ANOTHER_PROFILE"
    assert second["conflicting_profile_id"] == "xhs-a"


def test_claim_allows_user_confirmed_label_when_platform_identity_is_temporarily_unknown(tmp_path: Path) -> None:
    state_path = tmp_path / "identities.json"

    claimed = claim_xhs_account_identity(
        profile_id="xhs-a",
        account_slot="xhs_account_a",
        profile_dir_hash="a",
        display_label="A9331",
        nickname="",
        user_id="",
        allow_user_confirmed_without_identity=True,
        path=state_path,
    )

    assert claimed["status"] == "ok"
    assert claimed["identity_verification"] == "user_confirmed_pending_platform_proof"
    assert claimed["identity_fingerprint"] == ""
    assert xhs_account_identity_summary(path=state_path)["profiles"][0]["display_label"] == "A9331"


def test_manual_action_exposes_human_label_and_profile_scope(tmp_path: Path) -> None:
    store = BrowserSessionStore(tmp_path / "sessions.json", tmp_path / "events.jsonl")
    session = store.upsert(
        platform="xhs",
        debug_port="12733",
        profile_dir="D:/profiles/xhs/b",
        profile_id="xhs-b",
        account_slot="xhs_account_b",
        state="USER_INTERACTING",
        reason="login_required",
        metadata={"account_identity": {"display_label": "工作主号", "masked_hint": "尾号 1234"}},
    )

    action = manual_action_request_from_session(session)

    assert action["display_label"] == "工作主号"
    assert action["masked_hint"] == "尾号 1234"
    assert action["profile_id"] == "xhs-b"
    assert action["blocks_only_platform"] is True
    assert "工作主号" in action["human_message"]


def test_profile_scoped_transition_does_not_update_another_xhs_account(tmp_path: Path) -> None:
    store = BrowserSessionStore(tmp_path / "sessions.json", tmp_path / "events.jsonl")
    store.upsert(platform="xhs", profile_dir="D:/profiles/xhs/a", profile_id="xhs-a", state="USER_INTERACTING")
    store.upsert(platform="xhs", profile_dir="D:/profiles/xhs/b", profile_id="xhs-b", state="USER_INTERACTING")

    changed = store.transition("xhs", "READY_SILENT", profile_id="xhs-a", profile_dir="D:/profiles/xhs/a")
    states = {item["profile_id"]: item["state"] for item in store.summary()["sessions"]}

    assert changed["profile_id"] == "xhs-a"
    assert states == {"xhs-a": "READY_SILENT", "xhs-b": "USER_INTERACTING"}


def test_login_expiry_can_fail_over_but_security_verification_cannot() -> None:
    policy = {"default_mode": "safe_auto", "max_switches_per_task": 2}

    login = switch_policy_decision(purpose="search", mode="safe_auto", reason_code="LOGIN_REQUIRED", risk_score=10, policy=policy)
    verify = switch_policy_decision(purpose="search", mode="safe_auto", reason_code="SECURITY_VERIFICATION", risk_score=10, policy=policy)

    assert login["allowed"] is True
    assert verify["allowed"] is False
    assert verify["reason"] == "HIGH_RISK_REASON:SECURITY_VERIFICATION"


def test_manual_recovery_followup_allows_only_the_next_readonly_canary() -> None:
    policy = {"default_mode": "safe_auto", "max_switches_per_task": 2}

    decision = switch_policy_decision(
        purpose="search",
        mode="safe_auto",
        reason_code="SECURITY_VERIFICATION",
        risk_score=10,
        policy=policy,
        allow_manual_recovery_followup=True,
    )

    assert decision["allowed"] is True
    assert decision["reason"] == "MANUAL_RECOVERY_FOLLOWUP_ALLOWED:SECURITY_VERIFICATION"


def test_verified_recovery_starts_a_new_risk_epoch_for_that_profile() -> None:
    risk = score_account_risk(
        {"status": "healthy"},
        runtime_state={"state": "healthy", "manual_action_required": False},
        events=[
            {"reason_code": "SECURITY_VERIFICATION"},
            {"reason_code": "LOGIN_REQUIRED"},
            {"reason_code": "OK"},
        ],
    )

    assert risk["risk_score"] < 60
    assert risk["recommended"] is True
    assert "recovery_epoch_after_latest_ok" in risk["reasons"]


def test_xhs_switch_passes_recommended_profile_to_browser(monkeypatch) -> None:
    import runtime.xhs_account_switcher as switcher
    import runtime.chrome_manager as chrome_manager

    monkeypatch.setattr(
        switcher,
        "plan_xhs_account_switch",
        lambda *args, **kwargs: {
            "executable": True,
            "recommended_profile_id": "xhs-a",
            "recommended_account_slot": "xhs_account_a",
        },
    )
    calls = []
    monkeypatch.setattr(
        chrome_manager,
        "_ensure_chrome_debugging",
        lambda platform, *, target_profile_id="": calls.append((platform, target_profile_id)) or True,
    )

    result = execute_xhs_account_switch("search", current_profile_id="xhs-b")

    assert result["status"] == "ok"
    assert result["target_profile_id"] == "xhs-a"
    assert calls == [("xhs", "xhs-a")]


def test_next_xhs_use_recovers_matching_scanned_profile_without_polling(monkeypatch) -> None:
    from collectors.platform import xiaohongshu as xhs

    monkeypatch.setattr(xhs, "_xhs_login_state_ok", lambda state: True)
    monkeypatch.setattr(xhs, "_selected_xhs_profile_id", lambda: "xhs-b")
    monkeypatch.setattr(xhs, "_find_chrome_with_debug_port", lambda platform: 321)
    monkeypatch.setattr(xhs, "_chrome_user_data_dir_for_pid", lambda pid: "D:/profiles/xhs/b")
    profile_hash = xhs.browser_profile_hash("D:/profiles/xhs/b")
    monkeypatch.setattr(
        xhs,
        "browser_sessions_summary",
        lambda limit: {"pending_interactions": [{"platform": "xhs", "profile_id": "xhs-b", "profile_dir_hash": profile_hash}]},
    )
    calls = []
    monkeypatch.setattr(
        xhs,
        "complete_browser_interaction",
        lambda platform, probe_result, *, profile_id, profile_dir: calls.append((platform, probe_result, profile_id, profile_dir)) or {"status": "ok"},
    )

    result = xhs._recover_pending_xhs_interaction_if_authenticated({"ok": True})

    assert result["recovered"] is True
    assert calls[0][0] == "xhs"
    assert calls[0][2:] == ("xhs-b", "D:/profiles/xhs/b")


def test_xhs_target_profile_never_reuses_stale_remembered_profile(monkeypatch) -> None:
    import runtime.chrome_manager as chrome_manager

    expected = "D:/profiles/xhs/a"
    actual = "D:/profiles/xhs/b"
    switches = []

    class FakeResponse:
        def read(self):
            return json.dumps({"Browser": "Chrome/Test"}).encode("utf-8")

    monkeypatch.setattr(chrome_manager, "_managed_chrome_profile_dir", lambda *args, **kwargs: expected)
    monkeypatch.setattr(chrome_manager, "_chrome_debug_url", lambda platform: "http://127.0.0.1:1")
    monkeypatch.setattr(chrome_manager, "_chrome_debug_port", lambda platform: "1")
    monkeypatch.setattr(chrome_manager, "_find_chrome_with_debug_port", lambda platform: 123)
    monkeypatch.setattr(chrome_manager, "_chrome_user_data_dir_for_pid", lambda pid: actual)
    monkeypatch.setattr(chrome_manager, "_profile_metadata_for_platform", lambda *args, **kwargs: {})
    monkeypatch.setattr(chrome_manager, "upsert_browser_session", lambda **kwargs: {})
    monkeypatch.setattr(chrome_manager, "transition_browser_session", lambda *args, **kwargs: {})
    monkeypatch.setattr(
        chrome_manager,
        "_handle_same_platform_profile_switch",
        lambda platform, pid, actual_profile, expected_profile: switches.append((platform, pid, actual_profile, expected_profile)) or False,
    )
    monkeypatch.setattr(urllib.request, "urlopen", lambda *args, **kwargs: FakeResponse())
    monkeypatch.setitem(chrome_manager._MANAGED_CHROME_PROFILE_DIRS, "xhs", expected)

    result = chrome_manager._ensure_chrome_debugging_locked("xhs", target_profile_id="xhs-a")

    assert result is False
    assert len(switches) == 1
    assert switches[0][:2] == ("xhs", 123)
    assert chrome_manager._same_windows_path(switches[0][2], actual)
    assert chrome_manager._same_windows_path(switches[0][3], expected)


def test_xhs_auto_interaction_without_profile_or_evidence_never_brings_chrome_forward(monkeypatch) -> None:
    from runtime import chrome_manager

    front_calls = []
    monkeypatch.setattr(chrome_manager, "bring_chrome_to_front", lambda platform: front_calls.append(platform) or {"status": "ok"})

    missing_profile = chrome_manager.request_user_login("xhs", "login_required")
    missing_evidence = chrome_manager.request_user_login("xhs", "login_required", target_profile_id="xhs-a")

    assert missing_profile["reason_code"] == "PROFILE_BINDING_REQUIRED"
    assert missing_evidence["reason_code"] == "TRIGGER_EVIDENCE_REQUIRED"
    assert front_calls == []


def test_xhs_profile_binding_mismatch_never_brings_wrong_window_forward(monkeypatch, tmp_path) -> None:
    from runtime import chrome_manager

    expected = str(tmp_path / "account_a")
    actual = str(tmp_path / "account_b")
    released = []
    front_calls = []

    class Lease:
        acquired = True
        lease_id = "lease-1"

        def to_dict(self):
            return {"lease_id": self.lease_id}

    class Coordinator:
        def acquire_exclusive(self, *args, **kwargs):
            return Lease()

        def release(self, lease_id):
            released.append(lease_id)

    monkeypatch.setattr(chrome_manager, "get_runtime_lease_coordinator", lambda: Coordinator())
    monkeypatch.setattr(chrome_manager, "_managed_chrome_profile_dir", lambda *args, **kwargs: expected)
    monkeypatch.setattr(chrome_manager, "_profile_metadata_for_platform", lambda *args, **kwargs: {"profile_id": "xhs-a", "account_slot": "xhs_account_a"})
    monkeypatch.setattr(chrome_manager, "_chrome_debug_port", lambda platform: "12733")
    monkeypatch.setattr(chrome_manager, "_find_chrome_with_debug_port", lambda platform: 321)
    monkeypatch.setattr(chrome_manager, "_chrome_user_data_dir_for_pid", lambda pid: actual)
    monkeypatch.setattr(chrome_manager, "_ensure_chrome_debugging", lambda *args, **kwargs: True)
    monkeypatch.setattr(chrome_manager, "upsert_browser_session", lambda **kwargs: {"session_id": "xhs-session"})
    monkeypatch.setattr(chrome_manager, "transition_browser_session", lambda *args, **kwargs: {})
    monkeypatch.setattr(chrome_manager, "bring_chrome_to_front", lambda platform: front_calls.append(platform) or {"status": "ok"})
    chrome_manager._MANAGED_CHROME_PIDS.pop("xhs", None)
    chrome_manager._CHROME_MANUAL_INTERACTION_LEASES.pop("xhs", None)

    result = chrome_manager.request_user_login(
        "xhs",
        "security_verification",
        target_profile_id="xhs-a",
        trigger_evidence=["xhs_page_has_verify_prompt=true"],
        source="test",
    )

    assert result["reason_code"] == "PROFILE_BINDING_MISMATCH"
    assert released == ["lease-1"]
    assert front_calls == []
