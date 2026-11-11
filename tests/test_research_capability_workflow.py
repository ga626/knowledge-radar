"""能力优先入口与失败可见性回归；不把合同测试当研究质量证明。"""
import hashlib
import json
import time
from pathlib import Path

import pytest


def test_host_results_can_be_returned_after_search_without_prior_route(tmp_path, monkeypatch):
    from runtime import host_search_handoff, research_ledger
    from runtime.research_index import ResearchIndex
    monkeypatch.setattr(research_ledger, 'read_task', lambda **kw: {'project_id': 'demo'})
    index = ResearchIndex(tmp_path / 'index.sqlite3')
    items = [{'url': 'https://example.org/source', 'title': 'Source', 'host_reference': 'turn1search0'}]
    result = host_search_handoff.submit('research-demo', '', items, host_tool='web.run', query='real query', index=index)
    assert result['status'] == 'recorded'
    assert result['verification'] == 'host_declared_not_native_verified'
    again = host_search_handoff.submit('research-demo', '', items, host_tool='web.run', query='real query', index=index)
    assert again['batch_id'] == result['batch_id']
    assert len(index.read('research-demo', view='host_batches')['items']) == 1


def test_invalid_host_results_leave_no_registered_batch(tmp_path, monkeypatch):
    from runtime import host_search_handoff, research_ledger
    from runtime.research_index import ResearchIndex
    monkeypatch.setattr(research_ledger, 'read_task', lambda **kw: {'project_id': 'demo'})
    index = ResearchIndex(tmp_path / 'index.sqlite3')
    with pytest.raises(ValueError):
        host_search_handoff.submit('research-demo', '', [{'url': 'http://127.0.0.1/', 'title': 'private', 'host_reference': 'x'}],
                                   host_tool='web.run', query='query', index=index)
    assert index.read('research-demo', view='host_batches')['items'] == []


def test_routing_advice_does_not_require_permission_for_host_search():
    from runtime.composition import _HOST_INTERNAL_WEB_WAVE
    assert _HOST_INTERNAL_WEB_WAVE['admission'] == 'agent_selected_no_prior_route_required'


def test_skill_is_capability_entry_with_optional_depth_references():
    root = Path(__file__).resolve().parents[1] / 'config/codex-product/plugin/knowledgeradar-research/skills/research'
    text = (root / 'SKILL.md').read_text(encoding='utf-8')
    assert len(text) < 3600
    for name in ('recruitment', 'customer-evidence', 'media'):
        assert f'references/{name}.md' in text
        assert (root / 'references' / f'{name}.md').is_file()
    assert '先完成关键claim' not in text


def test_terminal_task_does_not_claim_enhancement_is_still_queued(tmp_path):
    from runtime.task_service import TaskService
    from runtime.tasks import TaskStore
    service = TaskService(TaskStore(tmp_path / 'tasks.sqlite3'))
    task = service.store.upsert_task('task-demo', task_type='durable_content_detail', platform='bilibili', target='video',
                                    metadata={'attempt_id': 'a', 'source_identity': 's'})
    task_id = task['task_id']
    folder = service.root / task_id
    folder.mkdir(parents=True)
    raw = json.dumps({'schema': 'knowledgeradar-task-artifact/v1', 'task_id': task_id, 'attempt_id': 'a',
        'identity': {'source': 's'}, 'result': {'content': 'description survives', 'enhancement_status': 'queued',
            '_enhancement_pending': True, 'content_coverage': {'parts': {'transcript': {'status': 'pending'}}, 'request_satisfied': False}}}).encode()
    path = folder / 'result.json'
    path.write_bytes(raw)
    service.store.mark_failed(task_id, 'deadline', error_code='deadline_exceeded', result_path=str(path),
        metadata={'artifact_sha256': hashlib.sha256(raw).hexdigest(), 'media_progress': {'stage': 'transcription', 'completed_chunks': 11}})
    before = path.read_bytes()
    result = service.result(task_id)
    assert result['status'] == 'failed'
    assert result['result']['enhancement_status'] == 'failed'
    assert result['result']['content_coverage']['parts']['transcript']['status'] == 'failed'
    assert result['result']['content'] == 'description survives'
    assert result['media_progress']['completed_chunks'] == 11
    assert result['automatic_retry'] is False
    assert path.read_bytes() == before


def test_progress_is_attempt_guarded_and_never_reopens_terminal_task(tmp_path):
    from runtime.execution_context import execution_scope, record_execution_progress
    from runtime.tasks import TaskStore
    store = TaskStore(tmp_path / 'tasks.sqlite3')
    task = store.upsert_task('task-demo', task_type='durable_optional_media', platform='', metadata={'attempt_id': 'a', 'execution_id': 'e'})
    store.mark_running(task['task_id'])
    context = {'store': store, 'KR_DURABLE_TASK_ID': task['task_id'], 'attempt_id': 'a', 'execution_id': 'e', 'deadline_at': time.time()+30}
    with execution_scope(context):
        record_execution_progress('transcription', completed_chunks=2, model_load_s=1.2)
    with execution_scope({**context, 'execution_id': 'old'}):
        record_execution_progress('transcription', completed_chunks=99)
    assert store.get_task(task['task_id'])['metadata']['media_progress']['completed_chunks'] == 2
    store.mark_cancelled(task['task_id'], 'cancelled')
    with execution_scope(context):
        record_execution_progress('transcription', completed_chunks=100)
    assert store.get_task(task['task_id'])['status'] == 'cancelled'
    assert store.get_task(task['task_id'])['metadata']['media_progress']['completed_chunks'] == 2


def test_native_asr_failure_retains_stage_metrics_without_publishing_text(tmp_path, monkeypatch):
    import numpy as np
    from types import SimpleNamespace
    from runtime import optional_media_worker as worker, media_decode
    from runtime.execution_context import execution_scope
    from runtime.tasks import TaskStore, compact_task_ref
    monkeypatch.setenv('KR_MEDIA_CACHE_DIR', str(tmp_path/'cache'))
    store = TaskStore(tmp_path/'tasks.sqlite3')
    task = store.upsert_task('asr-demo',task_type='durable_optional_media',platform='',
        metadata={'attempt_id':'a','execution_id':'e'})
    store.mark_running(task['task_id'])
    class Model:
        calls = 0
        def create_stream(self):
            return SimpleNamespace(accept_waveform=lambda *a:None,
                result=SimpleNamespace(text='speech',tokens=['KRlanguage',' English','<asr_text>','speech']))
        def decode_stream(self, stream):
            self.calls += 1
            if self.calls == 2:
                raise RuntimeError('decoder failed')
    monkeypatch.setattr(worker,'_load_asr_model',lambda *a:(Model(),False))
    monkeypatch.setattr(media_decode,'audio_chunks',lambda p:iter([(0,np.zeros(16000),16000),(1,np.zeros(16000),16000)]))
    request = dict(component='qwen3-asr-0.6b',input_path='fixture',input_sha256='s',model_root='fixture',
                   model_identity='m',language='auto',device='cpu',threads=4)
    with execution_scope({'store':store,'KR_DURABLE_TASK_ID':task['task_id'],'attempt_id':'a','execution_id':'e','deadline_at':time.time()+30}):
        with pytest.raises(RuntimeError,match='decoder failed'):
            worker.native_asr(request)
    progress = store.get_task(task['task_id'])['metadata']['media_progress']
    assert progress['completed_chunks'] == 1 and progress['stage'] == 'transcription'
    assert progress['model_load_s'] >= 0 and progress['transcription_s'] >= 0
    assert 'text' not in progress
    assert compact_task_ref(store.get_task(task['task_id']))['media_progress'] == progress
