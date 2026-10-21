"""评测机制自己的故障测试；不以产品全绿作为底座正确性的断言。"""
import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.kr_eval.core import (SCHEMA, Ledger, applicability, compare, dependencies, environment,
                                  inventory, load_suite, snapshot, status, write_new)
from scripts.kr_eval.demo import demonstrate
from scripts.kr_eval.runner import run_suite


def fixture_repo(tmp_path):
    root = tmp_path / "source"
    for name in ["src", "tests", "config/evaluation"]:
        (root / name).mkdir(parents=True)
    (root / "src/server.py").write_text('@mcp.tool()\ndef demo():\n    """demo"""\n    pass\n', encoding="utf-8")
    (root / "src/product.py").write_text('from shared import ok\n', encoding="utf-8")
    (root / "src/shared.py").write_text('ok=True\n', encoding="utf-8")
    cap = dict(capability_id="demo", version=1, lifecycle="active", entries=["demo"], dependencies=["src/product.py"])
    case = dict(case_id="demo.fixture", version=1, oracle_version=1, capabilities=["demo"], executor="manual",
                layer="fixture", auto=True, dependencies=["src/product.py"], success="fixed business assertion", manual_reason="requires input")
    write_new(root / "config/evaluation/catalog.json", dict(schema=SCHEMA, capabilities=[cap]))
    write_new(root / "config/evaluation/cases.json", dict(schema=SCHEMA, cases=[case]))
    return root, load_suite(root), Ledger(tmp_path / "ledger")


def test_full_edit_retest_history_demonstration(tmp_path):
    result = demonstrate(ROOT, tmp_path)
    assert result["status"] == "PASS", result["checks"]
    assert len(result["checks"]) >= 18


def test_not_run_and_readiness_are_not_implicit_pass(tmp_path):
    root, suite, ledger = fixture_repo(tmp_path)
    run_suite(root, suite, ledger)
    result = status(root, suite, ledger)
    assert result["gate"] == "INCOMPLETE"
    assert result["cases"][0]["verdict"] == "NOT_RUN"
    assert result["cases"][0]["readiness"] == "unknown"


def test_ledger_never_overwrites_and_detects_evidence_corruption(tmp_path):
    root, suite, ledger = fixture_repo(tmp_path)
    run = run_suite(root, suite, ledger)
    with pytest.raises(FileExistsError):
        ledger.save({k: v for k, v in run.items() if k != "integrity"})
    evidence = ledger.path / "runs" / run["run_id"] / run["results"][0]["evidence"][0]["path"]
    evidence.write_text("tampered", encoding="utf-8")
    with pytest.raises(ValueError, match="evidence missing/corrupt"):
        ledger.read(run["run_id"])


def test_unknown_change_invalidates_and_deleted_dependency_is_unknown(tmp_path):
    root, suite, ledger = fixture_repo(tmp_path)
    run_suite(root, suite, ledger)
    (root / "src/unmapped.py").write_text("new=True", encoding="utf-8")
    result = status(root, suite, ledger)
    assert result["cases"][0]["applicability"] == "stale"
    assert "unmapped_change" in result["cases"][0]["reasons"]
    (root / "src/product.py").unlink()
    assert status(root, suite, ledger)["cases"][0]["applicability"] == "unknown"


def test_runtime_dependency_version_change_stales(tmp_path, monkeypatch):
    root, suite, ledger = fixture_repo(tmp_path)
    run = run_suite(root, suite, ledger)
    env = environment()
    env["fingerprint"] = "changed-dependency-set"
    app, reasons = applicability(root, suite, suite["cases"][0], run["results"][0], run, snapshot(root), env)
    assert app == "stale"
    assert "environment_changed" in reasons


def test_incomplete_run_and_untrusted_history_fail_closed(tmp_path):
    root, suite, ledger = fixture_repo(tmp_path)
    ledger.begin()
    with pytest.raises(ValueError, match="incomplete run"):
        status(root, suite, ledger)


def test_no_automatic_baseline_promotion(tmp_path):
    root, suite, ledger = fixture_repo(tmp_path)
    run = run_suite(root, suite, ledger)
    assert not (ledger.path / "baselines").exists()
    ledger.accept(run["run_id"], "explicit reference, incomplete result remains incomplete")
    assert len(list((ledger.path / "baselines").glob("*.json"))) == 1
    assert ledger.baseline()['run_id']==run['run_id']
    assert status(root, suite, ledger)["gate"] == "INCOMPLETE"


def test_fixture_cannot_claim_native_or_installed_success(tmp_path):
    root, suite, ledger = fixture_repo(tmp_path)
    p = root / "config/evaluation/cases.json"
    payload = json.loads(p.read_text(encoding="utf-8"))
    payload["cases"][0].update(executor="pytest", layer="native_mcp")
    p.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="external evidence"):
        load_suite(root)
