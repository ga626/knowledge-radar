from __future__ import annotations

import hashlib
import json

from runtime.research_ledger import close_task, open_task, record_candidates, record_tool_receipt, review_task


def _passing_quality_receipt(tmp_path):
    report = tmp_path / "report.md"
    evidence = tmp_path / "report.evidence.json"
    receipt = tmp_path / "quality.receipt.json"
    report.write_text("# test", encoding="utf-8")
    evidence.write_text("{}", encoding="utf-8")
    receipt.write_text(json.dumps({
        "status": "pass", "report_path": str(report.resolve()), "evidence_path": str(evidence.resolve()),
        "report_sha256": hashlib.sha256(report.read_bytes()).hexdigest(),
        "evidence_sha256": hashlib.sha256(evidence.read_bytes()).hexdigest(),
    }), encoding="utf-8")
    return report, evidence, receipt


def test_research_ledger_fails_closed_until_every_considered_ecology_has_outcome(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("KR_STATE_DIR", str(tmp_path))
    task = open_task(
        objective="真实研究",
        budget="deep",
        task_id="test-ledger",
        considered=[
            {"source_ecology": "generic_web_ecology", "candidate_tools": ["kr_web_search"]},
            {"source_ecology": "zhihu_discussion_ecology", "candidate_tools": ["search_zhihu"]},
        ],
    )
    incomplete = close_task(
        task_id=task["research_task_id"],
        ecology_outcomes=[{"source_ecology": "generic_web_ecology", "outcome": "used"}],
        stop_rationale="已有足够证据",
        key_claims=[{"id": "C1", "importance": "high", "supporting_evidence_ids": ["E1"]}],
        quality_status="pass",
    )
    assert incomplete["status"] == "needs_repair"
    assert incomplete["closeout"]["missing_outcomes"] == ["generic_web_ecology", "zhihu_discussion_ecology"]

    generic = record_tool_receipt(task_id=task["research_task_id"], trace_id="trace-generic", tool="kr_web_search", status="ok")["receipt"]
    zhihu = record_tool_receipt(task_id=task["research_task_id"], trace_id="trace-zhihu", tool="search_zhihu", status="ok")["receipt"]
    report, evidence, quality_receipt = _passing_quality_receipt(tmp_path)

    complete = close_task(
        task_id=task["research_task_id"],
        ecology_outcomes=[
            {"source_ecology": "generic_web_ecology", "outcome": "used", "receipt_ids": [generic["receipt_id"]]},
            {"source_ecology": "zhihu_discussion_ecology", "outcome": "not_relevant", "reason": "本任务不需要观点谱系", "receipt_ids": [zhihu["receipt_id"]], "claim_gap_ids": ["gap-initial"], "reopen_condition": "claim_scope_changes"},
        ],
        stop_rationale="关键结论已有独立支撑，余下生态边际收益低。",
        key_claims=[{"id": "C1", "importance": "high", "supporting_evidence_ids": ["E1", "E2"], "supporting_receipt_ids": [generic["receipt_id"]]}],
        quality_status="pass",
        transcript_status="partial",
        report_path=str(report), evidence_path=str(evidence), quality_receipt_path=str(quality_receipt),
    )
    assert complete["status"] == "accepted_for_decision"


def test_progress_review_explains_candidate_only_gap_without_prescribing_a_tool(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("KR_STATE_DIR", str(tmp_path))
    task = open_task(objective="真实研究", budget="deep", task_id="review-ledger", considered=[])
    tool_receipt = record_tool_receipt(task_id=task["research_task_id"], trace_id="trace-candidate", tool="search_bilibili", status="ok")["receipt"]
    record_candidates(
        task_id=task["research_task_id"],
        source_ecology="bilibili_video_ecology",
        tool="search_bilibili",
        items=[{"id": "video-1", "title": "候选"}],
        query="视频证据",
        receipt_id=tool_receipt["receipt_id"],
    )

    review = review_task(task_id=task["research_task_id"], phase="before_delivery")

    assert review["stop_assessment"] == "not_ready"
    assert {item["code"] for item in review["gaps"]} >= {"candidate_only", "counterevidence_not_recorded"}
    assert "tool" not in review["autonomy_boundary"].lower() or "chooses" in review["autonomy_boundary"]
