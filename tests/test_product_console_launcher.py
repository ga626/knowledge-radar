from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

import pytest


ROOT = Path(__file__).resolve().parents[1]


def load_launcher():
    path = ROOT / "scripts" / "product_console_launcher.py"
    spec = importlib.util.spec_from_file_location("product_console_launcher_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def context(launcher, tmp_path: Path, *, role: str = "dev", port: int | None = None):
    install_root = tmp_path / "install"
    data_root = tmp_path / "data"
    install_root.mkdir()
    (data_root / "config").mkdir(parents=True)
    (data_root / "config" / "runtime.env").write_text("# isolated test runtime\n", encoding="utf-8")
    (install_root / "active.json").write_text(json.dumps({"schema": "knowledgeradar-active-install/v1", "program_root": str(ROOT), "data_root": str(data_root), "version": "test"}), encoding="utf-8")
    if role == "dev":
        os.environ["KR_DEV_PREVIEW_STATE_ROOT"] = str(tmp_path / "preview-state")
    selected_port = port if port is not None else (launcher.DEV_CONSOLE_PORT if role == "dev" else launcher.CONSOLE_PORT)
    return launcher.resolve_context(role=role, port=selected_port, install_root=install_root, program_override=ROOT if role == "dev" else None)


def test_context_keeps_stable_and_development_identities_separate(tmp_path: Path) -> None:
    launcher = load_launcher()
    dev = context(launcher, tmp_path, role="dev")
    assert dev.port == 18883
    with pytest.raises(RuntimeError, match="stable console"):
        launcher.resolve_context(role="stable", port=18882, install_root=dev.install_root, program_override=ROOT)
    with pytest.raises(RuntimeError, match="development preview"):
        launcher.resolve_context(role="dev", port=18882, install_root=dev.install_root, program_override=ROOT)


def test_foreign_or_stale_fixed_port_is_never_replaced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    launcher = load_launcher()
    candidate = context(launcher, tmp_path)
    monkeypatch.setattr(launcher, "_health", lambda port, expected=None: "stale")
    with pytest.raises(RuntimeError, match="不会抢占"):
        launcher.open_console(context=candidate, restart=False, open_browser=False)


def test_second_mutex_owner_is_rejected(tmp_path: Path) -> None:
    launcher = load_launcher()
    candidate = launcher.ConsoleContext(
        role=f"unit-{tmp_path.name}",
        port=18883,
        install_root=tmp_path / "install",
        program=ROOT,
        data=tmp_path / "data",
        supervisor_root=tmp_path / "preview-state",
        fingerprint="unit-test",
    )
    first = launcher._ConsoleMutex(candidate)
    second = launcher._ConsoleMutex(candidate)
    try:
        assert first.acquire() is True
        assert second.acquire() is False
    finally:
        first.release()
        second.release()


def test_stable_task_definition_uses_bounded_recovery_and_ignores_duplicates(tmp_path: Path) -> None:
    launcher = load_launcher()
    script = launcher._stable_task_script(enabled=True, install_root=tmp_path / "install")
    assert "RestartCount 5" in script
    assert "RestartInterval" in script
    assert "MultipleInstances IgnoreNew" in script
    assert "--role stable --port 18882" in script
    assert "--program-root" not in script


def test_dev_background_launch_is_detached_and_bound_to_its_candidate_identity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    launcher = load_launcher()
    candidate = context(launcher, tmp_path, role="dev")
    calls: list[tuple[list[str], Path]] = []
    monkeypatch.setattr(launcher, "_spawn", lambda command, *, log_path: calls.append((command, log_path)))
    launcher._start_background(candidate)
    assert len(calls) == 1
    command, log_path = calls[0]
    assert "--supervise" in command
    assert ["--program-root", str(candidate.program)] == command[-4:-2]
    assert ["--preview-state-root", str(candidate.supervisor_root)] == command[-2:]
    assert log_path == candidate.log_path


def test_status_reports_a_dead_supervisor_record_as_stale(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    launcher = load_launcher()
    candidate = context(launcher, tmp_path, role="dev")
    candidate.status_path.parent.mkdir(parents=True)
    candidate.status_path.write_text(json.dumps({"state": "READY", "supervisor_pid": 12345}), encoding="utf-8")
    monkeypatch.setattr(launcher, "_pid_is_alive", lambda pid: False)
    monkeypatch.setattr(launcher, "_health_details", lambda port: {"state": "absent"})
    snapshot = launcher.status_snapshot(candidate)
    assert snapshot["supervisor"]["state"] == "STALE_SUPERVISOR_STATE"
    assert snapshot["supervisor"]["prior_state"] == "READY"


def test_legacy_healthless_flag_is_limited_to_the_stable_supervisor(tmp_path: Path) -> None:
    launcher = load_launcher()
    with pytest.raises(SystemExit):
        launcher.main(["--role", "dev", "--port", str(launcher.DEV_CONSOLE_PORT), "--program-root", str(ROOT), "--legacy-healthless"])


def test_autostart_migrates_legacy_startup_file_to_stable_task(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    launcher = load_launcher()
    candidate = context(launcher, tmp_path, role="stable")
    (candidate.install_root / "console_product.py").write_text("fixture", encoding="utf-8")
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    legacy = launcher._startup_path()
    legacy.parent.mkdir(parents=True)
    legacy.write_text("legacy", encoding="utf-8")
    calls: list[str] = []
    monkeypatch.setattr(launcher, "_run_task_script", calls.append)

    assert launcher.set_autostart(enabled=True, context=candidate) == "enabled"
    assert calls and "Register-ScheduledTask" in calls[0]
    assert not legacy.exists()


def test_real_dev_supervisor_starts_restarts_and_stops_without_touching_stable(tmp_path: Path) -> None:
    launcher = load_launcher()
    if launcher._health_details(launcher.DEV_CONSOLE_PORT)["state"] != "absent":
        pytest.skip("18883 is already reserved by an interactive development preview")
    candidate = context(launcher, tmp_path)
    helper = candidate.install_root / "console_product.py"
    shutil.copyfile(ROOT / "scripts" / "product_console_launcher.py", helper)
    command = [sys.executable, str(helper), "--role", "dev", "--port", str(candidate.port), "--program-root", str(ROOT), "--no-open"]
    try:
        first = subprocess.run(command, text=True, encoding="utf-8", capture_output=True, check=False, timeout=20)
        assert first.returncode == 0, first.stderr
        assert launcher._wait_until_ready(candidate, timeout=12)
        generation = launcher._health_details(candidate.port)["generation"]

        restarted = subprocess.run(command + ["--restart"], text=True, encoding="utf-8", capture_output=True, check=False, timeout=20)
        assert restarted.returncode == 0, restarted.stderr
        assert launcher._wait_until_ready(candidate, timeout=12, generation_after=generation)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and launcher.status_snapshot(candidate)["supervisor"]["state"] != "READY":
            time.sleep(0.1)
        assert launcher.status_snapshot(candidate)["supervisor"]["state"] == "READY"
    finally:
        subprocess.run(command + ["--stop-supervisor"], text=True, encoding="utf-8", capture_output=True, check=False, timeout=20)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and launcher._health_details(candidate.port)["state"] != "absent":
            time.sleep(0.2)
    assert launcher._health_details(candidate.port)["state"] == "absent"
