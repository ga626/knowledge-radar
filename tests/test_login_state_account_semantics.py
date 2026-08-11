from __future__ import annotations

import json
import time
from pathlib import Path

from collectors.platform import boss, liepin, maimai, zhilian
from collectors.platform import xiaohongshu as xhs
from kr_core.collection import CollectionTrace, format_search_error
from runtime.browser_sessions import BrowserSessionStore
from runtime.profile_registry import raw_registry_for_platform, select_main_chain_profile
from runtime import recruitment_governance


def test_profile_registry_scopes_raw_rows_by_platform(monkeypatch, tmp_path: Path) -> None:
    profile_dir = tmp_path / "xhs-profile"
    profile_dir.mkdir()
    registry = tmp_path / "profile_registry.json"
    registry.write_text(
        """
{
  "schema": "knowledgeradar-profile-registry/v1",
  "accounts": [
    {"platform": "liepin", "account_slot": "liepin-a"},
    {"platform": "xiaohongshu", "account_slot": "xhs-a"}
  ],
  "profiles": [
    {"platform": "liepin", "profile_id": "liepin-profile", "account_slot": "liepin-a", "main_chain_allowed": true, "profile_dir": "missing"},
    {"platform": "xiaohongshu", "profile_id": "xhs-profile", "account_slot": "xhs-a", "main_chain_allowed": true, "profile_dir": "__PROFILE_DIR__"}
  ],
  "bindings": [
    {"platform": "liepin", "profile_id": "liepin-profile", "account_slot": "liepin-a"},
    {"platform": "xiaohongshu", "profile_id": "xhs-profile", "account_slot": "xhs-a"}
  ],
  "policy": {}
}
""".replace("__PROFILE_DIR__", str(profile_dir).replace("\\", "\\\\")),
        encoding="utf-8",
    )
    monkeypatch.setenv("KR_PROFILE_REGISTRY_PATH", str(registry))
    monkeypatch.setenv("KR_PROFILE_STATE_PATH", str(tmp_path / "state.json"))

    xhs_raw = raw_registry_for_platform("xhs")
    assert [row["account_slot"] for row in xhs_raw["accounts"]] == ["xhs-a"]
    assert [row["profile_id"] for row in xhs_raw["profiles"]] == ["xhs-profile"]
    assert select_main_chain_profile("xhs")["profile_id"] == "xhs-profile"


def test_browser_session_upsert_uses_profile_scope(tmp_path: Path) -> None:
    store = BrowserSessionStore(tmp_path / "sessions.json", tmp_path / "events.jsonl")
    first = store.upsert(platform="xhs", debug_port="12733", profile_dir=str(tmp_path / "a"), profile_id="xhs-a")
    second = store.upsert(platform="xhs", debug_port="12734", profile_dir=str(tmp_path / "b"), profile_id="xhs-b")
    reused = store.upsert(platform="xhs", debug_port="12733", profile_dir=str(tmp_path / "a"), profile_id="xhs-a")

    assert first["session_id"] != second["session_id"]
    assert first["session_id"] == reused["session_id"]


def test_manual_registry_platform_enters_chrome_runtime(monkeypatch, tmp_path: Path) -> None:
    profile_dir = tmp_path / "vip-profile"
    profile_dir.mkdir()
    registry = tmp_path / "profile_registry.json"
    registry.write_text(
        """
{
  "schema": "knowledgeradar-profile-registry/v1",
  "accounts": [],
  "profiles": [
    {"platform": "vip_oa", "profile_id": "vip-profile", "account_slot": "vip-a", "role": "manual_candidate", "channel_id": "authorized_browser_profile", "browser_base": "chrome_manual", "main_chain_allowed": false, "profile_dir": "__PROFILE_DIR__"}
  ],
  "bindings": [],
  "policy": {}
}
""".replace("__PROFILE_DIR__", str(profile_dir).replace("\\", "\\\\")),
        encoding="utf-8",
    )
    monkeypatch.setenv("KR_PROFILE_REGISTRY_PATH", str(registry))
    monkeypatch.setenv("KR_PROFILE_STATE_PATH", str(tmp_path / "state.json"))

    from runtime import chrome_manager

    assert "vip_oa" in chrome_manager.managed_browser_platforms()
    assert "xiaohongshu" not in chrome_manager.managed_browser_platforms()
    summary = chrome_manager.chrome_runtime_summary()
    assert "vip_oa" in summary["platforms"]
    assert summary["platforms"]["vip_oa"]["port"] == "12741"
    assert summary["platforms"]["vip_oa"]["manual_probe"]["startup_url"] == "https://www.cqvip.com/search?k=ai"


def test_stealth_chrome_flags_are_opt_in_for_managed_browsers() -> None:
    from runtime import chrome_manager

    assert chrome_manager._chrome_stealth_flags_for_platform("vip_oa") == []
    assert chrome_manager._chrome_stealth_flags_for_platform("socolar") == []
    assert chrome_manager._chrome_stealth_flags_for_platform("xhs") == []
    assert chrome_manager._chrome_stealth_flags_for_platform("boss") == []


def test_manual_browser_constants_enter_runtime_without_registry(monkeypatch, tmp_path: Path) -> None:
    registry = tmp_path / "profile_registry.json"
    registry.write_text(
        """
{
  "schema": "knowledgeradar-profile-registry/v1",
  "accounts": [],
  "profiles": [],
  "bindings": [],
  "policy": {}
}
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("KR_PROFILE_REGISTRY_PATH", str(registry))
    monkeypatch.setenv("KR_PROFILE_STATE_PATH", str(tmp_path / "state.json"))

    from runtime import chrome_manager

    assert "socolar" in chrome_manager.managed_browser_platforms()
    assert chrome_manager._chrome_debug_port("socolar") == "12747"
    assert chrome_manager._startup_url_for_platform("socolar") == "https://www.socolar.com/"
    profile_dir = Path(chrome_manager._managed_chrome_profile_dir("socolar"))
    assert profile_dir.parts[-4:] == ("browser_data", "profiles", "socolar", "account_a")


def test_manual_browser_interaction_launches_visible_managed_chrome(monkeypatch, tmp_path: Path) -> None:
    from runtime import chrome_manager
    from runtime.leases import RuntimeLeaseCoordinator

    profile_dir = tmp_path / "vip-profile"
    profile_dir.mkdir()
    monkeypatch.setattr(chrome_manager, "get_runtime_lease_coordinator", lambda: RuntimeLeaseCoordinator(tmp_path / "leases.sqlite3"))
    monkeypatch.setattr(chrome_manager, "_managed_chrome_profile_dir", lambda platform: str(profile_dir))
    monkeypatch.setattr(chrome_manager, "_profile_metadata_for_platform", lambda platform: {"profile_id": "vip-profile", "account_slot": "vip-a", "channel_id": "authorized_browser_profile"})
    calls = []

    def fake_ensure(platform: str, *, visible: bool = False, detach: bool = False) -> bool:
        calls.append((platform, visible, detach))
        chrome_manager._MANAGED_CHROME_PIDS[platform] = 12345
        return True

    monkeypatch.setattr(chrome_manager, "_ensure_chrome_debugging", fake_ensure)
    monkeypatch.setattr(chrome_manager, "bring_chrome_to_front", lambda platform: {"status": "ok", "platform": platform, "pid": 12345})
    monkeypatch.setattr(chrome_manager, "_find_chrome_with_debug_port", lambda platform: 12345)

    result = chrome_manager.request_browser_interaction("vip_oa", reason="manual_login")

    assert calls == [("vip_oa", False, False)]
    assert chrome_manager._CHROME_KEEP_ALIVE["vip_oa"] is True
    assert result["status"] == "waiting_for_user"
    assert result["lease"]["acquired"] is True
    assert result["next_step"] == "登录完成后，调用 health_check(mode='complete_browser_interaction:vip_oa') 恢复普通浏览器生命周期"


def test_manual_browser_interaction_busy_does_not_open_second_window(monkeypatch, tmp_path: Path) -> None:
    from runtime import chrome_manager
    from runtime.leases import RuntimeLeaseCoordinator

    coordinator = RuntimeLeaseCoordinator(tmp_path / "leases.sqlite3")
    monkeypatch.setattr(chrome_manager, "get_runtime_lease_coordinator", lambda: coordinator)
    monkeypatch.setattr(chrome_manager, "_managed_chrome_profile_dir", lambda platform: str(tmp_path / "vip-profile"))
    monkeypatch.setattr(chrome_manager, "_profile_metadata_for_platform", lambda platform: {"profile_id": "vip-profile", "account_slot": "vip-a"})

    first = coordinator.acquire_exclusive("manual_interaction", "vip_oa:vip-profile:vip-a:12741", owner={"client_id": "other"}, ttl_s=60)
    assert first.acquired is True
    calls = []
    monkeypatch.setattr(chrome_manager, "_ensure_chrome_debugging", lambda *args, **kwargs: calls.append((args, kwargs)) or True)

    result = chrome_manager.request_browser_interaction("vip_oa", reason="manual_login")

    assert result["status"] == "busy"
    assert result["validation_status"] == "EXPECTED_DEGRADED"
    assert calls == []


def test_complete_manual_browser_interaction_returns_to_operation_cleanup(monkeypatch, tmp_path: Path) -> None:
    from runtime import chrome_manager

    profile_dir = tmp_path / "vip-profile"
    profile_dir.mkdir()
    chrome_manager._CHROME_KEEP_ALIVE["vip_oa"] = True
    chrome_manager._MANAGED_CHROME_PIDS["vip_oa"] = 12345
    monkeypatch.setattr(chrome_manager, "_find_chrome_with_debug_port", lambda platform: 12345)
    monkeypatch.setattr(chrome_manager, "_minimize_chrome_windows", lambda pid: None)
    cleanup_calls = []
    monkeypatch.setattr(chrome_manager, "finish_chrome_automation", lambda platform, reason="": cleanup_calls.append((platform, reason)))

    result = chrome_manager.complete_browser_interaction("vip_oa", {"status": "ok"})

    assert result["status"] == "ok"
    assert chrome_manager._CHROME_KEEP_ALIVE["vip_oa"] is False
    assert cleanup_calls == [("vip_oa", "user_login_complete")]


def test_complete_manual_browser_interaction_records_auto_recovered_probe(monkeypatch, tmp_path: Path) -> None:
    from runtime import chrome_manager

    chrome_manager._CHROME_KEEP_ALIVE["vip_oa"] = True
    chrome_manager._MANAGED_CHROME_PIDS["vip_oa"] = 12345
    monkeypatch.setattr(chrome_manager, "_find_chrome_with_debug_port", lambda platform: 12345)
    monkeypatch.setattr(chrome_manager, "_minimize_chrome_windows", lambda pid: None)
    monkeypatch.setattr(chrome_manager, "finish_chrome_automation", lambda platform, reason="": None)
    transitions = []
    original_transition = chrome_manager.transition_browser_session

    def tracking_transition(*args, **kwargs):
        transitions.append((args, kwargs))
        return original_transition(*args, **kwargs)

    monkeypatch.setattr(chrome_manager, "transition_browser_session", tracking_transition)

    result = chrome_manager.complete_browser_interaction(
        "vip_oa",
        {"status": "ok", "manual_action_required": False, "platform_state": "search_ok"},
    )

    assert result["status"] == "ok"
    assert result["manual_state_auto_recovered"] is True
    assert any(call[1].get("event") == "manual_state_auto_recovered" for call in transitions)


def test_complete_xhs_interaction_recovers_only_the_verified_profile_account_state(monkeypatch) -> None:
    from runtime import chrome_manager

    transitions = []
    account_events = []
    monkeypatch.setattr(chrome_manager, "complete_user_login", lambda *args, **kwargs: {"status": "ok"})
    monkeypatch.setattr(chrome_manager, "transition_browser_session", lambda *args, **kwargs: transitions.append((args, kwargs)) or {})
    monkeypatch.setattr(
        chrome_manager,
        "record_xhs_account_event",
        lambda profile_id, reason, **kwargs: account_events.append((profile_id, reason, kwargs)) or {"status": "ok", "profile_id": profile_id, "state": "healthy"},
    )

    result = chrome_manager.complete_browser_interaction(
        "xhs",
        {"status": "ok", "manual_action_required": False, "platform_state": "authenticated"},
        profile_id="xhs-b",
        profile_dir="D:/profiles/xhs/b",
    )

    assert result["account_pool_recovery"] == {"status": "ok", "profile_id": "xhs-b", "state": "healthy"}
    assert account_events == [("xhs-b", "OK", {"last_tool": "complete_browser_interaction", "notes": ["authenticated_probe=true", "profile_bound=true"]})]
    assert transitions


def test_complete_xhs_interaction_does_not_clear_account_state_without_successful_probe(monkeypatch) -> None:
    from runtime import chrome_manager

    account_events = []
    monkeypatch.setattr(chrome_manager, "complete_user_login", lambda *args, **kwargs: {"status": "ok"})
    monkeypatch.setattr(chrome_manager, "transition_browser_session", lambda *args, **kwargs: {})
    monkeypatch.setattr(chrome_manager, "record_xhs_account_event", lambda *args, **kwargs: account_events.append((args, kwargs)) or {})

    result = chrome_manager.complete_browser_interaction(
        "xhs",
        {"status": "needs_interaction", "manual_action_required": True},
        profile_id="xhs-b",
    )

    assert "account_pool_recovery" not in result
    assert account_events == []


def test_browser_auth_probe_does_not_request_foreground_when_authenticated(monkeypatch, tmp_path: Path) -> None:
    from runtime import chrome_manager

    monkeypatch.setattr(chrome_manager, "managed_browser_platforms", lambda: ("vip_oa",))
    ensure_calls = []
    monkeypatch.setattr(chrome_manager, "_ensure_chrome_debugging", lambda platform, visible=False, detach=False: ensure_calls.append((platform, visible, detach)) or True)
    monkeypatch.setattr(chrome_manager, "_read_browser_cookie_names", lambda platform: ["journalOA-token", "_qdda"])
    cleanup_calls = []
    monkeypatch.setattr(chrome_manager, "finish_chrome_automation", lambda platform, reason="": cleanup_calls.append((platform, reason)))

    result = chrome_manager.probe_browser_auth("vip_oa")

    assert result["status"] == "ok"
    assert result["manual_action_required"] is False
    assert ensure_calls == [("vip_oa", False, False)]
    assert cleanup_calls == [("vip_oa", "browser_auth_probe")]


def test_browser_auth_probe_reports_needs_interaction_without_opening_browser(monkeypatch, tmp_path: Path) -> None:
    from runtime import chrome_manager

    monkeypatch.setattr(chrome_manager, "managed_browser_platforms", lambda: ("vip_oa",))
    monkeypatch.setattr(chrome_manager, "_ensure_chrome_debugging", lambda platform, visible=False, detach=False: True)
    monkeypatch.setattr(chrome_manager, "_read_browser_cookie_names", lambda platform: ["_qdda"])
    monkeypatch.setattr(chrome_manager, "finish_chrome_automation", lambda platform, reason="": None)

    result = chrome_manager.probe_browser_auth("vip_oa")

    assert result["status"] == "needs_interaction"
    assert result["manual_action_required"] is True
    assert result["recommended_action"] == "health_check(mode='request_browser_interaction:vip_oa:manual_login')"


def test_browser_auth_probe_supports_local_storage_login_state(monkeypatch) -> None:
    from runtime import chrome_manager

    monkeypatch.setattr(chrome_manager, "managed_browser_platforms", lambda: ("socolar",))
    monkeypatch.setattr(chrome_manager, "_ensure_chrome_debugging", lambda platform, visible=False, detach=False: True)
    monkeypatch.setattr(
        chrome_manager,
        "_read_browser_storage_names",
        lambda platform: {"local_storage": ["token", "refreshToken"], "session_storage": []},
    )
    monkeypatch.setattr(chrome_manager, "finish_chrome_automation", lambda platform, reason="": None)

    result = chrome_manager.probe_browser_auth("socolar")

    assert result["status"] == "ok"
    assert result["auth_state"] == "authenticated_with_socolar_token"
    assert result["manual_action_required"] is False
    assert result["observed_local_storage_keys"] == ["refreshToken", "token"]


def test_browser_auth_probe_reports_missing_local_storage_without_foreground(monkeypatch) -> None:
    from runtime import chrome_manager

    monkeypatch.setattr(chrome_manager, "managed_browser_platforms", lambda: ("socolar",))
    monkeypatch.setattr(chrome_manager, "_ensure_chrome_debugging", lambda platform, visible=False, detach=False: True)
    monkeypatch.setattr(
        chrome_manager,
        "_read_browser_storage_names",
        lambda platform: {"local_storage": ["token"], "session_storage": []},
    )
    monkeypatch.setattr(chrome_manager, "finish_chrome_automation", lambda platform, reason="": None)

    result = chrome_manager.probe_browser_auth("socolar")

    assert result["status"] == "needs_interaction"
    assert result["manual_action_required"] is True
    assert result["missing_local_storage_keys"] == ["refreshToken"]


def test_xhs_auth_probe_uses_platform_state_and_keeps_unconfirmed_state_non_manual(monkeypatch) -> None:
    from runtime import chrome_manager

    monkeypatch.setattr(chrome_manager, "_ensure_chrome_debugging", lambda *args, **kwargs: True)
    monkeypatch.setattr(chrome_manager, "finish_chrome_automation", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        xhs,
        "xiaohongshu_account_state",
        lambda chrome_debug_url: {"code": -1, "guest": False, "has_login_prompt": False, "has_verify_prompt": False},
    )

    result = chrome_manager.probe_browser_auth("xhs", target_profile_id="xhs-new-a-9341")

    assert result["status"] == "unknown"
    assert result["auth_state"] == "platform_state_unconfirmed"
    assert result["manual_action_required"] is False


def test_xhs_auth_probe_accepts_conservative_signed_in_ui_proof_when_api_identity_is_406(monkeypatch) -> None:
    from runtime import chrome_manager

    monkeypatch.setattr(chrome_manager, "_ensure_chrome_debugging", lambda *args, **kwargs: True)
    monkeypatch.setattr(chrome_manager, "finish_chrome_automation", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        xhs,
        "xiaohongshu_account_state",
        lambda chrome_debug_url: {
            "code": -1,
            "status": 406,
            "guest": False,
            "has_login_prompt": False,
            "has_verify_prompt": False,
            "ui_authenticated": True,
        },
    )

    result = chrome_manager.probe_browser_auth("xhs", target_profile_id="xhs-new-b-9342")

    assert result["status"] == "ok"
    assert result["auth_state"] == "authenticated_with_platform_confirmation"
    assert result["manual_action_required"] is False


def test_xhs_auth_probe_maps_explicit_security_verification_to_manual_action(monkeypatch) -> None:
    from runtime import chrome_manager

    monkeypatch.setattr(chrome_manager, "_ensure_chrome_debugging", lambda *args, **kwargs: True)
    monkeypatch.setattr(chrome_manager, "finish_chrome_automation", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        xhs,
        "xiaohongshu_account_state",
        lambda chrome_debug_url: {"code": -1, "guest": False, "has_login_prompt": False, "has_verify_prompt": True},
    )

    result = chrome_manager.probe_browser_auth("xhs", target_profile_id="xhs-new-a-9341")

    assert result["status"] == "needs_interaction"
    assert result["reason_code"] == "SECURITY_VERIFICATION"
    assert result["manual_action_required"] is True


def test_xhs_login_preflight_requests_managed_interaction(monkeypatch) -> None:
    calls = []

    def fake_request(platform: str, reason: str = "", **kwargs) -> dict:
        calls.append((platform, reason, kwargs))
        return {"status": "waiting_for_user", "platform": platform, "reason": reason, "session_id": "browser-test"}

    monkeypatch.setattr(xhs, "request_user_login", fake_request)
    monkeypatch.setattr(xhs, "_record_xhs_login_preflight", lambda *args, **kwargs: None)
    monkeypatch.setattr(xhs, "_selected_xhs_profile_id", lambda: "xhs-a")

    result = xhs._xhs_login_preflight_result(
        CollectionTrace("小红书", ["login_preflight"]),
        {"code": -1, "msg": "login required", "has_login_prompt": True},
    )

    assert calls == [("xhs", "login_required", {
        "target_profile_id": "xhs-a",
        "trigger_evidence": ["xhs_page_has_login_prompt=true"],
        "source": "search_xiaohongshu.login_preflight",
    })]
    assert result["metadata"]["manual_interaction"]["session_id"] == "browser-test"
    assert result["error"]["manual_interaction"]["status"] == "waiting_for_user"


def test_xhs_login_preflight_keeps_unconfirmed_state_in_background(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(xhs, "request_user_login", lambda *args, **kwargs: calls.append((args, kwargs)))
    monkeypatch.setattr(xhs, "_record_xhs_login_preflight", lambda *args, **kwargs: None)
    monkeypatch.setattr(xhs, "_selected_xhs_profile_id", lambda: "xhs-a")

    result = xhs._xhs_login_preflight_result(
        CollectionTrace("小红书", ["login_preflight"]),
        {"code": -1, "msg": "temporary state unknown", "guest": False},
    )

    assert calls == []
    assert result["error"]["platform_state"] == "auth_state_unconfirmed"
    assert result["error"]["manual_action_required"] is False


def test_boss_empty_search_is_not_manual_action(monkeypatch) -> None:
    monkeypatch.setattr(boss, "probe_boss_auth_state", lambda keepalive=True: {"status": "ok", "auth_state": "authenticated"})
    monkeypatch.setattr(boss, "check_search_gate", lambda platform, **kwargs: {"allowed": True})
    monkeypatch.setattr(boss, "_ensure_chrome_debugging", lambda platform: True)
    monkeypatch.setattr(boss, "finish_chrome_automation", lambda platform, reason="": None)
    monkeypatch.setattr(boss, "record_search_outcome", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        boss,
        "boss_search_via_cdp_state",
        lambda keyword, city="", limit=10: {
            "status": "failed",
            "items": [],
            "failure_type": "tool_failure_needs_repair",
            "platform_state": "search_route_unreadable",
        },
    )

    result = boss.legacy_search_boss("zzzz unlikely", city="北京", limit=3)

    assert result["error"]["failure_type"] == "tool_failure_needs_repair"
    assert result["error"]["platform_state"] == "search_route_unreadable"
    assert result["error"]["manual_action_required"] is False


def test_boss_login_search_is_manual_action(monkeypatch) -> None:
    monkeypatch.setattr(boss, "probe_boss_auth_state", lambda keepalive=True: {"status": "ok", "auth_state": "authenticated"})
    monkeypatch.setattr(boss, "check_search_gate", lambda platform, **kwargs: {"allowed": True})
    monkeypatch.setattr(boss, "_ensure_chrome_debugging", lambda platform: True)
    monkeypatch.setattr(boss, "finish_chrome_automation", lambda platform, reason="": None)
    monkeypatch.setattr(boss, "record_search_outcome", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        boss,
        "boss_search_via_cdp_state",
        lambda keyword, city="", limit=10: {"status": "needs_interaction", "items": [], "failure_type": "login_required", "platform_state": "login_required"},
    )

    result = boss.legacy_search_boss("Python", city="北京", limit=3)

    assert result["error"]["manual_action_required"] is True
    assert result["error"]["platform_state"] == "login_required"


def test_boss_auth_probe_login_required_reports_recovery_without_blocking_profile(monkeypatch) -> None:
    states = []

    monkeypatch.setattr(boss, "_ensure_chrome_debugging", lambda platform, visible=False, detach=False: True)
    monkeypatch.setattr(
        boss,
        "_parse_boss_cards_from_page",
        lambda port, keyword, city, limit: {
            "loginRequired": True,
            "url": "https://www.zhipin.com/web/user/",
            "title": "登录",
            "textSample": "扫码登录 密码登录",
        },
    )
    monkeypatch.setattr(boss, "_boss_cookie_quality", lambda port: {"status": "ok", "quality": "missing"})
    monkeypatch.setattr(boss, "_managed_chrome_profile_dir", lambda platform: "D:/tmp/boss")
    monkeypatch.setattr(boss, "finish_chrome_automation", lambda platform, reason="": None)
    monkeypatch.setattr(boss, "select_main_chain_profile", lambda platform: {"profile_id": "boss-main"})
    monkeypatch.setattr(boss, "record_profile_state", lambda *args, **kwargs: states.append((args, kwargs)))

    result = boss.probe_boss_auth_state()

    assert result["status"] == "needs_interaction"
    assert result["auth_state"] == "login_required"
    assert result["manual_action_required"] is True
    assert result["recommended_action"] == "health_check(mode='request_browser_interaction:boss:login_or_security_verification')"
    assert states[0][1]["state"] == "blocked"
    assert states[0][1]["manual_action_required"] is False
    assert states[0][1]["cooldown_seconds"] == 0


def test_boss_search_stops_before_search_when_auth_preflight_fails(monkeypatch) -> None:
    records = []

    monkeypatch.setattr(
        boss,
        "probe_boss_auth_state",
        lambda keepalive=True: {
            "status": "needs_interaction",
            "auth_state": "login_required",
            "failure_type": "login_required",
            "manual_action_required": True,
            "recommended_action": "health_check(mode='request_browser_interaction:boss:login_or_security_verification')",
        },
    )
    monkeypatch.setattr(boss, "record_search_outcome", lambda *args, **kwargs: records.append((args, kwargs)))
    monkeypatch.setattr(boss, "boss_search_via_cdp_state", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("search should not run")))

    result = boss.legacy_search_boss("Python", city="北京", limit=1)

    assert result["error"]["manual_action_required"] is True
    assert result["error"]["platform_state"] == "login_required"
    assert records == [(("boss", "failed", "login_required"), {"keyword": "Python", "city": "北京"})]


def test_search_recruitment_promotes_manual_interaction(monkeypatch) -> None:
    import server

    interactions = []
    class Lease:
        acquired = True
        lease_id = "test-boss-lease"

        def to_dict(self) -> dict:
            return {"acquired": True, "lease_id": self.lease_id}

    monkeypatch.setattr(
        server,
        "check_platform_admission",
        lambda platform, **kwargs: {"allowed": True, "admission": "open", "reason_code": "ok"},
    )
    monkeypatch.setattr(server, "acquire_platform_lease", lambda platform, **kwargs: Lease())
    monkeypatch.setattr(server, "release_platform_lease", lambda lease_id: None)
    monkeypatch.setattr(
        server,
        "_RECRUITMENT_ADAPTERS",
        {
            "boss": lambda request: {
                "items": [],
                "total": 0,
                "platform": "BOSS直聘",
                "error": {
                    "type": "login_required",
                    "failure_type": "login_required",
                    "platform_state": "login_required",
                    "manual_action_required": True,
                },
            }
        },
    )
    monkeypatch.setattr(server, "managed_browser_platforms", lambda: ("boss",))
    monkeypatch.setattr(
        server,
        "request_browser_interaction",
        lambda platform, reason, **kwargs: interactions.append((platform, reason, kwargs))
        or {"status": "waiting_for_user", "platform": platform, "reason": reason, "session_id": "boss-test"},
    )

    result = server.search_recruitment("boss", "Python", city="北京", limit=1)

    assert result["error"]["status_class"] == "NEEDS_INTERACTION"
    assert result["error"]["expected_degraded"] is False
    assert result["metadata"]["status_class"] == "NEEDS_INTERACTION"
    assert result["error"]["manual_interaction_envelope"]["platform"] == "boss"
    assert result["error"]["manual_interaction_envelope"]["original_tool"] == "search_recruitment"
    assert result["error"]["manual_interaction"]["status"] == "action_required_not_opened"
    assert result["error"]["manual_interaction"]["manual_open_mode"] == "health_check(mode='request_browser_interaction:boss:login_required')"
    assert interactions == []


def test_search_recruitment_does_not_open_browser_for_liepin_ambiguous(monkeypatch) -> None:
    import server

    interactions = []
    class Lease:
        acquired = True
        lease_id = "test-liepin-lease"

        def to_dict(self) -> dict:
            return {"acquired": True, "lease_id": self.lease_id}

    monkeypatch.setattr(
        server,
        "check_platform_admission",
        lambda platform, **kwargs: {"allowed": True, "admission": "open", "reason_code": "ok"},
    )
    monkeypatch.setattr(server, "acquire_platform_lease", lambda platform, **kwargs: Lease())
    monkeypatch.setattr(server, "release_platform_lease", lambda lease_id: None)
    monkeypatch.setattr(
        server,
        "_RECRUITMENT_ADAPTERS",
        {
            "liepin": lambda request: {
                "items": [],
                "total": 0,
                "platform": "猎聘",
                "error": {
                    "error": "猎聘搜索无结果",
                    "type": "ambiguous_page_state",
                    "failure_type": "ambiguous_page_state",
                    "platform_state": "suspected_manual_gate_not_confirmed",
                    "manual_action_required": False,
                    "manual_confidence": "suspected",
                },
            }
        },
    )
    monkeypatch.setattr(server, "managed_browser_platforms", lambda: ("liepin",))
    monkeypatch.setattr(
        server,
        "request_browser_interaction",
        lambda platform, reason: interactions.append((platform, reason))
        or {"status": "waiting_for_user", "platform": platform, "reason": reason, "session_id": "liepin-test"},
    )

    result = server.search_recruitment("liepin", "Python", city="杭州", limit=1)

    assert result["error"]["manual_action_required"] is False
    assert result["error"]["manual_confidence"] == "suspected"
    assert "manual_interaction" not in result["error"]
    assert interactions == []


def test_search_recruitment_uses_weak_web_fallback_for_native_route_failure(monkeypatch) -> None:
    import server

    class Lease:
        acquired = True
        lease_id = "test-boss-lease"

        def to_dict(self) -> dict:
            return {"acquired": True, "lease_id": self.lease_id}

    class WebResult:
        def to_mcp_dict(self) -> dict:
            return {
                "provider": "test",
                "items": [
                    {
                        "title": "AI产品经理 - 杭州",
                        "url": "https://www.zhipin.com/job_detail/1.html",
                        "snippet": "杭州 AI产品经理 招聘",
                    }
                ],
            }

    interactions = []
    monkeypatch.setattr(
        server,
        "check_platform_admission",
        lambda platform, **kwargs: {"allowed": True, "admission": "open", "reason_code": "ok"},
    )
    monkeypatch.setattr(server, "acquire_platform_lease", lambda platform, **kwargs: Lease())
    monkeypatch.setattr(server, "release_platform_lease", lambda lease_id: None)
    monkeypatch.setattr(
        server,
        "_RECRUITMENT_ADAPTERS",
        {
            "boss": lambda request: {
                "items": [],
                "total": 0,
                "platform": "BOSS直聘",
                "error": {
                    "error": "BOSS原生列表不可读",
                    "type": "tool_failure_needs_repair",
                    "failure_type": "tool_failure_needs_repair",
                    "platform_state": "search_route_unreadable",
                    "manual_action_required": False,
                },
            }
        },
    )
    monkeypatch.setattr(server, "managed_browser_platforms", lambda: ("boss",))
    monkeypatch.setattr(server, "request_browser_interaction", lambda platform, reason: interactions.append((platform, reason)))
    monkeypatch.setattr(server, "search_web", lambda request: WebResult())

    result = server.search_recruitment("boss", "AI产品经理", city="杭州", limit=3)

    assert result["items"]
    assert result["items"][0]["source"] == "web_search_fallback_after_native_route_failure"
    assert result["items"][0]["evidence_strength"] == "weak_open_index"
    assert result["metadata"]["native_route_failure"]["failure_type"] == "tool_failure_needs_repair"
    assert result["metadata"]["market_claim_allowed"] is False
    assert interactions == []


def test_zhilian_selector_miss_uses_weak_web_fallback(monkeypatch) -> None:
    import server

    class WebResult:
        def to_mcp_dict(self) -> dict:
            return {
                "provider": "test",
                "items": [
                    {
                        "title": "AI产品经理 - 智联杭州",
                        "url": "https://www.zhaopin.com/jobs/1.htm",
                        "snippet": "杭州 AI产品经理 招聘",
                    }
                ],
            }

    monkeypatch.setattr(
        server,
        "check_platform_admission",
        lambda platform, **kwargs: {"allowed": True, "admission": "open", "reason_code": "ok"},
    )
    monkeypatch.setattr(server, "managed_browser_platforms", lambda: ())
    monkeypatch.setattr(
        server,
        "_legacy_zhilian_from_request",
        lambda request: {
            "items": [],
            "total": 0,
            "platform": "智联招聘",
            "error": {
                "error": "智联选择器未命中",
                "type": "selector_miss",
                "failure_type": "selector_miss",
                "platform_state": "selector_miss",
                "manual_action_required": False,
            },
        },
    )
    monkeypatch.setattr(server, "search_web", lambda request: WebResult())

    result = server.search_recruitment("zhilian", "AI产品经理", city="杭州", limit=3)

    assert result["items"]
    assert result["items"][0]["source"] == "web_search_fallback_after_native_route_failure"
    assert result["items"][0]["evidence_strength"] == "weak_open_index"
    assert result["items"][0]["market_claim_allowed"] is False
    assert result["metadata"]["native_route_failure"]["failure_type"] == "selector_miss"
    assert result["metadata"]["market_claim_allowed"] is False


def test_zhilian_manual_state_does_not_use_web_fallback(monkeypatch) -> None:
    import server

    web_calls = []
    monkeypatch.setattr(
        server,
        "check_platform_admission",
        lambda platform, **kwargs: {"allowed": True, "admission": "open", "reason_code": "ok"},
    )
    monkeypatch.setattr(server, "managed_browser_platforms", lambda: ())
    monkeypatch.setattr(
        server,
        "_legacy_zhilian_from_request",
        lambda request: {
            "items": [],
            "total": 0,
            "platform": "智联招聘",
            "error": {
                "error": "需要登录",
                "type": "login_required",
                "failure_type": "login_required",
                "platform_state": "login_required",
                "manual_action_required": True,
            },
        },
    )
    monkeypatch.setattr(server, "search_web", lambda request: web_calls.append(request) or (_ for _ in ()).throw(AssertionError("no web fallback for manual state")))

    result = server.search_recruitment("zhilian", "AI产品经理", city="杭州", limit=3)

    assert result["items"] == []
    assert result["error"]["manual_action_required"] is True
    assert result["error"]["failure_type"] == "login_required"
    assert web_calls == []


def test_search_recruitment_rejects_busy_platform_lease(monkeypatch) -> None:
    import server

    monkeypatch.setattr(
        server,
        "check_platform_admission",
        lambda platform, **kwargs: {"allowed": True, "admission": "open", "reason_code": "ok"},
    )

    class BusyLease:
        acquired = False
        retry_after_s = 17

        def to_dict(self) -> dict:
            return {"acquired": False, "reason": "lease_unavailable", "retry_after_s": 17}

    monkeypatch.setattr(server, "acquire_platform_lease", lambda platform, **kwargs: BusyLease())
    monkeypatch.setattr(server, "release_platform_lease", lambda lease_id: (_ for _ in ()).throw(AssertionError("no lease acquired")))
    monkeypatch.setattr(
        server,
        "_RECRUITMENT_ADAPTERS",
        {"liepin": lambda request: (_ for _ in ()).throw(AssertionError("adapter should not run"))},
    )

    result = server.search_recruitment("liepin", "Python", city="杭州", limit=1)

    assert result["error"]["failure_type"] == "platform_lease_busy"
    assert result["error"]["reason_code"] == "platform_lease_busy"
    assert result["error"]["retry_after_s"] == 17
    assert result["market_claim_allowed"] is False


def test_recruitment_admission_blocks_existing_manual_session(monkeypatch) -> None:
    monkeypatch.setattr(
        recruitment_governance,
        "check_search_gate",
        lambda platform, **kwargs: {"allowed": True, "reason": "ok"},
    )
    monkeypatch.setattr(
        recruitment_governance,
        "browser_sessions_summary",
        lambda limit=10: {
            "pending_interactions": [
                {"platform": "liepin", "status": "waiting_for_user", "reason_code": "platform_verification_required"}
            ]
        },
    )
    monkeypatch.setattr(
        recruitment_governance,
        "_recover_pending_manual_session",
        lambda platform, pending: {"recovered": False, "probe": {"status": "needs_interaction"}},
    )

    result = recruitment_governance.check_platform_admission("liepin", keyword="Python", city="杭州")

    assert result["allowed"] is False
    assert result["reason_code"] == "pending_manual_interaction"
    assert result["manual_action_required"] is True
    assert result["market_claim_allowed"] is False


def test_recruitment_admission_auto_recovers_authenticated_manual_session(monkeypatch) -> None:
    monkeypatch.setattr(
        recruitment_governance,
        "check_search_gate",
        lambda platform, **kwargs: {"allowed": True, "reason": "ok"},
    )
    monkeypatch.setattr(
        recruitment_governance,
        "browser_sessions_summary",
        lambda limit=10: {
            "pending_interactions": [
                {"platform": "liepin", "status": "waiting_for_user", "reason_code": "login_or_security_verification"}
            ]
        },
    )
    monkeypatch.setattr(
        recruitment_governance,
        "_recover_pending_manual_session",
        lambda platform, pending: {"recovered": True, "probe": {"status": "ok", "manual_action_required": False}},
    )

    result = recruitment_governance.check_platform_admission("liepin", keyword="Python", city="杭州")

    assert result["allowed"] is True
    assert result["reason_code"] == "pending_manual_interaction_auto_recovered"
    assert result["manual_action_required"] is False


def test_liepin_city_filter_drops_explicit_mismatch() -> None:
    items = [
        {"title": "AI 产品经理", "area": "深圳"},
        {"title": "AI 产品经理", "area": "杭州"},
        {"title": "AI 产品经理", "area": "远程"},
        {"title": "AI 产品经理", "area": ""},
        {"title": "行政【合肥-瑶海区】", "area": ""},
    ]

    filtered, summary = liepin._apply_city_filter(items, "杭州")

    assert [item["city_match"] for item in filtered] == ["match", "remote", "unknown"]
    assert summary["dropped_count"] == 2


def test_liepin_detail_runtime_evaluate_error_is_collector_script_error(monkeypatch) -> None:
    monkeypatch.setattr(liepin, "_static_liepin_detail", lambda url: None)
    monkeypatch.setattr(liepin, "_ensure_chrome_debugging", lambda platform: True)

    class Proc:
        returncode = 0
        stderr = ""
        stdout = json.dumps(
            {
                "platform": "猎聘",
                "url": "https://www.liepin.com/job/1977755243.shtml",
                "status": "failed",
                "error": "SyntaxError: Invalid regular expression flags",
                "failure_type": "collector_script_error",
                "platform_state": "collector_script_error",
                "manual_action_required": False,
            },
            ensure_ascii=False,
        )

    monkeypatch.setattr(liepin, "silent_subprocess_run", lambda *args, **kwargs: Proc())

    result = liepin.liepin_detail_via_cdp("https://www.liepin.com/job/1977755243.shtml")

    assert result["failure_type"] == "collector_script_error"
    assert result["manual_action_required"] is False
    assert result["recommended_fallback"] == "static_detail_retry"


def test_liepin_soft_security_prompt_with_results_is_readable() -> None:
    result = liepin._finalize_liepin_search_state(
        {
            "items": [
                {
                    "title": "Python 工程师",
                    "area": "杭州",
                    "url": "https://www.liepin.com/job/1",
                }
            ],
            "blocked": True,
            "loginRequired": False,
            "cardCount": 12,
            "jobLinkCount": 8,
        },
        city="杭州",
        limit=3,
    )

    assert result["status"] == "ok"
    assert result["manual_action_required"] is False
    assert result["warning_type"] == "soft_security_prompt_with_results"
    assert result["page_state"]["result_readability"] == "readable"
    assert result["items"][0]["title"] == "Python 工程师"


def test_boss_soft_security_prompt_with_results_is_readable(monkeypatch) -> None:
    monkeypatch.setattr(boss, "_ensure_chrome_debugging", lambda platform: True)
    monkeypatch.setattr(
        boss,
        "_parse_boss_cards_from_page",
        lambda *args, **kwargs: {
            "items": [{"title": "Python 工程师", "company": "ACME", "area": "杭州", "url": "https://www.zhipin.com/job_detail/1"}],
            "blocked": True,
            "loginRequired": False,
            "cardCount": 1,
            "title": "安全验证 - BOSS直聘",
            "url": "https://www.zhipin.com/web/geek/jobs",
            "textSample": "安全验证 Python 工程师",
        },
    )

    result = boss.boss_search_via_cdp_state("Python", "杭州", 3)

    assert result["status"] == "ok"
    assert result["manual_action_required"] is False
    assert result["warning_type"] == "soft_security_prompt_with_results"
    assert result["items"][0]["title"] == "Python 工程师"
    assert result["items"][0]["salary_claim_allowed"] is False
    assert result["items"][0]["field_confidence"]["salary"] == "low_search_card"


def test_maimai_soft_security_prompt_with_results_is_readable(monkeypatch) -> None:
    class Proc:
        returncode = 0
        stderr = ""
        stdout = """
        {
          "items": [{"title": "AI 产品经理", "company": "ACME", "area": "杭州"}],
          "blocked": true,
          "needLogin": false,
          "url": "https://maimai.cn/web/gear/test/job/search",
          "title": "脉脉",
          "textSample": "访问被拦截 AI 产品经理"
        }
        """

    monkeypatch.setattr(maimai, "silent_subprocess_run", lambda *args, **kwargs: Proc())

    result = maimai.maimai_search_via_cdp_state("AI", 3)

    assert result["status"] == "ok"
    assert result["manual_action_required"] is False
    assert result["warning_type"] == "soft_security_prompt_with_results"
    assert result["items"][0]["title"] == "AI 产品经理"


def test_zhilian_soft_login_prompt_with_results_is_readable(monkeypatch) -> None:
    class Proc:
        returncode = 0
        stderr = ""
        stdout = """
        {
          "items": [{"title": "数据分析师", "company": "ACME", "area": "杭州"}],
          "blocked": false,
          "needLogin": true,
          "rateLimited": false,
          "url": "https://sou.zhaopin.com/",
          "title": "智联招聘",
          "textSample": "登录 数据分析师 招聘"
        }
        """

    monkeypatch.setattr(zhilian, "_ensure_chrome_debugging", lambda platform: True)
    monkeypatch.setattr(zhilian, "finish_chrome_automation", lambda platform, reason="": None)
    monkeypatch.setattr(zhilian, "silent_subprocess_run", lambda *args, **kwargs: Proc())

    result = zhilian.zhilian_search_via_cdp_state("数据分析", "杭州", 3)

    assert result["status"] == "ok"
    assert result["manual_action_required"] is False
    assert result["warning_type"] == "soft_login_prompt_with_results"
    assert result["items"][0]["title"] == "数据分析师"


def test_zhilian_cdp_unavailable_is_not_empty_result(monkeypatch) -> None:
    class Proc:
        returncode = 1
        stderr = "TypeError: fetch failed ECONNREFUSED 127.0.0.1:12749"
        stdout = ""

    monkeypatch.setattr(zhilian, "_ensure_chrome_debugging", lambda platform: True)
    monkeypatch.setattr(zhilian, "finish_chrome_automation", lambda platform, reason="": None)
    monkeypatch.setattr(zhilian, "silent_subprocess_run", lambda *args, **kwargs: Proc())

    result = zhilian.zhilian_search_via_cdp_state("数据分析", "杭州", 3)

    assert result["failure_type"] == "cdp_unavailable"
    assert result["platform_state"] == "cdp_unavailable"


def test_zhilian_cdp_no_output_is_connection_layer_failure(monkeypatch) -> None:
    class Proc:
        returncode = 0
        stderr = ""
        stdout = ""

    monkeypatch.setattr(zhilian, "_ensure_chrome_debugging", lambda platform: True)
    monkeypatch.setattr(zhilian, "finish_chrome_automation", lambda platform, reason="": None)
    monkeypatch.setattr(zhilian, "silent_subprocess_run", lambda *args, **kwargs: Proc())

    result = zhilian.zhilian_search_via_cdp_state("数据分析", "杭州", 3)

    assert result["failure_type"] == "cdp_no_output"
    assert result["manual_action_required"] is False
    assert result["diagnostics"]["layers"]["connection"]["status"] == "failed"


def test_zhilian_runtime_evaluate_error_is_not_empty_result(monkeypatch) -> None:
    class Proc:
        returncode = 0
        stderr = ""
        stdout = '{"items":[],"error":"runtime_evaluate_exception","exceptionDescription":"ReferenceError"}'

    monkeypatch.setattr(zhilian, "_ensure_chrome_debugging", lambda platform: True)
    monkeypatch.setattr(zhilian, "finish_chrome_automation", lambda platform, reason="": None)
    monkeypatch.setattr(zhilian, "silent_subprocess_run", lambda *args, **kwargs: Proc())

    result = zhilian.zhilian_search_via_cdp_state("数据分析", "杭州", 3)

    assert result["status"] == "failed"
    assert result["failure_type"] == "runtime_evaluate_exception"
    assert result["manual_action_required"] is False


def test_zhilian_selector_miss_is_classified(monkeypatch) -> None:
    class Proc:
        returncode = 0
        stderr = ""
        stdout = """
        {
          "items": [],
          "blocked": false,
          "needLogin": false,
          "rateLimited": false,
          "url": "https://sou.zhaopin.com/",
          "title": "智联招聘",
          "textSample": "职位 公司 薪资 招聘"
        }
        """

    monkeypatch.setattr(zhilian, "_ensure_chrome_debugging", lambda platform: True)
    monkeypatch.setattr(zhilian, "finish_chrome_automation", lambda platform, reason="": None)
    monkeypatch.setattr(zhilian, "silent_subprocess_run", lambda *args, **kwargs: Proc())

    result = zhilian.legacy_search_zhilian("数据分析", "杭州", 3)

    assert result["error"]["failure_type"] == "selector_miss"
    assert result["error"]["failure_class"] == "tool_error"
    assert result["market_claim_allowed"] is False


def test_zhilian_unknown_city_stops_before_browser(monkeypatch) -> None:
    monkeypatch.setattr(zhilian, "_ensure_chrome_debugging", lambda platform: (_ for _ in ()).throw(AssertionError("browser should not start")))

    result = zhilian.legacy_search_zhilian("数据分析", "火星", 3)

    assert result["error"]["failure_type"] == "city_mapping_missing"
    assert result["error"]["manual_action_required"] is False
    assert result["market_claim_allowed"] is False


def test_liepin_weak_security_prompt_without_results_is_not_manual() -> None:
    result = liepin._finalize_liepin_search_state(
        {
            "items": [],
            "blocked": True,
            "loginRequired": False,
            "cardCount": 0,
            "jobLinkCount": 0,
        },
        city="杭州",
        limit=3,
    )

    assert result["status"] == "ambiguous"
    assert result["failure_type"] == "ambiguous_page_state"
    assert result["platform_state"] == "suspected_manual_gate_not_confirmed"
    assert result["manual_action_required"] is False
    assert result["manual_confidence"] == "suspected"


def test_liepin_hard_security_prompt_without_results_requires_interaction() -> None:
    result = liepin._finalize_liepin_search_state(
        {
            "items": [],
            "blocked": True,
            "loginRequired": False,
            "cardCount": 0,
            "jobLinkCount": 0,
            "captchaElementCount": 1,
            "page_state": {
                "blocked_marker": True,
                "captcha_element_count": 1,
                "security_evidence_strength": "strong",
            },
        },
        city="杭州",
        limit=3,
    )

    assert result["status"] == "needs_interaction"
    assert result["failure_type"] == "platform_verification_required"
    assert result["platform_state"] == "hard_security_block"
    assert result["manual_action_required"] is True
    assert result["manual_confidence"] == "confirmed"


def test_liepin_login_prompt_without_results_requires_interaction() -> None:
    result = liepin._finalize_liepin_search_state(
        {
            "items": [],
            "blocked": False,
            "loginRequired": True,
            "cardCount": 0,
            "jobLinkCount": 0,
            "loginModalCount": 1,
            "page_state": {
                "login_marker": True,
                "login_modal_count": 1,
                "login_evidence_strength": "strong",
            },
        },
        city="杭州",
        limit=3,
    )

    assert result["status"] == "needs_interaction"
    assert result["failure_type"] == "login_required"
    assert result["platform_state"] == "login_required"
    assert result["manual_action_required"] is True
    assert result["manual_confidence"] == "confirmed"


def test_liepin_detail_readability_beats_security_marker(monkeypatch) -> None:
    jd = (
        "职位介绍 岗位职责：负责 KnowledgeRadar 平台后端研发、数据采集链路治理、状态机建模和质量门禁建设。"
        "任职要求：熟悉 Python、浏览器自动化、异步任务和工程化测试，能够独立推进复杂系统问题定位。"
        "工作内容：维护招聘搜索、详情抽取、登录态预检和人工交互恢复流程，持续补充回归测试和质量报告。"
        "岗位要求：理解 MCP、Agent 工具调用、页面状态分类和证据链管理，能把软提示和硬阻断区分清楚。"
    )

    class Proc:
        returncode = 0
        stderr = ""
        stdout = json.dumps(
            {
                "platform": "猎聘",
                "url": "https://www.liepin.com/job/1",
                "title": "高级 Python 工程师",
                "salary": "30-50k",
                "jd": jd,
                "blocked": True,
                "loginLike": False,
                "text_length": 900,
                "page_title": "安全验证",
            }
        )

    monkeypatch.setattr(liepin, "_ensure_chrome_debugging", lambda platform: True)
    monkeypatch.setattr(liepin, "silent_subprocess_run", lambda *args, **kwargs: Proc())

    result = liepin.liepin_detail_via_cdp("https://www.liepin.com/job/1")

    assert result["status"] == "ok"
    assert result["manual_action_required"] is False
    assert result["warning_type"] == "soft_security_prompt_with_results"
    assert "岗位职责" in result["jd"]


def test_search_error_normalizes_failure_type_when_type_missing() -> None:
    result = format_search_error(
        "猎聘",
        {
            "error": "猎聘搜索无结果",
            "hint": "页面异常，未自动判定登录或安全验证。",
            "failure_type": "city_mismatch",
            "platform_state": "city_mismatch",
            "manual_action_required": False,
        },
        strategy="chrome_cdp_page",
    )

    assert result["error"]["type"] == "city_mismatch"
    assert "login_required" not in result["error"]["failure_tags"]
    assert "anti_bot_verification" not in result["error"]["failure_tags"]


def test_search_error_preserves_ambiguous_page_state() -> None:
    result = format_search_error(
        "猎聘",
        {
            "error": "猎聘搜索无结果",
            "failure_type": "ambiguous_page_state",
            "platform_state": "suspected_manual_gate_not_confirmed",
            "manual_action_required": False,
            "manual_confidence": "suspected",
        },
        strategy="chrome_cdp_page",
    )

    assert result["error"]["type"] == "ambiguous_page_state"
    assert result["error"]["manual_action_required"] is False
    assert result["error"]["manual_confidence"] == "suspected"


def test_boss_failure_budget_is_scoped_by_keyword_and_city(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("KR_TASK_DB_PATH", str(tmp_path / "gate.sqlite3"))
    monkeypatch.setitem(
        recruitment_governance.PLATFORM_CONFIG,
        "boss",
        {
            "search_cooldown_s": 0,
            "min_search_interval_s": 0,
            "max_searches_per_hour": 20,
            "max_failures_per_hour": 3,
        },
    )

    for _ in range(3):
        recruitment_governance.record_search_outcome("boss", "failed", "login_required", keyword="Python", city="北京")

    same_scope = recruitment_governance.check_search_gate("boss", keyword="Python", city="北京")
    other_city = recruitment_governance.check_search_gate("boss", keyword="Python", city="杭州")

    assert same_scope["allowed"] is False
    assert "失败次数已达上限" in same_scope["reason"]
    assert other_city["allowed"] is True


def test_boss_login_failure_does_not_hard_cooldown_recovery(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("KR_TASK_DB_PATH", str(tmp_path / "gate.sqlite3"))
    monkeypatch.setitem(
        recruitment_governance.PLATFORM_CONFIG,
        "boss",
        {
            "search_cooldown_s": 1800,
            "min_search_interval_s": 0,
            "max_searches_per_hour": 20,
            "max_failures_per_hour": 3,
        },
    )

    recruitment_governance.record_search_outcome("boss", "failed", "login_required", keyword="Python", city="北京")

    gate = recruitment_governance.check_search_gate("boss", keyword="Python", city="北京")

    assert gate["allowed"] is True
    assert gate["failures_this_hour"] == 1


def test_tool_route_failure_does_not_hard_cooldown_recovery(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("KR_TASK_DB_PATH", str(tmp_path / "gate.sqlite3"))
    monkeypatch.setitem(
        recruitment_governance.PLATFORM_CONFIG,
        "liepin",
        {
            "search_cooldown_s": 1800,
            "min_search_interval_s": 0,
            "max_searches_per_hour": 20,
            "max_failures_per_hour": 10,
        },
    )

    recruitment_governance.record_search_outcome("liepin", "failed", "tool_failure_needs_repair", keyword="AI产品经理", city="杭州")

    gate = recruitment_governance.check_search_gate("liepin", keyword="AI产品经理", city="杭州")

    assert gate["allowed"] is True
    assert gate["failures_this_hour"] == 1


def test_legacy_tool_route_cooldown_row_is_ignored(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "gate.sqlite3"
    monkeypatch.setenv("KR_TASK_DB_PATH", str(db_path))
    monkeypatch.setitem(
        recruitment_governance.PLATFORM_CONFIG,
        "liepin",
        {
            "search_cooldown_s": 1800,
            "min_search_interval_s": 0,
            "max_searches_per_hour": 20,
            "max_failures_per_hour": 10,
        },
    )
    conn = recruitment_governance._get_conn()
    now = time.time()
    conn.execute(
        """
        INSERT INTO recruitment_search_gate
          (platform, ts, outcome, reason, cooldown_until, account_slot, keyword_norm, city_norm)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("liepin", now, "failed", "tool_failure_needs_repair", now + 1800, "", "ai产品经理", "杭州"),
    )
    conn.commit()
    conn.close()

    gate = recruitment_governance.check_search_gate("liepin", keyword="AI产品经理", city="杭州")

    assert gate["allowed"] is True
    assert gate["cooldown_remaining_s"] == 0


def test_recruitment_cooldown_doubles_previous_failure(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("KR_TASK_DB_PATH", str(tmp_path / "gate.sqlite3"))
    monkeypatch.setitem(
        recruitment_governance.PLATFORM_CONFIG,
        "boss",
        {
            "search_cooldown_s": 10,
            "search_cooldown_max_s": 40,
            "min_search_interval_s": 0,
            "max_searches_per_hour": 20,
            "max_failures_per_hour": 10,
        },
    )

    recruitment_governance.record_search_outcome("boss", "failed", "temporary_blocked", keyword="Python", city="北京")
    first = recruitment_governance.check_search_gate("boss", keyword="Python", city="北京")
    assert first["cooldown_remaining_s"] <= 10

    monkeypatch.setenv("KR_TASK_DB_PATH", str(tmp_path / "gate.sqlite3"))
    recruitment_governance.record_search_outcome("boss", "failed", "temporary_blocked", keyword="Python", city="北京")
    second = recruitment_governance.check_search_gate("boss", keyword="Python", city="北京")

    assert second["allowed"] is False
    assert second["cooldown_remaining_s"] > 10
    assert second["cooldown_remaining_s"] <= 20
    assert second["next_retry_at"] > 0


def test_liepin_and_maimai_empty_search_are_not_manual_action(monkeypatch) -> None:
    for module in (liepin, maimai):
        monkeypatch.setattr(module, "check_search_gate", lambda platform, **kwargs: {"allowed": True})
        monkeypatch.setattr(module, "_ensure_chrome_debugging", lambda platform: True)
        monkeypatch.setattr(module, "finish_chrome_automation", lambda platform, reason="": None)
        monkeypatch.setattr(module, "record_search_outcome", lambda *a, **k: None)
    monkeypatch.setattr(liepin, "liepin_search_via_cdp_state", lambda keyword, city="", limit=10: {"status": "empty", "items": []})
    monkeypatch.setattr(maimai, "maimai_search_via_cdp_state", lambda keyword, limit=10: {"status": "empty", "items": []})

    liepin_result = liepin.legacy_search_liepin("zzzz unlikely", "", 3)
    maimai_result = maimai.legacy_search_maimai("zzzz unlikely", 3)

    assert liepin_result["error"]["manual_action_required"] is False
    assert maimai_result["error"]["manual_action_required"] is False


def test_boss_search_state_marks_unreadable_route_as_tool_failure(monkeypatch) -> None:
    monkeypatch.setattr(boss, "_ensure_chrome_debugging", lambda platform: True)
    monkeypatch.setattr(
        boss,
        "_parse_boss_cards_from_page",
        lambda port, keyword, city, limit: {
            "items": [],
            "blocked": False,
            "loginRequired": False,
            "cardCount": 0,
            "title": "BOSS直聘",
            "url": "https://www.zhipin.com/web/geek/jobs?query=Python&city=101210100",
            "textSample": "Python 杭州 推荐职位",
            "waitState": {"empty": False},
        },
    )

    result = boss.boss_search_via_cdp_state("Python", "杭州", 3)

    assert result["status"] == "failed"
    assert result["failure_type"] == "tool_failure_needs_repair"
    assert result["platform_state"] == "search_route_unreadable"
    assert result["manual_action_required"] is False
    assert result["page_state"]["route_probe"]["route_decision"] == "needs_route_repair"


def test_liepin_search_state_marks_unreadable_route_as_tool_failure() -> None:
    result = liepin._finalize_liepin_search_state(
        {
            "items": [],
            "blocked": False,
            "loginRequired": False,
            "cardCount": 0,
            "jobLinkCount": 0,
            "url": "https://www.liepin.com/zhaopin/?key=Python&city=杭州",
            "title": "猎聘",
            "textSample": "Python 杭州 推荐",
            "keyword": "Python",
            "waitState": {"empty": False},
        },
        city="杭州",
        limit=3,
    )

    assert result["status"] == "failed"
    assert result["failure_type"] == "tool_failure_needs_repair"
    assert result["platform_state"] == "search_route_unreadable"
    assert result["manual_action_required"] is False
    assert result["page_state"]["route_probe"]["route_decision"] == "needs_route_repair"


def test_liepin_unknown_city_stops_before_browser(monkeypatch) -> None:
    monkeypatch.setattr(liepin, "_ensure_chrome_debugging", lambda platform: (_ for _ in ()).throw(AssertionError("browser should not start")))

    result = liepin.legacy_search_liepin("AI产品经理", city="火星", limit=3)

    assert result["error"]["failure_type"] == "city_mapping_missing"
    assert result["error"]["manual_action_required"] is False
    assert result["failure_class"] == "city_mapping_missing"
    assert result["market_claim_allowed"] is False
    assert result["metadata"]["collection"]["attempts"][0]["error_type"] == "city_mapping_missing"


def test_boss_search_state_accepts_network_route_items(monkeypatch) -> None:
    monkeypatch.setattr(boss, "_ensure_chrome_debugging", lambda platform: True)
    monkeypatch.setattr(
        boss,
        "_parse_boss_cards_from_page",
        lambda port, keyword, city, limit: {
            "items": [
                {
                    "title": "AI产品经理",
                    "company": "测试科技",
                    "area": "杭州",
                    "url": "https://www.zhipin.com/job_detail/abc.html",
                    "source": "boss_network_search",
                    "evidence_strength": "strong_platform_network",
                }
            ],
            "blocked": False,
            "loginRequired": False,
            "cardCount": 0,
            "title": "BOSS直聘",
            "url": "https://www.zhipin.com/web/geek/jobs?query=AI产品经理&city=101210100",
            "textSample": "AI产品经理 杭州 推荐职位",
            "waitState": {"empty": False},
        },
    )

    result = boss.boss_search_via_cdp_state("AI产品经理", "杭州", 3)

    assert result["status"] == "ok"
    assert result["items"][0]["source"] == "boss_network_search"
    assert result["failure_type"] == ""
    assert result["manual_action_required"] is False
    assert result["page_state"]["route_probe"]["route_decision"] == "network_items"


def test_liepin_search_state_accepts_network_route_items() -> None:
    result = liepin._finalize_liepin_search_state(
        {
            "items": [
                {
                    "title": "AI平台产品经理",
                    "company": "杭州样例公司",
                    "area": "杭州",
                    "url": "https://www.liepin.com/job/1.shtml",
                    "source": "liepin_network_search",
                    "evidence_strength": "strong_platform_network",
                }
            ],
            "blocked": False,
            "loginRequired": False,
            "cardCount": 0,
            "jobLinkCount": 0,
            "url": "https://www.liepin.com/zhaopin/?key=AI产品经理&city=杭州",
            "title": "猎聘",
            "textSample": "AI产品经理 杭州 推荐",
            "keyword": "AI产品经理",
            "waitState": {"empty": False},
        },
        city="杭州",
        limit=3,
    )

    assert result["status"] == "ok"
    assert result["items"][0]["source"] == "liepin_network_search"
    assert result["failure_type"] == ""
    assert result["manual_action_required"] is False
    assert result["page_state"]["route_probe"]["route_decision"] == "network_items"
