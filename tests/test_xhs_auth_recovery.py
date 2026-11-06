from __future__ import annotations

from runtime import chrome_manager
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
