"""临时副本实演：修改产品正文，旧结果失效，重测并保留修复前后记录。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import shutil
import uuid

from .core import (SCHEMA, Ledger, compare, inventory, load_suite, read_json,
                   status, write_new)
from .runner import plan, run_suite


def demonstrate(source, output):
    folder = Path(output).resolve() / uuid.uuid4().hex
    root = folder / "source"
    for directory in ["src", "tests", "scripts", "config/evaluation"]:
        (root / directory).mkdir(parents=True, exist_ok=True)
    shutil.copytree(source / "scripts/kr_eval", root / "scripts/kr_eval", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copyfile(source / "scripts/kr_test_selector.py", root / "scripts/kr_test_selector.py")
    (root / "pyproject.toml").write_text('[tool.pytest.ini_options]\npythonpath=["src"]\n', encoding="utf-8")
    server = root / "src/server.py"
    server.write_text('@mcp.tool()\ndef detail(url: str):\n    """详情入口"""\n    return url\n', encoding="utf-8")
    product = root / "src/product.py"
    broken = 'from helper import present\ndef verified(content):\n    return True\n'
    fixed = 'from helper import present\ndef verified(content):\n    return present(content)\n'
    product.write_text(broken, encoding="utf-8")
    (root / "src/helper.py").write_text('def present(content):\n    return bool(content.strip())\n', encoding="utf-8")
    (root / "tests/test_business.py").write_text('from product import verified\ndef test_empty_not_verified():\n    assert verified("") is False\n', encoding="utf-8")
    catalog = {"schema": SCHEMA, "version": 1, "capabilities": [{"capability_id": "detail", "version": 1,
               "lifecycle": "active", "entries": ["detail"], "dependencies": ["src/product.py"], "depends_on": []}]}
    case = {"case_id": "detail.empty", "version": 1, "oracle_version": 1, "capabilities": ["detail"],
            "executor": "pytest", "layer": "fixture", "auto": True, "dependencies": ["src/product.py", "tests/test_business.py"],
            "nodes": ["tests/test_business.py"], "success": "空白正文不得验证通过", "ttl_seconds": 60}
    write_new(root / "config/evaluation/catalog.json", catalog)
    write_new(root / "config/evaluation/cases.json", {"schema": SCHEMA, "cases": [case]})
    ledger = Ledger(folder / "ledger")
    suite = load_suite(root)
    first = run_suite(root, suite, ledger)
    first_bytes = (ledger.path / "runs" / first["run_id"] / "run.json").read_bytes()
    product.write_text(fixed, encoding="utf-8")
    stale = status(root, suite, ledger)
    chosen = plan(root, suite, ledger)
    second = run_suite(root, suite, ledger, chosen["selected"])
    comparison = compare(first, second)
    current = status(root, suite, ledger)
    reused = run_suite(root, suite, ledger, reuse=True)
    # 同一脏路径再次改正文，不能被路径名指纹掩盖。
    product.write_text(fixed + "\n# second edit\n", encoding="utf-8")
    repeat_edit = status(root, suite, ledger)
    product.write_text(fixed, encoding="utf-8")
    (root / "src/helper.py").write_text('def present(content):\n    return bool(content.strip()) # changed dependency\n', encoding="utf-8")
    transitive = status(root, suite, ledger)
    (root / "src/helper.py").write_text('def present(content):\n    return bool(content.strip())\n', encoding="utf-8")
    (root / "README.md").write_text("unrelated docs", encoding="utf-8")
    unrelated = status(root, suite, ledger)
    server.write_text(server.read_text(encoding="utf-8") + '\n@mcp.tool()\ndef extra():\n    """新增入口"""\n    return 1\n', encoding="utf-8")
    added = inventory(root, suite)
    server.write_text('@mcp.tool()\ndef renamed(url: str):\n    """重命名入口"""\n    return url\n', encoding="utf-8")
    renamed = inventory(root, suite)
    server.write_text('@mcp.tool()\ndef unrelated():\n    """删除原入口"""\n    pass\n', encoding="utf-8")
    removed = inventory(root, suite)
    # 修改判定标准且提升版本，历史必须明确不可比。
    changed_case = {**case, "version": 2, "success": "升级后的独立标准"}
    (root / "config/evaluation/cases.json").write_text(__import__("json").dumps({"schema": SCHEMA, "cases": [changed_case]}), encoding="utf-8")
    new_suite = load_suite(root)
    third = run_suite(root, new_suite, ledger)
    rules = compare(second, third)
    (root / "tests/test_business.py").write_text('raise RuntimeError("broken evaluator")\n', encoding="utf-8")
    evaluator_error = run_suite(root, new_suite, ledger)
    expired = status(root, suite, Ledger(folder / "empty-store"))
    from .core import applicability, environment, snapshot
    ttl, ttl_reasons = applicability(root, suite, case, second["results"][0], second, snapshot(root), environment(),
                                     datetime.fromisoformat(second["finished_at"]) + timedelta(seconds=61))
    checks = {
        "original_failure": first["results"][0]["verdict"] == "FAIL",
        "code_edit_stales": stale["cases"][0]["applicability"] == "stale",
        "targeted_plan": chosen["selected"] == ["detail.empty"],
        "repair_passes": second["results"][0]["verdict"] == "PASS",
        "comparable_fix": comparison["cases"][0]["transition"] == "fixed",
        "current_fresh": current["cases"][0]["applicability"] == "fresh",
        "same_path_second_edit": repeat_edit["cases"][0]["applicability"] == "stale",
        "transitive_dependency": transitive["cases"][0]["applicability"] == "stale",
        "unrelated_docs_reusable": unrelated["cases"][0]["applicability"] == "fresh",
        "reuse_links_old_run": reused["results"][0].get("reused_from") == second["run_id"],
        "added_uncovered": any(r["code"] == "uncovered_entry" for r in added["issues"]),
        "rename_detected": any(r["code"] == "missing_or_renamed_entry" for r in renamed["issues"]),
        "deletion_detected": any(r["code"] == "missing_or_renamed_entry" for r in removed["issues"]),
        "changed_rules_incomparable": rules["cases"][0]["transition"] == "incomparable",
        "evaluator_error_never_pass": evaluator_error["results"][0]["verdict"] == "ERROR",
        "old_history_unchanged": (ledger.path / "runs" / first["run_id"] / "run.json").read_bytes() == first_bytes,
        "missing_execution_unknown": expired["cases"][0]["verdict"] == "NOT_RUN",
        "ttl_expires": ttl == "stale" and "ttl_expired" in ttl_reasons,
    }
    result = {"schema": SCHEMA, "status": "PASS" if all(checks.values()) else "FAIL", "checks": checks,
              "folder": str(folder), "runs": [r["run_id"] for r in [first, second, reused, third, evaluator_error]],
              "comparison": comparison, "rule_comparison": rules, "source_modified": "isolated copy only"}
    write_new(folder / "demonstration.json", result)
    return result
