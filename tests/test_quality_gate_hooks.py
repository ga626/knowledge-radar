from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
QUALITY_STATE = ROOT / "project_status" / "Quality-Gate-State.json"


def _restore_quality_state(original: str | None) -> None:
    if original is None:
        if QUALITY_STATE.exists():
            QUALITY_STATE.unlink()
        return
    QUALITY_STATE.parent.mkdir(parents=True, exist_ok=True)
    QUALITY_STATE.write_text(original, encoding="utf-8")


def test_codex_hooks_config_declares_quality_events() -> None:
    config = json.loads((ROOT / ".codex" / "hooks.json").read_text(encoding="utf-8"))

    hooks = config["hooks"]
    assert {"SessionStart", "PostToolUse", "PreCompact", "Stop"} <= set(hooks)
    assert "quality_gate_hook.py" in hooks["Stop"][0]["hooks"][0]["commandWindows"]
    assert "PYTHONIOENCODING='utf-8'" in hooks["Stop"][0]["hooks"][0]["commandWindows"]
    hook_source = (ROOT / ".codex" / "hooks" / "quality_gate_hook.py").read_text(encoding="utf-8")
    assert "auto_commit_verified_changes" in hook_source


def test_git_hooks_are_installed_as_manifest_contract() -> None:
    pre_commit = (ROOT / ".githooks" / "pre-commit").read_text(encoding="utf-8")
    assert "kr_pre_commit_router.py --json" in pre_commit
    assert 'export PYTHONIOENCODING="utf-8"' in pre_commit
    pre_push = (ROOT / ".githooks" / "pre-push").read_text(encoding="utf-8")
    assert "kr_quality_gate.py --changed --json" in pre_push
    assert "kr_quality_gate.py --full --json" not in pre_push


def test_pre_commit_router_cli_json_emits_unicode_without_crashing() -> None:
    proc = subprocess.run(
        [sys.executable, "scripts/kr_pre_commit_router.py", "--json", "--plan-only"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="gbk",
        errors="replace",
        env={**dict(os.environ), "PYTHONIOENCODING": "gbk", "PYTHONUTF8": "0"},
        timeout=60,
    )

    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["schema"] == "knowledgeradar-pre-commit-router/v1"


def test_quality_hook_installer_sets_local_hooks_path() -> None:
    proc = subprocess.run(
        [sys.executable, "scripts/install_quality_hooks.py"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    payload = json.loads(proc.stdout)

    assert proc.returncode == 0
    assert payload["status"] == "PASS"
    assert payload["git_hooks_path"] == ".githooks"


def test_quality_gate_auto_posttooluse_marks_stale() -> None:
    original = QUALITY_STATE.read_text(encoding="utf-8") if QUALITY_STATE.exists() else None
    try:
        proc = subprocess.run(
            [sys.executable, "scripts/kr_quality_gate.py", "--auto", "--event", "PostToolUse", "--json"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
    finally:
        _restore_quality_state(original)
    payload = json.loads(proc.stdout)

    assert proc.returncode == 0
    assert payload["status"] == "STALE"
    assert payload["next_action"] == "python scripts/kr_closeout_router.py --json"


def test_codex_hook_tolerates_prefixed_windows_stdin() -> None:
    original = QUALITY_STATE.read_text(encoding="utf-8") if QUALITY_STATE.exists() else None
    try:
        proc = subprocess.run(
            [sys.executable, ".codex/hooks/quality_gate_hook.py"],
            cwd=ROOT,
            input='锘包謢{"hook_event_name":"PostToolUse"}\n',
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
        payload = json.loads(proc.stdout)
    finally:
        _restore_quality_state(original)

    assert proc.returncode == 0
    assert "STALE after tool use" in payload["systemMessage"]
    assert ".python312" in payload["systemMessage"]
    assert "kr_closeout_router.py" in payload["systemMessage"]


def test_codex_hook_sessionstart_returns_environment_brief() -> None:
    proc = subprocess.run(
        [sys.executable, ".codex/hooks/quality_gate_hook.py"],
        cwd=ROOT,
        input='{"hook_event_name":"SessionStart"}\n',
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    payload = json.loads(proc.stdout)

    assert proc.returncode == 0
    assert "KnowledgeRadar agent environment" in payload["systemMessage"]
    assert ".python312" in payload["systemMessage"]
    assert "PowerShell" in payload["systemMessage"]


def test_manual_quality_gate_does_not_write_state_by_default() -> None:
    before = QUALITY_STATE.read_text(encoding="utf-8") if QUALITY_STATE.exists() else None

    proc = subprocess.run(
        [sys.executable, "scripts/kr_quality_gate.py", "--fast", "--json"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    after = QUALITY_STATE.read_text(encoding="utf-8") if QUALITY_STATE.exists() else None
    payload = json.loads(proc.stdout)

    assert proc.returncode in {0, 1}
    assert payload["schema"] == "knowledgeradar-quality-gate-result/v1"
    assert "quality_state" not in payload
    assert after == before


def test_explicit_write_state_updates_quality_state_and_auto_commits(monkeypatch, capsys) -> None:
    import scripts.kr_quality_gate as quality_gate

    original = QUALITY_STATE.read_text(encoding="utf-8") if QUALITY_STATE.exists() else None
    auto_commit_calls: list[Path] = []
    try:
        monkeypatch.setattr(sys, "argv", ["kr_quality_gate.py", "--fast", "--json", "--write-state", "--no-auto-commit"])
        monkeypatch.setattr(quality_gate, "_run_fast", lambda: [{"status": "pass", "command": "ok", "returncode": 0}])
        monkeypatch.setattr(
            quality_gate,
            "auto_commit_verified_changes",
            lambda root: auto_commit_calls.append(root)
            or {"schema": "knowledgeradar-auto-commit/v1", "status": "PASS", "action": "noop"},
        )
        assert quality_gate.main() == 0
        payload = json.loads(capsys.readouterr().out)
        state = json.loads(QUALITY_STATE.read_text(encoding="utf-8"))
    finally:
        _restore_quality_state(original)

    assert payload["schema"] == "knowledgeradar-quality-gate-result/v1"
    assert "quality_state" in payload
    assert payload["auto_commit"]["status"] == "PASS"
    assert auto_commit_calls == [ROOT]
    assert state["schema"] == "knowledgeradar-quality-state/v1"
    assert state["status_class"] == "PASS"


def test_codex_stop_auto_commits_fresh_report_light(monkeypatch) -> None:
    import importlib.util

    hook_path = ROOT / ".codex" / "hooks" / "quality_gate_hook.py"
    spec = importlib.util.spec_from_file_location("quality_gate_hook_under_test", hook_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    writes: list[dict] = []
    monkeypatch.setattr(module, "_read_event", lambda: {"hook_event_name": "Stop"})
    monkeypatch.setattr(module, "_write", lambda payload: writes.append(payload))
    monkeypatch.setattr(
        module,
        "quality_state_freshness",
        lambda _root: {"fresh": True, "status_class": "PASS", "mode": "report_light", "profile": "report_light"},
    )
    monkeypatch.setattr(
        module,
        "auto_commit_verified_changes",
        lambda _root: {"schema": "knowledgeradar-auto-commit/v1", "status": "PASS", "action": "committed", "commit": "abc123"},
    )

    assert module.main() == 0
    assert writes == [{"continue": True, "systemMessage": "KnowledgeRadar auto-committed verified changes: abc123."}]
