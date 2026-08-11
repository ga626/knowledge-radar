from __future__ import annotations

import json
from pathlib import Path

from scripts.research_preflight import attach_preflight, build_preflight


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_research_preflight_inherits_unresolved_known_issues(tmp_path: Path) -> None:
    previous = tmp_path / "previous.evidence.json"
    _write_json(
        previous,
        {
            "schema": "knowledgeradar-research-evidence/v1",
            "known_issues": {
                "schema": "knowledgeradar-known-issues/v1",
                "issues": [
                    {
                        "id": "KI-OPEN",
                        "description": "open",
                        "discovered_at": "2026-06-15",
                        "status": "未解决",
                        "resolved_in_report": None,
                        "next_action": "carry",
                    },
                    {
                        "id": "KI-DONE",
                        "description": "done",
                        "discovered_at": "2026-06-15",
                        "status": "已解决",
                        "resolved_in_report": "previous.md",
                        "next_action": "none",
                    },
                ],
            },
        },
    )
    report = tmp_path / "report.md"
    report.write_text("# 治理报告\n\n```mermaid\nflowchart TD\n  P0 --> P1\n```\n\n## 详细执行步骤\n", encoding="utf-8")
    evidence = tmp_path / "report.evidence.json"
    _write_json(
        evidence,
        {
            "schema": "knowledgeradar-research-evidence/v1",
            "artifact": {"report_path": str(report), "title": "治理报告"},
            "budget": {"selected_profile": "deep"},
            "tool_calls": [{"tool": "health_check"}, {"tool": "get_capabilities"}],
            "process_quality": {
                "schema": "knowledgeradar-process-quality/v1",
                "capability_handshake": {"health_check": True, "get_capabilities": True},
                "local_archaeology": {"performed": True},
                "source_ecology_plan": {"considered": ["generic_web_ecology"], "selected": [], "skipped": []},
                "validation_ladder": {"selected_profile": "report_light", "commands": ["check_research_quality.py"]},
                "presentation": [{"surface": "report"}],
            },
        },
    )

    preflight = build_preflight(report, evidence, [previous])
    output = tmp_path / "report.preflight.json"
    attach_preflight(evidence=evidence, output=output, previous_evidence=[previous], preflight=preflight)

    updated = json.loads(evidence.read_text(encoding="utf-8"))
    issue_ids = {item["id"] for item in updated["known_issues"]["issues"]}
    assert "KI-OPEN" in issue_ids
    assert "KI-DONE" not in issue_ids
    assert updated["process_quality"]["preflight_ref"].endswith("report.preflight.json")
    assert updated["process_quality"]["known_issue_inheritance"]["required_issue_ids"] == ["KI-OPEN"]
