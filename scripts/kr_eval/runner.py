"""复用已有执行器；失败与基础设施异常分别保留。"""
from __future__ import annotations

from collections import Counter
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import os
from pathlib import Path
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

from .core import (SCHEMA, Ledger, applicability, dependencies, digest, environment, file_hash,
                   inventory, now, read_json, safe_path, snapshot, write_new)


def isolated_env(root, folder):
    env = {k: v for k, v in os.environ.items() if k.upper() in {"SYSTEMROOT", "WINDIR", "PATH", "PATHEXT", "TEMP", "TMP", "COMSPEC"}}
    data = folder / "sandbox"
    data.mkdir(exist_ok=True)
    env.update({"PYTHONIOENCODING": "utf-8", "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
                "PYTHONPATH": os.pathsep.join([str(root), str(root / "src")]), "PYTHONDONTWRITEBYTECODE": "1",
                "HOME": str(data), "USERPROFILE": str(data), "LOCALAPPDATA": str(data), "APPDATA": str(data),
                "KR_PROJECT_ROOT": str(root), "KR_STATE_DIR": str(data / "state"), "KR_LOG_DIR": str(data / "logs"),
                "KR_DATA_ROOT": str(data), "KR_USER_DATA_DIR": str(data), "KR_BROWSER_DATA_DIR": str(data / "browser"),
                "KR_PROFILE_REGISTRY_PATH": str(data / "profiles.json"), "KR_PROFILE_STATE_PATH": str(data / "profiles-state.json"),
                "CODEX_HOME": str(data / "codex"), "KR_EVALUATION_OFFLINE": "1"})
    return env


def _pytest(root, case, folder):
    nodes = case["nodes"]
    if not nodes or any(n.startswith("-") or not safe_path(root, n.split("::")[0]).is_file() for n in nodes):
        raise ValueError("invalid or missing pytest node")
    junit = folder / "junit.xml"
    command = [sys.executable, str(root / "scripts/kr_eval/pytest_entry.py"), "-q", "-p", "scripts.kr_eval.offline_guard", "-p", "no:cacheprovider",
               f"--junitxml={junit}", *nodes]
    started = time.monotonic()
    with (folder / "stdout.txt").open("x", encoding="utf-8") as out, (folder / "stderr.txt").open("x", encoding="utf-8") as err:
        try:
            proc = subprocess.run(command, cwd=root, env=isolated_env(root, folder), stdout=out, stderr=err,
                                  text=True, encoding="utf-8", timeout=case.get("timeout_seconds", 120))
        except subprocess.TimeoutExpired:
            return {"verdict": "ERROR", "summary": "executor_timeout", "returncode": None, "command": command,
                    "elapsed_s": time.monotonic() - started}
    details = {"returncode": proc.returncode, "command": command}
    if not junit.is_file():
        return {**details, "verdict": "ERROR", "summary": "missing_junit"}
    tree = ET.parse(junit)
    tests = list(tree.iter("testcase"))
    counts = {"tests": len(tests), "failures": len(list(tree.iter("failure"))), "errors": len(list(tree.iter("error"))),
              "skipped": len(list(tree.iter("skipped")))}
    if proc.returncode not in {0, 1} or counts["errors"] or not tests:
        verdict = "ERROR"
    elif counts["failures"]:
        verdict = "FAIL"
    elif counts["skipped"]:
        verdict = "SKIPPED"
    elif proc.returncode:
        verdict = "ERROR"
    else:
        verdict = "PASS"
    return {**details, "verdict": verdict, "summary": str(counts), "test_counts": counts}


def execute(root, case, folder, inv):
    executor = case["executor"]
    if executor == "manual":
        return {"verdict": "NOT_RUN", "readiness": "unknown", "summary": case["manual_reason"]}
    if executor == "surface":
        actual = inv["registered"].get(case["entry"])
        ok = actual and actual["signature"] == case["signature"] and actual["documented"]
        return {"verdict": "PASS" if ok else "FAIL", "summary": "AST registration/signature/docstring only; not business connectivity"}
    if executor == "pytest":
        return _pytest(root, case, folder)
    output = StringIO()
    with redirect_stdout(output), redirect_stderr(output):
        if executor == "natural_trace":
            from scripts.kr_natural_calling_eval import score_trace
            score = score_trace(case["trace_case"], safe_path(root, case["input"]), safe_path(root, case["trace_cases"]))
        elif executor == "source_ecology":
            from scripts.kr_source_ecology_eval import evaluate_trace_dir
            score = evaluate_trace_dir(safe_path(root, case["input"]), cases_path=safe_path(root, case["trace_cases"]))
            if not score.get('case_count'):
                raise ValueError('source ecology has no applicable cases')
        elif executor == "benchmark_json":
            report = read_json(safe_path(root, case["input"]))
            if not report.get("real_run") or not report.get("source_identity") or not report.get("samples"):
                raise ValueError("benchmark missing execution identity or real samples")
            passed = all(row.get("success") is True for row in report["samples"])
            score = {"status": "PASS" if passed else "FAIL", "sample_count": len(report["samples"]), "claims": "recorded benchmark only"}
        else:
            raise ValueError("unsupported executor")
    write_new(folder / "score.json", score)
    if output.getvalue():
        (folder / "adapter-output.txt").write_text(output.getvalue(), encoding="utf-8")
    return {"verdict": score["status"] if score.get("status") in {"PASS", "FAIL"} else "ERROR", "summary": str(score.get("misses", score.get("status")))}


def comparison_identity(root, suite, case, deps):
    # 产品代码变化仍可比较；规则、fixtures、判定器和测试实现变化不可默认为同一标准。
    oracle_files = {p: h for p, h in deps["files"].items()
                    if p.startswith(("tests/", "scripts/kr_eval/")) or p in case.get("oracle_dependencies", [])}
    return digest({"contract": deps["contract_hash"], "oracle_files": oracle_files})


def run_suite(root, suite, ledger, selected=None, *, reuse=False):
    inv = inventory(root, suite)
    snap, env = snapshot(root), environment()
    old = ledger.history()
    if selected:
        unknown = set(selected) - {c["case_id"] for c in suite["cases"]}
        if unknown:
            raise ValueError(f"unknown case IDs: {sorted(unknown)}")
    run_id, folder = ledger.begin()
    run = {"schema": SCHEMA, "run_id": run_id, "started_at": now(),
           "target": {"kind": "development_source", "identity": "working_tree_content", "evidence_boundary": "not_installed_or_native"},
           "snapshot": snap, "environment": env, "inventory": inv, "results": []}
    for case in suite["cases"]:
        if selected and case["case_id"] not in selected:
            continue
        deps = dependencies(root, suite, case, snap)
        prior = next(((r, previous) for previous in reversed(old) for r in previous["results"]
                      if r["case_id"] == case["case_id"] and not r.get("reused_from")), None)
        if reuse and prior:
            result, previous = prior
            app, reasons = applicability(root, suite, case, result, previous, snap, env)
            if app == "fresh" and result["verdict"] == "PASS":
                run["results"].append({**result, "reused_from": previous["run_id"], "reuse_reason": "content/contract/environment unchanged",
                                       "evidence": [], "original_checked_at": previous["finished_at"]})
                continue
        case_dir = folder / case["case_id"]
        case_dir.mkdir()
        started = time.monotonic()
        try:
            if deps["errors"] or deps["missing"]:
                outcome = {"verdict": "ERROR", "summary": str({"missing": deps["missing"], "errors": deps["errors"]})}
            elif not case["auto"]:
                outcome = {"verdict": "NOT_RUN", "summary": case.get("manual_reason", "explicit collection required")}
            else:
                outcome = execute(root, case, case_dir, inv)
        except Exception as exc:
            outcome = {"verdict": "ERROR", "summary": f"{type(exc).__name__}: {exc}"}
        result = {"case_id": case["case_id"], "version": case["version"], "layer": case["layer"],
                  "evidence_kind": {"contract": "source_ast", "fixture": "isolated_fixture", "integration": "isolated_integration"}.get(case["layer"], "uncollected"),
                  "readiness": "unknown", "dependency_identity": deps, "comparison_identity": comparison_identity(root, suite, case, deps),
                  "elapsed_s": round(time.monotonic() - started, 4), **outcome}
        write_new(case_dir / "outcome.json", outcome)
        result["evidence"] = [{"path": p.relative_to(folder).as_posix(), "sha256": file_hash(p)}
                              for p in sorted(case_dir.iterdir()) if p.is_file()]
        run["results"].append(result)
    run["finished_at"] = now()
    run["changed_during_execution"] = (snapshot(root)["content_hash"] != snap["content_hash"] or environment()["fingerprint"] != env["fingerprint"])
    run["counts"] = dict(Counter(r["verdict"] for r in run["results"]))
    return ledger.save(run)


def plan(root, suite, ledger):
    from .core import status
    summary = status(root, suite, ledger)
    selected = [c["case_id"] for c in summary["cases"] if c["applicability"] != "fresh" or c["verdict"] != "PASS"]
    allowed = {c["case_id"] for c in suite["cases"] if c["auto"]}
    changes = []
    history = ledger.history()
    if history:
        from .core import changed_files
        changes = changed_files(history[-1]["snapshot"]["files"], snapshot(root)["files"])
    from scripts.kr_test_selector import select_tests
    selector = select_tests(changes, root=root) if changes else {"selected_tests": [], "reason": "no content delta"}
    return {"schema": SCHEMA, "inventory_issues": summary["inventory"]["issues"],
            "selected": [c for c in selected if c in allowed], "manual": [c for c in selected if c not in allowed],
            "reuse": [{"case_id": c["case_id"], "from_run": c.get("run_id")} for c in summary["cases"] if c["case_id"] not in selected],
            "selector_advice": selector, "status": summary["gate"]}
