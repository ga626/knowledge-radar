import time
from pathlib import Path

from runtime.task_adapter import LocalTaskAdapter, LocalTaskSpec
from runtime.task_scope import make_task_scope
from runtime.tasks import TaskStore, compact_task_ref


def test_wait_for_session_waits_until_delayed_task_completes(tmp_path: Path) -> None:
    store = TaskStore(str(tmp_path / "tasks.sqlite3"))
    adapter = LocalTaskAdapter(store)

    adapter.submit(
        LocalTaskSpec(
            task_id="delayed",
            task_type="fake",
            platform="unit",
            metadata={
                "research_session_id": "session-1",
                "blocks_final_report": True,
                "result_reread_tool": "get_content_detail",
            },
        ),
        lambda: (time.sleep(0.2) or {"done": True}),
    )

    waited = store.wait_for_session("session-1", max_wait_s=3, poll_s=0.05)

    assert waited["status"] == "completed"
    assert waited["pending"] == []
    assert waited["terminal"][0]["status"] == "completed"


def test_wait_for_session_only_waits_blocking_tasks(tmp_path: Path) -> None:
    store = TaskStore(str(tmp_path / "tasks.sqlite3"))
    store.upsert_task(
        task_id="blocking",
        task_type="fake",
        platform="unit",
        status="completed",
        metadata={"research_session_id": "session-2", "blocks_final_report": True},
    )
    store.mark_completed("blocking")
    store.upsert_task(
        task_id="non-blocking",
        task_type="comment_filter",
        platform="unit",
        status="running",
        metadata={"research_session_id": "session-2", "blocks_final_report": False},
    )

    waited = store.wait_for_session("session-2", max_wait_s=0.2, poll_s=0.05)

    assert waited["status"] == "completed"
    assert {task["task_id"] for task in waited["tasks"]} == {"blocking"}


def test_compact_task_ref_keeps_timing_but_not_large_metadata(tmp_path: Path) -> None:
    store = TaskStore(str(tmp_path / "tasks.sqlite3"))
    task = store.upsert_task(
        task_id="asr",
        task_type="bilibili_transcribe",
        platform="B站",
        metadata={
            "research_session_id": "session-3",
            "blocks_final_report": True,
            "transcript": "x" * 10_000,
            "subtitle_probe_s": 0.12,
            "download_s": 0,
            "model_load_s": 0,
            "transcribe_s": 0,
            "total_s": 0.14,
        },
    )

    compact = compact_task_ref(task)

    assert compact["timing"]["subtitle_probe_s"] == 0.12
    assert compact["fanin"]["legacy_research_session_id"] == "session-3"
    assert "metadata" not in compact
    assert "transcript" not in repr(compact)


def test_wait_for_scope_waits_by_task_scope_and_source(tmp_path: Path) -> None:
    store = TaskStore(str(tmp_path / "tasks.sqlite3"))
    scope = make_task_scope(
        source_url="https://www.bilibili.com/video/BVscope",
        content_id="BVscope",
        platform="B站",
        research_session_id="legacy-scope",
    )
    store.upsert_task(
        task_id="scope-task",
        task_type="bilibili_transcribe",
        platform="B站",
        target="BVscope",
        content_id="BVscope",
        status="completed",
        metadata={
            **scope.to_metadata(),
            "blocks_final_report": True,
            "result_reread_tool": "get_content_detail",
        },
    )
    store.mark_completed("scope-task")

    by_scope = store.wait_for_scope(task_scope_id=scope.task_scope_id, max_wait_s=0.1, poll_s=0.05)
    by_source = store.wait_for_scope(source_url=scope.source_url, max_wait_s=0.1, poll_s=0.05)
    by_content = store.wait_for_scope(content_id=scope.content_id, max_wait_s=0.1, poll_s=0.05)

    assert by_scope["status"] == "completed"
    assert by_source["terminal"][0]["task_id"] == "scope-task"
    assert by_content["terminal"][0]["task_id"] == "scope-task"
    compact = compact_task_ref(store.get_task("scope-task"))
    assert compact["fanin"]["task_scope_id"] == scope.task_scope_id
    assert compact["fanin"]["work_scope_id"] == scope.work_scope_id
    assert compact["fanin"]["source_url"] == scope.source_url


def test_wait_for_scope_exposes_terminal_failures_without_timeout(tmp_path: Path) -> None:
    store = TaskStore(str(tmp_path / "tasks.sqlite3"))
    store.upsert_task(
        task_id="failed-scope-task",
        task_type="bilibili_qwen_video_analysis",
        platform="B站",
        status="failed",
        metadata={"task_scope_id": "scope-failed", "blocks_final_report": True},
    )
    store.mark_failed("failed-scope-task", error="provider denied", error_code="PROVIDER_UNAVAILABLE")

    waited = store.wait_for_scope(task_scope_id="scope-failed", max_wait_s=0.1, poll_s=0.05)

    assert waited["status"] == "completed"
    assert waited["outcome"] == "completed_with_failures"
    assert waited["terminal_failure_count"] == 1
