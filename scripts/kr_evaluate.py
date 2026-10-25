"""持续评测统一入口：发现、计划、运行、查询、对比与隔离演练。"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from scripts.kr_eval.core import Ledger, compare, inventory, load_suite, status, write_new
from scripts.kr_eval.runner import plan, run_suite
from runtime.evaluation_view import evaluation_store


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--store", type=Path)
    parser.add_argument("--suite", default="config/evaluation")
    parser.add_argument("--candidate-store", type=Path)
    parser.add_argument("--json", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ["discover", "validate", "status", "plan", "history", "publish-summary", "candidates"]:
        sub.add_parser(name)
    history_parser = sub.add_parser("import-history")
    history_parser.add_argument("source", type=Path)
    dataset_parser = sub.add_parser("import-speech-dataset")
    dataset_parser.add_argument("manifest", type=Path)
    collect_parser = sub.add_parser("collect-trace")
    collect_parser.add_argument("source", type=Path)
    receipt_parser = sub.add_parser("import-receipt")
    receipt_parser.add_argument("receipt", type=Path)
    receipt_parser.add_argument("--expected-target", type=Path, required=True)
    capture_parser = sub.add_parser("prepare-native-receipt")
    capture_parser.add_argument("transcript", type=Path)
    capture_parser.add_argument("--call-id", required=True)
    capture_parser.add_argument("--case", required=True)
    capture_parser.add_argument("--expected-target", type=Path, required=True)
    capture_parser.add_argument("--output", type=Path, required=True)
    ci_parser = sub.add_parser("ci")
    ci_parser.add_argument("--extra-node", action="append", default=[])
    ci_parser.add_argument("--changed-from")
    review_parser = sub.add_parser("review-candidate")
    review_parser.add_argument("candidate_id")
    review_parser.add_argument("--case")
    review_parser.add_argument("--reviewer", required=True)
    review_parser.add_argument("--reason", required=True)
    review_parser.add_argument("--input-sha256", default="")
    review_parser.add_argument("--oracle-version", type=int)
    run_parser = sub.add_parser("run")
    run_parser.add_argument("--case", action="append")
    run_parser.add_argument("--affected", action="store_true")
    run_parser.add_argument("--reuse", action="store_true")
    compare_parser = sub.add_parser("compare")
    compare_parser.add_argument("before", help="run ID or 'baseline'")
    compare_parser.add_argument("after")
    baseline_parser = sub.add_parser("accept-baseline")
    baseline_parser.add_argument("run_id")
    baseline_parser.add_argument("--reason", required=True)
    demo_parser = sub.add_parser("demo")
    demo_parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    root = args.root.resolve()
    ledger = Ledger(args.store or evaluation_store(root))
    try:
        suite = load_suite(root, args.suite)
        exit_code = 0
        if args.command in {"discover", "validate"}:
            result = inventory(root, suite)
            exit_code = int(bool(result["issues"]))
        elif args.command == "status":
            result = status(root, suite, ledger)
            exit_code = 0 if result["gate"] == "PASS" else 1
        elif args.command == "plan":
            result = plan(root, suite, ledger)
        elif args.command == "history":
            result = [{"run_id": row["run_id"], "at": row["finished_at"], "counts": row["counts"], "identity": row["snapshot"]["content_hash"]} for row in ledger.history()]
        elif args.command == "compare":
            result = compare(ledger.baseline() if args.before == "baseline" else ledger.read(args.before), ledger.read(args.after))
            exit_code = int(result["gate"] != "PASS")
        elif args.command == "import-history":
            from scripts.kr_eval.continuity import import_history
            result = import_history(ledger, args.source)
        elif args.command == "import-speech-dataset":
            from scripts.kr_eval.continuity import import_speech_dataset
            result = import_speech_dataset(ledger, args.manifest)
        elif args.command == "import-receipt":
            from scripts.kr_eval.continuity import import_receipt
            from scripts.kr_eval.core import read_json
            result = import_receipt(root, suite, ledger, args.receipt, read_json(args.expected_target))
            exit_code = int(any(r["verdict"] != "PASS" for r in result["results"]))
        elif args.command == "prepare-native-receipt":
            from scripts.kr_eval.continuity import prepare_native_receipt
            from scripts.kr_eval.core import read_json
            result = prepare_native_receipt(suite, args.transcript, args.call_id, args.case, read_json(args.expected_target), args.output)
        elif args.command == "publish-summary":
            from scripts.kr_eval.continuity import publish_summary
            result = publish_summary(root, suite, ledger)
        elif args.command in {"candidates", "review-candidate", "collect-trace"}:
            from runtime.evaluation_candidates import candidate_path, list_candidates, record_candidate, review_candidate
            candidate_store = args.candidate_store or candidate_path()
            if args.command == "candidates":
                result = list_candidates(candidate_store)
            elif args.command == "collect-trace":
                from collections import deque
                rows, invalid = 0, 0
                with args.source.open(encoding="utf-8-sig") as stream:
                    for line in deque(stream, maxlen=1000):
                        try:
                            event = json.loads(line)
                            record_candidate(event, candidate_store)
                            rows += 1
                        except (ValueError, TypeError, AttributeError):
                            invalid += 1
                result = {"status": "COLLECTED", "tail_limit": 1000, "read": rows, "invalid": invalid,
                          "candidates": len(list_candidates(candidate_store)), "gold_promoted": False}
            else:
                if args.case:
                    case = next(c for c in suite["cases"] if c["case_id"] == args.case)
                    if args.oracle_version != case["oracle_version"]:
                        raise ValueError("case oracle version mismatch")
                result = review_candidate(candidate_store, args.candidate_id, case=args.case, reviewer=args.reviewer,
                       reason=args.reason, input_sha256=args.input_sha256, oracle_version=args.oracle_version)
        elif args.command == "ci":
            from scripts.kr_eval.ci import docs_only_change, run_ci
            if docs_only_change(root, args.changed_from):
                result = {"status": "NO_EXECUTION", "reason": "documentation_only_change", "baseline_changed": False}
                write_new(ledger.path / "ci-scope.json", result)
            else:
                result, passed = run_ci(root, suite, ledger, args.extra_node)
                exit_code = int(not passed)
        elif args.command == "accept-baseline":
            result = ledger.accept(args.run_id, args.reason)
        elif args.command == "demo":
            from scripts.kr_eval.demo import demonstrate
            result = demonstrate(root, args.output or ledger.path / "demonstrations")
            exit_code = int(result["status"] != "PASS")
        else:
            selected = args.case
            if args.affected:
                selected = plan(root, suite, ledger)["selected"]
                if not selected:
                    result = {"status": "NO_EXECUTION", "reason": "no eligible affected cases", "plan": plan(root, suite, ledger)}
                    print(json.dumps(result, ensure_ascii=False, indent=2))
                    return 0
            result = run_suite(root, suite, ledger, selected, reuse=args.reuse)
            exit_code = int(bool(result["inventory"]["issues"]) or any(r["verdict"] != "PASS" for r in result["results"]))
        if args.command in {"run", "ci", "import-receipt", "import-history"}:
            from scripts.kr_eval.continuity import publish_summary
            publish_summary(root, suite, ledger)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        elif args.command == "status":
            print(f"{result['gate']} | {result['inventory']['registered_count']} tools | {result['denominator']} cases | {result['history_runs']} runs")
            print(json.dumps(result["counts"], ensure_ascii=False))
            for row in result["cases"]:
                print(f"{row['case_id']}: {row['verdict']} / {row['applicability']} / {row['readiness']} [{row['layer']}]")
        elif args.command == "run":
            print(f"run_id: {result['run_id']}\n" + json.dumps(result["counts"], ensure_ascii=False))
            for row in result["results"]:
                print(f"{row['case_id']}: {row['verdict']} " + (f"reused from {row['reused_from']}" if row.get("reused_from") else row.get("summary", "")))
        else:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        return exit_code
    except Exception as exc:
        print(json.dumps({"schema": "knowledgeradar-evaluation-error/v1", "status": "ERROR", "type": type(exc).__name__, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
