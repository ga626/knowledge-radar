"""断核后的只读状态、单次恢复与界面交接；全部使用隔离数据。"""
import json
import os
import time

import httpx
import pytest

from runtime import execution_owner as owner


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(owner, 'active_client', lambda: True)
    monkeypatch.setattr(owner, 'source_identity', lambda: 'candidate')
    return owner.OwnerClient(root=tmp_path/'owner')


def supervised(client, **changes):
    value = {'source_identity': 'candidate', 'supervisor_pid': os.getpid(),
             'process_identity': owner.process_identity(), 'state': 'circuit_open',
             'restart_count': 4, 'max_restarts': 3, 'incident_id': 'incident-1',
             'last_failure': {'code': 'unexpected_exit', 'exit_code': 9, 'cause': 'unknown'}}
    value.update(changes)
    owner._write_json(client.root/'supervision.json', value)
    return value


def test_runtime_query_survives_dead_owner_and_does_not_write(client):
    from runtime.runtime_control import runtime_snapshot
    supervised(client)
    before = {p.name: p.read_bytes() for p in client.root.iterdir()}
    result = runtime_snapshot(client)
    assert result['status'] == 'ok'
    assert result['runtime']['state'] == 'circuit_open'
    assert result['runtime']['last_failure']['exit_code'] == 9
    assert result['runtime']['next_action'] == 'await_user_decision'
    assert before == {p.name: p.read_bytes() for p in client.root.iterdir()}


def test_corrupt_status_is_unknown_not_absent(client):
    from runtime.runtime_control import runtime_snapshot
    client.root.mkdir()
    (client.root/'supervision.json').write_text('{broken', encoding='utf-8')
    result = runtime_snapshot(client)['runtime']
    assert result['state'] == 'unknown'
    assert result['observations']['supervision'] == 'invalid'
    assert result['automatic_replay'] is False


def test_alive_but_unreachable_keeps_supervisor_and_does_not_start(client, monkeypatch):
    from runtime.runtime_control import runtime_snapshot
    supervised(client, state='ready')
    monkeypatch.setattr(client, 'descriptor', lambda: {'pid': os.getpid(), 'source_identity': 'candidate', 'protocol': 1})
    monkeypatch.setattr(client, '_request', lambda *a, **k: (_ for _ in ()).throw(httpx.ConnectError('offline')))
    monkeypatch.setattr(client, 'start', lambda **k: pytest.fail('status must not start'))
    result = runtime_snapshot(client)['runtime']
    assert result['state'] == 'unresponsive'
    assert result['supervisor']['status'] == 'available'
    assert client.status()['supervision']['state'] == 'ready'


@pytest.mark.parametrize('failure', ['missing', 'identity', 'unknown'])
def test_all_runtime_failures_include_snapshot_and_original_receipt(client, monkeypatch, failure):
    supervised(client)
    if failure != 'missing':
        monkeypatch.setattr(client, 'descriptor', lambda: {'pid': os.getpid(), 'source_identity': 'wrong' if failure == 'identity' else 'candidate', 'protocol': 1})
    if failure == 'unknown':
        monkeypatch.setattr(client, '_request', lambda *a, **k: (_ for _ in ()).throw(httpx.ConnectError('offline')))
    result = client.call('read_knowledge', {}, request_id='original-request')
    assert result['error']['request_id'] == 'original-request'
    assert result['runtime']['automatic_replay'] is False
    assert result['error']['next_action'] in {'await_user_decision', 'reconcile_request'}
    if failure == 'unknown':
        assert result['error']['execution_state'] == 'unknown'
        assert result['error']['next_action'] == 'reconcile_request'


def test_missing_state_does_not_create_owner_root(client):
    from runtime.runtime_control import runtime_snapshot
    assert runtime_snapshot(client)['runtime']['state'] == 'unknown'
    assert not client.root.exists()


def test_wait_is_bounded_and_does_not_start(client, monkeypatch):
    from runtime.runtime_control import runtime_snapshot
    supervised(client, state='retrying', next_retry_at=time.time()+2)
    monkeypatch.setattr(client, 'start', lambda **k: pytest.fail('wait is read only'))
    start = time.monotonic()
    assert runtime_snapshot(client, wait_seconds=.02)['runtime']['state'] == 'recovering'
    assert time.monotonic()-start < .5


class Child:
    pid = os.getpid()
    returncode = None
    def poll(self):
        return self.returncode


def manager_fixture(client, monkeypatch, *, limit=3):
    children = []
    def start(**kwargs):
        assert kwargs['managed']
        child = Child()
        children.append(child)
        client.child = child
        return {'status': 'ready', 'generation': str(len(children)), 'pid': child.pid}
    monkeypatch.setattr(client, 'start', start)
    return owner.OwnerSupervisor(client, max_restarts=limit), children


def test_definite_permission_failure_stops_without_spending_all_retries(client, monkeypatch):
    manager, children = manager_fixture(client, monkeypatch)
    monkeypatch.setattr(client, 'start', lambda **k: (_ for _ in ()).throw(PermissionError(13, 'private-secret')))
    manager.tick(now=0)
    assert manager.state == 'blocked'
    snapshot = json.loads((client.root/'supervision.json').read_text())
    assert snapshot['last_failure']['code'] == 'access_denied'
    assert snapshot['last_failure']['cause'] == 'unknown'
    assert 'private-secret' not in json.dumps(snapshot)
    manager.tick(now=100)
    assert not children


def test_retry_keeps_last_exit_incident_and_wallclock_schedule(client, monkeypatch):
    manager, children = manager_fixture(client, monkeypatch)
    manager.tick(now=0)
    children[-1].returncode = 7
    manager.tick(now=1)
    first = json.loads((client.root/'supervision.json').read_text())
    assert first['last_failure']['exit_code'] == 7
    assert first['next_retry_at'] >= time.time()
    manager.tick(now=10)
    second = json.loads((client.root/'supervision.json').read_text())
    assert second['last_failure'] == first['last_failure']
    assert second['incident_id'] == first['incident_id']


def test_manual_once_preserves_exhausted_budget_and_is_idempotent(client, monkeypatch):
    from runtime.runtime_control import request_recovery
    supervised(client)
    manager, children = manager_fixture(client, monkeypatch)
    first = request_recovery(client, request_id='manual-1', expected_source='candidate', expected_incident='incident-1')
    duplicate = request_recovery(client, request_id='manual-1', expected_source='candidate', expected_incident='incident-1')
    assert first['recovery_request_id'] == duplicate['recovery_request_id']
    manager.tick(now=0)
    assert len(children) == 1
    assert manager.failures == 4
    children[-1].returncode = 9
    manager.tick(now=1)
    manager.tick(now=100)
    assert len(children) == 1
    assert manager.state == 'circuit_open'
    repeated = request_recovery(client, request_id='manual-1', expected_source='candidate', expected_incident='incident-1')
    assert repeated['status'] == 'succeeded'
    assert not (client.root/'start.request.json').exists()


def test_two_operator_requests_coalesce_before_manager_tick(client, monkeypatch):
    from runtime.runtime_control import request_recovery
    from concurrent.futures import ThreadPoolExecutor
    supervised(client)
    with ThreadPoolExecutor(max_workers=2) as pool:
        replies = list(pool.map(lambda x: request_recovery(client, request_id=x,
            expected_source='candidate', expected_incident='incident-1'), ['one', 'two']))
    assert len({r['recovery_request_id'] for r in replies}) == 1


def test_stale_or_foreign_recovery_is_rejected_without_write(client):
    from runtime.runtime_control import request_recovery
    supervised(client)
    before = {p.name: p.read_bytes() for p in client.root.iterdir()}
    result = request_recovery(client, request_id='stale', expected_source='other', expected_incident='incident-1')
    assert result['status'] == 'rejected'
    assert before == {p.name: p.read_bytes() for p in client.root.iterdir()}


def test_startup_deadline_does_not_kill_or_start_second_process(client, monkeypatch):
    manager, children = manager_fixture(client, monkeypatch)
    original = client.start
    def late(**kwargs):
        original(**kwargs)
        raise TimeoutError('still starting')
    monkeypatch.setattr(client, 'start', late)
    monkeypatch.setattr(client, 'status', lambda: {'status': 'unavailable'})
    manager.tick(now=0)
    manager.tick(now=1000)
    assert manager.state == 'unresponsive'
    assert len(children) == 1
    assert children[0].poll() is None


def test_manager_restart_does_not_repeat_consumed_manual_request(client, monkeypatch):
    from runtime.runtime_control import request_recovery
    supervised(client)
    manager, children = manager_fixture(client, monkeypatch)
    request_recovery(client, request_id='crashed-manager', expected_source='candidate', expected_incident='incident-1')
    def lost_manager(**kwargs):
        raise KeyboardInterrupt('manager died before result persisted')
    monkeypatch.setattr(client, 'start', lost_manager)
    with pytest.raises(KeyboardInterrupt):
        manager.tick(now=0)
    replacement, children = manager_fixture(client, monkeypatch)
    replacement.tick(now=1000)
    assert not children
    assert replacement.state in {'circuit_open', 'blocked'}


@pytest.mark.parametrize('error,code', [(FileNotFoundError(2,'x'),'required_file_missing'),
    (OSError(28,'x'),'storage_unavailable'), (ModuleNotFoundError('secret'),'required_dependency_missing')])
def test_classification_is_bounded_facts_without_exception_message(error, code):
    from runtime.runtime_control import classify_failure
    value = classify_failure(error, stage='spawn')
    assert value['code'] == code
    assert value['retry_policy'] == 'stop'
    assert 'secret' not in json.dumps(value)


def test_foreign_live_owner_cannot_be_recovered(client, monkeypatch):
    from runtime.runtime_control import request_recovery
    supervised(client)
    monkeypatch.setattr(client, 'status', lambda: {'status': 'incompatible'})
    result = request_recovery(client, request_id='foreign', expected_source='candidate', expected_incident='incident-1')
    assert result['status'] == 'rejected'
    assert not (client.root/'start.request.json').exists()


def test_restarted_manager_adopts_ready_owner_without_replay(client, monkeypatch):
    from runtime.runtime_control import request_recovery, recovery_receipt
    supervised(client)
    request_recovery(client, request_id='adopt', expected_source='candidate', expected_incident='incident-1')
    supervised(client, recovery_result='running', recovery_request_id='adopt')
    monkeypatch.setattr(client, 'status', lambda: {'status': 'ready', 'generation': 'live', 'pid': os.getpid()})
    monkeypatch.setattr(owner, '_ProcessWitness', lambda descriptor: Child())
    monkeypatch.setattr(client, 'start', lambda **kw: pytest.fail('must adopt'))
    manager = owner.OwnerSupervisor(client)
    manager.tick(now=0)
    assert manager.state == 'ready'
    assert recovery_receipt(client, 'adopt')['status'] == 'succeeded'


def test_bootstrap_failure_is_structured_and_tied_to_launch(client, monkeypatch):
    from runtime.runtime_control import read_record
    monkeypatch.setenv('KR_OWNER_LAUNCH_ID', 'this-launch')
    def broken():
        raise ModuleNotFoundError('secret module path')
    with pytest.raises(ModuleNotFoundError):
        owner.serve(root=client.root, bootstrap=broken)
    record, observation = read_record(client.root/'bootstrap-failure.json')
    assert observation == 'ok'
    assert record['launch_id'] == 'this-launch'
    assert record['failure']['code'] == 'required_dependency_missing'
    assert record['failure']['retry_policy'] == 'stop'
    assert 'secret module path' not in json.dumps(record)


def test_cli_start_is_manual_once_and_rearm_is_explicit(client, monkeypatch):
    from runtime.cli import parser, execute
    from runtime import runtime_control
    calls = []
    monkeypatch.setattr(runtime_control, 'request_recovery', lambda c, **kw: calls.append(kw) or {'status': 'accepted'})
    for action in ('start', 'rearm'):
        execute(parser().parse_args(['runtime', action, '--request-id', 'cli-'+action,
                                    '--expected-source', 'candidate']), client)
    assert [c['action'] for c in calls] == ['recover_once', 'rearm_auto_recovery']
    assert parser().parse_args(['runtime', 'status', '--wait', '1']).wait == 1


def test_stopped_manager_does_not_hide_persisted_circuit(client, monkeypatch):
    from runtime.runtime_control import runtime_snapshot
    supervised(client, supervisor_pid=99999999)
    value = runtime_snapshot(client)['runtime']
    assert value['state'] == 'circuit_open'
    assert value['supervisor']['status'] == 'unavailable'
    assert value['manual_recovery_allowed']


def test_manual_pending_is_published_before_start_leaves_control_lock(client, monkeypatch):
    from runtime.runtime_control import request_recovery
    supervised(client)
    manager, children = manager_fixture(client, monkeypatch)
    request_recovery(client, request_id='first', expected_source='candidate', expected_incident='incident-1')
    original = client.start
    def start(**kwargs):
        observed = request_recovery(client, request_id='second', expected_source='candidate', expected_incident='incident-1')
        assert observed['status'] == 'pending'
        assert observed['recovery_request_id'] == 'first'
        return original(**kwargs)
    monkeypatch.setattr(client, 'start', start)
    manager.tick(now=0)
    assert len(children) == 1
    assert not (client.root/'start.request.json').exists()


def test_required_import_failure_before_owner_import_is_recorded_in_real_child(tmp_path):
    import subprocess
    import sys
    from pathlib import Path
    root = tmp_path/'child'
    source = Path(__file__).resolve().parents[1]/'src'
    script = '''import builtins, sys
sys.path.insert(0, sys.argv[1])
original = builtins.__import__
def blocked(name, *a, **kw):
    if name == 'pydantic_core': raise ModuleNotFoundError('private module location')
    return original(name, *a, **kw)
builtins.__import__ = blocked
from runtime.owner_bootstrap import main
main()
'''
    env = dict(os.environ, PYTHONPATH=str(source), KR_OWNER_ROOT=str(root),
               KR_OWNER_LAUNCH_ID='child-launch', KR_OWNER_SOURCE_IDENTITY='candidate')
    result = subprocess.run([sys.executable, '-X', 'utf8', '-c', script, str(source)], env=env, capture_output=True, timeout=15)
    assert result.returncode != 0
    record = json.loads((root/'bootstrap-failure.json').read_text(encoding='utf-8'))
    assert record['launch_id'] == 'child-launch'
    assert record['failure']['stage'] == 'owner_import_or_bootstrap'
    assert record['failure']['code'] == 'required_dependency_missing'
    assert 'private module location' not in json.dumps(record)


def test_runtime_error_contract_describes_observed_state(client):
    from runtime.agent_contracts import ErrorResponse
    supervised(client)
    value = client.call('read_knowledge', {}, request_id='described')
    parsed = ErrorResponse.model_validate(value)
    assert parsed.runtime['state'] == 'circuit_open'
    assert parsed.error.request_id == 'described'


def test_uncertain_consumed_recovery_cannot_be_started_again(client):
    from runtime.runtime_control import request_recovery, runtime_snapshot
    supervised(client, state='blocked', recovery_result='unknown', recovery_request_id='consumed')
    assert not runtime_snapshot(client)['runtime']['manual_recovery_allowed']
    assert request_recovery(client, request_id='new-intent', expected_source='candidate',
                            expected_incident='incident-1')['status'] == 'rejected'


@pytest.mark.parametrize('method', ['receipt', 'receipts', 'stop'])
def test_management_transport_failure_carries_same_runtime(client, monkeypatch, method):
    supervised(client)
    monkeypatch.setattr(client, 'descriptor', lambda: {'pid': os.getpid(), 'protocol': owner.PROTOCOL})
    monkeypatch.setattr(client, '_request', lambda *a, **kw: (_ for _ in ()).throw(httpx.ConnectError('offline')))
    result = getattr(client, method)(*(['original'] if method == 'receipt' else []))
    assert result['runtime']['state'] == 'blocked'
    assert result['error']['automatic_investigation'] is False


def test_static_catalog_works_before_business_registration(client, monkeypatch):
    from runtime import application
    monkeypatch.setattr(application, 'OPERATIONS', {})
    result = client.call('get_capabilities', {'summary': True})
    assert result['tool_count'] == 27
    assert result['availability_boundary'] == 'static_contract_is_not_runtime_readiness'
    assert not client.root.exists()
    expanded = client.call('get_capabilities', {'tool_name': 'health_check'})
    assert 'wait_seconds' in expanded['tool']['input_schema']['properties']


def test_remaining_budget_is_not_replenished_after_successful_restart(client, monkeypatch):
    from runtime.runtime_control import runtime_snapshot
    manager, children = manager_fixture(client, monkeypatch)
    manager.tick(now=0)
    children[-1].returncode = 9
    manager.tick(now=1)
    assert runtime_snapshot(client)['runtime']['recovery']['remaining_restarts'] == 3
    manager.tick(now=10)
    assert runtime_snapshot(client)['runtime']['recovery']['remaining_restarts'] == 2


def test_manager_wakeup_timeout_does_not_lose_recovery_identity(client, monkeypatch):
    import subprocess
    from runtime import runtime_control
    supervised(client, supervisor_pid=99999999)
    monkeypatch.setattr(runtime_control, '_wake_manager', lambda c: (_ for _ in ()).throw(subprocess.TimeoutExpired('task', 10)))
    result = runtime_control.request_recovery(client, request_id='wakeup', expected_source='candidate', expected_incident='incident-1')
    assert result['status'] == 'unknown'
    assert result['recovery_request_id'] == 'wakeup'
    assert runtime_control.recovery_receipt(client, 'wakeup')['status'] == 'accepted'


@pytest.mark.parametrize('owner_status', ['ready', 'unavailable'])
def test_inactive_client_cannot_claim_old_live_owner_is_ready(client, monkeypatch, owner_status):
    from runtime.runtime_control import runtime_snapshot
    supervised(client, state='circuit_open', supervisor_pid=99999999)
    monkeypatch.setattr(owner, 'active_client', lambda: False)
    monkeypatch.setattr(client, 'status', lambda: {'status': owner_status})
    value = runtime_snapshot(client)['runtime']
    assert value['state'] == 'blocked'
    assert value['next_action'] == 'await_user_decision'


def test_foreign_supervisor_is_not_current_recovery_authority(client):
    from runtime.runtime_control import runtime_snapshot, request_recovery
    supervised(client, source_identity='foreign-install')
    value = runtime_snapshot(client)['runtime']
    assert value['state'] == 'blocked'
    assert not value['manual_recovery_allowed']
    result = request_recovery(client, request_id='foreign-manager', expected_source='candidate', expected_incident='incident-1')
    assert result['status'] == 'rejected'
