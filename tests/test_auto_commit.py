from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from scripts import kr_auto_commit
from runtime.quality_state import prune_missing_paths_from_quality_state, quality_state_freshness, read_quality_state, write_quality_state


ORIGINAL_RESTART_MCP_SERVER = kr_auto_commit._restart_mcp_server


def _stub_quality_state_refresh(monkeypatch) -> None:
    monkeypatch.setattr(
        kr_auto_commit,
        "refresh_quality_state_snapshot",
        lambda _root, event="post_commit_auto_commit": {
            "schema": "knowledgeradar-quality-state/v1",
            "snapshot": {"git_head": "abc1234", "dirty_count": 0},
        },
    )


@pytest.fixture(autouse=True)
def _stub_mcp_restart(monkeypatch):
    monkeypatch.setattr(
        kr_auto_commit,
        "_restart_mcp_server",
        lambda _root: {
            "phase": "restart_mcp_server",
            "returncode": 0,
            "stdout": '{"status":"PASS"}',
            "stderr": "",
        },
    )


def test_auto_commit_noops_when_worktree_is_clean(monkeypatch) -> None:
    monkeypatch.setattr(kr_auto_commit, "_dirty_paths", lambda _root: [])

    result = kr_auto_commit.auto_commit_verified_changes(Path("repo"))

    assert result["status"] == "PASS"
    assert result["action"] == "noop"
    assert result["reason"] == "worktree_clean"


def test_auto_commit_blocks_when_quality_state_is_not_fresh(monkeypatch) -> None:
    monkeypatch.setattr(kr_auto_commit, "_dirty_paths", lambda _root: ["src/example.py"])
    monkeypatch.setattr(
        kr_auto_commit,
        "quality_state_freshness",
        lambda _root: {
            "fresh": False,
            "status_class": "STALE",
            "recommended_next_command": "python scripts/kr_quality_gate.py --changed --json --write-state",
        },
    )

    result = kr_auto_commit.auto_commit_verified_changes(Path("repo"), dry_run=True)

    assert result["status"] == "BLOCKED"
    assert result["action"] == "none"
    assert result["reason"] == "quality_state_not_fresh_pass"


def test_auto_commit_report_mode_uses_minimal_check_not_quality_state(monkeypatch, tmp_path: Path) -> None:
    report = tmp_path / "docs" / "reference" / "测试报告-2026-07-06.md"
    evidence = tmp_path / "docs" / "reference" / "测试报告-2026-07-06.evidence.json"
    report.parent.mkdir(parents=True)
    report.write_text("# 测试报告\n", encoding="utf-8")
    evidence.write_text('{"schema":"knowledgeradar-research-evidence/v1"}\n', encoding="utf-8")
    monkeypatch.setattr(
        kr_auto_commit,
        "_dirty_paths",
        lambda _root: [
            "docs/reference/测试报告-2026-07-06.md",
            "docs/reference/测试报告-2026-07-06.evidence.json",
        ],
    )
    monkeypatch.setattr(
        kr_auto_commit,
        "quality_state_freshness",
        lambda _root: (_ for _ in ()).throw(AssertionError("report mode should not inspect global quality state")),
    )
    monkeypatch.setattr(
        kr_auto_commit,
        "_report_minimal_verify",
        lambda _root, _report, _evidence: {"returncode": 0, "stdout": '{"status":"PASS"}', "stderr": ""},
    )

    result = kr_auto_commit.auto_commit_verified_changes(tmp_path, dry_run=True)

    assert result["status"] == "PASS"
    assert result["action"] == "dry_run"
    assert result["reason"] == "would_commit_report_closeout_verified_changes"


def test_auto_commit_dry_run_reports_verified_commit_candidate(monkeypatch) -> None:
    monkeypatch.setattr(kr_auto_commit, "_dirty_paths", lambda _root: ["src/example.py"])
    monkeypatch.setattr(
        kr_auto_commit,
        "quality_state_freshness",
        lambda _root: {"fresh": True, "status_class": "PASS"},
    )

    result = kr_auto_commit.auto_commit_verified_changes(Path("repo"), message="Test message", dry_run=True)

    assert result["status"] == "PASS"
    assert result["action"] == "dry_run"
    assert result["message"] == "Test message"


def test_auto_commit_derives_message_from_changed_paths(monkeypatch) -> None:
    monkeypatch.setattr(kr_auto_commit, "_dirty_paths", lambda _root: ["scripts/kr_auto_commit.py"])
    monkeypatch.setattr(
        kr_auto_commit,
        "quality_state_freshness",
        lambda _root: {"fresh": True, "status_class": "PASS"},
    )

    result = kr_auto_commit.auto_commit_verified_changes(Path("repo"), dry_run=True)

    assert result["message"] == "Update hook closeout behavior"


def test_restart_mcp_server_redirects_launcher_output_to_files(tmp_path: Path, monkeypatch) -> None:
    script = tmp_path / "scripts" / "kr_mcp_runtime.py"
    script.parent.mkdir(parents=True)
    script.write_text("# test restart script\n", encoding="utf-8")
    state_path = tmp_path / "runtime" / "state" / "knowledgeradar-mcp-runtime.json"
    calls: dict[str, object] = {}

    def fake_run(command: list[str], **kwargs) -> object:
        calls["command"] = command
        calls["stdin"] = kwargs.get("stdin")
        calls["stdout"] = kwargs.get("stdout")
        calls["stderr"] = kwargs.get("stderr")
        calls["capture_output"] = kwargs.get("capture_output")
        kwargs["stdout"].write('{"status":"PASS"}\n')
        kwargs["stderr"].write("")
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(
            json.dumps(
                {
                    "schema": "knowledgeradar-mcp-runtime/v1",
                    "status": "applied",
                    "endpoint": "http://127.0.0.1:18765/mcp",
                    "started_pid": 1234,
                }
            ),
            encoding="utf-8",
        )

        class Proc:
            returncode = 0

        return Proc()

    monkeypatch.setattr(kr_auto_commit.subprocess, "run", fake_run)

    result = ORIGINAL_RESTART_MCP_SERVER(tmp_path)

    assert result["returncode"] == 0
    assert result["restart_status"] == "applied"
    assert result["state"]["started_pid"] == 1234
    assert calls["stdin"] is subprocess.DEVNULL
    assert calls["stdout"] is not subprocess.PIPE
    assert calls["stderr"] is not subprocess.PIPE
    assert calls["capture_output"] is None


def test_auto_commit_commits_with_prepared_project_status(monkeypatch) -> None:
    calls: list[list[str]] = []

    _stub_quality_state_refresh(monkeypatch)
    monkeypatch.setattr(kr_auto_commit, "_dirty_paths", lambda _root: ["src/example.py"])
    monkeypatch.setattr(kr_auto_commit, "_python_for_root", lambda _root: "python")
    monkeypatch.setattr(
        kr_auto_commit,
        "quality_state_freshness",
        lambda _root: {"fresh": True, "status_class": "PASS"},
    )

    def fake_run(command: list[str], _root: Path, *, timeout: int = 120) -> dict:
        calls.append(command)
        return {"command": " ".join(command), "returncode": 0, "stdout": "", "stderr": ""}

    cached_calls = 0

    def fake_git_output(_root: Path, args: list[str]) -> str:
        nonlocal cached_calls
        if args == ["diff", "--cached", "--name-only"]:
            cached_calls += 1
            if cached_calls == 3:
                return ""
            return "src/example.py\nproject_status/KnowledgeRadar-Project-State.md"
        if args == ["rev-parse", "--short", "HEAD"]:
            return "abc1234"
        return ""

    monkeypatch.setattr(kr_auto_commit, "_run", fake_run)
    monkeypatch.setattr(kr_auto_commit, "_git_output", fake_git_output)

    result = kr_auto_commit.auto_commit_verified_changes(Path("repo"), message="Auto test")

    assert result["status"] == "PASS"
    assert result["action"] == "committed"
    assert result["commit"] == "abc1234"
    assert result["mcp_restart"]["returncode"] == 0
    assert calls == [
        ["python", "scripts/kr_pre_commit_prepare.py", "--json"],
        ["git", "add", "-A"],
        ["git", "commit", "--no-verify", "-m", "Auto test"],
        ["python", "scripts/kr_pre_commit_prepare.py", "--json", "--preserve-pass-quality-state"],
        ["git", "add", "-A", "--", "project_status"],
    ]


def test_auto_commit_refreshes_quality_state_after_post_commit_prepare(monkeypatch) -> None:
    events: list[str] = []
    cached = iter(["src/example.py", "src/example.py", "project_status/Quality-Gate-State.json"])

    monkeypatch.setattr(kr_auto_commit, "_dirty_paths", lambda _root: ["src/example.py"])
    monkeypatch.setattr(kr_auto_commit, "_python_for_root", lambda _root: "python")
    monkeypatch.setattr(kr_auto_commit, "quality_state_freshness", lambda _root: {"fresh": True, "status_class": "PASS"})
    monkeypatch.setattr(
        kr_auto_commit,
        "refresh_quality_state_snapshot",
        lambda _root, event="post_commit_auto_commit": events.append("refresh_quality_state")
        or {"schema": "knowledgeradar-quality-state/v1", "snapshot": {"git_head": "post-prepare", "dirty_count": 1}},
    )

    def fake_run(command: list[str], _root: Path, *, timeout: int = 120) -> dict:
        if command[:3] == ["python", "scripts/kr_pre_commit_prepare.py", "--json"]:
            events.append("prepare_project_status")
        elif command == ["git", "commit", "--no-verify", "-m", "Auto test"]:
            events.append("initial_commit")
        elif command == ["git", "add", "-A", "--", "project_status"]:
            events.append("stage_project_status")
        elif command == ["git", "commit", "--amend", "--no-verify", "--no-edit"]:
            events.append("amend_status")
        return {"command": " ".join(command), "returncode": 0, "stdout": "", "stderr": ""}

    def fake_git_output(_root: Path, args: list[str]) -> str:
        if args == ["diff", "--cached", "--name-only"]:
            return next(cached)
        if args == ["rev-parse", "--short", "HEAD"]:
            return "abc1234"
        return ""

    monkeypatch.setattr(kr_auto_commit, "_run", fake_run)
    monkeypatch.setattr(kr_auto_commit, "_git_output", fake_git_output)

    result = kr_auto_commit.auto_commit_verified_changes(Path("repo"), message="Auto test")

    assert result["status"] == "PASS"
    assert events == [
        "prepare_project_status",
        "initial_commit",
        "prepare_project_status",
        "refresh_quality_state",
        "stage_project_status",
        "amend_status",
    ]


def test_auto_commit_amends_post_commit_status_only_changes(monkeypatch) -> None:
    calls: list[list[str]] = []
    cached = iter(
        [
            "src/example.py\nproject_status/KnowledgeRadar-Project-State.md",
            "src/example.py\nproject_status/KnowledgeRadar-Project-State.md",
            "project_status/KnowledgeRadar-Project-State.md",
        ]
    )

    _stub_quality_state_refresh(monkeypatch)
    monkeypatch.setattr(kr_auto_commit, "_dirty_paths", lambda _root: ["src/example.py"])
    monkeypatch.setattr(kr_auto_commit, "_python_for_root", lambda _root: "python")
    monkeypatch.setattr(kr_auto_commit, "quality_state_freshness", lambda _root: {"fresh": True, "status_class": "PASS"})

    def fake_run(command: list[str], _root: Path, *, timeout: int = 120) -> dict:
        calls.append(command)
        return {"command": " ".join(command), "returncode": 0, "stdout": "", "stderr": ""}

    def fake_git_output(_root: Path, args: list[str]) -> str:
        if args == ["diff", "--cached", "--name-only"]:
            return next(cached)
        if args == ["rev-parse", "--short", "HEAD"]:
            return "abc1234"
        return ""

    monkeypatch.setattr(kr_auto_commit, "_run", fake_run)
    monkeypatch.setattr(kr_auto_commit, "_git_output", fake_git_output)

    result = kr_auto_commit.auto_commit_verified_changes(Path("repo"), message="Auto test")

    assert result["status"] == "PASS"
    assert result["post_commit_status_amended"] is True
    assert result["post_commit_status_paths"] == ["project_status/KnowledgeRadar-Project-State.md"]
    assert calls[-1] == ["git", "commit", "--amend", "--no-verify", "--no-edit"]


def test_auto_commit_rejects_post_commit_non_status_changes(monkeypatch) -> None:
    cached = iter(
        [
            "src/example.py",
            "src/example.py",
            "src/unexpected.py",
        ]
    )

    _stub_quality_state_refresh(monkeypatch)
    monkeypatch.setattr(kr_auto_commit, "_dirty_paths", lambda _root: ["src/example.py"])
    monkeypatch.setattr(kr_auto_commit, "_python_for_root", lambda _root: "python")
    monkeypatch.setattr(kr_auto_commit, "quality_state_freshness", lambda _root: {"fresh": True, "status_class": "PASS"})
    monkeypatch.setattr(
        kr_auto_commit,
        "_run",
        lambda command, _root, timeout=120: {"command": " ".join(command), "returncode": 0, "stdout": "", "stderr": ""},
    )

    def fake_git_output(_root: Path, args: list[str]) -> str:
        if args == ["diff", "--cached", "--name-only"]:
            return next(cached)
        if args == ["rev-parse", "--short", "HEAD"]:
            return "abc1234"
        return ""

    monkeypatch.setattr(kr_auto_commit, "_git_output", fake_git_output)

    result = kr_auto_commit.auto_commit_verified_changes(Path("repo"), message="Auto test")

    assert result["status"] == "FAIL"
    assert result["reason"] == "post_commit_prepare_touched_non_status_paths"


def test_auto_commit_unstages_development_process_docs(monkeypatch) -> None:
    calls: list[list[str]] = []
    cached = iter(
        [
            "src/example.py\ndesign_docs/old-report.md\ndocs/research/speed-review.md\ndocs/acceptance-2026-06-03.md\ndocs/reference/ORGANIZATION_REPORT.md\ndocs/reference/PLATFORM_TEMPLATE.md",
            "src/example.py\ndocs/reference/PLATFORM_TEMPLATE.md",
            "",
        ]
    )

    _stub_quality_state_refresh(monkeypatch)
    monkeypatch.setattr(
        kr_auto_commit,
        "_dirty_paths",
        lambda _root: [
            "src/example.py",
            "design_docs/old-report.md",
            "docs/research/speed-review.md",
            "docs/acceptance-2026-06-03.md",
            "docs/reference/ORGANIZATION_REPORT.md",
        ],
    )
    monkeypatch.setattr(kr_auto_commit, "_python_for_root", lambda _root: "python")
    monkeypatch.setattr(kr_auto_commit, "quality_state_freshness", lambda _root: {"fresh": True, "status_class": "PASS"})

    def fake_run(command: list[str], _root: Path, *, timeout: int = 120) -> dict:
        calls.append(command)
        return {"command": " ".join(command), "returncode": 0, "stdout": "", "stderr": ""}

    def fake_git_output(_root: Path, args: list[str]) -> str:
        if args == ["diff", "--cached", "--name-only"]:
            return next(cached)
        if args == ["rev-parse", "--short", "HEAD"]:
            return "abc1234"
        return ""

    monkeypatch.setattr(kr_auto_commit, "_run", fake_run)
    monkeypatch.setattr(kr_auto_commit, "_git_output", fake_git_output)

    result = kr_auto_commit.auto_commit_verified_changes(Path("repo"), message="Auto test")

    assert result["status"] == "PASS"
    assert [
        "git",
        "restore",
        "--staged",
        "--",
        "design_docs/old-report.md",
        "docs/research/speed-review.md",
        "docs/acceptance-2026-06-03.md",
        "docs/reference/ORGANIZATION_REPORT.md",
    ] in calls
    assert "design_docs/old-report.md" not in result["staged_paths"]
    assert "docs/research/speed-review.md" not in result["staged_paths"]
    assert "docs/acceptance-2026-06-03.md" not in result["staged_paths"]
    assert "docs/reference/ORGANIZATION_REPORT.md" not in result["staged_paths"]
    assert "docs/reference/PLATFORM_TEMPLATE.md" in result["staged_paths"]


def test_auto_commit_lifecycle_accepts_committed_quality_snapshot(tmp_path: Path) -> None:
    repo = tmp_path
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "KR Test"], cwd=repo, check=True)
    (repo / "README.md").write_text("initial\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "Initial"], cwd=repo, check=True, capture_output=True, text=True)

    (repo / "src").mkdir()
    (repo / "src" / "example.py").write_text("print('changed')\n", encoding="utf-8")
    write_quality_state(
        {
            "schema": "knowledgeradar-quality-gate-result/v1",
            "mode": "changed",
            "status": "PASS",
            "total": 1,
            "failed": 0,
            "results": [{"status": "pass", "command": "test"}],
        },
        repo,
    )

    result = kr_auto_commit.auto_commit_verified_changes(
        repo,
        message="Lifecycle test",
        prepare_project_status=False,
        post_commit_status_refresh=False,
    )
    freshness = quality_state_freshness(repo)
    status = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    assert result["status"] == "PASS"
    assert result["action"] == "committed"
    assert status == ""
    assert freshness["fresh"] is True
    assert freshness["status_class"] == "PASS"
    assert freshness["checks"]["committed_snapshot_matches"] is True


def test_quality_state_prune_keeps_pass_state_when_missing_paths_are_removed(tmp_path: Path) -> None:
    repo = tmp_path
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "KR Test"], cwd=repo, check=True)
    (repo / "README.md").write_text("initial\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "Initial"], cwd=repo, check=True, capture_output=True, text=True)

    doomed = repo / "docs" / "removed.md"
    doomed.parent.mkdir(parents=True)
    doomed.write_text("temporary\n", encoding="utf-8")
    write_quality_state(
        {
            "schema": "knowledgeradar-quality-gate-result/v1",
            "mode": "changed",
            "status": "PASS",
            "total": 1,
            "failed": 0,
            "results": [{"status": "pass", "command": "test"}],
        },
        repo,
    )
    doomed.unlink()

    result = prune_missing_paths_from_quality_state(repo, preserve_pass_status=True)
    state = read_quality_state(repo)

    assert result["status"] == "pruned"
    assert state["status_class"] == "PASS"


def test_auto_commit_post_commit_refresh_does_not_need_status_only_head_change(tmp_path: Path) -> None:
    repo = tmp_path
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "KR Test"], cwd=repo, check=True)
    (repo / "README.md").write_text("initial\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "Initial"], cwd=repo, check=True, capture_output=True, text=True)

    (repo / "src").mkdir()
    (repo / "src" / "example.py").write_text("print('changed')\n", encoding="utf-8")
    write_quality_state(
        {
            "schema": "knowledgeradar-quality-gate-result/v1",
            "mode": "changed",
            "status": "PASS",
            "total": 1,
            "failed": 0,
            "results": [{"status": "pass", "command": "test"}],
        },
        repo,
    )

    result = kr_auto_commit.auto_commit_verified_changes(
        repo,
        message="Lifecycle test",
        prepare_project_status=False,
        restart_mcp_server=False,
    )
    freshness = quality_state_freshness(repo)
    status = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    assert result["status"] == "PASS"
    assert result["action"] == "committed"
    assert result["post_commit_status_amended"] is True
    assert status == ""
    assert freshness["fresh"] is True
    assert freshness["status_class"] == "PASS"
    assert freshness["checks"]["status_only_head_change"] is False
    assert freshness["checks"]["committed_snapshot_matches"] is True


def test_auto_commit_reports_failure_when_mcp_restart_fails(monkeypatch) -> None:
    _stub_quality_state_refresh(monkeypatch)
    monkeypatch.setattr(kr_auto_commit, "_dirty_paths", lambda _root: ["src/example.py"])
    monkeypatch.setattr(kr_auto_commit, "_python_for_root", lambda _root: "python")
    monkeypatch.setattr(kr_auto_commit, "quality_state_freshness", lambda _root: {"fresh": True, "status_class": "PASS"})
    monkeypatch.setattr(
        kr_auto_commit,
        "_restart_mcp_server",
        lambda _root: {"phase": "restart_mcp_server", "returncode": 1, "stdout": "", "stderr": "restart failed"},
    )

    def fake_run(command: list[str], _root: Path, *, timeout: int = 120) -> dict:
        return {"command": " ".join(command), "returncode": 0, "stdout": "", "stderr": ""}

    cached_calls = 0

    def fake_git_output(_root: Path, args: list[str]) -> str:
        nonlocal cached_calls
        if args == ["diff", "--cached", "--name-only"]:
            cached_calls += 1
            return "src/example.py" if cached_calls <= 2 else ""
        if args == ["rev-parse", "--short", "HEAD"]:
            return "abc1234"
        return ""

    monkeypatch.setattr(kr_auto_commit, "_run", fake_run)
    monkeypatch.setattr(kr_auto_commit, "_git_output", fake_git_output)

    result = kr_auto_commit.auto_commit_verified_changes(Path("repo"), message="Auto test")

    assert result["status"] == "FAIL"
    assert result["action"] == "committed_restart_failed"
    assert result["commit"] == "abc1234"
