"""Native candidate contracts; isolated state, no website or user account."""
import json
import pytest


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv('KR_STATE_DIR', str(tmp_path / 'state'))
    monkeypatch.setenv('KR_TASK_DB_PATH', str(tmp_path / 'tasks.sqlite3'))
    monkeypatch.setenv('KR_RUNTIME_LEASE_DB_PATH', str(tmp_path / 'leases.sqlite3'))
    monkeypatch.delenv('KR_DURABLE_TASK_ID', raising=False)


def test_native_search_uses_public_governance_and_releases_lease(monkeypatch):
    from collectors.platform import boss as public, boss as native
    from runtime import recruitment_governance as g
    monkeypatch.setattr(g, 'check_platform_admission', lambda *a, **k: {'allowed': True})
    def search(*a, **kw):
        assert g.get_runtime_lease_coordinator().resource_leases('recruitment_platform', 'boss')
        return {'status': 'ok', 'items': [{'title': 'fixture'}], 'metadata': {'transport': 'content_script_native_messaging'}}
    monkeypatch.setattr(native, '_search', search)
    public.search('fixture')
    assert not g.get_runtime_lease_coordinator().resource_leases('recruitment_platform', 'boss')
    conn = g._get_conn()
    assert conn.execute('SELECT operation,COUNT(*) FROM recruitment_search_gate GROUP BY operation').fetchall() == [('search', 1)]
    conn.close()


def test_native_transport_failure_does_not_trigger_remote_cooldown():
    from runtime import recruitment_governance as g
    for _ in range(5):
        g.record_search_outcome('boss', 'failed', 'native_command_timeout', operation='detail')
    assert g.check_search_gate('boss', operation='detail')['allowed']


def test_quality_rejection_is_not_success():
    from collectors.platform.boss import _map_response
    result = _map_response({'ok': True, 'data': {'content': 'very short', 'title': 'job'}}, operation='detail')
    assert result['status'] == 'failed' and result['detail_quality']['ok'] is False


def test_attributed_short_dom_body_uses_existing_quality_policy():
    from collectors.platform.boss import _map_response
    body = '岗位要求：负责后端开发，熟悉Python开发和数据结构。' * 5
    result = _map_response({'ok': True, 'data': {'content': body, 'title': 'Python', 'content_source': 'job_intro_primary', 'salary': '13-25K', 'salary_status': 'readable_dom'}}, operation='detail')
    assert result['status'] == 'ok' and result['detail_quality']['ok']
    assert result['salary_claim_allowed'] is True


def test_obfuscated_salary_is_never_exposed_as_decoded_value():
    from collectors.platform.boss import _map_response
    raw = '\ue032\ue034-\ue033\ue036K'
    result = _map_response({'ok': True, 'data': {'items': [{'salary': raw, 'title': 'job'}]}}, operation='search')
    item = result['items'][0]
    assert item['salary'] == '' and item['salary_raw'] == raw
    assert item['salary_claim_allowed'] is False
    assert 'company' in item['missing_fields']


def test_risk_latches_in_native_state_and_stops_next_admission():
    from collectors.platform.boss import _map_response, admission
    result = _map_response({'ok': True, 'data': {'risk': True}}, operation='search')
    assert result['failure_type'] == 'SECURITY_BLOCK'
    assert admission()['reason'] == 'SECURITY_BLOCK' and not admission()['allowed']


def test_native_identity_never_reads_opencli_configuration(monkeypatch):
    from collectors.platform import boss as public
    from runtime import opencli_bridge
    monkeypatch.setattr(opencli_bridge, 'connection_config', lambda: pytest.fail('unrelated driver identity'))
    result = public.runtime_identity()
    assert result['driver'] == 'content_script_native_messaging' and not result['may_close_process']


def test_registry_cancellation_delegates_to_adapter_only():
    from kr_core.registry import PlatformRegistry
    class Adapter:
        platform = 'fixture'
        def cancel_task(self, task_id):
            return {'cancelled_task': task_id}
    registry = PlatformRegistry()
    registry.register(Adapter())
    assert registry.cancel_task('fixture', 'task-1') == {'cancelled_task': 'task-1'}
    assert registry.cancel_task('unknown', 'task-1') is None


def test_cancel_binding_failure_cannot_prevent_worker_cleanup(monkeypatch):
    from runtime.task_service import TaskService
    from runtime import platform_execution
    from types import SimpleNamespace
    service = object.__new__(TaskService)
    service.store = SimpleNamespace(get_task=lambda _: {'platform': 'boss'})
    def unavailable():
        raise RuntimeError('fixture import failure')
    monkeypatch.setattr(platform_execution, 'ensure_bindings', unavailable)
    assert service._cancel_platform_operation('task-1') == {
        'ok': False, 'code': 'platform_cancel_failed', 'error_type': 'RuntimeError'}


def test_native_detail_preparation_never_starts_opencli(monkeypatch):
    from platform_adapters.boss_adapter import BossAdapter
    from runtime import opencli_bridge
    from kr_core import DetailRequest
    monkeypatch.setattr(opencli_bridge, 'invoke_page', lambda **kw: pytest.fail('unrelated driver preparation'))
    request = DetailRequest(url='https://www.zhipin.com/job_detail/fixture.html', platform='BOSS直聘')
    assert BossAdapter.prepare_detail(request) is request
