"""验证同进程取消、块恢复、依赖共存拒绝与公共PDF策略。"""
import sys
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest


def test_asr_resumes_completed_chunks_and_invalidates_changed_input(monkeypatch, tmp_path):
    from runtime import optional_media_worker as worker, media_decode
    monkeypatch.setenv('KR_MEDIA_CACHE_DIR', str(tmp_path / 'cache'))
    monkeypatch.setattr(media_decode, 'audio_chunks', lambda p: iter([(0, np.zeros(16000), 16000), (1, np.zeros(16000), 16000)]))
    calls = []
    fail = True
    class Model:
        def create_stream(self):
            result = SimpleNamespace(text='KRlanguage English<asr_text>original speech',
                                     tokens=['KRlanguage', ' English', '<asr_text>', 'original speech'])
            return SimpleNamespace(accept_waveform=lambda *a: None, result=result)
        def decode_stream(self, stream):
            calls.append('inference')
            if len(calls) == 2 and fail:
                raise RuntimeError('interrupted')
    monkeypatch.setattr(worker, '_load_asr_model', lambda *a: (Model(), True))
    request = dict(component='qwen3-asr-0.6b', input_path='fixture.wav', input_sha256='sha-one',
                   model_root='fixture', model_identity='model-one', language='zh', device='cpu', threads=4)
    with pytest.raises(RuntimeError, match='interrupted'):
        worker.native_asr(request)
    fail = False
    result = worker.native_asr(request)
    assert result['asr_metrics']['resumed_chunks'] == 1 and len(calls) == 3
    assert result['content_locators'][1]['start_s'] == 1
    for change in ({'input_sha256': 'sha-two'}, {'language': 'en'}, {'model_identity': 'model-two'}):
        before = len(calls)
        result = worker.native_asr({**request, **change})
        assert result['asr_metrics']['resumed_chunks'] == 0 and len(calls) == before + 2


@pytest.mark.parametrize('stop', ['cancel', 'deadline'])
def test_native_call_keeps_lease_until_return_and_never_archives_late(monkeypatch, tmp_path, stop):
    import wave
    from runtime import optional_media, optional_media_worker, component_environment, leases, resource_concurrency
    from runtime.task_service import TaskService
    from runtime.tasks import TaskStore
    for key, value in {'KR_TASK_TEST_MODE': '1', 'KR_DATA_ROOT': str(tmp_path/'data'),
                       'KR_STATE_DIR': str(tmp_path/'state'), 'KR_LOG_DIR': str(tmp_path/'logs'),
                       'KR_OPTIONAL_COMPONENT_RECEIPTS_DIR': str(tmp_path/'receipts')}.items():
        monkeypatch.setenv(key, value)
    coordinator = leases.RuntimeLeaseCoordinator(tmp_path/'leases.sqlite3')
    monkeypatch.setattr(leases, '_COORDINATOR', coordinator)
    monkeypatch.setattr(resource_concurrency, 'get_runtime_lease_coordinator', lambda: coordinator)
    receipts = optional_media.receipt_root()
    receipts.mkdir(parents=True)
    (receipts/'qwen3-asr-0.6b.json').write_text('{}')
    source = tmp_path/'voice.wav'
    with wave.open(str(source), 'wb') as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(16000)
        stream.writeframes(b'\0'*32000)
    monkeypatch.setattr(optional_media, 'validate_receipt', lambda c: {'python': sys.executable, 'model_root': str(tmp_path)})
    monkeypatch.setattr(component_environment, 'component_environment', lambda *a, **kw: {})
    entered, release = threading.Event(), threading.Event()
    def native(request):
        entered.set()
        assert release.wait(10)
        return {'status': 'acquired', 'component': request['component'], 'text': 'late text'}
    monkeypatch.setattr(optional_media_worker, 'execute', native)
    from kr_core.evidence_store import EvidenceStore
    archives = []
    monkeypatch.setattr(EvidenceStore, 'append_detail', lambda *a, **kw: archives.append(kw))
    service = TaskService(TaskStore(tmp_path/'tasks.sqlite3'))
    task = service.submit('optional_media', {'component': 'qwen3-asr-0.6b', 'input_path': str(source),
        'approved_input_root': str(tmp_path), 'input_sha256': optional_media.file_sha(source)}, timeout_s=2 if stop == 'deadline' else 10)
    try:
        assert entered.wait(5)
        if stop == 'cancel':
            service.cancel(task['task_id'])
        else:
            until = time.monotonic()+5
            while service.store.get_task(task['task_id'])['status'] == 'running' and time.monotonic() < until:
                time.sleep(.02)
        row = service.store.get_task(task['task_id'])
        assert row['status'] == ('cancelled' if stop == 'cancel' else 'failed')
        assert not resource_concurrency.resource_available('local_heavy')
        assert service._enhancement_active == 1
        light = service.submit('fixture_echo', {'value': 'other evidence'}, timeout_s=5)
        until = time.monotonic()+4
        while service.store.get_task(light['task_id'])['status'] != 'completed' and time.monotonic() < until:
            time.sleep(.02)
        assert service.result(light['task_id'])['result']['value'] == 'other evidence'
    finally:
        release.set()
        until = time.monotonic()+5
        while service._enhancement_active and time.monotonic() < until:
            time.sleep(.02)
    assert service._enhancement_active == 0 and resource_concurrency.resource_available('local_heavy')
    assert not archives and not service.store.get_task(task['task_id']).get('result_path')


def test_component_checks_transitive_extras_and_never_replaces_core(monkeypatch, tmp_path):
    from runtime import component_environment as environment, model_roles
    class Dist:
        def __init__(self, name, version, requires=()):
            self.metadata = {'Name': name}
            self.version = version
            self.requires = requires
    core = [Dist('shared-lib', '1.0')]
    extra = [Dist('office', '2.0', ['shared-lib>=2; extra == "format-office"']), Dist('shared-lib', '2.0')]
    monkeypatch.setattr(environment, 'distributions', lambda **kw: extra if kw else core)
    monkeypatch.setitem(model_roles.COMPONENTS, 'fixture', {'requirements': ['office==2.0'], 'extras': ['format-office']})
    before = list(sys.path)
    with pytest.raises(ValueError, match='inprocess_component_dependency_conflict:shared-lib'):
        environment.component_environment('fixture', {'python': str(tmp_path/'Scripts/python.exe')}, activate=True)
    assert sys.path == before


def test_pdf_fulltext_and_academic_page_policy_remain_separate():
    from io import BytesIO
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
    from runtime.pdf_text import extract_pdf_text
    writer = PdfWriter()
    for number in range(1, 8):
        page = writer.add_blank_page(300, 300)
        font = DictionaryObject({NameObject('/Type'): NameObject('/Font'),
            NameObject('/Subtype'): NameObject('/Type1'), NameObject('/BaseFont'): NameObject('/Helvetica')})
        page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'):
            DictionaryObject({NameObject('/F1'): writer._add_object(font)})})
        stream = DecodedStreamObject()
        stream.set_data(f'BT /F1 12 Tf 10 100 Td (page {number} full text) Tj ET'.encode())
        page[NameObject('/Contents')] = writer._add_object(stream)
    output = BytesIO()
    writer.write(output)
    academic = extract_pdf_text(output.getvalue(), max_pages=5)
    local = extract_pdf_text(output.getvalue())
    # 文字层的 coverage 只描述请求范围；没有把未请求的后两页冒充已读。
    assert academic['pages_read'] == 5 and academic['coverage'] == 'complete_selectable_text'
    assert academic['page_count'] == 7 and academic['requested_pages'] == [1, 2, 3, 4, 5]
    from runtime.document_contract import finish_document
    scoped = finish_document(academic)
    assert scoped['request_satisfied'] and not scoped['source_pages_exhausted']
    assert local['pages_read'] == 7 and local['coverage'] == 'complete_selectable_text'
    assert 'page 7 full text' in local['text'] and 'page 6 full text' not in academic['text']


def test_paid_video_steps_reuse_success_but_not_resource_wait(monkeypatch, tmp_path):
    import video_processor as video
    from runtime.resource_concurrency import ResourceDeferred
    monkeypatch.setenv('KR_MEDIA_CACHE_DIR', str(tmp_path/'cache'))
    calls = []
    def invoke():
        calls.append('paid')
        return ('whole response', 'selected model')
    request = {'prompt': 'all original content', 'model': 'selected model'}
    assert video._completed_call(request, invoke) == invoke_result('whole response')
    def busy():
        raise ResourceDeferred('frame_vision')
    with pytest.raises(ResourceDeferred):
        video._completed_call({'prompt': 'second step'}, busy)
    assert video._completed_call({'prompt': 'second step'}, lambda: invoke_result('resumed')) == invoke_result('resumed')
    assert video._completed_call(request, invoke) == invoke_result('whole response')
    assert len(calls) == 1
    video._completed_call({**request, 'prompt': 'changed original'}, invoke)
    assert len(calls) == 2


def invoke_result(text):
    return (text, 'selected model')


def test_explicit_visual_request_is_not_skipped_by_low_density(monkeypatch):
    import video_processor as video
    monkeypatch.setattr(video, '_score_density', lambda **kw: (1, 'low density', 'model'))
    monkeypatch.setattr(video, '_visual_dependency_adjustment', lambda v, score: (score, ''))
    calls = []
    def analyze(*args):
        calls.append(args)
        return {'analysis_time_s': 0, 'core_summary': 'requested visual evidence'}
    monkeypatch.setattr(video, 'deep_analyze_video', analyze)
    result = video.VideoProcessor().process(video.VideoInfo(title='plain footage', frame_samples=[{'pts_s': 1}]), force_deep=True)
    assert result.deep_analysis['core_summary'] == 'requested visual evidence' and len(calls) == 1
