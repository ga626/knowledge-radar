"""CI离线测试合批，独立执行受控进程测试；统一映射case并保留治理结果。"""
from __future__ import annotations

from collections import Counter
import subprocess
import xml.etree.ElementTree as ET

from .core import SCHEMA, dependencies, environment, file_hash, inventory, now, snapshot
from .runner import _pytest, comparison_identity, execute

HOST_NODE = 'tests/test_product_console_launcher.py::test_real_dev_supervisor_starts_restarts_and_stops_without_touching_stable'


def docs_only_change(root, base):
    if not base:
        return False
    proc = subprocess.run(['git','diff','--name-only',base,'HEAD'],cwd=root,capture_output=True,text=True,timeout=15)
    if proc.returncode:
        return False  # 浅克隆/未知身份保守执行，不能默默免检。
    paths = proc.stdout.splitlines()
    return bool(paths) and all(p.startswith('docs/') or (p.endswith('.md') and '/' not in p) for p in paths)


def _matches(test, node):
    parts = node.split("::")
    module = parts[0].removesuffix(".py").replace("/", ".")
    classname = test.get("classname", "")
    return (classname == module or classname.startswith(module + ".")) and (len(parts) == 1 or test.get("name", "").split("[")[0] == parts[-1])


def run_ci(root, suite, ledger, extra_nodes=()):
    snap, env, inv = snapshot(root), environment(), inventory(root, suite)
    run_id, folder = ledger.begin()
    pytest_cases = [c for c in suite["cases"] if c["auto"] and c["executor"] == "pytest"]
    nodes = list(dict.fromkeys([n for c in pytest_cases for n in c["nodes"]] + list(extra_nodes)))
    # 文件节点已覆盖其函数节点，避免同一个测试被pytest重复收集。
    files = {n for n in nodes if "::" not in n}
    nodes = [n for n in nodes if not any(n != other and (n.split("::")[0].startswith(other.rstrip("/") + "/") or ("::" in n and n.split("::")[0] == other)) for other in files)]
    started_at = now()
    tests_dir = folder / "pytest"
    tests_dir.mkdir()
    has_host = (root / HOST_NODE.split('::')[0]).is_file() and any(HOST_NODE == n or HOST_NODE.startswith(n.rstrip('/') + ('/' if not n.endswith('.py') else '::')) for n in nodes)
    offline_nodes = [n for n in nodes if n != HOST_NODE]
    outcome = _pytest(root, {"nodes": offline_nodes, "timeout_seconds": 600, "deselect": [HOST_NODE] if has_host else []}, tests_dir) if offline_nodes else {"verdict": "PASS", "returncode": 0}
    junit = tests_dir / "junit.xml"
    tests = list(ET.parse(junit).iter("testcase")) if junit.is_file() else []
    evidence = [{"path": p.relative_to(folder).as_posix(), "sha256": file_hash(p)} for p in tests_dir.iterdir() if p.is_file()]
    batches = {"offline": outcome}
    if has_host:
        host_dir = folder / 'host-process'
        host_dir.mkdir()
        host = _pytest(root, {"nodes": [HOST_NODE], "timeout_seconds": 120, "owned_host_process": True}, host_dir)
        batches['owned_host_process'] = host
        host_junit = host_dir / 'junit.xml'
        if host_junit.is_file():
            tests.extend(ET.parse(host_junit).iter('testcase'))
        evidence.extend({"path": p.relative_to(folder).as_posix(), "sha256": file_hash(p)} for p in host_dir.iterdir() if p.is_file())
        if host['verdict'] != 'PASS':
            outcome = {**outcome, 'verdict': host['verdict'], 'returncode': host.get('returncode')}
    rows = []
    for case in suite["cases"]:
        deps = dependencies(root, suite, case, snap)
        if case["executor"] == "pytest" and case["auto"]:
            selected = [t for t in tests if any(_matches(t, n) for n in case["nodes"])]
            covered = all(any(_matches(t, n) for t in tests) for n in case["nodes"])
            verdict = "ERROR" if not covered or outcome.get("returncode") not in {0, 1} else "ERROR" if any(t.find("error") is not None for t in selected) else "FAIL" if any(t.find("failure") is not None for t in selected) else "SKIPPED" if any(t.find("skipped") is not None for t in selected) else "PASS"
            result = {"verdict": verdict, "summary": f"single CI pytest execution: {len(selected)} tests", "evidence": evidence}
        elif not case["auto"]:
            result = {"verdict": "NOT_RUN", "summary": "CI does not run external/manual business evidence", "evidence": []}
        else:
            case_dir = folder / case["case_id"]
            case_dir.mkdir()
            try:
                outcome_case = execute(root, case, case_dir, inv)
            except Exception as exc:
                outcome_case = {"verdict": "ERROR", "summary": type(exc).__name__}
            result = {**outcome_case, "evidence": [{"path": p.relative_to(folder).as_posix(), "sha256": file_hash(p)} for p in case_dir.iterdir() if p.is_file()]}
        if deps["missing"] or deps["errors"]:
            result = {**result, "verdict": "ERROR", "summary": "dependency resolution incomplete"}
        rows.append({"case_id": case["case_id"], "version": case["version"], "layer": case["layer"],
                     "readiness": "unknown", "dependency_identity": deps, "comparison_identity": comparison_identity(root, suite, case, deps),
                     "evidence_kind": "ci_offline_contract" if case["auto"] else "uncollected", **result})
    run = ledger.save({"schema": SCHEMA, "run_id": run_id, "started_at": started_at, "finished_at": now(),
        "target": {"kind": "development_source", "identity": "working_tree_content", "evidence_boundary": "not_installed_or_native"},
        "snapshot": snap, "environment": env, "inventory": inv, "results": rows,
        "changed_during_execution": snapshot(root)["content_hash"] != snap["content_hash"],
        "counts": dict(Counter(r["verdict"] for r in rows)), "ci_pytest": {**outcome, 'batches': batches},
        "ci_evidence": evidence})
    passed = outcome["verdict"] == "PASS" and not inv["issues"] and not run["changed_during_execution"] and all(r["verdict"] == "PASS" for r in rows if r["layer"] not in {"native_mcp", "installed", "live"})
    return run, passed
