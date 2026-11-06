import time

import pytest

from runtime.task_service import TaskService
from runtime.tasks import TaskStore


def await_terminal(service, task_id, timeout=12):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        row = service.result(task_id)
        if row["status"] not in {"queued", "running"}:
            task = service.store.get_task(task_id) or {}
            if not (task.get('metadata') or {}).get('research_task_id') or row.get('research_receipt', {}).get('status') in {'recorded', 'recording_failed'}:
                return row
        time.sleep(.04)
    pytest.fail("isolated worker did not terminate")


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.setenv("KR_TASK_TEST_MODE", "1")
    monkeypatch.setenv("KR_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setenv("KR_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("KR_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv('KR_EVIDENCE_ROOT', str(tmp_path / 'assets'))
    instance = TaskService(TaskStore(str(tmp_path / "tasks.sqlite3")))
    yield instance
    instance.stop_maintenance()


def test_process_result_idempotency_and_integrity(service):
    start = time.monotonic()
    row = service.submit("fixture_echo", {"value": "same"}, timeout_s=10)
    assert time.monotonic() - start < 2
    result = await_terminal(service, row["task_id"])
    assert result["status"] == "completed"
    assert result["result"]["value"] == "same"
    assert service.submit("fixture_echo", {"value": "same"}, timeout_s=10)["task_id"] == row["task_id"]
    from pathlib import Path
    artifact = Path(service.store.get_task(row["task_id"])["result_path"])
    artifact.write_text("{}", encoding="utf-8")
    assert service.result(row["task_id"])["status"] == "artifact_corrupt"


def test_stage_handoff_exposes_body_and_keeps_collection_capacity(service):
    slow = [service.submit("fixture_staged", {"value": f"body-{i}", "enhancement_sleep": 3}, timeout_s=15) for i in range(2)]
    end = time.monotonic() + 10
    while time.monotonic() < end:
        states = [service.result(row["task_id"]) for row in slow]
        if all(state.get("partial_result") and "result" in state for state in states):
            break
        time.sleep(.03)
    else:
        pytest.fail("base evidence was not exposed before enhancement")
    assert all(state["result"]["value"].startswith("body-") for state in states)
    quick = service.submit("fixture_echo", {"value": "collection-is-free"}, timeout_s=10)
    assert await_terminal(service, quick["task_id"], timeout=2)["status"] == "completed"
    assert any(service.result(row["task_id"])["status"] in {"queued", "running"} for row in slow)
    for row in slow:
        assert await_terminal(service, row["task_id"])["status"] == "completed"


def test_enhancement_failure_preserves_committed_body(service):
    row = service.submit("fixture_staged", {"value": "preserve-body", "fail_enhancement": True}, timeout_s=10)
    result = await_terminal(service, row["task_id"])
    assert result["status"] == "failed"
    assert result["partial_result"] is True
    assert result["result"]["value"] == "preserve-body"


def test_resource_wait_releases_slots_and_resumes_same_task(service, monkeypatch, tmp_path):
    from runtime import leases
    coordinator = leases.RuntimeLeaseCoordinator(tmp_path / "leases.sqlite3")
    monkeypatch.setenv("KR_RUNTIME_LEASE_DB_PATH", str(tmp_path / "leases.sqlite3"))
    monkeypatch.setattr(leases, "_COORDINATOR", coordinator)
    from runtime import resource_concurrency
    monkeypatch.setattr(resource_concurrency, "get_runtime_lease_coordinator", lambda: coordinator)
    held = coordinator.acquire_bounded("runtime_resource", "local_heavy", limit=1, owner={"client_id": "fixture-holder"}, ttl_s=60)
    assert held.acquired
    rows = [service.submit("fixture_staged", {"value": f"waiting-{i}", "enhancement_resource": "asr_cpu"}, timeout_s=20) for i in range(2)]
    end = time.monotonic() + 12
    while time.monotonic() < end:
        if all((service.store.get_task(row["task_id"])["metadata"]).get("phase") == "waiting_resource" for row in rows):
            break
        time.sleep(.04)
    else:
        pytest.fail("worker did not yield its resource slot")
    for row in rows:
        assert service.result(row["task_id"])["result"]["value"].startswith("waiting-")
    light = service.submit("fixture_staged", {"value": "light-enhancement"}, timeout_s=15)
    assert await_terminal(service, light["task_id"], timeout=5)["status"] == "completed"
    # 等待期间取消一个任务；另一个仍用同一task_id自动续跑。
    service.cancel(rows[0]["task_id"])
    coordinator.release(held.lease_id)
    assert await_terminal(service, rows[1]["task_id"])["status"] == "completed"
    assert service.result(rows[0]["task_id"])["status"] == "cancelled"
    end = time.monotonic() + 3
    while service._enhancement_dispatcher is not None and time.monotonic() < end:
        time.sleep(.04)
    assert service._enhancement_dispatcher is None


def test_cancel_cannot_be_reopened_by_stage_handoff(service):
    store = service.store
    store.upsert_task("handoff", "fixture", "test", metadata={"attempt_id": "current"})
    assert store.claim_queued("handoff", {})
    store.cancel_task("handoff")
    assert not store.queue_enhancement("handoff", "current", result_path="late.json", artifact_sha256="late")
    assert store.get_task("handoff")["status"] == "cancelled"


def test_stale_worker_cannot_claim_new_attempt(service):
    store = service.store
    store.upsert_task("new-attempt", "fixture", "test", metadata={"attempt_id": "new"})
    assert not store.claim_queued("new-attempt", {"attempt_id": "old"})
    assert store.get_task("new-attempt")["status"] == "queued"


def test_late_worker_cannot_update_new_execution(service):
    store = service.store
    store.upsert_task("execution", "fixture", "test", metadata={"attempt_id": "attempt"})
    assert store.claim_queued("execution", {"attempt_id": "attempt", "execution_id": "first"})
    assert store.queue_resource_wait("execution", "attempt", "local_heavy")
    assert store.claim_queued("execution", {"attempt_id": "attempt", "execution_id": "second"})
    old = {"attempt_id": "attempt", "execution_id": "first", "phase": "late"}
    store.heartbeat("execution", old)
    store.mark_completed("execution", result_path="old.json", metadata=old)
    current = store.get_task("execution")
    assert current["status"] == "running"
    assert current["metadata"]["execution_id"] == "second"
    assert not current["result_path"]


def test_process_input_and_artifact_never_persist_access_token(service):
    import hashlib
    marker = "PRIVATE_XSEC_CANARY"
    url = "https://www.xiaohongshu.com/explore/fixture?xsec_token=" + marker
    row = service.submit("fixture_echo", {"value": {"url": url, "fetch_url": url}, "required_token_sha256": hashlib.sha256(marker.encode()).hexdigest()}, timeout_s=10)
    result = await_terminal(service, row["task_id"])
    assert result["status"] == "completed"
    assert result["result"]["token_received"] is True
    folder = service.root / row["task_id"]
    assert all(marker not in path.read_text(encoding="utf-8") for path in folder.glob("*.json"))
    assert result["result"]["value"]["url"] == "https://www.xiaohongshu.com/explore/fixture"


def test_hard_deadline_and_explicit_retry(service):
    row = service.submit("fixture_sleep", {"sleep": 3}, timeout_s=.4)
    result = await_terminal(service, row["task_id"])
    assert result["status"] == "failed"
    assert service.store.get_task(row["task_id"])["error_code"] == "deadline_exceeded"
    retry = service.retry(row["task_id"])
    assert retry["task_id"] != row["task_id"]
    import json
    original = json.loads((service.root / row["task_id"] / "input.json").read_text(encoding="utf-8"))
    successor = json.loads((service.root / retry["task_id"] / "input.json").read_text(encoding="utf-8"))
    assert successor["billing_scope_id"] == original["billing_scope_id"]
    await_terminal(service, retry["task_id"])


def test_cancel_and_restart_recovery(service):
    row = service.submit("fixture_sleep", {"sleep": 3, "value": "cancel"}, timeout_s=10)
    service.store.cancel_task(row["task_id"])
    time.sleep(.4)
    assert service.result(row["task_id"])["status"] == "cancelled"
    service.store.upsert_task("durable-abandoned", "durable_fixture", "test", metadata={"adapter": "durable_process", "owner_pid": 0})
    assert "durable-abandoned" in service.recover()["interrupted"]
    assert service.store.mark_completed("durable-abandoned")["status"] == "interrupted"


def test_terminal_heartbeat_does_not_change_committed_metadata(service):
    service.store.upsert_task("terminal", "fixture", "test")
    service.store.mark_completed("terminal", metadata={"phase": "completed"})
    service.store.heartbeat("terminal", {"phase": "late"})
    assert service.store.get_task("terminal")["metadata"]["phase"] == "completed"


def test_independent_stores_merge_metadata_and_cancel_wins(service):
    from concurrent.futures import ThreadPoolExecutor
    second = TaskStore(service.store.db_path)
    service.store.upsert_task("concurrent", "fixture", "test")
    def update(index):
        store = service.store if index % 2 else second
        store.heartbeat("concurrent", {f"worker_{index}": True})
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(update, range(24)))
    assert all(second.get_task("concurrent")["metadata"][f"worker_{i}"] for i in range(24))
    second.cancel_task("concurrent")
    service.store.mark_completed("concurrent", result_path="late.json")
    row = second.get_task("concurrent")
    assert row["status"] == "cancelled"
    assert not row["result_path"]


def test_recruitment_gate_storage_error_does_not_allow_collection(monkeypatch):
    from runtime import recruitment_governance
    def unavailable():
        raise OSError("fixture storage failure")
    monkeypatch.setattr(recruitment_governance, "_get_conn", unavailable)
    result = recruitment_governance.check_search_gate("BOSS直聘")
    assert result["allowed"] is False
    assert result["reason_code"] == "recruitment_gate_unavailable"


def test_reused_pid_does_not_keep_old_task_alive(service,monkeypatch):
    from runtime import task_service
    monkeypatch.setattr(task_service,'process_identity',lambda pid:'same-pid-new-generation')
    service.store.upsert_task('durable-old-generation','durable_fixture','test',metadata={
        'adapter':'durable_process','owner_pid':123,'owner_identity':'same-pid-old-generation','deadline_at':time.time()+60})
    assert 'durable-old-generation' in service.recover()['interrupted']


def test_background_result_is_archived_without_polling_and_is_task_scoped(service, monkeypatch):
    from runtime.research_ledger import open_task, read_task
    monkeypatch.setenv('KR_STATE_DIR', str(service.root.parent / 'state'))
    first = open_task(objective='one', budget='fast', considered=[])
    second = open_task(objective='two', budget='fast', considered=[])
    row = service.submit('fixture_echo', {'value': 'evidence'}, scope={'research_task_id': first['research_task_id']})
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        receipts = read_task(task_id=first['research_task_id']).get('tool_receipts', [])
        if receipts:
            break
        time.sleep(.05)
    assert receipts, 'completion must attach itself, without get_task_status polling'
    for _ in range(2):
        service.result(row['task_id'])
    assert len(read_task(task_id=first['research_task_id'])['tool_receipts']) == 1
    assert read_task(task_id=second['research_task_id'])['tool_receipts'] == []
    other = service.submit('fixture_echo', {'value': 'evidence'}, scope={'research_task_id': second['research_task_id']})
    assert other['task_id'] != row['task_id']
    await_terminal(service, other['task_id'])


def test_parent_stop_only_interrupts_its_owned_children(service):
    service.store.upsert_task('child', 'asr', 'B站', status='running', metadata={'durable_parent_task_id': 'durable-parent', 'phase': 'transcribe'})
    service.store.upsert_task('unrelated', 'asr', 'B站', status='running', metadata={'durable_parent_task_id': 'durable-other'})
    context = service._failure_context('durable-parent')
    assert context['child_tasks'][0]['phase'] == 'transcribe'
    service._settle_children('durable-parent')
    assert service.store.get_task('child')['status'] == 'interrupted'
    assert service.store.get_task('unrelated')['status'] == 'running'


def test_real_document_worker_projects_fixed_evidence_into_research_files(service, monkeypatch, tmp_path):
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
    from runtime.optional_media import file_sha
    from runtime.research_ledger import open_task, read_task
    from runtime.research_workspace import ResearchRecords, task_metrics, task_rounds
    monkeypatch.setenv('KR_STATE_DIR', str(tmp_path / 'state'))
    monkeypatch.setenv('KR_EVIDENCE_ROOT', str(tmp_path / 'assets'))
    path = tmp_path / 'text.pdf'
    writer = PdfWriter()
    page = writer.add_blank_page(300, 300)
    font = DictionaryObject({NameObject('/Type'): NameObject('/Font'), NameObject('/Subtype'): NameObject('/Type1'), NameObject('/BaseFont'): NameObject('/Helvetica')})
    page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): writer._add_object(font)})})
    stream = DecodedStreamObject()
    stream.set_data(b'BT /F1 12 Tf 10 100 Td (Evidence END-27) Tj ET')
    page[NameObject('/Contents')] = writer._add_object(stream)
    writer.write(path)
    task = open_task(objective='fixed document', budget='fast', considered=[])
    row = service.submit('optional_media', {'input_path': str(path), 'approved_input_root': str(tmp_path), 'input_sha256': file_sha(path), 'component': 'auto'}, scope={'research_task_id': task['research_task_id']})
    result = await_terminal(service, row['task_id'])
    assert result['status'] == 'completed'
    records = ResearchRecords()
    files = records.files(task['research_task_id'])
    assert any(item['type'] == 'fixed_evidence' and item['readable'] for item in files['files'])
    payload = read_task(task_id=task['research_task_id'])
    assert task_metrics(payload)['tool_calls'] == 0
    assert sum(item['tool_count'] for item in task_rounds(payload)) == 0
    # 归档故障保留已完成业务；修复后可重复补记。
    import runtime.research_ledger as ledger
    original = ledger.record_tool_receipt
    monkeypatch.setattr(ledger, 'record_tool_receipt', lambda **kw: (_ for _ in ()).throw(OSError('fixture')))
    # 故障补偿是执行动作，普通结果查询不再负责补记。
    service.store.record_result_association(service.store.get_task(row['task_id']),
        {'status': 'recording_failed', 'response': {'status': 'recording_failed'}})
    assert service.record_completion(row['task_id'])['research_receipt']['status'] == 'recording_failed'
    assert service.store.get_task(row['task_id'])['status'] == 'completed'
    monkeypatch.setattr(ledger, 'record_tool_receipt', original)
    service.recover()
    assert len(read_task(task_id=task['research_task_id'])['tool_receipts']) == 1


def test_failed_background_task_is_terminal_and_stays_visible(service, monkeypatch, tmp_path):
    from runtime.research_ledger import open_task, record_tool_receipt, close_task
    import runtime.tasks as tasks
    monkeypatch.setenv('KR_STATE_DIR', str(tmp_path / 'state'))
    monkeypatch.setattr(tasks, 'get_task_store', lambda: service.store)
    task = open_task(objective='terminal failure', budget='fast', considered=[])
    service.store.upsert_task('durable-failed-fixture', 'durable_content_detail', 'test')
    service.store.mark_failed('durable-failed-fixture', 'fixture', error_code='deadline_exceeded')
    record_tool_receipt(task_id=task['research_task_id'], trace_id='submit', tool='get_content_detail', status='partial', result={'task': {'task_id': 'durable-failed-fixture'}})
    closed = close_task(task_id=task['research_task_id'], ecology_outcomes=[], stop_rationale='已明确失败，换来源', key_claims=[], quality_status='pass')
    assert closed['closeout']['pending_background_tasks'] == []
    assert closed['closeout']['unsuccessful_background_tasks'][0]['error_code'] == 'deadline_exceeded'


def test_recover_backfills_legacy_scope_only_from_exact_submission_receipt(service, monkeypatch, tmp_path):
    from runtime.research_ledger import open_task, read_task, record_tool_receipt
    monkeypatch.setenv('KR_STATE_DIR', str(tmp_path / 'state'))
    task = open_task(objective='legacy task', budget='fast', considered=[])
    unrelated = open_task(objective='same title cannot imply association', budget='fast', considered=[])
    row = service.submit('fixture_echo', {'value': 'legacy artifact'})
    await_terminal(service, row['task_id'])
    record_tool_receipt(task_id=task['research_task_id'], trace_id='legacy-submit', tool='get_content_detail', status='partial', result={'task': {'task_id': row['task_id']}})
    assert len(read_task(task_id=task['research_task_id'])['tool_receipts']) == 1
    service.recover()
    service.recover()
    receipts = read_task(task_id=task['research_task_id'])['tool_receipts']
    assert len(receipts) == 2 and receipts[-1]['receipt_kind'] == 'background_completion'
    assert read_task(task_id=unrelated['research_task_id'])['tool_receipts'] == []


@pytest.mark.parametrize('platform', ['YouTube', '知乎', '小红书', 'B站', '微信公众号', '开放网页'])
def test_every_detail_platform_archives_without_polling(service, monkeypatch, tmp_path, platform):
    from runtime.research_ledger import open_task, read_task
    monkeypatch.setenv('KR_STATE_DIR', str(tmp_path/'state'))
    task = open_task(objective='平台归档合同', budget='fast', considered=[])
    row = service.submit('fixture_echo', {'value': '正文 ' + platform, 'platform': platform}, scope={'research_task_id': task['research_task_id']})
    # 不调用 service.result；等待监视器自己写 completion。
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        receipts = read_task(task_id=task['research_task_id'])['tool_receipts']
        if receipts:
            break
        time.sleep(.05)
    assert len(receipts) == 1 and receipts[0]['receipt_kind'] == 'background_completion'
    service.result(row['task_id'])
    assert len(read_task(task_id=task['research_task_id'])['tool_receipts']) == 1


def test_no_artifact_deadline_also_records_completion(service, monkeypatch, tmp_path):
    from runtime.research_ledger import open_task, read_task
    monkeypatch.setenv('KR_STATE_DIR', str(tmp_path/'state'))
    task = open_task(objective='失败任务留痕', budget='fast', considered=[])
    row = service.submit('fixture_sleep', {'sleep': 5}, timeout_s=.3, scope={'research_task_id': task['research_task_id']})
    result = await_terminal(service, row['task_id'])
    assert result['status'] == 'failed' and 'result' not in result
    receipts = read_task(task_id=task['research_task_id'])['tool_receipts']
    assert len(receipts) == 1 and receipts[0]['failure_code'] == 'deadline_exceeded'


def test_recovery_and_child_cleanup_are_not_limited_by_recent_history(service):
    store = service.store
    store.upsert_task('durable-old', 'durable_fixture', 'test', metadata={
        'adapter': 'durable_process', 'owner_pid': 0, 'deadline_at': 0})
    store.upsert_task('old-child', 'asr', 'test', status='running',
                      metadata={'durable_parent_task_id': 'durable-old'})
    # 模拟用户已有的大量历史；旧活动任务已经不在 UI 最近记录中。
    with store._connect() as conn:
        conn.executemany("INSERT INTO runtime_tasks(task_id,task_type,platform,status,created_at,updated_at) VALUES (?, 'fixture','test','completed',?,?)",
                         [(f'history-{i}', time.time()+i, time.time()+i) for i in range(210)])
    assert 'durable-old' not in {row['task_id'] for row in store.recent_tasks(500)}
    assert service._failure_context('durable-old')['child_tasks'][0]['task_id'] == 'old-child'
    assert 'durable-old' in service.recover()['interrupted']
    assert store.get_task('old-child')['status'] == 'interrupted'


@pytest.mark.parametrize('failure', ['resource', 'executor', 'collection'])
def test_dispatch_failure_releases_capacity_and_archives_without_poll(service, monkeypatch, failure):
    from runtime import task_service, resource_concurrency
    from runtime.research_ledger import open_task, read_task
    research = open_task(objective='派发失败也要留痕', budget='fast', considered=[])
    def fail(*args, **kwargs):
        raise RuntimeError('isolated coordinator/executor failure')
    if failure == 'collection':
        monkeypatch.setattr(task_service._MONITORS, 'submit', fail)
        row = service.submit('fixture_echo', {}, scope={'research_task_id': research['research_task_id']})
        task_id = row['task_id']
    else:
        task_id = 'durable-dispatch-failure'
        service.store.upsert_task(task_id, 'durable_content_detail', 'test', metadata={
            'research_task_id': research['research_task_id'], 'waiting_resource': 'asr_cpu'})
        if failure == 'resource':
            monkeypatch.setattr(resource_concurrency, 'resource_available', fail)
        else:
            monkeypatch.setattr(resource_concurrency, 'resource_available', lambda _: True)
            monkeypatch.setattr(task_service._ENHANCEMENT_MONITORS, 'submit', fail)
        service._queue_enhancement({'task_id': task_id, 'submitted_at': time.time(), 'timeout_s': 10}, service.root, b'{}')
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        receipts = read_task(task_id=research['research_task_id'])['tool_receipts']
        if receipts and service._enhancement_dispatcher is None:
            break
        time.sleep(.03)
    assert receipts and receipts[0]['status'] == 'failed'
    assert service.store.get_task(task_id)['status'] == 'failed'
    assert service._enhancement_active == 0 and service._enhancement_dispatcher is None


def test_cancel_queued_task_archives_without_monitor_or_poll(service):
    from runtime.research_ledger import open_task, read_task
    task = open_task(objective='排队取消', budget='fast', considered=[])
    service.store.upsert_task('cancel-pending', 'durable_content_detail', 'test',
        metadata={'research_task_id': task['research_task_id']})
    service.cancel('cancel-pending')
    receipts = read_task(task_id=task['research_task_id'])['tool_receipts']
    assert len(receipts) == 1 and receipts[0]['status'] == 'failed'


def test_result_rejects_artifact_from_another_task_even_with_matching_sha(service):
    import hashlib
    import json
    task = service.submit('fixture_echo', {'value': 'artifact identity'})
    await_terminal(service, task['task_id'])
    row = service.store.get_task(task['task_id'])
    from pathlib import Path
    path = Path(row['result_path'])
    artifact = json.loads(path.read_text(encoding='utf-8'))
    artifact['task_id'] = 'another-task'
    raw = json.dumps(artifact).encode()
    path.write_bytes(raw)
    metadata = {**row['metadata'], 'artifact_sha256': hashlib.sha256(raw).hexdigest()}
    with service.store._connect() as conn:
        conn.execute('UPDATE runtime_tasks SET metadata_json=? WHERE task_id=?', (json.dumps(metadata), task['task_id']))
    assert service.result(task['task_id'])['status'] == 'artifact_corrupt'


def test_archive_failure_is_repaired_by_runtime_without_query(service, monkeypatch):
    from runtime import research_ledger as ledger
    from runtime.research_workspace import ResearchRecords
    task = ledger.open_task(objective='runtime receipt repair', budget='fast', considered=[])
    original = ledger.link_tool_result
    failed = []
    def fail(**_kw):
        failed.append(True)
        raise OSError('fixture archive outage')
    monkeypatch.setattr(ledger, 'link_tool_result', fail)
    row = service.submit('fixture_echo', {'value': 'evidence survives'},
        scope={'research_task_id': task['research_task_id']})
    end = time.monotonic() + 5
    while time.monotonic() < end:
        current = service.store.get_task(row['task_id'])
        if current['metadata'].get('research_recording', {}).get('status') == 'recording_failed':
            break
        time.sleep(.03)
    assert failed and current['status'] == 'completed'
    monkeypatch.setattr(ledger, 'link_tool_result', original)
    service.maintain_once()
    current = service.store.get_task(row['task_id'])
    assert current['metadata']['research_recording']['status'] == 'recorded'
    assert current['status'] == 'completed'
    receipts = ResearchRecords().snapshot(task['research_task_id'])['tool_receipts']
    assert len(receipts) == 1
    assert service.result(row['task_id'])['result']['value'] == 'evidence survives'


def test_structured_link_failure_retries_only_publication(service, monkeypatch):
    from pathlib import Path
    from runtime import research_ledger as ledger
    research = ledger.open_task(objective='关联补交', budget='fast', considered=[])
    original = ledger.link_tool_result
    monkeypatch.setattr(ledger, 'link_tool_result', lambda **_: {
        'association': 'unique_source_identity', 'status': 'declared_only'})
    task = service.submit('fixture_echo', {'value': '原产物'}, scope={'research_task_id': research['research_task_id']})
    result = await_terminal(service, task['task_id'])
    assert result['status'] == 'completed'
    assert result['research_receipt']['status'] == 'recording_failed'
    path = Path(service.store.get_task(task['task_id'])['result_path'])
    before = path.read_bytes()
    monkeypatch.setattr(ledger, 'link_tool_result', original)
    service.maintain_once()
    after = service.result(task['task_id'])
    assert after['research_receipt']['status'] == 'recorded'
    assert path.read_bytes() == before
    assert len(ledger.read_task(task_id=research['research_task_id'])['tool_receipts']) == 1
    assert (path.parent/'spans.jsonl').read_text(encoding='utf-8').count('fixture_begin') == 1


def test_archive_index_failure_keeps_collected_body_and_existing_assets(service, monkeypatch):
    from dataclasses import asdict
    from types import SimpleNamespace
    from kr_core import DetailRequest, DetailResponse
    from kr_core.registry import registry
    from kr_core.evidence_store import EvidenceStore
    from kr_core.evidence_assets import EvidenceAssets
    from runtime import platform_execution
    from runtime.research_ledger import open_task, read_task
    from generic_web import url_policy
    url = 'https://example.org/archive-gap'
    calls = []
    def extract(request):
        calls.append(request.url)
        return DetailResponse.from_legacy('fixture', url, {'url': url, 'content': '必须保存的完整正文'})
    monkeypatch.setattr(platform_execution, 'ensure_bindings', lambda: None)
    monkeypatch.setattr(registry, 'detail_strategy', lambda _: SimpleNamespace(extract=extract))
    monkeypatch.setattr(url_policy, 'validate_detail_target', lambda *a: None)
    monkeypatch.setattr(EvidenceStore, 'append', lambda *a: (_ for _ in ()).throw(OSError('index unavailable')))
    research = open_task(objective='归档缺口不丢正文', budget='fast', considered=[])
    task = service.submit('content_detail', asdict(DetailRequest(url=url, platform='fixture')),
        scope={'research_task_id': research['research_task_id']})
    result = await_terminal(service, task['task_id'])
    assert result['status'] == 'failed'
    assert service.store.get_task(task['task_id'])['error_code'] == 'evidence_archive_incomplete'
    assert result['result']['content'] == '必须保存的完整正文'
    assert result['result']['evidence_storage_gaps'] == [{'part': 'repository', 'reason': 'OSError'}]
    ref = result['result']['evidence_assets'][0]
    assert EvidenceAssets().fetch(ref['version_id'])['text'] == result['result']['content']
    assert read_task(task_id=research['research_task_id'])['tool_receipts'][0]['status'] == 'failed'
    service.recover()
    assert calls == [url]


@pytest.mark.parametrize('field', ['schema', 'execution_id', 'source'])
def test_artifact_commit_identity_is_verified_separately_from_live_executor(service, field):
    import hashlib
    import json
    from pathlib import Path
    # 先等待执行侧收口，避免把独立后台写入误判成 result() 的副作用。
    import threading
    settled = threading.Event()
    record_completion = service.record_completion
    def record_and_signal(*args, **kwargs):
        try:
            return record_completion(*args, **kwargs)
        finally:
            settled.set()
    service.record_completion = record_and_signal
    task = service.submit('fixture_echo', {'value': 'identity'})
    await_terminal(service, task['task_id'])
    assert settled.wait(2)
    service.stop_maintenance()
    row = service.store.get_task(task['task_id'])
    path = Path(row['result_path'])
    artifact = json.loads(path.read_bytes())
    if field == 'source':
        artifact['identity']['source'] = 'different-source'
    else:
        artifact[field] = 'different-identity'
    raw = json.dumps(artifact).encode()
    path.write_bytes(raw)
    metadata = {**row['metadata'], 'artifact_sha256': hashlib.sha256(raw).hexdigest()}
    with service.store._connect() as conn:
        conn.execute('UPDATE runtime_tasks SET metadata_json=? WHERE task_id=?', (json.dumps(metadata), task['task_id']))
    before = service.store.get_task(task['task_id'])
    assert service.result(task['task_id'])['status'] == 'artifact_corrupt'
    assert service.store.get_task(task['task_id']) == before


def test_legacy_recording_is_backfilled_without_new_execution_or_receipt(service):
    from runtime import research_ledger as ledger
    research = ledger.open_task(objective='旧补记迁移', budget='fast', considered=[])
    task = service.submit('fixture_echo', {'value': 'legacy'}, scope={'research_task_id': research['research_task_id']})
    await_terminal(service, task['task_id'])
    # 先完成测试准备的注记；只读结果可在注记之前看到已提交的 ledger 回执。
    service.record_completion(task['task_id'])
    row = service.store.get_task(task['task_id'])
    old = {**row['metadata']['research_recording']}
    old.pop('handoff_version')
    service.store.record_result_association(row, old)
    with ledger._ledger_guard(research['research_task_id']):
        payload = ledger._read(research['research_task_id'])
        payload['tool_receipts'][0].pop('candidate_association')
        payload['tool_receipts'][0].pop('submission_sha256')
        ledger._write(payload)
    assert service.result(task['task_id'])['research_receipt']['status'] == 'receipt_only'
    pending = service.store.managed_tasks(adapter='durable_process', recording_pending=True)
    assert [row['task_id'] for row in pending] == [task['task_id']]
    service.maintain_once()
    result = service.result(task['task_id'])
    assert result['status'] == 'completed' and result['research_receipt']['status'] == 'recorded'
    assert len(ledger.read_task(task_id=research['research_task_id'])['tool_receipts']) == 1
    assert service.store.managed_tasks(adapter='durable_process', recording_pending=True) == []


def test_execution_completion_does_not_claim_requested_coverage_is_satisfied(service, monkeypatch):
    from dataclasses import asdict
    from types import SimpleNamespace
    from kr_core import DetailRequest, DetailResponse
    from kr_core.registry import registry
    from runtime import platform_execution, research_ledger as ledger
    from generic_web import url_policy
    url = 'https://example.org/missing-comments'
    monkeypatch.setattr(platform_execution, 'ensure_bindings', lambda: None)
    monkeypatch.setattr(url_policy, 'validate_detail_target', lambda *a: None)
    monkeypatch.setattr(registry, 'detail_strategy', lambda _: SimpleNamespace(extract=lambda request:
        DetailResponse.from_legacy('fixture', url, {'url': url, 'content': '可独立使用的正文', 'comments': []})))
    research = ledger.open_task(objective='必要评论缺口', budget='fast', considered=[])
    task = service.submit('content_detail', asdict(DetailRequest(url=url, platform='fixture',
        options={'include_comments': True})), scope={'research_task_id': research['research_task_id']})
    result = await_terminal(service, task['task_id'])
    assert result['status'] == 'failed' and result['partial_result'] is True
    assert result['task']['error_code'] == 'required_evidence_incomplete'
    assert result['result']['content_coverage']['required_gaps'] == ['comments']
    assert result['research_receipt']['receipt']['status'] == 'failed'
    assert len(ledger.read_task(task_id=research['research_task_id'])['tool_receipts']) == 1


def test_legacy_receipt_failure_keeps_exact_binding_for_runtime_repair(service, monkeypatch):
    from runtime import research_ledger as ledger
    research = ledger.open_task(objective='legacy repair', budget='fast', considered=[])
    row = service.submit('fixture_echo', {'value': 'legacy'})
    await_terminal(service, row['task_id'])
    ledger.record_tool_receipt(task_id=research['research_task_id'], trace_id='legacy-submit',
        tool='get_content_detail', status='partial', result={'task': {'task_id': row['task_id']}})
    original = ledger.link_tool_result
    monkeypatch.setattr(ledger, 'link_tool_result', lambda **_: (_ for _ in ()).throw(OSError('fixture')))
    service.recover()
    assert service.store.get_task(row['task_id'])['metadata']['research_recording']['status'] == 'recording_failed'
    monkeypatch.setattr(ledger, 'link_tool_result', original)
    service.maintain_once()
    assert service.store.get_task(row['task_id'])['metadata']['research_recording']['status'] == 'recorded'
    assert len(ledger.read_task(task_id=research['research_task_id'])['tool_receipts']) == 2


def test_missing_or_invalid_artifact_reads_do_not_repair_or_change_task(service):
    from pathlib import Path
    import hashlib
    import json
    task = service.submit('fixture_echo', {})
    await_terminal(service, task['task_id'])
    current = service.store.get_task(task['task_id'])
    path = Path(current['result_path'])
    path.unlink()
    assert service.result(task['task_id'])['status'] == 'artifact_missing'
    assert service.store.get_task(task['task_id']) == current
    raw = json.dumps([]).encode()
    path.write_bytes(raw)
    with service.store._connect() as conn:
        metadata = {**current['metadata'], 'artifact_sha256': hashlib.sha256(raw).hexdigest()}
        conn.execute('UPDATE runtime_tasks SET metadata_json=? WHERE task_id=?', (json.dumps(metadata), task['task_id']))
    current = service.store.get_task(task['task_id'])
    assert service.result(task['task_id'])['status'] == 'artifact_corrupt'
    assert service.store.get_task(task['task_id']) == current


def test_xhs_real_stages_use_shared_leases_quotas_checkpoints_and_evidence(service, monkeypatch, tmp_path):
    """真实适配器/同进程任务执行；只替换浏览器传输、CDN HTTP及模型边界。"""
    import json
    from kr_core import DetailRequest
    from runtime import asset_identity, leases, resource_concurrency, xhs_opencli as oc
    from runtime.paid_execution import read_usage, paid_send
    from runtime.research_ledger import open_task, read_task, record_tool_receipt, link_tool_result
    from kr_core.evidence_assets import EvidenceAssets
    from kr_core.evidence_store import EvidenceStore
    for name, value in {
        'KR_OPENCLI_STATE_DIR': str(tmp_path/'opencli'), 'KR_XHS_OPENCLI_CONTEXT_ID': 'isolated-chrome',
        'KR_XHS_OPENCLI_ADAPTER_SHA256': 'isolated-adapter', 'KR_RUNTIME_LEASE_DB_PATH': str(tmp_path/'leases.sqlite3'),
        'KR_EVIDENCE_ROOT': str(tmp_path/'assets'), 'KR_PAID_USAGE_PATH': str(tmp_path/'usage.json'),
        'KR_OPTIONAL_COMPONENT_RECEIPTS_DIR': str(tmp_path/'receipts'), 'SILICONFLOW_API_KEY': 'isolated-no-network',
    }.items():
        monkeypatch.setenv(name, value)
    # 旧的极小全局上限不能阻断免费的 OCR；真金额只归百炼。
    policy = tmp_path/'policy.json'
    policy.write_text(json.dumps({'schema':'knowledgeradar-paid-policy/v2', 'daily_limit_cny':'.000001'}), encoding='utf-8')
    monkeypatch.setenv('KR_PAID_POLICY_PATH', str(policy))
    coordinator = leases.RuntimeLeaseCoordinator(tmp_path/'leases.sqlite3')
    monkeypatch.setattr(leases, '_COORDINATOR', coordinator)
    monkeypatch.setattr(resource_concurrency, 'get_runtime_lease_coordinator', lambda: coordinator)
    monkeypatch.setattr(asset_identity, 'model_identity', lambda _: {'fixture':'asr-model-boundary'})
    note = 'abcdef123456789012345678'
    url = 'https://www.xiaohongshu.com/explore/' + note
    def transport(operation, **kwargs):
        return {'ok':True, 'data':[{'title':'隔离代表链','url':url}]} if operation == 'search' else {'ok':True,'data':{}}
    monkeypatch.setattr(oc, '_run', transport)
    research = open_task(objective='小红书底座集成', budget='fast', considered=[])
    found = oc.search('隔离代表链', 1, 'video', options={'research_task_id':research['research_task_id']})
    EvidenceStore().append_search(platform='小红书', query='隔离代表链', response=found)
    receipt = record_tool_receipt(task_id=research['research_task_id'], trace_id='search', tool='search_xiaohongshu', status='ok', result=found)['receipt']
    link_tool_result(task_id=research['research_task_id'], tool='search_xiaohongshu', result=found, receipt=receipt)
    import io
    import httpx
    import sys
    from PIL import Image
    from generic_web import url_policy
    from runtime import optional_media, optional_media_worker, component_environment
    image = io.BytesIO()
    Image.new('RGB', (30, 40), 'white').save(image, format='PNG')
    # CDN 边界提供可解码的小文件，仍由产品的真实 PyAV 校验器验证。
    import av
    import numpy as np
    video = io.BytesIO()
    with av.open(video, mode='w', format='mp4') as container:
        audio = container.add_stream('aac', rate=16000)
        audio.layout = 'mono'
        picture = container.add_stream('mpeg4', rate=2)
        picture.width = picture.height = 32
        picture.pix_fmt = 'yuv420p'
        for index in range(16):
            frame = av.AudioFrame.from_ndarray(np.zeros((1, 1024), dtype=np.float32), format='fltp', layout='mono')
            frame.sample_rate = 16000
            frame.pts = index * 1024
            for packet in audio.encode(frame):
                container.mux(packet)
        for packet in audio.encode(None):
            container.mux(packet)
        for index in range(2):
            frame = av.VideoFrame.from_ndarray(np.zeros((32, 32, 3), dtype=np.uint8), format='rgb24')
            frame.pts = index
            for packet in picture.encode(frame):
                container.mux(packet)
        for packet in picture.encode(None):
            container.mux(packet)
    video_bytes = video.getvalue()
    def collect(operation, **kwargs):
        if operation == 'inventory':
            return {'ok': True}
        assert operation == 'detail'
        held = coordinator.resource_leases('browser_site')
        assert len(held) == 1 and held[0]['metadata']['ownership'] == 'borrowed'
        with (tmp_path/'state'/'browser-calls.txt').open('a') as stream:
            stream.write('detail\n')
        return {'ok': True, 'data': {'note_id': note, 'title': '隔离代表链', 'content': '可回读正文',
            'comments': [{'native_comment_id': 'c1', 'text': '可回读评论'}], 'comment_count': 1,
            'comments_observation': {'skipped_empty': 0, 'expanders_remaining': 0},
            'completed_stages': ['body', 'comments', 'media'],
            'media_manifest': [{'type': 'image', 'url': 'https://sns-webpic.xhscdn.com/cover.png'},
                               {'type': 'video', 'url': 'https://sns-video.xhscdn.com/video.mp4'}]}}
    monkeypatch.setattr(oc, '_run', collect)
    def fetch(url, **kwargs):
        assert not coordinator.resource_leases('browser_site')
        return httpx.Response(200, request=httpx.Request('GET', url),
            content=image.getvalue() if url.endswith('.png') else video_bytes)
    monkeypatch.setattr(url_policy, 'guarded_fetch', fetch)
    def post(self, url, **kwargs):
        assert not coordinator.resource_leases('browser_site')
        return httpx.Response(200, request=httpx.Request('POST', url), json={
            'choices': [{'finish_reason': 'stop', 'message': {'content': '可回读 OCR'}}],
            'usage': {'prompt_tokens': 10, 'completion_tokens': 3, 'total_tokens': 13}})
    monkeypatch.setattr(httpx.Client, 'post', post)
    receipts = optional_media.receipt_root()
    receipts.mkdir(parents=True, exist_ok=True)
    (receipts/'qwen3-asr-0.6b.json').write_text('{}')
    monkeypatch.setattr(optional_media, 'validate_receipt', lambda _: {'python': sys.executable, 'model_root': str(receipts), 'package_versions': {}})
    monkeypatch.setattr(component_environment, 'component_environment', lambda *a, **k: {'status': 'ready'})
    def model(request):
        from kr_core.evidence_locators import transcript_locators
        assert not coordinator.resource_leases('browser_site')
        locations = transcript_locators('可回读 ASR', [{'start_s': 0, 'end_s': 1.024, 'text': '可回读 ASR'}])
        return {'status': 'acquired', 'component': 'qwen3-asr-0.6b', 'text': '可回读 ASR',
            'source_kind': 'asr', 'content_locators': locations['segments'],
            'asr_metrics': {'chunks': 1, 'audio_duration_s': 1.024}}
    monkeypatch.setattr(optional_media_worker, 'execute', model)
    held = coordinator.acquire_bounded('runtime_resource', 'local_heavy', limit=1,
        owner={'client_id':'isolated-heavy-holder'}, ttl_s=60)
    assert held.acquired
    try:
        from dataclasses import asdict
        request = DetailRequest(url=found['items'][0]['url'], platform='小红书', options={
            'include_comments':True,'include_ocr':True,'include_transcript':True})
        task = service.submit('content_detail', asdict(request), timeout_s=30,
            scope={'research_task_id':research['research_task_id']})
        deadline = time.monotonic()+15
        while time.monotonic()<deadline:
            current = service.store.get_task(task['task_id'])
            if current['metadata'].get('phase')=='waiting_resource' and service._enhancement_active==0:
                break
            if current['status']=='failed':
                pytest.fail(str(service.result(task['task_id'])))
            time.sleep(.04)
        else:
            pytest.fail('real XHS enhancement did not yield its resource slot')
        partial = service.result(task['task_id'])
        assert partial['result']['content']=='可回读正文'
        assert partial['result']['comments'][0]['text']=='可回读评论'
        assert not coordinator.resource_leases('browser_site')
        # 增强等待时，采集池与增强池的轻任务仍可推进。
        quick = service.submit('fixture_staged', {'value':'unblocked'}, timeout_s=10)
        assert await_terminal(service, quick['task_id'])['status']=='completed'
    finally:
        coordinator.release(held.lease_id)
    final = await_terminal(service, task['task_id'], timeout=15)
    assert final['status']=='completed', final
    assert final['result']['comments_metadata']['request_window_complete']
    assert final['result']['transcript_metadata']['status']=='aligned'
    assert final['result']['ocr_text']=='可回读 OCR' and final['result']['transcript']=='可回读 ASR'
    assert len(final['result']['downloaded_media'])==2
    assert (tmp_path/'state'/'browser-calls.txt').read_text().splitlines()==['detail']
    rows = [r for day in read_usage().values() for r in day.get('reservations',{}).values()]
    assert len(rows)==1 and rows[0]['provider']=='siliconflow_ocr' and rows[0]['accounted_cny']=='0'
    assets = EvidenceAssets()
    parts = {r['kind']:assets.fetch(r['version_id'])['text'] for r in final['result']['evidence_assets']}
    assert {'content','comments','media_manifest','ocr_text','transcript'} <= parts.keys()
    assert parts['content']=='可回读正文' and parts['transcript']=='可回读 ASR'
    receipts = read_task(task_id=research['research_task_id'])['tool_receipts']
    assert len(receipts)==2 and receipts[-1]['receipt_kind']=='background_completion'
    # 同一计费身份的未知结果不因更换 worker/重提而自动重发。
    with pytest.raises(RuntimeError, match='unknown outcome'):
        with paid_send('bailian', {'fixture':'unknown'}, bound=10, task_id='stable-billing'):
            raise RuntimeError('unknown outcome')
    from runtime.paid_execution import PaidAdmissionError
    with pytest.raises(PaidAdmissionError, match='already_sent_or_reserved'):
        with paid_send('bailian', {'fixture':'unknown'}, bound=10, task_id='stable-billing'):
            pytest.fail('must not send twice')


def test_maintenance_keeps_pending_sync_receipts_alive(service, monkeypatch):
    from runtime import research_ledger as ledger
    pending = {"value": True}
    checks = []
    monkeypatch.setattr(ledger, "has_pending_submissions", lambda: pending["value"])
    monkeypatch.setattr(service, "maintain_once", lambda: checks.append(time.monotonic()))
    stop = service.start_maintenance(interval_s=.01)
    end = time.monotonic() + 1
    while len(checks) < 3 and time.monotonic() < end:
        time.sleep(.01)
    assert len(checks) >= 3 and not stop.is_set()
    pending["value"] = False
    assert stop.wait(1)
