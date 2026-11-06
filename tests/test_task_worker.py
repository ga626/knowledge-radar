import time
from types import SimpleNamespace
import pytest

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

    monkeypatch.setattr("runtime.task_adapter.threading", SimpleNamespace(Thread=FakeThread))
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

    monkeypatch.setattr("runtime.task_adapter.threading", SimpleNamespace(Thread=FakeThread))
    adapter = LocalTaskAdapter(store)
    adapter.submit_lifecycle(
        LocalTaskSpec(task_id="asr-1", task_type="bilibili_transcribe", platform="test", target="x"),
        lambda: store.mark_completed("asr-1", metadata={"done": True}),
    )

    task = store.get_task("asr-1")
    assert task["status"] == "completed"
    assert task["metadata"]["resource_kind"] == "local_heavy"
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


@pytest.mark.parametrize('lifecycle', [False, True])
def test_late_thread_cannot_write_into_replacement_attempt(monkeypatch, tmp_path, lifecycle):
    from threading import Event, Thread
    from runtime import task_adapter
    store = TaskStore(str(tmp_path/'tasks.sqlite3'))
    adapter = LocalTaskAdapter(store)
    threads, entered, release = [], [Event(),Event()], [Event(),Event()]
    def factory(**kwargs):
        thread = Thread(**kwargs)
        threads.append(thread)
        return thread
    monkeypatch.setattr(task_adapter, 'threading', SimpleNamespace(Thread=factory))
    spec = LocalTaskSpec(task_id='same-id', task_type='fixture', platform='test', max_attempts=1)
    submit = adapter.submit_lifecycle if lifecycle else adapter.submit
    def work(index):
        if lifecycle:
            store.mark_running('same-id')
        entered[index].set()
        assert release[index].wait(8)
        if index==0:
            store.upsert_task('same-id','fixture','test',metadata={'late_upsert':True})
            store.heartbeat('same-id', {'late_heartbeat':True})
        if lifecycle:
            store.mark_completed('same-id', metadata={'value':index})
        return {'value':index}
    try:
        submit(spec, lambda:work(0))
        assert entered[0].wait(3)
        old = store.get_task('same-id')['metadata']['attempt_id']
        store.cancel_task('same-id')
        submit(spec, lambda:work(1))
        assert entered[1].wait(3)
        current = store.get_task('same-id')
        assert current['metadata']['attempt_id']!=old
        release[0].set()
        threads[0].join(timeout=3)
        current = store.get_task('same-id')
        assert current['status']=='running'
        assert not current['metadata'].get('late_upsert') and not current['metadata'].get('late_heartbeat')
    finally:
        for event in release:
            event.set()
        for thread in threads:
            thread.join(timeout=3)
    assert store.get_task('same-id')['status']=='completed'
    assert store.get_task('same-id')['metadata']['value']==1
