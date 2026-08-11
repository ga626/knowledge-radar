from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import scripts.kr_quality_gate as gate


def _pass_payload(schema: str) -> dict:
    return {"schema": schema, "status": "pass"}


def _forbidden_runner(name: str):
    def _runner(*_args, **_kwargs):
        raise AssertionError(f"{name} should not run for this campaign profile")

    return _runner


def test_campaign_smoke_is_lightweight_runtime_patrol(monkeypatch) -> None:
    monkeypatch.setattr(gate, "_run_full", _forbidden_runner("_run_full"))
    monkeypatch.setattr(gate, "_run_changed", _forbidden_runner("_run_changed"))
    monkeypatch.setattr(gate, "campaign_profile_manifest", lambda: _pass_payload("profiles/v1"))
    monkeypatch.setattr(gate, "run_campaign_runtime_smoke", lambda _root: _pass_payload("runtime-smoke/v1"))
    monkeypatch.setattr(gate, "run_campaign_compact_patrol_contract", lambda _root: _pass_payload("compact-patrol/v1"))
    monkeypatch.setattr(gate, "run_campaign_contract_smoke", lambda _root: _pass_payload("contract-smoke/v1"))
    monkeypatch.setattr(gate, "run_command", lambda *_args, **_kwargs: {"schema": "docs/v1", "status": "pass", "command": "docs status index check"})

    results = gate._run_campaign("smoke")

    assert [item["command"] for item in results] == [
        "campaign profile manifest",
        "campaign runtime smoke",
        "campaign compact patrol contract",
        "docs status index check",
        "campaign contract smoke",
    ]
    assert all("pytest" not in item["command"] for item in results)
    assert all("build_product_lite_package.py" not in item["command"] for item in results)


def test_campaign_deep_keeps_changed_gate_and_campaign_checks(monkeypatch) -> None:
    monkeypatch.setattr(gate, "_run_full", _forbidden_runner("_run_full"))
    monkeypatch.setattr(gate, "_run_changed", lambda: [_pass_payload("changed-gate/v1") | {"command": "changed quality gate"}])
    monkeypatch.setattr(gate, "campaign_profile_manifest", lambda: _pass_payload("profiles/v1"))
    monkeypatch.setattr(gate, "run_campaign_runtime_smoke", lambda _root: _pass_payload("runtime-smoke/v1"))
    monkeypatch.setattr(gate, "run_campaign_compact_patrol_contract", lambda _root: _pass_payload("compact-patrol/v1"))
    monkeypatch.setattr(gate, "run_campaign_contract_smoke", lambda _root: _pass_payload("contract-smoke/v1"))
    monkeypatch.setattr(gate, "run_campaign_deep_checks", lambda _root: _pass_payload("deep-checks/v1"))
    monkeypatch.setattr(gate, "run_campaign_fault_injection", lambda: _pass_payload("fault-injection/v1"))
    monkeypatch.setattr(gate, "run_campaign_tool_readiness", lambda: _pass_payload("tool-readiness/v1"))
    monkeypatch.setattr(
        gate,
        "run_command",
        lambda *_args, **_kwargs: {"schema": "pytest/v1", "status": "pass", "command": "pytest campaign quality gates"},
    )

    commands = [item["command"] for item in gate._run_campaign("deep")]

    assert "changed quality gate" in commands
    assert "campaign compact patrol contract" in commands
    assert "campaign deep deterministic checks" in commands
    assert "campaign fault injection" in commands
    assert "campaign tool readiness" in commands
    assert "pytest campaign quality gates" in commands


def test_quality_gate_manifest_separates_smoke_from_heavy_release_layers() -> None:
    manifest = json.loads((ROOT / "config" / "quality-gates.manifest.json").read_text(encoding="utf-8"))

    smoke_runs = manifest["campaign_profiles"]["smoke"]["runs"]
    assert "full quality gate" not in smoke_runs
    assert not any("pytest" in item for item in smoke_runs)
    assert not any("package" in item.lower() or "dry-run" in item.lower() for item in smoke_runs)
    assert "runtime health/capabilities/task/decision-log smoke" in smoke_runs

    deep_runs = manifest["campaign_profiles"]["deep"]["runs"]
    assert "changed quality gate" in deep_runs
    assert "campaign and recent runtime regression pytest" in deep_runs

    full_release_commands = manifest["commands"]["full_release"]
    assert any("build_product_lite_package.py --dry-run" in command for command in full_release_commands)
    assert any("verify_package_integrity.py" in command for command in full_release_commands)


def test_campaign_quality_gate_rejects_write_state(monkeypatch) -> None:
    monkeypatch.setattr(sys, "argv", ["kr_quality_gate.py", "--campaign", "--profile", "smoke", "--write-state"])

    with pytest.raises(SystemExit) as error:
        gate.main()

    assert error.value.code == 2
