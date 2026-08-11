from __future__ import annotations

import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import server
from runtime.research_ledger import record_tool_receipt
from scripts.review_research_state import review_research_state


def test_route_plan_keeps_all_ecologies_available_when_only_one_is_hint() -> None:
    plan = server._research_route_plan(
        task="研究一个开源项目的实现和使用体验",
        mode="deep_route",
        budget="balanced",
        source_ecology_hints=["github_repository_ecology"],
        evidence_needs="实现证据",
    )

    considered = {item["source_ecology"] for item in plan["considered_source_ecologies"]}
    assert "github_repository_ecology" in plan["selected_source_ecologies"]
    assert "xiaohongshu_experience_ecology" in considered
    assert "zhihu_discussion_ecology" in considered
    assert plan["candidate_lifecycle"][-1] == "degraded_or_blocked"


def test_candidate_records_are_upgradeable_without_exposing_query_text() -> None:
    records = server._research_candidate_records(
        "github_repository_ecology",
        [{"title": "repo", "url": "https://github.com/example/repo"}],
    )

    assert records[0]["candidate_id"].startswith("candidate-")
    assert records[0]["stage"] == "discovered_candidate"
    assert "get_content_detail" not in records[0]["detail_affordance"]


def test_kr_research_records_query_family_as_fingerprint(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("KR_STATE_DIR", str(tmp_path))

    result = server.kr_research(
        task="研究候选机制",
        mode="plan_only",
        budget="fast",
        evidence_needs="唯一的额外查询文本不应进入事件账本",
        research_task_id="query-family-test",
    )

    assert result["research_task"]["research_task_id"] == "query-family-test"
    payload = json.loads((tmp_path / "research_tasks" / "query-family-test.json").read_text(encoding="utf-8"))
    serialized = json.dumps(payload, ensure_ascii=False)
    assert "唯一的额外查询文本不应进入事件账本" not in serialized
    assert any(item.get("kind") == "query_family_created" and str(item.get("query_fingerprint")).startswith("hmac-sha256:") for item in payload["events"])


def test_mcp_research_ledger_tools_share_one_task_receipt(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("KR_STATE_DIR", str(tmp_path))

    route = server.kr_research(task="验证候选账本工具", mode="plan_only", budget="fast", research_task_id="ledger-mcp-test")
    task_id = route["research_task"]["research_task_id"]
    discovery = record_tool_receipt(task_id=task_id, trace_id="trace-mcp-discovery", tool="kr_web_search", status="ok")["receipt"]
    detail = record_tool_receipt(task_id=task_id, trace_id="trace-mcp-detail", tool="get_content_detail", status="ok")["receipt"]
    recorded = server.record_research_candidates_tool(
        research_task_id=task_id,
        source_ecology="generic_web_ecology",
        tool="kr_web_search",
        candidates=[{"id": "candidate-1"}],
        query="不应写入账本的原查询",
        language="zh",
        intent_label="evidence",
        receipt_id=discovery["receipt_id"],
    )

    assert recorded["status"] == "recorded"
    candidate_id = recorded["candidates"][0]["candidate_id"]
    advanced = server.advance_research_candidate(
        research_task_id=task_id,
        candidate_id=candidate_id,
        stage="identity_checked",
        tool="get_content_detail",
        outcome="checked",
        evidence_receipt_ids=[detail["receipt_id"]],
    )
    reviewed = server.review_research_progress(research_task_id=task_id, phase="before_delivery")

    assert advanced["status"] == "recorded"
    assert reviewed["status"] == "reviewed"
    persisted = (tmp_path / "research_tasks" / "ledger-mcp-test.json").read_text(encoding="utf-8")
    assert "不应写入账本的原查询" not in persisted


def test_research_state_review_marks_candidate_only_high_claim_as_draft(tmp_path: Path) -> None:
    report = tmp_path / "report.md"
    report.write_text("# 真实调研", encoding="utf-8")
    evidence = tmp_path / "report.evidence.json"
    evidence.write_text(
        json.dumps(
            {
                "research_task": {"task_id": "t", "objective": "真实调研"},
                "evidence_items": [{"id": "E1", "type": "web", "evidence_stage": "discovered_candidate"}],
                "claims": [{"id": "C1", "importance": "critical", "supporting_evidence_ids": ["E1"]}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = review_research_state(report=report, evidence=evidence)

    assert result["research_state"] == "research_draft"
    assert {item["code"] for item in result["repair_requests"]} >= {"candidate_only_high_claim", "independence_not_established"}


def test_research_state_review_uses_optional_runtime_ledger_without_tool_prescription(tmp_path: Path) -> None:
    report = tmp_path / "report.md"
    report.write_text("# 真实调研", encoding="utf-8")
    evidence = tmp_path / "report.evidence.json"
    evidence.write_text(json.dumps({"schema": "knowledgeradar-research-evidence/v1", "claims": [], "evidence_items": []}), encoding="utf-8")
    ledger = tmp_path / "ledger.json"
    ledger.write_text(json.dumps({"candidate_pool": [{"candidate_id": "candidate-1", "stage": "discovered_candidate"}], "events": []}), encoding="utf-8")

    result = review_research_state(report=report, evidence=evidence, ledger=ledger)

    assert result["research_state"] == "research_draft"
    assert {item["code"] for item in result["repair_requests"]} >= {"ledger_candidate_only", "ledger_stop_review_missing"}
