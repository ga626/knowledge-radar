"""交付接缝反例；不访问真实数据、账号或网络。"""
import json

from scripts.kr_eval import ci
from scripts.kr_eval.core import Ledger, snapshot


def test_interrupted_run_does_not_hide_complete_history(tmp_path):
    ledger = Ledger(tmp_path)
    run_id, _ = ledger.begin()
    rows = ledger.history()
    assert rows[0]['run_id'] == run_id
    assert rows[0]['counts'] == {'INCOMPLETE': 1}
    assert not rows[0]['results']


def test_snapshot_includes_line_ending_contract(tmp_path):
    path = tmp_path / '.gitattributes'
    path.write_text('* text=auto\n', encoding='utf-8')
    before = snapshot(tmp_path)['content_hash']
    path.write_text('* text eol=lf\n', encoding='utf-8')
    assert snapshot(tmp_path)['content_hash'] != before


def test_ci_partitions_never_duplicate_host_or_scale():
    scope = {'nodes': ['tests/test_browser_link_host.py', 'tests/test_research_archive.py'], 'node_tests': ['tests/test_site.mjs']}
    parts = ci.partition_scope(scope)
    assert 'tests/test_browser_link_host.py' not in parts['offline']['nodes']
    assert 'tests/test_browser_link_host.py' in parts['host']['nodes']
    assert parts['node']['node_tests'] == scope['node_tests']
    assert ci.SCALE_NODES[0] in parts['offline']['deselect']


def test_reuse_requires_trusted_complete_tree_environment_and_intact_evidence(tmp_path):
    from scripts.kr_eval.core import file_hash
    evidence = tmp_path / 'junit.xml'
    evidence.write_text('<testsuite tests="1"/>', encoding='utf-8')
    report = {'schema': 'kr-ci-batch/v1', 'status': 'PASS', 'tree': 'tree', 'environment': {'id': 'env'},
              'source_snapshot':'bytes-A',
              'scope': {'mode': 'full'}, 'jobs': {'offline': 'PASS', 'host': 'PASS', 'node': 'PASS'},
              'evidence': [{'path': 'junit.xml', 'sha256': file_hash(evidence)}]}
    trusted = {'repository': 'owner/repo', 'head_repository': 'owner/repo', 'event': 'pull_request',
               'actor': 'owner', 'allowed_actors': ['owner'], 'head_branch': 'codex/fix',
               'conclusion': 'success', 'run_id': 42, 'run_attempt': 1, 'tree': 'tree'}
    assert ci.reuse_batch(report, report, trusted, tmp_path)['reusable']
    assert not ci.reuse_batch(report, {**report, 'source_snapshot':'bytes-B'}, trusted, tmp_path)['reusable']
    for field, value in [('head_repository', 'fork/repo'), ('conclusion', 'failure'), ('tree', 'other')]:
        identity = {**trusted, field: value}
        assert not ci.reuse_batch(report, report, identity, tmp_path)['reusable']
    for key, value in [('environment', {'id': 'other'}), ('jobs', {'offline': 'PASS', 'host': 'SKIPPED'})]:
        bad = {**report, key: value}
        assert not ci.reuse_batch(bad, report, trusted, tmp_path)['reusable']
    bad = {**report, 'evidence': [{'path': 'missing', 'sha256': 'bad'}]}
    assert not ci.reuse_batch(bad, report, trusted, tmp_path)['reusable']


def test_ci_aggregation_rejects_skipped_job_and_keeps_successful_attempts(tmp_path):
    import pytest
    for group in ('offline','host','node'):
        folder = tmp_path/f'ci-{group}-42-1'
        folder.mkdir()
        (folder/'ci-outcome.json').write_text(json.dumps({'status':'PASS','group':group,'tree':'T',
            'source_snapshot':'bytes','environment':{'id':group},'scope':{'mode':'full'}}), encoding='utf-8')
    assert ci.aggregate_jobs(tmp_path, dict.fromkeys(('offline','host','node'), 'success'))['status'] == 'PASS'
    with pytest.raises(ValueError, match='missing_or_failed'):
        ci.aggregate_jobs(tmp_path, {'offline':'success','host':'success','node':'skipped'})
    # 原生只重跑 Node：其余组保留原始成功证据，不必复制/伪造新 attempt。
    second = tmp_path/'ci-node-42-2'
    second.mkdir()
    (second/'ci-outcome.json').write_bytes((tmp_path/'ci-node-42-1/ci-outcome.json').read_bytes())
    assert ci.aggregate_jobs(tmp_path, dict.fromkeys(('offline','host','node'), 'success'))['status']=='PASS'


def test_node_executor_distinguishes_skipped_tests_from_pass(tmp_path):
    node = tmp_path/'test_skip.mjs'
    node.write_text("import {test} from 'node:test'; test.skip('not run',()=>{});", encoding='utf-8')
    folder = tmp_path/'result'
    folder.mkdir()
    assert ci._node_tests(tmp_path, ['test_skip.mjs'], folder)['verdict']=='SKIPPED'
