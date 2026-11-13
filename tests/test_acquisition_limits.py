"""获取接续的离线合同；不访问平台、不使用账号或付费出口。"""
import json
import time
from types import SimpleNamespace

import pytest


@pytest.fixture(autouse=True)
def isolated_runtime(tmp_path, monkeypatch):
    for key, suffix in [('KR_DATA_ROOT','data'),('KR_STATE_DIR','state'),('KR_LOG_DIR','logs'),('KR_EVIDENCE_ROOT','evidence')]:
        monkeypatch.setenv(key,str(tmp_path/suffix))


def test_long_lived_checkpoint_is_explicit_and_identity_bound(tmp_path):
    from runtime.operation_checkpoint import OperationCheckpoint
    snapshot = OperationCheckpoint(tmp_path, {'input': 'one'}, {'model': 'one'}, max_age_s=None)
    snapshot.write({'chunks': ['fixed chunk']})
    data = json.loads(snapshot.path.read_text(encoding='utf-8')); data['at'] -= 2*86400
    snapshot.path.write_text(json.dumps(data), encoding='utf-8')
    assert snapshot.read()['chunks'] == ['fixed chunk']
    assert not OperationCheckpoint(tmp_path, {'input': 'one'}, {'model': 'two'}, max_age_s=None).read()
    assert not OperationCheckpoint(tmp_path, {'input': 'one'}, {'model': 'one'}).read()


def test_pdf_large_document_allows_small_explicit_workset(tmp_path):
    from pypdf import PdfWriter
    from runtime.pdf_text import _extract_pdf_text
    path = tmp_path/'large.pdf'; writer = PdfWriter()
    for _ in range(501): writer.add_blank_page(width=100, height=100)
    writer.write(path)
    options = dict(max_pages=None, max_bytes=32*1024*1024, max_chars=2000000)
    result = _extract_pdf_text(path, page_numbers=[500,501], **options)
    assert result['page_count'] == 501 and result['requested_pages'] == [500,501]
    assert result['pages_read'] == 2
    with pytest.raises(ValueError, match='page_budget'):
        _extract_pdf_text(path, page_numbers=None, **options)


def test_youtube_empty_page_with_token_is_not_exhausted(monkeypatch):
    from collectors.platform import youtube
    monkeypatch.setattr(youtube, 'youtube_api_key', lambda: 'fixture-not-secret')
    params = []
    monkeypatch.setattr(youtube, '_youtube_get', lambda operation, options: params.append(options) or {'items': [], 'nextPageToken': 'next-two'})
    result = youtube.search_youtube('question', page_token='page-one')
    assert params[0]['pageToken'] == 'page-one'
    assert result['metadata']['continuation']['next_page_token'] == 'next-two'
    assert result['metadata']['continuation']['status'] == 'more_available'


def test_youtube_repeated_cursor_is_not_reported_exhausted(monkeypatch):
    from collectors.platform import youtube
    monkeypatch.setattr(youtube, 'youtube_api_key', lambda: 'fixture-not-secret')
    monkeypatch.setattr(youtube, '_youtube_get', lambda *a: {'items': [], 'nextPageToken': 'same'})
    result = youtube.search_youtube('question', page_token='same')
    assert result['metadata']['continuation']['status'] == 'cursor_not_advanced'


def test_bilibili_search_page_is_forwarded_and_unknown_end_is_honest(monkeypatch):
    from collectors.platform import bilibili
    calls = []
    monkeypatch.setattr(bilibili.httpx, 'get', lambda *a, **kw: calls.append(kw) or SimpleNamespace(
        status_code=200, json=lambda: {'code': 0, 'data': {'result': []}}))
    result = bilibili.legacy_search_bilibili('question', page_size=20, page=2)
    assert calls[0]['params']['page'] == 2 and calls[0]['params']['page_size'] == 20
    assert result['metadata']['continuation']['status'] == 'end_unverified'


def test_asr_overflow_splits_instead_of_losing_whole_chunk(tmp_path, monkeypatch):
    import numpy as np
    from runtime import media_decode, optional_media_worker as worker
    monkeypatch.setenv('KR_MEDIA_CACHE_DIR', str(tmp_path/'cache'))
    monkeypatch.setattr(media_decode, 'audio_chunks', lambda *a: iter([(0, np.zeros(480000),16000)]))
    lengths = []
    class Model:
        def create_stream(self):
            return SimpleNamespace(accept_waveform=lambda rate, samples: lengths.append(len(samples)))
        def decode_stream(self, stream):
            stream.result = SimpleNamespace(text='speech', tokens=['overflow']*1024 if lengths[-1] > 240000 else ['KRlanguage',' English','<asr_text>','speech'])
    monkeypatch.setattr(worker, '_load_asr_model', lambda *a: (Model(), True))
    result = worker.native_asr(dict(component='qwen3-asr-0.6b',input_path='fixture',input_sha256='fixture',
        model_root='fixture',model_identity='fixture',language='auto',device='cpu',threads=4))
    assert result['text'] == 'speech\nspeech'
    assert lengths == [480000,240000,240000]
    assert result['content_locators'][0]['start_s'] == 0
    assert result['asr_metrics']['split_chunks'] == 1


def test_task_cache_pin_survives_pid_exit_but_not_expiry(tmp_path, monkeypatch):
    from runtime.storage_lifecycle import StorageLeases
    from runtime.tasks import TaskStore
    cache=tmp_path/'cache'; cache.mkdir()
    store=TaskStore(tmp_path/'tasks.sqlite3')
    store.upsert_task('task-one','fixture','fixture',metadata={'overall_deadline_at':time.time()+600})
    leases=StorageLeases(tmp_path/'leases.sqlite3')
    leases.pin_task(cache, 'task-one', store.db_path, time.time()+600)
    assert leases.protected(cache/'piece')
    store.mark_failed('task-one','stopped')
    assert not leases.protected(cache/'piece')


def test_public_wave_reduces_concurrency_from_observed_failures(monkeypatch):
    from search_providers import concurrency
    monkeypatch.setenv('KR_WEB_SEARCH_MAX_WORKERS', '5')
    for _ in range(3): concurrency.record_provider_outcome('fixture-bad', elapsed_s=20, failed=True)
    result = concurrency.compute_wave_concurrency(['fixture-bad','fixture-other','fixture-third'])
    assert result['selected_workers'] < 3
    assert 'observed_latency_or_errors' in result['reasons']


def test_running_search_late_candidate_is_preserved_without_new_send(monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from search_providers import service
    from search_providers.models import WebSearchRequest, SearchProviderResult
    from runtime.research_index import ResearchIndex
    ready = threading.Event(); captured=[]
    monkeypatch.setattr(ResearchIndex, 'candidates', lambda self, *a, **kw: captured.append((a,kw)))
    monkeypatch.setattr('runtime.tool_trace.current_tool_trace', lambda: {'research_task_id':'fixture-task','trace_id':'fixture-trace'})
    with ThreadPoolExecutor(max_workers=1) as executor:
        future=executor.submit(lambda: (ready.wait(2), ('fixture',[SearchProviderResult('title','https://example.com')],None))[1])
        service._retain_late_future(future, WebSearchRequest('question'))
        ready.set(); future.result()
    assert len(captured) == 1 and captured[0][0][0] == 'fixture-task'


def test_explicit_frame_workset_can_read_high_resolution(tmp_path):
    import av
    import numpy as np
    from runtime.media_decode import sample_frames
    path=tmp_path/'video.mp4'
    with av.open(str(path),'w') as container:
        stream=container.add_stream('mpeg4',rate=2);stream.width=1280;stream.height=720
        for value in [60,120,180,240]:
            frame=av.VideoFrame.from_ndarray(np.full((720,1280,3),value,dtype=np.uint8),format='rgb24')
            for packet in stream.encode(frame):container.mux(packet)
        for packet in stream.encode():container.mux(packet)
    rows=sample_frames(path,max_frames=2,frame_times_s=[0,1],image_max_side=1280)
    import base64,io
    from PIL import Image
    assert len(rows) == 2
    assert Image.open(io.BytesIO(base64.b64decode(rows[0]['image_base64']))).width == 1280


def test_asr_reuses_verified_legacy_completed_chunk(tmp_path, monkeypatch):
    import numpy as np
    from runtime import optional_media_worker as worker, media_decode
    from runtime.operation_checkpoint import OperationCheckpoint
    from runtime.media_cache import media_cache_subdir
    from runtime.model_roles import ASR_ENGINE
    monkeypatch.setenv('KR_MEDIA_CACHE_DIR',str(tmp_path/'cache'))
    monkeypatch.setattr(media_decode,'audio_chunks',lambda *a:iter([(0,np.zeros(16000),16000)]))
    request=dict(component='qwen3-asr-0.6b',input_path='fixture',input_sha256='input',model_root='fixture',
                 model_identity='model',language='auto',device='cpu',threads=4)
    identity={key:request[key] for key in ('input_sha256','model_identity','language','device','threads')}
    identity.update(engine=ASR_ENGINE,chunk_seconds=30,max_tokens=1024,language_contract=3,
        implementation='5f6bc27756b2ed4f87fd30ec4ed45a7fb784c9e60c172908c1263de0075f6bf5')
    old=OperationCheckpoint(media_cache_subdir('asr_checkpoints'),identity,identity)
    old.write({'chunks':[{'text':'previous speech','language':'en','start_s':0,'end_s':1,'kind':'audio_chunk'}]})
    class Model:
        def create_stream(self):pytest.fail('completed compatible chunk must not be inferred again')
    monkeypatch.setattr(worker,'_load_asr_model',lambda *a:(Model(),True))
    assert worker.native_asr(request)['text'] == 'previous speech'


def test_bilibili_server_overflow_remains_available(monkeypatch):
    from collectors.platform import bilibili
    monkeypatch.setattr(bilibili.httpx,'get',lambda *a,**kw:SimpleNamespace(status_code=200,json=lambda:
        {'code':0,'data':{'numPages':2,'result':[{'result_type':'video','data':[
            {'bvid':'BV'+str(i),'title':str(i)} for i in range(3)]}]}}))
    result=bilibili.legacy_search_bilibili('question',page_size=1)
    assert len(result['items']) == 1
    assert len(result['metadata']['acquired_items']) == 3
    assert result['metadata']['continuation']['next_page'] == 2


def test_detail_entry_forwards_timing_and_media_workset(monkeypatch):
    from use_cases.content_detail import request_content_detail
    from runtime.task_service import TaskService
    captured=[]
    monkeypatch.setattr(TaskService,'submit',lambda self, op,payload,**kw:captured.append((payload,kw)) or
        {'task_id':'fixture','status':'queued','platform':'B站','metadata':{}})
    monkeypatch.setattr(TaskService,'result',lambda self,task:{'status':'queued'})
    result=request_content_detail('https://www.bilibili.com/video/BVfixture',infer_platform=lambda u:'B站',
        execute_sync=lambda r:pytest.fail('media must use durable execution'),include_transcript=True,
        task_limits={'overall_timeout_s':1200.0,'progress_timeout_s':60.0},media={'max_frames':2,'frame_times_s':[1.0,10.0],'image_max_side':1280})
    assert result['accepted'] and captured[0][1]['overall_timeout_s'] == 1200
    assert captured[0][1]['progress_timeout_s'] == 60
    assert captured[0][0]['options']['media']['image_max_side'] == 1280


def test_task_payload_reference_rejects_tampering_and_escape(tmp_path):
    import hashlib
    from runtime.task_process import _read_reference
    raw=b'{"text":"source"}'; path=tmp_path/'payload.json';path.write_bytes(raw)
    ref={'file':'payload.json','sha256':hashlib.sha256(raw).hexdigest()}
    assert _read_reference(tmp_path,ref)['text'] == 'source'
    path.write_bytes(b'{"text":"changed"}')
    with pytest.raises(ValueError,match='corrupt'):_read_reference(tmp_path,ref)
    with pytest.raises(ValueError,match='invalid'):_read_reference(tmp_path,{**ref,'file':'../payload.json'})


def test_xhs_requested_twenty_is_not_silently_reduced_to_ten(monkeypatch):
    import server
    monkeypatch.delenv('KR_XHS_DEFAULT_SEARCH_LIMIT',raising=False)
    assert server.XHS_DEFAULT_SEARCH_LIMIT == 20
    monkeypatch.setattr(server,'_record_search_evidence',lambda *a:None)
    monkeypatch.setattr(server,'_xhs_budget_state',lambda:{})
    monkeypatch.setattr(server,'_cached_search',lambda *a,**kw:{'items':[],'total':0})
    result=server.search_xiaohongshu('fixture',limit=20)
    assert result['metadata']['requested_limit'] == result['metadata']['effective_limit'] == 20


def test_limit_catalog_explains_enforced_vs_advisory():
    from runtime.cost_latency import acquisition_limits_catalog
    info=acquisition_limits_catalog()
    assert info['task']['overall_timeout_s'] == 86400
    assert info['payload']['referenced_bytes'] == 32*1024*1024
    assert info['declaration_boundary']


def test_expired_youtube_cursor_is_not_empty_or_automatically_retryable(monkeypatch):
    import httpx
    from collectors.platform import youtube
    monkeypatch.setattr(youtube,'youtube_api_key',lambda:'fixture')
    response=httpx.Response(400,request=httpx.Request('GET','https://www.googleapis.com/youtube/v3/search'),
                            json={'error':{'errors':[{'reason':'invalidPageToken'}]}})
    def reject(*a):response.raise_for_status()
    monkeypatch.setattr(youtube,'_youtube_get',reject)
    result=youtube.search_youtube('question',page_token='expired')
    assert result['error']['type']=='invalid_page_token' and not result['error']['retryable']
    assert result['metadata']['continuation']['status']=='cursor_rejected'


def test_unsupported_search_continuation_does_not_claim_exhaustion(monkeypatch):
    import server
    monkeypatch.setattr(server.evidence_store,'append_search',lambda **kw:None)
    response={'items':[]}
    server._record_search_evidence('fixture','question',response)
    assert response['metadata']['continuation']['status']=='continuation_unavailable'
    assert response['metadata']['continuation']['source_exhausted'] is False


def test_youtube_http_cursor_rejection_is_classified_before_retry(monkeypatch):
    import httpx
    from collectors.platform import youtube
    calls=[]
    class Client:
        def __init__(self, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def get(self, url, **kw):
            calls.append(url)
            return httpx.Response(400, json={'error':{'errors':[{'reason':'invalidPageToken'}]}})
    monkeypatch.setattr(youtube.httpx, 'Client', Client)
    monkeypatch.setattr(youtube, 'get_httpx_proxy', lambda:None)
    monkeypatch.setattr(youtube, 'youtube_api_key', lambda:'fixture')
    result=youtube.search_youtube('question', page_token='expired')
    assert result['error']['type']=='invalid_page_token'
    assert len(calls)==1 and result['error']['retryable'] is False
