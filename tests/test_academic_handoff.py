"""学术发现、固定文献卡、独立全文和公共处理的交接合同。"""
import json
from dataclasses import replace
import httpx
import pytest
from academic_providers.models import AcademicWork, AcademicSearchRequest


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    for key in ('KR_STATE_DIR', 'KR_LOG_DIR', 'KR_EVIDENCE_ROOT'):
        monkeypatch.setenv(key, str(tmp_path / key))
    monkeypatch.setenv('KR_RUNTIME_LEASE_DB_PATH', str(tmp_path / 'leases.sqlite3'))
    from academic_providers import service
    service._CACHE.clear()
    monkeypatch.setattr(service, '_rate_limit', lambda *_: None)
    monkeypatch.setattr(service, 'academic_provider_status', lambda: {})


def test_fixed_card_excludes_observation_time_and_task_but_separates_versions():
    from academic_providers.identity import save_work, load_work, work_identity
    a = AcademicWork('Paper', 'https://arxiv.org/abs/1706.03762v7',
                     source_database='arxiv', version='7')
    b = replace(a, retrieved_at='tomorrow', raw={'query': 'different', 'fulltext_task': {'task_id': 'other'}})
    assert save_work(a) == save_work(b)
    assert work_identity(replace(a, url=a.url[:-1]+'8', version='8'))['edition_id'] != work_identity(a)['edition_id']
    with pytest.raises(ValueError, match='academic_version_url_conflict'):
        work_identity(replace(a, version='8'))
    assert load_work(save_work(a)).version == '7'


def test_legacy_permission_alias_blocks_inline_storage_and_cached_fulltext(monkeypatch):
    from academic_providers.identity import save_work, load_work
    from academic_providers.fulltext import extract_academic_fulltext
    from use_cases.academic_fulltext import request_academic_fulltext, execute
    work = AcademicWork('Restricted', 'https://example.org/p', source_database='nstrs',
        raw={'fulltext_download_allowed': False, 'download_allowed': True, 'fulltext_text': 'restricted text'})
    reference = save_work(work)
    restored = load_work(reference)
    assert restored.title == 'Restricted'  # Bibliographic cards remain allowed.
    assert restored.raw['download_allowed'] is False
    assert 'fulltext_inline_ref' not in restored.raw
    result = extract_academic_fulltext('nstrs', work.url, work=work)
    assert result.degraded_reason == 'academic_fulltext_permission_denied'
    monkeypatch.setattr('use_cases.academic_fulltext._reuse', lambda _: pytest.fail('permission must precede cache'))
    with pytest.raises(ValueError, match='permission_denied'):
        request_academic_fulltext(reference)
    with pytest.raises(ValueError, match='permission_denied'):
        execute({'work_reference': reference['version_id']})


def test_restored_old_task_checks_permissions_before_cache_lookup(monkeypatch):
    from dataclasses import asdict
    from use_cases.academic_fulltext import execute
    work = AcademicWork('Restricted', 'https://example.org/p', source_database='nstrs',
                        raw={'fulltext_download_allowed': False})
    monkeypatch.setattr('use_cases.academic_fulltext._reuse', lambda _: pytest.fail('old payload bypassed permission gate'))
    with pytest.raises(ValueError, match='permission_denied'):
        execute({'work': asdict(work)})


def test_search_cache_never_carries_another_research_task(monkeypatch):
    from academic_providers import service
    from runtime.tool_trace import current_tool_trace, set_current_tool_trace
    class Provider:
        def search(self, request):
            return [AcademicWork('Cancer treatment', 'https://example.org/p', source_database='europepmc')]
    monkeypatch.setattr(service, '_providers', lambda: {'europepmc': Provider()})
    seen = []
    def submit(reference, **kw):
        seen.append(kw['research_task_id'])
        return {'task_id': kw['research_task_id'], 'status': 'queued'}
    monkeypatch.setattr('use_cases.academic_fulltext.request_academic_fulltext', submit)
    previous = current_tool_trace()
    try:
        results = []
        for scope in ('research-one', 'research-two'):
            set_current_tool_trace({'research_task_id': scope})
            results.append(service.search_academic_metadata(AcademicSearchRequest('Cancer', provider='europepmc',
                options={'resolve_fulltext': True})))
        assert results[1].metadata['cache']['hit']
        assert seen == ['research-one', 'research-two']
        assert results[1].items[0].raw['fulltext_task']['task_id'] == 'research-two'
    finally:
        set_current_tool_trace(previous)


def test_cancelled_http_discovery_does_not_launch_browser(monkeypatch):
    from academic_providers.pubscholar import PubScholarProvider
    from runtime.execution_context import ExecutionStopped
    p = PubScholarProvider()
    monkeypatch.setattr(p, '_search_with_httpx', lambda **kw: (_ for _ in ()).throw(ExecutionStopped('cancelled')))
    monkeypatch.setattr(p, '_search_with_browser', lambda **kw: pytest.fail('cancel must not fall back'))
    with pytest.raises(ExecutionStopped):
        p.search(AcademicSearchRequest('知识图谱'))


def test_special_reader_processes_every_page_and_propagates_cancel(monkeypatch):
    from academic_providers.pubscholar import verify_pubscholar_pdf_text_url
    from runtime.execution_context import ExecutionStopped
    seen = []
    monkeypatch.setattr('generic_web.url_policy.guarded_fetch', lambda *a, **kw: httpx.Response(200,
        content=b'%PDF-fake', request=httpx.Request('GET', a[0])))
    def process(data, **kw):
        seen.append(kw)
        return {'text': '真实正文' * 200, 'page_count': 12, 'pages_read': 12, 'locators': [],
                'coverage': 'complete_selectable_text', 'ocr_status': 'not_needed'}
    monkeypatch.setattr('runtime.pdf_text.process_pdf', process)
    r = verify_pubscholar_pdf_text_url('https://example.org/paper.pdf')
    assert r['text_probe']['coverage'] == 'complete_selectable_text'
    assert r['text_probe']['page_count'] == 12 and seen[0].get('max_pages') is None
    monkeypatch.setattr('runtime.pdf_text.process_pdf', lambda *a, **kw: (_ for _ in ()).throw(ExecutionStopped('cancelled')))
    with pytest.raises(ExecutionStopped):
        verify_pubscholar_pdf_text_url('https://example.org/paper.pdf')


def test_edition_and_pdf_provenance_survive_public_evidence(monkeypatch):
    from academic_providers.fulltext import AcademicFullTextResult
    from academic_providers.identity import save_work
    from use_cases.academic_fulltext import execute
    from kr_core.evidence_assets import EvidenceAssets
    work = AcademicWork('Paper', 'https://example.org/paper', source_database='arxiv', version='7',
                        doi='10.1234/a', license='CC-BY')
    result = AcademicFullTextResult('arxiv', 'PASS', work.url, 'public_pdf_text', True,
        text='正文' * 600, coverage='partial', provenance={'resolved_url':'https://example.org/p.pdf',
        'input_sha256': 'a'*64, 'pdf_processing': {'ocr_status':'blocked','missing_text_pages':[6]}})
    monkeypatch.setattr('academic_providers.fulltext.extract_academic_fulltext', lambda *a, **kw: result)
    detail = execute({'work_reference':save_work(work), 'timeout_s':600})
    asset = detail['evidence_assets'][0]
    assert asset['provenance']['academic']['version'] == '7'
    assert asset['provenance']['academic']['doi'] == '10.1234/a'
    assert asset['provenance']['pdf_processing']['missing_text_pages'] == [6]
    assert EvidenceAssets().fetch(asset['version_id'])['text'] == detail['content']


def test_cross_research_reuses_fixed_evidence_and_does_not_redownload(monkeypatch):
    from academic_providers.fulltext import AcademicFullTextResult
    from academic_providers.identity import save_work
    from use_cases.academic_fulltext import execute
    calls = []
    def extract(pid, url, **kw):
        calls.append(url)
        return AcademicFullTextResult(pid,'PASS',url,'api_text',True,text='immutable evidence',coverage='partial')
    monkeypatch.setattr('academic_providers.fulltext.extract_academic_fulltext', extract)
    payload = {'work_reference':save_work(AcademicWork('Paper','https://example.org/p',source_database='openalex')),
               'timeout_s':60}
    one, two = execute(payload), execute(payload)
    assert len(calls) == 1
    assert one['evidence_assets'][0]['version_id'] == two['evidence_assets'][0]['version_id']
    assert two['reuse']['network_reverified'] is False
    assert execute({**payload,'refresh':True})['fulltext_status'] == 'PASS' and len(calls)==2


def test_pending_discovery_returns_handle_without_blocking_fast_sources(monkeypatch):
    from academic_providers import service
    from types import SimpleNamespace
    monkeypatch.setattr(service,'_providers',lambda:{'pubscholar':SimpleNamespace(search=lambda _:pytest.fail('must be background'))})
    monkeypatch.setattr('use_cases.academic_discovery.submit_discovery',lambda *a:{'task_id':'slow-source','status':'queued'})
    result=service.search_academic_metadata(AcademicSearchRequest('知识图谱',provider='pubscholar'))
    assert result.error is None and result.metadata['pending_discoveries'][0]['task_id']=='slow-source'
    assert result.metadata['fulltext_resolution']['applied'] is False


def test_false_lexical_filter_keeps_a_readable_metadata_reference(monkeypatch):
    from academic_providers import service
    from academic_providers.identity import load_work
    from types import SimpleNamespace
    rows=[AcademicWork('Bridge design','https://example.org/bridge',source_database='openalex')]
    monkeypatch.setattr(service,'_providers',lambda:{'openalex':SimpleNamespace(search=lambda _:rows)})
    result=service.search_academic_metadata(AcademicSearchRequest('cancer treatment',provider='openalex'))
    assert not result.items
    assert load_work(result.metadata['filtered_candidates'][0]['reference']).title=='Bridge design'


def test_known_different_editions_are_not_merged():
    from academic_providers.service import _dedupe
    work=AcademicWork('Paper','https://example.org/p',doi='10.1234/p',version='submittedVersion')
    assert len(_dedupe([work,replace(work,version='publishedVersion')]))==2


def test_exact_arxiv_id_routes_only_to_identity_lookup():
    from academic_providers.planner import plan_academic_search
    from academic_providers.profile import load_academic_provider_profiles
    plan=plan_academic_search(AcademicSearchRequest('请核查 arXiv:1706.03762v7'),'auto',load_academic_provider_profiles())
    assert plan.provider_order==['arxiv']


def test_datacite_doi_is_resolved_after_crossref_not_found(monkeypatch):
    from academic_providers.crossref import CrossrefProvider
    seen=[]
    original=httpx.Client
    def respond(req):
        seen.append(str(req.url))
        if 'crossref.org' in req.url.host:
            return httpx.Response(404)
        return httpx.Response(200,json={'data':{'attributes':{'doi':'10.1234/dataset','titles':[{'title':'Data'}],
            'url':'https://example.org/data','types':{'resourceTypeGeneral':'Dataset'}}}})
    monkeypatch.setattr(httpx,'Client',lambda **kw:original(transport=httpx.MockTransport(respond),**kw))
    rows=CrossrefProvider().search(AcademicSearchRequest('请核查 DOI 10.1234/dataset'))
    assert len(seen)==2 and rows[0].raw['metadata_agency']=='datacite'
    assert rows[0].doi=='10.1234/dataset'


def test_citation_import_requires_frozen_approved_input_and_pages(tmp_path):
    import hashlib
    from academic_providers.citation_import import CitationImportProvider, CitationImportError
    path=tmp_path/'文献.ris'
    path.write_text(''.join(f'TY  - JOUR\nTI  - 论文{i}\nER  -\n' for i in range(3)),encoding='utf-8')
    provider=CitationImportProvider()
    with pytest.raises(CitationImportError,match='approved_input_root'):
        provider.search(AcademicSearchRequest(str(path)))
    opts={'approved_input_root':str(tmp_path),'input_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'source_offset':1}
    rows=provider.search(AcademicSearchRequest(str(path),limit=1,options=opts))
    assert rows[0].title=='论文1' and rows.continuation['source_offset']==2
    path.write_text('changed',encoding='utf-8')
    with pytest.raises(CitationImportError,match='changed'):
        provider.search(AcademicSearchRequest(str(path),options=opts))


def test_jats_preserves_table_formula_caption_and_references():
    from bs4 import BeautifulSoup
    from academic_providers.structured_text import academic_blocks
    soup=BeautifulSoup('<article><body><sec><title>方法</title><p>正文</p><table-wrap><caption>表一</caption>'
        '<table><tr><td>甲</td><td>乙</td></tr></table></table-wrap><disp-formula><tex-math>E=mc^2</tex-math>'
        '</disp-formula></sec></body><back><ref-list><ref>引文一</ref></ref-list></back></article>','xml')
    blocks=academic_blocks(soup,soup.body,is_xml=True)
    assert {'heading','paragraph','caption','table','formula','reference'} <= {b['kind'] for b in blocks}
    assert next(b['text'] for b in blocks if b['kind']=='table')=='| 甲 | 乙 |'


def test_cannot_pass_a_content_asset_as_trusted_academic_card():
    from kr_core.evidence_assets import EvidenceAssets
    from academic_providers.identity import load_work
    asset=EvidenceAssets().put('https://example.org',json.dumps({'title':'fake','url':'https://other.org'}))
    with pytest.raises(ValueError,match='not_an_academic_card'):
        load_work(asset['version_id'])


def test_pdf_text_coverage_does_not_promote_sparse_image_page_to_complete():
    from io import BytesIO
    from pypdf import PdfWriter
    from pypdf.generic import DictionaryObject,NameObject,NumberObject,DecodedStreamObject
    from runtime.pdf_text import extract_pdf_text
    writer=PdfWriter()
    page=writer.add_blank_page(width=600,height=800)
    font=DictionaryObject({NameObject('/Type'):NameObject('/Font'),NameObject('/Subtype'):NameObject('/Type1'),NameObject('/BaseFont'):NameObject('/Helvetica')})
    image=DecodedStreamObject()
    image.set_data(b'\x00')
    image.update({NameObject('/Subtype'):NameObject('/Image'),NameObject('/Width'):NumberObject(600),NameObject('/Height'):NumberObject(800)})
    page[NameObject('/Resources')]=DictionaryObject({NameObject('/Font'):DictionaryObject({NameObject('/F1'):writer._add_object(font)}),NameObject('/XObject'):DictionaryObject({NameObject('/I1'):writer._add_object(image)})})
    stream=DecodedStreamObject()
    stream.set_data(b'BT /F1 12 Tf 20 760 Td (Header only) Tj ET')
    page[NameObject('/Contents')]=writer._add_object(stream)
    output=BytesIO()
    writer.write(output)
    parsed=extract_pdf_text(output.getvalue())
    assert parsed['coverage']=='partial' and parsed['suspicious_text_pages']==[1]
    assert parsed['quality_warnings'][0]['type']=='image_with_sparse_text'


def test_common_pdf_cache_reuses_bytes_and_resumes_only_failed_ocr_pages(monkeypatch):
    from runtime.pdf_text import process_pdf
    calls=[]
    parsed={'text':'selectable', 'page_count':3, 'pages_read':3, 'truncated':False,
        'document_blocks':[{'page':1,'kind':'paragraph','text':'selectable'}], 'locators':[],
        'empty_pages':[2,3], 'suspicious_text_pages':[], 'coverage':'partial'}
    monkeypatch.setattr('runtime.pdf_worker.extract_isolated',lambda *a,**kw:dict(parsed))
    def ocr(source, **kw):
        calls.append(kw['page_numbers'])
        page=2 if len(calls)==1 else 3
        return {'document_blocks':[{'page':page,'text':f'OCR page {page}'}], 'model':'approved-test',
            'page_errors':[{'page':3,'type':'temporary'}] if page==2 else []}
    monkeypatch.setattr('runtime.cloud_ocr.run_cloud_ocr',ocr)
    first=process_pdf(b'%PDF-same')
    second=process_pdf(b'%PDF-same')
    third=process_pdf(b'%PDF-same')
    assert first['missing_text_pages']==[3]
    assert second['ocr_pages']==[2,3] and second['coverage']=='complete_text_and_ocr'
    assert calls==[[2,3],[3]] and third['processing_reused'] is True
    assert second['text']==third['text'] and 'selectable' in third['text']


def test_internal_academic_cards_and_processing_snapshots_do_not_pollute_knowledge():
    from kr_core.evidence_assets import EvidenceAssets
    from runtime.knowledge import search, Search
    assets=EvidenceAssets()
    for kind in ('academic_card','academic_inline','document_processing','content'):
        assets.put('https://example.org/'+kind, 'unique searchable text', kind=kind)
    assert [a['kind'] for a in assets.search('searchable')]==['content']
    result=search(assets,Search(action='search',query='searchable'))
    assert len(result['items'])==1 and result['items'][0]['asset']['version_id']==assets.search('searchable')[0]['version_id']


def test_background_discovery_executes_and_records_with_keyword_contract(monkeypatch):
    from use_cases.academic_discovery import execute
    from academic_providers.models import AcademicSearchResponse
    seen=[]
    monkeypatch.setattr('academic_providers.service.search_academic_metadata',lambda req:AcademicSearchResponse(req.query,'pubscholar'))
    monkeypatch.setattr('kr_core.evidence_store.EvidenceStore.append_academic_search',lambda self,**kw:seen.append(kw))
    result=execute({'query':'test','provider':'pubscholar'})
    assert seen==[{'query':'test','response':result}]


def test_pdf_extraction_runs_in_owned_memory_bounded_process():
    from io import BytesIO
    from pypdf import PdfWriter
    from runtime.pdf_worker import extract_isolated
    writer=PdfWriter()
    writer.add_blank_page(width=400,height=600)
    output=BytesIO()
    writer.write(output)
    parsed=extract_isolated(output.getvalue(),timeout_s=15)
    assert parsed['page_count']==1 and parsed['empty_pages']==[1]
    assert parsed['extraction_boundary']['memory_limit_bytes']==768*1024*1024


def test_pmc_exact_identifier_is_not_filtered_as_topic_text():
    from academic_providers.relevance import has_query_evidence
    assert has_query_evidence('PMC7092803',AcademicWork('Unrelated title','https://example.org',raw={'pmcid':'PMC7092803'}))
    assert not has_query_evidence('PMC7092803',AcademicWork('PMC7092803 topic','https://example.org',raw={'pmcid':'PMC1'}))


def test_same_url_different_academic_editions_remain_distinct_candidates():
    from runtime.research_index import ResearchIndex
    from academic_providers.identity import save_work
    index = ResearchIndex()
    work = AcademicWork('Paper', 'https://example.org/same', version='submittedVersion')
    rows = []
    for edition in ('submittedVersion', 'publishedVersion'):
        item = replace(work, version=edition)
        rows.append({**item.to_dict(), 'raw': {'fulltext_ref':save_work(item)}})
    index.candidates('research', rows, 'academic_literature_ecology', 'receipt')
    with index.connection() as conn:
        result = [json.loads(row[0]) for row in conn.execute("SELECT value FROM current WHERE kind='candidate'")]
    assert len(result) == 2
    assert {row['version'] for row in result} == {'submittedVersion', 'publishedVersion'}


@pytest.mark.parametrize('permission', ['read_allowed','download_allowed','evidence_storage_allowed'])
def test_explicit_denial_never_starts_persistent_processing(monkeypatch, permission):
    from academic_providers.identity import save_work
    from academic_providers.fulltext import extract_academic_fulltext
    from use_cases.academic_fulltext import request_academic_fulltext
    from kr_core.evidence_assets import EvidenceAssets
    work = AcademicWork('Paper','https://example.org/p',source_database='pubscholar',
        raw={permission:False, 'fulltext_text':'sensitive body'})
    monkeypatch.setattr('academic_providers.fulltext.instantiate_academic_providers',lambda *a:pytest.fail('must reject before provider'))
    monkeypatch.setattr('runtime.task_service.TaskService.submit',lambda *a,**kw:pytest.fail('must reject before task'))
    result = extract_academic_fulltext('pubscholar',work.url,work=work)
    assert not result.text and ('not_authorized' in result.degraded_reason or 'permission_denied' in result.degraded_reason)
    with pytest.raises(ValueError,match='not_authorized|permission_denied'):
        request_academic_fulltext(save_work(work))
    if permission == 'evidence_storage_allowed':
        with EvidenceAssets().connection() as conn:
            assert conn.execute("SELECT count(*) FROM assets WHERE kind IN ('academic_inline','content','document_processing')").fetchone()[0] == 0


def test_metadata_merge_does_not_upgrade_permissions_or_assert_same_edition():
    from academic_providers.service import _dedupe
    a=AcademicWork('Paper','https://example.org/a',doi='10.1234/p',raw={'cloud_processing_allowed':True})
    b=replace(a,url='https://example.org/b',raw={'cloud_processing_allowed':False})
    merged=_dedupe([a,b])[0]
    assert merged.raw['cloud_processing_allowed'] is False
    assert merged.raw['edition_merge_verification']=='unverified'


def test_api_discovery_overlaps_and_records_whole_admitted_batch(monkeypatch):
    import threading
    from academic_providers import service
    from academic_providers.planner import AcademicRoutePlan, AcademicQueryIntent
    from runtime.tool_trace import current_tool_trace, set_current_tool_trace
    barrier = threading.Barrier(2)
    seen = []
    class Provider:
        def __init__(self,name): self.name=name
        def search(self,request):
            barrier.wait(timeout=3)
            seen.append((self.name,current_tool_trace().get('research_task_id')))
            return [AcademicWork('Cancer '+self.name,f'https://example.org/{self.name}',source_database=self.name)]
    monkeypatch.setattr(service,'_providers',lambda:{name:Provider(name) for name in ('openalex','crossref')})
    monkeypatch.setattr(service,'_provider_plan',lambda *a:AcademicRoutePlan(['openalex','crossref'],AcademicQueryIntent('en')))
    previous=current_tool_trace()
    try:
        set_current_tool_trace({'research_task_id':'parallel-research'})
        result=service.search_academic_metadata(AcademicSearchRequest('cancer',provider='auto',limit=1,options={'api_parallelism':2}))
    finally:
        set_current_tool_trace(previous)
    assert set(seen)=={('openalex','parallel-research'),('crossref','parallel-research')}
    assert result.metadata['attempted_providers']==['openalex','crossref']
    assert len(result.metadata['additional_candidates'])==1


def test_retry_wait_observes_cancel_before_retry(monkeypatch):
    from runtime.execution_context import ExecutionStopped
    from academic_providers.rate_limit import cooperative_wait
    monkeypatch.setattr('runtime.execution_context.check_execution',lambda:(_ for _ in ()).throw(ExecutionStopped('cancelled')))
    with pytest.raises(ExecutionStopped):
        cooperative_wait(8)


def test_api_discovery_never_imports_optional_browser_and_status_stays_small(monkeypatch):
    import builtins
    from academic_providers import service
    original = builtins.__import__
    def guarded(name,*args,**kw):
        if name.startswith('playwright'):
            raise ImportError('browser intentionally absent')
        return original(name,*args,**kw)
    monkeypatch.setattr(builtins,'__import__',guarded)
    monkeypatch.setattr('academic_providers.openalex.OpenAlexProvider.search',lambda *a:[
        AcademicWork('Cancer','https://example.org/p',source_database='openalex')])
    monkeypatch.setattr(service,'academic_provider_status',lambda:pytest.fail('must not build full source catalog'))
    result=service.search_academic_metadata(AcademicSearchRequest('cancer',provider='openalex'))
    assert len(result.items)==1
    assert set(result.metadata['provider_status'])=={'openalex'}
    assert len(json.dumps(result.metadata['provider_status']))<1000


def test_slow_discovery_preserves_explicit_fulltext_shortcut_but_only_submits(monkeypatch):
    from types import SimpleNamespace
    from academic_providers import service
    from use_cases.academic_discovery import submit_discovery, execute
    captured = []
    monkeypatch.setattr('runtime.task_service.TaskService.submit',
        lambda self, operation, payload, **kw: captured.append(payload) or {'task_id':'discovery','status':'queued'})
    submit_discovery(AcademicSearchRequest('cancer',provider='pubscholar',options={
        'resolve_fulltext':True,'fulltext_top_k':1,'fulltext_deadline_s':600}), 'pubscholar')
    monkeypatch.setattr(service,'_providers',lambda:{'pubscholar':SimpleNamespace(search=lambda request:[
        AcademicWork('Cancer treatment','https://example.org/p',source_database='pubscholar')])})
    submitted=[]
    monkeypatch.setattr('use_cases.academic_fulltext.request_academic_fulltext',lambda reference,**kw:
        submitted.append(kw) or {'task_id':'independent-fulltext','status':'queued'})
    monkeypatch.setattr('academic_providers.fulltext.extract_academic_fulltext',lambda *a,**kw:pytest.fail('discovery must not execute fulltext'))
    result=execute(captured[0])
    assert submitted[0]['timeout_s']==600
    assert result['items'][0]['raw']['fulltext_task']['task_id']=='independent-fulltext'
    assert result['metadata']['fulltext_resolution']['resolved_count']==0
