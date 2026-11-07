"""本轮现场缺陷的最小回归；所有状态和文件仅写pytest隔离目录。"""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize("reason", ["parse_failed", "selector_error", "empty_results", "cdp_runtime_error"])
def test_parser_failures_never_become_remote_cooldowns(reason):
    from runtime.platform_risk import normalize_platform_risk_event, compute_platform_cooldown
    event = normalize_platform_risk_event(platform="boss", operation="search", reason_code=reason, outcome="failed")
    assert event.recoverability == "no_cooldown"
    assert compute_platform_cooldown(event, base_s=60, maximum_s=300, now=100)["cooldown_seconds"] == 0


@pytest.mark.parametrize("seconds", [7200, 300.1, 0.1])
def test_server_retry_after_is_never_shortened(seconds):
    from runtime.platform_risk import normalize_platform_risk_event, compute_platform_cooldown
    event = normalize_platform_risk_event(platform="web", operation="search", reason_code="HTTP_429", retry_after_s=seconds)
    result = compute_platform_cooldown(event, base_s=60, maximum_s=300, now=100)
    assert result["next_retry_at"] >= 100 + seconds


def site(monkeypatch, tmp_path):
    from runtime.native_site import NativeSite
    monkeypatch.setenv("KR_STATE_DIR", str(tmp_path))
    return NativeSite("boss", "BOSS", "https://www.zhipin.com/", "https://www.zhipin.com/", lambda data: data.get("authenticated"))


def test_expired_login_wait_is_read_only_and_does_not_block(monkeypatch, tmp_path):
    from runtime.state_file import write_state
    s = site(monkeypatch, tmp_path)
    write_state(s.state_path(), {"expires_at": 1, "interaction_id": "expired"})
    before = s.state_path().read_bytes()
    assert s.admission()["allowed"]
    assert s.state_path().read_bytes() == before


def test_legacy_risk_expires_but_unknown_outcome_never_replays(monkeypatch, tmp_path):
    from runtime.state_file import write_state
    s = site(monkeypatch, tmp_path)
    write_state(s.state_path(), {"risk": True, "reason": "SECURITY_BLOCK", "at": 1})
    assert s.admission()["allowed"]
    write_state(s.state_path(), {"risk": True, "unknown_outcome": True, "at": 1})
    assert not s.admission()["allowed"]
    assert s.admission()["reason"] == "outcome_latched"


def test_recent_risk_has_finite_retry_and_queries_never_extend_it(monkeypatch, tmp_path):
    import runtime.native_site as ns
    from runtime.state_file import write_state
    s = site(monkeypatch, tmp_path)
    monkeypatch.setattr(ns.time, "time", lambda: 100)
    write_state(s.state_path(), {"risk": True, "at": 90})
    before = s.state_path().read_bytes()
    gate = s.admission()
    assert not gate["allowed"]
    assert 0 < gate["retry_after_s"] <= 300
    assert s.state_path().read_bytes() == before


def test_new_risk_signal_never_shortens_server_wait(monkeypatch, tmp_path):
    from runtime.state_file import write_state
    from types import SimpleNamespace
    s = site(monkeypatch, tmp_path)
    now = __import__('time').time()
    write_state(s.state_path(), {'risk': True, 'retry_at': now + 7200, 'reason': 'rate_limited_remote'})
    monkeypatch.setattr(type(s), 'bridge', lambda _: SimpleNamespace(request=lambda *a, **k: {'ok': False, 'code': 'SECURITY_BLOCK'}))
    s.invoke('probe')
    assert s.state()['retry_at'] >= now + 7200


def test_xhs_rate_limit_is_cooldown_and_expired_stop_does_not_block(monkeypatch, tmp_path):
    from runtime import xhs_opencli as oc
    monkeypatch.setenv("KR_OPENCLI_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("KR_XHS_OPENCLI_CONTEXT_ID", "isolated-test-context")
    from contextlib import nullcontext
    monkeypatch.setattr(oc.BorrowedBrowser, "lease", lambda *a, **k: nullcontext())
    monkeypatch.setattr(oc, "_observe_account", lambda *args: None)
    result = oc._failure("RATE_LIMITED")
    assert result["failure_type"] == "rate_limited"
    assert not result["manual_action_required"]
    raw = json.loads((tmp_path / "stop.json").read_text(encoding="utf-8"))
    assert raw["retry_at"] > raw["at"]
    raw["at"] = 1
    raw["retry_at"] = 2
    (tmp_path / "stop.json").write_text(json.dumps(raw), encoding="utf-8")
    monkeypatch.setattr(oc, "_run", lambda *args, **kw: {"ok": True})
    assert oc.invoke("search", keyword="test")["ok"]


def test_xhs_unknown_outcome_is_reconciliation_not_verification(monkeypatch, tmp_path):
    from runtime import xhs_opencli as oc
    monkeypatch.setenv("KR_OPENCLI_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(oc, "_observe_account", lambda *args: None)
    result = oc._failure("COMMAND_RESULT_UNKNOWN")
    assert result["failure_type"] == "request_outcome_unknown"
    assert not result["manual_action_required"]
    monkeypatch.setattr(oc, "_run", lambda *a, **k: pytest.fail("unknown outcome replayed"))
    assert not oc.invoke("search", keyword="test")["ok"]


def test_old_recruitment_failure_does_not_inflate_next_cooldown(monkeypatch, tmp_path):
    from runtime import recruitment_governance as rg
    monkeypatch.setenv("KR_TASK_DB_PATH", str(tmp_path / "tasks.sqlite3"))
    clock = [1000]
    monkeypatch.setattr(rg.time, "time", lambda: clock[0])
    rg.record_search_outcome("boss", "failed", "HTTP_429", keyword="test")
    clock[0] += 86400
    assert rg._latest_cooldown_seconds("boss", keyword="test") == 0
    rg.record_search_outcome("boss", "failed", "HTTP_429", keyword="test")
    assert 60 <= rg.check_search_gate("boss", keyword="test")["cooldown_remaining_s"] <= 72


def test_failure_count_does_not_relock_expired_recruitment_cooldown(monkeypatch, tmp_path):
    from runtime import recruitment_governance as rg
    monkeypatch.setenv("KR_TASK_DB_PATH", str(tmp_path / "tasks.sqlite3"))
    clock = [10000.0]
    monkeypatch.setattr(rg.time, "time", lambda: clock[0])
    for _ in range(3):
        rg.record_search_outcome("boss", "failed", "HTTP_429", keyword="test")
        clock[0] += 301
    assert rg.check_search_gate("boss", keyword="test")["allowed"]


def test_legacy_long_recruitment_lock_is_bounded_without_rewriting_history(monkeypatch, tmp_path):
    from runtime import recruitment_governance as rg
    monkeypatch.setenv("KR_TASK_DB_PATH", str(tmp_path / "tasks.sqlite3"))
    monkeypatch.setattr(rg.time, "time", lambda: 10000.0)
    conn = rg._get_conn()
    conn.execute("INSERT INTO recruitment_search_gate (platform,ts,outcome,reason,cooldown_until) VALUES ('boss',9000,'failed','HTTP_429',999999)")
    conn.commit()
    conn.close()
    assert rg.check_search_gate("boss")["allowed"]
    conn = rg._get_conn()
    assert conn.execute("SELECT cooldown_until FROM recruitment_search_gate").fetchone()[0] == 999999
    conn.close()


@pytest.mark.parametrize("code", ["COMMAND_RESULT_UNKNOWN", "opencli_caller_timeout", "opencli_invalid_output"])
def test_xhs_unknown_outcome_never_spends_on_fallback(monkeypatch, code):
    from runtime import xhs_opencli as oc
    from runtime import xhs_tikhub_fallback as paid
    monkeypatch.setattr(paid, "execute_paid_operation", lambda *a, **k: pytest.fail("unknown request spent on fallback"))
    result = {"ok": False, "error": {"error": code}}
    assert oc._recover_or_fallback(result, "search") is result


def test_metrics_recover_tool_ecology_and_exclude_internal_plans():
    from runtime.research_workspace import task_metrics, task_rounds
    data = {"tool_receipts": [{"tool": "extract_web_page", "source_ecology": " _ "}],
            "candidate_ecologies": [{"source_ecology": "_"}, {"source_ecology": " generic_web_ecology "}]}
    assert task_metrics(data)["source_ecologies"] == ["generic_web_ecology"]
    assert task_metrics(data)["planned_source_ecologies"] == ["generic_web_ecology"]
    assert task_rounds(data)[0]["source_ecologies"] == ["generic_web_ecology"]


def test_metrics_ignore_legacy_empty_ecology_marker():
    from runtime.research_workspace import task_metrics, task_rounds
    data = {"tool_receipts": [
        {"tool": "kr_research", "source_ecology": "_"},
        {"tool": "extract_web_page", "source_ecology": "generic_web_ecology"}]}
    assert task_metrics(data)["source_ecologies"] == ["generic_web_ecology"]
    assert {name for row in task_rounds(data) for name in row["source_ecologies"]} == {"generic_web_ecology"}


@pytest.mark.parametrize("encrypted", [True, False])
def test_pdf_invalid_input_has_stable_diagnostic(monkeypatch, tmp_path, encrypted):
    from pypdf import PdfWriter
    from runtime.pdf_worker import extract_isolated
    monkeypatch.setenv("KR_STATE_DIR", str(tmp_path / "state"))
    source = tmp_path / "test.pdf"
    if encrypted:
        writer = PdfWriter()
        writer.add_blank_page(72, 72)
        writer.encrypt("not-a-real-user-password")
        with source.open("wb") as stream:
            writer.write(stream)
    else:
        source.write_bytes(b"%PDF-1.7\ncorrupt object\n")
    code = "document_password_required" if encrypted else "document_corrupt"
    with pytest.raises(ValueError, match=code):
        extract_isolated(source, timeout_s=15)
    assert not list((tmp_path / "state" / "document_workers").glob("pdf-*"))


def test_pdf_late_page_parse_error_is_classified(monkeypatch):
    from types import SimpleNamespace
    import pypdf
    from pypdf.errors import PdfStreamError
    from runtime.pdf_text import extract_pdf_text, DocumentError
    class BrokenPage:
        def get(self, _key):
            raise PdfStreamError("private internal parser detail")
    monkeypatch.setattr(pypdf, "PdfReader", lambda *_: SimpleNamespace(is_encrypted=False, pages=[BrokenPage()]))
    with pytest.raises(DocumentError, match="^document_corrupt$"):
        extract_pdf_text(b"late-page-error")


@pytest.mark.parametrize("encrypted", [True, False])
def test_pdf_durable_task_returns_actionable_error_without_evidence(monkeypatch, tmp_path, encrypted):
    import hashlib, time
    from pypdf import PdfWriter
    from runtime.task_service import TaskService
    from runtime.tasks import TaskStore
    from runtime import leases, cloud_ocr
    for name, tail in {"KR_STATE_DIR":"state", "KR_DATA_ROOT":"data", "KR_LOG_DIR":"logs",
        "KR_EVIDENCE_ROOT":"assets", "KR_RUNTIME_LEASE_DB_PATH":"leases.sqlite3"}.items():
        monkeypatch.setenv(name, str(tmp_path / tail))
    monkeypatch.setattr(leases, "_COORDINATOR", None)
    monkeypatch.setattr(cloud_ocr, "run_cloud_ocr", lambda *a, **k: pytest.fail("invalid PDF sent to OCR"))
    source = tmp_path / "input.pdf"
    if encrypted:
        writer = PdfWriter(); writer.add_blank_page(72,72); writer.encrypt("test-only-password")
        with source.open("wb") as stream: writer.write(stream)
    else:
        source.write_bytes(b"%PDF-1.7\ncorrupt\n")
    service = TaskService(TaskStore(str(tmp_path / "tasks.sqlite3")))
    try:
        row = service.submit("optional_media", {"component":"pypdf", "input_path":str(source),
            "approved_input_root":str(tmp_path), "input_sha256":hashlib.sha256(source.read_bytes()).hexdigest()}, timeout_s=30)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            result = service.result(row["task_id"])
            if result["status"] not in {"queued", "running"}: break
            time.sleep(.05)
        assert result["status"] == "failed"
        code = "document_password_required" if encrypted else "document_corrupt"
        assert result["task"]["error_code"] == code
        assert result["error"]["type"] == code
        assert result["error"]["next_action"]
        assert result["error"]["ocr_attempted"] is False
        assert not result.get("result", {}).get("evidence_assets")
        assert not (tmp_path / "assets" / "versions").exists()
    finally:
        service.stop_maintenance()


def test_silent_process_rejects_flags_that_override_no_window(monkeypatch):
    from runtime import process
    monkeypatch.setattr(process.os, "name", "nt")
    monkeypatch.delenv("KR_SHOW_CHILD_CONSOLES", raising=False)
    for flag in (0x8, 0x10):
        with pytest.raises(ValueError, match="console_creation_flags_conflict"):
            process.silent_creationflags(flag)


@pytest.mark.skipif(os.name != "nt", reason="Windows GUI interpreter contract")
def test_background_python_uses_gui_venv_interpreter(tmp_path):
    from runtime.process import background_python_executable, silent_subprocess_run
    import shutil, importlib.util
    runtime = tmp_path / "venv"
    base = [sys.executable] if importlib.util.find_spec("venv") else [shutil.which("py") or "py", "-3.12"]
    made = silent_subprocess_run([*base, "-m", "venv", "--without-pip", str(runtime)], capture_output=True, timeout=40)
    assert made.returncode == 0, made.stderr
    interpreter = background_python_executable(runtime / "Scripts/python.exe")
    assert Path(interpreter).name == "pythonw.exe"
    output = tmp_path / "probe.json"
    script = tmp_path / "probe.py"
    script.write_text("import sys,json,ctypes,pathlib; pathlib.Path(sys.argv[1]).write_text(json.dumps({'console':ctypes.windll.kernel32.GetConsoleWindow(),'prefix':sys.prefix}),encoding='utf-8')", encoding="utf-8")
    child = silent_subprocess_run([interpreter, str(script), str(output)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
    assert child.returncode == 0
    result = json.loads(output.read_text(encoding="utf-8"))
    assert result["console"] == 0
    assert Path(result["prefix"]) == runtime
