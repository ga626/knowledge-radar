from __future__ import annotations

import json
import threading

import runtime.mcp_observability as observability


def test_snapshot_distinguishes_server_session_tools_and_host_thread(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(observability, "runtime_state_dir", lambda: tmp_path)

    started = observability.record_server_started(
        transport="stdio",
        tool_names=["z_tool", "a_tool"],
        source_fingerprint="src-1",
    )
    assert started["server_process"]["status"] == "running"
    assert started["tool_list"]["fingerprint"] == observability.tool_list_fingerprint(["a_tool", "z_tool"])

    observed = observability.record_tool_list(session_id="secret-session-id", tool_names=["a_tool", "z_tool"], transport="stdio")
    assert observed["events"][-1]["kind"] == "tools_list_observed"
    assert "secret-session-id" not in json.dumps(observed)

    snapshot = observability.snapshot(transport="stdio", tool_names=["a_tool", "z_tool"])
    assert snapshot["mcp_session"]["status"] == "observed"
    assert snapshot["tool_list"]["status"] == "observed"
    assert snapshot["thread_tool_surface"]["status"] == "host_observed_only"
    assert snapshot["thread_tool_surface"]["native_observed"] is None


def test_transport_buckets_prevent_short_lived_probe_from_overwriting_http(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(observability, "runtime_state_dir", lambda: tmp_path)
    observability.record_server_started(transport="streamable-http", tool_names=["http_tool"], source_fingerprint="http")
    observability.record_server_started(transport="stdio", tool_names=["stdio_tool"], source_fingerprint="stdio")
    observability.record_tool_list(transport="streamable-http", session_id="http-session", tool_names=["http_tool"])
    observability.record_tool_list(transport="stdio", session_id="stdio-session", tool_names=["stdio_tool"])

    http_snapshot = observability.snapshot(transport="streamable-http", tool_names=["http_tool"])
    stdio_snapshot = observability.snapshot(transport="stdio", tool_names=["stdio_tool"])
    assert http_snapshot["server_process"]["source_fingerprint"] == "http"
    assert http_snapshot["tool_list"]["observed_fingerprint"] == observability.tool_list_fingerprint(["http_tool"])
    assert stdio_snapshot["server_process"]["source_fingerprint"] == "stdio"


def test_tool_fingerprint_is_order_independent() -> None:
    assert observability.tool_list_fingerprint(["b", "a", "a"]) == observability.tool_list_fingerprint(["a", "b"])


def test_concurrent_transactions_keep_json_state_and_leave_no_fixed_tmp(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(observability, "runtime_state_dir", lambda: tmp_path)
    errors: list[Exception] = []

    def write(index: int) -> None:
        try:
            observability.record_server_started(
                transport=f"stdio-{index}",
                tool_names=[f"tool-{index}"],
                source_fingerprint=f"source-{index}",
            )
        except Exception as exc:  # pragma: no cover - assertion after concurrent join
            errors.append(exc)

    threads = [threading.Thread(target=write, args=(index,)) for index in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    payload = json.loads(observability.state_path().read_text(encoding="utf-8"))
    assert len(payload["server_processes"]) == 6
    assert not list(tmp_path.glob("knowledgeradar-mcp-observability.tmp"))


def test_fallback_observation_does_not_overwrite_native_session(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(observability, "runtime_state_dir", lambda: tmp_path)
    observability.record_server_started(transport="stdio", tool_names=["native"], source_fingerprint="native-source")
    observability.record_tool_list(transport="stdio", session_id="native-session", tool_names=["native"])

    observability.record_server_started(
        transport="stdio",
        tool_names=["fallback"],
        source_fingerprint="fallback-source",
        invocation_kind="continuity_fallback",
        invocation_id="fallback-1",
    )
    observability.record_tool_list(
        transport="stdio",
        session_id="fallback-session",
        tool_names=["fallback"],
        invocation_kind="continuity_fallback",
        invocation_id="fallback-1",
    )

    snapshot = observability.snapshot(transport="stdio", tool_names=["native"])
    assert snapshot["server_process"]["source_fingerprint"] == "native-source"
    assert snapshot["mcp_session"]["status"] == "observed"
    assert snapshot["fallback_processes"]["fallback-1"]["source_fingerprint"] == "fallback-source"


def test_fallback_server_exit_marks_record_terminal_and_bounds_history(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(observability, "runtime_state_dir", lambda: tmp_path)
    monkeypatch.setattr(observability, "_pid_is_running", lambda pid: False)
    observability.record_server_started(
        transport="stdio",
        tool_names=["fallback"],
        source_fingerprint="fallback-source",
        invocation_kind="continuity_fallback",
        invocation_id="fallback-1",
    )
    observability.record_fallback_server_stopped(invocation_id="fallback-1")
    snapshot = observability.snapshot(transport="stdio", tool_names=["native"])
    assert snapshot["fallback_processes"]["fallback-1"]["status"] == "stopped"
    assert snapshot["fallback_processes"]["fallback-1"]["stop_reason"] == "server_exit"

    for index in range(observability.FALLBACK_HISTORY_LIMIT + 2):
        observability.record_server_started(
            transport="stdio",
            tool_names=["fallback"],
            invocation_kind="continuity_fallback",
            invocation_id=f"fallback-{index + 2}",
        )
    bounded = observability.snapshot(transport="stdio", tool_names=["native"])
    assert len(bounded["fallback_processes"]) == observability.FALLBACK_HISTORY_LIMIT
    assert all(item["status"] == "stopped" for item in bounded["fallback_processes"].values())
