from __future__ import annotations

from runtime import chrome_manager
from runtime import xhs_auth_watcher
from collectors.platform import xiaohongshu


def test_xhs_profile_resource_uses_independent_registry_ports(monkeypatch) -> None:
    rows = [
        {"profile_id": "xhs-a", "account_slot": "xhs_account_a", "debug_port": "12735"},
        {"profile_id": "xhs-b", "account_slot": "xhs_account_b", "debug_port": "12733"},
        {"profile_id": "xhs-c", "account_slot": "xhs_account_c", "debug_port": "12736"},
    ]
    monkeypatch.setattr(chrome_manager, "raw_registry_for_platform", lambda _platform: {"profiles": rows})

    assert chrome_manager._browser_resource_key("xhs", "xhs-a") == "xhs:xhs-a"
    assert chrome_manager._chrome_debug_port("xhs:xhs-a") == "12735"
    assert chrome_manager._chrome_debug_port("xhs:xhs-b") == "12733"
    assert chrome_manager._chrome_debug_port("xhs:xhs-c") == "12736"


def test_bare_xhs_helper_reuses_the_single_active_profile_resource(monkeypatch) -> None:
    monkeypatch.setattr(chrome_manager, "_MANAGED_CHROME_PROCS", {})
    monkeypatch.setattr(chrome_manager, "_MANAGED_CHROME_PROFILE_DIRS", {"xhs:xhs-b": "D:/profiles/xhs/b"})
    monkeypatch.setattr(chrome_manager, "_MANAGED_CHROME_PIDS", {"xhs:xhs-b": 12345})
    monkeypatch.setattr(chrome_manager, "_CHROME_IDLE_TIMERS", {})
    monkeypatch.setattr(chrome_manager, "_CHROME_KEEP_ALIVE", {"xhs:xhs-b": True})
    monkeypatch.setattr(chrome_manager, "_CHROME_ACTIVE_OPERATIONS", {})
    monkeypatch.setattr(chrome_manager, "_CHROME_MANUAL_INTERACTION_LEASES", {})

    assert chrome_manager._browser_resource_key("xhs") == "xhs:xhs-b"


def test_bare_xhs_helper_never_guesses_between_multiple_active_profiles(monkeypatch) -> None:
    monkeypatch.setattr(chrome_manager, "_MANAGED_CHROME_PROCS", {})
    monkeypatch.setattr(
        chrome_manager,
        "_MANAGED_CHROME_PROFILE_DIRS",
        {"xhs:xhs-a": "D:/profiles/xhs/a", "xhs:xhs-b": "D:/profiles/xhs/b"},
    )
    monkeypatch.setattr(chrome_manager, "_MANAGED_CHROME_PIDS", {})
    monkeypatch.setattr(chrome_manager, "_CHROME_IDLE_TIMERS", {})
    monkeypatch.setattr(chrome_manager, "_CHROME_KEEP_ALIVE", {})
    monkeypatch.setattr(chrome_manager, "_CHROME_ACTIVE_OPERATIONS", {})
    monkeypatch.setattr(chrome_manager, "_CHROME_MANUAL_INTERACTION_LEASES", {})

    assert chrome_manager._browser_resource_key("xhs") == "xhs"
    result = chrome_manager.bring_chrome_to_front("xhs")
    assert result["status"] == "blocked"


def test_bare_xhs_startup_is_rejected_between_multiple_active_profiles(monkeypatch) -> None:
    monkeypatch.setattr(chrome_manager, "_MANAGED_CHROME_PROCS", {})
    monkeypatch.setattr(
        chrome_manager,
        "_MANAGED_CHROME_PROFILE_DIRS",
        {"xhs:xhs-a": "D:/profiles/xhs/a", "xhs:xhs-b": "D:/profiles/xhs/b"},
    )
    monkeypatch.setattr(chrome_manager, "_MANAGED_CHROME_PIDS", {})
    monkeypatch.setattr(chrome_manager, "_CHROME_IDLE_TIMERS", {})
    monkeypatch.setattr(chrome_manager, "_CHROME_KEEP_ALIVE", {})
    monkeypatch.setattr(chrome_manager, "_CHROME_ACTIVE_OPERATIONS", {})
    monkeypatch.setattr(chrome_manager, "_CHROME_MANUAL_INTERACTION_LEASES", {})
    monkeypatch.setattr(chrome_manager, "record_browser_event", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(chrome_manager, "_ensure_chrome_debugging_locked", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not guess")))

    assert chrome_manager._ensure_chrome_debugging("xhs") is False


def test_bare_xhs_idle_cleanup_is_bound_to_the_waiting_profile(monkeypatch, tmp_path) -> None:
    scheduled = {}
    cleanup_calls = []
    profile_dir = str(tmp_path / "account_b")

    class FakeTimer:
        def __init__(self, _delay, callback):
            scheduled["callback"] = callback
            self.daemon = False

        def cancel(self):
            pass

        def start(self):
            scheduled["started"] = True

    monkeypatch.setattr(chrome_manager, "_MANAGED_CHROME_PROCS", {})
    monkeypatch.setattr(chrome_manager, "_MANAGED_CHROME_PROFILE_DIRS", {"xhs:xhs-b": profile_dir})
    monkeypatch.setattr(chrome_manager, "_MANAGED_CHROME_PIDS", {"xhs:xhs-b": 12345})
    monkeypatch.setattr(chrome_manager, "_CHROME_IDLE_TIMERS", {})
    monkeypatch.setattr(chrome_manager, "_CHROME_KEEP_ALIVE", {"xhs:xhs-b": True})
    monkeypatch.setattr(chrome_manager, "_CHROME_ACTIVE_OPERATIONS", {})
    monkeypatch.setattr(chrome_manager, "_CHROME_MANUAL_INTERACTION_LEASES", {})
    monkeypatch.setattr(chrome_manager, "_CHROME_IDLE_SECONDS", 1)
    monkeypatch.setattr(chrome_manager, "_profile_metadata_for_platform", lambda *_args, **_kwargs: {"profile_id": "xhs-b"})
    monkeypatch.setattr(chrome_manager, "set_browser_session_deadline", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(chrome_manager, "record_browser_event", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(chrome_manager, "_cleanup_managed_chrome_platform", lambda *args, **kwargs: cleanup_calls.append((args, kwargs)))
    monkeypatch.setattr(chrome_manager.threading, "Timer", FakeTimer)

    chrome_manager._schedule_chrome_idle_cleanup("xhs", profile_dir=profile_dir)
    scheduled["callback"]()

    assert scheduled["started"] is True
    assert "xhs:xhs-b" in chrome_manager._CHROME_IDLE_TIMERS
    assert cleanup_calls == []


def test_legacy_xhs_cdp_helper_uses_the_active_profile_resource(monkeypatch) -> None:
    monkeypatch.setattr(xiaohongshu, "_browser_resource_key", lambda _platform: "xhs:xhs-c")

    endpoint = xiaohongshu._resolve_xhs_cdp_url(lambda resource: f"http://127.0.0.1/{resource}")

    assert endpoint == "http://127.0.0.1/xhs:xhs-c"


def test_xhs_manual_deadline_is_reminder_only(monkeypatch, tmp_path) -> None:
    scheduled = {}

    monkeypatch.setattr(chrome_manager, "set_browser_session_deadline", lambda *args, **kwargs: scheduled.update({"args": args, "kwargs": kwargs}) or {})
    chrome_manager._CHROME_MANUAL_INTERACTION_EXPIRY_TIMERS.clear()
    chrome_manager._schedule_manual_interaction_expiry(
        "xhs:xhs-a",
        deadline_at=9999999999,
        profile_dir=str(tmp_path / "xhs-a"),
        profile_id="xhs-a",
    )

    assert "xhs:xhs-a" not in chrome_manager._CHROME_MANUAL_INTERACTION_EXPIRY_TIMERS
    assert scheduled["kwargs"]["metadata"]["deadline_is_reminder_only"] is True


def test_watcher_event_only_completes_after_authoritative_probe(monkeypatch) -> None:
    watcher = xhs_auth_watcher.XhsAuthWatcher("xhs-a")
    events = []
    monkeypatch.setattr(watcher, "_record", lambda event, **metadata: events.append((event, metadata)))
    monkeypatch.setattr(watcher, "_observe_until_event_or_probe_due", lambda: True)
    monkeypatch.setattr(watcher, "_probe_and_complete", lambda trigger: trigger == "cdp_event")

    watcher._run()

    assert events == [("xhs_auth_watcher_started", {"observation": "cdp_page_dom_lifecycle_events"})]


def test_restart_restore_reopens_the_same_profile_before_watching(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(
        xhs_auth_watcher,
        "browser_sessions_summary",
        lambda **_kwargs: {
            "sessions": [
                {
                    "platform": "xhs:xhs-a",
                    "profile_id": "xhs-a",
                    "state": "USER_INTERACTING",
                    "reason": "login_required",
                    "metadata": {"trigger_evidence": ["login_prompt=true"]},
                }
            ]
        },
    )
    monkeypatch.setattr(xhs_auth_watcher, "start_xhs_auth_watcher", lambda profile_id: {"status": "started", "profile_id": profile_id})
    monkeypatch.setattr(chrome_manager, "request_browser_interaction", lambda *args, **kwargs: calls.append((args, kwargs)) or {"status": "waiting_for_user"})

    restored = xhs_auth_watcher.restore_pending_xhs_auth_watchers()

    assert restored["restored_profile_ids"] == ["xhs-a"]
    assert calls[0][0] == ("xhs", "login_required")
    assert calls[0][1]["target_profile_id"] == "xhs-a"
