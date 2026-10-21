"""Validate and score KR natural tool-calling eval cases."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from capabilities import source_ecology_manifest

DEFAULT_CASES = ROOT / "tests" / "fixtures" / "natural_calling_eval_cases.json"

VALID_EVIDENCE_SURFACES = {
    "web",
    "platform_search",
    "academic",
    "media_detail",
    "video_detail",
    "image_detail",
    "local_code",
    "upstream_code",
    "runtime_probe",
    "prior_report",
    "source_ecology",
}

VALID_SOURCE_ECOLOGIES = set(source_ecology_manifest()["ecologies"].keys())

VALID_OUTPUT_ARTIFACTS = {
    "report",
    "evidence_register",
    "quality_check",
    "repair_requests",
    "roadmap",
    "known_issues",
    "decision_register",
    "agentic_search_governance",
    "claim_evidence_chains",
}

VALID_PROCESS_QUALITY = {
    "capability_handshake",
    "local_archaeology",
    "detail_depth",
    "skip_reasons",
    "questioner_checkpoints",
    "known_issues",
    "decision_register",
    "source_ecology_plan",
    "validation_ladder",
    "presentation",
    "agentic_search_governance",
    "validation_profile",
    "host_internal_web_wave",
    "generic_web_fallback",
}

LEGACY_FORBIDDEN_TOOLS = {"expand_keywords", "plan_research"}


def _load_cases(path: Path = DEFAULT_CASES) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _registered_tool_names() -> set[str]:
    import server

    return set(server.mcp._tool_manager._tools.keys())


def validate_cases(path: Path = DEFAULT_CASES) -> dict[str, Any]:
    payload = _load_cases(path)
    registered = _registered_tool_names()
    issues: list[dict[str, str]] = []
    cases = payload.get("cases") or []

    if payload.get("schema") != "knowledgeradar-natural-calling-eval/v1":
        issues.append({"case": "__file__", "code": "bad_schema"})
    if not cases:
        issues.append({"case": "__file__", "code": "missing_cases"})

    for case in cases:
        case_id = str(case.get("id") or "")
        if not case_id:
            issues.append({"case": "__unknown__", "code": "missing_id"})
        if case.get("tier") not in {"light", "full"}:
            issues.append({"case": case_id, "code": "bad_tier"})
        if not str(case.get("prompt") or "").strip():
            issues.append({"case": case_id, "code": "missing_prompt"})
        expected_groups = [
            "expected_tools_any",
            "expected_tools_at_least",
            "expected_tools_optional",
            "forbidden_tools",
        ]
        for key in expected_groups:
            for tool_name in case.get(key) or []:
                if key == "forbidden_tools" and tool_name in LEGACY_FORBIDDEN_TOOLS:
                    continue
                if tool_name not in registered:
                    issues.append({"case": case_id, "code": "unknown_tool", "detail": f"{key}:{tool_name}"})
        if not case.get("expected_tools_any"):
            issues.append({"case": case_id, "code": "missing_expected_tools_any"})
        quality_contract = case.get("quality_contract")
        if quality_contract is not None:
            if not isinstance(quality_contract, dict):
                issues.append({"case": case_id, "code": "bad_quality_contract"})
            else:
                for surface in quality_contract.get("expected_evidence_surfaces") or []:
                    if surface not in VALID_EVIDENCE_SURFACES:
                        issues.append({"case": case_id, "code": "unknown_evidence_surface", "detail": str(surface)})
                for ecology in quality_contract.get("expected_source_ecologies") or []:
                    if ecology not in VALID_SOURCE_ECOLOGIES:
                        issues.append({"case": case_id, "code": "unknown_source_ecology", "detail": str(ecology)})
                for artifact in quality_contract.get("expected_output_artifacts") or []:
                    if artifact not in VALID_OUTPUT_ARTIFACTS:
                        issues.append({"case": case_id, "code": "unknown_output_artifact", "detail": str(artifact)})
                for item in quality_contract.get("expected_process_quality") or []:
                    if item not in VALID_PROCESS_QUALITY:
                        issues.append({"case": case_id, "code": "unknown_process_quality", "detail": str(item)})
                if "expected_repair_loop" in quality_contract and not isinstance(quality_contract["expected_repair_loop"], bool):
                    issues.append({"case": case_id, "code": "bad_expected_repair_loop"})

    return {
        "schema": "knowledgeradar-natural-calling-eval-result/v1",
        "status": "PASS" if not issues else "FAIL",
        "case_count": len(cases),
        "light_case_count": sum(1 for case in cases if case.get("tier") == "light"),
        "full_case_count": sum(1 for case in cases if case.get("tier") == "full"),
        "quality_case_count": sum(1 for case in cases if isinstance(case.get("quality_contract"), dict)),
        "issue_count": len(issues),
        "issues": issues,
    }


def _load_trace(trace_path: Path) -> Any:
    return json.loads(trace_path.read_text(encoding="utf-8-sig"))


def _trace_tool_names(trace_payload: Any) -> list[str]:
    payload = trace_payload
    if isinstance(payload, list):
        return [str(item) for item in payload]
    if isinstance(payload, dict):
        if isinstance(payload.get("tools"), list):
            return [str(item) for item in payload["tools"]]
        if isinstance(payload.get("tool_calls"), list):
            names = []
            for item in payload["tool_calls"]:
                if isinstance(item, str):
                    names.append(item)
                elif isinstance(item, dict) and item.get("name"):
                    names.append(str(item["name"]))
            return names
    return []


def _trace_set(trace_payload: Any, key: str) -> set[str]:
    if not isinstance(trace_payload, dict):
        return set()
    values = trace_payload.get(key)
    if not isinstance(values, list):
        return set()
    return {str(item) for item in values if str(item)}


def _trace_source_ecologies(trace_payload: Any) -> set[str]:
    if not isinstance(trace_payload, dict):
        return set()
    result = _trace_set(trace_payload, "source_ecologies")
    evidence = trace_payload.get("evidence_items")
    if isinstance(evidence, list):
        for item in evidence:
            if isinstance(item, dict) and item.get("source_ecology"):
                result.add(str(item["source_ecology"]))
    coverage = trace_payload.get("coverage")
    if isinstance(coverage, dict):
        for key in ("covered_source_ecologies", "required_source_ecologies"):
            values = coverage.get(key)
            if isinstance(values, list):
                result.update(str(item) for item in values if str(item))
    return result


def _trace_has_repair_loop(trace_payload: Any) -> bool:
    if not isinstance(trace_payload, dict):
        return False
    if trace_payload.get("repair_loop") is True:
        return True
    repairs = trace_payload.get("repair_requests")
    return isinstance(repairs, list) and bool(repairs)


def _trace_questioner_stages(trace_payload: Any) -> set[str]:
    if not isinstance(trace_payload, dict):
        return set()
    checkpoints = trace_payload.get("questioner_checkpoints")
    if not isinstance(checkpoints, list):
        return set()
    return {str(item.get("stage")) for item in checkpoints if isinstance(item, dict) and item.get("stage")}


def _trace_has_skip_reasons(trace_payload: Any) -> bool:
    if not isinstance(trace_payload, dict):
        return False
    coverage = trace_payload.get("coverage")
    if not isinstance(coverage, dict):
        return bool(trace_payload.get("skip_reasons"))
    skipped_surfaces = coverage.get("skipped_surfaces")
    skipped_ecologies = coverage.get("skipped_source_ecologies")
    return bool(skipped_surfaces or skipped_ecologies or trace_payload.get("skip_reasons"))


def _trace_process_quality(trace_payload: Any, calls: list[str]) -> set[str]:
    if not isinstance(trace_payload, dict):
        return set()
    result = _trace_set(trace_payload, "process_quality")
    called = set(calls)
    if {"health_check", "get_capabilities"} <= called:
        result.add("capability_handshake")
    surfaces = _trace_set(trace_payload, "evidence_surfaces")
    if "local_code" in surfaces or "local_archaeology" in result:
        result.add("local_archaeology")
    if bool({"extract_web_page", "extract_dynamic_page", "get_content_detail", "get_task_status"} & called) or "media_detail" in surfaces:
        result.add("detail_depth")
    if _trace_has_skip_reasons(trace_payload):
        result.add("skip_reasons")
    if _trace_questioner_stages(trace_payload):
        result.add("questioner_checkpoints")
    if trace_payload.get("known_issues"):
        result.add("known_issues")
    if trace_payload.get("decision_register"):
        result.add("decision_register")
    process_quality = trace_payload.get("process_quality")
    if isinstance(process_quality, dict):
        if process_quality.get("source_ecology_plan"):
            result.add("source_ecology_plan")
        if process_quality.get("validation_ladder"):
            result.add("validation_ladder")
            validation = process_quality.get("validation_ladder")
            if isinstance(validation, dict) and validation.get("selected_profile"):
                result.add("validation_profile")
        if process_quality.get("presentation"):
            result.add("presentation")
        if process_quality.get("host_internal_web_wave"):
            result.add("host_internal_web_wave")
        if process_quality.get("generic_web_fallback"):
            result.add("host_internal_web_wave")
            result.add("generic_web_fallback")
    if trace_payload.get("agentic_search"):
        result.add("agentic_search_governance")
    if trace_payload.get("claim_evidence_chains"):
        result.add("claim_evidence_chains")
    return result


def _trace_process_quality_dict(trace_payload: Any) -> dict[str, Any]:
    if not isinstance(trace_payload, dict):
        return {}
    process_quality = trace_payload.get("process_quality")
    return process_quality if isinstance(process_quality, dict) else {}


def _trace_host_internal_web_wave(trace_payload: Any) -> dict[str, Any]:
    process_quality = _trace_process_quality_dict(trace_payload)
    for key in ("host_internal_web_wave", "generic_web_fallback"):
        value = process_quality.get(key)
        if isinstance(value, dict):
            return value
    return {}


def _trace_host_internal_web_issues(trace_payload: Any, calls: list[str]) -> list[str]:
    if "builtin_web_search" not in set(calls):
        return []
    issues: list[str] = []
    try:
        first_builtin = calls.index("builtin_web_search")
    except ValueError:
        first_builtin = -1
    kr_preceders = {"kr_research", "kr_web_search", "health_check", "get_capabilities"}
    if first_builtin >= 0 and not (set(calls[:first_builtin]) & kr_preceders):
        issues.append("builtin_web_before_kr_admission")
    wave = _trace_host_internal_web_wave(trace_payload)
    if not wave:
        issues.append("missing_host_internal_web_wave_record")
        return issues
    if str(wave.get("wave_id") or "") != "host_internal_web_wave":
        issues.append("missing_host_internal_web_wave_id")
    if str(wave.get("strategy_tree") or "") != "web_search.provider_wave":
        issues.append("missing_host_internal_web_strategy_tree")
    relationship = str(wave.get("relationship_to_kr") or "")
    if "finger" not in relationship and "KR" not in relationship and "kr" not in relationship:
        issues.append("missing_host_internal_web_relationship_to_kr")
    if not str(wave.get("reason") or "").strip():
        issues.append("missing_host_internal_web_reason")
    return issues


def _trace_agentic_search_issues(trace_payload: Any) -> list[str]:
    if not isinstance(trace_payload, dict):
        return ["trace_payload_not_object"]
    block = trace_payload.get("agentic_search")
    if not isinstance(block, dict):
        return ["missing_agentic_search"]
    issues: list[str] = []
    if block.get("schema") != "knowledgeradar-agentic-search-governance/v1":
        issues.append("bad_schema")
    if block.get("agent_policy") != "model_decides_rounds_tools_and_stop":
        issues.append("bad_agent_policy")
    budget = block.get("budget_semantics")
    if not isinstance(budget, dict):
        issues.append("missing_budget_semantics")
    elif budget.get("position") != "runtime_sla_not_tool_route":
        issues.append("budget_semantics_not_runtime_sla")
    ledger = block.get("evidence_coverage_ledger")
    if not isinstance(ledger, list) or not ledger:
        issues.append("missing_evidence_coverage_ledger")
    else:
        required_ledger = {"claim_id", "evidence_role", "covered_by", "gap", "next_action"}
        for index, item in enumerate(ledger):
            if not isinstance(item, dict):
                issues.append(f"bad_ledger_item:{index}")
                continue
            missing = sorted(required_ledger - set(item))
            if missing:
                issues.append(f"bad_ledger_item:{index}:{','.join(missing)}")
            if not isinstance(item.get("covered_by"), list):
                issues.append(f"bad_ledger_item:{index}:covered_by_not_list")
    marginal_yield = block.get("marginal_yield")
    if not isinstance(marginal_yield, dict):
        issues.append("missing_marginal_yield")
    else:
        for key in ("continue_condition", "stop_condition", "current_assessment"):
            if not str(marginal_yield.get(key) or "").strip():
                issues.append(f"bad_marginal_yield:{key}")
    stopping = block.get("stopping_criteria")
    if not isinstance(stopping, dict):
        issues.append("missing_stopping_criteria")
    else:
        if not isinstance(stopping.get("criteria"), list) or not stopping.get("criteria"):
            issues.append("bad_stopping_criteria:criteria")
        if stopping.get("decision") not in {"continue", "stop"}:
            issues.append("bad_stopping_criteria:decision")
    return issues


def _trace_legacy_tool_names(trace_path: Path) -> list[str]:
    payload = json.loads(trace_path.read_text(encoding="utf-8-sig"))
    return _trace_tool_names(payload)


def score_trace(case_id: str, trace_path: Path, cases_path: Path = DEFAULT_CASES) -> dict[str, Any]:
    payload = _load_cases(cases_path)
    case = next((item for item in payload.get("cases") or [] if item.get("id") == case_id), None)
    if not case:
        return {"schema": "knowledgeradar-natural-calling-score/v1", "status": "FAIL", "error": f"unknown case: {case_id}"}
    trace_payload = _load_trace(trace_path)
    calls = _trace_tool_names(trace_payload)
    called = set(calls)
    expected_any = set(case.get("expected_tools_any") or [])
    expected_at_least = set(case.get("expected_tools_at_least") or [])
    forbidden = set(case.get("forbidden_tools") or [])
    misses = []
    if expected_any and not (called & expected_any):
        misses.append({"code": "missing_any", "tools": sorted(expected_any)})
    missing_required = sorted(expected_at_least - called)
    if missing_required:
        misses.append({"code": "missing_required", "tools": missing_required})
    forbidden_called = sorted(forbidden & called)
    if forbidden_called:
        misses.append({"code": "forbidden_called", "tools": forbidden_called})
    quality_contract = case.get("quality_contract") if isinstance(case.get("quality_contract"), dict) else {}
    expected_surfaces = set(quality_contract.get("expected_evidence_surfaces") or [])
    if expected_surfaces:
        present_surfaces = _trace_set(trace_payload, "evidence_surfaces")
        missing_surfaces = sorted(expected_surfaces - present_surfaces)
        if missing_surfaces:
            misses.append({"code": "missing_evidence_surfaces", "surfaces": missing_surfaces})
    expected_source_ecologies = set(quality_contract.get("expected_source_ecologies") or [])
    if expected_source_ecologies:
        present_source_ecologies = _trace_source_ecologies(trace_payload)
        missing_source_ecologies = sorted(expected_source_ecologies - present_source_ecologies)
        if missing_source_ecologies:
            misses.append({"code": "missing_source_ecologies", "source_ecologies": missing_source_ecologies})
    expected_artifacts = set(quality_contract.get("expected_output_artifacts") or [])
    if expected_artifacts:
        present_artifacts = _trace_set(trace_payload, "output_artifacts")
        missing_artifacts = sorted(expected_artifacts - present_artifacts)
        if missing_artifacts:
            misses.append({"code": "missing_output_artifacts", "artifacts": missing_artifacts})
    if quality_contract.get("expected_repair_loop") is True and not _trace_has_repair_loop(trace_payload):
        misses.append({"code": "missing_repair_loop"})
    expected_process_quality = set(quality_contract.get("expected_process_quality") or [])
    present_process_quality = _trace_process_quality(trace_payload, calls)
    if expected_process_quality:
        missing_process_quality = sorted(expected_process_quality - present_process_quality)
        if missing_process_quality:
            misses.append({"code": "missing_process_quality", "items": missing_process_quality})
    if "questioner_checkpoints" in expected_process_quality:
        required_stages = {"after_local_archaeology", "after_first_external_round", "before_final_report"}
        missing_stages = sorted(required_stages - _trace_questioner_stages(trace_payload))
        if missing_stages:
            misses.append({"code": "missing_questioner_checkpoint_stages", "stages": missing_stages})
    if "agentic_search_governance" in expected_process_quality or "agentic_search_governance" in expected_artifacts:
        agentic_issues = _trace_agentic_search_issues(trace_payload)
        if agentic_issues:
            misses.append({"code": "invalid_agentic_search_governance", "issues": agentic_issues})
    host_web_issues = _trace_host_internal_web_issues(trace_payload, calls)
    if host_web_issues:
        misses.append({"code": "invalid_host_internal_web_wave", "issues": host_web_issues})
    return {
        "schema": "knowledgeradar-natural-calling-score/v1",
        "status": "PASS" if not misses else "FAIL",
        "case_id": case_id,
        "called_tools": calls,
        "evidence_surfaces": sorted(_trace_set(trace_payload, "evidence_surfaces")),
        "source_ecologies": sorted(_trace_source_ecologies(trace_payload)),
        "output_artifacts": sorted(_trace_set(trace_payload, "output_artifacts")),
        "process_quality": sorted(present_process_quality),
        "misses": misses,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--light", action="store_true", help="validate eval case contract")
    parser.add_argument("--score-trace", type=Path, help="score a JSON tool-call trace")
    parser.add_argument("--case-id", help="case id for --score-trace")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if args.score_trace:
        if not args.case_id:
            parser.error("--case-id is required with --score-trace")
        result = score_trace(args.case_id, args.score_trace, args.cases)
    else:
        result = validate_cases(args.cases)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"{result['status']}: {result.get('issue_count', len(result.get('misses', [])))} issue(s)")
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
