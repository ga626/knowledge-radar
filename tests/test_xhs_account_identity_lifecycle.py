from __future__ import annotations

import json
from pathlib import Path
import urllib.request

from runtime.browser_sessions import BrowserSessionStore, manual_action_request_from_session
from runtime.xhs_account_identity import claim_xhs_account_identity, xhs_account_identity_summary


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
