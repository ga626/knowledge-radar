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


def test_task_reads_do_not_create_database_or_artifact_directory(tmp_path, monkeypatch):
    from runtime.task_service import TaskService
    root = tmp_path / 'never-created'
    store = TaskStore(str(root / 'tasks.sqlite3'))
    monkeypatch.setattr(store, 'initialize', lambda: (_ for _ in ()).throw(AssertionError('query initialized schema')))
    assert TaskService(store).result('missing')['status'] == 'not_found'
    assert store.recent_tasks() == []
    assert store.summary()['total'] == 0
    assert store.summary()['status'] == 'not_initialized'
    assert store.stale_tasks() == []
    assert store.tasks_for_scope(task_scope_id='scope') == []
    assert store.tasks_for_source(source_url='https://example.org') == []
    assert store.tasks_for_session('session') == []
    assert store.wait_for_scope(task_scope_id='scope', max_wait_s=0)['tasks'] == []
    assert not root.exists()


def test_old_schema_is_readable_without_migration(tmp_path):
    import sqlite3
    path = tmp_path / 'old.sqlite3'
    store = TaskStore(str(path))
    store.upsert_task('legacy', 'fake', 'test', metadata={'task_scope_id': 'scope'})
    # 上一代没有独立的作用域列，已有 metadata 仍可供只读查询。
    with sqlite3.connect(path) as conn:
        conn.execute('DROP INDEX idx_runtime_tasks_task_scope')
        conn.execute('ALTER TABLE runtime_tasks DROP COLUMN task_scope_id')
        conn.execute('ALTER TABLE runtime_tasks DROP COLUMN error_code')
    before = path.read_bytes()
    assert TaskStore(str(path)).tasks_for_scope(task_scope_id='scope')[0]['task_id'] == 'legacy'
    assert TaskStore(str(path)).summary()['total'] == 1
    assert path.read_bytes() == before
    assert store.get_task('legacy')['metadata']['task_scope_id'] == 'scope'


def test_runtime_cleanup_does_not_cancel_durable_work_or_fresh_heartbeat(tmp_path, monkeypatch):
    store = TaskStore(str(tmp_path / 'tasks.sqlite3'))
    store.upsert_task('durable', 'fake', 'test', metadata={'adapter': 'durable_process'})
    store.upsert_task('child', 'fake', 'test', metadata={'durable_parent_task_id': 'durable'})
    store.upsert_task('legacy', 'fake', 'test', status='running')
    stale = store.stale_tasks(running_seconds=-1, queued_seconds=-1)
    store.heartbeat('legacy')
    monkeypatch.setattr(store, 'stale_tasks', lambda **_: stale)
    assert store.cleanup_stale_tasks()['cleaned_count'] == 0
    assert store.get_task('durable')['status'] == 'queued'
    assert store.get_task('child')['status'] == 'queued'
    assert store.get_task('legacy')['status'] == 'running'


def test_result_association_cannot_overwrite_cancellation_or_new_artifact(tmp_path):
    store = TaskStore(str(tmp_path / 'tasks.sqlite3'))
    snapshot = store.upsert_task('task', 'fake', 'test', metadata={'attempt_id': 'first'})
    store.cancel_task('task')
    assert not store.record_result_association(snapshot, {'status': 'recorded'})
    assert 'research_recording' not in store.get_task('task')['metadata']


def test_public_queries_and_waits_have_no_business_writes(tmp_path, monkeypatch):
    import hashlib
    import pytest
    from runtime import research_ledger
    for key, directory in (('KR_LOG_DIR', 'logs'), ('KR_STATE_DIR', 'state'), ('KR_DATA_ROOT', 'data')):
        monkeypatch.setenv(key, str(tmp_path / directory))
    import server
    root = tmp_path / 'business'
    store = TaskStore(str(root / 'tasks.sqlite3'))
    store.upsert_task('durable-test', 'durable_content_detail', 'test', status='waiting_for_user',
        metadata={'adapter': 'durable_process', 'task_scope_id': 'scope', 'blocks_final_report': True})
    store.upsert_task('legacy', 'fake', 'test')
    store.mark_completed('legacy')
    monkeypatch.setattr(server, 'get_task_store', lambda: store)
    import runtime.task_service as tasks
    monkeypatch.setattr(tasks, 'get_task_store', lambda: store)
    def forbidden(*_a, **_kw):
        pytest.fail('a read path performed a business write or started maintenance')
    monkeypatch.setattr(store, 'initialize', forbidden)
    monkeypatch.setattr(store, 'cleanup_stale_tasks', forbidden)
    monkeypatch.setattr(tasks.TaskService, 'start_maintenance', forbidden)
    monkeypatch.setattr(research_ledger, 'record_tool_receipt', forbidden)
    monkeypatch.setattr(research_ledger, 'link_tool_result', forbidden)
    before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob('*') if p.is_file()}
    assert server.get_task_status(task_id='durable-test')['status'] == 'waiting_for_user'
    assert server.get_task_status(task_id='legacy')['task']['status'] == 'completed'
    assert server.get_task_status()['summary']['total'] == 2
    assert server.get_task_status(task_id='summary')['stale_cleanup']['cleaned_count'] == 0
    assert server._task_status_compact()['stale_cleanup']['cleaned_count'] == 0
    assert server.get_task_status(task_id='durable-test', wait=True, max_wait_s=.01)['wait']['status'] == 'timeout'
    assert server.get_task_status(task_scope_id='scope', wait=True, max_wait_s=.01)['wait']['status'] == 'timeout'
    assert server.process_local_evidence(task_id='durable-test', research_task_id='kr-research-read-only')['status'] == 'waiting_for_user'
    after = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob('*') if p.is_file()}
    assert after == before
    assert not (root / 'task_artifacts').exists()


def test_managed_history_cannot_hide_legacy_cleanup(tmp_path):
    import json
    store = TaskStore(str(tmp_path / 'tasks.sqlite3'))
    store.initialize()
    with store._connect() as conn:
        conn.executemany("INSERT INTO runtime_tasks(task_id,task_type,platform,status,created_at,updated_at,metadata_json) VALUES (?,'fixture','test','queued',?,?,?)",
            [(f'durable-{i}', 1, 1, json.dumps({'adapter': 'durable_process'})) for i in range(205)]
            + [('legacy-late', 2, 2, '{}')])
    cleaned = store.cleanup_stale_tasks()
    assert [row['task_id'] for row in cleaned['cleaned']] == ['legacy-late']
    assert store.get_task('durable-0')['status'] == 'queued'
