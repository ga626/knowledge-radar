from __future__ import annotations

import json
import threading

import runtime.tool_trace as tool_trace
from runtime.research_ledger import open_task, read_task


def test_trace_redacts_raw_query_and_marks_missing_task_scope(monkeypatch, tmp_path) -> None:
    recorder = tool_trace.ToolTraceRecorder(str(tmp_path / "trace.jsonl"))
    monkeypatch.setattr(tool_trace, "get_tool_trace_recorder", lambda: recorder)

    @tool_trace.traced_tool("search_demo", strategy="demo")
    def search_demo(*, query: str, research_task_id: str = "") -> dict:
        return {"items": []}

    search_demo(query="这段原始查询绝不能写入 trace", research_task_id="task-a")
    payload = json.loads((tmp_path / "trace.jsonl").read_text(encoding="utf-8").strip())

    assert "这段原始查询绝不能写入 trace" not in json.dumps(payload, ensure_ascii=False)
    assert payload["metadata"]["query_fingerprint"].startswith("hmac-sha256:")
    assert payload["metadata"]["research_association"] == "explicit_task_scope"


def test_trace_calls_without_research_scope_remain_explicitly_partial(monkeypatch, tmp_path) -> None:
    recorder = tool_trace.ToolTraceRecorder(str(tmp_path / "trace.jsonl"))
    monkeypatch.setattr(tool_trace, "get_tool_trace_recorder", lambda: recorder)

    @tool_trace.traced_tool("search_demo", strategy="demo")
    def search_demo(*, query: str) -> dict:
        return {"items": []}

    search_demo(query="parallel task")
    payload = json.loads((tmp_path / "trace.jsonl").read_text(encoding="utf-8").strip())

    assert payload["metadata"]["research_association"] == "partial_no_task_scope"
    assert "research_task_scope" not in payload["metadata"]


def test_concurrent_scoped_traces_do_not_inherit_another_task_scope(monkeypatch, tmp_path) -> None:
    recorder = tool_trace.ToolTraceRecorder(str(tmp_path / "trace.jsonl"))
    monkeypatch.setattr(tool_trace, "get_tool_trace_recorder", lambda: recorder)
    barrier = threading.Barrier(2)

    @tool_trace.traced_tool("search_demo", strategy="demo")
    def search_demo(*, query: str, research_task_id: str) -> dict:
        barrier.wait(timeout=2)
        return {"items": []}

    threads = [threading.Thread(target=search_demo, kwargs={"query": f"q-{index}", "research_task_id": f"task-{index}"}) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=3)

    events = [json.loads(line) for line in (tmp_path / "trace.jsonl").read_text(encoding="utf-8").splitlines()]
    scopes = {item["metadata"].get("research_task_scope") for item in events}
    assert scopes == {tool_trace.privacy_fingerprint("task-0"), tool_trace.privacy_fingerprint("task-1")}
    assert all(item["metadata"].get("research_association") == "explicit_task_scope" for item in events)


def test_explicit_task_scope_returns_a_receipt_without_persisting_task_id_in_trace(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("KR_STATE_DIR", str(tmp_path / "state"))
    recorder = tool_trace.ToolTraceRecorder(str(tmp_path / "trace.jsonl"))
    monkeypatch.setattr(tool_trace, "get_tool_trace_recorder", lambda: recorder)
    open_task(objective="receipt", budget="fast", task_id="receipt-task", considered=[])

    @tool_trace.traced_tool("search_demo", strategy="demo")
    def search_demo(*, query: str, research_task_id: str) -> dict:
        return {"items": []}

    result = search_demo(query="不能落盘", research_task_id="receipt-task")

    assert result["research_receipt"]["receipt_id"].startswith("receipt-")
    assert "receipt-task" not in (tmp_path / "trace.jsonl").read_text(encoding="utf-8")
    assert len(read_task(task_id="receipt-task")["tool_receipts"]) == 1


def test_trace_persists_sync_receipt_failure_for_runtime_repair(monkeypatch, tmp_path) -> None:
    from runtime import research_ledger as ledger
    monkeypatch.setenv("KR_STATE_DIR", str(tmp_path / "state"))
    recorder = tool_trace.ToolTraceRecorder(str(tmp_path / "trace.jsonl"))
    monkeypatch.setattr(tool_trace, "get_tool_trace_recorder", lambda: recorder)
    task = open_task(objective="trace pending", budget="fast", considered=[])
    original = ledger.link_tool_result
    monkeypatch.setattr(ledger, "link_tool_result", lambda **_: {"association": "unique_source_identity", "status": "declared_only"})

    @tool_trace.traced_tool("detail_demo", strategy="demo")
    def detail_demo(*, research_task_id: str) -> dict:
        return {"url": "https://example.org/detail", "content": "正文"}

    result = detail_demo(research_task_id=task["research_task_id"])
    assert result["research_receipt"]["status"] == "recording_failed"
    assert result["research_receipt"]["recovery"]["status"] == "pending_persisted"
    assert len(read_task(task_id=task["research_task_id"])["pending_submissions"]) == 1
    monkeypatch.setattr(ledger, "link_tool_result", original)
    assert ledger.repair_pending_submissions()["repaired"]
    assert read_task(task_id=task["research_task_id"])["pending_submissions"] == []
