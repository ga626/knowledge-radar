import time

from runtime.task_adapter import LocalTaskAdapter, LocalTaskSpec
from runtime.task_worker import cleanup_stale_worker_tasks, task_worker_summary
from runtime.tasks import TaskStore


def test_lifecycle_adapter_starts_legacy_task(monkeypatch, tmp_path):
    store = TaskStore(str(tmp_path / "tasks.sqlite3"))
    started = {"value": False}

    class FakeThread:
        def __init__(self, target, args=(), daemon=True):
            self.target = target
            self.args = args
            self.daemon = daemon

        def start(self):
            started["value"] = True

    monkeypatch.setattr("runtime.task_adapter.threading.Thread", FakeThread)
    adapter = LocalTaskAdapter(store)
    task = adapter.submit_lifecycle(
        LocalTaskSpec(task_id="legacy-1", task_type="legacy", platform="test", target="x"),
        lambda: None,
    )

    assert started["value"] is True
    assert task["status"] == "queued"
    assert task["metadata"]["adapter"] == "local_thread_lifecycle"
    assert task["metadata"]["resource_kind"] == "unbounded"


def test_lifecycle_adapter_records_resource_acquired(monkeypatch, tmp_path):
    store = TaskStore(str(tmp_path / "tasks.sqlite3"))

    class FakeThread:
        def __init__(self, target, args=(), daemon=True):
            self.target = target
            self.args = args
            self.daemon = daemon

        def start(self):
            self.target(*self.args)

    monkeypatch.setattr("runtime.task_adapter.threading.Thread", FakeThread)
    adapter = LocalTaskAdapter(store)
    adapter.submit_lifecycle(
        LocalTaskSpec(task_id="asr-1", task_type="bilibili_transcribe", platform="test", target="x"),
        lambda: store.mark_completed("asr-1", metadata={"done": True}),
    )

    task = store.get_task("asr-1")
    assert task["status"] == "completed"
    assert task["metadata"]["resource_kind"] == "asr_cpu"
    assert task["metadata"]["limit"] == 1


def test_worker_summary_and_stale_cleanup(tmp_path):
    store = TaskStore(str(tmp_path / "tasks.sqlite3"))
    store.upsert_task("stale-queued", "test", "unit", status="queued")
    store.heartbeat("stale-queued", {"phase": "queued"})
    # Force an old timestamp through the public cleanup thresholds.
    with store._connect() as conn:
        conn.execute("UPDATE runtime_tasks SET created_at=?, updated_at=? WHERE task_id=?", (time.time() - 3600, time.time() - 3600, "stale-queued"))

    cleaned = cleanup_stale_worker_tasks(store, running_seconds=1, queued_seconds=1)
    summary = task_worker_summary(store)

    assert cleaned["cleaned_count"] == 1
    assert store.get_task("stale-queued")["status"] == "cancelled"
    assert summary["schema"] == "knowledgeradar-task-worker/v1"
    assert summary["protocol"]["final_fanin_wait"] is True
    assert summary["protocol"]["task_scope_fanin"] is True
    assert summary["protocol"]["legacy_research_session_id_alias"] is True
    assert summary["server_run_id"].startswith("kr-run-")
    assert summary["store"]["path_role"] == "KR_TASK_DB_PATH"
    assert summary["store"]["name"] == "tasks.sqlite3"
    assert "path" not in summary["store"]


def test_failed_task_can_be_requeued_with_same_id(tmp_path):
    store = TaskStore(str(tmp_path / "tasks.sqlite3"))
    store.upsert_task("retry-me", "test", "unit", status="queued", max_attempts=2)
    store.mark_failed("retry-me", "first run failed", error_code="DownloadError")

    task = store.upsert_task("retry-me", "test", "unit", status="queued", max_attempts=2)

    assert task["status"] == "queued"
    assert task["attempts"] == 0
    assert task["finished_at"] is None
    assert task["error"] == ""
    assert task["error_code"] == ""
