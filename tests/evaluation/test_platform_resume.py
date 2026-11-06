"""用真实持久任务进程检查平台恢复钩子、取消与研究归属。"""
import json
import time
import pytest
from kr_core.registry import registry
from runtime.task_service import TaskService
from runtime.tasks import TaskStore


@pytest.fixture
def waiting(tmp_path,monkeypatch):
    monkeypatch.setenv("KR_TASK_TEST_MODE","1")
    monkeypatch.setenv("KR_DATA_ROOT",str(tmp_path/"data"))
    monkeypatch.setenv("KR_STATE_DIR",str(tmp_path/"state"))
    monkeypatch.setenv("KR_RUNTIME_LEASE_DB_PATH",str(tmp_path/"leases.sqlite3"))
    service=TaskService(TaskStore(str(tmp_path/"tasks.sqlite3")))
    task_id="durable-waiting"
    from runtime.research_ledger import open_task
    scope={"research_task_id":open_task(objective="resume fixture",budget="fast",considered=[])["research_task_id"]}
    service.store.upsert_task(task_id,"durable_fixture_echo","test",status="waiting_for_user",
        metadata={**scope,"adapter":"durable_process","deadline_at":time.time()+20})
    folder=service.root/task_id
    folder.mkdir(parents=True)
    (folder/"input.json").write_text(json.dumps({"operation":"fixture_echo","payload":{"value":"resumed"},
        "timeout_s":600,"scope":scope}),encoding="utf-8")
    yield service,task_id,scope
    service.stop_maintenance()


def test_confirmed_login_resumes_once_with_remaining_deadline_and_same_research(waiting,monkeypatch):
    service,original,scope=waiting
    monkeypatch.setattr(registry,"interaction_status",lambda _:"ready")
    service.maintain_once()
    first=service.result(original)
    successor=first["task"]["task_id"]
    assert successor!=original and first["resumed_from"]==original
    assert service.result(original)["task"]["task_id"]==successor
    job=json.loads((service.root/successor/"input.json").read_text(encoding="utf-8"))
    assert 0<job["timeout_s"]<=20 and job["scope"]==scope
    end=time.monotonic()+10
    while time.monotonic()<end:
        row=service.result(original)
        if row["status"] == "completed" and row.get('research_receipt', {}).get('status') == 'recorded':
            break
        time.sleep(.05)
    assert row["result"]["value"]=="resumed"
    from runtime.research_ledger import read_task
    receipts=read_task(task_id=scope["research_task_id"])["tool_receipts"]
    assert any(r["trace_id"]==successor+":result" for r in receipts)


def test_waiting_login_holds_no_worker_and_does_not_resubmit(waiting,monkeypatch):
    service,original,_=waiting
    monkeypatch.setattr(registry,"interaction_status",lambda _:"waiting")
    monkeypatch.setattr(service,"submit",lambda *_a,**_k:pytest.fail("must not hold or launch worker"))
    service.maintain_once()
    assert service.result(original)["status"]=="waiting_for_user"


def test_expired_original_deadline_cannot_be_reset_by_resume(waiting,monkeypatch):
    service,original,_=waiting
    service.store.mark_waiting_for_user(original,{"deadline_at":time.time()-1})
    monkeypatch.setattr(service,"submit",lambda *_a,**_k:pytest.fail("expired work restarted"))
    assert service.result(original)["status"]=="waiting_for_user"
    service.maintain_once()
    assert service.result(original)["status"]=="failed"
    assert service.store.get_task(original)["error_code"]=="deadline_exceeded"


def test_cancel_original_follows_continuation(waiting):
    service,original,_=waiting
    service.store.upsert_task("durable-child","fixture","test")
    service.store.mark_skipped(original,"continued",metadata={"continuation_task_id":"durable-child"})
    assert service.cancel(original)["task_id"]=="durable-child"
    assert service.result(original)["status"]=="cancelled"


def test_reading_never_probes_resumes_or_records(waiting, monkeypatch):
    service, original, _ = waiting
    before = service.store.get_task(original)
    monkeypatch.setattr(registry, 'interaction_status', lambda _: pytest.fail('query probed browser'))
    monkeypatch.setattr(service, 'retry', lambda *_a, **_k: pytest.fail('query restarted work'))
    monkeypatch.setattr(service, 'record_completion', lambda *_a, **_k: pytest.fail('query wrote research'))
    for _ in range(3):
        assert service.result(original)['status'] == 'waiting_for_user'
    assert service.store.get_task(original) == before


@pytest.mark.parametrize('state', ['expired', 'blocked', 'none', 'waiting'])
def test_unconfirmed_interaction_never_authorizes_reexecution(waiting, monkeypatch, state):
    service, original, _ = waiting
    monkeypatch.setattr(registry, 'interaction_status', lambda _: state)
    monkeypatch.setattr(service, 'retry', lambda *_a, **_k: pytest.fail('unconfirmed login restarted work'))
    service.maintain_once()
    assert service.store.get_task(original)['status'] == 'waiting_for_user'


def test_runtime_resumes_without_queries_and_is_singleton_per_database(waiting, monkeypatch):
    service, original, scope = waiting
    from runtime.research_workspace import ResearchRecords
    monkeypatch.setattr(registry, 'interaction_status', lambda _: 'ready')
    stop = service.start_maintenance(interval_s=.03)
    assert TaskService(service.store).start_maintenance() is stop
    end = time.monotonic() + 10
    while time.monotonic() < end:
        receipts = ResearchRecords().snapshot(scope['research_task_id']).get('tool_receipts', [])
        if any(r['status'] == 'ok' for r in receipts):
            break
        time.sleep(.03)
    else:
        pytest.fail('runtime failed to resume and archive without result queries')
    successor = service.store.get_task(original)['metadata']['continuation_task_id']
    assert service.store.get_task(successor)['status'] == 'completed'
    assert len([r for r in service.store.managed_tasks(adapter='durable_process') if r['task_id'] != original]) == 1


def test_runtime_expires_and_archives_without_queries(waiting, monkeypatch):
    service, original, scope = waiting
    from runtime.research_workspace import ResearchRecords
    service.store.mark_waiting_for_user(original, {'deadline_at': time.time() + .08})
    monkeypatch.setattr(registry, 'interaction_status', lambda _: 'blocked')
    service.start_maintenance(interval_s=.03)
    end = time.monotonic() + 3
    while time.monotonic() < end:
        receipts = ResearchRecords().snapshot(scope['research_task_id']).get('tool_receipts', [])
        if any(r['failure_code'] == 'deadline_exceeded' for r in receipts):
            break
        time.sleep(.03)
    else:
        pytest.fail('waiting deadline depended on a reader')
    assert service.store.get_task(original)['status'] == 'failed'
    assert receipts[-1]['status'] == 'failed'


def test_cancel_during_resume_does_not_leave_successor_running(waiting, monkeypatch):
    service, original, _ = waiting
    monkeypatch.setattr(registry, 'interaction_status', lambda _: 'ready')
    def retry(*_a, **_kw):
        service.store.cancel_task(original)
        return service.store.upsert_task('durable-racing-child', 'fixture', 'test')
    monkeypatch.setattr(service, 'retry', retry)
    service.maintain_once()
    assert service.store.get_task(original)['status'] == 'cancelled'
    assert service.store.get_task('durable-racing-child')['status'] == 'cancelled'


def test_legacy_wait_receipt_does_not_hide_final_failure(waiting):
    service, original, scope = waiting
    from runtime.research_ledger import record_tool_receipt
    record_tool_receipt(task_id=scope['research_task_id'], trace_id=original + ':result',
        tool='get_content_detail', status='partial', receipt_kind='background_completion')
    service.store.mark_waiting_for_user(original, {'deadline_at': time.time() - 1})
    service.maintain_once()
    result = service.result(original)
    assert result['status'] == 'failed'
    assert result['research_receipt']['receipt']['status'] == 'failed'
    assert result['research_receipt']['receipt']['failure_code'] == 'deadline_exceeded'
