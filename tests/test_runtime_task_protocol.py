from pathlib import Path

from runtime.tasks import TaskStore, compact_task_ref


def test_compact_task_ref_does_not_include_large_metadata(tmp_path: Path) -> None:
    store = TaskStore(str(tmp_path / "tasks.sqlite3"))
    task = store.upsert_task(
        task_id="task-large",
        task_type="video_analysis",
        platform="bilibili",
        target="BV1example",
        content_id="content-1",
        metadata={
            "phase": "queued",
            "transcript": "x" * 10_000,
            "deep_analysis": "y" * 10_000,
        },
    )

    compact = compact_task_ref(task)

    assert compact["schema_version"] == "knowledgeradar-runtime-task-ref/v1"
    assert compact["task_id"] == "task-large"
    assert compact["status"] == "queued"
    assert compact["recommended_next_action"] == "poll_get_task_status"
    assert "metadata" not in compact
    assert "transcript" not in repr(compact)
    assert "deep_analysis" not in repr(compact)


def test_compact_task_ref_redacts_fanin_result_path(tmp_path: Path) -> None:
    result_path = tmp_path / "result.json"
    result_path.write_text("{}", encoding="utf-8")
    store = TaskStore(str(tmp_path / "tasks.sqlite3"))
    task = store.upsert_task(
        task_id="task-result",
        task_type="video_analysis",
        platform="bilibili",
        target="BV1example",
        result_path=str(result_path),
        metadata={"result_path": str(result_path), "source_url": "https://example.com/video"},
    )

    compact = compact_task_ref(task)

    assert compact["result"]["name"] == "result.json"
    assert compact["fanin"]["result_ref"]["name"] == "result.json"
    assert "result_path" not in compact["fanin"]
    assert str(tmp_path) not in repr(compact)


def test_compact_task_ref_exposes_detail_reread_result_when_no_file(tmp_path: Path) -> None:
    store = TaskStore(str(tmp_path / "tasks.sqlite3"))
    task = store.upsert_task(
        task_id="xhs-ocr",
        task_type="xhs_image_ocr",
        platform="小红书",
        content_id="note-1",
        status="completed",
        metadata={
            "source_url": "https://www.xiaohongshu.com/explore/note-1",
            "result_reread_tool": "get_content_detail",
        },
    )
    store.mark_completed("xhs-ocr")
    task = store.get_task("xhs-ocr")

    compact = compact_task_ref(task)

    assert compact["result"]["available"] is True
    assert compact["result"]["kind"] == "detail_reread"
    assert compact["result"]["tool"] == "get_content_detail"
    assert compact["fanin"]["result_ref"]["kind"] == "detail_reread"


def test_stale_task_cleanup_returns_cancelled_reason(tmp_path: Path) -> None:
    store = TaskStore(str(tmp_path / "tasks.sqlite3"))
    store.upsert_task(
        task_id="stale-queued",
        task_type="ocr",
        platform="xiaohongshu",
        target="note-1",
        status="queued",
    )

    cleanup = store.cleanup_stale_tasks(
        queued_seconds=-1,
        running_seconds=-1,
        reason="unit_test_stale_cleanup",
    )
    task = store.get_task("stale-queued")
    compact = compact_task_ref(task)

    assert cleanup["cleaned_count"] == 1
    assert task is not None
    assert task["status"] == "cancelled"
    assert task["error_code"] == "cancelled"
    assert compact["reason"] == "unit_test_stale_cleanup"
    assert compact["recommended_next_action"] == "inspect_error"


def test_default_task_db_uses_log_dir(monkeypatch, tmp_path: Path) -> None:
    import runtime.tasks as tasks

    monkeypatch.delenv("KR_TASK_DB_PATH", raising=False)
    monkeypatch.setenv("KR_LOG_DIR", str(tmp_path / "logs"))

    assert tasks.default_task_db_path() == str(tmp_path / "logs" / "knowledgeradar-tasks.sqlite3")
