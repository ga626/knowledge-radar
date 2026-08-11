from __future__ import annotations

import json

import runtime.mcp_continuity as continuity


def test_evaluate_requires_native_tool_call_for_l2() -> None:
    ready = continuity.evaluate(
        config_ok=True,
        service_ok=True,
        session_status="observed",
        tool_list_ok=True,
        native_tools=["health_check"],
    )
    assert ready["status"] == "native_ready"
    assert ready["access_path"] == continuity.ACCESS_NATIVE
    assert ready["layers"]["l2_thread_native_surface"] == "pass"

    waiting = continuity.evaluate(
        config_ok=True,
        service_ok=True,
        session_status="observed",
        tool_list_ok=True,
    )
    assert waiting["status"] == "host_unobserved"
    assert waiting["layers"]["l2_thread_native_surface"] == "host_unobserved"


def test_fallback_is_explicit_and_not_native(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(continuity, "runtime_state_dir", lambda: tmp_path)
    saved = continuity.record_fallback(reason="host_refresh_unavailable", task_id="kr-research-test")
    assert saved["access_path"] == continuity.ACCESS_FALLBACK
    assert saved["status"] == "degraded_continuity"
    payload = json.loads((tmp_path / continuity.STATE_FILE).read_text(encoding="utf-8"))
    assert payload["events"][-1]["event"] == "continuity_fallback_activated"
    assert "host_refresh_unavailable" in json.dumps(payload, ensure_ascii=False)


def test_native_receipt_clears_degraded_reason(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(continuity, "runtime_state_dir", lambda: tmp_path)
    continuity.record_fallback(reason="transport_closed")
    saved = continuity.record_native_call(tool="health_check", source_fingerprint="src-1", tool_list_fingerprint="tools-1")
    assert saved["access_path"] == continuity.ACCESS_NATIVE
    assert saved["status"] == "native_ready"
    assert saved["last_error"] == ""
    assert saved["source_fingerprint"] == "src-1"


def test_fallback_call_receipt_is_explicit_and_cannot_pass_for_native(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(continuity, "runtime_state_dir", lambda: tmp_path)
    saved = continuity.record_fallback_call(
        tool="kr_web_search",
        outcome="ok",
        reason="transport_closed",
        task_id="kr-task-1",
        source_fingerprint="source-1",
        tool_list_fingerprint="tools-1",
    )
    assert saved["status"] == "degraded_continuity"
    assert saved["access_path"] == continuity.ACCESS_FALLBACK
    assert saved["layers"]["l2_thread_native_surface"] == "host_unobserved"
    assert saved["layers"]["l3_continuity_fallback"] == "active"
    assert saved["fallback_tool"] == "kr_web_search"
