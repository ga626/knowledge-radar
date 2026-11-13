"""平台失败合同：离线复现，不访问账号或正式任务。"""
import hashlib
import json

import pytest


def test_bilibili_metadata_does_not_spend_comment_page(monkeypatch):
    from collectors.platform import bilibili
    calls = []
    def get(url, **kwargs):
        calls.append(url)
        data = {'aid': 123} if url.endswith('/view') else {
            'replies': [{'rpid': 1, 'content': {'message': 'root'}}],
            'cursor': {'is_end': True}}
        return type('Response', (), {'raise_for_status': lambda self: None,
            'json': lambda self: {'code': 0, 'data': data}})()
    monkeypatch.setattr(bilibili.httpx, 'get', get)
    result = bilibili.collect_comments('BVfixture', options={
        'root_limit': 1, 'reply_limit': 0, 'total_limit': 10, 'max_pages': 1})
    assert len(result['comments']) == 1
    assert result['comments_complete'] is True
    assert result['comments_metadata']['pages'] == 1
    assert result['comments_metadata']['metadata_requests'] == 1
    assert len(calls) == 2


def test_bilibili_comment_page_budget_still_bounds_requests(monkeypatch):
    from collectors.platform import bilibili
    calls = []
    def get(url, **kwargs):
        calls.append(url)
        data = {'aid': 123} if url.endswith('/view') else {
            'replies': [{'rpid': 1, 'content': {'message': 'root'}}],
            'cursor': {'is_end': False, 'next': 2}}
        return type('Response', (), {'raise_for_status': lambda self: None,
            'json': lambda self: {'code': 0, 'data': data}})()
    monkeypatch.setattr(bilibili.httpx, 'get', get)
    result = bilibili.collect_comments('BVfixture', options={
        'root_limit': 2, 'reply_limit': 0, 'total_limit': 10, 'max_pages': 1})
    assert len(result['comments']) == 1 and len(calls) == 2
    assert result['comments_complete'] is False
    assert result['comments_metadata']['stop_reason'] == 'comment_page_budget'


@pytest.mark.parametrize('platform,expected', [('V2EX', 'platform_default'), ('unknown', 'unknown')])
def test_requested_sort_is_not_reported_as_observed_sort(platform, expected):
    from runtime.comments_contract import normalize_comments
    result = normalize_comments({'comments': [], 'comments_complete': True},
        platform=platform, source_url='https://example.org/t/1', options={'order': 'relevance'})
    assert result['comments_metadata']['actual_order'] == expected


@pytest.mark.parametrize('state_key', ['status','state'])
def test_failed_task_exposes_surviving_fixed_assets_before_retry(tmp_path, state_key):
    from runtime.tasks import TaskStore
    from runtime.task_service import TaskService
    store = TaskStore(str(tmp_path / 'tasks.sqlite3'))
    service = TaskService(store)
    value = {'content': 'description', 'evidence_assets': [{'version': 'fixed-transcript'}],
        'content_coverage': {'parts': {'transcript': {'status': 'complete'},
                                    'comments': {state_key: 'queued'}}}}
    artifact = {'schema': 'knowledgeradar-task-artifact/v1', 'task_id': 'partial',
        'attempt_id': 'one', 'identity': {'source': 'fixture'}, 'result': value}
    path = service.root / 'partial.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(artifact).encode()
    path.write_bytes(raw)
    store.upsert_task('partial', 'durable_content_detail', 'B站', status='failed', result_path=str(path),
        metadata={'attempt_id': 'one', 'source_identity': 'fixture',
                  'artifact_sha256': hashlib.sha256(raw).hexdigest()})
    result = service.result('partial')
    assert result['result']['content_coverage']['parts']['transcript']['status'] == 'complete'
    assert result['result']['content_coverage']['parts']['comments'][state_key] == 'failed'
    assert result['available_evidence'] == [{'version': 'fixed-transcript'}]
    assert result['next_action'].startswith('Read the available fixed evidence')
    assert path.read_bytes() == raw
