import httpx
import pytest
from PIL import Image

from runtime import optional_media

pytestmark = pytest.mark.usefixtures("paid_test_policy")


@pytest.fixture(autouse=True)
def isolated_pdf_snapshots(monkeypatch, tmp_path):
    monkeypatch.setenv('KR_EVIDENCE_ROOT', str(tmp_path / 'assets'))
    monkeypatch.setenv('KR_STATE_DIR', str(tmp_path / 'state'))


def source_request(tmp_path):
    path = tmp_path / 'scan.png'
    Image.new('RGB', (100, 100), 'white').save(path)
    return {'input_path': str(path), 'approved_input_root': str(tmp_path),
            'input_sha256': optional_media.file_sha(path), 'component': 'auto'}


def test_missing_fee_authority_never_uploads(tmp_path, monkeypatch):
    from runtime import cloud_ocr
    monkeypatch.setenv("SILICONFLOW_API_KEY", "fixture")
    monkeypatch.setenv("KR_PAID_POLICY_PATH", str(tmp_path / "absent"))
    class Client:
        def post(self, *args, **kwargs):
            pytest.fail("upload without explicit ceiling")
    with pytest.raises(cloud_ocr.CloudOcrError, match="paid_policy_invalid"):
        cloud_ocr.recognize_image(Client(), b"fixture", timeout=3)


def test_auto_ocr_uses_cloud_without_local_models_and_keeps_evidence(tmp_path, monkeypatch):
    from runtime import cloud_ocr
    monkeypatch.setenv('KR_STATE_DIR', str(tmp_path / 'state'))
    monkeypatch.setenv('SILICONFLOW_API_KEY', 'private-key')
    calls = []
    def handler(request):
        import json
        body = json.loads(request.content)
        calls.append(body)
        assert request.headers['authorization'] == 'Bearer private-key'
        assert body['model'] == 'PaddlePaddle/PaddleOCR-VL-1.5'
        assert body['messages'][0]['content'][0]['type'] == 'image_url'
        assert body['messages'][0]['content'][1] == {'type': 'text', 'text': 'OCR:'}
        assert body['frequency_penalty'] == 0.5
        return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {'content': 'document text'}}],
                                         'usage': {'total_tokens': 12}})
    original = httpx.Client
    monkeypatch.setattr(cloud_ocr.httpx, 'Client', lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))
    monkeypatch.setattr(optional_media, 'validate_receipt', lambda component: pytest.fail('cloud OCR must not load a local model'))
    result = optional_media.run_optional(source_request(tmp_path))
    assert result['text'] == 'document text'
    assert result['usage']['total_tokens'] == 12
    assert result['evidence_assets']
    assert len(calls) == 1
    assert 'private-key' not in str(result)


@pytest.mark.parametrize('status', [401, 429, 503])
def test_cloud_failure_never_falls_back_to_cpu(tmp_path, monkeypatch, status):
    from runtime import cloud_ocr
    monkeypatch.setenv('SILICONFLOW_API_KEY', 'private-key')
    original = httpx.Client
    monkeypatch.setattr(cloud_ocr.httpx, 'Client', lambda **kwargs: original(transport=httpx.MockTransport(lambda request: httpx.Response(status)), **kwargs))
    monkeypatch.setattr(optional_media, 'validate_receipt', lambda component: pytest.fail('no automatic CPU fallback'))
    with pytest.raises(cloud_ocr.CloudOcrError, match='cloud_ocr_http_' + str(status)):
        optional_media.run_optional(source_request(tmp_path))


def test_missing_cloud_key_fails_before_any_upload(tmp_path, monkeypatch):
    from runtime.cloud_ocr import CloudOcrError
    monkeypatch.delenv('SILICONFLOW_API_KEY', raising=False)
    with pytest.raises(CloudOcrError, match='siliconflow_api_key_missing'):
        optional_media.run_optional(source_request(tmp_path))


def test_scanned_pdf_renders_each_page_without_local_ocr(tmp_path):
    from runtime.cloud_ocr import document_images
    source = tmp_path / 'scan.pdf'
    Image.new('RGB', (200, 100), 'white').save(source, 'PDF')
    pages = list(document_images(source))
    assert len(pages) == 1 and pages[0][0] == 1 and pages[0][1][:2] == b'\xff\xd8'


def test_retired_offline_selection_is_rejected(tmp_path):
    payload = source_request(tmp_path)
    payload['component'] = 'paddleocr-vl-1.6'
    with pytest.raises(ValueError, match='unsupported_optional_component'):
        optional_media.select_component(payload)


def test_cloud_submit_requires_key_without_local_install_receipt(tmp_path, monkeypatch):
    from runtime.task_service import TaskService
    from runtime.tasks import TaskStore
    from runtime.cloud_ocr import CloudOcrError
    monkeypatch.delenv('SILICONFLOW_API_KEY', raising=False)
    monkeypatch.setattr(optional_media, 'validate_receipt', lambda component: pytest.fail('cloud must bypass local install'))
    service = TaskService(TaskStore(str(tmp_path / 'tasks.sqlite3')))
    with pytest.raises(CloudOcrError, match='siliconflow_api_key_missing'):
        service.submit('optional_media', source_request(tmp_path))


def test_truncated_generation_preserves_safe_diagnostics_and_usage(monkeypatch):
    from runtime.cloud_ocr import recognize_image, CloudOcrError
    monkeypatch.setenv('SILICONFLOW_API_KEY', 'private-key')
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={
        'choices': [{'finish_reason': 'length', 'message': {'content': '1. ' * 1200}}],
        'usage': {'prompt_tokens': 1300, 'completion_tokens': 4096, 'total_tokens': 5396,
                  'untrusted_field': 'private-key'}}))) as client:
        with pytest.raises(CloudOcrError, match='cloud_ocr_output_truncated') as caught:
            recognize_image(client, b'image', timeout=2)
    diagnostic = caught.value.diagnostics
    assert diagnostic['finish_reason'] == 'length'
    assert diagnostic['usage']['completion_tokens'] == 4096
    assert diagnostic['output_chars'] == 3600
    assert diagnostic['elapsed_s'] >= 0
    assert 'private-key' not in str(diagnostic)
    assert '1. 1.' not in str(diagnostic)


def test_success_keeps_provider_identity_without_prompt_or_credentials(monkeypatch):
    from runtime.cloud_ocr import recognize_image, CLOUD_OCR_MODEL
    monkeypatch.setenv('SILICONFLOW_API_KEY', 'private-key')
    def response(request):
        return httpx.Response(200, headers={'x-siliconcloud-trace-id':'trace-test-123'}, json={
            'model':CLOUD_OCR_MODEL,'choices':[{'finish_reason':'stop','message':{'content':'图内文字'}}],
            'usage':{'total_tokens':12},'untrusted':'private-key'})
    provenance={}
    with httpx.Client(transport=httpx.MockTransport(response)) as client:
        text,usage=recognize_image(client,b'image',timeout=2,provenance=provenance)
    assert text=='图内文字' and usage['total_tokens']==12
    assert provenance['trace_id']=='trace-test-123' and provenance['reported_model']==CLOUD_OCR_MODEL
    assert 'private-key' not in str(provenance) and 'untrusted' not in provenance


def test_timeout_has_request_trace_for_provider_lookup_without_replay(monkeypatch):
    from runtime.cloud_ocr import recognize_image, CloudOcrError
    monkeypatch.setenv('SILICONFLOW_API_KEY', 'private-key')
    calls = []
    def timeout(request):
        calls.append(request)
        raise httpx.ReadTimeout('no response', request=request)
    with httpx.Client(transport=httpx.MockTransport(timeout)) as client:
        with pytest.raises(CloudOcrError) as caught:
            recognize_image(client, b'image', timeout=2)
    assert len(calls) == 1
    trace = calls[0].headers['X-Trace-Id']
    assert caught.value.diagnostics['request_trace_id'] == trace
    assert caught.value.diagnostics['submitted_image_sha256']
    assert 'private-key' not in str(caught.value.diagnostics)


def test_cloud_read_wait_is_bounded_by_remaining_task_budget(monkeypatch):
    from runtime import cloud_ocr
    monkeypatch.setenv('SILICONFLOW_API_KEY', 'fixture')
    monkeypatch.setattr(cloud_ocr, 'document_images', lambda *a, **kw: iter([(1, b'a')]))
    waits = []
    def recognize(client, image, **kw):
        waits.append(kw['timeout'])
        return 'text', {}
    monkeypatch.setattr(cloud_ocr, 'recognize_image', recognize)
    cloud_ocr.run_cloud_ocr('unused', timeout_s=300)
    cloud_ocr.run_cloud_ocr('unused', timeout_s=12)
    assert waits[0].read == 180 and waits[0].connect <= 10
    assert 0 < waits[1].read <= 12


def mixed_pdf():
    from io import BytesIO
    from pypdf import PdfWriter
    from pypdf.generic import DictionaryObject,NameObject,DecodedStreamObject
    writer=PdfWriter()
    for value in ('First selectable page', '', 'Last selectable page'):
        page=writer.add_blank_page(200,100)
        if value:
            font=DictionaryObject({NameObject('/Type'):NameObject('/Font'),NameObject('/Subtype'):NameObject('/Type1'),NameObject('/BaseFont'):NameObject('/Helvetica')})
            page[NameObject('/Resources')]=DictionaryObject({NameObject('/Font'):DictionaryObject({NameObject('/F1'):font})})
            stream=DecodedStreamObject();stream.set_data(f'BT /F1 12 Tf 10 50 Td ({value}) Tj ET'.encode())
            page[NameObject('/Contents')]=stream
    out=BytesIO();writer.write(out)
    return out.getvalue()


def test_common_pdf_ocr_only_missing_page_and_archives_aligned_locators(tmp_path,monkeypatch):
    from runtime import cloud_ocr,pdf_text
    from kr_core.evidence_store import EvidenceStore
    from kr_core.evidence_assets import EvidenceAssets
    monkeypatch.setenv('KR_EVIDENCE_ROOT',str(tmp_path/'assets'))
    calls=[]
    def recognize(source,**kw):
        calls.append(kw['page_numbers'])
        return {'document_blocks':[{'page':2,'text':'Scanned middle page'}], 'model':'fixture-ocr','usage':{},'page_errors':[]}
    monkeypatch.setattr(cloud_ocr,'run_cloud_ocr',recognize)
    result=pdf_text.process_pdf(mixed_pdf())
    assert calls==[[2]] and result['ocr_pages']==[2]
    assert result['coverage']=='complete_text_and_ocr' and result['empty_pages']==[]
    assert [x['page'] for x in result['locators']]==[1,2,3]
    assert all(result['text'][loc['start']:loc['end']]==block['text'] for loc,block in zip(result['locators'],result['document_blocks']))
    detail={'content':result['text'],'document_blocks':result['document_blocks'],'metadata':result}
    EvidenceStore().record_detail(platform='web',url='https://example.org/mixed.pdf',response=detail)
    ref=detail['evidence_assets'][0]
    assert ref['coverage']=='complete_text_and_ocr'
    assert EvidenceAssets().fetch(ref['version_id'])['text']==result['text']


def test_pdf_unavailable_ocr_keeps_original_text_and_gap(monkeypatch):
    from runtime import cloud_ocr,pdf_text
    def fail(*a,**kw):raise cloud_ocr.CloudOcrError('siliconflow_api_key_missing')
    monkeypatch.setattr(cloud_ocr,'run_cloud_ocr',fail)
    result=pdf_text.process_pdf(mixed_pdf())
    assert result['text']=='First selectable page\nLast selectable page'
    assert result['coverage']=='partial' and result['missing_text_pages']==[2]
    assert result['ocr_errors']==[{'type':'siliconflow_api_key_missing'}]


def test_pdf_ocr_cannot_displace_selectable_pages_or_break_text_budget(monkeypatch):
    from runtime import cloud_ocr,pdf_text
    monkeypatch.setattr(cloud_ocr,'run_cloud_ocr',lambda *a,**kw:{'document_blocks':[{'page':2,'text':'x'*100}], 'model':'fixture','page_errors':[]})
    result=pdf_text.process_pdf(mixed_pdf(),max_chars=50)
    assert result['text']=='First selectable page\nLast selectable page'
    assert result['missing_text_pages']==[2] and result['ocr_status']=='partial'
    result=pdf_text.extract_pdf_text(mixed_pdf(),max_chars=25)
    assert len(result['text'])<=25 and result['truncated']
    assert all(result['text'][loc['start']:loc['end']]==block['text'] for loc,block in zip(result['locators'],result['document_blocks']))


def test_selected_missing_page_budget_is_independent_of_total_pages(tmp_path):
    from pypdf import PdfWriter
    from runtime.cloud_ocr import document_images,CloudOcrError
    writer=PdfWriter()
    for _ in range(25):writer.add_blank_page(100,100)
    path=tmp_path/'long.pdf';writer.write(path)
    assert [n for n,_ in document_images(path,page_numbers=[23])]==[23]
    with pytest.raises(CloudOcrError,match='page_budget'):
        list(document_images(path,page_numbers=list(range(1,22))))


def test_partial_ocr_keeps_completed_pages_without_replaying_failure(monkeypatch):
    from runtime import cloud_ocr
    monkeypatch.setenv('SILICONFLOW_API_KEY','fixture')
    monkeypatch.setattr(cloud_ocr,'document_images',lambda *a,**kw:iter([(2,b'a'),(4,b'b'),(6,b'c')]))
    calls=[]
    def recognize(client,image,**kw):
        calls.append(image)
        if image==b'b':raise cloud_ocr.CloudOcrError('cloud_ocr_timeout')
        return 'page two',{'total_tokens':12}
    monkeypatch.setattr(cloud_ocr,'recognize_image',recognize)
    result=cloud_ocr.run_cloud_ocr('unused',page_numbers=[2,4,6],allow_partial=True)
    assert calls==[b'a',b'b'] and result['document_blocks'][0]['page']==2
    assert len(result['page_errors'])==1
    assert result['page_errors'][0]['page']==4 and result['page_errors'][0]['type']=='cloud_ocr_timeout'
    assert result['page_errors'][0]['diagnostics']['completed_pages']==1
    assert result['usage']['total_tokens']==12


def test_pdf_cancellation_is_not_an_ocr_gap(monkeypatch):
    from runtime import cloud_ocr,pdf_text
    from runtime.execution_context import ExecutionStopped
    def stop(*a,**kw):raise ExecutionStopped('cancelled')
    monkeypatch.setattr(cloud_ocr,'run_cloud_ocr',stop)
    with pytest.raises(ExecutionStopped):pdf_text.process_pdf(mixed_pdf())


def test_cancelled_ocr_closes_suspended_pdf_renderer(monkeypatch):
    from runtime import cloud_ocr
    from runtime.execution_context import ExecutionStopped
    monkeypatch.setenv('SILICONFLOW_API_KEY', 'fixture')
    closed = []
    def images(*args, **kwargs):
        try:
            yield 1, b'page'
        finally:
            closed.append(True)
    def stop(*args, **kwargs):
        raise ExecutionStopped('cancelled')
    monkeypatch.setattr(cloud_ocr, 'document_images', images)
    monkeypatch.setattr(cloud_ocr, 'recognize_image', stop)
    with pytest.raises(ExecutionStopped):
        cloud_ocr.run_cloud_ocr('unused', allow_partial=True)
    assert closed == [True]
