import importlib.util
from pathlib import Path

from runtime.closeout_advisor import recommend_closeout, scan_text_for_path_leaks
from runtime import project_state as project_state_module
from runtime.project_policy import evaluate_pre_change
from runtime.project_state import (
    collect_project_state,
    _is_project_status_path,
    project_governance_manifest,
    project_state_freshness,
    render_project_state_markdown,
    write_project_status,
)

ROOT = Path(__file__).resolve().parents[1]
PREPARE_SPEC = importlib.util.spec_from_file_location(
    "kr_pre_commit_prepare_for_project_governance_test",
    ROOT / "scripts" / "kr_pre_commit_prepare.py",
)
kr_pre_commit_prepare = importlib.util.module_from_spec(PREPARE_SPEC)
assert PREPARE_SPEC and PREPARE_SPEC.loader
PREPARE_SPEC.loader.exec_module(kr_pre_commit_prepare)


def test_project_state_renders_redacted_root() -> None:
    state = collect_project_state()
    text = render_project_state_markdown(state)

    assert state["schema"] == "knowledgeradar-project-state/v1"
    assert "{KR_ROOT}" in text
    assert "D:\\Projects\\KnowledgeRadar" not in text


def test_write_project_status_uses_project_status_directory(tmp_path: Path) -> None:
    written = write_project_status(tmp_path)

    assert (tmp_path / "project_status" / "KnowledgeRadar-Project-State.md").is_file()
    assert not (tmp_path / "project_status" / "KnowledgeRadar-Open-Issues.md").exists()
    assert (tmp_path / "project_status" / "KnowledgeRadar-Validation-Semantics.md").is_file()
    assert (tmp_path / "project_status" / "KnowledgeRadar-Codex-Handoff.md").is_file()
    assert all("project_status" in path for path in written.values())


def test_project_policy_blocks_local_state_and_warns_capabilities() -> None:
    blocked = evaluate_pre_change("edit runtime", ["runtime/logs/a.jsonl", "src/capabilities.py"])

    assert blocked["status"] == "BLOCK"
    assert blocked["blocked"][0]["reason"] == "runtime_local_state"
    assert any(item["path"] == "src/capabilities.py" for item in blocked["warnings"])


def test_closeout_advisor_recommends_structure_checks() -> None:
    result = recommend_closeout(["config/project-structure.manifest.json", "project_status/KnowledgeRadar-Project-State.md"])

    assert "python scripts/generate_structure_docs.py" in result["recommended_checks"]
    assert "python scripts/kr_redact_report_paths.py --check project_status --strict" in result["recommended_checks"]
    assert "python scripts/kr_quality_gate.py --changed" in result["recommended_checks"]


def test_closeout_advisor_does_not_bind_development_docs_to_redaction() -> None:
    result = recommend_closeout(
        [
            "docs/reference/example-report.md",
            "docs/research/investigation.txt",
            "docs/notes/private.md",
        ]
    )

    redaction_checks = [item for item in result["recommended_checks"] if "kr_redact_report_paths.py" in item]
    assert redaction_checks == []
    assert "python scripts/kr_project_state.py --check-fresh" not in result["recommended_checks"]


def test_redaction_scanner_detects_local_paths() -> None:
    assert scan_text_for_path_leaks("D:\\Projects\\KnowledgeRadar\\docs\\x.md")
    assert not scan_text_for_path_leaks("{KR_ROOT}\\docs\\x.md")


def test_project_governance_manifest_is_agent_readable() -> None:
    manifest = project_governance_manifest()

    assert manifest["status"] == "implemented"
    assert manifest["status_dir"] == "project_status"
    assert manifest["scripts"]["quality_gate"] == "scripts/kr_quality_gate.py"
    assert manifest["scripts"]["quality_hook_installer"] == "scripts/install_quality_hooks.py"
    assert manifest["quality_gate_manifest"] == "config/quality-gates.manifest.json"
    assert manifest["quality_state"] == "project_status/Quality-Gate-State.json"
    assert "project_status/KnowledgeRadar-Codex-Handoff.md" in manifest["status_files"]
    assert manifest["agent_adapters"]["codex_hooks"] == ".codex/hooks.json"
    assert manifest["runtime_events"]["release_policy"] == "exclude_local_runtime"


def test_project_state_freshness_detects_mismatch(tmp_path: Path) -> None:
    out_dir = tmp_path / "project_status"
    out_dir.mkdir()
    (out_dir / "KnowledgeRadar-Project-State.md").write_text(
        "\n".join(
            [
                "# KnowledgeRadar Project State",
                "- git_head: `oldhead`",
                "- dirty_count_excluding_project_status: `999`",
            ]
        ),
        encoding="utf-8",
    )

    result = project_state_freshness(tmp_path)

    assert result["status"] == "stale"
    assert result["fresh"] is False


def test_project_status_path_detection_handles_short_status_prefixes() -> None:
    assert _is_project_status_path(" M project_status/KnowledgeRadar-Project-State.md")
    assert _is_project_status_path("?? project_status/Quality-Gate-State.json")
    assert not _is_project_status_path(" M src/runtime/project_state.py")


def test_status_only_head_change_is_accepted(monkeypatch) -> None:
    root = Path(__file__).resolve().parents[1]
    monkeypatch.setattr(project_state_module, "_head_diff_paths", lambda *_args: ["project_status/Quality-Gate-State.json"])

    ok, paths = project_state_module._head_change_is_status_only(root, "oldhead", "newhead")

    assert ok is True
    assert paths
    assert all(path.startswith("project_status/") for path in paths)


def test_precommit_snapshot_head_change_is_accepted(monkeypatch) -> None:
    root = Path(__file__).resolve().parents[1]
    fields = {"changed_paths_excluding_project_status": "src/example.py, docs/example.md"}
    monkeypatch.setattr(
        project_state_module,
        "_head_diff_paths",
        lambda *_args: [
            "src/example.py",
            "docs/example.md",
            "project_status/KnowledgeRadar-Project-State.md",
        ],
    )

    ok, recorded, head_paths = project_state_module._recorded_dirty_paths_are_head_diff(root, fields, "oldhead", "newhead")

    assert ok is True
    assert sorted(recorded) == ["docs/example.md", "src/example.py"]
    assert "project_status/KnowledgeRadar-Project-State.md" in head_paths


def test_precommit_prepare_skips_project_status_refresh_for_status_only_commit(monkeypatch, tmp_path: Path) -> None:
    written = []

    monkeypatch.setattr(
        kr_pre_commit_prepare,
        "_staged_paths",
        lambda _root: ["project_status/KnowledgeRadar-Project-State.md"],
    )
    monkeypatch.setattr(
        kr_pre_commit_prepare,
        "_dirty_paths",
        lambda _root: ["project_status/KnowledgeRadar-Project-State.md"],
    )
    monkeypatch.setattr(
        kr_pre_commit_prepare,
        "write_project_status",
        lambda _root: written.append("called") or {},
    )
    monkeypatch.setattr(
        kr_pre_commit_prepare,
        "prune_missing_paths_from_quality_state",
        lambda *_args, **_kwargs: {"status": "ok"},
    )

    result = kr_pre_commit_prepare.prepare_commit(tmp_path, stage=False)

    assert written == []
    assert result["project_status_refresh"] == "skipped_status_only_commit"
    assert result["non_status_staged_paths"] == []
    assert result["non_status_dirty_paths"] == []


def test_precommit_prepare_refreshes_project_status_for_code_commit(monkeypatch, tmp_path: Path) -> None:
    project_status = tmp_path / "project_status"
    project_status.mkdir()
    state = project_status / "KnowledgeRadar-Project-State.md"
    state.write_text("state", encoding="utf-8")

    monkeypatch.setattr(
        kr_pre_commit_prepare,
        "_staged_paths",
        lambda _root: [],
    )
    monkeypatch.setattr(
        kr_pre_commit_prepare,
        "_dirty_paths",
        lambda _root: ["src/server.py"],
    )
    monkeypatch.setattr(
        kr_pre_commit_prepare,
        "write_project_status",
        lambda _root: {"state": str(state)},
    )
    monkeypatch.setattr(
        kr_pre_commit_prepare,
        "prune_missing_paths_from_quality_state",
        lambda *_args, **_kwargs: {"status": "ok"},
    )

    result = kr_pre_commit_prepare.prepare_commit(tmp_path, stage=False)

    assert result["project_status_refresh"] == "written"
    assert result["non_status_dirty_paths"] == ["src/server.py"]
    assert result["staged_paths"] == []


def test_precommit_prepare_dirty_paths_preserve_first_character(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        kr_pre_commit_prepare,
        "_run_git",
        lambda _root, _args: {
            "returncode": 0,
            "stdout": " M scripts/kr_pre_commit_prepare.py\n?? src/new_file.py\n",
            "stderr": "",
        },
    )

    assert kr_pre_commit_prepare._dirty_paths(tmp_path) == [
        "scripts/kr_pre_commit_prepare.py",
        "src/new_file.py",
    ]


def test_precommit_prepare_can_skip_project_status_refresh_by_caller(monkeypatch, tmp_path: Path) -> None:
    written = []

    monkeypatch.setattr(kr_pre_commit_prepare, "_staged_paths", lambda _root: [])
    monkeypatch.setattr(kr_pre_commit_prepare, "_dirty_paths", lambda _root: ["src/server.py"])
    monkeypatch.setattr(
        kr_pre_commit_prepare,
        "write_project_status",
        lambda _root: written.append("called") or {},
    )
    monkeypatch.setattr(
        kr_pre_commit_prepare,
        "prune_missing_paths_from_quality_state",
        lambda *_args, **_kwargs: {"status": "ok"},
    )

    result = kr_pre_commit_prepare.prepare_commit(tmp_path, stage=False, refresh_project_status=False)

    assert written == []
    assert result["project_status_refresh"] == "skipped_by_caller"
    assert result["non_status_dirty_paths"] == ["src/server.py"]
