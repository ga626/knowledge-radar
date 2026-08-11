from __future__ import annotations

from types import SimpleNamespace

from runtime import chrome_manager, executables


def _touch(path) -> str:
    path.write_text("", encoding="ascii")
    return str(path)


def test_managed_chrome_uses_registry_candidate_when_localappdata_is_missing(monkeypatch, tmp_path) -> None:
    chrome = _touch(tmp_path / "chrome.exe")
    edge = _touch(tmp_path / "msedge.exe")
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setattr(
        executables,
        "_managed_chrome_candidates",
        lambda: [(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe", "legacy_env"), (edge, "legacy_edge"), (chrome, "windows_app_paths_hkcu")],
    )

    selection = executables.resolve_managed_chrome()

    assert selection is not None
    assert selection.path == chrome
    assert selection.family == "google_chrome"
    assert selection.selection_source == "windows_app_paths_hkcu"
    assert executables.find_chrome_exe_candidates() == [chrome]


def test_managed_chrome_never_uses_edge_as_the_only_available_browser(monkeypatch, tmp_path) -> None:
    edge = _touch(tmp_path / "msedge.exe")
    monkeypatch.setattr(executables, "_managed_chrome_candidates", lambda: [(edge, "legacy_edge")])

    assert executables.resolve_managed_chrome() is None
    assert executables.find_chrome_exe_candidates() == []
    assert executables.managed_chrome_resolution_summary()["policy"] == "google_chrome_only_no_implicit_edge_fallback"


def test_managed_chrome_ignores_edge_even_when_explicitly_configured(monkeypatch, tmp_path) -> None:
    edge = _touch(tmp_path / "msedge.exe")
    chrome = _touch(tmp_path / "chrome.exe")
    monkeypatch.setattr(
        executables,
        "_managed_chrome_candidates",
        lambda: [(edge, "KR_CHROME_EXE"), (chrome, "windows_app_paths_hkcu")],
    )

    selection = executables.resolve_managed_chrome()

    assert selection is not None
    assert selection.path == chrome
    assert selection.selection_source == "windows_app_paths_hkcu"


def test_normal_cdp_reuse_rejects_legacy_edge_but_cleanup_can_identify_it(monkeypatch) -> None:
    def fake_run(command, **_kwargs):
        if command[0] == "netstat":
            return SimpleNamespace(stdout="  TCP    127.0.0.1:12733    0.0.0.0:0    LISTENING    24680\n")
        return SimpleNamespace(stdout="Name\nmsedge.exe\n")

    monkeypatch.setattr(chrome_manager, "silent_subprocess_run", fake_run)
    monkeypatch.setattr(chrome_manager, "_chrome_debug_port", lambda _platform: "12733")

    assert chrome_manager._find_chrome_with_debug_port("xhs") is None
    assert chrome_manager._find_chrome_with_debug_port("xhs", allow_legacy_edge=True) == 24680


def test_legacy_edge_is_retired_only_after_its_managed_profile_is_confirmed(monkeypatch, tmp_path) -> None:
    profile = str(tmp_path / "account_c")
    monkeypatch.setattr(chrome_manager, "_chrome_user_data_dir_for_pid", lambda _pid: profile)
    monkeypatch.setattr(chrome_manager, "_find_chrome_with_debug_port", lambda _platform, **_kwargs: None)
    monkeypatch.setattr(chrome_manager, "_close_chrome_via_cdp", lambda _platform: None)
    monkeypatch.setattr(chrome_manager, "transition_browser_session", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(chrome_manager, "record_browser_event", lambda *_args, **_kwargs: {})

    assert chrome_manager._retire_legacy_edge_session("xhs", 24680, profile) is True


def test_legacy_edge_is_not_retired_when_the_profile_does_not_match(monkeypatch, tmp_path) -> None:
    expected = str(tmp_path / "account_c")
    unrelated = str(tmp_path / "unrelated")
    monkeypatch.setattr(chrome_manager, "_chrome_user_data_dir_for_pid", lambda _pid: unrelated)
    monkeypatch.setattr(chrome_manager, "_xhs_safe_profile_switch_allowed", lambda *_args: False)

    assert chrome_manager._retire_legacy_edge_session("xhs", 24680, expected) is False


def test_idle_cleanup_uses_the_profile_that_currently_owns_the_cdp_port(monkeypatch, tmp_path) -> None:
    default_profile = str(tmp_path / "account_b")
    active_profile = str(tmp_path / "account_c")
    scheduled = {}
    cleanup_calls = []

    class FakeTimer:
        def __init__(self, delay, callback):
            scheduled["delay"] = delay
            scheduled["callback"] = callback
            self.daemon = False

        def cancel(self):
            scheduled["cancelled"] = True

        def start(self):
            scheduled["started"] = True

    monkeypatch.setattr(chrome_manager, "_CHROME_IDLE_SECONDS", 1)
    monkeypatch.setattr(chrome_manager, "_managed_chrome_profile_dir", lambda _platform: default_profile)
    monkeypatch.setattr(chrome_manager, "_find_chrome_with_debug_port", lambda _platform: 24680)
    monkeypatch.setattr(chrome_manager, "_chrome_user_data_dir_for_pid", lambda _pid: active_profile)
    monkeypatch.setattr(
        chrome_manager,
        "_profile_metadata_for_platform",
        lambda _platform, **_kwargs: {"profile_id": "xhs-c", "account_slot": "xhs_account_c"},
    )
    monkeypatch.setattr(chrome_manager, "set_browser_session_deadline", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(chrome_manager, "record_browser_event", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        chrome_manager,
        "_cleanup_managed_chrome_platform",
        lambda platform, *, expected_profile_dir="": cleanup_calls.append((platform, expected_profile_dir)),
    )
    monkeypatch.setattr(chrome_manager.threading, "Timer", FakeTimer)
    chrome_manager._CHROME_IDLE_TIMERS.clear()
    chrome_manager._CHROME_KEEP_ALIVE.pop("xhs", None)
    chrome_manager._CHROME_ACTIVE_OPERATIONS.pop("xhs", None)

    chrome_manager._schedule_chrome_idle_cleanup("xhs")
    scheduled["callback"]()

    assert scheduled["started"] is True
    assert cleanup_calls == [("xhs", active_profile)]
    assert not chrome_manager._same_windows_path(cleanup_calls[0][1], default_profile)


def test_idle_cleanup_deadline_is_rehydrated_for_the_same_active_profile(monkeypatch, tmp_path) -> None:
    active_profile = str(tmp_path / "account_c")
    scheduled = []
    monkeypatch.setattr(
        chrome_manager,
        "browser_sessions_summary",
        lambda limit: {
            "sessions": [
                {
                    "platform": "xhs",
                    "profile_id": "xhs-c",
                    "state": "READY_SILENT",
                    "deadline_at": 0.0,
                }
            ]
        },
    )
    monkeypatch.setattr(chrome_manager, "_find_chrome_with_debug_port", lambda _platform: 24680)
    monkeypatch.setattr(chrome_manager, "_chrome_user_data_dir_for_pid", lambda _pid: active_profile)
    monkeypatch.setattr(
        chrome_manager,
        "_profile_metadata_for_platform",
        lambda _platform, **_kwargs: {"profile_id": "xhs-c", "account_slot": "xhs_account_c"},
    )
    monkeypatch.setattr(
        chrome_manager,
        "_schedule_chrome_idle_cleanup",
        lambda platform, **kwargs: scheduled.append((platform, kwargs)),
    )
    chrome_manager._CHROME_IDLE_TIMERS.clear()
    chrome_manager._CHROME_KEEP_ALIVE.pop("xhs", None)

    restored = chrome_manager.restore_chrome_idle_cleanups()

    assert restored == {"restored": 1, "overdue": 1}
    assert scheduled == [("xhs", {"profile_dir": active_profile, "deadline_at": 0.0, "restored": True})]


def test_restart_reclaims_a_legacy_ready_session_without_a_persisted_deadline(monkeypatch, tmp_path) -> None:
    active_profile = str(tmp_path / "account_c")
    scheduled = []
    monkeypatch.setattr(
        chrome_manager,
        "browser_sessions_summary",
        lambda limit: {"sessions": [{"platform": "xhs", "profile_id": "xhs-c", "state": "READY_SILENT", "deadline_at": None}]},
    )
    monkeypatch.setattr(chrome_manager, "_find_chrome_with_debug_port", lambda _platform: 24680)
    monkeypatch.setattr(chrome_manager, "_chrome_user_data_dir_for_pid", lambda _pid: active_profile)
    monkeypatch.setattr(
        chrome_manager,
        "_profile_metadata_for_platform",
        lambda _platform, **_kwargs: {"profile_id": "xhs-c", "account_slot": "xhs_account_c"},
    )
    monkeypatch.setattr(
        chrome_manager,
        "_schedule_chrome_idle_cleanup",
        lambda platform, **kwargs: scheduled.append((platform, kwargs)),
    )
    monkeypatch.setattr(chrome_manager.time, "time", lambda: 500.0)
    chrome_manager._CHROME_IDLE_TIMERS.clear()
    chrome_manager._CHROME_KEEP_ALIVE.pop("xhs", None)

    restored = chrome_manager.restore_chrome_idle_cleanups()

    assert restored == {"restored": 1, "overdue": 1}
    assert scheduled == [("xhs", {"profile_dir": active_profile, "deadline_at": 500.0, "restored": True})]


def test_restart_rejects_stale_xhs_manual_session_bound_to_another_profile(monkeypatch, tmp_path) -> None:
    expected = str(tmp_path / "account_a")
    actual = str(tmp_path / "account_b")
    transitions = []
    minimized = []
    monkeypatch.setattr(
        chrome_manager,
        "browser_sessions_summary",
        lambda limit: {"sessions": [{"platform": "xhs", "state": "USER_INTERACTING", "profile_id": "xhs-a"}]},
    )
    monkeypatch.setattr(chrome_manager, "_managed_chrome_profile_dir", lambda *args, **kwargs: expected)
    monkeypatch.setattr(chrome_manager, "_find_chrome_with_debug_port", lambda platform: 321)
    monkeypatch.setattr(chrome_manager, "_chrome_user_data_dir_for_pid", lambda pid: actual)
    monkeypatch.setattr(chrome_manager, "_minimize_chrome_windows", lambda pid: minimized.append(pid))
    monkeypatch.setattr(chrome_manager, "transition_browser_session", lambda *args, **kwargs: transitions.append((args, kwargs)) or {})
    chrome_manager._MANAGED_CHROME_PIDS.pop("xhs", None)
    chrome_manager._CHROME_KEEP_ALIVE["xhs"] = True

    result = chrome_manager.reconcile_stale_xhs_manual_interactions()

    assert result == {"scanned": 1, "rejected": 1}
    assert minimized == [321]
    assert transitions[0][0][:2] == ("xhs", "FAILED")
    assert transitions[0][1]["reason"] == "profile_binding_mismatch_reconciled"
    assert transitions[0][1]["desired_visibility"] == "silent"
    assert "xhs" not in chrome_manager._CHROME_KEEP_ALIVE


def test_manual_interaction_expiry_closes_only_the_expected_managed_profile(monkeypatch, tmp_path) -> None:
    profile = str(tmp_path / "boss-profile")
    chrome_manager._CHROME_KEEP_ALIVE["boss"] = True
    chrome_manager._MANAGED_CHROME_PIDS["boss"] = 4321
    monkeypatch.setattr(chrome_manager, "_find_chrome_with_debug_port", lambda _platform: 4321)
    monkeypatch.setattr(chrome_manager, "_chrome_user_data_dir_for_pid", lambda _pid: profile)
    monkeypatch.setattr(chrome_manager, "_idle_cleanup_profile_dir", lambda _platform: profile)
    monkeypatch.setattr(chrome_manager, "set_browser_session_deadline", lambda *_args, **_kwargs: {})
    transitions = []
    monkeypatch.setattr(chrome_manager, "transition_browser_session", lambda *args, **kwargs: transitions.append((args, kwargs)) or {})
    cleanup = []
    monkeypatch.setattr(chrome_manager, "_cleanup_managed_chrome_platform", lambda platform, *, expected_profile_dir="": cleanup.append((platform, expected_profile_dir)))

    result = chrome_manager.cancel_browser_interaction("boss", reason="manual_interaction_expired", expected_profile_dir=profile)

    assert result["status"] == "cancelled"
    assert "boss" not in chrome_manager._CHROME_KEEP_ALIVE
    assert cleanup == [("boss", profile)]
    assert any(kwargs.get("event") == "manual_interaction_expired" for _args, kwargs in transitions)
    assert all(kwargs.get("metadata", {}).get("authenticated") != True for _args, kwargs in transitions)


def test_idle_loop_does_not_renew_an_explicit_manual_interaction(monkeypatch, tmp_path) -> None:
    profile = str(tmp_path / "boss-profile")
    scheduled = {}

    class FakeTimer:
        def __init__(self, _delay, callback):
            scheduled["callback"] = callback
            self.daemon = False

        def cancel(self):
            pass

        def start(self):
            scheduled["started"] = True

    monkeypatch.setattr(chrome_manager, "_CHROME_IDLE_SECONDS", 1)
    monkeypatch.setattr(chrome_manager, "_idle_cleanup_profile_dir", lambda _platform: profile)
    monkeypatch.setattr(chrome_manager, "_profile_metadata_for_platform", lambda _platform, **_kwargs: {})
    monkeypatch.setattr(chrome_manager, "set_browser_session_deadline", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(chrome_manager.threading, "Timer", FakeTimer)
    chrome_manager._CHROME_IDLE_TIMERS.clear()
    chrome_manager._CHROME_KEEP_ALIVE["boss"] = True

    chrome_manager._schedule_chrome_idle_cleanup("boss")
    scheduled["callback"]()

    assert scheduled["started"] is True
    assert list(chrome_manager._CHROME_IDLE_TIMERS) == ["boss"]
