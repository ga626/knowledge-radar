"""CI离线测试合批，独立执行受控进程测试；统一映射case并保留治理结果。"""
from __future__ import annotations

from collections import Counter
import fnmatch
import json
import time
import subprocess
import xml.etree.ElementTree as ET

from .core import SCHEMA, dependencies, environment, file_hash, inventory, now, snapshot
from .runner import _pytest, comparison_identity, execute, isolated_env


def partition_scope(scope):
    """一份选测结果分执行边界；函数级 Host 从文件批次显式 deselect。"""
    nodes = scope.get('nodes', [])
    hosts = list(dict.fromkeys(host if _covers(node, host) else node
        for host in HOST_NODES for node in nodes if _covers(node, host) or _covers(host, node)))
    scales = [n for n in SCALE_NODES if any(_covers(p, n) for p in nodes)]
    return {
        'offline': {**scope, 'nodes': [n for n in nodes if not any(_covers(h, n) for h in hosts)],
                    'node_tests': [], 'deselect': [*hosts, *scales]},
        'host': {**scope, 'nodes': hosts + scales, 'node_tests': []},
        'node': {**scope, 'nodes': [], 'node_tests': scope.get('node_tests', [])}}


def reuse_batch(receipt, expected, trusted, folder):
    """仅整批数据复用；远端身份由 GitHub API 获取，附件不授予信任。"""
    reasons = []
    if (trusted.get('repository') != trusted.get('head_repository') or not trusted.get('repository')
            or trusted.get('event') != 'pull_request' or trusted.get('conclusion') != 'success'
            or trusted.get('actor') not in trusted.get('allowed_actors', [])
            or not trusted.get('head_branch', '').startswith('codex/')
            or not trusted.get('run_id') or not trusted.get('run_attempt')):
        reasons.append('untrusted_run')
    if receipt.get('schema') != 'kr-ci-batch/v1' or receipt.get('status') != 'PASS':
        reasons.append('incomplete_batch')
    if set(receipt.get('jobs', {})) != {'offline', 'host', 'node'} or any(
            v != 'PASS' for v in receipt.get('jobs', {}).values()):
        reasons.append('incomplete_jobs')
    for key in ('tree', 'source_snapshot', 'environment'):
        if not receipt.get(key) or receipt.get(key) != expected.get(key):
            reasons.append(key + '_changed')
    if trusted.get('tree') != receipt.get('tree') or receipt.get('scope', {}).get('mode') != 'full':
        reasons.append('tested_tree_or_coverage_mismatch')
    if not receipt.get('evidence'):
        reasons.append('missing_evidence')
    for item in receipt.get('evidence', []):
        from .core import safe_path
        try:
            path = safe_path(folder, item['path'])
            if not path.is_file() or file_hash(path) != item['sha256']:
                reasons.append('corrupt_evidence')
        except (ValueError, KeyError):
            reasons.append('invalid_evidence')
    return {'reusable': not reasons, 'reasons': reasons, 'run_id': trusted.get('run_id'),
            'run_attempt': trusted.get('run_attempt')}

HOST_NODE = 'tests/test_product_console_launcher.py::test_real_dev_supervisor_starts_restarts_and_stops_without_touching_stable'
HOST_NODES = (HOST_NODE, 'tests/test_product_runtime.py', 'tests/test_browser_link_host.py',
              'tests/test_codex_stdio_contract.py::test_direct_stdio_probe_exposes_research_surface')
SCALE_NODES = ('tests/test_research_archive.py::test_ten_thousand_versions_frozen_membership',)


def _covers(parent, child):
    return (parent == child or child.startswith(parent.rstrip('/') + '/')
            or child.startswith(parent + '::'))


def docs_only_change(root, base):
    if not base:
        return False
    proc = subprocess.run(['git','diff','--name-only',base,'HEAD'],cwd=root,capture_output=True,text=True,encoding='utf-8',timeout=15)
    if proc.returncode:
        return False  # 浅克隆/未知身份保守执行，不能默默免检。
    paths = proc.stdout.splitlines()
    return bool(paths) and all(p.startswith('docs/') or (p.endswith('.md') and '/' not in p) for p in paths)


def requires_artifact_runtime(paths):
    """Only coarse shared boundaries, not a second fine-grained dependency graph."""
    return paths is None or any(p.startswith(('src/runtime/', 'src/tools/', 'browser_link/',
        'config/codex-product/', 'scripts/kr_eval/')) or p in {
        'src/server.py', 'pyproject.toml', 'scripts/product_install.py',
        'scripts/verify_release_candidate.py', 'scripts/build_product_lite_package.py',
        'config/project-structure.manifest.json', 'config/package-manifest.product-lite.json',
        'config/runtime-win-py312.lock', 'config/runtime-artifacts.json', 'config/browser-components.json',
        'config/browser-link.json', 'config/storage-ownership.manifest.json', 'scripts/browser_link_install.py',
        'scripts/kr_opencli.py', 'scripts/kr_mcp_continuity.py', 'scripts/product_cli_launcher.py',
        'scripts/product_console_launcher.py', 'scripts/setup_codex_product.py', '.github/workflows/ci.yml'}
        for p in paths)


def changed_paths(root, base):
    if not base:
        return None
    result = subprocess.run(['git', '-c', 'core.quotePath=false', 'diff', '--name-only', base, 'HEAD'],
                            cwd=root, capture_output=True, text=True, encoding='utf-8', timeout=15)
    return result.stdout.splitlines() if result.returncode == 0 else None


def select_scope(root, suite, paths, *, full=False):
    """少量粗组；未映射/空变更/未知基线一律完整检查，不依赖建议器截断。"""
    policy = suite.get('ci', {})
    # 旧私密治理测试仍可留档；其实现未投影到产品时不由粗组误收集。
    private_only = {node for node, dependency in policy.get('private_governance_tests', {}).items()
                    if not (root/dependency).is_file()}
    registered = [n for c in suite['cases'] if c['auto'] and c['executor']=='pytest' for n in c['nodes']]
    changed_tests = [p for p in paths or [] if p.startswith('tests/') and p.endswith('.py')
                     and (root/p).is_file()]
    all_nodes = [n for n in dict.fromkeys([*registered, *policy.get('full_nodes', []), *changed_tests])
                 if n.split('::')[0] not in private_only]
    all_node_tests = sorted(p.relative_to(root).as_posix() for p in (root/'tests').rglob('test_*.mjs'))
    full_scope = {'mode':'full', 'reason':'explicit_or_unmapped', 'changed_paths':paths,
                  'nodes':all_nodes, 'node_tests':all_node_tests, 'excluded_private_tests':sorted(private_only)}
    if full or not paths or not policy or any(fnmatch.fnmatchcase(path,p)
            for path in paths or [] for p in policy.get('full_sources',[])):
        return full_scope
    groups = set()
    for path in paths:
        if path.startswith('docs/') or (path.endswith('.md') and '/' not in path):
            continue
        matches = [name for name, group in policy['groups'].items()
                   if any(fnmatch.fnmatchcase(path, p) for p in
                          (group['tests'] if path.startswith('tests/') else group['sources']))]
        if not matches:
            return full_scope
        groups.update(matches)
    if not groups:
        return full_scope
    # 扫描嵌套用例与Node用例，但仍复用原测试，不生成第二套题库。
    candidates = sorted(set(all_nodes + [p.relative_to(root).as_posix()
        for p in (root/'tests').rglob('test_*.py')]))
    patterns = [p for name in groups for p in policy['groups'][name]['tests']]
    selected = [n for n in candidates if n.split('::')[0] not in private_only
                and any(fnmatch.fnmatchcase(n.split('::')[0], p) for p in patterns)]
    node_tests = [n for n in all_node_tests if any(fnmatch.fnmatchcase(n,p) for p in patterns)]
    if not selected and not node_tests:
        return full_scope
    return {'mode':'affected', 'reason':'coarse_groups', 'groups':sorted(groups), 'changed_paths':paths,
            'nodes':list(dict.fromkeys([*policy['base_nodes'], *selected])), 'node_tests':node_tests,
            'excluded_private_tests':sorted(private_only)}


def _node_tests(root, nodes, folder):
    if not nodes:
        return {'verdict':'NOT_RUN', 'summary':'no selected Node tests'}
    started = time.monotonic()
    junit = folder/'node-junit.xml'
    with (folder/'node-stdout.txt').open('x',encoding='utf-8') as out, (folder/'node-stderr.txt').open('x',encoding='utf-8') as err:
        try:
            process = subprocess.run(['node','--test','--test-reporter=spec','--test-reporter-destination=stdout',
                '--test-reporter=junit','--test-reporter-destination='+str(junit),*nodes],cwd=root,env=isolated_env(root,folder),
                                     stdout=out,stderr=err,timeout=180)
            tree = ET.parse(junit)
            count = len(list(tree.iter('testcase')))
            verdict = ('ERROR' if not count or list(tree.iter('error')) else
                'FAIL' if process.returncode or list(tree.iter('failure')) else
                'SKIPPED' if list(tree.iter('skipped')) else 'PASS')
            return {'verdict':verdict, 'returncode':process.returncode, 'tests':count,
                    'nodes':nodes, 'elapsed_s':time.monotonic()-started}
        except (OSError, subprocess.TimeoutExpired, ET.ParseError) as exc:
            return {'verdict':'ERROR','error_type':type(exc).__name__,'nodes':nodes, 'elapsed_s':time.monotonic()-started}


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


def run_ci(root, suite, ledger, extra_nodes=(), *, scope=None, group='all'):
    if group not in {'all', 'offline', 'host', 'node'}:
        raise ValueError('invalid_ci_group')
    original_scope = scope
    if group != 'all':
        scope = partition_scope(scope or {'nodes': [n for c in suite['cases'] if c['auto'] and c['executor']=='pytest' for n in c['nodes']], 'node_tests': []})[group]
    clock_started = time.monotonic()
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
        if case['auto'] and case['executor']=='surface' and group in {'all', 'offline'}:
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
    if group == 'offline':
        host_nodes = []
    offline_nodes = [n for n in nodes if n not in scale_nodes and not any(_covers(host, n) for host in host_nodes)]
    # 当前统一批次超过千项；保留逐例进度/慢例与90秒堆栈，整批仍有明确硬期限。
    outcome = _pytest(root, {"nodes": offline_nodes, "timeout_seconds": 1200,
                            "deselect": list(dict.fromkeys([*host_nodes, *scale_nodes, *(scope or {}).get('deselect', [])]))}, tests_dir) if offline_nodes and contracts_ok else {"verdict": "PASS" if contracts_ok else "BLOCKED", "returncode": 0 if contracts_ok else None, 'applicable': False}
    junit = tests_dir / "junit.xml"
    tests = list(ET.parse(junit).iter("testcase")) if junit.is_file() else []
    evidence = [{"path": p.relative_to(folder).as_posix(), "sha256": file_hash(p)} for p in tests_dir.iterdir() if p.is_file()]
    batches = {"offline": outcome}
    if group == 'offline':
        scale_nodes = []
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
        elif case["executor"] == "pytest" and case["auto"] and scope and (not all(any(_covers(n,c) for n in nodes) for c in case['nodes']) or any(_covers(c, d) or _covers(d, c) for c in case['nodes'] for d in scope.get('deselect', []))):
            result = {'verdict':'NOT_RUN','summary':'case not fully selected; no new whole-case verdict','evidence':[]}
        elif case["executor"] == "pytest" and case["auto"]:
            selected = [t for t in tests if any(_matches(t, n) for n in case["nodes"])]
            covered = all(any(_matches(t, n) for t in tests) for n in case["nodes"])
            verdict = "ERROR" if not covered or outcome.get("returncode") not in {0, 1} else "ERROR" if any(t.find("error") is not None for t in selected) else "FAIL" if any(t.find("failure") is not None for t in selected) else "SKIPPED" if any(t.find("skipped") is not None for t in selected) else "PASS"
            result = {"verdict": verdict, "summary": f"single CI pytest execution: {len(selected)} tests", "evidence": evidence}
        elif not case["auto"]:
            result = {"verdict": "NOT_RUN", "summary": "CI does not run external/manual business evidence", "evidence": []}
        elif group not in {'all', 'offline'}:
            result = {'verdict': 'NOT_RUN', 'summary': 'different CI execution group', 'evidence': []}
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
        'elapsed_s': time.monotonic() - clock_started, 'ci_group': group, 'original_scope': original_scope,
        "counts": dict(Counter(r["verdict"] for r in rows)), "ci_scope": scope or {'mode':'full'}, "ci_pytest": {**outcome, 'batches': batches},
        "ci_evidence": evidence})
    passed = outcome["verdict"] == "PASS" and not inv["issues"] and not run["changed_during_execution"] and all(r["verdict"] == "PASS" for r in rows if r["layer"] not in {"native_mcp", "installed", "live"} and not (scope and r['verdict']=='NOT_RUN'))
    return run, passed


def aggregate_jobs(folder, expected_jobs):
    """必需 job 没跑/回执损坏不能被 always() 汇总为绿。"""
    from pathlib import Path
    from .core import digest
    reports, evidence = {}, []
    for group in ('offline', 'host', 'node'):
        paths = list(folder.glob(f'ci-{group}-*/ci-outcome.json')) or list(folder.glob(f'{group}/ci-outcome.json'))
        paths.sort(key=lambda p: int(p.parent.name.rsplit('-', 1)[-1]) if p.parent.name.startswith('ci-') else 0)
        if not paths or expected_jobs.get(group) != 'success':
            raise ValueError('required_ci_job_missing_or_failed:' + group)
        reports[group] = json.loads(paths[-1].read_text(encoding='utf-8'))
        if reports[group].get('status') != 'PASS' or reports[group].get('group') != group:
            raise ValueError('required_ci_job_not_pass:' + group)
    trees = {r['tree'] for r in reports.values()}
    if len(trees) != 1:
        raise ValueError('ci_jobs_tested_different_trees')
    snapshots = {r['source_snapshot'] for r in reports.values()}
    if len(snapshots) != 1:
        raise ValueError('ci_jobs_tested_different_source_bytes')
    for path in folder.rglob('*'):
        if path.is_file():
            evidence.append({'path': path.relative_to(folder).as_posix(), 'sha256': file_hash(path)})
    return {'schema': 'kr-ci-batch/v1', 'status': 'PASS', 'tree': trees.pop(),
            'source_snapshot':snapshots.pop(),
            'environment': {g: r['environment'] for g, r in reports.items()},
            'scope': {'mode': 'full' if all(r['scope']['mode']=='full' for r in reports.values()) else 'affected'},
            'jobs': {g: 'PASS' for g in reports}, 'evidence': evidence}


def try_workflow_reuse(root, group, destination):
    """main 只向合并 PR 的明确成功 run 要证据；任何缺口回到正常执行。"""
    import io
    import os
    import urllib.request
    import zipfile
    from pathlib import Path
    from .core import _git, safe_path
    if os.environ.get('GITHUB_EVENT_NAME') != 'push' or os.environ.get('GITHUB_REF') != 'refs/heads/main':
        return {'reusable': False, 'reasons': ['not_main_push']}
    repository = os.environ['GITHUB_REPOSITORY']
    token = os.environ.get('GH_TOKEN', '')
    def api(path, raw=False):
        request = urllib.request.Request('https://api.github.com/' + path,
            headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28'})
        class StripRedirectCredentials(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
                if redirected:
                    redirected.remove_header('Authorization')
                return redirected
        with urllib.request.build_opener(StripRedirectCredentials()).open(request, timeout=20) as response:
            data = response.read(64 * 1024 * 1024 + 1)
        if len(data) > 64 * 1024 * 1024:
            raise ValueError('ci_artifact_too_large')
        return data if raw else json.loads(data)
    try:
        sha = _git(root, ['rev-parse', 'HEAD'])
        prs = api(f'repos/{repository}/commits/{sha}/pulls')
        pr = next(p for p in prs if p.get('merged_at') and p.get('merge_commit_sha') == sha)
        runs = api(f'repos/{repository}/actions/workflows/ci.yml/runs?event=pull_request&head_sha={pr["head"]["sha"]}&status=success&per_page=10')['workflow_runs']
        run = next(r for r in runs if r.get('head_repository', {}).get('full_name') == repository)
        artifacts = api(f'repos/{repository}/actions/runs/{run["id"]}/artifacts')['artifacts']
        artifact = next(a for a in artifacts if a['name'] == f'ci-batch-{run["id"]}-{run["run_attempt"]}' and not a['expired'])
        with zipfile.ZipFile(io.BytesIO(api(f'repos/{repository}/actions/artifacts/{artifact["id"]}/zip', True))) as bundle:
            # 不提取脚本、不允许链接/路径穿越/无界解压；仅验证原日志数据。
            if sum(i.file_size for i in bundle.infolist()) > 256 * 1024 * 1024:
                raise ValueError('ci_artifact_unpacked_too_large')
            destination.mkdir(parents=True, exist_ok=True)
            for item in bundle.infolist():
                if item.is_dir():
                    continue
                target = safe_path(destination, item.filename)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(bundle.read(item))
        report = json.loads((destination/'batch.json').read_text(encoding='utf-8'))
        expected = {**report, 'tree': _git(root, ['rev-parse', 'HEAD^{tree}']), 'source_snapshot':snapshot(root)['content_hash'],
                    'environment': {**report['environment'], group: environment()}}
        trusted = {'repository': repository, 'head_repository': run['head_repository']['full_name'],
                   'event': run['event'], 'actor': run['actor']['login'], 'allowed_actors': [repository.split('/')[0]],
                   'head_branch': pr['head']['ref'], 'conclusion': run['conclusion'], 'run_id': run['id'],
                   'run_attempt': run['run_attempt'], 'tree': expected['tree']}
        decision = reuse_batch(report, expected, trusted, destination)
        if decision['reusable']:
            outcomes = list(destination.glob(f'ci-{group}-*/ci-outcome.json')) or list(destination.glob(f'{group}/ci-outcome.json'))
            outcome = max(outcomes, key=lambda p: int(p.parent.name.rsplit('-', 1)[-1]) if p.parent.name.startswith('ci-') else 0)
            value = json.loads(outcome.read_text(encoding='utf-8'))
            value['reuse'] = decision
            return {**decision, 'outcome': value}
        return decision
    except (OSError, ValueError, KeyError, TypeError, StopIteration, zipfile.BadZipFile):
        return {'reusable': False, 'reasons': ['no_complete_trusted_matching_batch']}


def main():
    import argparse
    import os
    from pathlib import Path
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('reuse', 'aggregate', 'node'))
    parser.add_argument('--group', choices=('offline', 'host', 'node'))
    parser.add_argument('--folder', type=Path, required=True)
    args = parser.parse_args()
    if args.action == 'reuse':
        result = try_workflow_reuse(Path.cwd(), args.group, args.folder/'reuse')
        if result['reusable']:
            args.folder.mkdir(parents=True, exist_ok=True)
            (args.folder/'ci-outcome.json').write_text(json.dumps(result['outcome']), encoding='utf-8')
        with open(os.environ['GITHUB_OUTPUT'], 'a', encoding='utf-8') as stream:
            stream.write('reused=' + str(result['reusable']).lower() + '\n')
    elif args.action == 'aggregate':
        result = aggregate_jobs(args.folder, json.loads(os.environ['KR_CI_NEEDS']))
        (args.folder/'batch.json').write_text(json.dumps(result), encoding='utf-8')
    else:
        from .core import _git, load_suite
        root = Path.cwd()
        base = os.environ.get('KR_EVAL_BASE')
        scope = ({'mode':'docs','nodes':[],'node_tests':[]} if docs_only_change(root, base) else
            select_scope(root, load_suite(root), changed_paths(root, base), full=os.environ.get('GITHUB_EVENT_NAME') == 'push'))
        args.folder.mkdir(parents=True, exist_ok=True)
        started = time.monotonic()
        outcome = _node_tests(root, scope['node_tests'], args.folder) if scope['node_tests'] else {'verdict': 'PASS', 'applicable': False}
        result = {'schema': 'kr-ci-job/v1', 'status': outcome['verdict'], 'group': 'node',
                  'source_snapshot':snapshot(root)['content_hash'],
                  'tree': _git(root, ['rev-parse', 'HEAD^{tree}']), 'environment': environment(),
                  'scope': scope, 'outcome': outcome, 'elapsed_s': time.monotonic()-started}
        (args.folder/'ci-outcome.json').write_text(json.dumps(result), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False))
    return int(result.get('status') not in {None, 'PASS'})


if __name__ == '__main__':
    raise SystemExit(main())
