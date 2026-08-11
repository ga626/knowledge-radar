from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from runtime.browser_channel import browser_channel_summary
from runtime.campaign_gates import campaign_profile_manifest, run_agent_sentinel_contract, run_campaign_fault_injection
from runtime.quality_gates import (
    classify_campaign_status,
    load_quality_gate_manifest,
    scan_forbidden_source_outputs,
    validate_provider_status_contract,
)
from runtime.quality_state import (
    QUALITY_STATE_SCHEMA,
    prune_missing_paths_from_quality_state,
    quality_snapshot,
    quality_state_freshness,
    quality_state_path,
    read_quality_state,
    write_quality_state,
)
import runtime.quality_state as quality_state_module
from scripts.kr_agent_env_probe import SCHEMA as AGENT_ENV_PROBE_SCHEMA, build_probe, format_brief
from scripts.kr_quality_gate import (
    CORE_CHANGED_TESTS,
    FALLBACK_CHANGED_TESTS,
    _can_auto_prepare_project_state,
    _merge_test_targets,
    _needs_runtime_coverage,
    _run_fast,
    _project_state_freshness_check,
    _selector_payload_to_changed_tests,
    main as quality_gate_main,
)
from scripts.kr_closeout_router import route_closeout
from scripts.kr_report_minimal_check import run_minimal_report_check
from scripts.kr_report_light_gate import run_report_light_gate


def test_docs_governance_legacy_boundary_is_explicit() -> None:
    import scripts.check_docs_governance as docs_governance

    result = docs_governance.run_checks()
    statuses = {item["report"]: item["status"] for item in result["stable_reports"]}

    assert statuses["docs/reference/知识雷达文档治理.md"] == "LEGACY_SKIPPED"
    assert statuses["docs/reference/感知层自然调用机制.md"] == "LEGACY_SKIPPED"


def test_quality_gate_manifest_declares_a_b_c_implemented() -> None:
    manifest = load_quality_gate_manifest()

    assert manifest["schema"] == "knowledgeradar-quality-gates/v1"
    assert manifest["phase_scope"]["A"] == "implemented"
    assert manifest["phase_scope"]["B"] == "implemented"
    assert manifest["phase_scope"]["C"] == "implemented"
    assert manifest["state_contract"]["path"] == "project_status/Quality-Gate-State.json"
    assert manifest["agent_adapters"]["codex"]["status"] == "implemented"
    assert manifest["agent_adapters"]["openclaw"]["status"] == "interface_designed"
    assert manifest["agent_adapters"]["git"]["hooks_path"] == ".githooks"
    assert manifest["campaign_profiles"]["default"] == "smoke"
    assert "data/filtered_*.json" in manifest["runtime_boundaries"]["forbidden_source_output_globs"]


def test_full_quality_gate_generates_indexes_before_drift_check(monkeypatch) -> None:
    import scripts.kr_quality_gate as quality_gate

    commands: list[list[str]] = []

    def fake_run(command: list[str], *_args, **_kwargs) -> dict:
        commands.append(command)
        return {"command": " ".join(command), "returncode": 0, "status": "pass", "stdout": "", "stderr": ""}

    monkeypatch.setattr(quality_gate, "run_command", fake_run)
    monkeypatch.setattr(quality_gate, "_run_changed", lambda: [])

    quality_gate._run_full()
    rendered = [" ".join(command) for command in commands]

    assert next(index for index, command in enumerate(rendered) if "generate_script_lifecycle_inventory.py" in command) < next(
        index for index, command in enumerate(rendered) if "check_doc_drift.py" in command
    )


def test_validation_check_registry_declares_report_minimal_side_effect_free() -> None:
    manifest = json.loads((ROOT / "config" / "validation-checks.manifest.json").read_text(encoding="utf-8"))

    assert manifest["schema"] == "knowledgeradar-validation-check-registry/v1"
    bundle = manifest["bundles"]["report-minimal"]
    assert bundle["mode"] == "draft-report"
    assert bundle["default_side_effect"] == "none"
    for check_id in bundle["checks"]:
        assert manifest["checks"][check_id]["side_effect"] == "none"


def test_agent_environment_probe_reports_compact_bootstrap_contract() -> None:
    probe = build_probe()

    assert probe["schema"] == AGENT_ENV_PROBE_SCHEMA
    assert probe["status"] in {"PASS", "FAIL"}
    assert probe["python"]["recommended"].endswith(".python312\\python.exe")
    assert probe["python"]["import_mode"].startswith("src layout")
    assert "PowerShell" in probe["shell"]["expected"] or probe["shell"]["current_os"] != "Windows"
    assert probe["quality_state"]["status_class"] in {"PASS", "EXPECTED_DEGRADED", "FAIL", "NEEDS_INTERACTION", "STALE"}
    assert isinstance(probe["quality_state"]["closeout_ready"], bool)
    assert probe["commands"]["changed_write_state"].endswith("--write-state")
    assert "kr_closeout_router.py" in probe["commands"]["closeout_router"]
    assert "kr_report_minimal_check.py" in probe["commands"]["report_minimal_gate"]
    assert "--report-light" not in probe["commands"].get("report_minimal_gate", "")
    assert "report_only_closeout" in probe["validation_policy"]
    assert "report-minimal" in probe["validation_policy"]["report_only_closeout"]
    assert "targeted_pytest" in probe["commands"]
    assert "full_required_when" in probe["validation_policy"]
    assert "kr_python_exec.py" in probe["commands"]["python_exec_stdin"]


def test_agent_environment_brief_is_short_and_actionable() -> None:
    brief = format_brief(build_probe())

    assert len(brief.splitlines()) <= 12
    assert "src layout" in brief
    assert "closeout_ready=" in brief
    assert "kr_python_exec.py" in brief
    assert "kr_closeout_router.py" in brief
    assert "report-minimal" in brief


def test_closeout_router_recommends_report_minimal_for_report_artifacts(tmp_path: Path) -> None:
    report = ROOT / "docs" / "reference" / "demo-report.md"
    evidence = ROOT / "docs" / "reference" / "demo-report.evidence.json"
    evidence.write_text(
        json.dumps(
            {
                "schema": "knowledgeradar-research-evidence/v1",
                "artifact": {"report_path": "docs/reference/demo-report.md"},
                "process_quality": {"validation_ladder": {"selected_profile": "report_light"}},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    try:
        result = route_closeout(
            changed_paths=[
                "docs/reference/demo-report.evidence.json",
                "docs/reference/demo-report.preflight.json",
            ],
            report=report,
            evidence=evidence,
        )
    finally:
        evidence.unlink(missing_ok=True)

    assert result["recommended_profile"] == "report_minimal"
    assert result["side_effects"]["runs_pytest"] is False
    assert result["side_effects"]["writes_quality_state"] is False
    assert result["side_effects"]["auto_commits"] is False
    assert result["side_effects"]["restarts_mcp"] is False
    assert any("kr_report_minimal_check.py" in command for command in result["commands"])
    assert not any("--write-state" in command for command in result["commands"])
    assert not any("--no-auto-commit" in command for command in result["commands"])
    assert not any("--no-prepare-artifacts" in command for command in result["commands"])
    assert not any("research_preflight.py" in command for command in result["commands"])


def test_closeout_router_report_sidecar_does_not_hide_code_changes(tmp_path: Path) -> None:
    root = tmp_path
    report = root / "docs" / "reference" / "demo-report.md"
    evidence = root / "docs" / "reference" / "demo-report.evidence.json"
    report.parent.mkdir(parents=True)
    report.write_text("# Demo\n", encoding="utf-8")
    evidence.write_text(
        json.dumps(
            {
                "schema": "knowledgeradar-research-evidence/v1",
                "task_type": "report",
                "validation_profile": "report_light",
                "artifact": {"report_path": "docs/reference/demo-report.md"},
                "change_boundary": {
                    "code_changes": False,
                    "allowed_paths": [
                        "docs/reference/demo-report.md",
                        "docs/reference/demo-report.evidence.json",
                    ],
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = route_closeout(
        changed_paths=[
            "src/server.py",
            "tests/test_quality_gate_contracts.py",
            "docs/reference/demo-report.evidence.json",
        ],
        report=report,
        evidence=evidence,
        root=root,
    )

    assert result["recommended_profile"] == "runtime_boundary"
    assert result["task_type"] == "report"
    assert "src/server.py" in result["changed_paths"]
    assert "src/server.py" in result["profile_decision_paths"]
    assert result["side_effects"]["runs_pytest"] is True
    assert result["side_effects"]["writes_quality_state"] is True
    assert ".\\.python312\\python.exe scripts\\generate_docs_index.py" in result["commands"]
    assert ".\\.python312\\python.exe scripts\\generate_script_lifecycle_inventory.py" in result["commands"]
    assert ".\\.python312\\python.exe scripts\\generate_docs_index.py --check --json" in result["commands"]
    assert ".\\.python312\\python.exe scripts\\generate_script_lifecycle_inventory.py --check --json" in result["commands"]


def test_closeout_router_keeps_doc_indexes_on_report_minimal_path() -> None:
    result = route_closeout(
        changed_paths=[
            "docs/reference/demo-report.evidence.json",
            "docs/doc-index.json",
        ],
        report=ROOT / "docs" / "reference" / "demo-report.md",
        evidence=ROOT / "docs" / "reference" / "demo-report.evidence.json",
    )

    assert result["recommended_profile"] == "report_minimal"
    assert result["side_effects"]["runs_pytest"] is False
    assert result["side_effects"]["writes_quality_state"] is False
    assert any("kr_report_minimal_check.py" in command for command in result["commands"])
    assert not any("--report-light" in command for command in result["commands"])
    assert not any("generate_docs_index.py" in command for command in result["commands"])
    assert not any("generate_script_lifecycle_inventory.py" in command for command in result["commands"])
    assert not any("check_docs_governance.py" in command for command in result["commands"])
    assert result["deferred_commands"] == []


def test_closeout_router_still_keeps_explicit_docs_report_on_minimal_path() -> None:
    result = route_closeout(
        changed_paths=[
            "docs/reference/demo-report.evidence.json",
            "docs/doc-index.json",
        ],
        report=ROOT / "docs" / "reference" / "demo-report.md",
        evidence=ROOT / "docs" / "reference" / "demo-report.evidence.json",
        task_type="docs_governance",
    )

    assert result["recommended_profile"] == "report_minimal"
    assert any("kr_report_minimal_check.py" in command for command in result["commands"])
    assert not any("--report-light" in command for command in result["commands"])
    assert not any("generate_docs_index.py" in command for command in result["commands"])
    assert not any("generate_script_lifecycle_inventory.py" in command for command in result["commands"])
    assert not any("check_docs_governance.py" in command for command in result["commands"])
    assert result["deferred_commands"] == []
    assert result["side_effects"]["writes_quality_state"] is False


def test_closeout_router_auto_detects_report_pair_from_workspace(tmp_path: Path) -> None:
    root = tmp_path
    report = root / "docs" / "reference" / "demo-report.md"
    evidence = root / "docs" / "reference" / "demo-report.evidence.json"
    report.parent.mkdir(parents=True)
    report.write_text("# Demo\n", encoding="utf-8")
    evidence.write_text(
        json.dumps(
            {
                "schema": "knowledgeradar-research-evidence/v1",
                "task_type": "report",
                "validation_profile": "report_light_plus_docs_governance",
                "artifact": {"report_path": "docs/reference/demo-report.md"},
                "change_boundary": {
                    "code_changes": False,
                    "allowed_paths": [
                        "docs/reference/demo-report.md",
                        "docs/reference/demo-report.evidence.json",
                        "docs/script-lifecycle-inventory.json",
                        "docs/脚本生命周期清单.md",
                        "project_status/Quality-Gate-State.json",
                    ],
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = route_closeout(
        changed_paths=[
            "docs/reference/demo-report.md",
            "docs/reference/demo-report.evidence.json",
            "docs/script-lifecycle-inventory.json",
            "docs/脚本生命周期清单.md",
            "project_status/Quality-Gate-State.json",
        ],
        root=root,
    )

    assert result["recommended_profile"] == "report_minimal"
    assert result["report"] == "docs/reference/demo-report.md"
    assert result["evidence"] == "docs/reference/demo-report.evidence.json"
    assert result["side_effects"]["writes_quality_state"] is False
    assert result["side_effects"]["auto_commits"] is False
    assert result["deferred_commands"] == []


def test_closeout_router_quality_state_only_is_not_docs_governance() -> None:
    result = route_closeout(changed_paths=["project_status/Quality-Gate-State.json"])

    assert result["recommended_profile"] == "quality_state_only"
    assert result["commands"] == []
    assert result["side_effects"]["runs_pytest"] is False
    assert result["side_effects"]["writes_quality_state"] is False
    assert not any("check_docs_governance.py" in command for command in result["commands"])


def test_closeout_router_code_plus_stable_report_includes_docs_inventory_closeout(tmp_path: Path) -> None:
    root = tmp_path
    report = root / "docs" / "reference" / "stable-report.md"
    evidence = root / "docs" / "reference" / "stable-report.evidence.json"
    report.parent.mkdir(parents=True)
    report.write_text("# Stable Report\n", encoding="utf-8")
    evidence.write_text(
        json.dumps(
            {
                "schema": "knowledgeradar-research-evidence/v1",
                "artifact": {"report_path": "docs/reference/stable-report.md"},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = route_closeout(
        changed_paths=[
            "scripts/example.py",
            "docs/reference/stable-report.md",
            "docs/reference/stable-report.evidence.json",
        ],
        root=root,
    )

    assert result["recommended_profile"] == "code_changed"
    assert ".\\.python312\\python.exe scripts\\generate_docs_index.py" in result["commands"]
    assert ".\\.python312\\python.exe scripts\\generate_script_lifecycle_inventory.py" in result["commands"]
    assert ".\\.python312\\python.exe scripts\\generate_docs_index.py --check --json" in result["commands"]
    assert ".\\.python312\\python.exe scripts\\generate_script_lifecycle_inventory.py --check --json" in result["commands"]
    assert any("check_docs_governance.py" in command for command in result["commands"])
    assert any("kr_quality_gate.py --changed --json --write-state" in command for command in result["commands"])


def test_closeout_router_defaults_process_report_to_draft_report() -> None:
    result = route_closeout(
        changed_paths=["docs/reference/招聘平台列表抓取失败根因与系统治理报告-2026-07-06.md"]
    )

    assert result["recommended_profile"] == "draft_report"
    assert result["selected_bundle"] == "report-minimal"
    assert result["validation_registry"] == "config/validation-checks.manifest.json"
    assert result["side_effects"]["writes_quality_state"] is False
    assert result["side_effects"]["auto_commits"] is False
    assert result["side_effects"]["restarts_mcp"] is False
    assert any("kr_report_minimal_check.py" in command for command in result["commands"])


def test_closeout_router_keeps_report_split_on_minimal_path(tmp_path: Path) -> None:
    report = tmp_path / "docs" / "reference" / "split-report.md"
    evidence = tmp_path / "docs" / "reference" / "split-report.evidence.json"
    report.parent.mkdir(parents=True)
    report.write_text("# Split Report\n", encoding="utf-8")
    evidence.write_text(
        json.dumps(
            {
                "schema": "knowledgeradar-research-evidence/v1",
                "task_type": "report_split",
                "validation_profile": "report_minimal",
                "artifact": {
                    "report_path": "docs/reference/split-report.md",
                    "evidence_path": "docs/reference/split-report.evidence.json",
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = route_closeout(
        changed_paths=["docs/reference/split-report.md", "docs/reference/split-report.evidence.json"],
        report=report,
        evidence=evidence,
        root=tmp_path,
    )

    assert result["recommended_profile"] == "report_minimal"
    assert result["side_effects"]["runs_pytest"] is False
    assert result["side_effects"]["writes_quality_state"] is False
    assert result["side_effects"]["auto_commits"] is False
    assert result["deferred_commands"] == []
    assert any("kr_report_minimal_check.py" in command for command in result["commands"])
    assert not any("kr_quality_gate.py" in command for command in result["commands"])


def test_report_minimal_check_is_side_effect_free(tmp_path: Path) -> None:
    report = tmp_path / "demo-report.md"
    report.write_text("# Demo\n\n## Root Cause\n", encoding="utf-8")

    result = run_minimal_report_check(report=report, keywords=["Root Cause"])

    assert result["status"] == "PASS"
    assert result["selected_bundle"] == "report-minimal"
    assert result["side_effects"]["writes_report_artifacts"] is False
    assert result["side_effects"]["writes_quality_state"] is False
    assert result["side_effects"]["auto_commits"] is False


def test_closeout_router_escalates_runtime_boundary() -> None:
    result = route_closeout(changed_paths=["src/server.py", "docs/reference/demo.evidence.json"])

    assert result["recommended_profile"] == "runtime_boundary"
    assert result["side_effects"]["runs_pytest"] is True
    assert result["side_effects"]["writes_quality_state"] is True
    assert result["side_effects"]["auto_commits"] is True
    assert any("kr_quality_gate.py --changed --json --write-state" in command for command in result["commands"])


def test_report_light_gate_can_skip_artifact_prepare(monkeypatch, tmp_path: Path) -> None:
    report = tmp_path / "report.md"
    evidence = tmp_path / "report.evidence.json"
    report.write_text("# Report", encoding="utf-8")
    evidence.write_text("{}", encoding="utf-8")

    import scripts.kr_report_light_gate as report_light

    commands: list[list[str]] = []

    def fake_run(command: list[str]) -> dict:
        commands.append(command)
        return {"command": " ".join(command), "returncode": 0, "status": "PASS", "stdout": '{"status":"PASS"}', "stderr": ""}

    monkeypatch.setattr(report_light, "_run", fake_run)

    result = run_report_light_gate(report=report, evidence=evidence, prepare_artifacts=False)

    assert result["status"] == "PASS"
    assert not any("--update-evidence" in command for command in commands)
    assert not any("normalize evidence sidecar" == item["command"] for item in result["checks"])
    assert result["side_effects"] == {
        "runs_pytest": False,
        "runs_ruff": False,
        "writes_quality_state": False,
        "auto_commits": False,
        "restarts_mcp": False,
        "writes_report_artifacts": False,
    }


def test_report_light_gate_cli_help_direct_execution() -> None:
    proc = subprocess.run(
        [sys.executable, "scripts/kr_report_light_gate.py", "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    assert "--report" in proc.stdout


def test_report_light_gate_prepares_evidence_before_check(monkeypatch, tmp_path: Path) -> None:
    report = tmp_path / "report.md"
    evidence = tmp_path / "report.evidence.json"
    report.write_text(
        """# 治理报告

## 详细执行步骤

### Step 1 / P0：Inspect
### Step 2 / P1：Repair
### Step 3 / P2：Verify

```mermaid
flowchart TD
  P0 --> P1
  P1 --> P2
```
""",
        encoding="utf-8",
    )
    evidence.write_text("{}", encoding="utf-8")

    import scripts.kr_report_light_gate as report_light

    def fake_run(command: list[str]) -> dict:
        return {"command": " ".join(command), "returncode": 0, "status": "PASS", "stdout": '{"status":"pass"}', "stderr": ""}

    monkeypatch.setattr(report_light, "_run", fake_run)

    result = run_report_light_gate(report=report, evidence=evidence, prepare_artifacts=True)
    payload = json.loads(evidence.read_text(encoding="utf-8"))

    assert result["status"] == "PASS"
    assert any(item["command"] == "normalize evidence sidecar" for item in result["checks"])
    assert payload["task_type"] == "report"
    assert payload["process_quality"]["schema"] == "knowledgeradar-process-quality/v1"
    assert payload["questioner_checkpoints"] == []
    assert len(payload["roadmap"]) == 3
    assert result["side_effects"]["writes_report_artifacts"] is True


def test_report_light_gate_materialize_updates_evidence(monkeypatch, tmp_path: Path) -> None:
    report = tmp_path / "report.md"
    evidence = tmp_path / "report.evidence.json"
    report.write_text("# Report", encoding="utf-8")
    evidence.write_text("{}", encoding="utf-8")

    import scripts.kr_report_light_gate as report_light

    commands: list[list[str]] = []

    def fake_run(command: list[str]) -> dict:
        commands.append(command)
        return {"command": " ".join(command), "returncode": 0, "status": "PASS", "stdout": '{"status":"PASS"}', "stderr": ""}

    monkeypatch.setattr(report_light, "_run", fake_run)

    result = run_report_light_gate(report=report, evidence=evidence, materialize=True)

    assert result["status"] == "PASS"
    assert any("--update-evidence" in command for command in commands)


def test_quality_gate_report_light_mode_bypasses_changed_gate(monkeypatch, tmp_path: Path, capsys) -> None:
    report = tmp_path / "report.md"
    evidence = tmp_path / "report.evidence.json"
    report.write_text("# Report", encoding="utf-8")
    evidence.write_text("{}", encoding="utf-8")

    import scripts.kr_quality_gate as quality_gate

    def fake_report_light_gate(
        *,
        report: Path,
        evidence: Path,
        preflight: Path | None = None,
        materialize: bool = False,
        prepare_artifacts: bool = True,
    ) -> dict:
        return {
            "schema": "knowledgeradar-report-light-gate/v1",
            "status": "PASS",
            "report": str(report),
            "evidence": str(evidence),
            "preflight": str(preflight or ""),
            "materialized": materialize,
            "prepared": prepare_artifacts,
            "checks": [],
            "side_effects": {
                "runs_pytest": False,
                "runs_ruff": False,
                "writes_quality_state": False,
                "auto_commits": False,
                "restarts_mcp": False,
                "writes_report_artifacts": prepare_artifacts,
            },
        }

    monkeypatch.setattr(quality_gate, "run_report_light_gate", fake_report_light_gate)
    monkeypatch.setattr(quality_gate, "_run_changed", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("changed gate should not run")))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "kr_quality_gate.py",
            "--report-light",
            "--report",
            str(report),
            "--evidence",
            str(evidence),
            "--json",
        ],
    )

    assert quality_gate_main() == 0
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "PASS"
    assert output["side_effects"]["runs_pytest"] is False
    assert output["prepared"] is False


def test_quality_gate_report_light_rejects_write_state(monkeypatch, tmp_path: Path) -> None:
    report = tmp_path / "report.md"
    evidence = tmp_path / "report.evidence.json"
    report.write_text("# Report", encoding="utf-8")
    evidence.write_text("{}", encoding="utf-8")

    import scripts.kr_quality_gate as quality_gate

    def fake_report_light_gate(
        *,
        report: Path,
        evidence: Path,
        preflight: Path | None = None,
        materialize: bool = False,
        prepare_artifacts: bool = True,
    ) -> dict:
        return {
            "schema": "knowledgeradar-report-light-gate/v1",
            "status": "PASS",
            "report": str(report),
            "evidence": str(evidence),
            "preflight": str(preflight or ""),
            "materialized": materialize,
            "prepared": prepare_artifacts,
            "checks": [{"command": "report", "returncode": 0, "status": "PASS", "stdout": "", "stderr": ""}],
            "side_effects": {
                "runs_pytest": False,
                "runs_ruff": False,
                "writes_quality_state": False,
                "auto_commits": False,
                "restarts_mcp": False,
                "writes_report_artifacts": prepare_artifacts,
            },
        }

    monkeypatch.setattr(quality_gate, "run_report_light_gate", fake_report_light_gate)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "kr_quality_gate.py",
            "--report-light",
            "--report",
            str(report),
            "--evidence",
            str(evidence),
            "--json",
            "--write-state",
        ],
    )

    with pytest.raises(SystemExit) as exc:
        quality_gate_main()
    assert exc.value.code == 2


def test_pre_commit_router_report_only_skips_project_status_prepare(monkeypatch, tmp_path: Path) -> None:
    import scripts.kr_pre_commit_router as pre_commit_router

    report = tmp_path / "docs" / "reference" / "demo.md"
    evidence = tmp_path / "docs" / "reference" / "demo.evidence.json"
    report.parent.mkdir(parents=True)
    report.write_text("# Demo\n", encoding="utf-8")
    evidence.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(pre_commit_router, "staged_paths", lambda: ["docs/reference/demo.md", "docs/reference/demo.evidence.json"])
    monkeypatch.setattr(pre_commit_router, "_find_report_pair", lambda _paths: (report, evidence))
    monkeypatch.setattr(pre_commit_router, "_python", lambda: "python")

    commands: list[list[str]] = []

    def fake_run(command: list[str], *, timeout: int = 240) -> dict:
        commands.append(command)
        return {"command": " ".join(command), "returncode": 0, "status": "PASS", "stdout": "", "stderr": ""}

    monkeypatch.setattr(pre_commit_router, "_run", fake_run)

    result = pre_commit_router.run_pre_commit_router(execute=True)

    assert result["status"] == "PASS"
    assert result["route"]["recommended_profile"] == "report_minimal"
    assert commands
    assert not any("kr_pre_commit_prepare.py" in command for command in commands)
    assert any(any(item.endswith("kr_report_minimal_check.py") for item in command) for command in commands)
    assert not any(
        any(item.endswith("kr_quality_gate.py") for item in command) and "--report-light" in command for command in commands
    )
    assert not any("--no-prepare-artifacts" in item for command in commands for item in command)
    assert not any("--no-auto-commit" in item for command in commands for item in command)
    assert not any("--write-state" in command for command in commands)


def test_pre_commit_router_report_split_uses_minimal_check(monkeypatch, tmp_path: Path) -> None:
    import scripts.kr_pre_commit_router as pre_commit_router

    report = tmp_path / "docs" / "reference" / "split-report.md"
    evidence = tmp_path / "docs" / "reference" / "split-report.evidence.json"
    report.parent.mkdir(parents=True)
    report.write_text("# Split Report\n", encoding="utf-8")
    evidence.write_text(
        json.dumps(
            {
                "schema": "knowledgeradar-research-evidence/v1",
                "task_type": "report_split",
                "validation_profile": "report_minimal",
                "artifact": {
                    "report_path": "docs/reference/split-report.md",
                    "evidence_path": "docs/reference/split-report.evidence.json",
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(pre_commit_router, "staged_paths", lambda: ["docs/reference/split-report.md", "docs/reference/split-report.evidence.json"])
    monkeypatch.setattr(pre_commit_router, "_find_report_pair", lambda _paths: (report, evidence))
    monkeypatch.setattr(pre_commit_router, "_python", lambda: "python")

    commands: list[list[str]] = []

    def fake_run(command: list[str], *, timeout: int = 240) -> dict:
        commands.append(command)
        return {"command": " ".join(command), "returncode": 0, "status": "PASS", "stdout": "", "stderr": ""}

    monkeypatch.setattr(pre_commit_router, "_run", fake_run)

    result = pre_commit_router.run_pre_commit_router(execute=True)

    assert result["status"] == "PASS"
    assert result["route"]["recommended_profile"] == "report_minimal"
    assert commands
    assert any(any(item.endswith("kr_report_minimal_check.py") for item in command) for command in commands)
    assert not any(any(item.endswith("kr_quality_gate.py") for item in command) for command in commands)
    assert not any("kr_pre_commit_prepare.py" in command for command in commands)


def test_kr_python_exec_handles_powershell_text_and_kr_imports() -> None:
    code = "\ufefffrom academic_providers.service import academic_provider_status\nprint('socolar' in academic_provider_status())\n"
    proc = subprocess.run(
        [sys.executable, "scripts/kr_python_exec.py", "--stdin"],
        input=code.encode("utf-8"),
        cwd=ROOT,
        capture_output=True,
        timeout=15,
    )

    assert proc.returncode == 0, proc.stderr.decode("utf-8", errors="replace")
    assert proc.stdout.decode("utf-8", errors="replace").strip() == "True"


def test_changed_gate_uses_selector_targets_when_confident() -> None:
    payload = {
        "schema": "knowledgeradar-test-selector/v1",
        "confidence": "high",
        "fallback_required": False,
        "selected_tests": [{"path": "tests/test_academic_provider_profiles.py"}],
    }

    targets, source = _selector_payload_to_changed_tests(payload)

    assert source == "selector"
    assert targets == _merge_test_targets(CORE_CHANGED_TESTS, ["tests/test_academic_provider_profiles.py"])


def test_changed_gate_falls_back_when_selector_is_uncertain() -> None:
    payload = {
        "schema": "knowledgeradar-test-selector/v1",
        "confidence": "low",
        "fallback_required": True,
        "selected_tests": [],
    }

    targets, source = _selector_payload_to_changed_tests(payload)

    assert source == "fallback"
    assert targets == _merge_test_targets(CORE_CHANGED_TESTS, FALLBACK_CHANGED_TESTS)


def test_changed_gate_skips_runtime_coverage_for_non_runtime_changes() -> None:
    assert _needs_runtime_coverage(["src/academic_providers/planner.py"]) is False
    assert _needs_runtime_coverage(["src/runtime/quality_gates.py"]) is True
    assert _needs_runtime_coverage(["scripts/kr_quality_gate.py"]) is True


def test_fast_gate_redaction_checks_project_status_only(monkeypatch) -> None:
    import scripts.kr_quality_gate as quality_gate

    commands: list[list[str]] = []

    monkeypatch.setattr(quality_gate, "_project_state_freshness_check", lambda allow_auto_prepare=False: {"status": "pass", "command": "fresh"})
    monkeypatch.setattr(quality_gate, "_prepare_project_state_for_gate", lambda stage=False: {"status": "pass", "command": "prepare"})
    monkeypatch.setattr(quality_gate, "_internal_checks", lambda: [])

    def fake_run_command(command: list[str], *_args, **_kwargs) -> dict:
        commands.append(command)
        return {"status": "pass", "command": " ".join(command), "returncode": 0, "stdout": "", "stderr": ""}

    monkeypatch.setattr(quality_gate, "run_command", fake_run_command)

    checks = _run_fast()

    assert all(check["status"] == "pass" for check in checks)
    redaction = next(command for command in commands if any("kr_redact_report_paths.py" in part for part in command))
    assert "project_status" in redaction
    assert not any(part.startswith("docs/") for part in redaction)
    assert "KnowledgeRadar-Bug-Closeout-And-Quality-Governance-Plan-2026-06-09.md" not in redaction
    assert any(any("check_mcp_tool_schema.py" in part for part in command) for command in commands)


def test_fast_gate_prepares_project_state_before_freshness_check(monkeypatch) -> None:
    import scripts.kr_quality_gate as quality_gate

    calls: list[str] = []

    monkeypatch.setattr(quality_gate, "run_command", lambda *args, **kwargs: {"status": "pass", "command": "run", "returncode": 0})
    monkeypatch.setattr(quality_gate, "_internal_checks", lambda: [])
    monkeypatch.setattr(quality_gate, "_prepare_project_state_for_gate", lambda stage=False: calls.append("prepare") or {"status": "pass", "command": "prepare"})
    monkeypatch.setattr(
        quality_gate,
        "_project_state_freshness_check",
        lambda allow_auto_prepare=False: calls.append("freshness") or {"status": "pass", "command": "freshness"},
    )

    checks = _run_fast()

    assert calls[:2] == ["prepare", "freshness"]
    assert [check["command"] for check in checks[:3]] == ["prepare", "run", "freshness"]


def test_prepare_state_command_runs_without_gate(monkeypatch) -> None:
    import scripts.kr_quality_gate as quality_gate

    calls: list[bool] = []

    monkeypatch.setattr(sys, "argv", ["kr_quality_gate.py", "--prepare-state", "--json"])
    monkeypatch.setattr(
        quality_gate,
        "_prepare_project_state_for_gate",
        lambda stage=False: calls.append(stage)
        or {
            "schema": "knowledgeradar-quality-gate-project-state-prepare/v1",
            "status": "pass",
            "command": "prepare",
            "returncode": 0,
            "stdout": '{"status":"PASS"}',
            "stderr": "",
        },
    )
    monkeypatch.setattr(quality_gate, "_run_fast", lambda: (_ for _ in ()).throw(AssertionError("gate should not run")))

    assert quality_gate_main() == 0
    assert calls == [False]


def test_project_state_auto_prepare_is_limited_to_effective_head_match() -> None:
    assert _can_auto_prepare_project_state({"checks": {"head_effectively_matches": True}}) is True
    assert _can_auto_prepare_project_state({"checks": {"status_only_head_change": True}}) is False
    assert _can_auto_prepare_project_state({"checks": {"head_effectively_matches": False}}) is False


def test_project_state_freshness_check_auto_prepares_once(monkeypatch) -> None:
    import scripts.kr_quality_gate as quality_gate

    calls: list[str] = []

    def fake_run_command(command: list[str], _root: Path, *, timeout: int = 120) -> dict:
        joined = " ".join(command)
        calls.append(joined)
        if "kr_project_state.py" in joined and len(calls) == 1:
            return {
                "command": joined,
                "returncode": 1,
                "status": "fail",
                "stdout": '{"checks":{"head_effectively_matches":true}}',
                "stderr": "",
            }
        return {"command": joined, "returncode": 0, "status": "pass", "stdout": "{}", "stderr": ""}

    monkeypatch.setattr(quality_gate, "run_command", fake_run_command)

    result = _project_state_freshness_check(allow_auto_prepare=True)

    assert result["status"] == "pass"
    assert result["command"] == "project_state freshness with auto prepare"
    assert result["auto_prepare"]["status"] == "pass"
    assert len(calls) == 3


def test_project_state_freshness_check_auto_prepares_truncated_json(monkeypatch) -> None:
    import scripts.kr_quality_gate as quality_gate

    calls: list[str] = []

    def fake_run_command(command: list[str], _root: Path, *, timeout: int = 120) -> dict:
        joined = " ".join(command)
        calls.append(joined)
        if "kr_project_state.py" in joined and len(calls) == 1:
            return {
                "command": joined,
                "returncode": 1,
                "status": "fail",
                "stdout": '    "head_effectively_matches": true,\n    "dirty_effectively_matches": false\n  }\n}',
                "stderr": "",
            }
        return {"command": joined, "returncode": 0, "status": "pass", "stdout": "{}", "stderr": ""}

    monkeypatch.setattr(quality_gate, "run_command", fake_run_command)

    result = _project_state_freshness_check(allow_auto_prepare=True)

    assert result["status"] == "pass"
    assert result["auto_prepare"]["status"] == "pass"
    assert len(calls) == 3


def test_quality_gate_prepare_check_does_not_refresh_project_status(monkeypatch) -> None:
    import scripts.kr_quality_gate as quality_gate

    calls = []

    def fake_prepare(root: Path, *, stage: bool = False, refresh_project_status: bool = True) -> dict:
        calls.append((root, stage, refresh_project_status))
        return {"status": "PASS", "project_status_refresh": "skipped_by_caller"}

    monkeypatch.setattr(quality_gate, "prepare_commit", fake_prepare)
    monkeypatch.setattr(quality_gate, "_staged_project_status_snapshot_exists", lambda: True)

    result = quality_gate._prepare_project_state_for_gate(stage=False)

    assert result["status"] == "pass"
    assert calls == [(ROOT, False, False)]


def test_quality_gate_prepare_check_refreshes_project_status_when_no_staged_snapshot(monkeypatch) -> None:
    import scripts.kr_quality_gate as quality_gate

    calls = []

    def fake_prepare(root: Path, *, stage: bool = False, refresh_project_status: bool = True) -> dict:
        calls.append((root, stage, refresh_project_status))
        return {"status": "PASS", "project_status_refresh": "written"}

    monkeypatch.setattr(quality_gate, "prepare_commit", fake_prepare)
    monkeypatch.setattr(quality_gate, "_staged_project_status_snapshot_exists", lambda: False)

    result = quality_gate._prepare_project_state_for_gate(stage=False)

    assert result["status"] == "pass"
    assert calls == [(ROOT, False, True)]


def test_write_state_pass_gate_triggers_auto_commit(monkeypatch) -> None:
    import scripts.kr_quality_gate as quality_gate

    calls: list[Path] = []

    monkeypatch.setattr(sys, "argv", ["kr_quality_gate.py", "--fast", "--json", "--write-state"])
    monkeypatch.setattr(quality_gate, "_run_fast", lambda: [{"status": "pass", "command": "ok", "returncode": 0}])
    monkeypatch.setattr(
        quality_gate,
        "write_quality_state",
        lambda summary, root: {"schema": "knowledgeradar-quality-state/v1", "status_class": summary["status"]},
    )

    def fake_auto_commit(root: Path) -> dict:
        calls.append(root)
        return {"schema": "knowledgeradar-auto-commit/v1", "status": "PASS", "action": "committed", "commit": "abc123"}

    monkeypatch.setattr(quality_gate, "auto_commit_verified_changes", fake_auto_commit)

    assert quality_gate_main() == 0
    assert calls == [ROOT]


def test_write_state_failed_gate_does_not_auto_commit(monkeypatch) -> None:
    import scripts.kr_quality_gate as quality_gate

    calls: list[Path] = []

    monkeypatch.setattr(sys, "argv", ["kr_quality_gate.py", "--fast", "--json", "--write-state"])
    monkeypatch.setattr(quality_gate, "_run_fast", lambda: [{"status": "fail", "command": "bad", "returncode": 1}])
    monkeypatch.setattr(
        quality_gate,
        "write_quality_state",
        lambda summary, root: {"schema": "knowledgeradar-quality-state/v1", "status_class": summary["status"]},
    )
    monkeypatch.setattr(
        quality_gate,
        "auto_commit_verified_changes",
        lambda root: calls.append(root) or {"schema": "knowledgeradar-auto-commit/v1", "status": "PASS", "action": "noop"},
    )

    assert quality_gate_main() == 1
    assert calls == []


def test_write_state_no_auto_commit_flag_is_ignored(monkeypatch) -> None:
    import scripts.kr_quality_gate as quality_gate

    calls: list[Path] = []

    monkeypatch.setattr(sys, "argv", ["kr_quality_gate.py", "--fast", "--json", "--write-state", "--no-auto-commit"])
    monkeypatch.setattr(quality_gate, "_run_fast", lambda: [{"status": "pass", "command": "ok", "returncode": 0}])
    monkeypatch.setattr(
        quality_gate,
        "write_quality_state",
        lambda summary, root: {"schema": "knowledgeradar-quality-state/v1", "status_class": summary["status"]},
    )
    monkeypatch.setattr(
        quality_gate,
        "auto_commit_verified_changes",
        lambda root: calls.append(root) or {"schema": "knowledgeradar-auto-commit/v1", "status": "PASS", "action": "noop"},
    )

    assert quality_gate_main() == 0
    assert calls == [ROOT]


def test_write_state_auto_commit_failure_fails_gate(monkeypatch) -> None:
    import scripts.kr_quality_gate as quality_gate

    monkeypatch.setattr(sys, "argv", ["kr_quality_gate.py", "--fast", "--json", "--write-state"])
    monkeypatch.setattr(quality_gate, "_run_fast", lambda: [{"status": "pass", "command": "ok", "returncode": 0}])
    monkeypatch.setattr(
        quality_gate,
        "write_quality_state",
        lambda summary, root: {"schema": "knowledgeradar-quality-state/v1", "status_class": summary["status"]},
    )
    monkeypatch.setattr(
        quality_gate,
        "auto_commit_verified_changes",
        lambda root: {"schema": "knowledgeradar-auto-commit/v1", "status": "FAIL", "error": "commit failed"},
    )

    assert quality_gate_main() == 1


def test_runtime_boundary_scan_shape() -> None:
    result = scan_forbidden_source_outputs()

    assert result["schema"] == "knowledgeradar-runtime-boundary-scan/v1"
    assert isinstance(result["hits"], list)
    assert result["status"] in {"ok", "fail"}


def test_provider_status_contract_requires_reason_for_degraded() -> None:
    ok = validate_provider_status_contract(
        {
            "openalex": {"available": True, "configured": True},
            "serpapi": {"configured": False, "status": "degraded", "role": "quota_limited_fallback"},
        }
    )
    bad = validate_provider_status_contract({"mystery": {"status": "degraded"}})

    assert ok["status"] == "ok"
    assert bad["status"] == "fail"
    assert bad["violations"][0]["reason"] == "missing_available_or_configured"


def test_campaign_profiles_cover_smoke_deep_destructive_and_agent_sentinel() -> None:
    manifest = campaign_profile_manifest()

    assert manifest["default_profile"] == "smoke"
    assert set(manifest["profiles"]) == {"smoke", "deep", "destructive", "agent-sentinel"}


def test_campaign_status_classification_separates_degraded_manual_and_fail() -> None:
    assert classify_campaign_status({"status": "degraded", "degraded_reason": "quota"})["classification"] == "EXPECTED_DEGRADED"
    assert classify_campaign_status({"status": "needs_interaction", "manual_action": "login"})["classification"] == "NEEDS_INTERACTION"
    assert classify_campaign_status({"status": "error", "main_chain": True})["classification"] == "FAIL"


def test_campaign_fault_injection_expected_classifications() -> None:
    result = run_campaign_fault_injection()

    assert result["status"] == "pass"
    assert not result["violations"]


def test_agent_sentinel_contract_is_lightweight_and_forbids_builtin_search() -> None:
    contract = run_agent_sentinel_contract()

    assert contract["status"] == "pass"
    assert contract["token_budget"]["target_total_tokens"] <= 20000
    assert "built-in web_search" in contract["forbidden_tools"]


def test_browser_channel_summary_reports_orphan_records() -> None:
    summary = browser_channel_summary(
        [
            {
                "platform": "xhs",
                "profile_id": "xhs-a",
                "channel_id": "playwright_cdp",
                "browser_base": "chrome",
                "launch_policy": "managed",
            },
            {
                "platform": "academic",
                "profile_id": "manual-a",
                "browser_base": "chrome",
            },
        ]
    )

    assert summary["status"] == "degraded"
    assert summary["counts"]["orphan_browser_records"] == 1
    assert summary["ownership"]["orphan_records"][0]["missing"] == ["channel_id", "launch_policy"]


def test_quality_state_write_and_freshness(tmp_path: Path) -> None:
    summary = {
        "schema": "knowledgeradar-quality-gate-result/v1",
        "mode": "fast",
        "status": "PASS",
        "total": 1,
        "failed": 0,
        "results": [{"status": "pass", "command": "noop"}],
    }

    state = write_quality_state(summary, tmp_path)
    freshness = quality_state_freshness(tmp_path)

    assert state["schema"] == QUALITY_STATE_SCHEMA
    assert quality_state_path(tmp_path).is_file()
    assert freshness["fresh"] is True
    assert freshness["status_class"] == "PASS"


def test_quality_state_stale_text_does_not_look_like_manual_action(tmp_path: Path) -> None:
    summary = {
        "schema": "knowledgeradar-quality-gate-result/v1",
        "mode": "campaign",
        "status": "FAIL",
        "total": 1,
        "failed": 1,
        "results": [
            {
                "status": "fail",
                "command": "project_status freshness",
                "stdout": '{"status":"stale","recommended_next_command":"manual_action docs mention"}',
            }
        ],
    }

    state = write_quality_state(summary, tmp_path)

    assert state["status_class"] == "FAIL"


def test_quality_snapshot_ignores_own_state_file(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        quality_state_module,
        "git_changed_paths",
        lambda *_args: ["project_status/Quality-Gate-State.json", "src/runtime/quality_state.py"],
    )
    monkeypatch.setattr(
        quality_state_module,
        "_git_output",
        lambda _root, args: "\n".join(
            [
                "M\tproject_status/Quality-Gate-State.json",
                "M\tsrc/runtime/quality_state.py",
            ]
        )
        if args == ["diff", "--name-status", "HEAD"]
        else "project_status/Quality-Gate-State.json",
    )

    snapshot = quality_snapshot(tmp_path)

    assert snapshot["dirty_count"] == 1
    assert snapshot["changed_paths"] == ["src/runtime/quality_state.py"]


def test_quality_freshness_accepts_committed_verified_snapshot(tmp_path: Path, monkeypatch) -> None:
    path = quality_state_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(
        """{
  "schema": "knowledgeradar-quality-state/v1",
  "schema_version": 1,
  "status_class": "PASS",
  "recommended_next_command": "none",
  "snapshot": {
    "git_head": "old1234",
    "dirty_count": 1,
    "changed_paths": ["src/example.py"],
    "changed_paths_hash": "old-hash",
    "dirty_hash": "old-dirty"
  }
}""",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        quality_state_module,
        "quality_snapshot",
        lambda _root: {
            "git_head": "new1234",
            "dirty_count": 0,
            "changed_paths": [],
            "changed_paths_hash": "new-hash",
            "dirty_hash": "new-dirty",
        },
    )
    monkeypatch.setattr(
        quality_state_module,
        "_git_output",
        lambda _root, args: "src/example.py\nproject_status/Quality-Gate-State.json"
        if args == ["diff", "--name-only", "old1234", "new1234"]
        else "",
    )

    freshness = quality_state_freshness(tmp_path)

    assert freshness["fresh"] is True
    assert freshness["status_class"] == "PASS"
    assert freshness["checks"]["committed_snapshot_matches"] is True


def test_quality_freshness_accepts_committed_quality_state_snapshot_without_status_only_flag(
    tmp_path: Path, monkeypatch
) -> None:
    path = quality_state_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(
        """{
  "schema": "knowledgeradar-quality-state/v1",
  "schema_version": 1,
  "status_class": "PASS",
  "recommended_next_command": "none",
  "snapshot": {
    "git_head": "old1234",
    "dirty_count": 0,
    "changed_paths": [],
    "changed_paths_hash": "empty-paths",
    "dirty_hash": "empty-dirty"
  }
}""",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        quality_state_module,
        "quality_snapshot",
        lambda _root: {
            "git_head": "new1234",
            "dirty_count": 0,
            "changed_paths": [],
            "changed_paths_hash": "empty-paths",
            "dirty_hash": "empty-dirty",
        },
    )
    monkeypatch.setattr(
        quality_state_module,
        "_git_output",
        lambda _root, args: "project_status/Quality-Gate-State.json"
        if args == ["diff", "--name-only", "old1234", "new1234"]
        else "",
    )

    freshness = quality_state_freshness(tmp_path)

    assert freshness["fresh"] is True
    assert freshness["status_class"] == "PASS"
    assert freshness["checks"]["committed_snapshot_matches"] is True
    assert freshness["checks"]["status_only_head_change"] is False


def test_quality_state_prunes_missing_snapshot_paths(tmp_path: Path, monkeypatch) -> None:
    path = quality_state_path(tmp_path)
    path.parent.mkdir(parents=True)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "kept.py").write_text("print('kept')\n", encoding="utf-8")
    path.write_text(
        """{
  "schema": "knowledgeradar-quality-state/v1",
  "schema_version": 1,
  "status": "PASS",
  "status_class": "PASS",
  "recommended_next_command": "none",
  "snapshot": {
    "git_head": "abc1234",
    "dirty_count": 2,
    "changed_paths": ["docs/obsolete/missing.md", "src/kept.py"],
    "changed_paths_hash": "old-hash",
    "dirty_hash": "old-dirty"
  },
  "failure_summary": [
    {"stdout": "docs/obsolete/missing.md"}
  ]
}""",
        encoding="utf-8",
    )
    monkeypatch.setattr(quality_state_module, "git_changed_paths", lambda _root: [])

    result = prune_missing_paths_from_quality_state(tmp_path)
    state = read_quality_state(tmp_path)

    assert result["status"] == "pruned"
    assert result["removed_paths"] == ["docs/obsolete/missing.md"]
    assert state["status_class"] == "STALE"
    assert state["snapshot"]["changed_paths"] == ["src/kept.py"]
    assert "obsolete/missing.md" not in json_dumps_for_test(state["failure_summary"])


def json_dumps_for_test(value: object) -> str:
    import json

    return json.dumps(value, ensure_ascii=False)


def test_quality_state_schema_mismatch_is_stale(tmp_path: Path) -> None:
    path = quality_state_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text('{"schema":"old","schema_version":0}', encoding="utf-8")

    state = read_quality_state(tmp_path)

    assert state["status_class"] == "STALE"
    assert state["reason"] == "quality_state_schema_mismatch"
