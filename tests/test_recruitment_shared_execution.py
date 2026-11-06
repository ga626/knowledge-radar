"""招聘公共出口：记账、并发与登录恢复，不访问真实账号。"""
import pytest
from conftest import patch_native
from runtime import native_site


@pytest.fixture(autouse=True)
def isolation(monkeypatch, tmp_path):
    monkeypatch.setenv('KR_STATE_DIR', str(tmp_path / 'state'))
    monkeypatch.setenv('KR_TASK_DB_PATH', str(tmp_path / 'tasks.sqlite3'))
    monkeypatch.setenv('KR_RUNTIME_LEASE_DB_PATH', str(tmp_path / 'leases.sqlite3'))


def test_each_operation_accounted_once_including_nested_facade(monkeypatch):
    from runtime import recruitment_governance as g
    monkeypatch.setattr(g, 'check_platform_admission', lambda *a, **kw: {'allowed': True})
    result = g.execute_operation('maimai', 'search', lambda: g.execute_operation(
        'maimai', 'search', lambda: {'status': 'ok', 'items': [{'url': 'post'}]}))
    assert result['items']
    conn = g._get_conn()
    assert conn.execute('SELECT operation, COUNT(*) FROM recruitment_search_gate GROUP BY operation').fetchall() == [('search', 1)]
    conn.close()


def test_search_interval_does_not_block_detail_but_risk_does(monkeypatch):
    from runtime import recruitment_governance as g
    monkeypatch.setattr(g.time, 'time', lambda: 10000.)
    g.record_search_outcome('boss', 'ok')
    assert not g.check_search_gate('boss')['allowed']
    assert g.check_search_gate('boss', operation='detail')['allowed']
    g.record_search_outcome('boss', 'blocked', 'risk_control', operation='detail')
    assert not g.check_search_gate('boss', operation='detail')['allowed']


def test_admission_rejection_not_a_site_request(monkeypatch):
    from runtime import recruitment_governance as g
    monkeypatch.setattr(g, 'check_platform_admission', lambda *a, **kw: {'allowed': False, 'reason_code': 'pending_manual_interaction', 'manual_action_required': True})
    result = g.execute_operation('maimai', 'search', lambda: pytest.fail('must not access site'))
    assert result['manual_action_required']
    conn = g._get_conn()
    assert conn.execute('SELECT COUNT(*) FROM recruitment_search_gate').fetchone()[0] == 0
    conn.close()


def test_exception_recorded_and_lease_released(monkeypatch):
    from runtime import recruitment_governance as g
    monkeypatch.setattr(g, 'check_platform_admission', lambda *a, **kw: {'allowed': True})
    with pytest.raises(ValueError):
        g.execute_operation('maimai', 'detail', lambda: (_ for _ in ()).throw(ValueError('broken parser')))
    conn = g._get_conn()
    assert conn.execute('SELECT outcome FROM recruitment_search_gate').fetchone()[0] == 'failed'
    conn.close()
    lease = g.acquire_platform_lease('maimai')
    assert lease.acquired
    g.release_platform_lease(lease.lease_id)


def test_boss_auth_recovery_changes_failure_cache_identity(monkeypatch):
    from collectors.platform import boss as b
    before = b.runtime_identity()
    import hashlib
    from pathlib import Path
    assert before['driver']=='opencli'
    patch_native(monkeypatch, lambda **kw: {'ok': True, 'data': {'authenticated_ui': True}})
    assert b.complete_login()['status'] == 'ok'
    assert b.runtime_identity() != before


def test_boss_uses_shared_opencli_and_passive_login_without_native_transport(monkeypatch):
    from collectors.platform.boss import BossOpenCLI
    from runtime import opencli_bridge
    calls = []
    monkeypatch.setattr(opencli_bridge, 'invoke_page', lambda **kw: calls.append(kw) or {'ok': True, 'data': {}})
    result = BossOpenCLI().request('search', {'url': 'https://www.zhipin.com/web/geek/jobs?query=Python'})
    assert result['ok']
    assert calls[0]['session'] == 'kr:boss' and calls[0]['isolated_work'] is True
    assert 'site.extract("search")' in calls[0]['expression']
    assert calls[0]['passive_manual_login'] is True
    BossOpenCLI().request('probe', {})
    assert len(calls) == 1


def test_current_diagnostics_do_not_admit_retired_owned_profiles():
    from runtime.channel_admission import current_browser_diagnostics
    registry = {'profiles': [{'platform': 'xiaohongshu', 'main_chain_allowed': True, 'launch_policy': 'safe_auto'}]}
    current, admission = current_browser_diagnostics(registry, opencli={'connected': True}, native={'ok': False, 'code': 'browser_link_distribution_pending'})
    assert current['profiles'] == [] and current['retired_profile_count'] == 1
    assert admission['production_routing'] == 'platform_adapter_owned'
    channels = {row['platform']: row for row in admission['channels']}
    assert channels['xiaohongshu']['connected'] is True and channels['zhihu']['connected'] is False
    assert not any(row['main_chain_allowed'] for row in channels.values())
    assert registry['profiles'][0]['main_chain_allowed'] is True


def test_all_recruitment_details_bind_without_server():
    from runtime.platform_execution import ensure_bindings
    from kr_core.registry import registry
    ensure_bindings()
    for platform in ('BOSS直聘', '猎聘', '脉脉', 'V2EX'):
        assert registry.detail_strategy(platform) is not None


def test_borrowed_platforms_cannot_start_owned_browser():
    from runtime import chrome_manager as c
    for platform in ('boss', 'liepin', 'maimai'):
        assert platform not in c.managed_browser_platforms()
        assert c._managed_chrome_profile_dir(platform) == ''
        assert c._ensure_chrome_debugging(platform) is False


def test_parser_failure_not_remote_risk_or_account_cooldown():
    from runtime import recruitment_governance as g
    for _ in range(4):
        g.record_search_outcome('boss', 'failed', 'page_identity_changed', operation='detail')
    assert g.check_search_gate('boss', operation='detail')['allowed']
    g.record_search_outcome('boss', 'blocked', 'risk_control', operation='detail')
    # A later tool failure must not mask a real outstanding remote restriction.
    g.record_search_outcome('boss', 'failed', 'page_structure_or_not_ready', operation='detail')
    assert not g.check_search_gate('boss', operation='detail')['allowed']


def test_invalid_url_is_local_failure_without_remote_cooldown():
    from runtime import recruitment_governance as g
    g.record_search_outcome('boss','failed','ERR_INVALID_URL',operation='detail')
    assert g.check_search_gate('boss',operation='detail')['allowed']


def test_detail_strategy_preserves_admission_and_transport_failures():
    from detail_strategies.recruitment import _normalize_recruitment_detail
    for status, kind in [('blocked', 'rate_limited'), ('failed', 'page_identity_changed'),
                         ('failed', 'opencli_dependency_or_output_invalid')]:
        result = _normalize_recruitment_detail('BOSS直聘', 'https://www.zhipin.com/job_detail/one.html',
            {'status': status, 'failure_type': kind, 'error': {'type': kind}, 'retry_after_s': 123})
        assert result['failure_type'] == kind and result['status'] == status
        assert result['retry_after_s'] == 123
