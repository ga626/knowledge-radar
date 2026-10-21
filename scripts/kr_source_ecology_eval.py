"""Evaluate source-ecology coverage from natural calling traces."""

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

from scripts.kr_natural_calling_eval import DEFAULT_CASES, score_trace, validate_cases


def _load_cases(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    cases = payload.get("cases") if isinstance(payload, dict) else []
    return [case for case in cases if isinstance(case, dict)]


def source_ecology_cases(path: Path = DEFAULT_CASES) -> list[dict[str, Any]]:
    """Return natural-calling cases that declare source-ecology expectations."""
    return [
        case
        for case in _load_cases(path)
        if isinstance(case.get("quality_contract"), dict)
        and case["quality_contract"].get("expected_source_ecologies")
    ]


def evaluate_trace_dir(trace_dir: Path, *, cases_path: Path = DEFAULT_CASES) -> dict[str, Any]:
    """Score one trace file per source-ecology case.

    Trace files are expected at ``<trace_dir>/<case_id>.json``. The trace shape is
    intentionally the same as kr_natural_calling_eval.score_trace.
    """
    case_validation = validate_cases(cases_path)
    results: list[dict[str, Any]] = []
    for case in source_ecology_cases(cases_path):
        case_id = str(case.get("id") or "")
        trace_path = trace_dir / f"{case_id}.json"
        if not trace_path.is_file():
            quality_contract = case.get("quality_contract") or {}
            results.append(
                {
                    "case_id": case_id,
                    "status": "MISSING_TRACE",
                    "trace_path": str(trace_path),
                    "expected_source_ecologies": list(
                        quality_contract.get("expected_source_ecologies") or []
                    ),
                    "covered_source_ecologies": [],
                    "misses": [{"code": "missing_trace"}],
                }
            )
            continue
        score = score_trace(case_id, trace_path, cases_path)
        quality_contract = case.get("quality_contract") or {}
        results.append(
            {
                "case_id": case_id,
                "status": score.get("status"),
                "trace_path": str(trace_path),
                "expected_source_ecologies": list(
                    quality_contract.get("expected_source_ecologies") or []
                ),
                "covered_source_ecologies": list(score.get("source_ecologies") or []),
                "called_tools": list(score.get("called_tools") or []),
                "misses": list(score.get("misses") or []),
            }
        )
    failing = [item for item in results if item.get("status") != "PASS"]
    return {
        "schema": "knowledgeradar-source-ecology-eval/v1",
        "status": "PASS" if not failing and case_validation.get("status") == "PASS" else "FAIL",
        "case_contract_status": case_validation.get("status"),
        "case_count": len(results),
        "pass_count": sum(1 for item in results if item.get("status") == "PASS"),
        "missing_trace_count": sum(1 for item in results if item.get("status") == "MISSING_TRACE"),
        "fail_count": len(failing),
        "results": results,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--trace-dir", type=Path, required=True)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    result = evaluate_trace_dir(args.trace_dir, cases_path=args.cases)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"{result['status']}: {result['pass_count']}/{result['case_count']} source-ecology case(s) passed")
        for item in result["results"]:
            if item.get("status") != "PASS":
                print(f"- {item['case_id']}: {item['status']} {item.get('misses')}")
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
