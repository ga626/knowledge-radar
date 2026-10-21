"""Installed recovery must retain the registered profile and a visible window."""
from runtime import chrome_manager as chrome, profile_registry as registry


def test_explicit_boss_recovery_uses_registered_profile_even_when_blocked(monkeypatch, tmp_path):
    expected = str(tmp_path / "registered")
    monkeypatch.setattr(chrome, "raw_registry_for_platform", lambda *_: {"profiles": [
        {"profile_id": "boss-personal", "profile_dir": expected, "main_chain_allowed": True}]})
    monkeypatch.setattr(chrome, "select_main_chain_profile", lambda *_: {"status": "blocked", "profile_id": "boss-personal"})
    monkeypatch.setattr(chrome, "BOSS_CHROME_USER_DATA_DIR", "")
    monkeypatch.delenv("KR_BOSS_CHROME_USER_DATA_DIR", raising=False)
    assert chrome._managed_chrome_profile_dir("boss", target_profile_id="boss-personal") == expected
    assert chrome._managed_chrome_profile_dir("boss") == expected
    assert chrome._managed_chrome_profile_dir("boss", target_profile_id="missing") == ""


def test_explicit_auth_failure_is_actionable_in_single_account_projection(monkeypatch, tmp_path):
    monkeypatch.setenv("KR_STATE_DIR", str(tmp_path))
    from runtime.account_observations import AccountObservationStore
    for reason, state in [("login_required", "needs_login"), ("platform_verification_required", "needs_verification")]:
        registry.record_profile_state("boss-personal", platform="boss", state="blocked", reason_code=reason,
                                      manual_action_required=False, last_tool="probe_browser_auth:boss")
        fact = AccountObservationStore().get("boss-personal")
        assert fact["state"] == state
        assert fact["needs_action"]


def test_restore_happens_before_reposition(monkeypatch):
    import ctypes
    from types import SimpleNamespace
    events=[]
    class User32:
        def EnumWindows(self, callback, _): callback(123, 0)
        def GetWindowThreadProcessId(self, _, ptr): ptr._obj.value=42
        def IsWindowVisible(self, _): return True
        def ShowWindow(self, *args): events.append("restore")
        def SetWindowPos(self, *args): events.append("position")
        def SetForegroundWindow(self, *args): events.append("foreground")
    monkeypatch.setattr(chrome.os, "name", "nt")
    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(user32=User32()), raising=False)
    monkeypatch.setattr(ctypes, "WINFUNCTYPE", lambda *_: lambda fn: fn, raising=False)
    monkeypatch.setattr(chrome, "_chrome_process_tree_pids", lambda _: {42})
    monkeypatch.setattr(chrome, "_platform_for_chrome_pid", lambda _: "")
    chrome._bring_chrome_windows_to_front(42)
    assert events == ["restore", "position", "foreground"]
