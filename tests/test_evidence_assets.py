from concurrent.futures import ThreadPoolExecutor
import hashlib
import pytest
from kr_core.evidence_assets import EvidenceAssets
from kr_core.evidence_store import EvidenceStore


def test_academic_body_keeps_actual_fetch_url_in_fixed_evidence(tmp_path):
    store = EvidenceStore(str(tmp_path / 'academic.jsonl'))
    actual = 'https://www.ebi.ac.uk/europepmc/webservices/rest/PMC7092803/fullTextXML'
    item = {'url': 'https://repository.example/article', 'title': 'Article', 'raw': {
        'fulltext_resolution': {'text': 'Verified body. ' * 20, 'coverage': 'partial',
            'mode': 'public_xml_text', 'provenance': {'provider_id': 'europepmc',
                'source_url': 'https://repository.example/article', 'resolved_url': actual}}}}
    store.append_academic_search(query='body', response={'items': [item]})
    ref = item['evidence_assets'][0]
    assert ref['source_url'] == actual
    assert store.assets.fetch(ref['version_id'])['asset']['provenance']['resolved_url'] == actual
from kr_core.evidence_coverage import content_coverage


def test_atomic_publish_failure_and_readonly_legacy_plan(tmp_path, monkeypatch):
    from kr_core.evidence_migration import migration_plan
    import json
    legacy = tmp_path / 'legacy.jsonl'
    raw = (json.dumps({'record_type': 'detail', 'id': 'legacy-1', 'payload': {'content': 'x' * 4000}}) + '\n').encode()
    legacy.write_bytes(raw)
    plan = migration_plan(legacy)
    assert plan['read_only'] and not plan['automatic_apply']
    assert plan['rows'][0]['status'] == 'partial_legacy_excerpt'
    assert not plan['rows'][0]['completeness_proven']
    assert legacy.read_bytes() == raw
    assets = EvidenceAssets(tmp_path / 'assets')
    def fail_publish(*args):
        raise OSError('simulated_publish_failure')
    monkeypatch.setattr('kr_core.evidence_assets.os.replace', fail_publish)
    with pytest.raises(OSError, match='publish_failure'):
        assets.put('https://example.org/atomic', '完整原件')
    assert assets.search('') == []
    assert not list(assets.root.glob('*.utf8'))
    assert not list(assets.root.glob('.asset-*.tmp'))


def test_complete_old_version_unicode_and_summary(tmp_path):
    store = EvidenceStore(str(tmp_path / 'evidence.jsonl'))
    text = ('前文🙂\r\n' * 3000) + '末尾反例：不可忽略'
    response = {'content': text, 'title': '测试'}
    store.append_detail(platform='web', url='https://example.org/a', response=response)
    ref = response['evidence_assets'][0]
    assert len(store.recent()[0]['payload']['content']) == 4000
    parts, offset = [], 0
    while True:
        item = store.assets.fetch(ref['version_id'], start=offset, max_chars=173)
        parts.append(item['text'])
        if item['next_start'] is None:
            break
        offset = item['next_start']
    assert ''.join(parts) == text
    assert hashlib.sha256(''.join(parts).encode()).hexdigest() == ref['raw_sha256']
    newer = store.assets.put('https://example.org/a', '新版本')
    assert newer['version_id'] != ref['version_id']
    assert store.assets.fetch(ref['version_id'], start=len(text) - 10)['text'] == text[-10:]
    assert store.assets.search('末尾反例')[0]['version_id'] == ref['version_id']


def test_duplicate_concurrency_capacity_integrity_withdrawal(tmp_path):
    assets = EvidenceAssets(tmp_path, max_bytes=30)
    with ThreadPoolExecutor(max_workers=4) as pool:
        rows = list(pool.map(lambda _: assets.put('https://example.org', 'abc中文'), range(8)))
    assert len({r['version_id'] for r in rows}) == 1
    row = rows[0]
    assets.reference(row['version_id'], 'report:1')
    assert assets.retention_plan()['reference_count'] == 1
    with pytest.raises(ValueError, match='capacity'):
        assets.put('https://example.org/b', 'x' * 31)
    path = assets._file(row['raw_sha256'])
    path.write_text('corrupt', encoding='utf-8')
    with pytest.raises(ValueError, match='integrity'):
        assets.fetch(row['version_id'])
    path.unlink()
    with pytest.raises(ValueError, match='missing'):
        assets.fetch(row['version_id'])
    assets.withdraw(row['source_id'])
    assert assets.search('abc') == []
    with pytest.raises(ValueError, match='withdrawn'):
        assets.put('https://example.org', 'changed')


def test_default_asset_root_shared(tmp_path, monkeypatch):
    monkeypatch.setenv('KR_LOG_DIR', str(tmp_path / 'logs'))
    monkeypatch.setenv('KR_EVIDENCE_ROOT', str(tmp_path / 'assets'))
    store = EvidenceStore()
    response = {'content': '原文'}
    store.append_detail(platform='web', url='https://example.org', response=response)
    assert EvidenceAssets().fetch(response['evidence_assets'][0]['version_id'])['text'] == '原文'


@pytest.mark.parametrize('component,source_kind,coverage,locator', [
    ('pypdf', 'pdf_text', 'complete_selectable_text', {'page': 2, 'kind': 'paragraph'}),
    ('qwen3-asr-0.6b', 'asr', 'processed_audio_chunks', {'start_s': 0., 'end_s': 12., 'kind': 'audio_chunk'}),
])
def test_local_processing_coverage_and_locator_survive_fixed_read_without_rewriting_legacy(
    tmp_path, monkeypatch, component, source_kind, coverage, locator,
):
    from runtime.optional_media import _save_result
    monkeypatch.setenv('KR_LOG_DIR', str(tmp_path / 'logs'))
    monkeypatch.setenv('KR_EVIDENCE_ROOT', str(tmp_path / 'assets'))
    text = 'parsed material'
    input_sha = 'a' * 64
    url = 'https://local-evidence.invalid/sha256/' + input_sha
    legacy = {'content': text, 'collector': component,
              'content_coverage': {'parts': {'content': {'coverage': 'unknown'}}},
              'content_locators': [{'start': 0, 'end': len(text), **locator}]}
    EvidenceStore().record_detail(platform='local_document', url=url, response=legacy)
    old = legacy['evidence_assets'][0]
    parsed = {'text': text, 'status': 'acquired', 'component': component,
              'source_kind': source_kind, 'content_locators': legacy['content_locators']}
    if source_kind == 'pdf_text':
        parsed['coverage'] = coverage
    result = _save_result(parsed, {'input_sha256': input_sha})
    ref = result['evidence_assets'][0]
    fetched = EvidenceAssets().fetch(ref['version_id'])
    assert ref['version_id'] != old['version_id']
    assert ref['coverage'] == coverage == result['content_coverage']['parts']['content']['coverage']
    assert ref['provenance']['input_sha256'] == input_sha
    assert fetched['text'] == text and fetched['locators'][0].items() >= locator.items()
    assert EvidenceAssets().fetch(old['version_id'])['asset']['coverage'] == 'unknown'
    assert result['quality_status'] == 'unmeasured'


def test_archive_boundary_does_not_swallow_cancellation(tmp_path, monkeypatch):
    from runtime.execution_context import ExecutionStopped
    store = EvidenceStore(str(tmp_path/'events.jsonl'))
    def cancelled(**_):
        raise ExecutionStopped('execution_superseded_or_cancelled')
    monkeypatch.setattr(store, 'append_detail', cancelled)
    response = {'content': 'late body'}
    with pytest.raises(ExecutionStopped):
        store.record_detail(platform='fixture', url='https://example.org', response=response)
    assert 'evidence_storage_gaps' not in response
    assert not (tmp_path/'events.jsonl').exists()


def test_evidence_large_records_survive_multiple_processes(tmp_path):
    import json, os, subprocess, sys
    from pathlib import Path
    path = tmp_path / 'shared.jsonl'
    code = '''
import os,sys
sys.path.insert(0,os.environ['PYTHONPATH'])
from kr_core.evidence_store import EvidenceStore, EvidenceRecord
store = EvidenceStore(sys.argv[1])
for i in range(12):
    store.append(EvidenceRecord('detail','test','https://example.org',
        {'writer':sys.argv[2],'index':i,'content':'中文🙂'*20000}))
'''
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / 'src'), PYTHONIOENCODING='utf-8')
    processes = [subprocess.Popen([sys.executable, '-X', 'utf8', '-c', code, str(path), str(i)], env=env,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE) for i in range(3)]
    for process in processes:
        _, err = process.communicate(timeout=20)
        assert process.returncode == 0, err.decode('utf-8')
    rows = [json.loads(line)['payload'] for line in path.read_text(encoding='utf-8').splitlines()]
    assert {(row['writer'], row['index']) for row in rows} == {(str(w), i) for w in range(3) for i in range(12)}
    assert all(row['content'] == '中文🙂'*20000 for row in rows)


def test_three_media_counterexamples_and_queryable_pending():
    response = {'content': '正文', 'images': [], 'comments': [], 'comment_count': 24, 'transcript': 'pending'}
    coverage = content_coverage(response, {'include_images': True, 'include_comments': True, 'include_transcript': False})
    assert coverage['parts']['images']['state'] == 'missing'
    assert coverage['parts']['comments']['state'] == 'missing'
    assert coverage['parts']['transcript']['state'] == 'not_requested'
    response['pending_tasks'] = [{'task_id': 'fake', 'status': 'running', 'task_type': 'asr'}]
    assert content_coverage(response, {'include_transcript': True}, task_lookup=lambda _: None)['parts']['transcript']['state'] == 'missing'
    lookup = lambda task_id: {'task_id': task_id, 'task_type': 'asr', 'status': 'running'}
    pending = content_coverage(response, {'include_transcript': True}, task_lookup=lookup)
    assert pending['parts']['transcript']['pending_task_ids'] == ['fake']
    assert not pending['complete']
    assert not pending['request_satisfied']
