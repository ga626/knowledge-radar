"""评测运营合同：先记录现实，再允许 Agent 作可撤销判断。"""
import json
import sqlite3
from datetime import datetime, timezone, timedelta

import pytest

from runtime import evaluation_candidates as candidates


def event(trace='a', status='failed', **kw):
    return dict(tool_name='search_web', trace_id=trace * 16, status=status,
                failure_tags=['empty_detail'], **kw)


def test_all_calls_and_task_denominators_are_distinct_and_private(tmp_path):
    path = tmp_path/'candidates.sqlite3'
    for e in [event(), event(), event('b', 'ok'), event('c', 'failed', parent_trace_id='parent')]:
        candidates.record_candidate(e, path)
    candidates.observe_task({'research_task_id': 'private task', 'revision': 1, 'status': 'open',
                            'objective': 'SECRET', 'project_context': {'project_id': 'private'}}, path)
    candidates.observe_task({'research_task_id': 'private task', 'revision': 2, 'status': 'closed'}, path)
    view = candidates.observation_summary(path)
    assert view['tool_calls'] == 2 and view['failed_tool_calls'] == 1
    assert view['observed_tasks'] == 1 and view['task_states'] == {'closed': 1}
    assert view['semantic_success'] == 'not_judged'
    assert 'SECRET' not in json.dumps(view) and 'private' not in path.read_bytes().decode('utf-8', errors='ignore')


def test_members_are_preserved_without_claiming_same_root_cause(tmp_path):
    path = tmp_path/'candidates.sqlite3'
    candidates.record_candidate(event(), path)
    candidates.record_candidate(event('b'), path)
    cluster = candidates.list_candidates(path)[0]
    members = candidates.candidate_members(cluster['candidate_id'], path)
    assert len(members) == 2
    assert all(m['semantic_classification'] == 'unreviewed' for m in members)


def test_retention_loss_is_visible_and_reads_do_not_mutate(tmp_path, monkeypatch):
    path = tmp_path/'candidates.sqlite3'
    monkeypatch.setattr(candidates, 'LIMIT', 1)
    candidates.record_candidate(event(), path)
    candidates.record_candidate({**event('b'), 'tool_name': 'get_content_detail'}, path)
    view = candidates.observation_summary(path)
    assert view['evicted_candidates'] == 1 and view['tool_calls'] == 2
    before = path.read_bytes()
    candidates.observation_summary(path)
    candidates.candidate_members(candidates.list_candidates(path)[0]['candidate_id'], path)
    assert before == path.read_bytes()


def test_task_lifecycle_is_observed_by_real_ledger(tmp_path, monkeypatch):
    from runtime import research_ledger
    monkeypatch.setattr(candidates, 'candidate_path', lambda: tmp_path/'candidate.sqlite3')
    monkeypatch.setattr(research_ledger, 'runtime_state_dir', lambda: tmp_path/'state')
    monkeypatch.setattr('runtime.research_index.ResearchIndex.ledger_projection', lambda *a: None)
    research_ledger.open_task(objective='sensitive', budget='bounded', considered=[], task_id='task-one')
    assert candidates.observation_summary()['observed_tasks'] == 1


def manager(tmp_path):
    from runtime.evaluation_maintenance import Maintenance
    return Maintenance(tmp_path/'candidates.sqlite3')


def card(case='content.shell', **kw):
    return dict(case_id=case, title='错误壳不能当正文', prevents='防止登录提示当成正文',
                layer='materials', case_type='regression', critical=True,
                coverage=[case], examples=[{'input_hash': 'a'*64, 'bad_hash': 'b'*64,
                'good_hash': 'c'*64, 'oracle': 'login-shell-v1', 'provenance': 'fixture:checked'}], **kw)


def command(operation, case='content.shell', revision=0, **kw):
    return dict(operation=operation, case_id=case, expected_revision=revision,
                request_id=f'{operation}-{case}-{revision}', actor='codex', rule_version='maintenance-v1',
                skill_version='skill-1', reason='已检查输入输出和判据', evidence=['d'*64]) | kw


def create(m, case='content.shell', **kw):
    return m.execute(command('upsert', case, card=card(case, **kw),
                             review={'reviewer': 'independent-review', 'evidence': ['d'*64], 'verdict': 'accepted'}))


def test_admission_shadow_and_independent_review(tmp_path):
    m = manager(tmp_path)
    bad = command('upsert', card=card(), review={'reviewer':'codex','evidence':['d'*64],'verdict':'accepted'})
    with pytest.raises(ValueError, match='independent'):
        m.execute(bad)
    proposal = command('upsert', card=card(), mode='shadow', request_id='shadow')
    assert m.execute(proposal)['status'] == 'shadow'
    assert not m.view()['cases']
    assert create(m)['revision'] == 1
    assert m.view()['cases'][0]['lifecycle'] == 'shadow'


def test_optimistic_atomic_idempotent_and_human_lock(tmp_path):
    m = manager(tmp_path)
    first = create(m)
    assert create(m) == first
    with pytest.raises(ValueError, match='idempotency'):
        m.execute({**command('upsert', card=card()), 'reason':'changed'})
    with pytest.raises(ValueError, match='revision'):
        m.execute(command('state', revision=0, patch={'frequency':'sample'}))
    m.execute(command('lock', revision=1, human=True))
    with pytest.raises(ValueError, match='locked'):
        m.execute(command('state', revision=2, patch={'frequency':'sample'}))
    assert m.view()['cases'][0]['revision'] == 2


def test_critical_green_cannot_retire_and_axes_are_independent(tmp_path):
    m = manager(tmp_path); create(m)
    with pytest.raises(ValueError, match='retirement'):
        m.execute(command('state', revision=1, patch={'lifecycle':'retired_archive'}))
    row = m.execute(command('state', revision=1, patch={'frequency':'sample','runnability':'external_blocked'}))
    assert row['revision'] == 2
    current = m.view()['cases'][0]
    assert current['lifecycle'] == 'shadow' and current['frequency'] == 'sample'
    assert current['runnability'] == 'external_blocked'


def test_merge_preserves_subexamples_and_compensation_history(tmp_path):
    m = manager(tmp_path); create(m); create(m, 'content.paywall')
    merge = command('merge', revision=1, target_id='content.paywall', target_revision=1,
                    coverage_map={'content.shell':'content.paywall'},
                    review={'reviewer':'other','evidence':['d'*64],'verdict':'accepted'})
    result = m.execute(merge)
    assert len(m.view()['cases'][1]['examples']) >= 1
    change = result['change_id']
    restored = m.execute(command('compensate', revision=2, change_id=change, human=True))
    rows = {c['case_id']:c for c in m.view()['cases']}
    assert rows['content.shell']['lifecycle'] == 'shadow'
    assert rows['content.paywall']['revision'] == 3
    assert restored['affected_trials'] == 'requires_reevaluation'
    assert m.view()['changes'][0]['operation'] == 'compensate'


def test_wrong_root_cause_merge_and_unsafe_recovery_are_rejected(tmp_path):
    m = manager(tmp_path); create(m); create(m,'content.paywall')
    with pytest.raises(ValueError, match='coverage'):
        m.execute(command('merge', revision=1,target_id='content.paywall',target_revision=1,
                          review={'reviewer':'other','evidence':['d'*64],'verdict':'accepted'}))
    assert all(c['revision']==1 for c in m.view()['cases'])


def test_queue_budget_no_agent_and_feedback_is_not_approval(tmp_path):
    m = manager(tmp_path); create(m)
    receipt = m.feedback(case_id='content.shell', revision=1, text='这个判定不对', request_id='human-1')
    assert receipt['status'] == 'waiting_for_agent'
    assert m.feedback(case_id='content.shell', revision=1, text='这个判定不对', request_id='human-1') == receipt
    assert m.work_batch(limit=0)['items'] == []
    assert m.work_batch(limit=99)['limit'] == 20
    assert m.view()['feedback'][0]['status'] == 'waiting_for_agent'


def test_decision_reads_do_not_create_database(tmp_path):
    m = manager(tmp_path)
    assert m.view()['cases'] == [] and not m.path.exists()
