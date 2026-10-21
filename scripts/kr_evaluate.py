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


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--store", type=Path)
    parser.add_argument("--suite", default="config/evaluation")
    parser.add_argument("--json", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ["discover", "validate", "status", "plan", "history"]:
        sub.add_parser(name)
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
    ledger = Ledger(args.store or root / "local/evaluation")
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
            exit_code = int(any(r["transition"] == "regression" for r in result["cases"]))
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
