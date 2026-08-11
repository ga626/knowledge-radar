from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.kr_natural_calling_eval import score_trace, validate_cases


def test_natural_calling_eval_cases_are_valid() -> None:
    result = validate_cases()

    assert result["status"] == "PASS", result["issues"]
    assert result["light_case_count"] >= 2
    assert result["full_case_count"] >= 4
    assert result["quality_case_count"] >= 2


def test_natural_calling_trace_scoring_rejects_planning_tools(tmp_path: Path) -> None:
    trace_path = tmp_path / "trace.json"
    trace_path.write_text(json.dumps(["kr_web_search", "plan_research"]), encoding="utf-8")

    result = score_trace("simple_current_web", trace_path)

    assert result["status"] == "FAIL"
    assert any(item["code"] == "forbidden_called" for item in result["misses"])


def test_natural_calling_trace_scoring_rejects_independent_builtin_web(tmp_path: Path) -> None:
    trace_path = tmp_path / "trace.json"
    trace_path.write_text(json.dumps({"tool_calls": ["builtin_web_search", "kr_research"]}), encoding="utf-8")

    result = score_trace("simple_current_web", trace_path)

    assert result["status"] == "FAIL"
    miss = next(item for item in result["misses"] if item["code"] == "invalid_host_internal_web_wave")
    assert "builtin_web_before_kr_admission" in miss["issues"]
    assert "missing_host_internal_web_wave_record" in miss["issues"]


def test_natural_calling_trace_scoring_accepts_builtin_web_inside_kr_wave(tmp_path: Path) -> None:
    trace_path = tmp_path / "trace.json"
    trace_path.write_text(
        json.dumps(
            {
                "tool_calls": ["kr_research", "builtin_web_search"],
                "process_quality": {
                    "host_internal_web_wave": {
                        "wave_id": "host_internal_web_wave",
                        "strategy_tree": "web_search.provider_wave",
                        "relationship_to_kr": "finger_of_kr_hand",
                        "reason": "official docs are best retrieved by host web",
                    },
                    "generic_web_fallback": {
                        "wave_id": "host_internal_web_wave",
                        "strategy_tree": "web_search.provider_wave",
                        "relationship_to_kr": "finger_of_kr_hand",
                        "reason": "compatibility alias for process quality scoring",
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    result = score_trace("simple_current_web", trace_path)

    assert result["status"] == "PASS", result["misses"]


def test_natural_calling_trace_scoring_checks_quality_contract(tmp_path: Path) -> None:
    trace_path = tmp_path / "trace.json"
    trace_path.write_text(
        json.dumps(
            {
                "tool_calls": ["health_check", "get_capabilities", "analyze_decision_logs"],
                "evidence_surfaces": ["local_code"],
                "output_artifacts": ["evidence_register"],
            }
        ),
        encoding="utf-8",
    )

    result = score_trace("research_quality_framework_diagnosis", trace_path)

    assert result["status"] == "FAIL"
    assert any(item["code"] == "missing_evidence_surfaces" for item in result["misses"])
    assert any(item["code"] == "missing_output_artifacts" for item in result["misses"])
    assert any(item["code"] == "missing_repair_loop" for item in result["misses"])
    assert any(item["code"] == "missing_process_quality" for item in result["misses"])


def test_natural_calling_trace_scoring_accepts_quality_contract(tmp_path: Path) -> None:
    trace_path = tmp_path / "trace.json"
    trace_path.write_text(
        json.dumps(
            {
                "tool_calls": ["health_check", "get_capabilities", "analyze_decision_logs"],
                "evidence_surfaces": ["local_code", "prior_report", "runtime_probe"],
                "output_artifacts": ["evidence_register", "quality_check", "roadmap", "known_issues", "decision_register", "claim_evidence_chains"],
                "repair_loop": True,
                "known_issues": {"issues": [{"id": "KI-20260616-001"}]},
                "decision_register": {"decisions": [{"id": "D001"}]},
                "process_quality": {
                    "source_ecology_plan": {"considered": ["generic_web_ecology"]},
                    "validation_ladder": {"selected_profile": "report_light", "commands": ["check_research_quality.py"]},
                    "presentation": [{"surface": "report"}],
                },
                "claim_evidence_chains": [
                    {
                        "claim_id": "C001",
                        "tool_call_ids": ["T001"],
                        "evidence_ids": ["E001"],
                        "sources": [{"type": "local", "value": "x"}],
                        "extracted_facts": ["fact"],
                        "inference": "inference",
                        "limitations": ["limit"],
                        "strength": "strong",
                    }
                ],
                "questioner_checkpoints": [
                    {"stage": "after_local_archaeology"},
                    {"stage": "after_first_external_round"},
                    {"stage": "before_final_report"},
                ],
            }
        ),
        encoding="utf-8",
    )

    result = score_trace("research_quality_framework_diagnosis", trace_path)

    assert result["status"] == "PASS", result["misses"]


def test_natural_calling_trace_scoring_checks_source_ecologies(tmp_path: Path) -> None:
    trace_path = tmp_path / "trace.json"
    trace_path.write_text(
        json.dumps(
            {
                "tool_calls": ["kr_web_search"],
                "evidence_surfaces": ["source_ecology"],
                "source_ecologies": ["wechat_public_article_ecology"],
                "output_artifacts": ["evidence_register"],
            }
        ),
        encoding="utf-8",
    )

    result = score_trace("public_discourse_source_ecology", trace_path)

    assert result["status"] == "FAIL"
    assert any(item["code"] == "missing_source_ecologies" for item in result["misses"])


def test_natural_calling_trace_scoring_accepts_source_ecology_via_evidence_items(tmp_path: Path) -> None:
    trace_path = tmp_path / "trace.json"
    trace_path.write_text(
        json.dumps(
            {
                "tool_calls": ["kr_web_search", "search_zhihu"],
                "evidence_surfaces": ["source_ecology"],
                "output_artifacts": ["evidence_register"],
                "skip_reasons": [{"reason": "detail not needed for source-ecology fixture"}],
                "evidence_items": [
                    {"id": "E001", "source_ecology": "wechat_public_article_ecology"},
                    {"id": "E002", "source_ecology": "zhihu_discussion_ecology"},
                ],
            }
        ),
        encoding="utf-8",
    )

    result = score_trace("public_discourse_source_ecology", trace_path)

    assert result["status"] == "PASS", result["misses"]
    assert result["source_ecologies"] == ["wechat_public_article_ecology", "zhihu_discussion_ecology"]


def test_natural_calling_trace_scoring_rejects_incomplete_agentic_search(tmp_path: Path) -> None:
    trace_path = tmp_path / "trace.json"
    trace_path.write_text(
        json.dumps(
            {
                "tool_calls": ["health_check", "get_capabilities", "analyze_decision_logs"],
                "evidence_surfaces": ["local_code", "prior_report", "runtime_probe", "web"],
                "output_artifacts": ["evidence_register", "quality_check", "roadmap", "agentic_search_governance"],
                "repair_loop": True,
                "process_quality": {"source_ecology_plan": {"considered": ["generic_web_ecology"]}},
                "questioner_checkpoints": [
                    {"stage": "after_local_archaeology"},
                    {"stage": "after_first_external_round"},
                    {"stage": "before_final_report"},
                ],
                "agentic_search": {"schema": "knowledgeradar-agentic-search-governance/v1"},
            }
        ),
        encoding="utf-8",
    )

    result = score_trace("p8_agentic_search_governance", trace_path)

    assert result["status"] == "FAIL"
    assert any(item["code"] == "invalid_agentic_search_governance" for item in result["misses"])


def test_natural_calling_trace_scoring_accepts_agentic_search_governance(tmp_path: Path) -> None:
    trace_path = tmp_path / "trace.json"
    trace_path.write_text(
        json.dumps(
            {
                "tool_calls": ["health_check", "get_capabilities", "analyze_decision_logs"],
                "evidence_surfaces": ["local_code", "prior_report", "runtime_probe", "web"],
                "output_artifacts": ["evidence_register", "quality_check", "roadmap", "agentic_search_governance"],
                "repair_loop": True,
                "process_quality": {"source_ecology_plan": {"considered": ["generic_web_ecology"]}},
                "questioner_checkpoints": [
                    {"stage": "after_local_archaeology"},
                    {"stage": "after_first_external_round"},
                    {"stage": "before_final_report"},
                ],
                "agentic_search": {
                    "schema": "knowledgeradar-agentic-search-governance/v1",
                    "agent_policy": "model_decides_rounds_tools_and_stop",
                    "budget_semantics": {"position": "runtime_sla_not_tool_route"},
                    "evidence_coverage_ledger": [
                        {
                            "claim_id": "C001",
                            "evidence_role": "claim_support",
                            "covered_by": ["E001"],
                            "gap": "",
                            "next_action": "covered",
                        }
                    ],
                    "marginal_yield": {
                        "continue_condition": "continue if high claims are uncovered",
                        "stop_condition": "stop when gaps are explicit",
                        "current_assessment": "sufficient_or_explicitly_bounded",
                    },
                    "stopping_criteria": {"criteria": ["important claims covered"], "decision": "stop"},
                },
            }
        ),
        encoding="utf-8",
    )

    result = score_trace("p8_agentic_search_governance", trace_path)

    assert result["status"] == "PASS", result["misses"]


def test_natural_calling_process_quality_requires_questioner_stages(tmp_path: Path) -> None:
    trace_path = tmp_path / "trace.json"
    trace_path.write_text(
        json.dumps(
            {
                "tool_calls": ["health_check", "get_capabilities", "analyze_decision_logs"],
                "evidence_surfaces": ["local_code", "prior_report", "runtime_probe"],
                "output_artifacts": ["evidence_register", "quality_check", "roadmap"],
                "repair_loop": True,
                "questioner_checkpoints": [{"stage": "after_local_archaeology"}],
            }
        ),
        encoding="utf-8",
    )

    result = score_trace("research_quality_framework_diagnosis", trace_path)

    assert result["status"] == "FAIL"
    assert "questioner_checkpoints" in result["process_quality"]
    assert any(item["code"] == "missing_questioner_checkpoint_stages" for item in result["misses"])
