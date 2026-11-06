from __future__ import annotations

import pytest
import server


@pytest.mark.parametrize('legacy_toggle', ['true', 'false'])
def test_health_summary_v2_is_compact_and_never_probes_external_providers(monkeypatch, legacy_toggle):
    monkeypatch.setenv('KR_HEALTH_SUMMARY_COLD_STATIC', legacy_toggle)
    def forbidden(*args, **kwargs):
        pytest.fail('light summary must not probe providers or browsers')
    for name in ('provider_status', 'academic_provider_status', 'chrome_runtime_summary', 'chrome_runtime_quick_summary'):
        monkeypatch.setattr(server, name, forbidden)
    monkeypatch.setattr(server.gh_cli_sidecar, 'health', forbidden)
    monkeypatch.setattr(server.get_task_store(), 'summary', lambda recent_limit=1: {'counts':{'queued':2}})
    result=server._health_check_agent_summary_uncached()
    assert result['schema_version']=='knowledgeradar-health-agent-summary/v2'
    assert result['diagnostic_mode']=="health_check(mode='diagnostic_summary')"
    assert result['providers']=='not_probed' and result['browser']=='not_probed'
    assert result['task_counts']=={'queued':2}
    from runtime.agent_contracts import catalog
    expected=catalog()
    assert result['tool_count']==expected['tool_count'] and result['contract_hash']==expected['contract_hash']
