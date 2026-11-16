"""上下文减负只改变投影，不改变执行、证据或继续读取合同。"""
import copy
import json

import pytest


@pytest.mark.parametrize('mode,operation', [
    ('probe_browser_auth:boss', 'probe_browser_auth'),
    ('request_browser_interaction:boss:login', 'request_browser_interaction'),
    ('complete_browser_interaction:boss', 'complete_browser_interaction'),
    ('cancel_browser_interaction:boss', 'cancel_browser_interaction'),
])
def test_platform_health_does_not_fetch_unrelated_history(monkeypatch, mode, operation):
    from runtime import composition
    observation = {'status': 'needs_interaction', 'request_executed': False,
                   'manual_action_required': True, 'operation_id': 'current-only',
                   'failure_type': 'login_required', 'next_action': 'user_login'}
    monkeypatch.setattr(composition, operation, lambda *a, **k: observation)
    monkeypatch.setattr(composition, 'browser_sessions_summary',
                        lambda **k: pytest.fail('unrelated history fetched'))
    monkeypatch.setattr(composition, 'chrome_runtime_summary',
                        lambda: pytest.fail('all-platform runtime fetched'))
    result = composition.health_check(mode=mode)
    actual = next(iter(result['checks'].values()))
    assert actual == observation
    assert result['platform'] == 'boss'
    assert result['diagnostics']['request']['mode'] == 'browser_sessions'


def test_explicit_browser_diagnostic_still_returns_history(monkeypatch):
    from runtime import composition
    history = {'sessions': [{'platform': 'boss', 'state': 'NEEDS_USER'}], 'pending_human_action': True}
    monkeypatch.setattr(composition, 'browser_sessions_summary', lambda **k: history)
    monkeypatch.setattr(composition, 'chrome_runtime_summary', lambda: {})
    result = composition.health_check(mode='browser_sessions')
    assert result['checks']['browser_sessions'] == history


@pytest.mark.parametrize('failed', [False, True])
def test_static_strategy_is_folded_without_hiding_attempts_or_evidence(failed):
    from runtime.agent_contracts import compact_evidence
    value = {'content': '已取得正文', 'error': {'type': 'timeout'} if failed else None,
             'evidence_assets': [{'kind': 'content', 'version_id': 'a'*64}],
             'disclosure': {'parts': [{'next_start': 800, 'version_id': 'a'*64}]},
             'metadata': {'strategy': {'id': 'generic_web_extraction', 'max_depth': 3,
                'nodes': [{'id': 'reader', 'notes': 'static'*1000}],
                'error_taxonomy': {'timeout': {'retryable': True}}},
                'failure_taxonomy': {'timeout': {'retryable': True}},
                'attempts': [{'name': 'reader', 'status': 'failed' if failed else 'ok'}],
                'fallback_errors': [{'type': 'timeout'}] if failed else [],
                'selected_node': 'reader'}}
    before = copy.deepcopy(value)
    result = compact_evidence(value)
    assert value == before  # Original execution/receipt facts remain immutable.
    assert result['content'] == value['content']
    assert result['error'] == value['error']
    assert result['metadata']['attempts'] == value['metadata']['attempts']
    assert result['metadata']['fallback_errors'] == value['metadata']['fallback_errors']
    assert result['disclosure'] == value['disclosure']
    assert result['evidence_assets'] == value['evidence_assets']
    assert 'nodes' not in result['metadata']['strategy']
    assert 'error_taxonomy' not in result['metadata']['strategy']
    assert 'failure_taxonomy' not in result['metadata']
    assert result['metadata']['strategy_details']['request']['summary'] is False


def test_non_catalog_failure_taxonomy_is_not_removed():
    from runtime.agent_contracts import compact_evidence
    value = {'metadata': {'failure_taxonomy': {'code': 'POLICY_DENIED', 'reason': 'current failure'}}}
    assert compact_evidence(value) == value


def test_plugin_bundle_identity_changes_for_reference_only_edit(tmp_path, monkeypatch):
    from scripts import setup_codex_product as setup
    source = tmp_path/'source'
    skill = source/'skills/research/SKILL.md'
    ref = source/'skills/research/references/media.md'
    manifest = source/'.codex-plugin/plugin.json'
    ref.parent.mkdir(parents=True)
    manifest.parent.mkdir(parents=True)
    skill.write_text('入口不变', encoding='utf-8')
    ref.write_text('预算旧说明', encoding='utf-8')
    manifest.write_text(json.dumps({'name': 'test', 'version': '0.1.0+codex'}), encoding='utf-8')
    before = setup._plugin_version_with_skill_fingerprint(source)
    ref.write_text('预算新说明', encoding='utf-8')
    after = setup._plugin_version_with_skill_fingerprint(source)
    assert before != after


def test_install_and_source_deploy_share_complete_bundle_identity(tmp_path, monkeypatch):
    from scripts import setup_codex_product as setup, product_install as install
    from pathlib import Path
    source = Path(setup.PLUGIN_SOURCE)
    monkeypatch.setattr(setup, 'PLUGIN_SOURCE', source)
    development = tmp_path/'development-plugin'
    development_identity = setup._copy_plugin(development)
    home = tmp_path/'codex-home'
    package_identity = install.copy_plugin(source.parents[3], home)
    assert development_identity['version'] == package_identity['plugin_version']
    assert development_identity['bundle_sha256'] == package_identity['plugin_bundle_sha256']
    first = package_identity['plugin_version']
    manifest = home/'plugins/knowledgeradar-research/.codex-plugin/plugin.json'
    payload = json.loads(manifest.read_text(encoding='utf-8'))
    payload['version'] = '0.1.0a22+codex.derived-placeholder'
    manifest.write_text(json.dumps(payload, indent=4), encoding='utf-8')
    assert setup._plugin_version_with_skill_fingerprint(manifest.parent.parent) == first


def test_default_route_is_small_but_diagnostic_keeps_full_cards(tmp_path, monkeypatch):
    from runtime import composition
    monkeypatch.setenv('KR_STATE_DIR', str(tmp_path/'state'))
    small = composition.kr_research(task='公开文档核验', research_task_id='route-small')
    full = composition.kr_research(task='公开文档核验', budget='diagnostic', research_task_id='route-full')
    assert 'source_ecology_cards' not in small['route_plan']
    assert 'source_ecology_cards' in full['route_plan']
    assert small['route_plan']['selected_source_ecologies'] == full['route_plan']['selected_source_ecologies']
    assert [x['source_ecology'] for x in small['route_plan']['considered_source_ecologies']] == [x['source_ecology'] for x in full['route_plan']['considered_source_ecologies']]
    assert small['research_task']['research_task_id'] == 'route-small'
    assert len(json.dumps(small['route_plan'])) < len(json.dumps(full['route_plan'])) / 2
