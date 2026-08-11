from __future__ import annotations

import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.build_codex_thread_trace import build_codex_thread_trace
from scripts.evaluate_codex_research_turn import evaluate_trace


def _sidecar(path: Path) -> Path:
    evidence = path / "report.evidence.json"
    evidence.write_text(
        json.dumps(
            {
                "schema": "knowledgeradar-research-evidence/v1",
                "artifact": {"title": "调研 A", "report_path": "report.md"},
                "research_task": {"schema": "knowledgeradar-research-task/v1", "task_id": "task-a", "objective": "调研 A"},
                "research_delivery": {"schema": "knowledgeradar-research-delivery-contract/v1", "report_artifact": "report.md", "coverage_recorded": True, "skips_and_degradation_recorded": True},
                "tool_calls": [{"id": "T1", "tool": "kr_research"}],
                "coverage": {"covered_surfaces": ["runtime_probe"], "covered_source_ecologies": []},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return evidence


def test_turn_evaluator_records_unique_receipt_as_probabilistic_not_complete(tmp_path: Path) -> None:
    report = tmp_path / "report.md"
    report.write_text("# 调研 A", encoding="utf-8")
    evidence = _sidecar(tmp_path)
    turn_id = "turn-a"
    session = tmp_path / "session.jsonl"
    session.write_text(
        "\n".join(
            [
                json.dumps({"timestamp": "2026-08-04T00:00:00+00:00", "payload": {"session_id": "thread-a", "type": "session_meta"}}, ensure_ascii=False),
                json.dumps({"timestamp": "2026-08-04T00:00:01+00:00", "payload": {"type": "function_call", "id": "call-a", "name": "mcp__knowledgeradar.kr_research", "internal_chat_message_metadata_passthrough": {"turn_id": turn_id}}}, ensure_ascii=False),
                json.dumps({"timestamp": "2026-08-04T00:00:02+00:00", "payload": {"type": "mcp_tool_call_end", "call_id": "call-a", "turn_id": turn_id, "invocation": {"server": "knowledgeradar", "tool": "kr_research"}}}, ensure_ascii=False),
            ]
        ),
        encoding="utf-8",
    )
    kr_trace = tmp_path / "kr-trace.jsonl"
    kr_trace.write_text(json.dumps({"trace_id": "kr-a", "tool_name": "kr_research", "timestamp": "2026-08-04T00:00:02+00:00", "status": "ok", "strategy": "workflow", "metadata": {"invocation_origin": "root"}}) + "\n", encoding="utf-8")

    trace = build_codex_thread_trace(report=report, evidence=evidence, task_id="task-a", codex_jsonl=session, kr_tool_trace=kr_trace, turn_id=turn_id)
    result = evaluate_trace(trace, require_transcript=True, require_complete_correlation=True)

    assert trace["correlation"]["status"] == "partial"
    assert trace["correlation"]["confidence"] == "probabilistic"
    assert result["status"] == "PASS", result
    assert result["observability"]["codex_turn_visibility"] == "available"


def test_turn_evaluator_marks_missing_transcript_as_unavailable_not_fake_complete(tmp_path: Path) -> None:
    report = tmp_path / "report.md"
    report.write_text("# 调研 A", encoding="utf-8")
    trace = build_codex_thread_trace(report=report, evidence=_sidecar(tmp_path), task_id="task-a")

    result = evaluate_trace(trace)

    assert result["status"] == "PASS"
    assert result["observability"]["codex_turn_visibility"] == "unavailable"
    assert any(item["code"] == "codex_turn_visibility_unavailable" for item in result["findings"])


def test_concurrent_same_tool_receipts_are_left_ambiguous(tmp_path: Path) -> None:
    report = tmp_path / "report.md"
    report.write_text("# 调研 A", encoding="utf-8")
    evidence = _sidecar(tmp_path)
    turn_id = "turn-a"
    session = tmp_path / "session.jsonl"
    session.write_text(
        "\n".join(
            [
                json.dumps({"timestamp": "2026-08-04T00:00:01+00:00", "payload": {"type": "function_call", "id": "call-a", "name": "mcp__knowledgeradar.kr_research", "turn_id": turn_id}}),
                json.dumps({"timestamp": "2026-08-04T00:00:02+00:00", "payload": {"type": "mcp_tool_call_end", "call_id": "call-a", "turn_id": turn_id, "invocation": {"server": "knowledgeradar", "tool": "kr_research"}}}),
            ]
        ),
        encoding="utf-8",
    )
    kr_trace = tmp_path / "kr-trace.jsonl"
    kr_trace.write_text(
        "\n".join(
            [
                json.dumps({"trace_id": "other", "tool_name": "kr_research", "timestamp": "2026-08-04T00:00:02+00:00", "status": "ok", "metadata": {"invocation_origin": "root"}}),
                json.dumps({"trace_id": "own", "tool_name": "kr_research", "timestamp": "2026-08-04T00:00:03+00:00", "status": "ok", "metadata": {"invocation_origin": "root"}}),
            ]
        ) + "\n",
        encoding="utf-8",
    )
    trace = build_codex_thread_trace(report=report, evidence=evidence, task_id="task-a", codex_jsonl=session, kr_tool_trace=kr_trace, turn_id=turn_id)

    assert trace["correlation"]["status"] == "partial"
    assert trace["correlation"]["confidence"] == "none"
    assert trace["correlation"]["ambiguous_host_calls"] == ["call-a"]
    assert trace["kr_receipts"]["matched_receipt_count"] == 2


def test_legacy_unscoped_receipt_cannot_be_complete(tmp_path: Path) -> None:
    report = tmp_path / "report.md"
    report.write_text("# 调研 A", encoding="utf-8")
    session = tmp_path / "session.jsonl"
    session.write_text(json.dumps({"timestamp": "2026-08-04T00:00:01+00:00", "payload": {"type": "mcp_tool_call_end", "call_id": "call-a", "turn_id": "turn-a", "invocation": {"server": "knowledgeradar", "tool": "kr_research"}}}) + "\n", encoding="utf-8")
    kr_trace = tmp_path / "kr-trace.jsonl"
    kr_trace.write_text(json.dumps({"trace_id": "legacy", "tool_name": "kr_research", "timestamp": "2026-08-04T00:00:02+00:00", "status": "ok"}) + "\n", encoding="utf-8")
    trace = build_codex_thread_trace(report=report, evidence=_sidecar(tmp_path), task_id="task-a", codex_jsonl=session, kr_tool_trace=kr_trace, turn_id="turn-a")

    assert trace["correlation"]["status"] == "partial"
    assert trace["correlation"]["method"] == "posthoc_tool_time_receipt_association"
