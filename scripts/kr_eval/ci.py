"""CI离线测试合批，独立执行受控进程测试；统一映射case并保留治理结果。"""
from __future__ import annotations

from collections import Counter
import fnmatch
import subprocess
import xml.etree.ElementTree as ET

from .core import SCHEMA, dependencies, environment, file_hash, inventory, now, snapshot
from .runner import _pytest, comparison_identity, execute, isolated_env

HOST_NODE = 'tests/test_product_console_launcher.py::test_real_dev_supervisor_starts_restarts_and_stops_without_touching_stable'
HOST_NODES = (HOST_NODE, 'tests/test_product_runtime.py',
              'tests/test_codex_stdio_contract.py::test_direct_stdio_probe_exposes_research_surface')
SCALE_NODES = ('tests/test_research_archive.py::test_ten_thousand_versions_frozen_membership',)


def _covers(parent, child):
    return (parent == child or child.startswith(parent.rstrip('/') + '/')
            or child.startswith(parent + '::'))


def docs_only_change(root, base):
    if not base:
        return False
    proc = subprocess.run(['git','diff','--name-only',base,'HEAD'],cwd=root,capture_output=True,text=True,timeout=15)
    if proc.returncode:
        return False  # 浅克隆/未知身份保守执行，不能默默免检。
    paths = proc.stdout.splitlines()
    return bool(paths) and all(p.startswith('docs/') or (p.endswith('.md') and '/' not in p) for p in paths)


def changed_paths(root, base):
    if not base:
        return None
    result = subprocess.run(['git', '-c', 'core.quotePath=false', 'diff', '--name-only', base, 'HEAD'],
                            cwd=root, capture_output=True, text=True, timeout=15)
    return result.stdout.splitlines() if result.returncode == 0 else None


def select_scope(root, suite, paths, *, full=False):
    """少量粗组；未映射/空变更/未知基线一律完整检查，不依赖建议器截断。"""
    policy = suite.get('ci', {})
    registered = [n for c in suite['cases'] if c['auto'] and c['executor']=='pytest' for n in c['nodes']]
    changed_tests = [p for p in paths or [] if p.startswith('tests/') and p.endswith('.py')
                     and (root/p).is_file()]
    all_nodes = list(dict.fromkeys([*registered, *policy.get('full_nodes', []), *changed_tests]))
    all_node_tests = sorted(p.relative_to(root).as_posix() for p in (root/'tests').rglob('test_*.mjs'))
    full_scope = {'mode':'full', 'reason':'explicit_or_unmapped', 'changed_paths':paths,
                  'nodes':all_nodes, 'node_tests':all_node_tests}
    if full or not paths or not policy:
        return full_scope
    groups = set()
    for path in paths:
        if path.startswith('docs/') or (path.endswith('.md') and '/' not in path):
            continue
        matches = [name for name, group in policy['groups'].items()
                   if any(fnmatch.fnmatchcase(path, p) for p in group['sources'])]
        if not matches:
            return full_scope
        groups.update(matches)
    if not groups:
        return full_scope
    # 扫描嵌套用例与Node用例，但仍复用原测试，不生成第二套题库。
    candidates = sorted(set(all_nodes + [p.relative_to(root).as_posix()
        for p in (root/'tests').rglob('test_*.py')]))
    patterns = [p for name in groups for p in policy['groups'][name]['tests']]
    selected = [n for n in candidates if any(fnmatch.fnmatchcase(n.split('::')[0], p) for p in patterns)]
    node_tests = [n for n in all_node_tests if any(fnmatch.fnmatchcase(n,p) for p in patterns)]
    if not selected and not node_tests:
        return full_scope
    return {'mode':'affected', 'reason':'coarse_groups', 'groups':sorted(groups), 'changed_paths':paths,
            'nodes':list(dict.fromkeys([*policy['base_nodes'], *selected])), 'node_tests':node_tests}


def _node_tests(root, nodes, folder):
    if not nodes:
        return {'verdict':'NOT_RUN', 'summary':'no selected Node tests'}
    with (folder/'node-stdout.txt').open('x',encoding='utf-8') as out, (folder/'node-stderr.txt').open('x',encoding='utf-8') as err:
        try:
            process = subprocess.run(['node','--test',*nodes],cwd=root,env=isolated_env(root,folder),
                                     stdout=out,stderr=err,timeout=180)
            return {'verdict':'PASS' if process.returncode==0 else 'FAIL', 'returncode':process.returncode,
                    'nodes':nodes}
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {'verdict':'ERROR','error_type':type(exc).__name__,'nodes':nodes}


def run_documentation_checks(root):
    """纯文档不跑业务，但不能免掉生成物与当前引用检查。"""
    import sys
    result = subprocess.run([sys.executable, str(root / 'scripts/check_current_docs.py')],
                            cwd=root, capture_output=True, text=True, encoding='utf-8', timeout=90)
    return {'status': 'PASS' if result.returncode == 0 else 'FAIL',
            'reason': 'documentation_only_change', 'business_tests': 'not_run',
            'stdout': result.stdout, 'stderr': result.stderr, 'exit_code': result.returncode}


def _matches(test, node):
    parts = node.split("::")
    module = parts[0].removesuffix(".py").replace("/", ".")
    classname = test.get("classname", "")
    return (classname == module or classname.startswith(module + ".")) and (len(parts) == 1 or test.get("name", "").split("[")[0] == parts[-1])


def run_ci(root, suite, ledger, extra_nodes=(), *, scope=None):
    snap, env, inv = snapshot(root), environment(), inventory(root, suite)
    run_id, folder = ledger.begin()
    pytest_cases = [c for c in suite["cases"] if c["auto"] and c["executor"] == "pytest"]
    nodes = list(dict.fromkeys((scope['nodes'] if scope else [n for c in pytest_cases for n in c['nodes']]) + list(extra_nodes)))
    # 文件节点已覆盖其函数节点，避免同一个测试被pytest重复收集。
    files = {n for n in nodes if "::" not in n}
    nodes = [n for n in nodes if not any(n != other and (n.split("::")[0].startswith(other.rstrip("/") + "/") or ("::" in n and n.split("::")[0] == other)) for other in files)]
    started_at = now()
    tests_dir = folder / "pytest"
    tests_dir.mkdir()
    preflight = {}
    for case in suite['cases']:
        if case['auto'] and case['executor']=='surface':
            case_dir = folder/case['case_id']
            case_dir.mkdir()
            preflight[case['case_id']] = execute(root, case, case_dir, inv)
    contracts_ok = not inv['issues'] and all(r['verdict']=='PASS' for r in preflight.values())
    host_nodes = list(dict.fromkeys(
        host if _covers(node, host) else node
        for host in HOST_NODES if (root / host.split('::')[0]).is_file()
        for node in nodes if _covers(node, host) or _covers(host, node)))
    # Only route the expensive scale case when the selected scope explicitly
    # includes its module/case.  A synthetic `tests/` selection in contract
    # tests must remain one offline batch plus the owned-host batch.
    scale_nodes = [scale for scale in SCALE_NODES
                   if scale in nodes or any(n.split('::')[0] == 'tests/test_research_archive.py' for n in nodes)]
    offline_nodes = [n for n in nodes if not any(_covers(host, n) for host in host_nodes)]
    # 当前统一批次超过千项；保留逐例进度/慢例与90秒堆栈，整批仍有明确硬期限。
    outcome = _pytest(root, {"nodes": offline_nodes, "timeout_seconds": 1200,
                            "deselect": [*host_nodes, *scale_nodes]}, tests_dir) if offline_nodes and contracts_ok else {"verdict": "PASS" if contracts_ok else "BLOCKED", "returncode": 0 if contracts_ok else None}
    junit = tests_dir / "junit.xml"
    tests = list(ET.parse(junit).iter("testcase")) if junit.is_file() else []
    evidence = [{"path": p.relative_to(folder).as_posix(), "sha256": file_hash(p)} for p in tests_dir.iterdir() if p.is_file()]
    batches = {"offline": outcome}
    if scale_nodes and contracts_ok:
        scale_dir = folder / 'scale-process'
        scale_dir.mkdir()
        scale = _pytest(root, {"nodes": scale_nodes, "timeout_seconds": 360,
                               "faulthandler_timeout": 300}, scale_dir)
        batches['scale_process'] = scale
        scale_junit = scale_dir / 'junit.xml'
        if scale_junit.is_file():
            tests.extend(ET.parse(scale_junit).iter('testcase'))
        evidence.extend({"path": p.relative_to(folder).as_posix(), "sha256": file_hash(p)}
                        for p in scale_dir.iterdir() if p.is_file())
        if scale['verdict'] != 'PASS':
            outcome = {**outcome, 'verdict': scale['verdict'], 'returncode': scale.get('returncode')}
    if host_nodes and contracts_ok:
        host_dir = folder / 'host-process'
        host_dir.mkdir()
        # 合批后仍保留所属case的执行预算；避免新增生命周期回归被旧硬期限截断。
        host_timeout = max([240, *[case.get('timeout_seconds', 120) for case in pytest_cases
                                  if case.get('owned_host_process')]])
        host = _pytest(root, {"nodes": host_nodes, "timeout_seconds": host_timeout, "owned_host_process": True}, host_dir)
        batches['owned_host_process'] = host
        host_junit = host_dir / 'junit.xml'
        if host_junit.is_file():
            tests.extend(ET.parse(host_junit).iter('testcase'))
        evidence.extend({"path": p.relative_to(folder).as_posix(), "sha256": file_hash(p)} for p in host_dir.iterdir() if p.is_file())
        if host['verdict'] != 'PASS':
            outcome = {**outcome, 'verdict': host['verdict'], 'returncode': host.get('returncode')}
    if scope and scope.get('node_tests') and contracts_ok:
        batches['node'] = _node_tests(root, scope['node_tests'], tests_dir)
        if batches['node']['verdict'] != 'PASS':
            outcome = {**outcome, 'verdict':batches['node']['verdict'], 'returncode':1}
        evidence = [{"path":p.relative_to(folder).as_posix(),"sha256":file_hash(p)} for p in tests_dir.iterdir() if p.is_file()] + [e for e in evidence if not e['path'].startswith('pytest/')]
    rows = []
    for case in suite["cases"]:
        deps = dependencies(root, suite, case, snap)
        if case['case_id'] in preflight:
            result = {**preflight[case['case_id']], 'evidence':[]}
        elif case["executor"] == "pytest" and case["auto"] and scope and not all(any(_covers(n,c) for n in nodes) for c in case['nodes']):
            result = {'verdict':'NOT_RUN','summary':'case not fully selected; no new whole-case verdict','evidence':[]}
        elif case["executor"] == "pytest" and case["auto"]:
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
        "counts": dict(Counter(r["verdict"] for r in rows)), "ci_scope": scope or {'mode':'full'}, "ci_pytest": {**outcome, 'batches': batches},
        "ci_evidence": evidence})
    passed = outcome["verdict"] == "PASS" and not inv["issues"] and not run["changed_during_execution"] and all(r["verdict"] == "PASS" for r in rows if r["layer"] not in {"native_mcp", "installed", "live"} and not (scope and r['verdict']=='NOT_RUN'))
    return run, passed
