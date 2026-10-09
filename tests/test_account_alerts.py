from runtime import account_alerts, desktop_notification
from runtime.browser_sessions import manual_action_request_from_session
from runtime.browser_sessions import BrowserSessionStore
from pathlib import Path
import subprocess
import sys


def test_long_lived_console_observes_other_process_updates(tmp_path):
    state, events = tmp_path / "sessions.json", tmp_path / "events.jsonl"
    console = BrowserSessionStore(state, events)
    tool = BrowserSessionStore(state, events)
    assert console.summary()["total"] == 0
    tool.upsert(platform="xhs:A", profile_id="A", state="NEEDS_USER", metadata={"account_recovery_pending": True})
    assert console.summary()["pending_human_action"] == 1
    console.upsert(platform="zhihu", profile_id="Z", state="NEEDS_USER")
    tool.transition("xhs:A", "READY_SILENT", metadata={"account_recovery_pending": False})
    assert console.summary()["total"] == 2
    assert console.summary()["pending_human_action"] == 1


def test_two_real_writers_preserve_each_others_sessions(tmp_path):
    state, events = tmp_path / "sessions.json", tmp_path / "events.jsonl"
    reader = BrowserSessionStore(state, events)
    assert reader.summary()["total"] == 0
    from runtime import browser_sessions
    source = str(Path(browser_sessions.__file__).resolve().parents[1])
    code = "from pathlib import Path; import sys; sys.path.insert(0,sys.argv[4]); from runtime.browser_sessions import BrowserSessionStore; s=BrowserSessionStore(Path(sys.argv[1]),Path(sys.argv[2])); [s.upsert(platform=sys.argv[3]+str(i),profile_id=sys.argv[3]+str(i),state='NEEDS_USER') for i in range(12)]"
    writers = [subprocess.Popen([sys.executable, "-c", code, str(state), str(events), prefix, source], stdout=subprocess.PIPE, stderr=subprocess.PIPE) for prefix in ("A", "B")]
    for writer in writers:
        _, error = writer.communicate(timeout=30)
        assert writer.returncode == 0, error.decode("utf-8", errors="replace")
    assert reader.summary()["total"] == 24
    assert reader.summary()["pending_human_action"] == 24


def test_registered_name_number_and_slot_survive_the_manual_action_contract():
    action = manual_action_request_from_session({
        "platform": "xhs:profile-c", "profile_id": "profile-c", "account_slot": "xhs_account_c",
        "state": "NEEDS_USER", "metadata": {"account_identity": {"display_label": "C0805"}},
    }, reason_code="login_required")
    assert action["display_label"] == "C0805"
    assert action["account_number"] == "0805"
    assert action["slot_label"] == "C"
    assert "0805" in action["human_message"]
    assert action["number_source"] == "registered_display_label"


def test_closed_window_never_becomes_verified_account(monkeypatch):
    monkeypatch.setattr(account_alerts, "profile_registry_internal", lambda: {"raw": {"profiles": [{"profile_id": "A", "platform": "xiaohongshu", "account_slot": "xhs_account_a"}]}})
    monkeypatch.setattr(account_alerts, "identity_for_profile", lambda _: {"display_label": "A9331"})
    monkeypatch.setattr(account_alerts, "browser_sessions_summary", lambda **_: {"sessions": [{"profile_id": "A", "state": "CLOSED"}]})
    row = account_alerts.account_alert_snapshot()["accounts"][0]
    assert row["state"] == "CLOSED"
    assert row["account_number"] == "9331"
    assert "authenticated" not in row
    assert not row["needs_action"]


def test_preview_does_not_dispatch_windows_notification(monkeypatch):
    monkeypatch.setenv("KR_CONSOLE_READ_ONLY", "1")
    monkeypatch.setattr(desktop_notification.subprocess, "Popen", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("preview must not notify")))
    assert desktop_notification.notify_account_failure({})["status"] == "disabled"


def test_closing_unverified_login_window_keeps_recovery_pending(monkeypatch):
    monkeypatch.setattr(account_alerts, "profile_registry_internal", lambda: {"raw": {"profiles": [{"profile_id": "A", "platform": "xiaohongshu"}]}})
    monkeypatch.setattr(account_alerts, "identity_for_profile", lambda _: {"display_label": "A9331"})
    monkeypatch.setattr(account_alerts, "browser_sessions_summary", lambda **_: {"sessions": [{"profile_id": "A", "state": "CLOSED", "metadata": {"account_recovery_pending": True}}]})
    assert account_alerts.account_alert_snapshot()["pending_count"] == 1


def test_verify_recovery_requires_probe_before_any_cleanup(monkeypatch):
    from runtime import chrome_manager
    monkeypatch.setattr(account_alerts, "profile_registry_internal", lambda: {"raw": {"profiles": [{"profile_id": "A", "platform": "xiaohongshu"}]}})
    probes = []
    monkeypatch.setattr(chrome_manager, "probe_browser_auth", lambda platform, **kwargs: probes.append((platform,kwargs)) or {"status": "needs_interaction", "manual_action_required": True})
    monkeypatch.setattr(chrome_manager, "complete_browser_interaction", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("do not close or declare recovered")))
    assert account_alerts.recover_account("A", "verify")["status"] == "not_verified"
    assert probes == [("xhs", {"target_profile_id": "A"})]


def test_notification_uses_data_not_interpolated_powershell(monkeypatch):
    monkeypatch.delenv("KR_CONSOLE_READ_ONLY", raising=False)
    monkeypatch.delenv("KR_DESKTOP_NOTIFICATIONS_DISABLED", raising=False)
    monkeypatch.setattr(desktop_notification.os, "name", "nt")
    monkeypatch.setattr(desktop_notification.shutil, "which", lambda _: "powershell.exe")
    captured = {}
    class Process:
        pid = 42
    def spawn(command, **kwargs):
        captured.update(command=command, env=kwargs["env"])
        return Process()
    monkeypatch.setattr(desktop_notification.subprocess, "Popen", spawn)
    result = desktop_notification.notify_account_failure({"display_label": "quote'$(private)", "human_message": "需要恢复"})
    assert result["status"] == "dispatched"
    assert result["delivery_confirmed"] is False
    assert not any("private" in arg for arg in captured["command"])
