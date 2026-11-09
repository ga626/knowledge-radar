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
