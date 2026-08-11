from __future__ import annotations

import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.check_research_quality import _execution_steps_status, validate_research_quality


def _roadmap() -> list[dict[str, str]]:
    return [
        {
            "phase": "P0",
            "task": "inspect current evidence",
            "method": "read local files and sidecar metadata",
            "input": "x",
            "output": "y",
            "dependency": "none",
            "acceptance": "pass",
        },
        {
            "phase": "P1",
            "task": "implement governance rule",
            "method": "patch project rules and checker fixtures",
            "input": "y",
            "output": "z",
            "dependency": "P0",
            "acceptance": "pass",
        },
        {
            "phase": "P2",
            "task": "verify report contract",
            "method": "run targeted quality validation",
            "input": "z",
            "output": "done",
            "dependency": "P1",
            "acceptance": "pass",
        },
        {
            "phase": "P3",
            "task": "publish findings",
            "method": "update report and sidecar",
            "input": "done",
            "output": "final",
            "dependency": "P2",
            "acceptance": "pass",
        },
    ]


def _base_sidecar() -> dict:
    return {
        "schema": "knowledgeradar-research-evidence/v1",
        "generated_at": "2026-06-17T00:00:00+08:00",
        "artifact": {"report_path": "report.md"},
        "budget": {"selected_profile": "deep", "remaining_repair_rounds": 1},
        "tool_calls": [{"id": "T001", "tool": "health_check"}],
        "evidence_items": [{"id": "E001", "type": "local_code", "summary": "code"}],
        "claims": [{"id": "C001", "importance": "high", "supporting_evidence_ids": ["E001"]}],
        "claim_evidence_chains": [
            {
                "claim_id": "C001",
                "tool_call_ids": ["T001"],
                "evidence_ids": ["E001"],
                "sources": ["local test fixture"],
                "extracted_facts": ["The fixture records local_code evidence E001."],
                "inference": "The claim is supported by the recorded local code evidence.",
                "limitations": "Synthetic fixture used only for governance checker contracts.",
                "strength": "strong",
            }
        ],
        "coverage": {
            "required_surfaces": ["local_code"],
            "covered_surfaces": ["local_code"],
            "required_source_ecologies": [],
            "covered_source_ecologies": [],
        },
        "roadmap": _roadmap(),
    }


def _deep_governance_report() -> str:
    return """# Governance Report

## 规划图

```mermaid
flowchart TD
  P0["P0"] --> P1["P1"]
  P1 --> P2["P2"]
  P2 --> P3["P3"]
```

## 详细执行步骤

| 阶段 | 做什么 | 怎么做 | 输入 | 输出 | 验收 | 依赖 | 是否改代码 |
|---|---|---|---|---|---|---|---|
| P0 | inspect current evidence | read local files and sidecar metadata | x | y | pass | 无前置依赖 | 不改代码 |
| P1 | implement governance rule | patch project rules and checker fixtures | y | z | pass | P0 | 是 |
| P2 | verify report contract | run targeted quality validation | z | done | pass | P1 | 是 |
| P3 | publish findings | update report and sidecar | done | final | pass | P2 | 不改代码 |
"""


def _add_governance_fields(payload: dict) -> dict:
    payload["roadmap"] = _roadmap()
    payload["known_issues"] = {
        "schema": "knowledgeradar-known-issues/v1",
        "generated_at": "2026-06-16T00:00:00+08:00",
        "report_path": "report.md",
        "issues": [
            {
                "id": "KI-20260616-001",
                "description": "issue",
                "discovered_at": "2026-06-16",
                "status": "已解决",
                "resolved_in_report": "report.md",
                "next_action": "none",
            }
        ],
    }
    payload["decision_register"] = {
        "schema": "knowledgeradar-decision-register/v1",
        "generated_at": "2026-06-16T00:00:00+08:00",
        "report_path": "report.md",
        "decisions": [
            {
                "id": "D001",
                "title": "decision",
                "status": "approved",
                "recommended_action": "approve",
                "options": [{"value": "approve"}, {"value": "reject"}],
                "decision_record_location": ["report.md"],
                "next_review": "next report",
            }
        ],
    }
    payload["process_quality"] = {
        "schema": "knowledgeradar-process-quality/v1",
        "preflight": {
            "schema": "knowledgeradar-research-preflight/v1",
            "status": "PASS",
            "requires_preflight": True,
            "known_issue_inheritance": {"required_issue_ids": []},
        },
        "capability_handshake": {"health_check": True, "get_capabilities": True},
        "local_archaeology": {"performed": True, "scope": ["scripts"], "evidence_ids": ["E001"]},
        "source_ecology_plan": {"considered": ["generic_web_ecology"], "selected": [], "skipped": []},
        "validation_ladder": {"selected_profile": "report_light", "commands": ["check_research_quality.py"]},
        "presentation": [{"surface": "report_section"}],
    }
    return payload


def test_execution_steps_accept_semantic_no_code_boundary() -> None:
    report = """# Governance Report

## 详细执行步骤

1. P0 输入：x。做什么：inspect。怎么做：read files。输出：y。验收：pass。依赖：无前置依赖。边界：无需代码变更。
2. P1 输入：y。做什么：write report。怎么做：edit markdown。输出：z。验收：pass。依赖：P0。边界：无需代码变更。
3. P2 输入：z。做什么：closeout。怎么做：run report gate。输出：done。验收：pass。依赖：P1。边界：无需代码变更。
"""

    ok, issues = _execution_steps_status(report, ["P0", "P1", "P2"], detailed=True)

    assert ok, issues


def test_execution_steps_accept_sidecar_no_code_boundary() -> None:
    report = """# Governance Report

## 详细执行步骤

1. P0 输入：x。做什么：inspect。怎么做：read files。输出：y。验收：pass。依赖：无前置依赖。
2. P1 输入：y。做什么：write report。怎么做：edit markdown。输出：z。验收：pass。依赖：P0。
3. P2 输入：z。做什么：closeout。怎么做：run report gate。输出：done。验收：pass。依赖：P1。
"""

    ok, issues = _execution_steps_status(
        report,
        ["P0", "P1", "P2"],
        detailed=True,
        payload={"change_boundary": {"code_changes": False}},
    )

    assert ok, issues


def test_deep_research_requires_questioner_checkpoints(tmp_path: Path) -> None:
    report = tmp_path / "report.md"
    report.write_text(_deep_governance_report(), encoding="utf-8")
    evidence = tmp_path / "report.evidence.json"
    evidence.write_text(json.dumps(_add_governance_fields(_base_sidecar()), ensure_ascii=False), encoding="utf-8")

    result = validate_research_quality(report, evidence)

    assert result["status"] == "needs_revision"
    assert any(item["code"] == "missing_questioner_checkpoints" for item in result["hard_findings"])
    assert any(item["id"] == "R007" for item in result["repair_requests"])


def test_deep_research_accepts_questioner_checkpoints(tmp_path: Path) -> None:
    report = tmp_path / "report.md"
    report.write_text(_deep_governance_report(), encoding="utf-8")
    payload = _add_governance_fields(_base_sidecar())
    payload["questioner_checkpoints"] = [
        {
            "stage": "after_local_archaeology",
            "questions": ["本地事实是否足够？"],
            "decision": "continue",
            "reason": "需要外部证据。",
        },
        {
            "stage": "after_first_external_round",
            "questions": ["来源是否偏窄？"],
            "decision": "expand",
            "reason": "需要补充来源生态。",
        },
        {
            "stage": "before_final_report",
            "questions": ["结论是否有证据？"],
            "decision": "finalize",
            "reason": "证据和路线图已齐备。",
        },
    ]
    evidence = tmp_path / "report.evidence.json"
    evidence.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    result = validate_research_quality(report, evidence)

    assert result["status"] == "pass", result


def test_deep_governance_requires_planning_diagram_and_execution_steps(tmp_path: Path) -> None:
    report = tmp_path / "report.md"
    report.write_text("# 治理报告\n\n只有文字，没有图和步骤。\n", encoding="utf-8")
    payload = _add_governance_fields(_base_sidecar())
    payload["questioner_checkpoints"] = [
        {"stage": "after_local_archaeology", "questions": ["q"], "decision": "continue", "reason": "r"},
        {"stage": "after_first_external_round", "questions": ["q"], "decision": "continue", "reason": "r"},
        {"stage": "before_final_report", "questions": ["q"], "decision": "finalize", "reason": "r"},
    ]
    evidence = tmp_path / "report.evidence.json"
    evidence.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    result = validate_research_quality(report, evidence)

    assert result["status"] == "needs_revision"
    assert any(item["code"] == "missing_or_invalid_planning_diagram" for item in result["hard_findings"])
    assert any(item["code"] == "missing_or_incomplete_execution_steps" for item in result["hard_findings"])


def test_deep_governance_requires_known_issues_decisions_and_process_quality(tmp_path: Path) -> None:
    report = tmp_path / "report.md"
    report.write_text(_deep_governance_report(), encoding="utf-8")
    payload = _base_sidecar()
    payload["roadmap"] = _roadmap()
    payload["questioner_checkpoints"] = [
        {"stage": "after_local_archaeology", "questions": ["q"], "decision": "continue", "reason": "r"},
        {"stage": "after_first_external_round", "questions": ["q"], "decision": "continue", "reason": "r"},
        {"stage": "before_final_report", "questions": ["q"], "decision": "finalize", "reason": "r"},
    ]
    evidence = tmp_path / "report.evidence.json"
    evidence.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    result = validate_research_quality(report, evidence)

    assert result["status"] == "needs_revision"
    codes = {item["code"] for item in result["hard_findings"]}
    assert "missing_or_invalid_known_issues" in codes
    assert "missing_or_invalid_decision_register" in codes
    assert "missing_or_invalid_process_quality" in codes


def test_deep_governance_requires_detailed_planning_roadmap(tmp_path: Path) -> None:
    report = tmp_path / "report.md"
    report.write_text(_deep_governance_report(), encoding="utf-8")
    payload = _add_governance_fields(_base_sidecar())
    payload["roadmap"] = [
        {"phase": "P0", "input": "x", "output": "y", "dependency": "none", "acceptance": "pass"},
        {"phase": "P1", "task": "task", "method": "method", "input": "y"},
    ]
    payload["questioner_checkpoints"] = [
        {"stage": "after_local_archaeology", "questions": ["q"], "decision": "continue", "reason": "r"},
        {"stage": "after_first_external_round", "questions": ["q"], "decision": "continue", "reason": "r"},
        {"stage": "before_final_report", "questions": ["q"], "decision": "finalize", "reason": "r"},
    ]
    evidence = tmp_path / "report.evidence.json"
    evidence.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    result = validate_research_quality(report, evidence)

    assert result["status"] == "needs_revision"
    assert any(item["code"] == "missing_or_incomplete_planning_roadmap" for item in result["hard_findings"])
    assert any(item["id"] == "R015" for item in result["repair_requests"])


def test_deep_governance_allows_legacy_roadmap_before_detailed_contract(tmp_path: Path) -> None:
    report = tmp_path / "report.md"
    report.write_text(
        """# Governance Report

## 规划图

```mermaid
flowchart TD
  P0["P0"] --> P1["P1"]
  P1 --> P2["P2"]
  P2 --> P3["P3"]
```

## 详细执行步骤

| 阶段 | 输入 | 动作 | 输出 | 验收 | 依赖 | 是否改代码 |
|---|---|---|---|---|---|---|
| P0 | x | inspect | y | pass | 无前置依赖 | 不改代码 |
| P1 | y | inspect | z | pass | P0 | 不改代码 |
| P2 | z | report | done | pass | P1 | 不改代码 |
| P3 | done | closeout | final | pass | P2 | 不改代码 |
""",
        encoding="utf-8",
    )
    payload = _add_governance_fields(_base_sidecar())
    payload["generated_at"] = "2026-06-16T00:00:00+08:00"
    payload["roadmap"] = [
        {"phase": "P0", "input": "x", "output": "y", "dependency": "none", "acceptance": "pass"},
        {"phase": "P1", "input": "y", "output": "z", "dependency": "P0", "acceptance": "pass"},
        {"phase": "P2", "input": "z", "output": "done", "dependency": "P1", "acceptance": "pass"},
        {"phase": "P3", "input": "done", "output": "final", "dependency": "P2", "acceptance": "pass"},
    ]
    payload["questioner_checkpoints"] = [
        {"stage": "after_local_archaeology", "questions": ["q"], "decision": "continue", "reason": "r"},
        {"stage": "after_first_external_round", "questions": ["q"], "decision": "continue", "reason": "r"},
        {"stage": "before_final_report", "questions": ["q"], "decision": "finalize", "reason": "r"},
    ]
    evidence = tmp_path / "report.evidence.json"
    evidence.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    result = validate_research_quality(report, evidence)

    assert result["status"] == "pass", result


def test_deep_governance_enforces_detailed_roadmap_for_new_reports(tmp_path: Path) -> None:
    report = tmp_path / "report.md"
    report.write_text(_deep_governance_report(), encoding="utf-8")
    payload = _add_governance_fields(_base_sidecar())
    payload["generated_at"] = "2026-06-17T00:00:00+08:00"
    payload["roadmap"] = [
        {"phase": "P0", "input": "x", "output": "y", "dependency": "none", "acceptance": "pass"},
        {"phase": "P1", "input": "y", "output": "z", "dependency": "P0", "acceptance": "pass"},
        {"phase": "P2", "input": "z", "output": "done", "dependency": "P1", "acceptance": "pass"},
    ]
    payload["questioner_checkpoints"] = [
        {"stage": "after_local_archaeology", "questions": ["q"], "decision": "continue", "reason": "r"},
        {"stage": "after_first_external_round", "questions": ["q"], "decision": "continue", "reason": "r"},
        {"stage": "before_final_report", "questions": ["q"], "decision": "finalize", "reason": "r"},
    ]
    evidence = tmp_path / "report.evidence.json"
    evidence.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    result = validate_research_quality(report, evidence)

    assert result["status"] == "needs_revision"
    assert any(item["code"] == "missing_or_incomplete_planning_roadmap" for item in result["hard_findings"])


def test_deep_governance_requires_research_preflight(tmp_path: Path) -> None:
    report = tmp_path / "report.md"
    report.write_text(_deep_governance_report(), encoding="utf-8")
    payload = _add_governance_fields(_base_sidecar())
    payload["questioner_checkpoints"] = [
        {"stage": "after_local_archaeology", "questions": ["q"], "decision": "continue", "reason": "r"},
        {"stage": "after_first_external_round", "questions": ["q"], "decision": "continue", "reason": "r"},
        {"stage": "before_final_report", "questions": ["q"], "decision": "finalize", "reason": "r"},
    ]
    payload["process_quality"].pop("preflight")
    evidence = tmp_path / "report.evidence.json"
    evidence.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    result = validate_research_quality(report, evidence)

    assert result["status"] == "needs_revision"
    assert any(item["code"] == "missing_or_invalid_research_preflight" for item in result["hard_findings"])


def test_deep_governance_accepts_preflight_ref_and_inherited_issue(tmp_path: Path) -> None:
    report = tmp_path / "report.md"
    report.write_text(_deep_governance_report(), encoding="utf-8")
    payload = _add_governance_fields(_base_sidecar())
    payload["questioner_checkpoints"] = [
        {"stage": "after_local_archaeology", "questions": ["q"], "decision": "continue", "reason": "r"},
        {"stage": "after_first_external_round", "questions": ["q"], "decision": "continue", "reason": "r"},
        {"stage": "before_final_report", "questions": ["q"], "decision": "finalize", "reason": "r"},
    ]
    payload["known_issues"]["issues"].append(
        {
            "id": "KI-OLD-001",
            "description": "inherited",
            "discovered_at": "2026-06-15",
            "status": "未解决",
            "resolved_in_report": None,
            "next_action": "carry",
        }
    )
    payload["process_quality"].pop("preflight")
    payload["process_quality"]["preflight_ref"] = "report.preflight.json"
    (tmp_path / "report.preflight.json").write_text(
        json.dumps(
            {
                "schema": "knowledgeradar-research-preflight/v1",
                "status": "PASS",
                "requires_preflight": True,
                "known_issue_inheritance": {"required_issue_ids": ["KI-OLD-001"]},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    evidence = tmp_path / "report.evidence.json"
    evidence.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    result = validate_research_quality(report, evidence)

    assert result["status"] == "pass", result
