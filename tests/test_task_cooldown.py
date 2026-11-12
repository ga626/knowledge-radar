"""持久冷却必须可取消、跨实例恢复、有限重试且不自动重放未知请求。"""
from concurrent.futures import ThreadPoolExecutor
import time

import pytest

from runtime.task_cooldown import cooldown_plan
from runtime.tasks import TaskStore, compact_task_ref
from runtime.task_service import TaskService


@pytest.mark.parametrize('reason',['login_required','qr_required','request_outcome_unknown','cookie_expired','parse_failed'])
def test_login_unknown_and_parser_errors_never_auto_replay(reason):
    assert cooldown_plan({'failure_type':reason,'retry_after_s':60},operation='content_detail') is None


def test_cooldown_keeps_server_retry_after_and_bounds_local_backoff():
    assert cooldown_plan({'failure_type':'http_429','retry_after_s':1000.1},operation='content_detail',now=10)['not_before']==1011
    assert cooldown_plan({'failure_type':'platform_cooldown'},operation='content_detail',attempts=8,now=10)['not_before']==310
    assert cooldown_plan({'failure_type':'rate_limit','retry_after_s':float('nan')},operation='content_detail') is None


def test_nested_paid_unknown_prevents_discovery_replay():
    assert cooldown_plan({'failure_type':'anti_bot_verification', 'metadata':{'fallback':{'outcome_unknown':True}}},operation='platform_discovery') is None


@pytest.mark.parametrize('platform', ['zhihu','boss'])
def test_collectors_only_login_needs_human_and_risk_can_cool(platform):
    from collectors.platform.zhihu import failure
    from collectors.platform.boss import _failure
    classify = failure if platform == 'zhihu' else _failure
    assert classify('login_required')['manual_action_required']
    for reason in ('anti_bot_verification','rate_limited'):
        result=classify(reason)
        assert not result['manual_action_required']
        assert cooldown_plan(result,operation='platform_discovery')
    assert cooldown_plan(classify('request_outcome_unknown'),operation='platform_discovery') is None


def waiting(tmp_path, *, attempts=0):
    path=str(tmp_path/'tasks.sqlite3');store=TaskStore(path)
    store.upsert_task('durable-test','durable_content_detail','zhihu',metadata={'adapter':'durable_process','attempt_id':'attempt','deadline_at':time.time()+600})
    store.mark_running('durable-test')
    store.mark_waiting_cooldown('durable-test',{'not_before':time.time()-1,'cooldown_attempts':attempts,'cooldown_limit':3})
    return store,path


def test_restarted_store_and_competing_maintenance_claim_only_one_probe(tmp_path):
    store,path=waiting(tmp_path)
    assert compact_task_ref(store.get_task('durable-test'))['recommended_next_action']=='wait_for_automatic_bounded_probe_or_cancel'
    with ThreadPoolExecutor(max_workers=4) as pool:
        claimed=list(pool.map(lambda _:TaskStore(path).claim_cooldown_probe('durable-test'),range(4)))
    assert claimed.count(True)==1
    assert TaskStore(path).get_task('durable-test')['metadata']['cooldown_attempts']==1


def test_cancelled_and_exhausted_waits_cannot_be_requeued(tmp_path):
    store,path=waiting(tmp_path,attempts=3)
    assert not store.claim_cooldown_probe('durable-test')
    store.cancel_task('durable-test')
    assert not TaskStore(path).claim_cooldown_probe('durable-test')
    assert store.get_task('durable-test')['status']=='cancelled'


@pytest.fixture
def service(tmp_path,monkeypatch):
    for name,relative in [('KR_DATA_ROOT','data'),('KR_STATE_DIR','state'),('KR_LOG_DIR','logs'),('KR_EVIDENCE_ROOT','assets'),('KR_RUNTIME_LEASE_DB_PATH','leases.sqlite3')]:
        monkeypatch.setenv(name,str(tmp_path/relative))
    monkeypatch.setattr(TaskService,'start_maintenance',lambda *a,**k:None)
    return TaskService(TaskStore(str(tmp_path/'tasks.sqlite3')))


def settled(service,task_id):
    until=time.monotonic()+5
    while time.monotonic()<until:
        task=service.store.get_task(task_id)
        if task['status'] not in {'running','queued'}:return task
        time.sleep(.01)
    pytest.fail('task did not release execution slot')


def test_real_task_artifact_wait_restart_probe_and_success_keep_task_identity(service,monkeypatch):
    from runtime import composition
    calls=[]
    def execute(payload):
        calls.append(payload)
        return {'failure_type':'rate_limited','retry_after_s':60,'error':'429'} if len(calls)==1 else {'content':'正文保留','status':'ok'}
    monkeypatch.setattr(composition,'execute_research_query',execute)
    row=service.submit('research_query',{'query':'isolated cooldown'},timeout_s=300)
    task=settled(service,row['task_id'])
    assert task['status']=='waiting_cooldown'
    assert service.result(row['task_id'])['result']['failure_type']=='rate_limited'
    service._advance_cooldown(row['task_id'])
    assert len(calls)==1
    service.store._mark(row['task_id'],'waiting_cooldown',metadata={'not_before':time.time()-1})
    restarted=TaskService(TaskStore(service.store.db_path))
    restarted.recover()
    restarted._advance_cooldown(row['task_id'])
    assert settled(restarted,row['task_id'])['status']=='completed'
    assert restarted.result(row['task_id'])['result']['content']=='正文保留'
    assert len(calls)==2


def test_repeated_risk_stops_after_three_automatic_probes(service,monkeypatch):
    from runtime import composition
    calls=[]
    monkeypatch.setattr(composition,'execute_research_query',lambda payload:calls.append(payload) or {'failure_type':'anti_bot_verification','error':'blocked'})
    row=service.submit('research_query',{'query':'bounded probes'},timeout_s=300)
    for attempt in range(3):
        assert settled(service,row['task_id'])['status']=='waiting_cooldown'
        service.store._mark(row['task_id'],'waiting_cooldown',metadata={'not_before':time.time()-1})
        until=time.monotonic()+3
        while time.monotonic()<until:
            service._advance_cooldown(row['task_id'])
            if (service.store.get_task(row['task_id'])['metadata'].get('cooldown_attempts') or 0)>attempt:break
            time.sleep(.01)
    task=settled(service,row['task_id'])
    assert task['status']=='failed' and task['error_code']=='cooldown_probe_exhausted'
    service._advance_cooldown(row['task_id'])
    assert len(calls)==4


@pytest.mark.parametrize('successor_state', ['queued', 'running'])
def test_old_monitor_cannot_interrupt_next_cooldown_probe(service, monkeypatch, successor_state):
    import threading
    from runtime import composition, task_process

    first_committed = threading.Event()
    release_first = threading.Event()
    successor_entered = threading.Event()
    release_successor = threading.Event()
    first_monitor_finished = threading.Event()
    calls = []
    executions = []
    original_execute = task_process.execute_job
    original_monitor = service._monitor

    def business(payload):
        calls.append(payload)
        if len(calls) == 1:
            return {'failure_type': 'rate_limited', 'retry_after_s': 60}
        if successor_state == 'running':
            successor_entered.set()
            assert release_successor.wait(5)
        return {'status': 'ok', 'content': '恢复成功'}

    def execute(*args, **kwargs):
        executions.append(True)
        first = len(executions) == 1
        if not first and successor_state == 'queued':
            successor_entered.set()
            assert release_successor.wait(5)
        result = original_execute(*args, **kwargs)
        if first:
            first_committed.set()
            assert release_first.wait(5)
        return result

    def monitor(job, *args, **kwargs):
        try:
            return original_monitor(job, *args, **kwargs)
        finally:
            if not job.get('cooldown_probe_lease_id'):
                first_monitor_finished.set()

    monkeypatch.setattr(composition, 'execute_research_query', business)
    monkeypatch.setattr(task_process, 'execute_job', execute)
    monkeypatch.setattr(service, '_monitor', monitor)
    row = service.submit('research_query', {'query': 'monitor handoff'}, timeout_s=300)
    try:
        assert first_committed.wait(5)
        service.store._mark(row['task_id'], 'waiting_cooldown', metadata={'not_before': time.time()-1})
        service._advance_cooldown(row['task_id'])
        assert successor_entered.wait(5)
        release_first.set()
        assert first_monitor_finished.wait(5)
        assert service.store.get_task(row['task_id'])['status'] == successor_state
    finally:
        release_first.set()
        release_successor.set()
    assert settled(service, row['task_id'])['status'] == 'completed'
    assert len(calls) == 2 and service.store.get_task(row['task_id'])['metadata']['cooldown_attempts'] == 1


def test_discovery_wait_persists_initial_risk_without_immediate_replay(service,monkeypatch):
    from runtime import composition
    monkeypatch.setattr(composition, 'TaskService', lambda: service, raising=False)
    import runtime.task_service as module
    monkeypatch.setattr(module, 'TaskService', lambda: service)
    calls=[]
    monkeypatch.setattr(composition,'execute_platform_discovery',lambda payload:calls.append(payload) or {'items':[{'title':'恢复后的发现'}]})
    result=composition._persist_discovery_cooldown({'failure_type':'anti_bot_verification'},
        tool='search_zhihu', platform='知乎', arguments={'keyword':'fixture','research_task_id':''})
    task=settled(service,result['task_id'])
    assert task['status']=='waiting_cooldown' and calls==[]
    service.store._mark(task['task_id'],'waiting_cooldown',metadata={'not_before':time.time()-1})
    service._advance_cooldown(task['task_id'])
    assert settled(service,task['task_id'])['status']=='completed'
    assert len(calls)==1
