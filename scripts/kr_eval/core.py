"""版本化合同、内容身份、不可覆盖账本和当前状态投影。"""
from __future__ import annotations

import ast
from collections import Counter
from datetime import datetime, timezone
import fnmatch
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import uuid

SCHEMA = "knowledgeradar-evaluation/v1"
VERDICTS = {"PASS", "FAIL", "ERROR", "BLOCKED", "SKIPPED", "NOT_RUN"}
READINESS = {"ready", "degraded", "needs_interaction", "unavailable", "unknown"}
SCOPES = ("src/", "scripts/", "tests/", "config/", "bridge/", ".github/workflows/")


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def file_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def write_new(path, data):
    """独占创建；任何历史记录都不能被本入口覆盖。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def safe_path(root, relative):
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError(f"path escapes repository: {relative}")
    return path


def discover(root):
    """只读AST发现，不导入 server，不启动浏览器、后台任务或读取认证。"""
    tree = ast.parse((root / "src/server.py").read_text(encoding="utf-8-sig"))
    found = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            call = decorator if isinstance(decorator, ast.Call) else None
            func = call.func if call else decorator
            native = isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id == "mcp" and func.attr == "tool"
            dispatched = isinstance(func, ast.Name) and func.id == "nonblocking_mcp_tool"
            if native or dispatched:
                name = node.name
                if call:
                    for kw in call.keywords:
                        if kw.arg == "name":
                            if not isinstance(kw.value, ast.Constant) or not isinstance(kw.value.value, str):
                                raise ValueError("dynamic MCP name needs an explicit discovery adapter")
                            name = kw.value.value
                if name in found:
                    raise ValueError(f"duplicate MCP registration: {name}")
                found[name] = {"function": node.name, "signature": ast.dump(node.args, include_attributes=False),
                               "documented": bool(ast.get_docstring(node)), "line": node.lineno}
    if not found:
        raise ValueError("MCP discovery returned no entries")
    return found


def load_suite(root, directory="config/evaluation"):
    directory = safe_path(root, directory)
    catalog = read_json(directory / "catalog.json")
    definitions = read_json(directory / "cases.json")
    if catalog.get("schema") != SCHEMA or definitions.get("schema") != SCHEMA:
        raise ValueError("unsupported evaluation schema")
    caps, cases = catalog["capabilities"], definitions["cases"]
    for rows, key in [(caps, "capability_id"), (cases, "case_id")]:
        ids = [row[key] for row in rows]
        if len(set(ids)) != len(ids) or not all(ids):
            raise ValueError(f"duplicate or empty {key}")
    cap_map = {c["capability_id"]: c for c in caps}
    for cap in caps:
        if not cap.get("version") or cap["lifecycle"] not in {"active", "retired"}:
            raise ValueError("invalid capability contract")
        if any(x not in cap_map for x in cap.get("depends_on", [])):
            raise ValueError("unknown capability dependency")
    for case in cases:
        if not all(ch.isalnum() or ch in '._-' for ch in case["case_id"]) or case["case_id"] in {".", ".."}:
            raise ValueError("unsafe case ID")
        if not case.get("version") or not case.get("oracle_version") or not case.get("success"):
            raise ValueError("missing version or business success rule")
        if not case.get("capabilities") or any(x not in cap_map for x in case["capabilities"]):
            raise ValueError("case has unknown capabilities")
        if case["executor"] not in {"surface", "pytest", "natural_trace", "source_ecology", "benchmark_json", "manual"}:
            raise ValueError("unsupported executor")
        if case["layer"] not in {"contract", "fixture", "integration", "live", "native_mcp", "installed"}:
            raise ValueError("unsupported evidence layer")
        if case["executor"] != "manual" and case["layer"] in {"live", "native_mcp", "installed"}:
            raise ValueError("external evidence requires explicit, separately bound collection; offline executor forbidden")
        if not case.get("dependencies") or not isinstance(case.get("auto"), bool):
            raise ValueError("missing dependencies or execution policy")
        if case.get("ttl_seconds") is not None and case["ttl_seconds"] <= 0:
            raise ValueError("invalid TTL")
        for pattern in case["dependencies"]:
            safe_path(root, pattern)
        contract = case.get("receipt_contract")
        if contract and (not contract.get("tool") or not contract.get("assertions") or not contract.get("identity_fields")):
            raise ValueError("external receipt needs reviewed business assertions and identity fields")
    return {"catalog": catalog, "cases": cases, "caps": cap_map}


def inventory(root, suite):
    found = discover(root)
    registered = set(found)
    active = [c for c in suite["catalog"]["capabilities"] if c["lifecycle"] == "active"]
    owners = {}
    issues = []
    for cap in active:
        for name in cap.get("entries", []):
            if name in owners:
                issues.append({"code": "duplicate_entry_owner", "entry": name})
            owners[name] = cap["capability_id"]
            if name not in registered:
                issues.append({"code": "missing_or_renamed_entry", "entry": name, "capability_id": cap["capability_id"]})
        if not any(cap["capability_id"] in c["capabilities"] for c in suite["cases"]):
            issues.append({"code": "uncovered_capability", "capability_id": cap["capability_id"]})
    for name in sorted(registered - set(owners)):
        issues.append({"code": "uncovered_entry", "entry": name})
    return {"registered_count": len(found), "registered": found, "issues": issues,
            "retired": [c["capability_id"] for c in suite["catalog"]["capabilities"] if c["lifecycle"] == "retired"]}


def _git(root, args):
    result = subprocess.run(["git", *args], cwd=root, capture_output=True, timeout=15)
    return result.stdout.decode("utf-8", errors="replace").strip() if result.returncode == 0 else ""


def snapshot(root):
    listed = _git(root, ["ls-files", "-c", "-o", "--exclude-standard", "-z"])
    names = listed.split("\0") if listed else [p.relative_to(root).as_posix() for prefix in SCOPES for p in (root / prefix).rglob("*") if p.is_file()]
    files = {}
    for name in sorted(set(names)):
        if not (name.startswith(SCOPES) or name in {"pyproject.toml", "uv.lock", "requirements.txt", "requirements.lock"}):
            continue
        if any(x in Path(name).parts for x in {"__pycache__", "node_modules", ".pytest_cache", "data", "local", ".git"}):
            continue
        if name in {"config/profile_registry.json", "config/runtime.env", "config/mcp-approvals.json"}:
            continue
        path = safe_path(root, name)
        if path.is_file():
            files[name] = file_hash(path)
    return {"files": files, "content_hash": digest(files), "git_head": _git(root, ["rev-parse", "HEAD"]),
            "git_status": _git(root, ["status", "--porcelain"]), "source_kind": "working_tree"}


def environment():
    packages = {}
    for dist in importlib.metadata.distributions():
        name = dist.metadata.get("Name")
        if name:
            packages[name.lower()] = dist.version
    facts = {"python": platform.python_version(), "implementation": platform.python_implementation(),
             "platform": platform.platform(), "machine": platform.machine(), "packages": dict(sorted(packages.items())),
             "execution_policy": "offline_no_credentials_v1", "models": "not_used", "network": "disabled"}
    return {"facts": facts, "fingerprint": digest(facts), "python_executable": sys.executable}


def _module_map(root, snap):
    result = {}
    for path in snap["files"]:
        if path.endswith(".py"):
            module = path[:-3].replace("/", ".")
            if module.endswith(".__init__"):
                module = module[:-9]
            result[module] = path
            if module.startswith("src."):
                result[module[4:]] = path
    return result


def dependencies(root, suite, case, snap):
    """显式globs + 传递能力依赖 + 相对/绝对Python imports。动态缺口保守失效。"""
    cache = suite.setdefault("_dependency_cache", {})
    cache_key = (snap["content_hash"], digest(case))
    if cache_key in cache:
        return cache[cache_key]
    patterns = list(case["dependencies"])
    seen = set()
    def add(cap_id):
        if cap_id in seen:
            return
        seen.add(cap_id)
        cap = suite["caps"][cap_id]
        patterns.extend(cap.get("dependencies", []))
        for item in cap.get("depends_on", []):
            add(item)
    for cap in case["capabilities"]:
        add(cap)
    patterns += ["pyproject.toml", "*lock*", "requirements*.txt", "scripts/kr_eval/*.py", "scripts/kr_evaluate.py"]
    selected = {p for p in snap["files"] if any(fnmatch.fnmatchcase(p, pattern) for pattern in patterns)}
    missing = [p for p in case["dependencies"] if not any(fnmatch.fnmatchcase(x, p) for x in snap["files"])]
    modules = _module_map(root, snap)
    pending = list(selected)
    errors = []
    parsed = set()
    while pending:
        path = pending.pop()
        if path in parsed or not path.endswith(".py"):
            continue
        parsed.add(path)
        import_cache = suite.setdefault("_import_cache", {})
        import_key = (path, snap["files"][path])
        cached = import_cache.get(import_key)
        if cached is not None:
            if isinstance(cached, str):
                errors.append(cached)
                continue
            imports = cached
        else:
            try:
                tree = ast.parse((root / path).read_text(encoding="utf-8-sig"))
            except (SyntaxError, UnicodeError, OSError) as exc:
                error = f"{path}: {type(exc).__name__}"
                import_cache[import_key] = error
                errors.append(error)
                continue
            package = path[:-3].replace("/", ".").split(".")[:-1]
            if path.endswith("/__init__.py"):
                package = path[:-12].replace("/", ".").split(".")
            imports = []
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imports.extend(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom):
                    prefix = package[:len(package) - node.level + 1] if node.level else []
                    base = ".".join([*prefix, node.module or ""]).strip(".")
                    imports += [base, *(f"{base}.{alias.name}" for alias in node.names)]
            import_cache[import_key] = imports
        for module in imports:
            parts = module.split(".")
            for end in range(1, len(parts) + 1):
                imported = modules.get(".".join(parts[:end]))
                if imported and imported not in selected:
                    selected.add(imported)
                    pending.append(imported)
    caps = {key: suite["caps"][key] for key in sorted(seen)}
    result = {"files": {p: snap["files"][p] for p in sorted(selected)}, "contract_hash": digest({"case": case, "caps": caps}),
              "missing": missing, "errors": errors}
    cache[cache_key] = result
    return result


class Ledger:
    def __init__(self, path):
        self.path = Path(path).resolve()

    def begin(self):
        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex
        folder = self.path / "runs" / run_id
        folder.mkdir(parents=True, exist_ok=False)
        write_new(folder / "started.json", {"schema": SCHEMA, "run_id": run_id, "started_at": now()})
        return run_id, folder

    def save(self, run):
        payload = {**run, "integrity": digest(run)}
        write_new(self.path / "runs" / run["run_id"] / "run.json", payload)
        return payload

    def read(self, run_id):
        if Path(run_id).name != run_id:
            raise ValueError("invalid run ID")
        folder = self.path / "runs" / run_id
        run = read_json(folder / "run.json")
        raw = {k: v for k, v in run.items() if k != "integrity"}
        if digest(raw) != run.get("integrity"):
            raise ValueError(f"ledger integrity mismatch: {run_id}")
        for result in [*run["results"], {"evidence": run.get('ci_evidence', [])}]:
            for item in result.get("evidence", []):
                evidence = safe_path(folder, item["path"])
                if not evidence.is_file() or file_hash(evidence) != item["sha256"]:
                    raise ValueError(f"evidence missing/corrupt: {run_id}/{item['path']}")
        return run

    def history(self):
        rows = []
        for folder in sorted((self.path / "runs").glob("*")):
            if not (folder / "run.json").is_file():
                raise ValueError(f"incomplete run requires investigation: {folder.name}")
            rows.append(self.read(folder.name))
        return sorted(rows, key=lambda row: (row["started_at"], row["run_id"]))

    def accept(self, run_id, reason):
        run = self.read(run_id)
        if not reason.strip():
            raise ValueError("baseline acceptance needs a reason")
        event = {"schema": SCHEMA, "event": "baseline_accepted", "run_id": run_id, "integrity": run["integrity"], "reason": reason, "at": now()}
        write_new(self.path / "baselines" / f"{uuid.uuid4().hex}.json", event)
        return event

    def baseline(self):
        events = [read_json(p) for p in (self.path / "baselines").glob("*.json")]
        if not events:
            raise ValueError("no explicitly accepted baseline")
        event = max(events, key=lambda r: r["at"])
        run = self.read(event["run_id"])
        if event["integrity"] != run["integrity"]:
            raise ValueError("accepted baseline integrity mismatch")
        return run


def changed_files(old, new):
    return sorted(p for p in old.keys() | new.keys() if old.get(p) != new.get(p))


def applicability(root, suite, case, result, run, current, env, at=None):
    deps = dependencies(root, suite, case, current)
    reasons = []
    if deps["missing"] or deps["errors"]:
        return "unknown", ["dependency_resolution_incomplete", *deps["missing"], *deps["errors"]]
    if run["environment"]["fingerprint"] != env["fingerprint"]:
        reasons.append("environment_changed")
    if result["dependency_identity"]["contract_hash"] != deps["contract_hash"]:
        reasons.append("contract_changed")
    affected = changed_files(result["dependency_identity"]["files"], deps["files"])
    if affected:
        reasons.append("dependency_content_changed")
    # 未映射产品变化使所有旧结果失效；selector只作建议，不为未覆盖代码放行。
    changes = changed_files(run["snapshot"]["files"], current["files"])
    mapped = set()
    for item in suite["cases"]:
        mapped.update(dependencies(root, suite, item, current)["files"])
    unmapped = [p for p in changes if p not in mapped and p.startswith(SCOPES)]
    if unmapped:
        reasons.append("unmapped_change")
    if run.get("changed_during_execution"):
        reasons.append("changed_during_execution")
    if case.get("ttl_seconds") is not None:
        elapsed = ((at or datetime.now(timezone.utc)) - datetime.fromisoformat(run["finished_at"])).total_seconds()
        if elapsed < 0:
            reasons.append("clock_before_run")
        elif elapsed > case["ttl_seconds"]:
            reasons.append("ttl_expired")
    return ("stale" if reasons else "fresh"), reasons + affected + unmapped


def status(root, suite, ledger, *, at=None):
    snap, env = snapshot(root), environment()
    inv = inventory(root, suite)
    history = ledger.history()
    current = {}
    for run in history:
        if run["target"]["kind"] != "development_source":
            continue
        for row in run["results"]:
            # 仅引用实测；没有重测的复用记录不能伪造最近一次执行时间。
            if not row.get("reused_from"):
                current[row["case_id"]] = (row, run)
    rows = []
    for case in suite["cases"]:
        if case["case_id"] not in current:
            rows.append({"case_id": case["case_id"], "layer": case["layer"], "verdict": "NOT_RUN", "applicability": "unknown", "readiness": "unknown", "reasons": ["no_execution"]})
            continue
        row, run = current[case["case_id"]]
        applicable, reasons = applicability(root, suite, case, row, run, snap, env, at)
        if row["verdict"] == "NOT_RUN":
            applicable = "unknown" if applicable == "fresh" else applicable
            reasons = ["no_execution", *reasons]
        rows.append({"case_id": case["case_id"], "layer": case["layer"], "verdict": row["verdict"], "applicability": applicable,
                     "readiness": row["readiness"], "run_id": run["run_id"], "checked_at": run["finished_at"] if row["verdict"] != "NOT_RUN" else None, "reasons": reasons,
                     "known_issue": case.get("known_issue"), "summary": row.get("summary", "")})
    counts = {key: dict(Counter(row[key] for row in rows)) for key in ["verdict", "applicability", "readiness", "layer"]}
    gate = "INCOMPLETE"
    latest_ci = next((r for r in reversed(history) if r['target']['kind'] == 'development_source' and 'ci_pytest' in r), None)
    ci_gate = None
    if latest_ci:
        ci_fresh = latest_ci['snapshot']['content_hash'] == snap['content_hash'] and latest_ci['environment']['fingerprint'] == env['fingerprint'] and not latest_ci.get('changed_during_execution')
        ci_gate = {'run_id': latest_ci['run_id'], 'verdict': latest_ci['ci_pytest']['verdict'], 'applicability': 'fresh' if ci_fresh else 'stale'}
    if inv["issues"] or (ci_gate and ci_gate['applicability'] == 'fresh' and ci_gate['verdict'] != 'PASS') or any(r["verdict"] in {"FAIL", "ERROR", "BLOCKED"} and r["applicability"] == "fresh" for r in rows):
        gate = "BLOCKED"
    elif rows and all(r["verdict"] == "PASS" and r["applicability"] == "fresh" for r in rows):
        gate = "PASS"
    return {"schema": SCHEMA, "target": "development_source", "gate": gate, "ci_gate": ci_gate, "inventory": inv, "denominator": len(rows),
            "counts": counts, "cases": rows, "history_runs": len(history), "snapshot_hash": snap["content_hash"]}


def compare(before, after):
    old = {r["case_id"]: r for r in before["results"]}
    new = {r["case_id"]: r for r in after["results"]}
    rows = []
    for key in sorted(old.keys() | new.keys()):
        left, right = old.get(key), new.get(key)
        reasons = []
        if not left or not right:
            reasons.append("case_added_or_removed")
        else:
            for field in ["comparison_identity", "layer"]:
                if left.get(field) != right.get(field):
                    reasons.append(f"{field}_changed")
        if before["environment"]["fingerprint"] != after["environment"]["fingerprint"]:
            reasons.append("environment_changed")
        if any(r["environment"]["fingerprint"].startswith("unknown:") for r in (before, after)):
            reasons.append("environment_unmeasured")
        if before["target"] != after["target"]:
            reasons.append("target_changed")
        transition = "incomparable" if reasons else "unchanged"
        if not reasons and left["verdict"] != right["verdict"]:
            if left["verdict"] == "PASS" and right["verdict"] != "PASS":
                transition = "regression"
            elif left["verdict"] == "FAIL" and right["verdict"] == "PASS":
                transition = "fixed"
            else:
                transition = "verdict_changed"
        rows.append({"case_id": key, "before": left and left["verdict"], "after": right and right["verdict"],
                     "transition": transition, "reasons": reasons, "reused": bool(right and right.get("reused_from"))})
    gate = "BLOCKED" if any(r["transition"] == "regression" for r in rows) else "INCOMPLETE" if any(r["transition"] == "incomparable" or r["after"] != "PASS" for r in rows) else "PASS"
    if after.get('ci_pytest', {}).get('verdict', 'PASS') != 'PASS':
        gate = 'BLOCKED'
    return {"schema": SCHEMA, "before": before["run_id"], "after": after["run_id"], "gate": gate, "cases": rows}
