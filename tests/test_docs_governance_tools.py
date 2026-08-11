from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.build_agent_tool_trace import build_trace
from scripts.generate_docs_index import build_index, render_markdown
from scripts.generate_process_docs_index import build_index as build_process_index
from scripts.generate_script_lifecycle_inventory import build_inventory
from scripts.probe_source_ecology_boundaries import build_probe_plan
from scripts.suggest_capability_atlas_updates import suggest_updates


def test_docs_index_includes_stable_chinese_reports() -> None:
    payload = build_index()
    paths = {item["path"] for item in payload["documents"]}

    assert "docs/reference/知识雷达文档治理.md" in paths
    assert "docs/reference/感知层自然调用机制.md" in paths
    assert "docs/reference/KnowledgeRadar-登录风控状态机系统考古与统一方案-2026-06-30.md" not in paths
    assert "docs/research/kr-perception-layer-nonuse-root-cause-2026-06-15.md" not in paths
    assert "docs/doc-index.json" not in paths
    assert "docs/文档索引.md" not in paths
    assert "docs/script-lifecycle-inventory.json" not in paths
    assert "docs/脚本生命周期清单.md" not in paths
    assert "docs/process-doc-index.json" not in paths
    assert "docs/过程文档索引.md" not in paths

    rendered = render_markdown(payload)
    assert "docs/` 是当前 KnowledgeRadar 项目的统一文档入口" in rendered


def test_script_lifecycle_inventory_marks_gate_scripts_s0() -> None:
    payload = build_inventory()
    by_path = {item["path"]: item for item in payload["entries"]}

    assert by_path["scripts/kr_quality_gate.py"]["level"] == "S0"
    assert by_path[".codex/hooks/quality_gate_hook.py"]["level"] == "S0"
    assert by_path[".githooks/pre-commit"]["level"] == "S0"
    assert by_path["scripts/compare_research_budget_profiles.py"]["level"] == "S2"
    assert by_path["scripts/kr_test_impact_index.py"]["level"] == "S2"
    assert payload["counts"]["S0"] >= 3


def test_source_ecology_boundary_probe_plan_is_plan_only() -> None:
    payload = build_probe_plan("bilibili_video_ecology", max_queries=1, include_detail=True)

    assert payload["schema"] == "knowledgeradar-source-ecology-boundary-probe-plan/v1"
    assert payload["status"] == "PASS"
    plan = payload["plans"][0]
    assert plan["ecology"] == "bilibili_video_ecology"
    assert "search_bilibili" in plan["candidate_tools"]
    assert plan["side_effects"] == {
        "network_calls": False,
        "browser_launch": False,
        "model_calls": False,
        "writes": False,
    }


def test_decision_logs_can_suggest_capability_atlas_updates() -> None:
    payload = suggest_updates(
        {
            "total_events": 3,
            "success_count": 1,
            "failure_count": 2,
            "success_rate": 0.33,
            "by_platform": {"小红书": {"success": 0, "failure": 2}},
            "failure_tags": {"by_tag": {"anti_bot": 1, "empty_detail": 1}},
        }
    )

    assert payload["schema"] == "knowledgeradar-capability-atlas-update-suggestions/v1"
    assert payload["status"] == "PASS"
    assert payload["suggestions"][0]["ecology"] == "xiaohongshu_experience_ecology"
    assert payload["suggestions"][0]["action"] == "review_failure_modes"


def test_process_docs_index_keeps_history_out_of_current_docs() -> None:
    payload = build_process_index()
    paths = {item["path"] for item in payload["documents"]}

    assert payload["document_count"] > 0
    if (ROOT / "design_docs").exists():
        assert "design_docs/INDEX.md" in paths
    assert any(path.startswith("archive/history/") for path in paths)
    source_ids = {source["id"] for source in payload["sources"]}
    assert "archive_history" in source_ids
    if (ROOT / "design_docs").exists():
        assert "design_docs" in source_ids


def test_agent_tool_trace_from_sidecar_can_score_quality_contract(tmp_path: Path) -> None:
    report = tmp_path / "report.md"
    report.write_text("# report\n", encoding="utf-8")
    evidence = tmp_path / "report.evidence.json"
    evidence.write_text(
        json.dumps(
            {
                "schema": "knowledgeradar-research-evidence/v1",
                "artifact": {"report_path": str(report), "task_prompt_hash": "sha256:test"},
                "budget": {"selected_profile": "balanced", "remaining_repair_rounds": 1},
                "tool_calls": [
                    {"id": "T001", "tool": "health_check", "status": "ok"},
                    {"id": "T002", "tool": "get_capabilities", "status": "ok"},
                    {"id": "T003", "tool": "analyze_decision_logs", "status": "ok"},
                ],
                "evidence_items": [
                    {"id": "E001", "type": "runtime_probe", "summary": "runtime"},
                    {"id": "E002", "type": "local_code", "summary": "code"},
                    {"id": "E003", "type": "prior_report", "summary": "prior"},
                ],
                "claims": [],
                "coverage": {
                    "covered_surfaces": ["runtime_probe", "local_code", "prior_report"],
                    "covered_source_ecologies": [],
                },
                "roadmap": [{"phase": "P0"}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    trace = build_trace(report=report, evidence=evidence, task_id="research_quality_framework_diagnosis")

    assert trace["tool_calls"] == ["health_check", "get_capabilities", "analyze_decision_logs"]
    assert set(trace["evidence_surfaces"]) == {"runtime_probe", "local_code", "prior_report"}
    assert "evidence_register" in trace["output_artifacts"]
    assert "quality_check" in trace["output_artifacts"]
    assert "roadmap" in trace["output_artifacts"]
    assert trace["repair_loop"] is True
