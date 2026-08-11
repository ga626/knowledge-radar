from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

from runtime.campaign_patrol import (
    _campaign_quality_gate_command,
    claim_next_campaign_patrol_notification,
    format_campaign_patrol_report,
    peek_next_campaign_patrol_notification,
    run_campaign_patrol,
)


def test_campaign_patrol_pass_writes_json_only(tmp_path: Path, monkeypatch) -> None:
    import runtime.campaign_patrol as campaign_patrol

    class Proc:
        returncode = 0
        stdout = json.dumps({"status": "PASS", "results": [], "quality_state": {"status_class": "PASS"}})
        stderr = ""

    monkeypatch.setattr(campaign_patrol, "silent_subprocess_run", lambda *args, **kwargs: Proc())

    result = run_campaign_patrol(tmp_path, "smoke", now=datetime(2026, 6, 9, tzinfo=timezone.utc))

    assert result["status"] == "PASS"
    assert result["report_written"] is False
    assert result["notification_written"] is False
    assert result["summary"]["impact"] == "无异常。"
    assert Path(result["raw_result_path"]).is_file()
    raw = json.loads(Path(result["raw_result_path"]).read_text(encoding="utf-8"))
    assert "--no-write-state" in raw["command"]
    assert "--write-state" not in raw["command"]


def test_campaign_patrol_failure_writes_short_human_report(tmp_path: Path, monkeypatch) -> None:
    import runtime.campaign_patrol as campaign_patrol

    payload = {
        "status": "FAIL",
        "results": [
            {
                "command": "campaign runtime smoke",
                "status": "fail",
                "stdout": '{"platform":"zhihu","reason":"login_required","manual_action":"login"}',
            }
        ],
        "quality_state": {"status_class": "NEEDS_INTERACTION"},
    }

    class Proc:
        returncode = 1
        stdout = json.dumps(payload)
        stderr = ""

    monkeypatch.setattr(campaign_patrol, "silent_subprocess_run", lambda *args, **kwargs: Proc())

    result = run_campaign_patrol(tmp_path, "smoke", now=datetime(2026, 6, 9, tzinfo=timezone.utc))

    assert result["status_class"] == "NEEDS_INTERACTION"
    assert result["report_written"] is True
    assert result["notification_written"] is True
    report = Path(result["report_path"]).read_text(encoding="utf-8")
    assert "KnowledgeRadar smoke 巡检异常" in report
    assert "campaign runtime smoke" in report
    assert "不会自动修复" in report
    notification = json.loads(Path(result["notification_path"]).read_text(encoding="utf-8"))
    assert "KnowledgeRadar smoke 巡检未完成：FAIL" in notification["message"]
    assert "分类：NEEDS_INTERACTION" in notification["message"]
    assert "未自动修复" in notification["message"]


def test_campaign_notification_probe_peek_is_read_only(tmp_path: Path) -> None:
    pending_dir = tmp_path / "runtime" / "reports" / "campaign-patrol" / "notifications"
    pending_dir.mkdir(parents=True)
    pending = pending_dir / "pending-20260609T000000Z-smoke.json"
    pending.write_text(
        json.dumps(
            {
                "schema": "knowledgeradar-campaign-patrol-notification/v1",
                "profile": "smoke",
                "status_class": "FAIL",
                "message": "KnowledgeRadar smoke 巡检异常：FAIL",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = peek_next_campaign_patrol_notification(tmp_path)

    assert result["status"] == "pending"
    assert "KnowledgeRadar smoke" in str(result["message"])
    assert pending.exists()
    assert "claimed_path" not in result


def test_campaign_notification_probe_claims_newest_pending_and_supersedes_older_same_profile(tmp_path: Path) -> None:
    pending_dir = tmp_path / "runtime" / "reports" / "campaign-patrol" / "notifications"
    pending_dir.mkdir(parents=True)
    older = pending_dir / "pending-20260609T000000Z-smoke.json"
    newer = pending_dir / "pending-20260610T000000Z-smoke.json"
    deep = pending_dir / "pending-20260609T010000Z-deep.json"
    for path, profile, text in (
        (older, "smoke", "older smoke"),
        (newer, "smoke", "newer smoke"),
        (deep, "deep", "deep keep"),
    ):
        path.write_text(
            json.dumps(
                {
                    "schema": "knowledgeradar-campaign-patrol-notification/v1",
                    "profile": profile,
                    "status_class": "FAIL",
                    "message": text,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    result = claim_next_campaign_patrol_notification(tmp_path)

    claimed = Path(str(result["claimed_path"]))
    superseded = pending_dir / "archive" / "superseded" / "superseded-20260609T000000Z-smoke.json"
    assert result["status"] == "pending"
    assert result["message"] == "newer smoke"
    assert result["superseded_pending_count"] == 1
    assert claimed.is_file()
    assert superseded.is_file()
    assert not older.exists()
    assert not newer.exists()
    assert deep.exists()


def test_campaign_patrol_never_classifies_nonzero_exit_as_pass(tmp_path: Path, monkeypatch) -> None:
    import runtime.campaign_patrol as campaign_patrol

    class Proc:
        returncode = 1
        stdout = json.dumps({"status": "FAIL", "results": [], "quality_state": {"status_class": "PASS"}})
        stderr = "closeout blocked"

    monkeypatch.setattr(campaign_patrol, "silent_subprocess_run", lambda *args, **kwargs: Proc())

    result = run_campaign_patrol(tmp_path, "smoke", now=datetime(2026, 6, 9, tzinfo=timezone.utc))

    assert result["status"] == "FAIL"
    assert result["status_class"] == "FAIL"
    report = Path(result["report_path"]).read_text(encoding="utf-8")
    assert "执行结果：FAIL" in report
    assert "分类：FAIL" in report


def test_campaign_command_is_explicitly_read_only(tmp_path: Path) -> None:
    command = _campaign_quality_gate_command(tmp_path, "smoke")

    assert "--no-write-state" in command
    assert "--write-state" not in command


def test_campaign_success_resolves_same_profile_pending_notification(tmp_path: Path, monkeypatch) -> None:
    import runtime.campaign_patrol as campaign_patrol

    pending_dir = tmp_path / "runtime" / "reports" / "campaign-patrol" / "notifications"
    pending_dir.mkdir(parents=True)
    pending = pending_dir / "pending-20260608T000000Z-smoke.json"
    pending.write_text(json.dumps({"profile": "smoke", "status_class": "FAIL"}), encoding="utf-8")

    class Proc:
        returncode = 0
        stdout = json.dumps({"status": "PASS", "results": []})
        stderr = ""

    monkeypatch.setattr(campaign_patrol, "silent_subprocess_run", lambda *args, **kwargs: Proc())
    result = run_campaign_patrol(tmp_path, "smoke", now=datetime(2026, 6, 9, tzinfo=timezone.utc))

    assert result["status"] == "PASS"
    assert not pending.exists()
    assert (pending_dir / "archive" / "resolved" / "resolved-20260608T000000Z-smoke.json").is_file()


def test_notification_claim_archives_false_pass_notification(tmp_path: Path) -> None:
    pending_dir = tmp_path / "runtime" / "reports" / "campaign-patrol" / "notifications"
    pending_dir.mkdir(parents=True)
    pending = pending_dir / "pending-20260609T000000Z-smoke.json"
    pending.write_text(json.dumps({"profile": "smoke", "status_class": "PASS", "message": "bad verdict"}), encoding="utf-8")

    result = claim_next_campaign_patrol_notification(tmp_path)

    assert result["status"] == "none"
    assert result["archived_pending_count"] == 1
    assert (pending_dir / "archive" / "invalid" / "invalid-20260609T000000Z-smoke.json").is_file()


def test_campaign_notification_probe_accepts_utf8_bom(tmp_path: Path) -> None:
    pending_dir = tmp_path / "runtime" / "reports" / "campaign-patrol" / "notifications"
    pending_dir.mkdir(parents=True)
    pending = pending_dir / "pending-20260609T000000Z-deep.json"
    pending.write_text(
        json.dumps(
            {
                "schema": "knowledgeradar-campaign-patrol-notification/v1",
                "profile": "deep",
                "status_class": "FAIL",
                "message": "KnowledgeRadar deep 巡检异常：FAIL",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8-sig",
    )

    result = claim_next_campaign_patrol_notification(tmp_path)

    assert result["status"] == "pending"
    assert result["profile"] == "deep"


def test_campaign_patrol_report_template_is_human_readable() -> None:
    report = format_campaign_patrol_report(
        {
            "profile": "deep",
            "status_class": "FAIL",
            "finished_at": "2026-06-09T09:00:00+00:00",
            "command": "python scripts/kr_quality_gate.py --campaign --profile deep --json --write-state",
            "raw_result_path": "runtime/reports/campaign-patrol/example.json",
            "returncode": 1,
            "result": {
                "results": [
                    {
                        "command": "campaign deep deterministic checks",
                        "status": "fail",
                        "stdout": "provider_status_matrix failed",
                    }
                ]
            },
        }
    )

    assert "## 问题" in report
    assert "## 证据" in report
    assert "provider_status_matrix failed" in report
